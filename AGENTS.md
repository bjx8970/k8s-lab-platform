# K8s Lab Platform — Agent Guide

## Quick start

```bash
venv\Scripts\activate    # Windows
pip install -r requirements.txt
python app.py            # Flask dev server on :5000
```

## Architecture

- `app.py` — Flask entrypoint, all routes defined here (no blueprints)
- `modules/db.py` — SQLAlchemy models + SQLite (`k8s_lab.db`), schema migration via raw `ALTER TABLE` in `init_db()`
- `modules/pve_client.py` — PVE API via `proxmoxer`
- `modules/openwrt_client.py` — OpenWrt SSH/UCI via `paramiko`
- `modules/k8s_manager.py` — cluster orchestration, in-process async tasks via `threading` + in-memory `_task_store` dict

## Key conventions

- **Language**: UI strings, error messages, and logs are all in Chinese (except SSH keys).
- **No tests, no CI, no linter, no type checker, no formatter config** exists in this repo.
- **No blueprints or app factory** — single `app = Flask(__name__)` at module level.
- **Dependencies**: flask, proxmoxer, paramiko, sqlalchemy, cryptography.
- **`.db` files are gitignored**; `k8s_lab.db` is auto-created at startup.
- **Only one commit** in git history.

## Cluster naming & network rules

| Property | Pattern |
|---|---|
| Cluster name | `k8s_{db_auto_increment_id}` |
| VLAN ID | `100 + id` |
| Subnet | `10.100.{id}.0/24` |
| Gateway | `10.100.{id}.1` |
| SSH port forward | WAN:`50000+id` → client:22 |
| VM names | `client-k8s{id}`, `master{i}-k8s{id}`, `node{i}-k8s{id}` |

## Editing notes

- Schema changes: add raw `ALTER TABLE` in `init_db()` wrapped in `try/except` (no alembic).
- Async task store (`_task_store`) is in-memory only — lost on restart, tasks older than 30 min auto-expire.
- When modifying PVE/OpenWrt API calls, consult `openwrt_client.py` and `pve_client.py` for the exact UCI/proxmoxer patterns used.
- SSH keys are Ed25519 (generated via `cryptography`), not RSA.
- `venv/` and `__pycache__/` are gitignored.
