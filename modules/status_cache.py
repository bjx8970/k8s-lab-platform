import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_vm_cache = {}
_cache_lock = threading.Lock()
_monitor_started = False
_last_refresh_error = None
_last_build_error = None

REFRESH_INTERVAL = 300


def _get_pve_server_configs():
    from modules.db import list_pve_servers, get_pve_server, get_config
    servers = {}
    for s in list_pve_servers():
        sid = s["id"]
        full = get_pve_server(sid)
        if not full:
            continue
        servers[sid] = {
            "host": full["host"],
            "user": full["user"],
            "token_name": full["token_name"],
            "token_value": full["token_value"],
            "verify_ssl": s.get("verify_ssl", False),
            "port": int(s.get("port", 8006)),
        }
    cfg = get_config("pve")
    if cfg and 0 not in servers:
        servers[0] = {
            "host": cfg["host"],
            "user": cfg["user"],
            "token_name": cfg["token_name"],
            "token_value": cfg["token_value"],
            "verify_ssl": cfg.get("verify_ssl", False),
            "port": int(cfg.get("port", 8006)),
        }
    return servers


def _build_clients(server_configs):
    global _last_build_error
    from modules.pve_client import PVEClient, PVEError
    clients = {}
    errors = []
    for sid, cfg in server_configs.items():
        try:
            client = PVEClient(
                host=cfg["host"], user=cfg["user"],
                token_name=cfg["token_name"], token_value=cfg["token_value"],
                verify_ssl=cfg.get("verify_ssl", False),
                port=int(cfg.get("port", 8006)),
            )
            client.connect()
            clients[sid] = client
        except Exception as e:
            errors.append(f"ID={sid} ({cfg.get('host','?')}): {type(e).__name__}: {e}")
    _last_build_error = "; ".join(errors) if errors else None
    return clients


def _query_single_vm(client, node, vmid, now):
    data = client.get_vm_status(node, vmid)
    key = f"{node}_{vmid}"
    return key, {
        "status": data.get("status", "unknown"),
        "updated_at": now,
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
        for cluster in clusters.values():
            vms = cluster.get("vms")
            if not vms:
                continue
            sid = cluster.get("pve_server_id", 0)
            server_vms.setdefault(sid, []).extend(
                (vm["node"], vm["vmid"]) for vm in vms.values()
            )

        if not server_vms:
            _last_refresh_error = "所有集群均无 VM 节点"
            return

        configs = _get_pve_server_configs()
        if not configs:
            _last_refresh_error = "未找到 PVE 服务器配置，请在 PVE 页面中配置服务器"
            return

        sid_list = list(server_vms.keys())
        found = [s for s in sid_list if s in configs]
        missing = [s for s in sid_list if s not in configs]
        if missing:
            _last_refresh_error = f"集群引用了不存在的 PVE 服务器 ID: {missing}，可用 ID: {list(configs.keys())}"
            return

        clients = _build_clients(configs)
        has_client = [s for s in sid_list if s in clients]
        no_client = [s for s in sid_list if s not in clients]
        if no_client:
            _last_refresh_error = f"PVE 服务器 {no_client} 连接失败"
            if _last_build_error:
                _last_refresh_error += f"（详情: {_last_build_error}）"
            return

        now = time.time()
        new_cache = {}
        total = 0
        fail = 0
        for sid in has_client:
            client = clients[sid]
            vm_list = server_vms[sid]
            with ThreadPoolExecutor(max_workers=10) as pool:
                futures = [
                    pool.submit(_query_single_vm, client, node, vmid, now)
                    for node, vmid in vm_list
                ]
                for f in as_completed(futures):
                    total += 1
                    try:
                        key, entry = f.result()
                        new_cache[key] = entry
                    except Exception:
                        fail += 1

        with _cache_lock:
            _vm_cache.clear()
            _vm_cache.update(new_cache)

        if new_cache:
            _last_refresh_error = None
        elif total == 0:
            _last_refresh_error = "PVE 连接正常但返回为空，请检查集群 VM 信息"
        else:
            _last_refresh_error = f"共 {total} 台 VM, {fail} 台查询失败, {len(new_cache)} 台成功"
    except Exception as e:
        _last_refresh_error = f"{type(e).__name__}: {e}"


def _cache_worker():
    while True:
        time.sleep(REFRESH_INTERVAL)
        try:
            _refresh_cache()
        except Exception:
            pass


def get_vm_status(node, vmid):
    key = f"{node}_{vmid}"
    with _cache_lock:
        entry = _vm_cache.get(key)
        return {"status": entry["status"]} if entry else {"status": "unknown"}


def update_vm_status(node, vmid, status):
    key = f"{node}_{vmid}"
    with _cache_lock:
        _vm_cache[key] = {"status": status, "updated_at": time.time()}


def dump_cache():
    with _cache_lock:
        return {
            "entries": {k: v for k, v in _vm_cache.items()},
            "count": len(_vm_cache),
            "last_refresh": max((e["updated_at"] for e in _vm_cache.values()), default=0),
            "refresh_interval": REFRESH_INTERVAL,
            "last_error": _last_refresh_error,
        }


def trigger_refresh():
    from threading import Thread
    t = Thread(target=_refresh_cache, daemon=True)
    t.start()


def start_monitor():
    global _monitor_started
    if _monitor_started:
        return
    _monitor_started = True
    try:
        _refresh_cache()
    except Exception:
        pass
    t = threading.Thread(target=_cache_worker, daemon=True)
    t.start()
