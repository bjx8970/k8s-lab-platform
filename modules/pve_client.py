from proxmoxer import ProxmoxAPI
from proxmoxer.core import ResourceException


class PVEError(Exception):
    pass


class PVEClient:
    def __init__(self, host, user, token_name, token_value,
                 verify_ssl=True, port=8006):
        self.host = host
        self.user = user
        self.token_name = token_name
        self.token_value = token_value
        self.verify_ssl = verify_ssl
        self.port = port
        self._api = None

    def connect(self):
        try:
            self._api = ProxmoxAPI(
                host=self.host,
                user=self.user,
                token_name=self.token_name,
                token_value=self.token_value,
                verify_ssl=self.verify_ssl,
                port=self.port,
            )
            version_info = self._api.version.get()
            return version_info
        except Exception as e:
            err = str(e)
            if "CERTIFICATE_VERIFY_FAILED" in err or "SSL" in err.upper():
                hint = "请取消勾选设置中的 Verify SSL 后再试"
                raise PVEError(f"SSL 证书验证失败，{hint}: {e}") from e
            raise PVEError(f"Failed to connect to PVE: {e}") from e

    @property
    def api(self):
        if self._api is None:
            self.connect()
        return self._api

    def get_nodes(self):
        try:
            return self.api.nodes.get()
        except Exception as e:
            raise PVEError(f"Failed to get nodes: {e}") from e

    def get_vms(self, node=None):
        try:
            if node:
                vms = self.api.nodes(node).qemu.get()
            else:
                vms = []
                for n in self.get_nodes():
                    node_vms = self.api.nodes(n["node"]).qemu.get()
                    for vm in node_vms:
                        vm["node"] = n["node"]
                    vms.extend(node_vms)
            return vms
        except Exception as e:
            raise PVEError(f"Failed to get VMs: {e}") from e

    def get_vm_status(self, node, vmid):
        try:
            return self.api.nodes(node).qemu(vmid).status.current.get()
        except Exception as e:
            raise PVEError(f"Failed to get VM status: {e}") from e

    def get_vm_config(self, node, vmid):
        try:
            return self.api.nodes(node).qemu(vmid).config.get()
        except Exception as e:
            raise PVEError(f"Failed to get VM config: {e}") from e

    def get_templates(self, node=None):
        try:
            all_vms = self.get_vms(node)
            return [vm for vm in all_vms if vm.get("template") == 1]
        except Exception as e:
            raise PVEError(f"Failed to get templates: {e}") from e

    def get_next_vmid(self):
        try:
            return int(self.api.cluster.nextid.get())
        except Exception as e:
            raise PVEError(f"Failed to get next VM ID: {e}") from e

    def clone_template(self, node, vmid, newid, name, config=None):
        try:
            clone_params = {"newid": newid, "name": name}
            if config:
                for key in ("full", "storage", "format", "pool", "snapname"):
                    if key in config:
                        clone_params[key] = config[key]
            self.api.nodes(node).qemu(vmid).clone.post(**clone_params)
            if config:
                self._update_vm_config(node, newid, config)
            return newid
        except Exception as e:
            raise PVEError(f"Failed to clone template: {e}") from e

    def create_vm(self, node, template_vmid, config=None, newid=None):
        try:
            if newid is None:
                newid = self.get_next_vmid()
            name = config.get("name", f"vm-{newid}") if config else f"vm-{newid}"
            return self.clone_template(node, template_vmid, newid, name, config)
        except Exception as e:
            raise PVEError(f"Failed to create VM: {e}") from e

    def _update_vm_config(self, node, vmid, config):
        skip_keys = {"full", "storage", "format", "pool", "snapname"}
        update_params = {k: v for k, v in config.items() if k not in skip_keys}
        if update_params:
            self.api.nodes(node).qemu(vmid).config.put(**update_params)

    def update_vm_config(self, node, vmid, config):
        try:
            self._update_vm_config(node, vmid, config)
        except Exception as e:
            raise PVEError(f"Failed to update VM config: {e}") from e

    def start_vm(self, node, vmid):
        try:
            status = self.get_vm_status(node, vmid)
            if status.get("status") == "running":
                return {"message": f"VM {vmid} is already running"}
            self.api.nodes(node).qemu(vmid).status.start.post()
            return {"message": f"VM {vmid} started"}
        except Exception as e:
            raise PVEError(f"Failed to start VM {vmid}: {e}") from e

    def reboot_vm(self, node, vmid, timeout=120):
        try:
            status = self.get_vm_status(node, vmid)
            if status.get("status") != "running":
                self.api.nodes(node).qemu(vmid).status.start.post()
                import time
                for _ in range(timeout // 2):
                    time.sleep(2)
                    current = self.get_vm_status(node, vmid)
                    if current.get("status") == "running":
                        break
                return {"message": f"VM {vmid} started (was not running)"}
            self.api.nodes(node).qemu(vmid).status.reboot.post()
            import time
            for _ in range(timeout // 2):
                time.sleep(2)
                current = self.get_vm_status(node, vmid)
                if current.get("status") == "running":
                    return {"message": f"VM {vmid} rebooted"}
            raise PVEError(f"VM {vmid} did not come back up within {timeout}s")
        except Exception as e:
            raise PVEError(f"Failed to reboot VM {vmid}: {e}") from e

    def stop_vm(self, node, vmid, force=False):
        try:
            status = self.get_vm_status(node, vmid)
            if status.get("status") != "running":
                return {"message": f"VM {vmid} is not running"}
            action = "stop" if force else "shutdown"
            getattr(self.api.nodes(node).qemu(vmid).status, action).post()
            return {"message": f"VM {vmid} stopped"}
        except Exception as e:
            raise PVEError(f"Failed to stop VM {vmid}: {e}") from e

    def guest_exec(self, node, vmid, command, input_data=None):
        try:
            params = {"command": command}
            if input_data is not None:
                params["input-data"] = input_data
            return self.api.nodes(node).qemu(vmid).agent.exec.post(**params)
        except Exception as e:
            raise PVEError(f"Failed to exec command in VM {vmid}: {e}") from e

    def guest_exec_status(self, node, vmid, pid):
        try:
            agent = self.api.nodes(node).qemu(vmid).agent
            return agent("exec-status").get(pid=pid)
        except Exception as e:
            raise PVEError(f"Failed to get exec status for VM {vmid}: {e}") from e

    def wait_for_guest_agent(self, node, vmid, timeout=90):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                self.api.nodes(node).qemu(vmid).agent.ping.get()
                return True
            except Exception:
                pass
            try:
                agent = self.api.nodes(node).qemu(vmid).agent
                getattr(agent, "network-get-interfaces").get()
                return True
            except Exception:
                pass
            time.sleep(1)
        raise PVEError(f"Guest agent did not become ready on VM {vmid} within {timeout}s")

    def get_vm_ip(self, node, vmid):
        try:
            agent = self.api.nodes(node).qemu(vmid).agent
            result = getattr(agent, "network-get-interfaces").get()
            for iface in result.get("result", []):
                for addr in iface.get("ip-addresses", []):
                    ip = addr.get("ip-address", "")
                    if addr.get("ip-address-type") == "ipv4" and not ip.startswith("127."):
                        return ip
            return None
        except Exception as e:
            raise PVEError(f"Failed to get VM IP for {vmid}: {e}") from e

    def release_vm(self, node, vmid, purge=True):
        try:
            status = self.get_vm_status(node, vmid)
            if status.get("status") == "running":
                self.api.nodes(node).qemu(vmid).status.stop.post()
                import time
                for _ in range(30):
                    time.sleep(2)
                    current = self.get_vm_status(node, vmid)
                    if current.get("status") != "running":
                        break
            params = {}
            if purge:
                params["purge"] = 1
            self.api.nodes(node).qemu(vmid).delete(**params)
            return {"message": f"VM {vmid} released"}
        except Exception as e:
            raise PVEError(f"Failed to release VM {vmid}: {e}") from e
