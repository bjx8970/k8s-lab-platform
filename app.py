import os
from functools import wraps

from flask import Flask, render_template, request, jsonify

from modules.db import (
    get_config, init_db, migrate_config_from_json, migrate_from_json, set_config,
)
from modules.pve_client import PVEClient, PVEError
from modules.openwrt_client import OpenWrtClient, OpenWrtError
from modules.k8s_manager import create_cluster, create_cluster_async, deploy_k8s_async, delete_cluster_async, list_clusters, get_cluster, delete_cluster, get_task_status, K8sError

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

init_db()
_base_dir = os.path.dirname(os.path.abspath(__file__))
migrate_from_json(os.path.join(_base_dir, ".k8s_clusters.json"))
migrate_config_from_json("pve", os.path.join(_base_dir, ".pve_config.json"))
migrate_config_from_json("openwrt", os.path.join(_base_dir, ".openwrt_config.json"))


def get_pve_client():
    cfg = get_config("pve")
    if not cfg:
        raise PVEError(f"PVE 未配置，请先在页面中保存配置")
    missing = [k for k in ("host", "user", "token_name", "token_value") if not cfg.get(k)]
    if missing:
        raise PVEError(f"PVE 配置不完整: {', '.join(missing)}，请先在页面中保存配置")
    return PVEClient(
        host=cfg["host"],
        user=cfg["user"],
        token_name=cfg["token_name"],
        token_value=cfg["token_value"],
        verify_ssl=cfg.get("verify_ssl", False),
        port=int(cfg.get("port", 8006)),
    )


def api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except PVEError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
    return wrapper


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/users")
def users():
    return render_template("users.html")


@app.route("/classes")
def classes():
    return render_template("classes.html")


@app.route("/k8s")
def k8s():
    clusters = list_clusters()
    ow_cfg = get_config("openwrt") or {}
    openwrt_host = ow_cfg.get("host", "")
    return render_template("k8s.html", clusters=clusters, openwrt_host=openwrt_host)


@app.route("/pve")
def pve():
    cfg = get_config("pve") or {}
    return render_template("pve.html", config=cfg)


@app.route("/api/pve/config", methods=["GET", "POST"])
@api_error_handler
def pve_config():
    if request.method == "POST":
        data = request.get_json() or {}
        if not data.get("host") or not data.get("user") or not data.get("token_name") or not data.get("token_value"):
            return jsonify({"error": "host、user、token_name、token_value 均为必填项"}), 400
        set_config("pve", data)
        return jsonify({"message": "配置已保存"})

    cfg = get_config("pve") or {}
    safe = {k: v for k, v in cfg.items() if k != "token_value"}
    return jsonify(safe)


@app.route("/api/pve/test", methods=["POST"])
@api_error_handler
def pve_test_connection():
    data = request.get_json() or {}
    client = PVEClient(
        host=data.get("host", ""),
        user=data.get("user", ""),
        token_name=data.get("token_name", ""),
        token_value=data.get("token_value", ""),
        verify_ssl=data.get("verify_ssl", False),
        port=int(data.get("port", 8006)),
    )
    version = client.connect()
    return jsonify({"message": "Connection successful", "version": version})


@app.route("/api/pve/nodes", methods=["GET"])
@api_error_handler
def pve_get_nodes():
    client = get_pve_client()
    return jsonify(client.get_nodes())


@app.route("/api/pve/debug", methods=["GET"])
@api_error_handler
def pve_debug():
    client = get_pve_client()
    nodes_raw = client.get_nodes()
    result = {"nodes": nodes_raw, "vms_by_node": {}}
    for n in nodes_raw:
        node_name = n.get("node", n.get("id", "unknown"))
        try:
            qemu_list = client.api.nodes(node_name).qemu.get()
            result["vms_by_node"][node_name] = qemu_list
        except Exception as e:
            result["vms_by_node"][node_name] = f"Error: {e}"
    return jsonify(result)


@app.route("/api/pve/vms", methods=["GET"])
@api_error_handler
def pve_get_vms():
    node = request.args.get("node")
    client = get_pve_client()
    return jsonify(client.get_vms(node))


@app.route("/api/pve/vms/<node>/<int:vmid>/status", methods=["GET"])
@api_error_handler
def pve_get_vm_status(node, vmid):
    client = get_pve_client()
    return jsonify(client.get_vm_status(node, vmid))


@app.route("/api/pve/vms/<node>/<int:vmid>/config", methods=["GET"])
@api_error_handler
def pve_get_vm_config(node, vmid):
    client = get_pve_client()
    return jsonify(client.get_vm_config(node, vmid))


@app.route("/api/pve/templates", methods=["GET"])
@api_error_handler
def pve_get_templates():
    node = request.args.get("node")
    client = get_pve_client()
    return jsonify(client.get_templates(node))


@app.route("/api/pve/nextid", methods=["GET"])
@api_error_handler
def pve_get_nextid():
    client = get_pve_client()
    return jsonify({"nextid": client.get_next_vmid()})


@app.route("/api/pve/clone", methods=["POST"])
@api_error_handler
def pve_clone_vm():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    node = data.get("node")
    vmid = data.get("vmid")
    newid = data.get("newid")
    name = data.get("name")
    config = data.get("config")
    if not all([node, vmid, newid, name]):
        return jsonify({"error": "Missing required fields: node, vmid, newid, name"}), 400
    client = get_pve_client()
    result = client.clone_template(node, vmid, newid, name, config)
    return jsonify({"message": "VM cloned successfully", "newid": result}), 201


@app.route("/api/pve/create", methods=["POST"])
@api_error_handler
def pve_create_vm():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    node = data.get("node")
    template_vmid = data.get("template_vmid")
    config = data.get("config")
    if not all([node, template_vmid]):
        return jsonify({"error": "Missing required fields: node, template_vmid"}), 400
    client = get_pve_client()
    result = client.create_vm(node, template_vmid, config)
    return jsonify({"message": "VM created successfully", "newid": result}), 201


@app.route("/api/pve/vms/<node>/<int:vmid>/start", methods=["POST"])
@api_error_handler
def pve_start_vm(node, vmid):
    client = get_pve_client()
    result = client.start_vm(node, vmid)
    return jsonify(result)


@app.route("/api/pve/vms/<node>/<int:vmid>/stop", methods=["POST"])
@api_error_handler
def pve_stop_vm(node, vmid):
    data = request.get_json() or {}
    force = data.get("force", False)
    client = get_pve_client()
    result = client.stop_vm(node, vmid, force)
    return jsonify(result)


@app.route("/api/pve/vms/<node>/<int:vmid>", methods=["DELETE"])
@api_error_handler
def pve_release_vm(node, vmid):
    data = request.get_json() or {}
    purge = data.get("purge", True)
    client = get_pve_client()
    result = client.release_vm(node, vmid, purge)
    return jsonify(result)


@app.route("/openwrt")
def openwrt():
    cfg = get_config("openwrt") or {}
    return render_template("openwrt.html", config=cfg)


def get_openwrt_client():
    cfg = get_config("openwrt")
    if not cfg:
        raise OpenWrtError("OpenWrt 未配置，请先在页面中保存配置")
    missing = [k for k in ("host", "username", "password") if not cfg.get(k)]
    if missing:
        raise OpenWrtError(f"OpenWrt 配置不完整: {', '.join(missing)}")
    return OpenWrtClient(
        host=cfg["host"],
        username=cfg["username"],
        password=cfg["password"],
        port=int(cfg.get("port", 22)),
    )


def openwrt_api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except OpenWrtError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
    return wrapper


@app.route("/api/openwrt/config", methods=["GET", "POST"])
@openwrt_api_error_handler
def openwrt_config():
    if request.method == "POST":
        data = request.get_json() or {}
        if not data.get("host") or not data.get("username") or not data.get("password"):
            return jsonify({"error": "host、username、password 均为必填项"}), 400
        set_config("openwrt", data)
        return jsonify({"message": "配置已保存"})
    cfg = get_config("openwrt") or {}
    safe = {k: v for k, v in cfg.items() if k != "password"}
    return jsonify(safe)


@app.route("/api/openwrt/test", methods=["POST"])
@openwrt_api_error_handler
def openwrt_test():
    data = request.get_json() or {}
    client = OpenWrtClient(
        host=data.get("host", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        port=int(data.get("port", 22)),
    )
    version = client.connect()
    client.close()
    return jsonify({"message": "SSH connection successful", "version": version})


def _with_openwrt(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        client = get_openwrt_client()
        client.connect()
        try:
            result = f(client, *args, **kwargs)
            return jsonify(result)
        except OpenWrtError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
        finally:
            try:
                client.close()
            except Exception:
                pass
    return wrapper


@app.route("/api/openwrt/vlans", methods=["GET"])
@openwrt_api_error_handler
def openwrt_get_vlans():
    client = get_openwrt_client()
    client.connect()
    try:
        vlans = client.get_vlan_devices()
        return jsonify(list(vlans.values()))
    finally:
        client.close()


@app.route("/api/openwrt/vlans", methods=["POST"])
@openwrt_api_error_handler
def openwrt_create_vlan():
    data = request.get_json() or {}
    name = data.get("name")
    iface = data.get("iface")
    vid = data.get("vid")
    if not all([name, iface, vid]):
        return jsonify({"error": "name、iface、vid 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_vlan_device(name, iface, vid)
        return jsonify({"message": f"VLAN device {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/vlans/<name>", methods=["DELETE"])
@openwrt_api_error_handler
def openwrt_delete_vlan(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_vlan_device(name)
        return jsonify({"message": f"VLAN device {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/interfaces", methods=["GET"])
@openwrt_api_error_handler
def openwrt_get_interfaces():
    client = get_openwrt_client()
    client.connect()
    try:
        ifaces = client.get_interfaces()
        return jsonify(list(ifaces.values()))
    finally:
        client.close()


@app.route("/api/openwrt/interfaces", methods=["POST"])
@openwrt_api_error_handler
def openwrt_create_interface():
    data = request.get_json() or {}
    name = data.get("name")
    device = data.get("device")
    proto = data.get("proto", "static")
    ipaddr = data.get("ipaddr")
    netmask = data.get("netmask", "255.255.255.0")
    if not all([name, device]):
        return jsonify({"error": "name、device 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_interface(name, device, proto, ipaddr, netmask)
        zones = client.get_firewall_zones()
        for sec_name, zone in zones.items():
            if zone.get("name", "").lower() == "lan":
                client.add_interface_to_zone(sec_name, name)
                break
        return jsonify({"message": f"Interface {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/interfaces/<name>", methods=["PUT"])
@openwrt_api_error_handler
def openwrt_update_interface(name):
    data = request.get_json() or {}
    client = get_openwrt_client()
    client.connect()
    try:
        client.update_interface(name, **data)
        return jsonify({"message": f"Interface {name} updated"})
    finally:
        client.close()


@app.route("/api/openwrt/interfaces/<name>", methods=["DELETE"])
@openwrt_api_error_handler
def openwrt_delete_interface(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_interface(name)
        return jsonify({"message": f"Interface {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/dhcp", methods=["GET"])
@openwrt_api_error_handler
def openwrt_get_dhcp():
    client = get_openwrt_client()
    client.connect()
    try:
        pools = client.get_dhcp_pools()
        return jsonify(list(pools.values()))
    finally:
        client.close()


@app.route("/api/openwrt/dhcp", methods=["POST"])
@openwrt_api_error_handler
def openwrt_create_dhcp():
    data = request.get_json() or {}
    name = data.get("name")
    interface = data.get("interface")
    start = data.get("start", "100")
    limit = data.get("limit", "150")
    leasetime = data.get("leasetime", "12h")
    dhcpv4 = data.get("dhcpv4", "server")
    if not all([name, interface]):
        return jsonify({"error": "name、interface 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_dhcp_pool(name, interface, start, limit, leasetime, dhcpv4)
        return jsonify({"message": f"DHCP pool {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/dhcp/<name>", methods=["DELETE"])
@openwrt_api_error_handler
def openwrt_delete_dhcp(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_dhcp_pool(name)
        return jsonify({"message": f"DHCP pool {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/dnsmasq", methods=["GET"])
@openwrt_api_error_handler
def openwrt_get_dnsmasq():
    client = get_openwrt_client()
    client.connect()
    try:
        instances = client.get_dnsmasq_instances()
        return jsonify(list(instances.values()))
    finally:
        client.close()


@app.route("/api/openwrt/dnsmasq", methods=["POST"])
@openwrt_api_error_handler
def openwrt_create_dnsmasq():
    data = request.get_json() or {}
    name = data.get("name")
    interface = data.get("interface")
    listen_address = data.get("listen_address")
    domain = data.get("domain")
    if not all([name, interface, listen_address, domain]):
        return jsonify({"error": "name、interface、listen_address、domain 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_dnsmasq(name, interface, listen_address, domain)
        return jsonify({"message": f"dnsmasq instance {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/dnsmasq/<name>", methods=["DELETE"])
@openwrt_api_error_handler
def openwrt_delete_dnsmasq(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_dnsmasq(name)
        return jsonify({"message": f"dnsmasq instance {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/firewall/zones", methods=["GET"])
@openwrt_api_error_handler
def openwrt_get_firewall_zones():
    client = get_openwrt_client()
    client.connect()
    try:
        zones = client.get_firewall_zones()
        return jsonify(list(zones.values()))
    finally:
        client.close()


@app.route("/api/openwrt/firewall/<zone>/interfaces/<interface>", methods=["POST"])
@openwrt_api_error_handler
def openwrt_add_interface_to_zone(zone, interface):
    client = get_openwrt_client()
    client.connect()
    try:
        client.add_interface_to_zone(zone, interface)
        return jsonify({"message": f"Interface {interface} added to zone {zone}"})
    finally:
        client.close()


@app.route("/api/openwrt/firewall/<zone>/interfaces/<interface>", methods=["DELETE"])
@openwrt_api_error_handler
def openwrt_remove_interface_from_zone(zone, interface):
    client = get_openwrt_client()
    client.connect()
    try:
        client.remove_interface_from_zone(zone, interface)
        return jsonify({"message": f"Interface {interface} removed from zone {zone}"})
    finally:
        client.close()


@app.route("/api/openwrt/restart/network", methods=["POST"])
@openwrt_api_error_handler
def openwrt_restart_network():
    client = get_openwrt_client()
    client.connect()
    try:
        client.restart_network()
        return jsonify({"message": "Network restarted"})
    finally:
        client.close()


@app.route("/api/openwrt/restart/firewall", methods=["POST"])
@openwrt_api_error_handler
def openwrt_restart_firewall():
    client = get_openwrt_client()
    client.connect()
    try:
        client.restart_firewall()
        return jsonify({"message": "Firewall restarted"})
    finally:
        client.close()


@app.route("/api/openwrt/restart/dnsmasq", methods=["POST"])
@openwrt_api_error_handler
def openwrt_restart_dnsmasq():
    client = get_openwrt_client()
    client.connect()
    try:
        client.exec("/etc/init.d/dnsmasq restart", tolerant=True)
        return jsonify({"message": "dnsmasq restarted"})
    finally:
        client.close()



def k8s_api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (K8sError, PVEError, OpenWrtError) as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
    return wrapper


@app.route("/api/k8s/clusters", methods=["GET"])
@k8s_api_error_handler
def k8s_list_clusters():
    data = list_clusters()
    safe = {}
    for name, c in data.items():
        entry = {k: v for k, v in c.items() if k != "ssh_private_key"}
        safe[name] = entry
    return jsonify(safe)


@app.route("/api/k8s/clusters", methods=["POST"])
@k8s_api_error_handler
def k8s_create_cluster():
    data = request.get_json() or {}
    master_count = int(data.get("master_count", 1))
    node_count = int(data.get("node_count", 1))
    master_cores = int(data.get("master_cores", 4))
    master_memory = int(data.get("master_memory", 4096))
    node_cores = int(data.get("node_cores", 4))
    node_memory = int(data.get("node_memory", 4096))
    pve_node = data.get("pve_node", "")
    template_vmid = int(data.get("template_vmid", 9000))
    password = data.get("password", "k8s.1234")

    if master_count < 1:
        return jsonify({"error": "主节点数量至少为 1"}), 400
    if node_count < 1:
        return jsonify({"error": "子节点数量至少为 1"}), 400
    if not pve_node:
        return jsonify({"error": "请选择 PVE 节点"}), 400
    if not template_vmid:
        return jsonify({"error": "请选择模板 VMID"}), 400

    name, cluster = create_cluster(
        master_count, node_count,
        master_cores, master_memory,
        node_cores, node_memory,
        pve_node, template_vmid,
        password=password,
    )
    safe = {k: v for k, v in cluster.items() if k != "ssh_private_key"}
    return jsonify({"name": name, "cluster": safe}), 201


@app.route("/api/k8s/clusters/<name>", methods=["DELETE"])
@k8s_api_error_handler
def k8s_delete_cluster(name):
    task_id = delete_cluster_async(name)
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/create", methods=["POST"])
@k8s_api_error_handler
def k8s_create_cluster_async_route():
    data = request.get_json() or {}
    master_count = int(data.get("master_count", 1))
    node_count = int(data.get("node_count", 1))
    master_cores = int(data.get("master_cores", 4))
    master_memory = int(data.get("master_memory", 4096))
    node_cores = int(data.get("node_cores", 4))
    node_memory = int(data.get("node_memory", 4096))
    pve_node = data.get("pve_node", "")
    template_vmid = int(data.get("template_vmid", 9000))
    password = data.get("password", "k8s.1234")

    if master_count < 1:
        return jsonify({"error": "主节点数量至少为 1"}), 400
    if node_count < 1:
        return jsonify({"error": "子节点数量至少为 1"}), 400
    if not pve_node:
        return jsonify({"error": "请选择 PVE 节点"}), 400
    if not template_vmid:
        return jsonify({"error": "请选择模板 VMID"}), 400

    task_id = create_cluster_async(
        master_count, node_count,
        master_cores, master_memory,
        node_cores, node_memory,
        pve_node, template_vmid,
        password=password,
    )
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/tasks/<task_id>", methods=["GET"])
@k8s_api_error_handler
def k8s_get_task(task_id):
    status = get_task_status(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(status)


@app.route("/api/k8s/clusters/<name>/ssh-key", methods=["GET"])
@k8s_api_error_handler
def k8s_get_ssh_key(name):
    cluster = get_cluster(name)
    if not cluster:
        return jsonify({"error": "Cluster not found"}), 404
    return jsonify({
        "private_key": cluster.get("ssh_private_key", ""),
        "public_key": cluster.get("ssh_public_key", ""),
    })


@app.route("/api/k8s/clusters/<name>/deploy", methods=["POST"])
@k8s_api_error_handler
def k8s_deploy_cluster(name):
    cluster = get_cluster(name)
    if not cluster:
        return jsonify({"error": "集群不存在"}), 404
    if cluster.get("status") != "running":
        return jsonify({"error": "集群状态异常，无法部署 K8s"}), 400
    task_id = deploy_k8s_async(name)
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/clusters/<name>/upload-ssh-key", methods=["POST"])
@k8s_api_error_handler
def k8s_upload_ssh_key(name):
    from modules.k8s_manager import get_cluster
    cluster = get_cluster(name)
    if not cluster:
        return jsonify({"error": "Cluster not found"}), 404

    priv_key = cluster.get("ssh_private_key", "")
    if not priv_key:
        return jsonify({"error": "No SSH private key found for this cluster"}), 400

    client_vm = None
    for vm_name, vm_info in cluster.get("vms", {}).items():
        if vm_name.startswith("client-"):
            client_vm = vm_info
            break
    if not client_vm:
        return jsonify({"error": "No client VM found in this cluster"}), 400

    client = get_pve_client()
    client.connect()
    node = client_vm["node"]
    vmid = client_vm["vmid"]

    client.guest_exec(node, vmid,
        ["sh", "-c", "mkdir -p /home/k8s/.ssh && chmod 700 /home/k8s/.ssh"])
    client.guest_exec(node, vmid,
        ["tee", "/home/k8s/.ssh/id_rsa"], input_data=priv_key)
    client.guest_exec(node, vmid,
        ["chmod", "600", "/home/k8s/.ssh/id_rsa"])
    client.guest_exec(node, vmid,
        ["chown", "-R", "k8s:k8s", "/home/k8s/.ssh"])
    return jsonify({"message": "SSH private key uploaded to client VM"})


@app.route("/k8s/logs/<task_id>")
@k8s_api_error_handler
def k8s_logs_page(task_id):
    return render_template("k8s_logs.html", task_id=task_id)


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
