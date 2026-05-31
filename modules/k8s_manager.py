import io
import secrets
import threading
import time as _time
import uuid
from urllib.parse import quote

import paramiko
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.db import (
    Cluster, backfill_group_student_numbers, delete_cluster_db, get_config, get_group, get_pve_server,
    list_group_members, load_cluster,
    load_clusters, save_cluster, session_scope,
)
from modules.openwrt_client import OpenWrtClient, OpenWrtError
from modules.pve_client import PVEClient, PVEError
from modules.task_queue import scheduler, Task


class K8sError(Exception):
    pass


_task_store = {}
_task_lock = threading.Lock()
_task_cancel_events = {}
_on_task_update = None

_openwrt_locks = {}
_openwrt_locks_lock = threading.Lock()


def _new_task_id():
    return uuid.uuid4().hex[:12]


def set_on_task_update(callback):
    global _on_task_update
    _on_task_update = callback


def _update_task(task_id, status="running", progress=0, message="", result=None, error=None,
                 created_by=None, queue=None):
    with _task_lock:
        entry = _task_store.get(task_id)
        if entry is None:
            entry = {}
            _task_store[task_id] = entry
        entry["status"] = status
        entry["progress"] = progress
        entry["message"] = message
        if result is not None:
            entry["result"] = result
        if error is not None:
            entry["error"] = error
        if created_by is not None:
            entry["created_by"] = created_by
        if queue is not None:
            entry["queue"] = queue
        entry["updated_at"] = _time.time()
        entry.setdefault("logs", []).append({
            "time": _time.strftime("%H:%M:%S"),
            "progress": progress,
            "message": message,
        })
    if _on_task_update:
        _on_task_update(task_id, status, progress, message, entry.get("created_by"), queue)


def _append_log(task_id, message):
    with _task_lock:
        entry = _task_store.get(task_id)
        if entry is None:
            return
        entry["updated_at"] = _time.time()
        entry.setdefault("logs", []).append({
            "time": _time.strftime("%H:%M:%S"),
            "progress": entry.get("progress", 0),
            "message": message,
        })


def get_task_status(task_id):
    with _task_lock:
        return _task_store.get(task_id)


def _cleanup_old_tasks():
    now = _time.time()
    with _task_lock:
        expired = [tid for tid, t in _task_store.items() if now - t.get("updated_at", 0) > 1800]
        for tid in expired:
            del _task_store[tid]
            _task_cancel_events.pop(tid, None)


def cancel_task(task_id):
    with _task_lock:
        entry = _task_store.get(task_id)
        if not entry:
            return False
        ev = _task_cancel_events.get(task_id)
        if ev:
            ev.set()
        if entry.get("status") == "running":
            entry["status"] = "cancelling"
            entry["message"] = "正在取消..."
        return True


def list_tasks(created_by=None):
    with _task_lock:
        now = _time.time()
        result = []
        for tid, entry in list(_task_store.items()):
            if created_by and entry.get("created_by") != created_by:
                continue
            result.append({
                "task_id": tid,
                "status": entry.get("status"),
                "progress": entry.get("progress", 0),
                "message": entry.get("message", ""),
                "queue": entry.get("queue"),
                "updated_at": entry.get("updated_at", 0),
            })
        result.sort(key=lambda t: t["updated_at"], reverse=True)
        return result


def _openwrt_lock(server_id=None):
    if server_id:
        cfg = get_pve_server(server_id)
        host = cfg.get("ow_host", "_default_") if cfg else "_default_"
    else:
        cfg = get_config("openwrt")
        host = cfg.get("host", "_default_") if cfg else "_default_"
    with _openwrt_locks_lock:
        if host not in _openwrt_locks:
            _openwrt_locks[host] = threading.Lock()
        return _openwrt_locks[host]


def _allocate_cluster_id(session):
    session.execute(text("SELECT pg_advisory_xact_lock(42)"))
    used_ids = sorted(row[0] for row in session.query(Cluster.id).all())
    num = 1
    for used_id in used_ids:
        if used_id > num:
            break
        num = used_id + 1
    return num


def _generate_ssh_key():
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    priv = ed25519.Ed25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    )
    return priv_bytes.decode(), pub_bytes.decode().strip()


def _random_mac():
    prefix = "52:54:00"
    suffix = ":".join(f"{secrets.randbits(8):02x}" for _ in range(3))
    return f"{prefix}:{suffix}"


class _SSHClient:
    def __init__(self, host, port, username, private_key):
        self.host = host
        self.port = port
        self.username = username
        self.private_key = private_key
        self._ssh = None

    def _get_pkey(self):
        return paramiko.Ed25519Key.from_private_key(io.StringIO(self.private_key))

    def connect(self, timeout=30):
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(
            self.host, port=self.port, username=self.username,
            pkey=self._get_pkey(), timeout=timeout,
            banner_timeout=timeout,
        )

    def close(self):
        if self._ssh:
            self._ssh.close()
            self._ssh = None

    def exec(self, command, timeout=60):
        if not self._ssh:
            raise K8sError("SSH 未连接")
        stdin, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr.read().decode().strip()
            raise K8sError(f"SSH 命令失败 (exit={exit_code}): {err[:200]}")

    def exec_with_output(self, command, timeout=30):
        if not self._ssh:
            raise K8sError("SSH 未连接")
        stdin, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode().strip()
        if exit_code != 0:
            err = stderr.read().decode().strip()
            raise K8sError(f"SSH 命令失败 (exit={exit_code}): {err[:200]}")
        return out

    def exec_streaming(self, command, log_callback=None, timeout=3600):
        if not self._ssh:
            raise K8sError("SSH 未连接")
        transport = self._ssh.get_transport()
        channel = transport.open_session()
        channel.exec_command(command)

        prev_data = ""
        deadline = _time.time() + timeout
        while _time.time() < deadline:
            if channel.recv_ready():
                data = channel.recv(4096).decode(errors="replace")
                if data:
                    prev_data += data
                    if "\n" in prev_data:
                        lines = prev_data.split("\n")
                        for line in lines[:-1]:
                            if log_callback and line.rstrip():
                                log_callback(f"  {line.rstrip()}")
                        prev_data = lines[-1]

            if channel.exit_status_ready():
                break
            _time.sleep(3)

        while channel.recv_ready():
            data = channel.recv(4096).decode(errors="replace")
            prev_data += data
        if prev_data.strip() and log_callback:
            for line in prev_data.rstrip("\n").split("\n"):
                if line.rstrip():
                    log_callback(f"  {line.rstrip()}")

        exit_code = channel.recv_exit_status()
        channel.close()
        if exit_code != 0:
            raise K8sError(f"命令失败 (exit={exit_code})")

    def write_file(self, path, content, sudo=False):
        prefix = "sudo " if sudo else ""
        cmd = f"{prefix}tee {path} > /dev/null"
        if not self._ssh:
            raise K8sError("SSH 未连接")
        stdin, stdout, stderr = self._ssh.exec_command(cmd, timeout=30)
        stdin.write(content)
        stdin.close()
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr.read().decode().strip()
            raise K8sError(f"SSH 写入文件失败 (exit={exit_code}): {err[:200]}")

    def file_exists(self, path):
        try:
            self.exec(f"test -f {path}")
            return True
        except K8sError:
            return False

    def dir_exists(self, path):
        try:
            self.exec(f"test -d {path}")
            return True
        except K8sError:
            return False


def _wait_for_ssh(host, port, username, private_key, timeout=120):
    deadline = _time.time() + timeout
    last_error = ""
    while _time.time() < deadline:
        try:
            ssh = _SSHClient(host, port, username, private_key)
            ssh.connect(timeout=10)
            ssh.close()
            return True
        except Exception as e:
            last_error = str(e)
            _time.sleep(2)
    raise K8sError(f"SSH 连接失败 ({host}:{port}): {last_error}")


def _wait_for_vms_ssh(ssh, vm_ips, timeout=120, log_callback=None):
    deadline = _time.time() + timeout
    pending = dict(vm_ips)
    while _time.time() < deadline and pending:
        for name, ip in list(pending.items()):
            try:
                ssh.exec(
                    f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "
                    f"teacher@{ip} 'echo OK'",
                    timeout=10)
                if log_callback:
                    log_callback(f"{name} ({ip}): SSH 就绪")
                del pending[name]
            except K8sError:
                pass
        if pending:
            _time.sleep(5)
    if pending:
        names = ", ".join(pending.keys())
        raise K8sError(f"以下节点 SSH 超时未就绪: {names}")


def _openwrt_client(server_id=None):
    if server_id:
        cfg = get_pve_server(server_id)
        if not cfg:
            raise K8sError(f"PVE 服务器 (ID={server_id}) 不存在")
        ow_host = cfg.get("ow_host", "")
        ow_username = cfg.get("ow_username", "")
        ow_password = cfg.get("ow_password", "")
    else:
        cfg = get_config("openwrt")
        if not cfg:
            raise K8sError(f"OpenWrt 未配置，请先在页面中保存配置")
        ow_host = cfg.get("host", "")
        ow_username = cfg.get("username", "")
        ow_password = cfg.get("password", "")
    missing = []
    if not ow_host: missing.append("host")
    if not ow_username: missing.append("username")
    if not ow_password: missing.append("password")
    if missing:
        raise K8sError(f"OpenWrt 配置不完整: {', '.join(missing)}")
    return OpenWrtClient(
        host=ow_host,
        username=ow_username,
        password=ow_password,
        port=int(cfg.get("ow_port", 22) if server_id else cfg.get("port", 22)),
    )


def _pve_client(server_id=None):
    if server_id:
        cfg = get_pve_server(server_id)
        if not cfg:
            raise K8sError(f"PVE 服务器 (ID={server_id}) 不存在")
    else:
        cfg = get_config("pve")
        if not cfg:
            raise K8sError(f"PVE 未配置，请先在页面中保存配置")
    missing = [k for k in ("host", "user", "token_name", "token_value") if not cfg.get(k)]
    if missing:
        raise K8sError(f"PVE 配置不完整: {', '.join(missing)}")
    return PVEClient(
        host=cfg["host"],
        user=cfg["user"],
        token_name=cfg["token_name"],
        token_value=cfg["token_value"],
        verify_ssl=cfg.get("verify_ssl", False),
        port=int(cfg.get("port", 8006)),
    )


def list_clusters():
    return load_clusters()


def get_cluster(name):
    return load_cluster(name)


def delete_cluster(name, status_callback=None, log_callback=None):
    def report(p, m):
        if status_callback: status_callback(p, m)
    def _log(m):
        if log_callback: log_callback(m)

    report(5, "正在加载集群信息...")
    cluster = load_cluster(name)
    if not cluster:
        raise K8sError(f"Cluster {name} not found")
    _num = name.split("_")[1]
    pve = _pve_client(server_id=cluster.get("pve_server_id"))
    pve.connect()
    _vms = list(cluster.get("vms", {}).items())
    _total = len(_vms)
    report(10, f"正在释放虚拟机 (共 {_total} 台)...")
    for i, (vm_name, vm_info) in enumerate(_vms):
        _log(f"释放虚拟机 {vm_name} (VMID {vm_info['vmid']})...")
        try:
            pve.release_vm(vm_info["node"], vm_info["vmid"], purge=True)
            _log(f"{vm_name} 已释放")
        except Exception as e:
            _log(f"{vm_name} 释放失败: {e}")
        report(10 + int((i + 1) / _total * 30), f"正在释放虚拟机 ({i + 1}/{_total})...")
    try:
        pve.api
    except Exception:
        pass

    _pve_server_id = cluster.get("pve_server_id")
    report(40, "正在清理 OpenWrt 配置...")
    ow_lock = _openwrt_lock(server_id=_pve_server_id)
    if not ow_lock.acquire(timeout=30):
        raise K8sError("OpenWrt 操作超时，系统繁忙，请稍后重试")
    try:
        ow = _openwrt_client(server_id=_pve_server_id)
        ow.connect()
        try:
            report(45, "删除路由转发")
            try:
                ow.delete_redirect(name)
                _log("路由转发已删除")
            except Exception:
                pass
            _vnames = list(cluster.get("vms", {}).keys())
            report(50, f"删除 DHCP 主机绑定 ({len(_vnames)} 个)")
            for vm_name in _vnames:
                try:
                    ow.delete_dhcp_host(cluster["vms"][vm_name]["ip"], skip_restart=True)
                    _log(f"DHCP 主机绑定 {vm_name} 已删除")
                except Exception:
                    pass
            ow.exec("/etc/init.d/dnsmasq reload", tolerant=True)
            _log("dnsmasq 已重载")
            report(65, "删除 DHCP 池")
            try:
                ow.delete_dhcp_pool(cluster["interface"])
                _log("DHCP 池已删除")
            except Exception:
                pass
            report(70, "删除接口")
            try:
                ow.delete_interface(cluster["interface"])
            except Exception:
                pass
            report(75, "删除 VLAN 设备")
            try:
                ow.delete_vlan_device(cluster["vlan_device"])
            except Exception:
                pass
            report(80, "清理 DHCP 动态租约")
            try:
                _gw = cluster.get("gateway", "")
                if _gw:
                    _prefix = ".".join(_gw.split(".")[:3]) + "."
                    ow.exec(f"sed -i '/ {_prefix}/d' /tmp/dhcp.leases", tolerant=True)
                    _log("DHCP 动态租约已清理")
            except Exception:
                pass
            report(85, "重启网络")
            try:
                ow.exec("/etc/init.d/network restart", tolerant=True)
                _log("网络已重启")
            except Exception:
                pass
        finally:
            ow.close()
    finally:
        ow_lock.release()
    report(95, "清理数据库")
    delete_cluster_db(name)
    _log("数据库记录已删除")
    report(100, f"集群 {name} 已删除")


def delete_cluster_async(name, created_by=None):
    task_id = _new_task_id()
    _update_task(task_id, status="running", progress=0, message="排队中，等待资源...",
                 created_by=created_by, queue="delete")

    def _cb(p, m):
        _update_task(task_id, progress=p, message=m)
    def _log(m):
        _append_log(task_id, m)

    def _run():
        _update_task(task_id, status="running", progress=0, message="正在初始化删除...")
        try:
            delete_cluster(name, status_callback=_cb, log_callback=_log)
            _update_task(task_id, status="completed", progress=100, message="集群已删除")
        except Exception as e:
            _update_task(task_id, status="error", progress=0, message=str(e), error=str(e))

    scheduler.enqueue("delete", Task("delete", task_id, _run))
    _cleanup_old_tasks()
    return task_id


def create_cluster(master_count, node_count, master_cores, master_memory,
                   node_cores, node_memory, pve_node,
                   pve_server_id=0, group_id=None,
                   class_id=None, created_by=None,
                   status_callback=None, log_callback=None, cancel_event=None):
    def report(progress, message):
        if status_callback:
            status_callback(progress, message)

    def _log(msg):
        if log_callback:
            log_callback(msg)

    password = secrets.token_urlsafe(16)

    _log(f"开始创建集群: master={master_count}, node={node_count}, "
         f"cores=(master={master_cores}, node={node_cores}), "
         f"memory=(master={master_memory}MB, node={node_memory}MB)")
    _log(f"PVE 节点: {pve_node}")
    with session_scope(commit=True) as session:
        try:
            num = _allocate_cluster_id(session)
            cluster_name = f"k8s_{num}"
            cluster_row = Cluster(id=num, name=cluster_name, status="allocating")
            session.add(cluster_row)
        except Exception:
            raise K8sError("无法分配集群编号，请重试")

    _log(f"集群编号: {num}, 名称: {cluster_name}")
    vlan_id = 100 + num
    vlan_device = f"eth1.{vlan_id}"
    iface_name = cluster_name
    ip_prefix = f"10.100.{num}"
    gateway = f"{ip_prefix}.1"
    netmask = "255.255.255.0"
    dnsmasq_name = f"k8s{num}"
    dhcp_start = "100"
    dhcp_limit = "150"

    report(5, "正在生成 SSH 密钥对...")
    _log("生成 2048-bit RSA 密钥对")
    priv_key, pub_key = _generate_ssh_key()
    _log(f"SSH 公钥: {pub_key[:80]}...")
    _log(f"SSH 私钥长度: {len(priv_key)} 字节")

    pve_cfg = get_pve_server(pve_server_id) if pve_server_id else get_config("pve")
    template_vmid = pve_cfg.get("template_vmid") if pve_cfg else None
    if not template_vmid:
        template_vmid = 9000
        _log(f"PVE: 未配置模板 VMID，使用默认值 {template_vmid}")
    else:
        _log(f"PVE: 模板 VMID = {template_vmid}")

    _log("保存集群信息到数据库 (创建中...)")
    _initial_entry = {
        "status": "creating",
        "vlan_id": vlan_id,
        "vlan_device": vlan_device,
        "interface": iface_name,
        "gateway": gateway,
        "netmask": netmask,
        "dnsmasq": dnsmasq_name,
        "ssh_private_key": priv_key,
        "ssh_public_key": pub_key,
        "pve_node": pve_node,
        "pve_server_id": pve_server_id,
        "template_vmid": template_vmid,
        "ssh_port": 50000 + num,
        "vms": {},
    }
    try:
        save_cluster(cluster_name, _initial_entry)
    except Exception as e:
        try:
            delete_cluster_db(cluster_name)
        except Exception:
            pass
        raise K8sError(f"数据库保存失败: {e}") from e

    vms = {}
    created_vms = []

    if cancel_event and cancel_event.is_set():
        _cleanup_db()
        raise K8sError("任务已取消")

    def _cleanup_db():
        try:
            delete_cluster_db(cluster_name)
        except Exception:
            pass

    def _rollback_openwrt(ow):
        try: ow.delete_redirect(cluster_name)
        except Exception: pass
        for vm_name in list(vms.keys()):
            try: ow.delete_dhcp_host(vms[vm_name]["ip"], skip_restart=True)
            except Exception: pass
        try: ow.exec("/etc/init.d/dnsmasq reload", tolerant=True)
        except Exception: pass
        try: ow.delete_dhcp_pool(iface_name)
        except Exception: pass
        try: ow.delete_interface(iface_name)
        except Exception: pass
        try: ow.delete_vlan_device(vlan_device)
        except Exception: pass
        try: ow.exec("/etc/init.d/network restart", tolerant=True)
        except Exception: pass

    ow_lock = _openwrt_lock(server_id=pve_server_id if pve_server_id else None)
    if not ow_lock.acquire(timeout=30):
        raise K8sError("OpenWrt 操作超时，系统繁忙，请稍后重试")
    try:
        ow = _openwrt_client(server_id=pve_server_id if pve_server_id else None)

        try:
            if cancel_event and cancel_event.is_set():
                raise K8sError("任务已取消")
            ow.connect()
            report(10, "正在创建 VLAN 设备...")
            _log(f"VLAN: 创建设备 {vlan_device} (iface=eth1, vid={vlan_id})")
            _log(f"执行: uci add network device")
            _log(f"执行: uci set network.@device[-1].type=8021q, ifname=eth1, vid={vlan_id}, name={vlan_device}")
            ow.create_vlan_device(vlan_device, "eth1", vlan_id)
            _log(f"VLAN 设备 {vlan_device} 创建完成")

            report(20, "正在配置接口...")
            _log(f"接口: 创建 {iface_name} (device={vlan_device}, ip={gateway}/{netmask})")
            ow.create_interface(iface_name, vlan_device, "static", gateway, netmask)
            _log(f"接口 {iface_name} 创建完成")

            report(28, "正在配置 DHCP...")
            _log(f"DHCP: 创建池 {iface_name} (interface={iface_name}, start={dhcp_start}, limit={dhcp_limit})")
            ow.create_dhcp_pool(iface_name, iface_name, dhcp_start, dhcp_limit)
            _log(f"DHCP 池 {iface_name} 创建完成")

            report(38, "正在配置防火墙...")
            _log("防火墙: 查找 LAN 区域")
            zones = ow.get_firewall_zones()
            _log(f"防火墙: 找到 {len(zones)} 个区域: {[z.get('name','?') for z in zones.values()]}")
            matched = False
            for sec_name, zone in zones.items():
                if zone.get("name", "").lower() == "lan":
                    _log(f"防火墙: 找到 LAN 区域 (section={sec_name}), 添加接口 {iface_name}")
                    ow.add_interface_to_zone(sec_name, iface_name)
                    _log(f"防火墙: 接口 {iface_name} 已加入 LAN 区域，防火墙已重启")
                    matched = True
                    break
            if not matched:
                _log("防火墙: 警告 - 未找到 LAN 区域，跳过接口绑定")

            report(42, "正在重启 OpenWrt 网络...")
            _log("执行: /etc/init.d/network restart")
            ow.exec("/etc/init.d/network restart", tolerant=True)
            _log("OpenWrt 网络已重启")
        except Exception as e:
            _rollback_openwrt(ow)
            ow.close()
            _cleanup_db()
            raise K8sError(f"OpenWrt setup failed: {e}") from e
    finally:
        ow_lock.release()

    client_cores = max(2, master_cores)
    client_memory = max(2048, master_memory)

    if cancel_event and cancel_event.is_set():
        _rollback_openwrt(ow)
        ow.close()
        _cleanup_db()
        raise K8sError("任务已取消")

    _log(f"PVE: 正在连接 {pve_cfg.get('host', '?') if pve_cfg else '?'}:{pve_cfg.get('port', 8006) if pve_cfg else '?'}")
    report(45, "正在连接 PVE...")
    pve = _pve_client(server_id=pve_server_id if pve_server_id else None)
    version = pve.connect()
    _log(f"PVE: 连接成功, 版本 {version.get('version', '?') if isinstance(version, dict) else version}")

    try:
        configs = [
            ("client", f"client-k8s{num}", client_cores, client_memory),
        ]
        for i in range(1, master_count + 1):
            configs.append(("master", f"master{i}-k8s{num}", master_cores, master_memory))
        for i in range(1, node_count + 1):
            configs.append(("node", f"node{i}-k8s{num}", node_cores, node_memory))

        total_vms = len(configs)
        _log(f"PVE: 共 {total_vms} 个虚拟机，逐个分配 VMID")
        if cancel_event and cancel_event.is_set():
            raise K8sError("任务已取消")
        for idx, item in enumerate(configs):
            if cancel_event and cancel_event.is_set():
                raise K8sError("任务已取消")
            role = item[0]
            vm_name = item[1]
            cores = item[2]
            memory = item[3]

            vm_progress = 50 + int((idx / total_vms) * 38)
            report(vm_progress, f"正在创建虚拟机 {vm_name} ({idx + 1}/{total_vms})...")

            cfg = {
                "name": vm_name,
                "full": 0,
                "agent": 1,
                "protection": 0,
                "ciuser": "teacher",
                "cipassword": password,
                "sshkeys": quote(pub_key.strip(), safe=''),
                "ipconfig0": "ip=dhcp",
                "cores": cores,
                "memory": memory,
            }

            _log(f"VM {vm_name}: 读取模板 (VMID {template_vmid}) 网络配置")
            tmpl_config = pve.get_vm_config(pve_node, template_vmid)
            old_net = tmpl_config.get("net0", "")
            _log(f"VM {vm_name}: 模板 net0 = \"{old_net}\"")
            parts = old_net.split(",")
            model = parts[0].split("=")[0] if "=" in parts[0] else parts[0]
            parts[0] = model
            parts = [p for p in parts if not p.startswith("tag=")]
            parts = [p for p in parts if not p.startswith("macaddr=")]
            parts.append(f"tag={vlan_id}")
            mac = _random_mac()
            parts.append(f"macaddr={mac}")
            cfg["net0"] = ",".join(parts)
            _log(f"VM {vm_name}: 生成 MAC = {mac}, VLAN = {vlan_id}")
            _log(f"VM {vm_name}: net0 = \"{cfg['net0']}\"")
            _log(f"VM {vm_name}: 配置 cores={cores}, memory={memory}, cipassword=***, full=0")

            newid = pve.create_vm(pve_node, template_vmid, cfg)
            _log(f"VM {vm_name}: 创建成功, VMID = {newid}")
            vms[vm_name] = {"node": pve_node, "vmid": newid, "mac": mac, "role": role}
            created_vms.append((vm_name, pve_node, newid))

        # ── 预分配 IP（开机前写入 DHCP 静态绑定，不依赖 Guest Agent）──
        _ip_prefix = f"10.100.{num}"
        _ip_list = (
            [f"{_ip_prefix}.101"] +
            [f"{_ip_prefix}.{111 + i}" for i in range(master_count)] +
            [f"{_ip_prefix}.{121 + i}" for i in range(node_count)]
        )
        for _vm_name, _ip in zip(list(vms.keys()), _ip_list):
            vms[_vm_name]["ip"] = _ip
            _log(f"VM {_vm_name}: 预分配 IP = {_ip}")

        _log("批量写入 DHCP 静态绑定和端口转发...")
        try:
            _dhcp_lock = _openwrt_lock(server_id=pve_server_id if pve_server_id else None)
            if not _dhcp_lock.acquire(timeout=30):
                raise K8sError("OpenWrt 操作超时，系统繁忙，请稍后重试")
            try:
                _ow_dhcp = _openwrt_client(server_id=pve_server_id if pve_server_id else None)
                _ow_dhcp.connect()
                try:
                    for _vm_name, _vi in vms.items():
                        _ow_dhcp.create_dhcp_host(_vi["ip"], _vi["mac"])
                        _log(f"DHCP 绑定: {_vm_name} → {_vi['ip']} ({_vi['mac']})")
                    _ow_dhcp.exec("/etc/init.d/dnsmasq reload", tolerant=True)
                    _log("dnsmasq 已重载")

                    _cli_ip = next(_vi["ip"] for _vi in vms.values()
                                   if _vi.get("role") == "client")
                    _ow_dhcp.create_redirect(cluster_name, 50000 + num, _cli_ip, "22")
                    _log(f"端口转发: WAN:{50000 + num} → {_cli_ip}:22")
                    report(95, f"端口转发已配置: WAN:{50000 + num} → {_cli_ip}:22")
                finally:
                    _ow_dhcp.close()
            finally:
                _dhcp_lock.release()
        except Exception as e:
            _log(f"DHCP/端口转发配置异常 ({e})")

        report(92, "正在启动虚拟机...")
        for vm_name, node, vmid in created_vms:
            _log(f"VM {vm_name}: 发送启动命令")
            try:
                pve.start_vm(node, vmid)
                _log(f"VM {vm_name}: 启动命令已发送")
            except Exception as e:
                _log(f"VM {vm_name}: 启动跳过 ({e})")

        report(94, "正在重启虚拟机以刷新主机名...")
        for vm_name, node, vmid in created_vms:
            _log(f"VM {vm_name}: 发送重启命令")
            try:
                pve.reboot_vm(node, vmid)
                _log(f"VM {vm_name}: 重启完成")
            except Exception as e:
                _log(f"VM {vm_name}: 重启跳过 ({e})")

        report(96, "正在等待 client VM 就绪并配置 SSH...")
        client_vm = next((item for item in created_vms if item[0].startswith("client-")), None)
        _ssh_host = ""
        _ssh_port = 0
        if client_vm:
            if pve_server_id:
                _ow_host_cfg = get_pve_server(pve_server_id) or {}
                _ssh_host = _ow_host_cfg.get("ow_host", "")
            else:
                _ow_cfg = get_config("openwrt")
                _ssh_host = _ow_cfg["host"] if _ow_cfg else ""
            _ssh_port = 50000 + num
            _log(f"Client VM {client_vm[0]}: 等待 SSH 就绪 ({_ssh_host}:{_ssh_port}, 120s 超时)")
            try:
                _wait_for_ssh(_ssh_host, _ssh_port, "teacher", priv_key, timeout=120)
                _log(f"Client VM {client_vm[0]}: SSH 已就绪")
            except Exception as e:
                _log(f"Client VM {client_vm[0]}: SSH 未响应 ({e})")

            report(98, "正在通过 SSH 配置 client 免密登录...")
            _log(f"Client VM {client_vm[0]}: SSH 上传私钥")
            try:
                ssh = _SSHClient(_ssh_host, _ssh_port, "teacher", priv_key)
                ssh.connect(timeout=30)
                _log(f"SSH: mkdir -p /home/teacher/.ssh && chmod 700")
                ssh.exec("mkdir -p /home/teacher/.ssh && chmod 700 /home/teacher/.ssh")
                _log(f"SSH: 写入 /home/teacher/.ssh/id_rsa ({len(priv_key)} bytes)")
                ssh.write_file("/home/teacher/.ssh/id_rsa", priv_key)
                _log(f"SSH: chmod 600 && chown")
                ssh.exec("chmod 600 /home/teacher/.ssh/id_rsa && chown -R teacher:teacher /home/teacher/.ssh")
                _log(f"SSH: 写入 /home/teacher/.ssh/id_rsa.pub")
                ssh.write_file("/home/teacher/.ssh/id_rsa.pub", pub_key)
                ssh.exec("chmod 644 /home/teacher/.ssh/id_rsa.pub && chown teacher:teacher /home/teacher/.ssh/id_rsa.pub")
                _log(f"SSH: 私钥上传完成")

                _log(f"SSH: sudo mkdir -p /root/.ssh && chmod 700")
                ssh.exec("sudo mkdir -p /root/.ssh && sudo chmod 700 /root/.ssh")
                _log(f"SSH: 写入 /root/.ssh/authorized_keys")
                ssh.write_file("/root/.ssh/authorized_keys", pub_key, sudo=True)
                _log(f"SSH: sudo chmod 600 && chown root")
                ssh.exec("sudo chmod 600 /root/.ssh/authorized_keys && sudo chown root:root /root/.ssh/authorized_keys")
                _log(f"SSH: 公钥上传完成")
                ssh.close()
            except Exception as e:
                _log(f"Client VM {client_vm[0]}: SSH 配置异常, 终止创建 ({e})")
                raise K8sError(f"Client VM SSH 配置失败: {e}") from e

    except Exception as e:
        _log(f"错误: {e}")
        _log("回滚: 释放已创建的虚拟机")
        for vm_name, node, vmid in created_vms:
            try:
                pve.release_vm(node, vmid, purge=True)
                _log(f"回滚: VM {vm_name} (VMID {vmid}) 已释放")
            except Exception as re:
                _log(f"回滚: VM {vm_name} 释放失败 ({re})")
        _log("回滚: 清理 OpenWrt 配置")
        _rollback_openwrt(ow)
        ow.close()
        _cleanup_db()
        _log("回滚: 数据库记录已清理")
        raise K8sError(f"PVE VM creation failed: {e}") from e

    ow.close()

    # ── 创建学生账户（按组最大人数）──
    _pending_students = {}
    if group_id and _ssh_host and _ssh_port:
        grp = get_group(group_id)
        max_n = grp["max_students"] if (grp and grp.get("max_students")) else 0
        if max_n > 0:
            backfill_group_student_numbers(group_id)
            report(97.5, f"正在创建 {max_n} 个学生账户...")
            _log(f"创建 {max_n} 个学生账户 (student1 ~ student{max_n})")
            try:
                ssh = _SSHClient(_ssh_host, _ssh_port, "teacher", priv_key)
                ssh.connect(timeout=30)
                try:
                    for i in range(1, max_n + 1):
                        uname = f"student{i}"
                        spass = secrets.token_urlsafe(12)
                        _pending_students[uname] = {"password": spass}
                        _log(f"创建学生账户 {uname}")
                        ssh.exec(f"sudo useradd -m {uname} -s /bin/bash 2>/dev/null || true")
                        ssh.exec(f"echo '{uname}:{spass}' | sudo chpasswd")
                        ssh.exec(f"sudo mkdir -p /home/{uname}/.ssh")
                        ssh.exec(f"sudo cp /home/teacher/.ssh/id_rsa /home/{uname}/.ssh/")
                        ssh.exec(f"sudo cp /home/teacher/.ssh/id_rsa.pub /home/{uname}/.ssh/")
                        ssh.exec(f"sudo sh -c 'cat /home/teacher/.ssh/id_rsa.pub >> /home/{uname}/.ssh/authorized_keys'")
                        ssh.exec(f"sudo chmod 700 /home/{uname}/.ssh")
                        ssh.exec(f"sudo chmod 600 /home/{uname}/.ssh/id_rsa")
                        ssh.exec(f"sudo chmod 644 /home/{uname}/.ssh/authorized_keys")
                        ssh.exec(f"sudo chown -R {uname}:{uname} /home/{uname}/.ssh")
                    _log(f"已创建 {max_n} 个学生账户")
                finally:
                    ssh.close()
            except Exception as e:
                _log(f"创建学生账户失败: {e}")

    report(97, "正在保存集群信息...")
    _log("保存集群信息到数据库")
    _client_mac = next((vm["mac"] for vm in vms.values() if vm.get("role") == "client"), None)
    cluster_entry = {
        "status": "running",
        "vlan_id": vlan_id,
        "vlan_device": vlan_device,
        "interface": iface_name,
        "gateway": gateway,
        "netmask": netmask,
        "dnsmasq": dnsmasq_name,
        "ssh_private_key": priv_key,
        "ssh_public_key": pub_key,
        "password": password,
        "pve_node": pve_node,
        "pve_server_id": pve_server_id,
        "template_vmid": template_vmid,
        "vms": vms,
        "ssh_port": 50000 + num,
        "client_mac": _client_mac,
        "group_id": group_id,
        "class_id": class_id,
        "created_by": created_by,
        "students": _pending_students,
    }
    save_cluster(cluster_name, cluster_entry)

    _log(f"集群 {cluster_name} 已保存: {total_vms} 个 VM, VLAN {vlan_id}")
    report(100, "集群创建完成")

    return cluster_name, cluster_entry


def create_cluster_async(master_count, node_count, master_cores, master_memory,
                         node_cores, node_memory, pve_node,
                         pve_server_id=0,
                         group_id=None, class_id=None, created_by=None):
    task_id = _new_task_id()
    _update_task(task_id, status="running", progress=0, message="排队中，等待资源...",
                 created_by=created_by, queue="create")

    def _cb(progress, message):
        _update_task(task_id, progress=progress, message=message)

    def _log(msg):
        _append_log(task_id, msg)

    def _run():
        _update_task(task_id, status="running", progress=0, message="正在初始化...")
        ev = threading.Event()
        _task_cancel_events[task_id] = ev
        try:
            def _create_cb(p, m):
                _update_task(task_id, progress=int(p * 0.5), message=m)
            name, cluster = create_cluster(
                master_count, node_count,
                master_cores, master_memory,
                node_cores, node_memory,
                pve_node,
                pve_server_id=pve_server_id,
                group_id=group_id,
                class_id=class_id,
                created_by=created_by,
                status_callback=_create_cb,
                log_callback=_log,
                cancel_event=ev,
            )
            _append_log(task_id, "虚拟机创建完成，排队等待部署 K8s...")

            def _deploy_cb(p, m):
                _update_task(task_id, progress=50 + int(p * 0.5), message=m)
            deploy_log = lambda m: _append_log(task_id, m)

            def _run_deploy():
                _update_task(task_id, queue="deploy")
                try:
                    deploy_k8s(name, status_callback=_deploy_cb, log_callback=deploy_log)
                    cluster = load_cluster(name)
                    safe = {k: v for k, v in cluster.items() if k != "ssh_private_key"}
                    _update_task(task_id, status="completed", progress=100,
                                 message="集群创建并部署 K8s 完成",
                                 result={"name": name, "cluster": safe})
                except Exception as e:
                    if ev.is_set():
                        _update_task(task_id, status="cancelled", progress=0,
                                     message="任务已取消")
                    else:
                        _update_task(task_id, status="error", progress=0,
                                     message=str(e), error=str(e))

            scheduler.enqueue("deploy", Task("deploy", _new_task_id(), _run_deploy))

        except Exception as e:
            if ev.is_set():
                _update_task(task_id, status="cancelled", progress=0,
                             message="任务已取消")
            else:
                _update_task(task_id, status="error", progress=0,
                             message=str(e), error=str(e))
        finally:
            _task_cancel_events.pop(task_id, None)

    scheduler.enqueue("create", Task("create", task_id, _run))
    _cleanup_old_tasks()
    return task_id


def force_delete_cluster(name):
    delete_cluster_db(name)


def deploy_k8s(name, status_callback=None, log_callback=None):
    cluster = load_cluster(name)
    if not cluster:
        raise K8sError(f"集群 {name} 不存在")
    if cluster.get("status") != "running":
        raise K8sError(f"集群 {name} 状态异常，无法部署 K8s")

    def report(progress, message):
        if status_callback:
            status_callback(progress, message)
    def _log(msg):
        if log_callback:
            log_callback(msg)

    _log(f"开始部署 K8s: 集群 {name}")

    _pve_sid = cluster.get("pve_server_id")
    if _pve_sid:
        _ow_cfg = get_pve_server(_pve_sid) or {}
        _ssh_host = _ow_cfg.get("ow_host", "")
    else:
        _ow_cfg = get_config("openwrt") or {}
        _ssh_host = _ow_cfg.get("host", "")
    _ssh_port = cluster.get("ssh_port") or 50000 + int(name.split("_")[1])
    _priv_key = cluster.get("ssh_private_key", "")
    _pub_key = cluster.get("ssh_public_key", "")

    report(10, "正在通过 SSH 连接 client VM...")
    _log(f"通过端口转发连接 client VM ({_ssh_host}:{_ssh_port})")
    try:
        _wait_for_ssh(_ssh_host, _ssh_port, "teacher", _priv_key, timeout=120)
    except Exception as e:
        raise K8sError(f"client VM SSH 连接失败: {e}")
    _log("client VM SSH 连接就绪")

    ssh = _SSHClient(_ssh_host, _ssh_port, "teacher", _priv_key)
    ssh.connect(timeout=30)
    _log("SSH 连接已建立")

    def _download_with_retry(url, tmp_path, log_name, timeout=120, retries=3, delay=5):
        for attempt in range(1, retries + 1):
            try:
                ssh.exec(f"wget -q -O {tmp_path} {url} --timeout=30", timeout=timeout)
                return
            except K8sError as e:
                if attempt < retries:
                    _log(f"{log_name} 下载失败 (尝试 {attempt}/{retries})，{delay}s 后重试...")
                    _time.sleep(delay)
                    continue
                raise

    report(15, "正在复制 SSH 密钥到 /root/.ssh/...")
    _existing_key = ssh.exec_with_output("sudo cat /root/.ssh/id_rsa 2>/dev/null || true")
    if _existing_key != _priv_key:
        _log("复制 SSH 私钥 → /root/.ssh/id_rsa")
        try:
            ssh.exec("sudo cp /home/teacher/.ssh/id_rsa /root/.ssh/id_rsa && "
                     "sudo chmod 600 /root/.ssh/id_rsa && "
                     "sudo chown root:root /root/.ssh/id_rsa")
            _log("SSH 密钥复制完成")
        except K8sError as e:
            _log(f"SSH 密钥复制失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log("/root/.ssh/id_rsa 内容正确，跳过")

    report(18, "正在上传 SSH 公钥到所有节点...")
    _log("通过 client VM 分发 SSH 公钥到各节点")
    if not _pub_key:
        _log("SSH 公钥缺失")
        cluster["k8s_status"] = "failed"
        save_cluster(name, cluster)
        raise K8sError("集群 SSH 公钥缺失")

    ssh.exec("mkdir -p /tmp/k8s-setup")
    ssh.write_file("/tmp/k8s-setup/cluster.pub", _pub_key)

    vm_ips = [(vm_name, info["ip"])
              for vm_name, info in cluster.get("vms", {}).items()
              if info.get("role") != "client" and info.get("ip")]
    _log("等待所有节点 SSH 就绪...")
    _wait_for_vms_ssh(ssh, vm_ips, timeout=180, log_callback=_log)
    _log("所有节点 SSH 已就绪")

    for vm_name, vm_info in cluster.get("vms", {}).items():
        if vm_info.get("role") == "client":
            continue
        _ip = vm_info.get("ip")
        if not _ip:
            _log(f"{vm_name}: 无 IP 信息，跳过")
            continue
        _log(f"{vm_name}: 配置 root SSH ({_ip})")
        try:
            ssh.exec(
                f"cat /tmp/k8s-setup/cluster.pub | "
                f"ssh -o StrictHostKeyChecking=no teacher@{_ip} "
                f"'sudo mkdir -p /root/.ssh && sudo chmod 700 /root/.ssh && "
                f"sudo tee /root/.ssh/authorized_keys > /dev/null && "
                f"sudo chmod 600 /root/.ssh/authorized_keys && "
                f"sudo chown root:root /root/.ssh/authorized_keys'",
                timeout=60)
            _log(f"{vm_name}: 配置完成")
        except K8sError as e:
            _log(f"{vm_name}: 配置失败 ({e})")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise

    # ── 在 master/node 上创建学生账户 ──
    students = cluster.get("students", {})
    if students:
        _log("在 master/node 节点上创建学生账户")
        for vm_name, vm_info in cluster.get("vms", {}).items():
            if vm_info.get("role") == "client":
                continue
            vip = vm_info.get("ip")
            if not vip:
                continue
            for uname, sinfo in students.items():
                spass = sinfo["password"]
                try:
                    ssh.exec(
                        f"ssh -o StrictHostKeyChecking=no teacher@{vip} "
                        f"'sudo useradd -m {uname} -s /bin/bash 2>/dev/null || true'",
                        timeout=30)
                    ssh.exec(
                        f"ssh -o StrictHostKeyChecking=no teacher@{vip} "
                        f"'echo \"{uname}:{spass}\" | sudo chpasswd'",
                        timeout=30)
                    ssh.exec(
                        f"cat /tmp/k8s-setup/cluster.pub | "
                        f"ssh -o StrictHostKeyChecking=no teacher@{vip} "
                        f"'sudo mkdir -p /home/{uname}/.ssh && "
                        f"sudo sh -c \"cat >> /home/{uname}/.ssh/authorized_keys\" && "
                        f"sudo chmod 700 /home/{uname}/.ssh && "
                        f"sudo chmod 600 /home/{uname}/.ssh/authorized_keys && "
                        f"sudo chown -R {uname}:{uname} /home/{uname}/.ssh'",
                        timeout=30)
                except K8sError as e:
                    _log(f"  {vm_name}: 创建学生账户 {uname} 失败 ({e})")
        _log("各节点学生账户创建完成")

    report(25, "正在更新集群 K8s 状态...")
    cluster["k8s_status"] = "installing"
    save_cluster(name, cluster)
    _log("集群 K8s 状态已更新为 installing")

    # ── ezdown ──
    report(30, "正在下载 ezdown...")
    if not ssh.file_exists("/home/teacher/ezdown"):
        _log("下载 ezdown → /home/teacher/ezdown")
        try:
            _download_with_retry(
                "http://10.11.43.82/download/ezdown",
                "/tmp/ezdown", "ezdown", timeout=120)
            ssh.exec("mv /tmp/ezdown /home/teacher/ezdown && chmod 755 /home/teacher/ezdown && chown teacher:teacher /home/teacher/ezdown")
            _log("ezdown 下载完成")
        except K8sError as e:
            _log(f"ezdown 下载失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log("ezdown 已存在，跳过下载")

    # ── kubeasz_offline.tgz ──
    report(40, "正在下载 kubeasz 离线包...")
    if not ssh.file_exists("/home/teacher/kubeasz_offline.tgz"):
        _log("下载 kubeasz_offline.tgz → /home/teacher/kubeasz_offline.tgz")
        try:
            _download_with_retry(
                "http://10.11.43.82/download/kubeasz_offline.tgz",
                "/tmp/kubeasz_offline.tgz", "kubeasz 离线包", timeout=600)
            ssh.exec("mv /tmp/kubeasz_offline.tgz /home/teacher/kubeasz_offline.tgz && chown teacher:teacher /home/teacher/kubeasz_offline.tgz")
            _log("kubeasz_offline.tgz 下载完成")
        except K8sError as e:
            _log(f"kubeasz_offline.tgz 下载失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log("kubeasz_offline.tgz 已存在，跳过下载")

    # ── extract kubeasz_offline.tgz ──
    report(55, "正在解压 kubeasz 离线包...")
    if not ssh.dir_exists("/etc/kubeasz/roles"):
        _log("解压 kubeasz_offline.tgz → /etc")
        try:
            ssh.exec("sudo tar xzf /home/teacher/kubeasz_offline.tgz -C /etc", timeout=300)
            _log("解压完成")
        except K8sError as e:
            _log(f"解压失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log("kubeasz 已解压，跳过")

    # ── ezdown: download dependencies ──
    report(65, "正在部署 ezdown 依赖...")
    if not ssh.exec_with_output("which docker || true"):
        _log("执行: sudo /home/teacher/ezdown -D")
        try:
            ssh.exec("sudo /home/teacher/ezdown -D", timeout=600)
            _log("ezdown -D 下载完成")
        except K8sError as e:
            _log(f"ezdown -D 失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log("Docker 已安装，跳过")

    # ── ezdown: create kubeasz container ──
    report(75, "正在创建 kubeasz 容器...")
    _container_name = ssh.exec_with_output("sudo docker ps -a --format '{{.Names}}' | grep -w kubeasz || true")
    if not _container_name:
        _log("执行: sudo /home/teacher/ezdown -S")
        try:
            ssh.exec("sudo /home/teacher/ezdown -S", timeout=120)
            _log("kubeasz 容器创建完成")
        except K8sError as e:
            _log(f"ezdown -S 失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log("kubeasz 容器已存在，跳过创建")

    # ── ezctl new + config ──
    report(80, "正在检查集群配置文件...")
    cluster_dir = f"/etc/kubeasz/clusters/{name}"
    if not ssh.dir_exists(cluster_dir):
        _log(f"执行: docker exec kubeasz ezctl new {name}")
        try:
            ssh.exec(f"sudo docker exec kubeasz ezctl new {name}", timeout=60)
            _log("ezctl new 完成")
        except K8sError as e:
            _log(f"ezctl new 失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log(f"配置目录 {cluster_dir} 已存在，跳过")

    f_config = f"{cluster_dir}/config.yml"

    masters = sorted([k for k, v in cluster["vms"].items() if v["role"] == "master"])
    nodes   = sorted([k for k, v in cluster["vms"].items() if v["role"] == "node"])
    _log(f"master 节点: {masters}")
    _log(f"node 节点:   {nodes}")

    # Resolve IPs: 使用预分配的 IP
    report(82, "正在获取节点 IP 地址...")
    _log("获取节点 IP（使用预分配 IP）")
    master_ips = {}
    node_ips = {}
    for vm_name in masters + nodes:
        vm_info = cluster["vms"][vm_name]
        ip = vm_info.get("ip")
        if ip:
            _log(f"{vm_name}: IP = {ip}")
        else:
            _log(f"{vm_name}: 无预分配 IP，使用主机名")
        role = vm_info.get("role")
        if role == "master":
            master_ips[vm_name] = ip or vm_name
        else:
            node_ips[vm_name] = ip or vm_name

    # hosts: 读取已生成的文件，只替换三个占位符
    _log(f"读取已有 hosts 文件: {cluster_dir}/hosts")
    _tmpl = ssh.exec_with_output(f"sudo cat {cluster_dir}/hosts")

    _tmpl = _tmpl.replace("{{etcd_server}}", "\n".join(
        master_ips[m] for m in masters
    ))
    _tmpl = _tmpl.replace("{{master_server}}", "\n".join(
        f"{master_ips[m]} k8s_nodename='{m}'" for m in masters
    ))
    _tmpl = _tmpl.replace("{{node_server}}", "\n".join(
        f"{node_ips[n]} k8s_nodename='{n}'" for n in nodes
    ))

    _log("写入 hosts 文件")
    try:
        ssh.write_file(f"{cluster_dir}/hosts", _tmpl, sudo=True)
        _log("hosts 文件写入完成")
    except K8sError as e:
        _log(f"hosts 文件写入失败: {e}")
        cluster["k8s_status"] = "failed"
        save_cluster(name, cluster)
        raise

    # config.yml: INSTALL_SOURCE
    _log("修改 config.yml: INSTALL_SOURCE=offline")
    ssh.exec(f"""sudo sed -i 's/^INSTALL_SOURCE: "online"/INSTALL_SOURCE: "offline"/' {f_config}""")

    # MASTER_CERT_HOSTS: 替换示例 IP 为第一个 master 的真实 IP (非致命)
    if masters:
        _master0 = masters[0]
        _master0_ip = master_ips.get(_master0, _master0)
        _log(f"更新 MASTER_CERT_HOSTS: {_master0} → {_master0_ip}")
        _prefix = "'s/^  - \"10\\.1\\.1\\.1\"/  - \"'"
        _suffix = "'\"/'"
        ssh.exec(f"sudo sed -i {_prefix}{_master0_ip}{_suffix} {f_config} && "
                 f"sudo sed -i '/k8s\\.easzlab\\.io/s/^/#/' {f_config} || true")

    # ── 实际安装 K8s ──
    report(92, "正在检查 K8s 集群安装状态...")
    if not ssh.file_exists(f"{cluster_dir}/kubeconfig"):
        _log("开始安装 Kubernetes 集群（预计 15-30 分钟）...")
        try:
            ssh.exec_streaming(f"sudo docker exec kubeasz ezctl setup {name} all", log_callback=_log)
            _log("Kubernetes 集群安装完成")
        except K8sError as e:
            _log(f"集群安装失败: {e}")
            cluster["k8s_status"] = "failed"
            save_cluster(name, cluster)
            raise
    else:
        _log("K8s 集群已安装，跳过安装步骤")

    # ── 下载 kubeconfig ──
    report(97, "正在下载 kubeconfig...")
    _kube_dir = "/home/teacher/.kube"
    _kubeconfig_path = f"{_kube_dir}/config"
    if not ssh.file_exists(_kubeconfig_path):
        _log(f"创建目录 {_kube_dir}")
        ssh.exec(f"mkdir -p {_kube_dir} && chown teacher:teacher {_kube_dir}")
        _log(f"从第一个 master 节点下载 kubeconfig → {_kubeconfig_path}")
        try:
            first_master_ip = master_ips[masters[0]]
            ssh.exec(f"ssh -o StrictHostKeyChecking=no root@{first_master_ip} "
                     f"'cat /root/.kube/config' > {_kubeconfig_path} && "
                     f"chown teacher:teacher {_kubeconfig_path}")
            _log("kubeconfig 下载完成")
        except Exception as e:
            _log(f"kubeconfig 下载失败（可手动下载）: {e}")
    else:
        _log("kubeconfig 已存在，跳过下载")

    # ── 复制 kubeconfig 到学生账户 ──
    if students:
        _log("复制 kubeconfig 到学生账户")
        for uname in students:
            try:
                ssh.exec(f"sudo mkdir -p /home/{uname}/.kube")
                ssh.exec(f"sudo cp /home/teacher/.kube/config /home/{uname}/.kube/config")
                ssh.exec(f"sudo chown -R {uname}:{uname} /home/{uname}/.kube")
                _log(f"  已复制 kubeconfig 到 {uname}")
            except K8sError as e:
                _log(f"  复制 kubeconfig 到 {uname} 失败 ({e})")
        _log("kubeconfig 复制完成")

    # ── 安装 kubectl ──
    report(98, "正在安装 kubectl...")
    if not ssh.file_exists("/usr/local/bin/kubectl"):
        _log("从 /etc/kubeasz/bin/kubectl 安装 kubectl")
        try:
            ssh.exec("sudo cp /etc/kubeasz/bin/kubectl /usr/local/bin/kubectl && "
                     "sudo chmod 755 /usr/local/bin/kubectl")
            _log("kubectl 安装完成")
        except Exception as e:
            _log(f"kubectl 安装失败（可手动安装）: {e}")
    else:
        _log("kubectl 已存在，跳过安装")

    # ── 部署验证 ──
    report(98.5, "正在验证集群部署...")
    _log("开始验证集群连通性")
    try:
        _log("验证 DNS 解析: nslookup kubernetes.default.svc.cluster.local")
        ssh.exec("nslookup kubernetes.default.svc.cluster.local || nslookup kubernetes.default || echo 'DNS 验证跳过'", timeout=30)
        _log("DNS 验证完成")
    except Exception as e:
        _log(f"DNS 验证警告: {e}")

    _log("验证 SSH 连通性到 master 节点")
    if masters:
        first_master_ip = master_ips[masters[0]]
        try:
            ssh.exec(f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 root@{first_master_ip} 'echo SSH_OK'", timeout=30)
            _log(f"SSH 连通性验证完成: root@{first_master_ip}")
        except Exception as e:
            _log(f"SSH 验证警告: {e}")

    report(99, "正在保存 K8s 部署状态...")
    _log("更新集群 K8s 状态为 installed")
    cluster["k8s_status"] = "installed"
    save_cluster(name, cluster)

    ssh.close()

    _log("K8s 部署完成")
    report(100, "K8s 部署完成")


def deploy_k8s_async(name, created_by=None):
    task_id = _new_task_id()
    _update_task(task_id, status="running", progress=0, message="排队中，等待资源...",
                 created_by=created_by, queue="deploy")

    def _cb(progress, message):
        _update_task(task_id, progress=progress, message=message)
    def _log(msg):
        _append_log(task_id, msg)

    def _run():
        _update_task(task_id, status="running", progress=0, message="正在初始化 K8s 部署...")
        try:
            deploy_k8s(name, status_callback=_cb, log_callback=_log)
            _update_task(task_id, status="completed", progress=100,
             message="K8s 部署完成")
        except Exception as e:
            _update_task(task_id, status="error", progress=0,
                         message=str(e), error=str(e))

    scheduler.enqueue("deploy", Task("deploy", task_id, _run))
    _cleanup_old_tasks()
    return task_id


def batch_create_clusters(group_ids, master_count, node_count,
                          master_cores, master_memory,
                          node_cores, node_memory,
                          pve_node,
                          pve_server_id=0,
                          created_by=None,
                          status_callback=None, log_callback=None,
                          cancel_event=None):
    def report(p, m):
        if status_callback: status_callback(p, m)
    def _log(msg):
        if log_callback: log_callback(msg)

    total = len(group_ids)
    results = []
    for idx, gid in enumerate(group_ids):
        if cancel_event and cancel_event.is_set():
            raise K8sError("批量任务已取消")
        prefix = f"[{idx + 1}/{total}] "
        _log(f"{prefix}开始为分组 ID={gid} 创建集群")
        try:
            name, cluster = create_cluster(
                master_count, node_count,
                master_cores, master_memory,
                node_cores, node_memory,
                pve_node,
                pve_server_id=pve_server_id,
                group_id=gid,
                created_by=created_by,
                status_callback=lambda p, m, idx=idx, total=total: report(
                    int((idx * 100 + p * 0.5) / total), m
                ),
                log_callback=lambda m, prefix=prefix: _log(prefix + m),
                cancel_event=cancel_event,
            )
            _log(f"{prefix}虚拟机创建完成，自动部署 K8s...")
            deploy_k8s(
                name,
                status_callback=lambda p, m, idx=idx, total=total: report(
                    int((idx * 100 + 50 + p * 0.5) / total), m
                ),
                log_callback=lambda m, prefix=prefix: _log(prefix + m),
            )
            results.append({"group_id": gid, "name": name, "status": "success"})
            report(int((idx + 1) * 100 / total), f"分组 {idx + 1}/{total} 完成")
        except Exception as e:
            if cancel_event and cancel_event.is_set():
                raise K8sError("批量任务已取消") from e
            _log(f"{prefix}失败: {e}，跳过本组")
            results.append({"group_id": gid, "status": "failed", "error": str(e)})
            report(int((idx + 1) * 100 / total), f"分组 {idx + 1}/{total} 失败，继续下一组")

    ok = sum(1 for r in results if r["status"] == "success")
    report(100, f"批量创建完成，成功 {ok}/{total} 组")
    return results


def batch_create_clusters_async(group_ids, master_count, node_count,
                                master_cores, master_memory,
                                node_cores, node_memory,
                                pve_node,
                                pve_server_id=0,
                                created_by=None,
                                class_id=None):
    task_ids = []
    for gid in group_ids:
        tid = create_cluster_async(
            master_count, node_count,
            master_cores, master_memory,
            node_cores, node_memory,
            pve_node,
            pve_server_id=pve_server_id,
            group_id=gid,
            class_id=class_id,
            created_by=created_by,
        )
        task_ids.append(tid)
    return task_ids
