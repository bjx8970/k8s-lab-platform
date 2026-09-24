"""Read-only VM identity inspection; no application/configuration imports.

This is an operational helper, not a dependency of any frozen migration.
"""

import re
from collections import defaultdict

from sqlalchemy import inspect, text


ISSUE_LABELS = {
    "missing_cluster": "VM 缺少 Cluster",
    "invalid_server": "Cluster 或 VM 缺少有效 PVE server",
    "missing_server": "VM 引用了不存在的 PVE server",
    "invalid_vmid": "VMID 必须是正整数",
    "invalid_node": "VM 节点定位信息无效",
    "conflicting_identity": "已填写的 VM 平台身份与 Cluster 冲突",
}


def _positive(value):
    return type(value) is int and value > 0


def audit_vm_identity(connection):
    """Describe legacy, partially backfilled, or upgraded rows without writes.

    ``backfillable`` counts only NULL/absent VM providers whose entire identity
    is valid and unique, not already-filled rows or rows requiring manual repair.
    Duplicate groups use the effective identity (explicit VM provider first).
    """
    columns = {column["name"] for column in inspect(connection).get_columns("vms")}
    provider = "v.pve_server_id" if "pve_server_id" in columns else "NULL"
    rows = connection.execute(text(
        "SELECT v.id AS vm_id, v.vm_name, v.cluster_id, v.vmid, v.node, "
        f"{provider} AS stored_pve_server_id, c.id AS found_cluster_id, "
        "c.name AS cluster_name, c.pve_server_id AS cluster_pve_server_id "
        "FROM vms v LEFT JOIN clusters c ON c.id=v.cluster_id ORDER BY v.id"
    )).mappings()
    servers = set(connection.execute(text("SELECT id FROM pve_servers")).scalars())
    result = {name: 0 for name in ISSUE_LABELS}
    result.update(vm_total=0, backfillable=0, already_filled=0, mapping=[], row_ids={})
    groups = defaultdict(list)
    for row in rows:
        item = dict(row)
        cluster_exists = item.pop("found_cluster_id") is not None
        cluster_provider = item["cluster_pve_server_id"]
        stored = item["stored_pve_server_id"]
        effective = stored if stored is not None else cluster_provider
        item["pve_server_id"] = effective
        item["needs_backfill"] = stored is None
        issues = []
        if not cluster_exists:
            issues.append("missing_cluster")
        if (cluster_exists and not _positive(cluster_provider)) or (
            stored is not None and not _positive(stored)
        ):
            issues.append("invalid_server")
        if (cluster_exists and _positive(cluster_provider) and cluster_provider not in servers) or (
            _positive(stored) and stored not in servers
        ):
            issues.append("missing_server")
        if stored is not None and cluster_exists and stored != cluster_provider:
            issues.append("conflicting_identity")
        if not _positive(item["vmid"]):
            issues.append("invalid_vmid")
        node = item["node"]
        if not isinstance(node, str) or len(node) > 32 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", node):
            issues.append("invalid_node")
        item["issues"] = issues
        for issue in issues:
            result[issue] += 1
            result["row_ids"].setdefault(issue, []).append(item["vm_id"])
        if _positive(effective) and _positive(item["vmid"]):
            groups[(effective, item["vmid"])].append(item)
        result["mapping"].append(item)

    duplicates = []
    for (provider_id, vmid), members in sorted(groups.items()):
        if len(members) > 1:
            duplicates.append({"pve_server_id": provider_id, "vmid": vmid,
                               "count": len(members), "vm_ids": [m["vm_id"] for m in members]})
            for member in members:
                member["issues"].append("duplicate_identity")
    result["duplicate_identities"] = duplicates
    result["row_ids"]["duplicate_identity"] = sorted(
        vm_id for group in duplicates for vm_id in group["vm_ids"]
    )
    for item in result["mapping"]:
        if not item["issues"]:
            result["backfillable" if item["needs_backfill"] else "already_filled"] += 1
    result["vm_total"] = len(result["mapping"])
    result["ready"] = not any(item["issues"] for item in result["mapping"])
    return result


def require_valid_vm_identity(connection):
    result = audit_vm_identity(connection)
    if not result["ready"]:
        labels = {**ISSUE_LABELS, "duplicate_identity": "同一平台存在重复 VMID"}
        problems = [f"{labels[issue]} (VM row IDs: {ids})"
                    for issue, ids in result["row_ids"].items() if ids]
        raise RuntimeError("VM 身份升级失败: " + "; ".join(problems))
    return result
