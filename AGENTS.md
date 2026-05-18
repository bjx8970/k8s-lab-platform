# K8s Lab Platform — Agent Guide

## Quick start

```bash
venv\Scripts\activate
pip install -r requirements.txt
python app.py            # Flask dev at :5000
```

## Architecture

- `app.py` — Flask entrypoint, all routes (no blueprints). `FLASK_SECRET_KEY` env var for production secret_key (default: `dev-secret-key-change-in-production`).
- `modules/db.py` — SQLAlchemy models + SQLite by default. Schema migration via raw `ALTER TABLE` in `init_db()` wrapped in `try/except` (no alembic).
- `modules/pve_client.py` — PVE API via `proxmoxer` (API token auth, not password). Port forwarding config uses UCI `redirect`.
- `modules/openwrt_client.py` — OpenWrt SSH/UCI via `paramiko`. All UCI ops commit immediately.
- `modules/k8s_manager.py` — cluster orchestration (create/delete/deploy K8s), async via `threading` + in-memory `_task_store` (lost on restart, 30 min expiry).
- `modules/pg_client.py` — PostgreSQL connection test via `pg8000` (used by DB config UI, not runtime persistence).

## Database

- **Default**: SQLite (`k8s_lab.db`, auto-created). `.db` files gitignored.
- **PostgreSQL mode**: set `{"type": "postgresql", "host": ..., "user": ..., "password": ..., "database": ...}` in `.db_config.json` and restart. Migration from SQLite via UI at `/db`.
- `modules/db.py` loads `.db_config.json` at import time to decide engine.

## Multi-PVE server support

PVE servers stored in `pve_servers` table (not single `config` key). Clusters reference `pve_server_id`. Old single-config data migrates on startup via `migrate_pve_config()`.

## K8s deployment (`deploy_k8s`)

After a cluster is created, you can deploy K8s via `POST /api/k8s/clusters/<name>/deploy`. It runs inside the client VM using kubeasz:
1. Downloads `ezdown` and `kubeasz_offline.tgz` from `http://10.11.43.82/download/`
2. Extracts to `/etc/kubeasz`, runs `ezdown -D` (Docker) + `ezdown -S` (kubeasz container)
3. Runs `ezctl new <cluster>` + `ezctl setup <cluster> all` inside the Docker container

Cluster naming follows same `k8s_{id}` pattern; K8s cluster name in kubeasz = `k8s_{id}`.

## Key conventions

- **Language**: UI strings, error messages, and logs are all in Chinese (except SSH keys).
- **No tests, no CI, no linter, no type checker, no formatter** configured.
- **SSH keys**: Ed25519 via `cryptography` (not RSA), stored per-cluster in DB.
- **Async tasks**: `create_cluster_async`, `delete_cluster_async`, `deploy_k8s_async` — all return `task_id`; poll via `GET /api/k8s/tasks/<task_id>`.
- **Dependencies**: flask, proxmoxer, paramiko, sqlalchemy, cryptography, pg8000. (`invoke` in requirements.txt is unused.)

## Cluster naming & network rules

| Property | Pattern |
|---|---|
| Cluster name | `k8s_{db_auto_increment_id}` |
| VLAN ID | `100 + id` |
| VLAN device | `eth1.{vlan_id}` |
| Subnet | `10.100.{id}.0/24` |
| Gateway | `10.100.{id}.1` |
| SSH port forward | WAN:`50000+id` → client:22 |
| VM names | `client-k8s{id}`, `master{i}-k8s{id}`, `node{i}-k8s{id}` |

## Templates

| Route | Template |
|---|---|
| `/` | `index.html` |
| `/k8s` | `k8s.html` (cluster list + create form + progress bar) |
| `/k8s/logs/<task_id>` | `k8s_logs.html` |
| `/pve` | `pve.html` (multi-server config) |
| `/openwrt` | `openwrt.html` |
| `/db` | `db_config.html` (SQLite ↔ PostgreSQL) |
| `/users` | `users.html` |
| `/classes` | `classes.html` |
