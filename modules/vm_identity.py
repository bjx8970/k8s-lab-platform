"""Canonical PVE virtual-machine identity helpers.

P1 uses ``(pve_server_id, vmid)`` as a stable transitional identity. P3 uses
``(domain_id, vmid)`` for business identity; connections are access endpoints.
The PVE node is a mutable locator and never selects a provider.
"""

from __future__ import annotations

import re
from typing import Any


class VmIdentityError(ValueError):
    pass


def positive_id(value: Any, field_name: str = "资源编号") -> int:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        try:
            return int(value)
        except ValueError:
            pass
    raise VmIdentityError(f"{field_name}必须是正整数")


def vm_identity(pve_server_id: Any, vmid: Any) -> tuple[int, int]:
    return (
        positive_id(pve_server_id, "PVE 服务器编号"),
        positive_id(vmid, "VMID"),
    )


def vm_identity_key(pve_server_id: Any, vmid: Any) -> str:
    server_id, normalized_vmid = vm_identity(pve_server_id, vmid)
    return f"pve-{server_id}-vm-{normalized_vmid}"


def validate_node(node: Any, *, required: bool = True) -> str | None:
    if node is None and not required:
        return None
    if not isinstance(node, str) or not node.strip():
        raise VmIdentityError("PVE 节点定位信息无效")
    normalized = node.strip()
    if len(normalized) > 32 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", normalized):
        raise VmIdentityError("PVE 节点定位信息无效")
    return normalized


def cluster_vm_entries(cluster: dict) -> list[tuple[str, dict]]:
    """Validate an entire cluster before executing any of its VM operations."""
    if not isinstance(cluster, dict):
        raise VmIdentityError("集群身份无效")
    server_id = positive_id(cluster.get("pve_server_id"), "PVE 服务器编号")
    vms = cluster.get("vms")
    if not isinstance(vms, dict):
        raise VmIdentityError("集群 VM 身份列表无效")
    entries, seen = [], set()
    for name, vm in vms.items():
        if not isinstance(name, str) or not name or not isinstance(vm, dict):
            raise VmIdentityError("VM 身份记录无效")
        identity = vm_identity(vm.get("pve_server_id"), vm.get("vmid"))
        if identity[0] != server_id:
            raise VmIdentityError("VM 平台身份与集群不匹配")
        if identity in seen:
            raise VmIdentityError("集群中存在重复 VM 身份")
        seen.add(identity)
        entries.append((name, {**vm, "pve_server_id": identity[0], "vmid": identity[1],
                               "node": validate_node(vm.get("node")),
                               "key": vm_identity_key(*identity)}))
    return entries


def cluster_vm(cluster: dict, pve_server_id: Any, vmid: Any) -> dict:
    identity = vm_identity(pve_server_id, vmid)
    entries = cluster_vm_entries(cluster)
    if positive_id(cluster.get("pve_server_id")) != identity[0]:
        raise VmIdentityError("VM 平台身份与集群不匹配")
    matches = [vm for _, vm in entries if vm_identity(vm["pve_server_id"], vm["vmid"]) == identity]
    if len(matches) != 1:
        raise VmIdentityError("VM 不存在或身份不唯一")
    return matches[0]


def cluster_identity_fingerprint(cluster: dict) -> tuple:
    entries = cluster_vm_entries(cluster)
    return (
        cluster.get("id"), cluster.get("name"), positive_id(cluster.get("pve_server_id")),
        cluster.get("created_by"), cluster.get("group_id"), cluster.get("class_id"),
        tuple(sorted((name, vm["pve_server_id"], vm["vmid"]) for name, vm in entries)),
    )


def client_vm_identity(cluster: dict) -> tuple[int, int]:
    clients = [vm for _, vm in cluster_vm_entries(cluster) if vm.get("role") == "client"]
    if len(clients) != 1:
        raise VmIdentityError("客户端虚拟机不存在或身份不唯一")
    return vm_identity(clients[0]["pve_server_id"], clients[0]["vmid"])
