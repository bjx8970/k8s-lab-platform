"""Run the full suite with mandatory PostgreSQL and no skipped tests."""

import os
import shutil
import sys
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]


def check_environment():
    raw_url = os.environ.get("K8S_LAB_TEST_POSTGRES_URL")
    if not raw_url:
        raise RuntimeError("需要 K8S_LAB_TEST_POSTGRES_URL 指向一次性 PostgreSQL 数据库")
    try:
        url = make_url(raw_url)
    except Exception:
        raise RuntimeError("PostgreSQL 测试 URL 无效") from None
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("CI 测试必须使用 PostgreSQL")
    if shutil.which("node") is None:
        raise RuntimeError("CI 测试需要 Node.js")
    engine = None
    try:
        engine = create_engine(url, hide_parameters=True, connect_args={"timeout": 10})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        raise RuntimeError("无法连接一次性 PostgreSQL 测试数据库") from None
    finally:
        if engine is not None:
            engine.dispose()


def run_suite(suite):
    if suite.countTestCases() == 0:
        print("CI 未收集到任何测试", file=sys.stderr)
        return 1
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.skipped:
        print(f"CI 不允许跳过测试：{len(result.skipped)} 项未执行", file=sys.stderr)
    return 0 if result.wasSuccessful() and not result.skipped else 1


def main():
    try:
        check_environment()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"),
        pattern="test_*.py", top_level_dir=str(ROOT))
    return run_suite(suite)


if __name__ == "__main__":
    raise SystemExit(main())
