"""P0 control-plane schema and legacy schema baseline."""

import json
import os
from pathlib import Path

revision = "0001_control_plane_p0"
checksum_files = ("v0001_legacy_baseline.sql", "v0001_control_plane_p0.sql")

ENCRYPTED_PREFIX = "enc:v1:"


def _statements(sql):
    """Split PostgreSQL SQL without breaking quoted or dollar-quoted bodies."""
    statements, current = [], []
    quote = None
    dollar = None
    index = 0
    while index < len(sql):
        if dollar:
            if sql.startswith(dollar, index):
                current.append(dollar)
                index += len(dollar)
                dollar = None
            else:
                current.append(sql[index])
                index += 1
            continue
        char = sql[index]
        if quote:
            current.append(char)
            index += 1
            if char == quote:
                if index < len(sql) and sql[index] == quote:
                    current.append(sql[index])
                    index += 1
                else:
                    quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "$":
            end = sql.find("$", index + 1)
            if end != -1:
                tag = sql[index:end + 1]
                if tag == "$$" or tag[1:-1].replace("_", "a").isalnum():
                    dollar = tag
                    current.append(tag)
                    index = end + 1
                    continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    if quote or dollar:
        raise ValueError("迁移 SQL 存在未闭合的引用")
    return statements


def _validate_secret(value, field_name):
    """Frozen credential validation — fails closed, never exposes secret content."""
    if value is None or value == "":
        return value
    if not isinstance(value, str) or not value.startswith(ENCRYPTED_PREFIX):
        raise RuntimeError(f"legacy credential validation failed: {field_name}")
    try:
        from cryptography.fernet import Fernet, InvalidToken
        raw_key = os.environ.get("K8S_LAB_CREDENTIAL_KEY", "")
        if not raw_key:
            raise RuntimeError("legacy credential validation failed: missing key")
        token = value[len(ENCRYPTED_PREFIX):].encode("ascii")
        Fernet(raw_key.encode("ascii")).decrypt(token)
    except RuntimeError:
        raise
    except (InvalidToken, ValueError, TypeError, UnicodeDecodeError):
        raise RuntimeError(f"legacy credential validation failed: {field_name}") from None
    except Exception:
        raise RuntimeError(f"legacy credential validation failed: {field_name}") from None
    return value


def _migrate_pve_config(connection):
    """Equivalent of modules.db.migrate_pve_config(), frozen for v0001."""
    from sqlalchemy import text

    row = connection.execute(text("SELECT COUNT(*) FROM pve_servers")).scalar()
    if row > 0:
        return

    config_row = connection.execute(text("SELECT value FROM config WHERE key='pve'")).first()
    if config_row is None:
        return

    try:
        cfg = json.loads(config_row[0])
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError("legacy credential validation failed: invalid pve config JSON")

    if not isinstance(cfg, dict):
        raise RuntimeError("legacy credential validation failed: pve config must be a JSON object")

    token_value = _validate_secret(cfg.get("token_value", ""), "pve.token_value")

    port = cfg.get("port", 8006)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise RuntimeError("legacy credential validation failed: invalid pve port")

    connection.execute(text(
        "INSERT INTO pve_servers(name,host,port,\"user\",token_name,token_value,node,template_vmid) "
        "VALUES(:name,:host,:port,:user,:token_name,:token_value,:node,9000)"),
        {"name": "default", "host": cfg.get("host", ""), "port": port,
         "user": cfg.get("user", ""), "token_name": cfg.get("token_name", ""),
         "token_value": token_value, "node": cfg.get("node", "")})

    connection.execute(text("DELETE FROM config WHERE key='pve'"))


def _migrate_openwrt_to_pve_servers(connection):
    """Equivalent of modules.db._migrate_openwrt_to_pve_servers(), frozen for v0001."""
    from sqlalchemy import text

    config_row = connection.execute(text("SELECT value FROM config WHERE key='openwrt'")).first()
    if config_row is None:
        return

    try:
        ow_cfg = json.loads(config_row[0])
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError("legacy credential validation failed: invalid openwrt config JSON")

    if not isinstance(ow_cfg, dict):
        raise RuntimeError("legacy credential validation failed: openwrt config must be a JSON object")

    servers = connection.execute(text("SELECT id FROM pve_servers WHERE ow_host=''")).fetchall()
    if not servers:
        return

    password = _validate_secret(ow_cfg.get("password", ""), "openwrt.password")
    host = ow_cfg.get("host", "")
    port = ow_cfg.get("port", 22)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise RuntimeError("legacy credential validation failed: invalid openwrt port")
    username = ow_cfg.get("username", "")

    for server in servers:
        connection.execute(text(
            "UPDATE pve_servers SET ow_host=:host, ow_port=:port, ow_username=:username, ow_password=:password "
            "WHERE id=:id AND ow_host=''"),
            {"host": host, "port": port, "username": username, "password": password, "id": server[0]})


def upgrade(connection):
    # 1. Frozen legacy baseline
    legacy_sql = Path(__file__).with_name("v0001_legacy_baseline.sql").read_text(encoding="utf-8")
    for stmt in _statements(legacy_sql):
        connection.exec_driver_sql(stmt)

    # 2. Additive normalization (columns, indexes) and data migrations
    p0_sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    for stmt in _statements(p0_sql):
        connection.exec_driver_sql(stmt)

    # 3. Frozen data migrations — fail closed on invalid credentials
    _migrate_pve_config(connection)
    _migrate_openwrt_to_pve_servers(connection)