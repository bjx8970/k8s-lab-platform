# K8s Lab Platform — Agent Guide

## Quick start

```bash
venv\Scripts\activate
pip install -r requirements.txt
# simple-websocket is NOT in requirements.txt — install if WebSocket transport needed:
#   venv\Scripts\activate; pip install simple-websocket
python app.py            # Flask dev at :5000 (with flask-socketio threading mode)
# Production: set FLASK_SECRET_KEY=...; python run.py  (generates .secret_key on first run)
```

## Architecture

- `app.py` — Flask entrypoint, all ~2774 lines of routes (no blueprints). `FLASK_SECRET_KEY` env var (default: `dev-secret-key-change-in-production`).
- `modules/db.py` — SQLAlchemy models + SQLite by default. Schema migration via raw `ALTER TABLE` in `init_db()`. Loads `.db_config.json` at import.
- `modules/pve_client.py` — PVE API via `proxmoxer` (API token auth, not password).
- `modules/openwrt_client.py` — OpenWrt SSH/UCI via `paramiko`. All UCI ops commit immediately.
- `modules/k8s_manager.py` — cluster orchestration, async via `modules/task_queue.py` + in-memory `_task_store` (lost on restart, 30 min expiry).
- `modules/ssh_terminal.py` — WebSSH session pool. SSHSession per (user_id, cluster_name). Supports takeover/view modes, log buffer, configurable limits.
- `modules/status_cache.py` — background VM status cache, 300s refresh interval. Call `update_vm_status()` after VM start/stop.

## Key gotchas

- **No tests, CI, linter, typechecker, or formatter** configured.
- **Invoke in requirements.txt is unused** (`invoke_shell` is paramiko's method, not the `invoke` package).
- **No `simple-websocket` or `gunicorn`/`waitress` in requirements.txt** — add manually if deploying with WebSockets or waitress.
- **API routes** use `@csrf.exempt`. CSRFProtect active on non-API routes.
- **`.db_config.json` contains live credentials** (gitignored). Same for `.secret_key`.
- **Language**: UI/errors/logs in Chinese (except SSH keys).
- **CSV encoding**: `_decode_csv()` tries `utf-8-sig` → `gbk` → `gb2312`. Template downloads use `gbk`.
- **SSH keys**: Ed25519 via `cryptography`, stored per-cluster in DB.
- **Startup**: auto-migrates from legacy `.k8s_clusters.json`, `.pve_config.json`, `.openwrt_config.json` if present.
- **VM shutdown**: uses voting mechanism among students (in-memory, lost on restart).
- **User roles**: admin > teacher > student. Teachers own their students/clusters/classes. Students see `student.html` at `/`.

## Setup flow

`/db-config` (Postgres or skip for SQLite) → `/setup` (create admin) → `/pve` → `/openwrt` → `/k8s`.

## Database

- SQLite default (`k8s_lab.db`, auto-created, gitignored). PostgreSQL: set `{"type": "postgresql", ...}` in `.db_config.json`.
- Cluster ID allocation uses `pg_advisory_xact_lock(42)` on Postgres. Migration from SQLite via UI at `/db`.

## Async tasks & queues

Three queues via `modules/task_queue.py`: `create` (1 worker), `delete` (1 worker), `deploy` (2 workers). Return `task_id`; poll via `GET /api/k8s/tasks/<task_id>`. OpenWrt ops serialized per-host via `_openwrt_lock()` (30s timeout).

## Cluster naming & network rules

| Property | Pattern |
|---|---|
| Cluster name | `k8s_{db_auto_increment_id}` |
| VLAN ID | `100 + id` |
| VLAN device | `eth1.{vlan_id}` |
| Subnet | `10.100.{id}.0/24`, gateway `.1` |
| SSH port forward | WAN:`50000+id` → client:22 |
| VM names | `client-k8s{id}`, `master{i}-k8s{id}`, `node{i}-k8s{id}` |
| IPs | client `.101`, masters `.111+`, nodes `.121+` |

## Cluster creation flow

1. Allocate ID → generate Ed25519 keypair
2. OpenWrt: VLAN device → interface → DHCP pool → firewall zone → network restart
3. PVE: linked clone VMs from template (client + masters + nodes), cloud-init + random MACs + VLAN tag
4. OpenWrt: static DHCP binds + dnsmasq reload + port forward WAN:50000+N → client:22
5. Start VMs → reboot → wait for client SSH → upload SSH keys to client

## K8s deployment

`POST /api/k8s/clusters/<name>/deploy` (requires `"status": "running"`). Inside client VM via kubeasz: download offline pkgs from `http://10.11.43.82/download/`, extract, `ezdown -D` + `ezdown -S`, `ezctl new k8s_{id}` + `ezctl setup k8s_{id} all`.

## Templates

| Route | Template |
|---|---|
| `/` | `index.html` (student → `student.html`) |
| `/k8s` | `k8s.html` |
| `/k8s/logs/<task_id>` | `k8s_logs.html` |
| `/pve` | `pve.html` |
| `/openwrt` | `openwrt.html` |
| `/db` | `db_config.html` |
| `/users` | `users.html` |
| `/classes` | `classes.html` |
| `/admin/webssh` | `admin_webssh.html` |
