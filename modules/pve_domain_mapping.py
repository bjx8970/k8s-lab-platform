"""Read-only P1-to-P3 identity mapping contract; no resource writes."""

from modules.vm_identity import positive_id, vm_identity, validate_node


class DomainMappingError(ValueError):
    pass


def _identifier(value, label):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise DomainMappingError(f"{label}必须显式指定")
    return value


def plan_pve_vm_mapping(server_bindings, vms):
    """Resolve explicitly registered connections and VM resources into domains.

    ``server_bindings`` maps legacy PVE server IDs to ``domain_id`` and
    ``connection_id``. Several connections may share a domain. A duplicated
    domain/VMID must name the very same pre-registered resource and owner;
    neither endpoint URL nor node is identity evidence.
    """
    if not isinstance(server_bindings, dict) or not isinstance(vms, (list, tuple)):
        raise DomainMappingError("平台映射和 VM 列表必须显式提供")
    bindings = {}
    connection_domains = {}
    for raw_id, binding in server_bindings.items():
        try:
            server_id = positive_id(raw_id, "PVE 服务器编号")
        except ValueError as exc:
            raise DomainMappingError(str(exc)) from None
        if server_id in bindings or not isinstance(binding, dict):
            raise DomainMappingError("PVE 服务器映射不唯一或无效")
        domain_id = _identifier(binding.get("domain_id"), "domain_id")
        connection_id = _identifier(binding.get("connection_id"), "connection_id")
        previous = connection_domains.setdefault(connection_id, domain_id)
        if previous != domain_id:
            raise DomainMappingError("同一 connection 不可归属不同 domain")
        bindings[server_id] = (domain_id, connection_id)

    result = {}
    for vm in vms:
        if not isinstance(vm, dict):
            raise DomainMappingError("VM 映射记录无效")
        try:
            server_id, vmid = vm_identity(vm.get("pve_server_id"), vm.get("vmid"))
            node = validate_node(vm.get("node"))
            cluster_id = positive_id(vm.get("cluster_id"), "Cluster 编号")
        except ValueError as exc:
            raise DomainMappingError(str(exc)) from None
        if server_id not in bindings:
            raise DomainMappingError("VM 所属 PVE server 缺少显式 domain/connection 映射")
        resource_id = _identifier(vm.get("resource_id"), "resource_id")
        domain_id, connection_id = bindings[server_id]
        key = (domain_id, "pve.qemu/v1", str(vmid))
        current = result.get(key)
        if current is None:
            result[key] = {
                "domain_id": domain_id, "driver_id": "pve.qemu/v1",
                "external_key": str(vmid), "resource_id": resource_id,
                "cluster_id": cluster_id, "connections": [connection_id],
                "legacy_pve_server_ids": [server_id],
                "locators": [{"connection_id": connection_id, "node": node}],
            }
        else:
            if current["resource_id"] != resource_id or current["cluster_id"] != cluster_id:
                raise DomainMappingError("同一 domain/VMID 的旧记录存在歧义，必须人工核对")
            if connection_id not in current["connections"]:
                current["connections"].append(connection_id)
            if server_id not in current["legacy_pve_server_ids"]:
                current["legacy_pve_server_ids"].append(server_id)
            locator = {"connection_id": connection_id, "node": node}
            if locator not in current["locators"]:
                current["locators"].append(locator)
    return [result[key] for key in sorted(result)]
