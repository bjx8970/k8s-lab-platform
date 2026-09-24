"""Immutable PostgreSQL v0002 VM identity migration."""

from pathlib import Path

revision = "0002_pve_vm_identity"
checksum_files = ("v0002_pve_vm_identity.sql",)


def upgrade(connection):
    source = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    statements = []
    current = []
    quote = None
    dollar = None
    index = 0
    while index < len(source):
        char = source[index]
        if dollar:
            if source.startswith(dollar, index):
                current.append(dollar)
                index += len(dollar)
                dollar = None
            else:
                current.append(char)
                index += 1
            continue
        if quote:
            current.append(char)
            index += 1
            if char == quote:
                if index < len(source) and source[index] == quote:
                    current.append(source[index])
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
            end = source.find("$", index + 1)
            if end != -1:
                tag = source[index:end + 1]
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
        else:
            current.append(char)
        index += 1
    if quote or dollar:
        raise RuntimeError("v0002 SQL 存在未闭合引用")
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    for statement in statements:
        connection.exec_driver_sql(statement)
