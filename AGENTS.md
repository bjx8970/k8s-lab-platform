# K8s Lab Platform — Agent Guide

## Quick start

```bash
venv\Scripts\activate
pip install -r requirements.txt
python app.py            # Flask dev at :5000 (with WebSSH via simple-websocket)
# Production: set FLASK_SECRET_KEY=...; python run.py
```

## Architecture

- `app.py` — Flask entrypoint, all routes (no blueprints). `FLASK_SECRET_KEY` env var for secret_key (default: `dev-secret-key-change-in-production`).
- `modules/db.py` — SQLAlchemy models + SQLite by default. Schema migration via raw `ALTER TABLE` in `init_db()` (no alembic). Loads `.db_config.json` at import for engine.
- `modules/pve_client.py` — PVE API via `proxmoxer` (API token auth, not password).
- `modules/openwrt_client.py` — OpenWrt SSH/UCI via `paramiko`. All UCI ops commit immediately.
- `modules/k8s_manager.py` — cluster orchestration, async via `modules/task_queue.py` + in-memory `_task_store` (lost on restart, 30 min expiry).
- `modules/pg_client.py` — PostgreSQL connection test via `pg8000` (UI only, not runtime persistence).
- `modules/status_cache.py` — background VM status cache (300s refresh, 60s TTL). Call `update_vm_status()` after VM start/stop.
- `modules/ssh_terminal.py` — WebSSH 会话池管理器。`SSHSession` 持久化 SSH 连接，`SSHManager` 管理会话池。支持接管/查看模式、日志缓冲、断线重连、可配置限额。

## WebSSH

学生界面包含浏览器终端（xterm.js + SocketIO + paramiko），用于 SSH 访问客户端 VM。
- 连接：Flask 服务器 → OpenWrt WAN IP → client VM `10.100.{id}.101:22`（端口转发）
- 认证：SocketIO 会话验证 `current_user`；每次连接执行 `_check_cluster_access()`
- 会话模型：每 (user_id, cluster_name) 最多 1 个 SSHSession。窗口关闭不中断 SSH，保留至超时。
- 接管模式：教师/管理员可接管学生会话，学生收到通知并可请求恢复（10 秒倒计时）。
- 查看模式：教师/管理员可查看会话输出，学生无感知，不可输入。
- 前端：`static/vendor/` 包含 socket.io, xterm.js, xterm-addon-fit（本地，无 CDN）
- 传输层：`simple-websocket`（无 monkey-patching）
- 生产：`python run.py`（Werkzeug threaded 服务器，`allow_unsafe_werkzeug=True`）
- 管理：`/admin/webssh` — 查看/终止会话、配置限额（全局/学生/教师最大连接数、超时时间）

## Initial setup flow

`/db-config` (Postgres or skip for SQLite) → `/setup` (create admin) → `/pve` → `/openwrt` → `/k8s`.

## Auth & routing

- Decorator chain: `@login_required` → `@admin_required` / `@teacher_required` / `@teacher_or_admin_required`.
- API routes use `@csrf.exempt`. CSRFProtect active on non-API routes via flask-wtf.
- Role hierarchy: admin > teacher > student. Teachers own their students/clusters/classes.

## Database

- **SQLite** (`k8s_lab.db`, auto-created, gitignored). **PostgreSQL**: set `{"type": "postgresql", ...}` in `.db_config.json` (file is gitignored, contains live credentials).
- Migration from SQLite via UI at `/db`.
- Cluster ID allocation uses `pg_advisory_xact_lock(42)` on Postgres.

## Async tasks & queues

Three task queues via `modules/task_queue.py`: `create` (1 worker), `delete` (1 worker), `deploy` (2 workers).
All return `task_id`; poll via `GET /api/k8s/tasks/<task_id>`. OpenWrt ops serialized per-host via `_openwrt_lock()` (30s timeout).

## Cluster creation flow

1. Allocate ID → generate Ed25519 keypair
2. OpenWrt: VLAN device → interface → DHCP pool → firewall zone → network restart
3. PVE: linked clone VMs from template (client + masters + nodes), cloud-init + random MACs + VLAN tag
4. OpenWrt: static DHCP binds + dnsmasq reload + port forward WAN:50000+N → client:22
5. Start VMs → reboot → wait for client SSH → upload SSH keys to client

## VM IP allocation

| Role | IP |
|---|---|
| client | `10.100.{id}.101` |
| masters | `10.100.{id}.111+` |
| nodes | `10.100.{id}.121+` |

## K8s deployment

`POST /api/k8s/clusters/<name>/deploy` (requires `"status": "running"`). Inside client VM via kubeasz:
1. Download `ezdown` + `kubeasz_offline.tgz` from `http://10.11.43.82/download/`
2. Extract to `/etc/kubeasz`, `ezdown -D` (Docker) + `ezdown -S` (kubeasz container)
3. Distribute SSH pub key from client to all other VMs
4. `ezctl new k8s_{id}` + `ezctl setup k8s_{id} all` inside Docker container

## Key conventions

- **Language**: UI/errors/logs in Chinese (except SSH keys).
- **No tests, CI, linter, typechecker, or formatter** configured.
- **CSV encoding**: `_decode_csv()` tries `utf-8-sig` → `gbk` → `gb2312`. Template downloads use `gbk`.
- **SSH keys**: Ed25519 via `cryptography`, stored per-cluster in DB.
- **`invoke` in requirements.txt is unused**. `gunicorn` in requirements for production.
- **Student role**: `/` renders `student.html` instead of `index.html`.

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
| `/` | `index.html` (student → `student.html`) |
| `/k8s` | `k8s.html` |
| `/k8s/logs/<task_id>` | `k8s_logs.html` |
| `/pve` | `pve.html` |
| `/openwrt` | `openwrt.html` |
| `/db` | `db_config.html` |
| `/users` | `users.html` |
| `/classes` | `classes.html` |
