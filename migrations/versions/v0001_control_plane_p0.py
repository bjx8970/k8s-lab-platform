"""P0 control-plane schema and legacy schema baseline."""

from pathlib import Path

revision = "0001_control_plane_p0"
checksum_files = ("v0001_control_plane_p0.sql",)


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


def upgrade(connection):
    from modules.db import Base

    Base.metadata.create_all(bind=connection)
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    for statement in _statements(sql):
        connection.exec_driver_sql(statement)
