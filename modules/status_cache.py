import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from modules.vm_identity import vm_identity, vm_identity_key, validate_node, cluster_vm_entries

_vm_cache = {}
_cache_lock = threading.Lock()
_monitor_started = False
_last_refresh_error = None
_last_build_error = None
_on_vm_update = None
_cache_invalidated_at = {}

REFRESH_INTERVAL = 300


def _get_pve_server_configs():
    from modules.db import list_pve_servers, get_pve_server
    servers = {}
    for s in list_pve_servers():
        sid = s["id"]
        try:
            sid, _ = vm_identity(sid, 1)
        except ValueError:
            continue
        full = get_pve_server(sid)
        if not full:
            continue
        servers[sid] = {
            "host": full["host"], "user": full["user"],
            "token_name": full["token_name"], "token_value": full["token_value"],
            "verify_ssl": full.get("verify_ssl", False),
            "port": int(full.get("port", 8006)),
        }
    return servers


def _build_clients(server_configs):
    global _last_build_error
    from modules.pve_client import PVEClient
    clients = {}
    errors = []
    for sid, cfg in server_configs.items():
        try:
            client = PVEClient(
                host=cfg["host"], user=cfg["user"],
                token_name=cfg["token_name"], token_value=cfg["token_value"],
                verify_ssl=cfg.get("verify_ssl", False), port=int(cfg.get("port", 8006)),
            )
            client.connect()
            clients[sid] = client
        except Exception:
            errors.append(f"PVE 服务器 ID={sid} 连接失败")
    _last_build_error = "; ".join(errors) if errors else None
    return clients


def _query_single_vm(server_id, client, node, vmid, now):
    server_id, vmid = vm_identity(server_id, vmid)
    node = validate_node(node)
    data = client.get_vm_status(node, vmid)
    key = vm_identity_key(server_id, vmid)
    return key, {
        "pve_server_id": server_id, "vmid": vmid, "node": node,
        "status": data.get("status", "unknown"), "updated_at": now,
    }


def _refresh_cache():
    global _last_refresh_error
    try:
        from modules.db import load_clusters
        clusters = load_clusters()
        if not clusters:
            _last_refresh_error = "load_clusters 返回空（尚无集群）"
            return
        server_vms = {}
        seen = set()
        for cluster in clusters.values():
            for _, vm in cluster_vm_entries(cluster):
                identity = vm_identity(vm["pve_server_id"], vm["vmid"])
                if identity in seen:
                    raise ValueError("不同集群重复 VM 身份")
                seen.add(identity)
                server_vms.setdefault(identity[0], []).append((vm["node"], identity[1]))
        if not server_vms:
            _last_refresh_error = "所有集群均无 VM 节点"
            return
        configs = _get_pve_server_configs()
        missing = [sid for sid in server_vms if sid not in configs]
        if missing:
            _last_refresh_error = f"集群引用了不存在的 PVE 服务器 ID: {missing}"
            return
        clients = _build_clients(configs)
        no_client = [sid for sid in server_vms if sid not in clients]
        if no_client:
            _last_refresh_error = f"PVE 服务器 {no_client} 连接失败"
            if _last_build_error:
                _last_refresh_error += f"（详情: {_last_build_error}）"
            return
        refresh_started = now = time.time()
        new_cache = {}
        total = fail = 0
        for server_id, vm_list in server_vms.items():
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [pool.submit(_query_single_vm, server_id, clients[server_id], node, vmid, now)
                           for node, vmid in vm_list]
                for future in as_completed(futures):
                    total += 1
                    try:
                        key, entry = future.result()
                        new_cache[key] = entry
                    except Exception:
                        fail += 1
        with _cache_lock:
            recent = {key: value for key, value in _vm_cache.items()
                      if value.get("updated_at", 0) > refresh_started}
            _vm_cache.clear()
            _vm_cache.update({key: value for key, value in new_cache.items()
                              if _cache_invalidated_at.get(key, 0) <= refresh_started})
            _vm_cache.update(recent)
        _last_refresh_error = None if new_cache else ("PVE 连接正常但返回为空，请检查集群 VM 信息" if total == 0 else f"共 {total} 台 VM, {fail} 台查询失败, {len(new_cache)} 台成功")
    except Exception:
        _last_refresh_error = "PVE 状态缓存刷新失败，请检查连接和集群身份"


def _cache_worker():
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            _refresh_cache()
        except Exception:
            pass


def set_on_vm_update(callback):
    global _on_vm_update
    _on_vm_update = callback


def _notify(entry):
    if _on_vm_update:
        try:
            _on_vm_update({field: entry.get(field) for field in
                           ("key", "pve_server_id", "vmid", "node", "status")})
        except Exception:
            pass


def get_vm_status(pve_server_id, vmid, node=None):
    server_id, vmid = vm_identity(pve_server_id, vmid)
    if node is not None:
        node = validate_node(node)
    key = vm_identity_key(server_id, vmid)
    with _cache_lock:
        entry = _vm_cache.get(key)
        if entry:
            return {"key": key, "pve_server_id": server_id, "vmid": vmid,
                    "node": node or entry.get("node"), "status": entry["status"]}
    return {"key": key, "pve_server_id": server_id, "vmid": vmid,
            "node": node, "status": "unknown"}


def update_vm_status(pve_server_id, vmid, status, node=None):
    server_id, vmid = vm_identity(pve_server_id, vmid)
    if node is not None:
        node = validate_node(node)
    key = vm_identity_key(server_id, vmid)
    with _cache_lock:
        previous = _vm_cache.get(key, {})
        entry = {"key": key, "pve_server_id": server_id, "vmid": vmid,
                 "node": node if node is not None else previous.get("node"),
                 "status": status, "updated_at": time.time()}
        _vm_cache[key] = entry
        _cache_invalidated_at.pop(key, None)
    _notify(entry)


def remove_vm_status(pve_server_id, vmid):
    server_id, vmid = vm_identity(pve_server_id, vmid)
    key = vm_identity_key(server_id, vmid)
    with _cache_lock:
        previous = _vm_cache.pop(key, {})
        _cache_invalidated_at[key] = time.time()
    _notify({"key": key, "pve_server_id": server_id, "vmid": vmid,
             "node": previous.get("node"), "status": "unknown"})


def dump_cache():
    with _cache_lock:
        return {
            "entries": {k: v for k, v in _vm_cache.items()}, "count": len(_vm_cache),
            "last_refresh": max((e["updated_at"] for e in _vm_cache.values()), default=0),
            "refresh_interval": REFRESH_INTERVAL, "last_error": _last_refresh_error,
        }


def trigger_refresh():
    from threading import Thread
    Thread(target=_refresh_cache, daemon=True).start()


def start_monitor():
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True
    try:
        _refresh_cache()
    except Exception:
        pass
    threading.Thread(target=_cache_worker, daemon=True).start()
