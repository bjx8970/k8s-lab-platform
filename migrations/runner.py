"""Fail-fast PostgreSQL migration runner with immutable checksums."""

import hashlib
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from sqlalchemy import text

MIGRATION_LOCK_ID = 5_005_897
VERSIONS_DIR = Path(__file__).with_name("versions")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Migration:
    revision: str
    checksum: str
    path: Path
    module: ModuleType


def _load(path):
    spec = importlib.util.spec_from_file_location(f"k8s_lab_migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"无法加载迁移文件: {path.name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def discover_migrations(directory=VERSIONS_DIR):
    result, seen = [], set()
    for path in sorted(directory.glob("v[0-9][0-9][0-9][0-9]_*.py")):
        module = _load(path)
        revision = getattr(module, "revision", None)
        if not isinstance(revision, str) or not callable(getattr(module, "upgrade", None)):
            raise MigrationError(f"迁移 {path.name} 缺少 revision 或 upgrade")
        if revision in seen:
            raise MigrationError(f"重复的迁移版本: {revision}")
        seen.add(revision)
        digest = hashlib.sha256(path.read_bytes())
        for dependency in getattr(module, "checksum_files", ()):
            dep = path.with_name(dependency)
            if not dep.is_file():
                raise MigrationError(f"迁移依赖不存在: {dependency}")
            digest.update(dependency.encode())
            digest.update(dep.read_bytes())
        result.append(Migration(revision, digest.hexdigest(), path, module))
    if not result:
        raise MigrationError("未发现任何数据库迁移")
    if [m.revision for m in result] != sorted(m.revision for m in result):
        raise MigrationError("迁移文件顺序与 revision 顺序不一致")
    return result


def run_migrations(engine, migrations=None):
    if engine is None:
        raise MigrationError("数据库未配置，无法执行迁移")
    if engine.dialect.name != "postgresql":
        raise MigrationError("控制面迁移仅支持 PostgreSQL")
    ordered = list(migrations if migrations is not None else discover_migrations())
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        connection.execute(text("""CREATE TABLE IF NOT EXISTS cp_schema_migrations (
            revision VARCHAR(64) PRIMARY KEY, checksum CHAR(64) NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)"""))
        applied = {r.revision: r.checksum for r in connection.execute(text(
            "SELECT revision, checksum FROM cp_schema_migrations ORDER BY revision"))}
        unknown = sorted(set(applied) - {m.revision for m in ordered})
        if unknown:
            raise MigrationError(f"数据库包含当前程序未知的迁移版本: {', '.join(unknown)}")
        for migration in ordered:
            previous = applied.get(migration.revision)
            if previous is not None:
                if previous != migration.checksum:
                    raise MigrationError(f"迁移 checksum 不匹配: {migration.revision}")
                continue
            migration.module.upgrade(connection)
            connection.execute(text("INSERT INTO cp_schema_migrations (revision, checksum) "
                                    "VALUES (:revision, :checksum)"),
                               {"revision": migration.revision, "checksum": migration.checksum})
