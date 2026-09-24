"""Transactional legacy SQLite VM-table upgrade, independent of app startup."""

import re

from modules.identity_audit import require_valid_vm_identity


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _parts(body):
    """Split table declarations without splitting quoted/default expressions."""
    start, depth, quote, index = 0, 0, None, 0
    while index < len(body):
        char = body[index]
        if quote:
            if char == quote:
                if index + 1 < len(body) and body[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif char in "'\"`[":
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            yield body[start:index].strip()
            start = index + 1
        index += 1
    yield body[start:].strip()


_IDENTIFIER = r'(?:"(?:[^"]|"")+"|`[^`]+`|\[[^]]+\]|[A-Za-z_][A-Za-z_0-9]*)'


def _column_name(declaration):
    match = re.match(_IDENTIFIER, declaration)
    return match.group().strip('"`[]').replace('""', '"') if match else None


def _bare_vmid_unique(declaration):
    pattern = rf'^(?:CONSTRAINT\s+{_IDENTIFIER}\s+)?UNIQUE\s*\(\s*([^)]+)\s*\)'
    match = re.match(pattern, declaration, re.I)
    if not match:
        return False
    columns = list(_parts(match.group(1)))
    return len(columns) == 1 and _column_name(columns[0]).lower() == "vmid"


def _index_columns(connection, name):
    return [row[2] for row in connection.exec_driver_sql(f"PRAGMA index_info({_quote(name)})")]


def _complete(connection, columns, indexes, sql):
    normalized = re.sub(r'[\s"`\[\]]', '', sql).lower()
    foreign_keys = list(connection.exec_driver_sql("PRAGMA foreign_key_list(vms)"))
    fks = {(r[3], r[2], r[4]) for r in foreign_keys}
    groups = {}
    for row in foreign_keys:
        groups.setdefault(row[0], []).append((row[1], row[2], row[3], row[4]))
    compound = any(sorted((part[2], part[3]) for part in parts) ==
                   [("cluster_id", "id"), ("pve_server_id", "pve_server_id")]
                   and all(part[1] == "clusters" for part in parts)
                   for parts in groups.values())
    return (
        all(name in columns and columns[name][3] for name in ("cluster_id", "pve_server_id", "vmid", "node"))
        and all(f"check({name}>0)" in normalized for name in ("pve_server_id", "vmid"))
        and ("pve_server_id", "pve_servers", "id") in fks and compound
        and any(r[2] and not r[4] and _index_columns(connection, r[1]) == ["pve_server_id", "vmid"] for r in indexes)
        and not any(r[2] and _index_columns(connection, r[1]) == ["vmid"] for r in indexes)
    )


def _replacement_sql(connection, original, columns):
    opening, closing = original.index("("), original.rindex(")")
    declarations = []
    for declaration in _parts(original[opening + 1:closing]):
        if _bare_vmid_unique(declaration):
            continue
        name = _column_name(declaration)
        if name == "vmid":
            declaration = re.sub(r"\bUNIQUE\b(?:\s+ON\s+CONFLICT\s+\w+)?", "", declaration, flags=re.I)
        if name in ("cluster_id", "pve_server_id", "vmid", "node") and not columns[name][3]:
            declaration += " NOT NULL"
        declarations.append(declaration)
    if "pve_server_id" not in columns:
        # Columns must precede table-level constraints in SQLite's grammar.
        declarations.insert(0, "pve_server_id INTEGER NOT NULL")
    existing_fks = {(r[3], r[2], r[4]) for r in connection.exec_driver_sql("PRAGMA foreign_key_list(vms)")}
    if ("pve_server_id", "pve_servers", "id") not in existing_fks:
        declarations.append("FOREIGN KEY (pve_server_id) REFERENCES pve_servers(id)")
    declarations.append(
        "CONSTRAINT fk_vms_cluster_pve_server FOREIGN KEY (cluster_id, pve_server_id) "
        "REFERENCES clusters(id, pve_server_id)"
    )
    # Duplicate equivalent checks/compound uniqueness in a partial schema are
    # harmless; keep their original names rather than discarding local schema.
    normalized = re.sub(r'[\s"`\[\]]', '', original).lower()
    for field in ("pve_server_id", "vmid"):
        if f"check({field}>0)" not in normalized:
            declarations.append(f"CONSTRAINT ck_vms_{field}_positive CHECK ({field} > 0)")
    declarations.append("CONSTRAINT uq_vms_pve_server_vmid UNIQUE (pve_server_id, vmid)")
    return "CREATE TABLE vms__pve_identity_new (" + ",\n".join(declarations) + ")" + original[closing + 1:]


def upgrade_sqlite_vm_identity(target_engine):
    """Rebuild under a real BEGIN IMMEDIATE; any error rolls back all DDL/data.

    Retain unrelated columns/constraints, indexes, triggers, incoming/outgoing
    foreign keys, and connection PRAGMAs. Bare VMID uniqueness alone is removed.
    """
    if target_engine.dialect.name != "sqlite":
        raise ValueError("SQLite VM 身份升级仅支持 SQLite")
    with target_engine.connect() as connection:
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
        legacy_alter = connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar()
        connection.commit()
        # SQLite ignores foreign_keys changes inside a transaction. Disable it
        # only for this connection while rebuilding, and validate before commit.
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()
        try:
            # SQLAlchemy begin() alone does NOT emit BEGIN for sqlite3's legacy
            # transaction mode. This statement must precede even the first DDL.
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            original = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='vms'"
            ).scalar()
            if original is None:
                connection.commit()
                return
            columns = {r[1]: r for r in connection.exec_driver_sql("PRAGMA table_info(vms)")}
            indexes = connection.exec_driver_sql("PRAGMA index_list(vms)").fetchall()
            audit = require_valid_vm_identity(connection)
            connection.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_clusters_id_pve_server_id "
                "ON clusters(id, pve_server_id)"
            )
            if _complete(connection, columns, indexes, original):
                connection.commit()
                return
            bare_indexes = {r[1] for r in indexes if r[2] and _index_columns(connection, r[1]) == ["vmid"]}
            objects = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE tbl_name='vms' "
                "AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name"
            ).fetchall()
            create_sql = _replacement_sql(connection, original, columns)
            # table_xinfo includes generated columns, which must not be copied.
            names = [r[1] for r in connection.exec_driver_sql("PRAGMA table_xinfo(vms)") if r[6] == 0]
            if "pve_server_id" not in names:
                names.append("pve_server_id")
            expressions = [
                ("COALESCE(v.pve_server_id,c.pve_server_id)" if "pve_server_id" in columns else "c.pve_server_id")
                if name == "pve_server_id" else "v." + _quote(name) for name in names
            ]
            sequence = None
            if "AUTOINCREMENT" in original.upper():
                sequence = connection.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name='vms'").scalar()
            connection.exec_driver_sql(create_sql)
            connection.exec_driver_sql(
                "INSERT INTO vms__pve_identity_new (" + ",".join(map(_quote, names)) + ") SELECT "
                + ",".join(expressions) + " FROM vms v JOIN clusters c ON c.id=v.cluster_id"
            )
            if connection.exec_driver_sql("SELECT COUNT(*) FROM vms__pve_identity_new").scalar() != audit["vm_total"]:
                raise RuntimeError("SQLite VM 身份升级失败: 记录数校验失败")
            connection.exec_driver_sql("DROP TABLE vms")
            connection.exec_driver_sql("ALTER TABLE vms__pve_identity_new RENAME TO vms")
            if sequence is not None:
                connection.exec_driver_sql("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='vms'", (sequence,))
            for name, sql in objects:
                if name not in bare_indexes:
                    connection.exec_driver_sql(sql)
            for field in ("cluster_id", "node", "pve_server_id"):
                connection.exec_driver_sql(f"CREATE INDEX IF NOT EXISTS ix_vms_{field} ON vms({field})")
            failures = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if failures:
                raise RuntimeError(f"SQLite VM 身份升级失败: 外键校验失败 (table/row IDs: {failures})")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.exec_driver_sql(f"PRAGMA legacy_alter_table={int(legacy_alter)}")
            connection.exec_driver_sql(f"PRAGMA foreign_keys={int(foreign_keys)}")
            connection.commit()
