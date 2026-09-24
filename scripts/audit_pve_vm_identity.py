#!/usr/bin/env python3
"""Read-only audit of legacy or upgraded PVE VM identities."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine
from sqlalchemy.engine import URL

from modules.identity_audit import audit_vm_identity


def configured_url():
    path = ROOT / ".db_config.json"
    if not path.is_file():
        raise ValueError("数据库未配置；请指定 --url-env 或配置 .db_config.json")
    config = json.loads(path.read_text(encoding="utf-8"))
    if config.get("type") != "postgresql":
        raise ValueError("数据库配置类型必须为 PostgreSQL")
    return URL.create(
        "postgresql+pg8000", username=config["user"], password=config["password"],
        host=config["host"], port=int(config.get("port", 5432)), database=config["database"],
    )


def main():
    parser = argparse.ArgumentParser(description="只读审计 PVE VM 身份与迁移前回填情况")
    parser.add_argument("--url-env", metavar="NAME", help="读取数据库 URL 的环境变量名称")
    args = parser.parse_args()
    url = os.environ.get(args.url_env) if args.url_env else configured_url()
    if not url:
        parser.error("指定的环境变量没有数据库 URL")
    engine = create_engine(url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            report = audit_vm_identity(connection)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report["ready"] else 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError) as exc:
        raise SystemExit(f"身份审计失败: {exc}") from None
