import paramiko
import time


class OpenWrtError(Exception):
    pass


def _parse_uci_show(output, section_type=None):
    sections = {}
    current_name = None
    current_type = None
    current = None

    for line in output.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("package"):
            continue
        if "=" in line and not line.startswith("."):
            parts = line.split("=", 1)
            key = parts[0]
            val = parts[1].strip("'")
            if "." not in key:
                continue
            if key.count(".") == 1 and key.split(".")[0].isalpha():
                sec = key.split(".")[1]
                if "=" in val or val.isalpha():
                    current_name = sec
                    current_type = val.strip("'")
                    current = {"_type": current_type, "_name": current_name}
                    sections[current_name] = current
                    continue
            key_path = key.split(".")
            if len(key_path) == 3:
                _, sec, attr = key_path
                if sec not in sections:
                    sections[sec] = {"_name": sec, "_type": "unknown"}
                sections[sec][attr] = val

    if section_type:
        return {k: v for k, v in sections.items() if v.get("_type") == section_type}
    return sections


def _build_indexed_sections(output):
    result = {}
    for line in output.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if "@" in line and "=" in line:
            parts = line.split("=", 1)
            key = parts[0]
            val = parts[1].strip("'")
            idx = key.find("@")
            dot = key.find(".", idx)
            section_path = key[idx + 1:dot] if dot > idx else key[idx + 1:]
            attr = key[dot + 1:] if dot > idx else None
            if attr is None:
                continue
            arr = section_path.rstrip("]").split("[")
            if arr:
                sec_name = f"@{arr[0]}"
                if sec_name not in result:
                    result[sec_name] = {"_type": val, "_name": sec_name}
                result[sec_name][attr] = val
    return result


class OpenWrtClient:
    def __init__(self, host, username, password, port=22):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self._ssh = None

    def connect(self):
        try:
            self._ssh = paramiko.SSHClient()
            self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._ssh.connect(
                self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                timeout=10,
            )
            _, stdout, _ = self._ssh.exec_command("uci --version")
            version = stdout.read().decode().strip()
            return version
        except Exception as e:
            raise OpenWrtError(f"SSH connection failed: {e}") from e

    def exec(self, command, tolerant=False):
        if not self._ssh:
            raise OpenWrtError("Not connected")
        try:
            stdin, stdout, stderr = self._ssh.exec_command(command, timeout=30)
            stdout.channel.settimeout(120)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode().strip()
            err = stderr.read().decode().strip()
            if exit_code != 0 and err:
                if tolerant:
                    return out
                raise OpenWrtError(f"Command failed (exit={exit_code}): {err}")
            return out
        except OpenWrtError:
            raise
        except Exception as e:
            raise OpenWrtError(f"SSH exec error: {e}") from e

    def close(self):
        if self._ssh:
            self._ssh.close()
            self._ssh = None

    def _uci_set(self, config, section, key, value):
        full = f"{config}.{section}.{key}"
        quoted = str(value).replace("'", "'\\''")
        self.exec(f"uci set '{full}={quoted}'")

    def _uci_add_list(self, config, section, key, value):
        full = f"{config}.{section}.{key}"
        quoted = str(value).replace("'", "'\\''")
        self.exec(f"uci add_list '{full}={quoted}'")

    def _uci_delete(self, config, section):
        self.exec(f"uci -q delete {config}.{section}")

    def _uci_commit(self, config):
        self.exec(f"uci commit {config}")

    # ── VLAN devices ──

    def get_vlan_devices(self):
        raw = self.exec("uci show network")
        sections = _parse_uci_show(raw, section_type="device")
        vlans = {}
        for section_name, sec in sections.items():
            if sec.get("type") == "8021q":
                vlans[section_name] = sec
        return vlans

    def _find_vlan_section(self, display_name):
        vlans = self.get_vlan_devices()
        for section_name, sec in vlans.items():
            if sec.get("name") == display_name:
                return section_name
        raise OpenWrtError(f"VLAN device '{display_name}' not found")

    def create_vlan_device(self, name, iface, vid):
        self.exec("uci add network device")
        self._uci_set("network", "@device[-1]", "type", "8021q")
        self._uci_set("network", "@device[-1]", "ifname", iface)
        self._uci_set("network", "@device[-1]", "vid", str(vid))
        self._uci_set("network", "@device[-1]", "name", name)
        self._uci_commit("network")
        return name

    def delete_vlan_device(self, name):
        section = self._find_vlan_section(name)
        self._uci_delete("network", section)
        self._uci_commit("network")

    # ── Interfaces ──

    def get_interfaces(self):
        raw = self.exec("uci show network")
        sections = _parse_uci_show(raw, section_type="interface")
        return sections

    def create_interface(self, name, device, proto="static",
                         ipaddr=None, netmask="255.255.255.0"):
        self.exec(f"uci set network.{name}=interface")
        self._uci_set("network", name, "device", device)
        self._uci_set("network", name, "proto", proto)
        if ipaddr and proto != "dhcp":
            self._uci_set("network", name, "ipaddr", ipaddr)
            self._uci_set("network", name, "netmask", netmask)
        self._uci_set("network", name, "multipath", "off")
        self._uci_commit("network")
        return name

    def update_interface(self, name, **kwargs):
        for key, value in kwargs.items():
            if value is not None:
                self._uci_set("network", name, key, value)
        self._uci_commit("network")

    def delete_interface(self, name):
        zones = self.get_firewall_zones()
        for sec_name, zone in zones.items():
            self.remove_interface_from_zone(sec_name, name)
        self._uci_delete("network", name)
        self._uci_commit("network")
        if zones:
            self.exec("/etc/init.d/firewall restart", tolerant=True)

    # ── DHCP pools ──

    def get_dhcp_pools(self):
        raw = self.exec("uci show dhcp")
        return _parse_uci_show(raw, section_type="dhcp")

    def create_dhcp_pool(self, name, interface, start="100",
                         limit="150", leasetime="12h", dhcpv4="server"):
        self.exec(f"uci set dhcp.{name}=dhcp")
        self._uci_set("dhcp", name, "interface", interface)
        self._uci_set("dhcp", name, "start", start)
        self._uci_set("dhcp", name, "limit", limit)
        self._uci_set("dhcp", name, "leasetime", leasetime)
        self._uci_set("dhcp", name, "dhcpv4", dhcpv4)
        self._uci_set("dhcp", name, "dynamicdhcp", "0")
        self._uci_commit("dhcp")
        return name

    def delete_dhcp_pool(self, name):
        self._uci_delete("dhcp", name)
        self._uci_commit("dhcp")

    # ── dnsmasq instances ──

    def get_dnsmasq_instances(self):
        raw = self.exec("uci show dhcp")
        return _parse_uci_show(raw, section_type="dnsmasq")

    def create_dnsmasq(self, name, interface, listen_address, domain,
                       authoritative="1", sequential_ip="1",
                       address_as_local="1", bind_interfaces="1"):
        self.exec(f"uci set dhcp.{name}=dnsmasq")
        self._uci_set("dhcp", name, "authoritative", authoritative)
        self._uci_set("dhcp", name, "domain", domain)
        self._uci_set("dhcp", name, "sequential_ip", sequential_ip)
        self._uci_set("dhcp", name, "address_as_local", address_as_local)
        self._uci_add_list("dhcp", name, "interface", interface)
        self._uci_add_list("dhcp", name, "listen_address", listen_address)
        self._uci_add_list("dhcp", name, "notinterface", "loopback")
        self._uci_set("dhcp", name, "bind_interfaces", bind_interfaces)
        self._uci_commit("dhcp")
        self.exec("/etc/init.d/dnsmasq restart", tolerant=True)
        return name

    def delete_dnsmasq(self, name):
        self._uci_delete("dhcp", name)
        self._uci_commit("dhcp")

    # ── Firewall zone ──

    def get_firewall_zones(self):
        raw = self.exec("uci show firewall")
        return _parse_uci_show(raw, section_type="zone")

    def add_interface_to_zone(self, zone_name, interface):
        self._uci_add_list("firewall", zone_name, "network", interface)
        self._uci_commit("firewall")
        self.exec("/etc/init.d/firewall restart", tolerant=True)

    def remove_interface_from_zone(self, zone_name, interface):
        current = self.exec(f"uci -q get firewall.{zone_name}.network")
        nets = [n.strip("' ") for n in current.split() if n.strip()]
        if not nets:
            return
        if interface in nets:
            self.exec(f"uci delete firewall.{zone_name}.network")
            for net in nets:
                if net != interface:
                    self._uci_add_list("firewall", zone_name, "network", net)
            self._uci_commit("firewall")

    # ── Port forwarding (redirect) ──

    def get_redirects(self):
        raw = self.exec("uci show firewall")
        return _parse_uci_show(raw, section_type="redirect")

    def create_redirect(self, name, src_dport, dest_ip, dest_port,
                        src="*", dest="lan", target="DNAT"):
        self.exec("uci add firewall redirect")
        self._uci_set("firewall", "@redirect[-1]", "name", name)
        self._uci_set("firewall", "@redirect[-1]", "src", src)
        self._uci_set("firewall", "@redirect[-1]", "dest", dest)
        self._uci_set("firewall", "@redirect[-1]", "target", target)
        self._uci_set("firewall", "@redirect[-1]", "src_dport", str(src_dport))
        self._uci_set("firewall", "@redirect[-1]", "dest_ip", dest_ip)
        self._uci_set("firewall", "@redirect[-1]", "dest_port", str(dest_port))
        self._uci_commit("firewall")
        self.exec("/etc/init.d/firewall restart", tolerant=True)
        return name

    def delete_redirect(self, name):
        redirects = self.get_redirects()
        for sec_name, sec in redirects.items():
            if sec.get("name") == name:
                self._uci_delete("firewall", sec_name)
                self._uci_commit("firewall")
                self.exec("/etc/init.d/firewall restart", tolerant=True)
                return
        raise OpenWrtError(f"Redirect '{name}' not found")

    # ── Static DHCP host ──

    def get_dhcp_hosts(self):
        raw = self.exec("uci show dhcp")
        return _parse_uci_show(raw, section_type="host")

    def create_dhcp_host(self, ip, mac):
        self.exec("uci add dhcp host")
        self._uci_add_list("dhcp", "@host[-1]", "mac", mac)
        self._uci_set("dhcp", "@host[-1]", "ip", ip)
        self._uci_commit("dhcp")

    def delete_dhcp_host(self, ip, skip_restart=False):
        hosts = self.get_dhcp_hosts()
        for sec_name, sec in hosts.items():
            if sec.get("ip") == ip:
                self._uci_delete("dhcp", sec_name)
                self._uci_commit("dhcp")
                if not skip_restart:
                    self.exec("/etc/init.d/dnsmasq restart", tolerant=True)
                return
        raise OpenWrtError(f"DHCP host with IP '{ip}' not found")

    # ── Service control ──

    def restart_network(self):
        self.exec("/etc/init.d/network restart", tolerant=True)

    def restart_firewall(self):
        self.exec("/etc/init.d/firewall restart", tolerant=True)
