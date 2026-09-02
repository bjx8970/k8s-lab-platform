import os
import time
import threading
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, render_template, request, jsonify, redirect, g, make_response, session
from flask_login import LoginManager, login_user, logout_user, login_required as flask_login_required, current_user
from flask_wtf.csrf import CSRFError, CSRFProtect, generate_csrf
from werkzeug.security import check_password_hash, generate_password_hash

from modules.db import (
    add_group_member, check_user_in_class_group, count_classes, count_groups, count_users,
    create_class, create_group, create_pve_server, create_user, delete_class, delete_group,
    delete_pve_server, delete_user, find_cluster_by_vm, get_class, get_class_by_name, get_classes_for_student,
    get_config, get_db_config, get_db_status, get_group, get_group_member, get_pve_server,
    get_student_group_ids, get_students_created_by, get_user, get_user_by_username,
    get_user_cluster_ids, get_user_groups, init_db, is_db_configured, list_classes,
    list_cluster_names_by_class_id, list_cluster_names_by_group_id,
    detach_clusters_from_group, detach_clusters_from_class,
    list_group_members, list_group_members_batch, list_groups, list_groups_batch, list_pve_servers, list_users,
    get_or_create_group, migrate_config_from_json,
    migrate_from_json, reload_db_engine, remove_group_member, save_cluster,
    set_config, set_db_config, update_class, update_group, update_pve_server, update_user,
)
from modules.pve_client import PVEClient, PVEError
from modules.openwrt_client import OpenWrtClient, OpenWrtError
from modules.k8s_manager import create_cluster, create_cluster_async, deploy_k8s_async, delete_cluster_async, batch_create_clusters_async, list_clusters, get_cluster, delete_cluster, get_task_status, list_tasks, cancel_task, K8sError, force_delete_cluster, set_on_task_update
from modules.pg_client import PGClient, PGError
from modules.status_cache import get_vm_status as get_cached_vm_status, start_monitor as start_status_monitor, update_vm_status
from modules.authz import Actions, AuthorizationDenied, is_allowed
from modules.audit import sanitize, security_audit, safe_error_message as _safe_error_message, sanitize_text as _sanitize_text
from modules.security_service import validate_cluster_creation as _validate_cluster_creation, authorize_cluster_action as _authorize_cluster_action

from flask_socketio import SocketIO, emit, join_room, leave_room
from modules.ssh_terminal import SSHManager, SSHConnectionError, SessionExistsError, TooManyConnectionsError

# ── 用户在线追踪 (内存) ──
_online_users = {}  # user_id → {"sids": set(), "online_since": timestamp}
_webssh_sid_users = {}  # WebSSH sid → immutable user id for its lifetime
_state_sid_users = {}  # state namespace sid → immutable user id for its lifetime


def _state_user_snapshot():
    """Return live state-socket users and prune disabled/deleted connections."""
    result = []
    connections = [(sid, uid) for uid, info in list(_online_users.items())
                   for sid in tuple(info["sids"])]
    for sid, user_id in connections:
        user = _fresh_user(user_id)
        if user is None:
            _state_sid_users.pop(sid, None)
            info = _online_users.get(user_id)
            if info:
                info["sids"].discard(sid)
                if not info["sids"]:
                    _online_users.pop(user_id, None)
            continue
        result.append((sid, user))
    return result


def _emit_state(event, data, predicate):
    for sid, user in _state_user_snapshot():
        try:
            allowed = bool(predicate(user))
        except Exception:
            allowed = False
        if allowed:
            socketio.emit(event, data, to=sid, namespace="/state")

def _add_online_user(user_id, sid):
    if user_id not in _online_users:
        _online_users[user_id] = {"sids": set(), "online_since": time.time()}
        _emit_state(
            "user_online", {"user_id": user_id},
            lambda user: user.role in {"admin", "teacher"},
        )
    _online_users[user_id]["sids"].add(sid)

def _remove_online_user(sid):
    for uid, info in list(_online_users.items()):
        info["sids"].discard(sid)
        if not info["sids"]:
            del _online_users[uid]
            _emit_state(
                "user_offline", {"user_id": uid},
                lambda user: user.role in {"admin", "teacher"},
            )

def _update_online_user(sid):
    for uid, info in _online_users.items():
        if sid in info["sids"]:
            info["online_since"] = time.time()

def is_user_online(user_id):
    return user_id in _online_users

# ── VM 关机投票 (内存) ──
_vm_shutdown_votes = {}
_vm_shutdown_pending_vms = {}
_vm_shutdown_lock = threading.RLock()
_vm_shutdown_vote_seq = 0

def _gen_vote_id():
    global _vm_shutdown_vote_seq
    _vm_shutdown_vote_seq += 1
    return f"sd_{int(time.time())}_{_vm_shutdown_vote_seq}"

def _get_online_students_in_group(group_id):
    members = list_group_members(group_id)
    return [m for m in members if is_user_online(m["id"])]

def _do_shutdown_vm(node, vmid, pve_server_id):
    client = get_pve_client(pve_server_id)
    result = client.stop_vm(node, vmid)
    update_vm_status(node, vmid, "stopped")
    return result

def _get_vm_name(node, vmid):
    cluster = find_cluster_by_vm(node, vmid)
    if cluster:
        for vm_name, vm_info in (cluster.get("vms") or {}).items():
            if vm_info.get("node") == node and vm_info.get("vmid") == vmid:
                return vm_name
    return f"VM {vmid}"

def _do_shutdown_cluster_vms(cluster_name, pve_server_id, cluster=None):
    cluster = cluster if cluster is not None else get_cluster(cluster_name)
    if not cluster:
        raise K8sError(f"集群 {cluster_name} 不存在")
    client = get_pve_client(pve_server_id)
    ok = 0
    for vm_name, vm_info in (cluster.get("vms") or {}).items():
        client.stop_vm(vm_info["node"], vm_info["vmid"])
        update_vm_status(vm_info["node"], vm_info["vmid"], "stopped")
        ok += 1
    return ok

def _cleanup_stale_votes():
    now = time.time()
    with _vm_shutdown_lock:
        stale = [vid for vid, v in list(_vm_shutdown_votes.items())
                 if v["completed"] and now - v["started_at"] > 300]
        for vid in stale:
            v = _vm_shutdown_votes.pop(vid, None)
            if v:
                key = (
                    ("cluster", v.get("pve_server_id"), v.get("cluster_name"))
                    if v["type"] == "cluster"
                    else ("vm", v.get("pve_server_id"), v.get("node"), v.get("vmid"))
                )
                _vm_shutdown_pending_vms.pop(key, None)

def _load_allowed_origins():
    origins = []
    for value in os.getenv("K8S_LAB_ALLOWED_ORIGINS", "").split(","):
        origin = value.strip().rstrip("/")
        if not origin:
            continue
        parsed = urlsplit(origin)
        if ("*" in origin or parsed.scheme not in {"http", "https"}
                or not parsed.netloc or parsed.username is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise RuntimeError("K8S_LAB_ALLOWED_ORIGINS 必须使用完整且无通配符的 HTTP/HTTPS Origin")
        origins.append(origin)
    return origins


_allowed_origins = _load_allowed_origins()


def _socketio_origin_allowed(origin, environ=None):
    """Apply the same exact-origin policy to Engine.IO and HTTP requests."""
    if not origin:
        return True
    environ = environ or {}
    scheme = environ.get("wsgi.url_scheme", "")
    host = environ.get("HTTP_HOST", "")
    same_origin = f"{scheme}://{host}".rstrip("/") if scheme and host else ""
    normalized = origin.strip().rstrip("/")
    return bool(normalized and (normalized == same_origin or normalized in _allowed_origins))


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")
app.config["PERMANENT_SESSION_LIFETIME"] = 3600 * 8  # 8 小时
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
if os.getenv("K8S_LAB_ENV", "").strip().lower() == "production":
    app.config["SESSION_COOKIE_SECURE"] = True
else:
    app.config["SESSION_COOKIE_SECURE"] = (
        os.getenv("SESSION_COOKIE_SECURE", "false").strip().lower() == "true"
    )

login_manager = LoginManager(app)
login_manager.login_view = "login_page"
login_manager.login_message = None

csrf = CSRFProtect()

socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins=_socketio_origin_allowed,
    manage_session=False,
)
ssh_manager = SSHManager()
ssh_manager.init_app(socketio)


@login_manager.user_loader
def _load_user(user_id):
    from modules.db import load_user
    return load_user(user_id)


@login_manager.unauthorized_handler
def _unauthorized():
    if request.path.startswith("/api/"):
        return jsonify({"error": "未登录，请先登录"}), 401
    return redirect("/login")

_base_dir = os.path.dirname(os.path.abspath(__file__))
if is_db_configured():
    init_db()
    migrate_from_json(os.path.join(_base_dir, ".k8s_clusters.json"))
    migrate_config_from_json("pve", os.path.join(_base_dir, ".pve_config.json"))
    migrate_config_from_json("openwrt", os.path.join(_base_dir, ".openwrt_config.json"))

start_status_monitor()


@app.context_processor
def inject_globals():
    return dict(get_config=get_config)


# ── Auth helpers ──

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录，请先登录"}), 401
            return redirect("/login")
        if not current_user.is_active:
            logout_user()
            if request.path.startswith("/api/"):
                return jsonify({"error": "用户已被禁用"}), 403
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if current_user.role not in roles:
                if request.path.startswith("/api/"):
                    return jsonify({"error": "权限不足"}), 403
                return render_template("403.html"), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator


def admin_required(f):
    return role_required("admin")(f)


def teacher_required(f):
    return role_required("teacher")(f)


def teacher_or_admin_required(f):
    return role_required("admin", "teacher")(f)


def _decode_csv(content):
    for enc in ("utf-8-sig", "gbk", "gb2312"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


# ── Setup / Login / Logout ──

_setup_complete = False

def _needs_setup():
    global _setup_complete
    if _setup_complete:
        return False
    try:
        needs = count_users() == 0
        if not needs:
            _setup_complete = True
        return needs
    except Exception:
        return True


def _request_origin_is_allowed(origin):
    return _socketio_origin_allowed(origin, request.environ)


@app.before_request
def before_request():
    origin = request.headers.get("Origin")
    if origin and not _request_origin_is_allowed(origin):
        return jsonify({"error": "不允许的请求来源"}), 403
    if request.path.startswith("/static"):
        return
    db_setup_paths = ("/db-config", "/api/db-config", "/api/csrf-token")
    if not is_db_configured() and request.path not in db_setup_paths:
        return redirect("/db-config")
    if request.path in db_setup_paths:
        return
    if _needs_setup() and request.path not in ("/setup", "/api/setup", "/api/csrf-token"):
        return redirect("/setup")

    if current_user.is_authenticated:
        session.permanent = True


csrf.init_app(app)


@app.errorhandler(CSRFError)
def handle_csrf_error(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "CSRF 校验失败"}), 400
    return make_response("CSRF 校验失败", 400)


@app.after_request
def apply_cors_and_security_audit(response):
    origin = request.headers.get("Origin")
    if origin and _request_origin_is_allowed(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.vary.add("Origin")
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRFToken"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"

    actor = current_user if current_user.is_authenticated else None
    metadata = {
        "method": request.method,
        "path": request.path,
        "status": response.status_code,
    }
    if response.status_code in (401, 403):
        security_audit(
            "http.authorization",
            "denied",
            actor=actor,
            resource_type="http",
            resource_id=request.path,
            metadata=metadata,
        )
    elif request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400:
        security_audit(
            "http.write",
            "success",
            actor=actor,
            resource_type="http",
            resource_id=request.path,
            metadata=metadata,
        )
    return response


@app.route("/api/csrf-token", methods=["GET"])
def api_csrf_token():
    return jsonify({"csrf_token": generate_csrf()})


@app.route("/setup")
def setup_page():
    if not _needs_setup():
        return redirect("/")
    return render_template("setup.html")


@app.route("/api/setup", methods=["POST"])
def api_setup():
    if not _needs_setup():
        return jsonify({"error": "已初始化"}), 400
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if len(password) < 6:
        return jsonify({"error": "密码长度至少 6 位"}), 400
    existing = get_user_by_username(username)
    if existing:
        return jsonify({"error": "用户名已存在"}), 400
    uid = create_user({
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": "admin",
    })
    user = get_user_by_username(username)
    if user:
        login_user(user)
        session.permanent = True
    return jsonify({"message": "初始化完成", "user_id": uid})


@app.route("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect("/")
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    user = get_user_by_username(username)
    if not user:
        return jsonify({"error": "用户名或密码错误"}), 401
    if not user.is_active:
        return jsonify({"error": "账号已被禁用"}), 403
    if not check_password_hash(user.password_hash, password):
        return jsonify({"error": "用户名或密码错误"}), 401
    if user.role == "student":
        groups = get_user_groups(user.id)
        if not groups:
            return jsonify({"error": "您尚未被分配到课程组，请联系教师"}), 403
    login_user(user)
    session.permanent = True
    return jsonify({"message": "登录成功", "user": {
        "id": user.id, "username": user.username, "role": user.role,
    }})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    logout_user()
    return jsonify({"message": "已退出登录"})


# ── Database config (setup wizard) ──

@app.route("/db-config")
def db_config_wizard():
    if is_db_configured():
        return redirect("/")
    return render_template("db_setup.html")


@app.route("/api/db-config", methods=["POST"])
def api_db_config():
    if is_db_configured():
        return jsonify({"error": "数据库已配置"}), 400
    data = request.get_json() or {}
    missing = [k for k in ("host", "user", "password", "database") if not data.get(k)]
    if missing:
        return jsonify({"error": f"请填写必填项: {', '.join(missing)}"}), 400

    from modules.pg_client import PGClient, PGError
    client = PGClient(
        host=data["host"], port=int(data.get("port", 5432)),
        user=data["user"], password=data["password"],
        database=data["database"],
    )
    try:
        version = client.connect()
        client.close()
    except PGError as e:
        return jsonify({"error": _safe_error_message(e, "数据库连接失败")}), 400
    except Exception as e:
        return jsonify({"error": _safe_error_message(e, "数据库连接失败")}), 400

    cfg = {
        "type": "postgresql",
        "host": data["host"],
        "port": int(data.get("port", 5432)),
        "user": data["user"],
        "password": data["password"],
        "database": data["database"],
    }
    set_db_config(cfg)
    reload_db_engine()
    init_db()
    return jsonify({"message": "数据库配置已保存并初始化完成"})


# ── User API ──

@app.route("/api/users/me", methods=["GET"])
@login_required
def api_users_me():
    result = {"id": current_user.id, "username": current_user.username, "name": current_user.name or "", "role": current_user.role}
    if current_user.role == "student":
        result["classes"] = get_classes_for_student(current_user.id)
    elif current_user.role == "teacher":
        result["classes"] = list_classes(created_by=current_user.id)
    return jsonify(result)


@app.route("/api/users/me/password", methods=["POST"])
@login_required
def api_change_my_password():
    data = request.get_json(force=True) or {}
    old_pw = data.get("old_password", "")
    new_pw = data.get("new_password", "")
    if not old_pw or not new_pw:
        return jsonify({"error": "密码不能为空"}), 400
    if len(new_pw) < 6:
        return jsonify({"error": "新密码至少 6 位"}), 400
    if not check_password_hash(current_user.password_hash, old_pw):
        return jsonify({"error": "原密码错误"}), 403
    update_user(current_user.id, {"password_hash": generate_password_hash(new_pw)})
    return jsonify({"message": "密码修改成功"})


@app.route("/api/users", methods=["GET"])
@login_required
def api_list_users():
    role_filter = request.args.get("role")
    if current_user.role == "admin":
        users = list_users(role=role_filter)
    elif current_user.role == "teacher":
        users = list_users(role="student")
    else:
        return jsonify({"error": "权限不足"}), 403
    for u in users:
        u["online"] = is_user_online(u["id"])
    return jsonify(users)


@app.route("/api/users", methods=["POST"])
@login_required
def api_create_user():
    data = request.get_json() or {}
    username = data.get("username", "").strip()
    password = data.get("password", "").strip()
    role = data.get("role", "student")
    name = data.get("name", "").strip()
    if not username or not password:
        return jsonify({"error": "用户名和密码不能为空"}), 400
    if role not in ("admin", "teacher", "student"):
        return jsonify({"error": "无效的角色"}), 400
    if role == "admin":
        return jsonify({"error": "无法创建管理员"}), 403
    if current_user.role == "teacher" and role != "student":
        return jsonify({"error": "教师只能创建学生用户"}), 403
    if current_user.role != "admin" and role in ("admin", "teacher"):
        return jsonify({"error": "权限不足"}), 403
    existing = get_user_by_username(username)
    if existing:
        return jsonify({"error": "用户名已存在"}), 400
    uid = create_user({
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": role,
        "name": name,
        "created_by": current_user.id,
    })
    return jsonify({"id": uid, "message": "用户创建成功"}), 201


@app.route("/api/users/template", methods=["GET"])
@login_required
@teacher_or_admin_required
def api_users_template():
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    if current_user.role == "admin":
        writer.writerow(["用户名", "密码", "姓名", "角色"])
        writer.writerow(["zhangsan", "123456", "张三", "student"])
    else:
        writer.writerow(["用户名", "密码", "姓名"])
        writer.writerow(["lisi", "123456", "李四"])
    resp = make_response(output.getvalue().encode("gbk"))
    resp.headers["Content-Type"] = "text/csv; charset=gbk"
    resp.headers["Content-Disposition"] = "attachment; filename=user_import_template.csv"
    return resp


@app.route("/api/users/import", methods=["POST"])
@login_required
@teacher_or_admin_required
def api_users_import():
    import csv, io
    if "file" not in request.files:
        return jsonify({"error": "请上传 CSV 文件"}), 400
    file = request.files["file"]
    if not file.filename or not file.filename.endswith(".csv"):
        return jsonify({"error": "仅支持 .csv 文件"}), 400
    content = _decode_csv(file.read())
    reader = csv.DictReader(io.StringIO(content))
    required_cols_teacher = {"用户名", "密码", "姓名"}
    required_cols_admin = {"用户名", "密码", "姓名", "角色"}
    if current_user.role == "admin":
        if not reader.fieldnames or not required_cols_admin.issubset(reader.fieldnames):
            return jsonify({"error": "CSV 格式错误，需要列: 用户名, 密码, 姓名, 角色"}), 400
    else:
        if not reader.fieldnames or not required_cols_teacher.issubset(reader.fieldnames):
            return jsonify({"error": "CSV 格式错误，需要列: 用户名, 密码, 姓名"}), 400

    result = {"created": 0, "skipped": 0, "errors": []}
    for row_num, row in enumerate(reader, start=2):
        username = (row.get("用户名") or "").strip()
        password = (row.get("密码") or "").strip()
        name = (row.get("姓名") or "").strip()
        role = (row.get("角色") or "student").strip()

        if not username or not password:
            result["errors"].append(f"第 {row_num} 行: 用户名和密码不能为空")
            continue
        if current_user.role == "teacher":
            role = "student"
        elif role not in ("student", "teacher"):
            result["errors"].append(f"第 {row_num} 行: 无效的角色 '{role}'")
            continue
        elif role == "admin":
            result["errors"].append(f"第 {row_num} 行: 无法创建管理员")
            continue
        existing = get_user_by_username(username)
        if existing:
            result["skipped"] += 1
            continue
        try:
            create_user({
                "username": username,
                "password_hash": generate_password_hash(password),
                "role": role,
                "name": name,
                "created_by": current_user.id,
            })
            result["created"] += 1
        except Exception as e:
            result["errors"].append(f"第 {row_num} 行: {_safe_error_message(e, '处理失败')}")
    return jsonify(result)


@app.route("/api/users/<int:uid>", methods=["PUT"])
@login_required
def api_update_user(uid):
    data = request.get_json() or {}
    target = get_user(uid)
    if not target:
        return jsonify({"error": "用户不存在"}), 404
    if current_user.role == "teacher":
        if target.created_by != current_user.id:
            return jsonify({"error": "只能编辑自己创建的学生"}), 403
    update_data = {}
    if "username" in data:
        update_data["username"] = data["username"].strip()
    if "password" in data and data["password"].strip():
        update_data["password_hash"] = generate_password_hash(data["password"].strip())
    if "is_active" in data:
        update_data["is_active"] = data["is_active"]
    if "name" in data:
        update_data["name"] = data["name"].strip()
    if update_data:
        update_user(uid, update_data)
    return jsonify({"message": "用户已更新"})


@app.route("/api/users/<int:uid>", methods=["DELETE"])
@login_required
def api_delete_user(uid):
    target = get_user(uid)
    if not target:
        return jsonify({"error": "用户不存在"}), 404
    if current_user.role == "teacher":
        if target.created_by != current_user.id:
            return jsonify({"error": "只能删除自己创建的学生"}), 403
    delete_user(uid)
    return jsonify({"message": "用户已删除"})


# ── Class API ──

@app.route("/api/classes", methods=["GET"])
@login_required
def api_list_classes():
    if current_user.role == "admin":
        classes = list_classes()
    elif current_user.role == "teacher":
        classes = list_classes(created_by=current_user.id)
    else:
        classes = get_classes_for_student(current_user.id)
    return jsonify(classes)


@app.route("/api/classes", methods=["POST"])
@login_required
@teacher_or_admin_required
def api_create_class():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "课程名称不能为空"}), 400
    cid = create_class({
        "name": name,
        "description": data.get("description", ""),
        "created_by": current_user.id,
    })
    return jsonify({"id": cid, "message": "课程创建成功"}), 201


@app.route("/api/classes/<int:cid>", methods=["GET"])
@login_required
def api_get_class(cid):
    cls = get_class(cid)
    if not cls:
        return jsonify({"error": "课程不存在"}), 404
    if current_user.role == "teacher" and cls.get("created_by") != current_user.id:
        return jsonify({"error": "权限不足"}), 403
    if current_user.role == "student":
        ok = check_user_in_class_group(current_user.id, cid)
        if not ok:
            return jsonify({"error": "权限不足"}), 403
    return jsonify(cls)


@app.route("/api/classes/<int:cid>", methods=["PUT"])
@login_required
@teacher_or_admin_required
def api_update_class(cid):
    cls = get_class(cid)
    if not cls:
        return jsonify({"error": "课程不存在"}), 404
    if current_user.role == "teacher" and cls.get("created_by") != current_user.id:
        return jsonify({"error": "只能编辑自己创建的课程"}), 403
    data = request.get_json() or {}
    update_data = {}
    if "name" in data:
        update_data["name"] = data["name"].strip()
    if "description" in data:
        update_data["description"] = data["description"]
    if update_data:
        update_class(cid, update_data)
    return jsonify({"message": "课程已更新"})


@app.route("/api/classes/<int:cid>", methods=["DELETE"])
@login_required
@teacher_or_admin_required
def api_delete_class(cid):
    cls = get_class(cid)
    if not cls:
        return jsonify({"error": "课程不存在"}), 404
    if current_user.role == "teacher" and cls.get("created_by") != current_user.id:
        return jsonify({"error": "只能删除自己创建的课程"}), 403
    cluster_names = list_cluster_names_by_class_id(cid)
    detach_clusters_from_class(cid)
    task_ids = []
    for name in cluster_names:
        tid = delete_cluster_async(name, created_by=current_user.id)
        task_ids.append(tid)
    delete_class(cid)
    return jsonify({
        "message": f"课程已删除，已提交 {len(cluster_names)} 个集群释放任务",
        "task_ids": task_ids,
    })


@app.route("/api/classes/template", methods=["GET"])
@login_required
@teacher_or_admin_required
def api_classes_template():
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["课程名称", "组名称", "用户名"])
    writer.writerow(["示例课程", "示例组", "student_username"])
    resp = make_response(output.getvalue().encode("gbk"))
    resp.headers["Content-Type"] = "text/csv; charset=gbk"
    resp.headers["Content-Disposition"] = "attachment; filename=class_import_template.csv"
    return resp


@app.route("/api/classes/import", methods=["POST"])
@login_required
@teacher_or_admin_required
def api_classes_import():
    import csv, io
    if "file" not in request.files:
        return jsonify({"error": "请上传 CSV 文件"}), 400
    file = request.files["file"]
    if not file.filename or not file.filename.endswith(".csv"):
        return jsonify({"error": "仅支持 .csv 文件"}), 400
    content = _decode_csv(file.read())
    reader = csv.DictReader(io.StringIO(content))
    required_cols = {"课程名称", "组名称", "用户名"}
    if not reader.fieldnames or not required_cols.issubset(reader.fieldnames):
        return jsonify({"error": "CSV 格式错误，需要列: 课程名称, 组名称, 用户名"}), 400

    result = {"created": 0, "skipped": 0, "errors": []}
    for row_num, row in enumerate(reader, start=2):
        class_name = (row.get("课程名称") or "").strip()
        group_name = (row.get("组名称") or "").strip()
        username = (row.get("用户名") or "").strip()
        if not class_name or not group_name or not username:
            result["errors"].append(f"第 {row_num} 行: 课程名称、组名称、用户名不能为空")
            continue
        created_by_filter = current_user.id if current_user.role == "teacher" else None
        class_obj = get_class_by_name(class_name, created_by=created_by_filter)
        if not class_obj:
            result["errors"].append(f"第 {row_num} 行: 课程 '{class_name}' 不存在")
            continue
        user_obj = get_user_by_username(username)
        if not user_obj:
            result["errors"].append(f"第 {row_num} 行: 用户 '{username}' 不存在")
            continue
        try:
            group_id = get_or_create_group(class_obj["id"], group_name, current_user.id)
            add_group_member(group_id, user_obj.id)
            result["created"] += 1
        except ValueError as e:
            result["skipped"] += 1
        except Exception as e:
            result["errors"].append(f"第 {row_num} 行: {_safe_error_message(e, '处理失败')}")
    return jsonify(result)


# ── Group API ──

@app.route("/api/classes/groups", methods=["GET"])
@login_required
def api_list_groups_batch():
    class_ids_str = request.args.get("class_ids", "")
    if not class_ids_str:
        return jsonify({})
    try:
        class_ids = [int(x) for x in class_ids_str.split(",")]
    except ValueError:
        return jsonify({"error": "无效的 class_ids 参数"}), 400

    if current_user.role == "student":
        groups = get_user_groups(current_user.id)
        result = {}
        for grp in groups:
            cid = str(grp["class_id"])
            result.setdefault(cid, []).append({
                "id": grp["group_id"],
                "name": grp["group_name"],
                "class_id": grp["class_id"],
            })
        return jsonify(result)

    if current_user.role == "teacher":
        teacher_classes = list_classes(created_by=current_user.id)
        allowed_ids = [c["id"] for c in teacher_classes]
        class_ids = [cid for cid in class_ids if cid in allowed_ids]

    if not class_ids:
        return jsonify({})

    groups_by_class = list_groups_batch(class_ids)
    all_group_ids = [g["id"] for groups in groups_by_class.values() for g in groups]
    members_by_group = list_group_members_batch(all_group_ids) if all_group_ids else {}

    result = {}
    for cid, groups in groups_by_class.items():
        enriched = []
        for grp in groups:
            grp["members"] = members_by_group.get(grp["id"], [])
            grp["member_count"] = len(grp["members"])
            enriched.append(grp)
        result[str(cid)] = enriched
    return jsonify(result)


@app.route("/api/classes/<int:cid>/groups", methods=["GET"])
@login_required
def api_list_groups(cid):
    cls = get_class(cid)
    if not cls:
        return jsonify({"error": "课程不存在"}), 404
    if current_user.role == "teacher" and cls.get("created_by") != current_user.id:
        return jsonify({"error": "权限不足"}), 403
    if current_user.role == "student":
        groups = get_user_groups(current_user.id)
        groups_in_class = [g for g in groups if g["class_id"] == cid]
        return jsonify(groups_in_class)
    groups = list_groups(class_id=cid)
    if not groups:
        return jsonify([])
    group_ids = [g["id"] for g in groups]
    members_by_group = list_group_members_batch(group_ids)
    result = []
    for grp in groups:
        grp["members"] = members_by_group.get(grp["id"], [])
        grp["member_count"] = len(grp["members"])
        result.append(grp)
    return jsonify(result)


@app.route("/api/groups", methods=["POST"])
@login_required
@teacher_or_admin_required
def api_create_group():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    class_id = data.get("class_id")
    if not name or not class_id:
        return jsonify({"error": "组名和课程ID不能为空"}), 400
    cls = get_class(class_id)
    if not cls:
        return jsonify({"error": "课程不存在"}), 404
    if current_user.role == "teacher" and cls.get("created_by") != current_user.id:
        return jsonify({"error": "只能在自己创建的课程中创建组"}), 403
    gid = create_group({
        "name": name,
        "class_id": class_id,
        "max_students": data.get("max_students", 0),
        "created_by": current_user.id,
    })
    return jsonify({"id": gid, "message": "组创建成功"}), 201


@app.route("/api/groups/<int:gid>", methods=["PUT"])
@login_required
@teacher_or_admin_required
def api_update_group(gid):
    grp = get_group(gid)
    if not grp:
        return jsonify({"error": "组不存在"}), 404
    if current_user.role == "teacher" and grp.get("created_by") != current_user.id:
        return jsonify({"error": "只能修改自己创建的组"}), 403
    data = request.get_json() or {}
    update_data = {}
    if "name" in data:
        name = data["name"].strip()
        if name:
            update_data["name"] = name
    if "max_students" in data:
        update_data["max_students"] = int(data["max_students"])
    if not update_data:
        return jsonify({"error": "没有需要修改的字段"}), 400
    update_group(gid, update_data)
    return jsonify({"message": "组已更新"}), 200


@app.route("/api/groups/<int:gid>", methods=["DELETE"])
@login_required
@teacher_or_admin_required
def api_delete_group(gid):
    grp = get_group(gid)
    if not grp:
        return jsonify({"error": "组不存在"}), 404
    if current_user.role == "teacher" and grp.get("created_by") != current_user.id:
        return jsonify({"error": "只能删除自己创建的组"}), 403
    cluster_names = list_cluster_names_by_group_id(gid)
    detach_clusters_from_group(gid)
    task_ids = []
    for name in cluster_names:
        tid = delete_cluster_async(name, created_by=current_user.id)
        task_ids.append(tid)
    delete_group(gid)
    return jsonify({
        "message": f"组已删除，已提交 {len(cluster_names)} 个集群释放任务",
        "task_ids": task_ids,
    })


# ── Group Member API ──

@app.route("/api/groups/<int:gid>/members", methods=["GET"])
@login_required
def api_list_group_members(gid):
    members = list_group_members(gid)
    return jsonify(members)


@app.route("/api/groups/<int:gid>/members", methods=["POST"])
@login_required
@teacher_or_admin_required
def api_add_group_member(gid):
    data = request.get_json() or {}
    user_id = data.get("user_id")
    if not user_id:
        return jsonify({"error": "用户ID不能为空"}), 400
    target = get_user(user_id)
    if not target:
        return jsonify({"error": "用户不存在"}), 404
    if target.role != "student":
        return jsonify({"error": "只能将学生加入组"}), 400
    try:
        add_group_member(gid, user_id)
        return jsonify({"message": "已加入组"}), 201
    except ValueError as e:
        return jsonify({"error": _safe_error_message(e, "操作失败")}), 400


@app.route("/api/groups/<int:gid>/members/<int:uid>", methods=["DELETE"])
@login_required
@teacher_or_admin_required
def api_remove_group_member(gid, uid):
    remove_group_member(gid, uid)
    return jsonify({"message": "已从组中移除"})


def get_pve_client(server_id=None):
    if server_id:
        cfg = get_pve_server(server_id)
        if not cfg:
            raise PVEError(f"PVE 服务器 (ID={server_id}) 不存在")
    else:
        cfg = get_config("pve")
        if not cfg:
            raise PVEError(f"PVE 未配置，请先在页面中保存配置")
    missing = [k for k in ("host", "user", "token_name", "token_value") if not cfg.get(k)]
    if missing:
        raise PVEError(f"PVE 配置不完整: {', '.join(missing)}")
    return PVEClient(
        host=cfg["host"],
        user=cfg["user"],
        token_name=cfg["token_name"],
        token_value=cfg["token_value"],
        verify_ssl=cfg.get("verify_ssl", False),
        port=int(cfg.get("port", 8006)),
    )


def _student_groups_for(user):
    if getattr(user, "role", None) != "student":
        return set()
    return set(get_student_group_ids(user.id))


def _fresh_user(user_id):
    if user_id is None:
        return None
    try:
        user = get_user(user_id)
    except Exception:
        return None
    if not user or not getattr(user, "is_active", False):
        return None
    return user


def _is_allowed_for_user(user, action, resource=None):
    return bool(user and is_allowed(
        user,
        action,
        resource,
        student_group_ids=_student_groups_for(user),
    ))


def _is_allowed(action, resource=None):
    return is_allowed(
        current_user,
        action,
        resource,
        student_group_ids=_student_groups_for(current_user),
    )


def _forbidden(message="权限不足"):
    return jsonify({"error": message}), 403


def _check_cluster_access(name, action=Actions.CLUSTER_READ):
    cluster = get_cluster(name)
    return bool(cluster and _is_allowed(action, cluster))


def _check_vm_access(node, vmid, action=Actions.CLUSTER_READ):
    cluster = find_cluster_by_vm(node, vmid)
    if cluster:
        g._vm_cluster = cluster
    return bool(cluster and _is_allowed(action, cluster))


def _session_authz_resource(session):
    owner = session.get("owner", {}) if isinstance(session, dict) else session.owner
    owner_user_id = owner.get("user_id")
    owner_user = get_user(owner_user_id)
    return {
        "cluster_name": session.cluster_name if not isinstance(session, dict) else session.get("cluster_name"),
        "owner_user_id": owner_user_id,
        "owner_teacher_id": getattr(owner_user, "created_by", None) if owner_user else None,
    }


def _webssh_binding_allowed(sid, session, role):
    """Re-check the live user and resource before every WebSSH operation."""
    user_id = _webssh_sid_users.get(sid)
    user = _fresh_user(user_id)
    if not user or not session or session.status == "terminated":
        return False
    cluster = get_cluster(session.cluster_name)
    if not cluster:
        return False

    owner_id = (session.owner or {}).get("user_id")
    owner = _fresh_user(owner_id)
    if not owner or not _is_allowed_for_user(owner, Actions.WEBSSH_CONNECT, cluster):
        return False
    if role == "owner":
        return user.id == owner_id and _is_allowed_for_user(
            user, Actions.WEBSSH_CONNECT, cluster
        )
    if role in {"takeover", "viewer"}:
        return _is_allowed_for_user(
            user, Actions.WEBSSH_OBSERVE, _session_authz_resource(session)
        )
    return False


def _authorize_webssh_binding(sid, session, role):
    try:
        allowed = _webssh_binding_allowed(sid, session, role)
    except Exception:
        allowed = False
    if not allowed:
        security_audit(
            "webssh.authorization", "denied",
            actor={"id": _webssh_sid_users.get(sid)},
            resource_type="webssh", resource_id=session.session_id if session else None,
            metadata={"binding_role": role},
        )
    return allowed


ssh_manager.set_authorizer(_authorize_webssh_binding)




def api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except AuthorizationDenied:
            return jsonify({"error": "权限不足"}), 403
        except ValueError:
            return jsonify({"error": "请求参数无效"}), 400
        except PVEError as e:
            return jsonify({"error": _safe_error_message(e, "PVE 操作失败")}), 400
        except Exception as e:
            return jsonify({"error": _safe_error_message(e, "操作失败，请稍后重试")}), 500
    return wrapper


@app.route("/")
@login_required
def index():
    if current_user.role == "student":
        return render_template("student.html")
    return render_template("index.html")


@app.route("/users")
@login_required
@teacher_or_admin_required
def users():
    return render_template("users.html")


@app.route("/classes")
@login_required
@teacher_or_admin_required
def classes():
    return render_template("classes.html")


@app.route("/k8s")
@login_required
def k8s():
    return render_template("k8s.html")


@app.route("/pve")
@login_required
@admin_required
def pve():
    servers = list_pve_servers()
    return render_template("pve.html", servers=servers)


@app.route("/db")
@login_required
@admin_required
def db_config_page():
    cfg = get_db_config()
    safe = {k: v for k, v in cfg.items() if k != "password"}
    return render_template("db_config.html", config=safe)


@app.route("/api/pve/config", methods=["GET", "POST"])
@login_required
@admin_required
@api_error_handler
def pve_config():
    if request.method == "POST":
        data = request.get_json() or {}
        if not data.get("host") or not data.get("user") or not data.get("token_name") or not data.get("token_value"):
            return jsonify({"error": "host、user、token_name、token_value 均为必填项"}), 400
        set_config("pve", data)
        return jsonify({"message": "配置已保存"})

    cfg = get_config("pve") or {}
    return jsonify(sanitize(cfg))


@app.route("/api/pve/servers", methods=["GET"])
@login_required
@api_error_handler
def pve_list_servers():
    return jsonify(sanitize(list_pve_servers()))


@app.route("/api/pve/servers", methods=["POST"])
@login_required
@admin_required
@api_error_handler
def pve_create_server():
    data = request.get_json() or {}
    missing = [k for k in ("name", "host", "user", "token_name", "token_value") if not data.get(k)]
    if missing:
        return jsonify({"error": f"缺少必填项: {', '.join(missing)}"}), 400
    sid = create_pve_server(data)
    return jsonify({"id": sid, "message": "服务器已创建"}), 201


@app.route("/api/pve/servers/<int:sid>", methods=["PUT"])
@login_required
@admin_required
@api_error_handler
def pve_update_server(sid):
    data = request.get_json() or {}
    result = update_pve_server(sid, data)
    if result is None:
        return jsonify({"error": "服务器不存在"}), 404
    return jsonify({"message": "服务器已更新"})


@app.route("/api/pve/servers/<int:sid>", methods=["DELETE"])
@login_required
@admin_required
@api_error_handler
def pve_delete_server(sid):
    delete_pve_server(sid)
    return jsonify({"message": "服务器已删除"})


@app.route("/api/pve/servers/<int:sid>/test", methods=["POST"])
@login_required
@admin_required
@api_error_handler
def pve_test_server(sid):
    cfg = get_pve_server(sid)
    if not cfg:
        return jsonify({"error": "服务器不存在"}), 404
    client = PVEClient(
        host=cfg["host"],
        user=cfg["user"],
        token_name=cfg["token_name"],
        token_value=cfg["token_value"],
        verify_ssl=False,
        port=cfg.get("port", 8006),
    )
    version = client.connect()
    return jsonify({"message": "连接成功", "version": version})


@app.route("/api/pve/servers/<int:sid>/test-openwrt", methods=["POST"])
@login_required
@admin_required
@api_error_handler
def pve_test_openwrt_server(sid):
    cfg = get_pve_server(sid)
    if not cfg:
        return jsonify({"error": "服务器不存在"}), 404
    if not cfg.get("ow_host"):
        return jsonify({"error": "未配置 OpenWrt 连接信息"}), 400
    client = OpenWrtClient(
        host=cfg["ow_host"],
        username=cfg["ow_username"],
        password=cfg["ow_password"],
        port=int(cfg.get("ow_port", 22)),
    )
    try:
        version = client.connect()
        return jsonify({"message": "OpenWrt 连接成功", "version": version})
    finally:
        client.close()


@app.route("/api/pve/servers/<int:sid>/nodes", methods=["GET"])
@login_required
@api_error_handler
def pve_server_nodes(sid):
    client = get_pve_client(server_id=sid)
    return jsonify(client.get_nodes())


@app.route("/api/pve/servers/<int:sid>/vms", methods=["GET"])
@login_required
@api_error_handler
def pve_server_vms(sid):
    node = request.args.get("node")
    client = get_pve_client(server_id=sid)
    return jsonify(client.get_vms(node))


@app.route("/api/pve/test", methods=["POST"])
@login_required
@admin_required
@api_error_handler
def pve_test_connection():
    data = request.get_json() or {}
    client = PVEClient(
        host=data.get("host", ""),
        user=data.get("user", ""),
        token_name=data.get("token_name", ""),
        token_value=data.get("token_value", ""),
        verify_ssl=data.get("verify_ssl", False),
        port=int(data.get("port", 8006)),
    )
    version = client.connect()
    return jsonify({"message": "Connection successful", "version": version})


@app.route("/api/pve/nodes", methods=["GET"])
@login_required
@api_error_handler
def pve_get_nodes():
    server_id = request.args.get("server_id", type=int)
    client = get_pve_client(server_id=server_id)
    return jsonify(client.get_nodes())


@app.route("/api/pve/debug", methods=["GET"])
@login_required
@admin_required
@api_error_handler
def pve_debug():
    server_id = request.args.get("server_id", type=int)
    client = get_pve_client(server_id=server_id)
    nodes_raw = client.get_nodes()
    result = {"nodes": nodes_raw, "vms_by_node": {}}
    for n in nodes_raw:
        node_name = n.get("node", n.get("id", "unknown"))
        try:
            qemu_list = client.api.nodes(node_name).qemu.get()
            result["vms_by_node"][node_name] = qemu_list
        except Exception as e:
            result["vms_by_node"][node_name] = "Error: 节点信息获取失败"
    return jsonify(result)


@app.route("/api/pve/vms", methods=["GET"])
@login_required
@api_error_handler
def pve_get_vms():
    node = request.args.get("node")
    server_id = request.args.get("server_id", type=int)
    client = get_pve_client(server_id=server_id)
    return jsonify(client.get_vms(node))


@app.route("/api/pve/vms/<node>/<int:vmid>/status", methods=["GET"])
@login_required
@api_error_handler
def pve_get_vm_status(node, vmid):
    if not _check_vm_access(node, vmid):
        return jsonify({"error": "无权访问该虚拟机"}), 403
    return jsonify(get_cached_vm_status(node, vmid))


@app.route("/api/pve/vms/status/batch", methods=["POST"])
@login_required
@api_error_handler
def pve_get_vms_status_batch():
    data = request.get_json() or {}
    vms = data.get("vms", [])
    results = {}
    for vm in vms:
        node = vm.get("node")
        vmid = vm.get("vmid")
        if not node or not vmid:
            continue
        if not _check_vm_access(node, vmid, Actions.CLUSTER_READ):
            continue
        key = f"{node}_{vmid}"
        results[key] = get_cached_vm_status(node, vmid)
    return jsonify({"statuses": results})


@app.route("/api/pve/vms/<node>/<int:vmid>/config", methods=["GET"])
@login_required
@api_error_handler
def pve_get_vm_config(node, vmid):
    if not _check_vm_access(node, vmid):
        return jsonify({"error": "无权访问该虚拟机"}), 403
    client = get_pve_client(getattr(g, "_vm_cluster", {}).get("pve_server_id"))
    return jsonify(client.get_vm_config(node, vmid))


@app.route("/api/pve/templates", methods=["GET"])
@login_required
@api_error_handler
def pve_get_templates():
    node = request.args.get("node")
    server_id = request.args.get("server_id", type=int)
    client = get_pve_client(server_id=server_id)
    return jsonify(client.get_templates(node))


@app.route("/api/pve/nextid", methods=["GET"])
@login_required
@api_error_handler
def pve_get_nextid():
    client = get_pve_client()
    return jsonify({"nextid": client.get_next_vmid()})


@app.route("/api/pve/clone", methods=["POST"])
@login_required
@admin_required
@api_error_handler
def pve_clone_vm():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    node = data.get("node")
    vmid = data.get("vmid")
    newid = data.get("newid")
    name = data.get("name")
    config = data.get("config")
    if not all([node, vmid, newid, name]):
        return jsonify({"error": "Missing required fields: node, vmid, newid, name"}), 400
    client = get_pve_client()
    result = client.clone_template(node, vmid, newid, name, config)
    return jsonify({"message": "VM cloned successfully", "newid": result}), 201


@app.route("/api/pve/create", methods=["POST"])
@login_required
@admin_required
@api_error_handler
def pve_create_vm():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data provided"}), 400
    node = data.get("node")
    template_vmid = data.get("template_vmid")
    config = data.get("config")
    if not all([node, template_vmid]):
        return jsonify({"error": "Missing required fields: node, template_vmid"}), 400
    client = get_pve_client()
    result = client.create_vm(node, template_vmid, config)
    return jsonify({"message": "VM created successfully", "newid": result}), 201


@app.route("/api/pve/vms/<node>/<int:vmid>/start", methods=["POST"])
@login_required
@api_error_handler
def pve_start_vm(node, vmid):
    if not _check_vm_access(node, vmid, Actions.CLUSTER_VM_ACTION):
        return jsonify({"error": "无权访问该虚拟机"}), 403
    client = get_pve_client(getattr(g, "_vm_cluster", {}).get("pve_server_id"))
    result = client.start_vm(node, vmid)
    update_vm_status(node, vmid, "running")
    return jsonify(result)


@app.route("/api/pve/vms/<node>/<int:vmid>/stop", methods=["POST"])
@login_required
@api_error_handler
def pve_stop_vm(node, vmid):
    if not _check_vm_access(node, vmid, Actions.CLUSTER_VM_ACTION):
        return jsonify({"error": "无权访问该虚拟机"}), 403
    data = request.get_json() or {}
    force = data.get("force", False)
    client = get_pve_client(getattr(g, "_vm_cluster", {}).get("pve_server_id"))
    result = client.stop_vm(node, vmid, force)
    update_vm_status(node, vmid, "stopped")
    return jsonify(result)


@app.route("/api/pve/vms/<node>/<int:vmid>", methods=["DELETE"])
@login_required
@admin_required
@api_error_handler
def pve_release_vm(node, vmid):
    if not _check_vm_access(node, vmid, Actions.CLUSTER_READ):
        return jsonify({"error": "虚拟机不存在"}), 404
    data = request.get_json(silent=True) or {}
    purge = data.get("purge", True)
    client = get_pve_client(g._vm_cluster["pve_server_id"])
    result = client.release_vm(node, vmid, purge)
    return jsonify(result)


@app.route("/openwrt")
@login_required
@admin_required
def openwrt():
    return redirect("/pve")


def get_openwrt_client(server_id=None):
    if server_id:
        cfg = get_pve_server(server_id)
        if not cfg:
            raise OpenWrtError(f"PVE 服务器 (ID={server_id}) 不存在")
        ow_host = cfg.get("ow_host", "")
        ow_username = cfg.get("ow_username", "")
        ow_password = cfg.get("ow_password", "")
        ow_port = int(cfg.get("ow_port", 22))
    else:
        cfg = get_config("openwrt")
        if not cfg:
            raise OpenWrtError("OpenWrt 未配置，请先在页面中保存配置")
        ow_host = cfg.get("host", "")
        ow_username = cfg.get("username", "")
        ow_password = cfg.get("password", "")
        ow_port = int(cfg.get("port", 22))
    missing = []
    if not ow_host: missing.append("host")
    if not ow_username: missing.append("username")
    if not ow_password: missing.append("password")
    if missing:
        raise OpenWrtError(f"OpenWrt 配置不完整: {', '.join(missing)}")
    return OpenWrtClient(
        host=ow_host,
        username=ow_username,
        password=ow_password,
        port=ow_port,
    )


def openwrt_api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except OpenWrtError as e:
            return jsonify({"error": _safe_error_message(e, "OpenWrt 操作失败")}), 400
        except Exception as e:
            return jsonify({"error": _safe_error_message(e, "OpenWrt 操作失败，请稍后重试")}), 500
    return wrapper


@app.route("/api/openwrt/config", methods=["GET", "POST"])
@login_required
@openwrt_api_error_handler
def openwrt_config():
    if request.method == "POST":
        if current_user.role != "admin":
            return _forbidden()
        data = request.get_json() or {}
        if not data.get("host") or not data.get("username") or not data.get("password"):
            return jsonify({"error": "host、username、password 均为必填项"}), 400
        set_config("openwrt", data)
        return jsonify({"message": "配置已保存"})
    cfg = get_config("openwrt") or {}
    return jsonify(sanitize(cfg))


@app.route("/api/openwrt/test", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_test():
    data = request.get_json() or {}
    client = OpenWrtClient(
        host=data.get("host", ""),
        username=data.get("username", ""),
        password=data.get("password", ""),
        port=int(data.get("port", 22)),
    )
    version = client.connect()
    client.close()
    return jsonify({"message": "SSH connection successful", "version": version})


def _with_openwrt(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        client = get_openwrt_client()
        client.connect()
        try:
            result = f(client, *args, **kwargs)
            return jsonify(result)
        except OpenWrtError as e:
            return jsonify({"error": _safe_error_message(e, "OpenWrt 操作失败")}), 400
        except Exception as e:
            return jsonify({"error": _safe_error_message(e, "OpenWrt 操作失败，请稍后重试")}), 500
        finally:
            try:
                client.close()
            except Exception:
                pass
    return wrapper


@app.route("/api/openwrt/vlans", methods=["GET"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_get_vlans():
    client = get_openwrt_client()
    client.connect()
    try:
        vlans = client.get_vlan_devices()
        return jsonify(list(vlans.values()))
    finally:
        client.close()


@app.route("/api/openwrt/vlans", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_create_vlan():
    data = request.get_json() or {}
    name = data.get("name")
    iface = data.get("iface")
    vid = data.get("vid")
    if not all([name, iface, vid]):
        return jsonify({"error": "name、iface、vid 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_vlan_device(name, iface, vid)
        return jsonify({"message": f"VLAN device {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/vlans/<name>", methods=["DELETE"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_delete_vlan(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_vlan_device(name)
        return jsonify({"message": f"VLAN device {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/interfaces", methods=["GET"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_get_interfaces():
    client = get_openwrt_client()
    client.connect()
    try:
        ifaces = client.get_interfaces()
        return jsonify(list(ifaces.values()))
    finally:
        client.close()


@app.route("/api/openwrt/interfaces", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_create_interface():
    data = request.get_json() or {}
    name = data.get("name")
    device = data.get("device")
    proto = data.get("proto", "static")
    ipaddr = data.get("ipaddr")
    netmask = data.get("netmask", "255.255.255.0")
    if not all([name, device]):
        return jsonify({"error": "name、device 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_interface(name, device, proto, ipaddr, netmask)
        zones = client.get_firewall_zones()
        for sec_name, zone in zones.items():
            if zone.get("name", "").lower() == "lan":
                client.add_interface_to_zone(sec_name, name)
                break
        return jsonify({"message": f"Interface {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/interfaces/<name>", methods=["PUT"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_update_interface(name):
    data = request.get_json() or {}
    client = get_openwrt_client()
    client.connect()
    try:
        client.update_interface(name, **data)
        return jsonify({"message": f"Interface {name} updated"})
    finally:
        client.close()


@app.route("/api/openwrt/interfaces/<name>", methods=["DELETE"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_delete_interface(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_interface(name)
        return jsonify({"message": f"Interface {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/dhcp", methods=["GET"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_get_dhcp():
    client = get_openwrt_client()
    client.connect()
    try:
        pools = client.get_dhcp_pools()
        return jsonify(list(pools.values()))
    finally:
        client.close()


@app.route("/api/openwrt/dhcp", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_create_dhcp():
    data = request.get_json() or {}
    name = data.get("name")
    interface = data.get("interface")
    start = data.get("start", "100")
    limit = data.get("limit", "150")
    leasetime = data.get("leasetime", "12h")
    dhcpv4 = data.get("dhcpv4", "server")
    if not all([name, interface]):
        return jsonify({"error": "name、interface 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_dhcp_pool(name, interface, start, limit, leasetime, dhcpv4)
        return jsonify({"message": f"DHCP pool {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/dhcp/<name>", methods=["DELETE"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_delete_dhcp(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_dhcp_pool(name)
        return jsonify({"message": f"DHCP pool {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/dnsmasq", methods=["GET"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_get_dnsmasq():
    client = get_openwrt_client()
    client.connect()
    try:
        instances = client.get_dnsmasq_instances()
        return jsonify(list(instances.values()))
    finally:
        client.close()


@app.route("/api/openwrt/dnsmasq", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_create_dnsmasq():
    data = request.get_json() or {}
    name = data.get("name")
    interface = data.get("interface")
    listen_address = data.get("listen_address")
    domain = data.get("domain")
    if not all([name, interface, listen_address, domain]):
        return jsonify({"error": "name、interface、listen_address、domain 均为必填项"}), 400
    client = get_openwrt_client()
    client.connect()
    try:
        client.create_dnsmasq(name, interface, listen_address, domain)
        return jsonify({"message": f"dnsmasq instance {name} created"}), 201
    finally:
        client.close()


@app.route("/api/openwrt/dnsmasq/<name>", methods=["DELETE"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_delete_dnsmasq(name):
    client = get_openwrt_client()
    client.connect()
    try:
        client.delete_dnsmasq(name)
        return jsonify({"message": f"dnsmasq instance {name} deleted"})
    finally:
        client.close()


@app.route("/api/openwrt/firewall/zones", methods=["GET"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_get_firewall_zones():
    client = get_openwrt_client()
    client.connect()
    try:
        zones = client.get_firewall_zones()
        return jsonify(list(zones.values()))
    finally:
        client.close()


@app.route("/api/openwrt/firewall/<zone>/interfaces/<interface>", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_add_interface_to_zone(zone, interface):
    client = get_openwrt_client()
    client.connect()
    try:
        client.add_interface_to_zone(zone, interface)
        return jsonify({"message": f"Interface {interface} added to zone {zone}"})
    finally:
        client.close()


@app.route("/api/openwrt/firewall/<zone>/interfaces/<interface>", methods=["DELETE"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_remove_interface_from_zone(zone, interface):
    client = get_openwrt_client()
    client.connect()
    try:
        client.remove_interface_from_zone(zone, interface)
        return jsonify({"message": f"Interface {interface} removed from zone {zone}"})
    finally:
        client.close()


@app.route("/api/openwrt/restart/network", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_restart_network():
    client = get_openwrt_client()
    client.connect()
    try:
        client.restart_network()
        return jsonify({"message": "Network restarted"})
    finally:
        client.close()


@app.route("/api/openwrt/restart/firewall", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_restart_firewall():
    client = get_openwrt_client()
    client.connect()
    try:
        client.restart_firewall()
        return jsonify({"message": "Firewall restarted"})
    finally:
        client.close()


@app.route("/api/openwrt/restart/dnsmasq", methods=["POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_restart_dnsmasq():
    client = get_openwrt_client()
    client.connect()
    try:
        client.exec("/etc/init.d/dnsmasq restart", tolerant=True)
        return jsonify({"message": "dnsmasq restarted"})
    finally:
        client.close()



def _filter_clusters(clusters_dict):
    """Filter clusters through the shared authorization policy."""
    return {
        name: cluster
        for name, cluster in clusters_dict.items()
        if _is_allowed(Actions.CLUSTER_READ, cluster)
    }


def k8s_api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except AuthorizationDenied:
            return jsonify({"error": "权限不足"}), 403
        except ValueError:
            return jsonify({"error": "请求参数无效"}), 400
        except (K8sError, PVEError, OpenWrtError) as e:
            return jsonify({"error": _safe_error_message(e, "集群操作失败")}), 400
        except Exception as e:
            return jsonify({"error": _safe_error_message(e, "集群操作失败，请稍后重试")}), 500
    return wrapper


@app.route("/api/k8s/clusters", methods=["GET"])
@login_required
@k8s_api_error_handler
def k8s_list_clusters():
    data = list_clusters()
    data = _filter_clusters(data)
    safe = {}
    for name, c in data.items():
        entry = {k: v for k, v in c.items() if k != "ssh_private_key"}
        _pve_sid = c.get("pve_server_id")
        if _pve_sid:
            _ow_cfg = get_pve_server(_pve_sid) or {}
            entry["ssh_host"] = _ow_cfg.get("ow_host", "")
        else:
            _ow_cfg = get_config("openwrt") or {}
            entry["ssh_host"] = _ow_cfg.get("host", "")
        entry.pop("ssh_private_key", None)
        safe[name] = sanitize(entry)
    return jsonify(safe)


@app.route("/api/k8s/clusters", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_create_cluster():
    if not _is_allowed(Actions.CLUSTER_CREATE):
        return _forbidden()
    data = request.get_json() or {}
    master_count = int(data.get("master_count", 1))
    node_count = int(data.get("node_count", 1))
    master_cores = int(data.get("master_cores", 4))
    master_memory = int(data.get("master_memory", 4096))
    node_cores = int(data.get("node_cores", 4))
    node_memory = int(data.get("node_memory", 4096))
    pve_node = data.get("pve_node", "")
    password = data.get("password", "k8s.1234")
    pve_server_id = int(data.get("pve_server_id", 0))
    _validate_cluster_creation(
        current_user,
        group_ids=[data["group_id"]] if data.get("group_id") is not None else [],
        class_id=data.get("class_id"),
    )

    if master_count < 1:
        return jsonify({"error": "主节点数量至少为 1"}), 400
    if node_count < 1:
        return jsonify({"error": "子节点数量至少为 1"}), 400
    if not pve_node:
        return jsonify({"error": "请选择 PVE 节点"}), 400

    name, cluster = create_cluster(
        master_count, node_count,
        master_cores, master_memory,
        node_cores, node_memory,
        pve_node,
        pve_server_id=pve_server_id,
        created_by=current_user.id,
        group_id=data.get("group_id"),
        class_id=data.get("class_id"),
    )
    save_cluster(name, {**cluster, "created_by": current_user.id})
    safe = dict(cluster)
    safe.pop("ssh_private_key", None)
    safe = sanitize(safe)
    return jsonify({"name": name, "cluster": safe}), 201


@app.route("/api/k8s/clusters/<name>", methods=["DELETE"])
@login_required
@k8s_api_error_handler
def k8s_delete_cluster(name):
    cluster = get_cluster(name)
    if not cluster or not _is_allowed(Actions.CLUSTER_DELETE, cluster):
        return _forbidden("无权操作该集群")
    task_id = delete_cluster_async(name, created_by=current_user.id)
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/clusters/<name>/force", methods=["DELETE"])
@login_required
@admin_required
@k8s_api_error_handler
def k8s_force_delete_cluster(name):
    force_delete_cluster(name, actor_id=current_user.id)
    return jsonify({"message": "集群已强制删除"})


@app.route("/api/k8s/create", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_create_cluster_async_route():
    if not _is_allowed(Actions.CLUSTER_CREATE):
        return _forbidden()
    data = request.get_json() or {}
    master_count = int(data.get("master_count", 1))
    node_count = int(data.get("node_count", 1))
    master_cores = int(data.get("master_cores", 4))
    master_memory = int(data.get("master_memory", 4096))
    node_cores = int(data.get("node_cores", 4))
    node_memory = int(data.get("node_memory", 4096))
    pve_node = data.get("pve_node", "")
    pve_server_id = int(data.get("pve_server_id", 0))
    group_id = data.get("group_id")
    class_id = data.get("class_id")
    _validate_cluster_creation(
        current_user,
        group_ids=[group_id] if group_id is not None else [],
        class_id=class_id,
    )

    if master_count < 1:
        return jsonify({"error": "主节点数量至少为 1"}), 400
    if node_count < 1:
        return jsonify({"error": "子节点数量至少为 1"}), 400
    if not pve_node:
        return jsonify({"error": "请选择 PVE 节点"}), 400

    task_id = create_cluster_async(
        master_count, node_count,
        master_cores, master_memory,
        node_cores, node_memory,
        pve_node,
        pve_server_id=pve_server_id,
        group_id=group_id,
        class_id=class_id,
        created_by=current_user.id,
    )
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/batch-create", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_batch_create_clusters():
    if not _is_allowed(Actions.CLUSTER_CREATE):
        return _forbidden()
    data = request.get_json() or {}
    group_ids = data.get("group_ids", [])
    if not group_ids:
        return jsonify({"error": "请选择至少一个组"}), 400
    master_count = int(data.get("master_count", 1))
    node_count = int(data.get("node_count", 1))
    master_cores = int(data.get("master_cores", 4))
    master_memory = int(data.get("master_memory", 4096))
    node_cores = int(data.get("node_cores", 4))
    node_memory = int(data.get("node_memory", 4096))
    pve_node = data.get("pve_node", "")
    pve_server_id = int(data.get("pve_server_id", 0))
    class_id = data.get("class_id")
    _validate_cluster_creation(current_user, group_ids=group_ids, class_id=class_id)

    task_ids = batch_create_clusters_async(
        group_ids, master_count, node_count,
        master_cores, master_memory,
        node_cores, node_memory,
        pve_node,
        pve_server_id=pve_server_id,
        created_by=current_user.id,
        class_id=class_id,
    )
    return jsonify({"task_ids": task_ids, "count": len(task_ids)}), 202


@app.route("/api/k8s/tasks", methods=["GET"])
@login_required
@k8s_api_error_handler
def k8s_list_tasks():
    return jsonify({"tasks": sanitize(list_tasks(actor=current_user))})


@app.route("/api/k8s/tasks/<task_id>", methods=["GET"])
@login_required
@k8s_api_error_handler
def k8s_get_task(task_id):
    status = get_task_status(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404
    if not _is_allowed(Actions.TASK_READ, status):
        return _forbidden()
    return jsonify(sanitize(status))


@app.route("/api/k8s/tasks/<task_id>/cancel", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_cancel_task(task_id):
    status = get_task_status(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404
    if not _is_allowed(Actions.TASK_CANCEL, status):
        return _forbidden()
    ok = cancel_task(task_id, actor_id=current_user.id)
    if not ok:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"message": "取消请求已发送"})


@app.route("/api/k8s/clusters/<name>/deploy", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_deploy_cluster(name):
    _actor, cluster = _authorize_cluster_action(
        current_user.id, Actions.CLUSTER_DEPLOY, name
    )
    if not cluster:
        return jsonify({"error": "集群不存在"}), 404
    if cluster.get("status") != "running":
        return jsonify({"error": "集群状态异常，无法部署 K8s"}), 400
    task_id = deploy_k8s_async(name, created_by=current_user.id)
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/clusters/<name>/vm-action-all", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_cluster_vm_action_all(name):
    if not _check_cluster_access(name, Actions.CLUSTER_VM_ACTION):
        return _forbidden("无权操作该集群")
    cluster = get_cluster(name)
    if not cluster:
        return jsonify({"error": "集群不存在"}), 404
    data = request.get_json() or {}
    action = data.get("action", "")
    if action not in ("start", "stop"):
        return jsonify({"error": "无效操作"}), 400
    client = get_pve_client(server_id=cluster.get("pve_server_id"))
    results = []
    for vm_name, vm_info in cluster.get("vms", {}).items():
        try:
            if action == "start":
                client.start_vm(vm_info["node"], vm_info["vmid"])
                update_vm_status(vm_info["node"], vm_info["vmid"], "running")
            else:
                client.stop_vm(vm_info["node"], vm_info["vmid"])
                update_vm_status(vm_info["node"], vm_info["vmid"], "stopped")
            results.append({"vm": vm_name, "status": "ok"})
        except Exception as e:
            results.append({"vm": vm_name, "status": "failed", "error": "虚拟机操作失败"})
    ok = sum(1 for r in results if r["status"] == "ok")
    return jsonify({"results": results, "message": f"{ok}/{len(results)}"})


@app.route("/api/classes/<int:cid>/vm-action-all", methods=["POST"])
@login_required
def api_class_vm_action_all(cid):
    cls = get_class(cid)
    if not cls:
        return jsonify({"error": "课程不存在"}), 404
    if current_user.role == "teacher" and cls.get("created_by") != current_user.id:
        return jsonify({"error": "只能操作自己创建的课程"}), 403
    if current_user.role == "student":
        return jsonify({"error": "无权操作"}), 403
    data = request.get_json() or {}
    action = data.get("action", "")
    if action not in ("start", "stop"):
        return jsonify({"error": "无效操作"}), 400

    cluster_names = list_cluster_names_by_class_id(cid)
    if not cluster_names:
        return jsonify({"error": "该课程下没有集群"}), 400

    results = []
    client_cache = {}
    for name in cluster_names:
        cluster = get_cluster(name)
        if not cluster or not cluster.get("vms"):
            continue
        server_id = cluster.get("pve_server_id")
        if server_id not in client_cache:
            client_cache[server_id] = get_pve_client(server_id=server_id)
        client = client_cache[server_id]
        for vm_name, vm_info in cluster["vms"].items():
            try:
                if action == "start":
                    client.start_vm(vm_info["node"], vm_info["vmid"])
                    update_vm_status(vm_info["node"], vm_info["vmid"], "running")
                else:
                    client.stop_vm(vm_info["node"], vm_info["vmid"])
                    update_vm_status(vm_info["node"], vm_info["vmid"], "stopped")
                results.append({"cluster": name, "vm": vm_name, "status": "ok"})
            except Exception as e:
                results.append({"cluster": name, "vm": vm_name, "status": "failed", "error": "虚拟机操作失败"})

    ok = sum(1 for r in results if r["status"] == "ok")
    return jsonify({"results": results, "message": f"{ok}/{len(results)}"})


@app.route("/k8s/logs/<task_id>")
@login_required
@k8s_api_error_handler
def k8s_logs_page(task_id):
    status = get_task_status(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404
    if not _is_allowed(Actions.TASK_READ, status):
        return _forbidden()
    return render_template("k8s_logs.html", task_id=task_id)


# ── Database config (admin) ──

@app.route("/api/db/config", methods=["GET", "POST"])
@login_required
@admin_required
def db_config():
    if request.method == "POST":
        data = request.get_json() or {}
        set_db_config(data)
        reload_db_engine()
        init_db()
        return jsonify({"message": "数据库配置已保存并重新初始化"})
    cfg = get_db_config()
    return jsonify({"config": sanitize(cfg)})


@app.route("/api/db/status", methods=["GET"])
@login_required
@admin_required
def db_status():
    return jsonify(get_db_status())


@app.route("/api/db/test", methods=["POST"])
@login_required
@admin_required
def db_test():
    data = request.get_json() or {}
    host = data.get("host", "")
    port = data.get("port", 5432)
    user = data.get("user", "")
    password = data.get("password", "")
    database = data.get("database", "postgres")

    client = PGClient(
        host=host, port=port, user=user,
        password=password, database=database,
    )
    try:
        version = client.connect()
        return jsonify({"version": version, "ok": True})
    except PGError as e:
        return jsonify({"error": _safe_error_message(e, "数据库连接失败")}), 400
    except Exception as e:
        return jsonify({"error": _safe_error_message(e, "数据库连接失败")}), 500
    finally:
        client.close()


# ── WebSSH ──

_webssh_connect_times = {}


def _socket_user():
    if not current_user.is_authenticated:
        return None
    user_id = getattr(current_user, "id", None)
    registry = _webssh_sid_users if request.namespace == "/webssh" else _state_sid_users
    bound_id = registry.get(request.sid, user_id)
    if bound_id != user_id:
        return None
    return _fresh_user(bound_id)


def _audit_webssh(action, outcome, resource_id, actor=None, metadata=None):
    security_audit(
        action, outcome, actor=actor,
        resource_type="webssh", resource_id=resource_id, metadata=metadata,
    )


def _webssh_event(action):
    """Audit every event outcome without retaining terminal input or secrets."""
    def decorate(handler):
        @wraps(handler)
        def wrapped(data=None):
            actor = _socket_user()
            resource_id = None
            try:
                if not actor or request.sid not in _webssh_sid_users:
                    ssh_manager.unbind_ws(request.sid)
                    raise AuthorizationDenied("未登录或账号已失效")
                data = {} if data is None else data
                if not isinstance(data, dict):
                    raise ValueError("请求参数无效")
                resource_id = data.get("cluster") or data.get("session_id")
                if resource_id is None:
                    bound = ssh_manager.bound_session(request.sid)
                    resource_id = bound.session_id if bound else None
                result = handler(data, actor)
                _audit_webssh(action, "ignored" if result == "ignored" else "success", resource_id, actor=actor)
                return result
            except AuthorizationDenied:
                _audit_webssh(action, "denied", resource_id, actor=actor)
                emit("ssh_error", {"message": "会话不存在或权限已失效"})
            except ValueError:
                _audit_webssh(action, "denied", resource_id, actor=actor)
                emit("ssh_error", {"message": "请求参数无效"})
            except SessionExistsError:
                _audit_webssh(action, "denied", resource_id, actor=actor)
                emit("ssh_error", {"message": "已有连接存在，请重新连接或终止后重试"})
            except TooManyConnectionsError:
                _audit_webssh(action, "denied", resource_id, actor=actor)
                emit("ssh_error", {"message": "连接数已达上限，请稍后重试"})
            except Exception:
                _audit_webssh(action, "failure", resource_id, actor=actor)
                emit("ssh_error", {"message": "SSH 操作失败，请稍后重试"})
        return wrapped
    return decorate


def _require_session(session_id):
    terminal = ssh_manager.get_session(session_id)
    if not terminal or terminal.status == "terminated":
        raise AuthorizationDenied("会话不存在")
    return terminal


@socketio.on("connect", namespace="/webssh")
def webssh_connect(auth=None):
    actor = _socket_user()
    if not actor:
        _audit_webssh("webssh.connect", "denied", None)
        return False
    _webssh_sid_users[request.sid] = actor.id
    _audit_webssh("webssh.connect", "success", None, actor=actor)


@socketio.on("session_create", namespace="/webssh")
@_webssh_event("webssh.connect")
def webssh_session_create(data, actor):
    cluster_name = data.get("cluster")
    cluster = get_cluster(cluster_name) if cluster_name else None
    if not cluster or not _is_allowed_for_user(actor, Actions.WEBSSH_CONNECT, cluster):
        raise AuthorizationDenied("无权访问该集群")
    if cluster.get("status") != "running":
        raise ValueError("集群未运行")
    if not any(info.get("role") == "client" for info in (cluster.get("vms") or {}).values()):
        raise ValueError("客户端虚拟机不存在")
    sid = request.sid
    now = time.time()
    if now - _webssh_connect_times.get(sid, 0) < 10:
        raise ValueError("操作过于频繁")
    _webssh_connect_times[sid] = now
    server_id = cluster.get("pve_server_id")
    host_config = (get_pve_server(server_id) or {}) if server_id else (get_config("openwrt") or {})
    host = host_config.get("ow_host" if server_id else "host", "")
    username = "teacher"
    password = cluster.get("password", "")
    if actor.role == "student":
        member = get_group_member(cluster.get("group_id"), actor.id)
        number = member.get("student_number") if member else None
        username = f"student{number}"
        credential = (cluster.get("students") or {}).get(username)
        if number is None or not credential:
            raise AuthorizationDenied("学生账户不存在")
        password = credential.get("password", "")
    owner = {"user_id": actor.id, "username": actor.username, "role": actor.role}
    terminal = ssh_manager.create_session(
        cluster_name, owner, host, cluster.get("ssh_port", 22), username, password
    )
    try:
        if not ssh_manager.bind_ws(sid, terminal.session_id, role="owner"):
            raise AuthorizationDenied("权限已失效")
        ssh_manager.connect_session(terminal.session_id, sid=sid)
    except Exception:
        ssh_manager.terminate_session(terminal.session_id)
        raise
    ssh_manager.emit_to(terminal, "session_created", {
        "session_id": terminal.session_id, "cluster": cluster_name,
    }, sid)
    _emit_session_update(cluster_name, actor.id, "created")


@socketio.on("session_reconnect", namespace="/webssh")
@_webssh_event("webssh.reconnect")
def webssh_session_reconnect(data, actor):
    terminal = ssh_manager.get_session_by_cluster(actor.id, data.get("cluster"))
    if not terminal or terminal.status == "terminated":
        raise AuthorizationDenied("会话不存在")
    if not ssh_manager.bind_ws(request.sid, terminal.session_id, role="owner"):
        raise AuthorizationDenied("权限已失效")
    if not terminal.channel or terminal.channel.closed:
        ssh_manager.connect_session(terminal.session_id, sid=request.sid)
    if not ssh_manager.replay(request.sid):
        raise AuthorizationDenied("权限已失效")
    if terminal.takeover_active and terminal.takeover_by:
        ssh_manager.emit_to(terminal, "takeover_notify", {
            "by": terminal.takeover_by.get("username", ""),
            "cluster": terminal.cluster_name,
        }, request.sid)
    ssh_manager.emit_to(terminal, "session_reconnected", {
        "session_id": terminal.session_id, "cluster": terminal.cluster_name,
    }, request.sid)
    _emit_session_update(terminal.cluster_name, actor.id, "reconnected")


@socketio.on("session_terminate", namespace="/webssh")
@_webssh_event("webssh.terminate_own")
def webssh_session_terminate(data, actor):
    terminal = ssh_manager.get_session_by_cluster(actor.id, data.get("cluster"))
    if not terminal or not _authorize_webssh_binding(request.sid, terminal, "owner"):
        raise AuthorizationDenied("权限已失效")
    if not _is_allowed_for_user(actor, Actions.WEBSSH_TERMINATE_OWN, _session_authz_resource(terminal)):
        raise AuthorizationDenied("权限不足")
    ssh_manager.terminate_by_owner(actor.id, terminal.cluster_name)
    emit("session_terminated", {"cluster": terminal.cluster_name})
    _emit_session_update(terminal.cluster_name, actor.id, "terminated")


@socketio.on("takeover_start", namespace="/webssh")
@_webssh_event("webssh.takeover_start")
def webssh_takeover_start(data, actor):
    terminal = _require_session(data.get("session_id"))
    ok, _result = ssh_manager.takeover_session(terminal.session_id, {
        "user_id": actor.id, "username": actor.username, "role": actor.role,
    }, sid=request.sid)
    if not ok:
        raise AuthorizationDenied("无权接管")
    if not ssh_manager.replay(request.sid):
        raise AuthorizationDenied("权限已失效")
    ssh_manager.emit_to(terminal, "takeover_started", {
        "session_id": terminal.session_id, "cluster": terminal.cluster_name,
    }, request.sid)
    ssh_manager.emit_to(terminal, "takeover_notify", {
        "by": actor.username, "cluster": terminal.cluster_name,
    }, terminal.owner_sid)
    _emit_session_update(terminal.cluster_name, terminal.owner["user_id"], "takeover_start", {
        "taker": actor.username,
    })


@socketio.on("takeover_stop", namespace="/webssh")
@_webssh_event("webssh.takeover_stop")
def webssh_takeover_stop(data, actor):
    terminal = _require_session(data.get("session_id"))
    if not ssh_manager.release_takeover(terminal.session_id, sid=request.sid):
        raise AuthorizationDenied("无权释放接管")
    emit("takeover_stopped", {"session_id": terminal.session_id})


@socketio.on("view_start", namespace="/webssh")
@_webssh_event("webssh.view_start")
def webssh_view_start(data, actor):
    terminal = _require_session(data.get("session_id"))
    if not ssh_manager.view_session(terminal.session_id, request.sid):
        raise AuthorizationDenied("无权观察")
    if not ssh_manager.replay(request.sid):
        raise AuthorizationDenied("权限已失效")
    ssh_manager.emit_to(terminal, "view_started", {
        "session_id": terminal.session_id, "cluster": terminal.cluster_name,
    }, request.sid)


@socketio.on("view_stop", namespace="/webssh")
@_webssh_event("webssh.view_stop")
def webssh_view_stop(data, actor):
    terminal = _require_session(data.get("session_id"))
    if not ssh_manager.authorize_ws(request.sid, terminal.session_id, "viewer"):
        raise AuthorizationDenied("无权停止观察")
    ssh_manager.unview_session(request.sid)
    emit("view_stopped", {"session_id": terminal.session_id})


@socketio.on("reconnect_request", namespace="/webssh")
@_webssh_event("webssh.control_request")
def webssh_reconnect_request(data, actor):
    terminal = _require_session(data.get("session_id"))
    ok, _result = ssh_manager.request_reconnect(terminal.session_id, {
        "user_id": actor.id, "username": actor.username,
    }, sid=request.sid)
    if not ok:
        raise AuthorizationDenied("无权恢复控制")
    ssh_manager.emit_to(terminal, "reconnect_requested", {
        "session_id": terminal.session_id, "expires_at": terminal.reconnect_expires_at,
    }, request.sid)
    ssh_manager.emit_to(terminal, "reconnect_request_notify", {
        "session_id": terminal.session_id, "student": actor.username,
        "cluster": terminal.cluster_name,
    }, terminal.takeover_sid)


@socketio.on("reconnect_response", namespace="/webssh")
@_webssh_event("webssh.control_response")
def webssh_reconnect_response(data, actor):
    terminal = _require_session(data.get("session_id"))
    if data.get("action") not in {"accept", "reject"}:
        raise ValueError("请求参数无效")
    accepted = data["action"] == "accept"
    if not ssh_manager.respond_reconnect(terminal.session_id, accepted, sid=request.sid):
        raise AuthorizationDenied("无权恢复控制")
    if accepted:
        emit("takeover_accepted", {"session_id": terminal.session_id})
    ssh_manager.emit_to(terminal, "reconnect_response", {
        "accepted": accepted, "session_id": terminal.session_id,
    }, terminal.owner_sid)


@socketio.on("ssh_data", namespace="/webssh")
@_webssh_event("webssh.write")
def webssh_ssh_data(data, actor):
    raw = data.get("data", "")
    if not isinstance(raw, str) or not raw:
        raise ValueError("终端输入无效")
    if not ssh_manager.write(request.sid, raw):
        raise AuthorizationDenied("无权输入")


@socketio.on("ssh_resize", namespace="/webssh")
@_webssh_event("webssh.resize")
def webssh_ssh_resize(data, actor):
    cols, rows = data.get("cols", 80), data.get("rows", 24)
    if any(type(value) is not int or not 1 <= value <= 1000 for value in (cols, rows)):
        raise ValueError("终端尺寸无效")
    terminal = ssh_manager.bound_session(request.sid)
    if terminal and terminal._role_for(request.sid) == "viewer":
        if not ssh_manager.authorize_ws(request.sid, terminal.session_id, "viewer"):
            raise AuthorizationDenied("观察权限已失效")
        # The browser fits its local terminal after view_started.  Viewers do
        # not control the remote PTY, but this automatic event is not an error.
        return "ignored"
    if not ssh_manager.resize(request.sid, cols, rows):
        raise AuthorizationDenied("无权调整终端")


@socketio.on("disconnect", namespace="/webssh")
def webssh_disconnect(*args):
    actor = _fresh_user(_webssh_sid_users.get(request.sid))
    ssh_manager.unbind_ws(request.sid)
    _webssh_sid_users.pop(request.sid, None)
    _webssh_connect_times.pop(request.sid, None)
    _audit_webssh("webssh.disconnect", "success", None, actor=actor)


# ── State Push Namespace ──

@socketio.on("connect", namespace="/state")
def state_connect():
    user = _socket_user()
    if not user:
        return False
    _state_sid_users[request.sid] = user.id
    _add_online_user(user.id, request.sid)
    if user.role == "admin":
        join_room("admin")
    elif user.role == "teacher":
        join_room("teacher")
    join_room(f"user_{user.id}")

@socketio.on("disconnect", namespace="/state")
def state_disconnect():
    _state_sid_users.pop(request.sid, None)
    _remove_online_user(request.sid)

@socketio.on("heartbeat", namespace="/state")
def state_heartbeat(*args):
    if _socket_user():
        leave_room("admin")
        leave_room("teacher")
        _update_online_user(request.sid)
    else:
        _state_sid_users.pop(request.sid, None)
        _remove_online_user(request.sid)


# ── VM 关机投票 SocketIO 事件 ──

def _shutdown_key(vote):
    if vote["type"] == "cluster":
        return ("cluster", vote["pve_server_id"], vote["cluster_name"])
    return ("vm", vote["pve_server_id"], vote["node"], vote["vmid"])


def _shutdown_identity(cluster):
    return (
        cluster.get("pve_server_id"), cluster.get("created_by"), cluster.get("group_id"),
        tuple(sorted((name, info.get("node"), info.get("vmid"))
                     for name, info in (cluster.get("vms") or {}).items())),
    )


def _authorize_shutdown(actor_id, cluster_name, node=None, vmid=None, expected=None):
    actor = _fresh_user(actor_id)
    cluster = get_cluster(cluster_name) if cluster_name else None
    if not actor or not cluster or not _is_allowed_for_user(actor, Actions.CLUSTER_VM_ACTION, cluster):
        raise AuthorizationDenied("无权操作该集群")
    if node is not None or vmid is not None:
        if not isinstance(node, str) or not node or type(vmid) is not int:
            raise ValueError("虚拟机参数无效")
        if not any(info.get("node") == node and info.get("vmid") == vmid
                   for info in (cluster.get("vms") or {}).values()):
            raise AuthorizationDenied("虚拟机不属于指定集群")
    if expected is not None and _shutdown_identity(cluster) != expected:
        raise AuthorizationDenied("资源身份已变化")
    return actor, cluster


def _audit_shutdown(action, outcome, vote, actor=None, reason=None):
    security_audit(
        action, outcome, actor=actor,
        resource_type="cluster_vm" if vote.get("type") == "single" else "cluster",
        resource_id=vote.get("cluster_name"), reason=reason,
        metadata={
            "actor_id": (vote.get("initiator") or {}).get("id"),
            "provider_id": vote.get("pve_server_id"),
            "node": vote.get("node"), "vmid": vote.get("vmid"), "vote_id": vote.get("id"),
        },
    )


def _shutdown_event(action):
    def decorate(handler):
        @wraps(handler)
        def wrapped(data=None):
            actor = _socket_user()
            g.shutdown_resource = {}
            try:
                if not actor or request.sid not in _state_sid_users:
                    raise AuthorizationDenied("账号已失效")
                if not isinstance(data, dict):
                    raise ValueError("请求参数无效")
                handler(data, actor)
                _audit_shutdown(action, "success", g.shutdown_resource, actor=actor)
            except (AuthorizationDenied, ValueError):
                _audit_shutdown(action, "denied", g.shutdown_resource, actor=actor,
                                reason="权限或资源校验失败")
                emit("vm_shutdown_error", {"message": "无权操作或请求参数无效"})
            except Exception:
                _audit_shutdown(action, "failure", g.shutdown_resource, actor=actor,
                                reason="关机操作失败")
                emit("vm_shutdown_error", {"message": "关机操作失败，请稍后重试"})
        return wrapped
    return decorate


def _emit_vote_event(vote, event, data):
    try:
        cluster = get_cluster(vote.get("cluster_name"))
    except Exception:
        return
    if not cluster:
        return
    participants = set(vote.get("voters", {}))
    participants.add(vote.get("initiator", {}).get("id"))
    _emit_state(event, data, lambda user: (
        (user.role == "admin" or user.id in participants)
        and _is_allowed_for_user(user, Actions.CLUSTER_VM_ACTION, cluster)
    ))


def _broadcast_countdown(vote_id):
    with _vm_shutdown_lock:
        vote = _vm_shutdown_votes.get(vote_id)
        if not vote or vote["completed"]:
            return
        remaining = max(0, vote["timeout"] - int(time.time() - vote["started_at"]))
    if remaining > 0:
        _emit_vote_event(vote, "vm_shutdown_countdown", {
            "vote_id": vote_id, "remaining": remaining,
        })
        timer = threading.Timer(1, _broadcast_countdown, args=[vote_id])
        timer.daemon = True
        timer.start()


def _emit_vote_result(vote_id, vote, all_agree):
    # Caller holds the vote lock.  No external authorization is performed here.
    vote["completed"] = True
    vote["cancelled"] = not all_agree
    if vote.get("timer"):
        vote["timer"].cancel()
    _vm_shutdown_pending_vms.pop(_shutdown_key(vote), None)


def _execute_vote_action(vote_id, vote):
    with _vm_shutdown_lock:
        if vote.get("executing") or vote.get("executed") or vote.get("cancelled"):
            return
        vote["executing"] = True
    actor = None
    try:
        actor, cluster = _authorize_shutdown(
            vote["initiator"]["id"], vote["cluster_name"],
            vote.get("node"), vote.get("vmid"), expected=vote["identity"],
        )
        if actor.role != vote["initiator"]["role"]:
            raise AuthorizationDenied("发起人角色已变化")
        for uid in vote.get("voters", {}):
            voter = _fresh_user(uid)
            if not voter or voter.role != "student" or not _is_allowed_for_user(
                voter, Actions.CLUSTER_VM_ACTION, cluster
            ):
                raise AuthorizationDenied("参与者权限已变化")
        if vote["type"] == "single":
            _do_shutdown_vm(vote["node"], vote["vmid"], cluster.get("pve_server_id"))
        else:
            _do_shutdown_cluster_vms(vote["cluster_name"], cluster.get("pve_server_id"), cluster=cluster)
        vote["executed"] = True
        _audit_shutdown("vm_shutdown.execute", "success", vote, actor=actor)
        if vote_id:
            _emit_vote_event(vote, "vm_shutdown_proceed", {
                "vote_id": vote_id, "type": vote["type"],
                "cluster_name": vote["cluster_name"], "node": vote.get("node"), "vmid": vote.get("vmid"),
            })
    except Exception as exc:
        _audit_shutdown(
            "vm_shutdown.execute", "denied" if isinstance(exc, AuthorizationDenied) else "failure",
            vote, actor=actor, reason="执行前校验失败" if isinstance(exc, AuthorizationDenied) else "关机失败",
        )
        raise
    finally:
        vote["executing"] = False


def _complete_vote(vote):
    if all(info["agree"] for info in vote["voters"].values()):
        try:
            _execute_vote_action(vote["id"], vote)
        except Exception:
            _emit_vote_event(vote, "vm_shutdown_error", {
                "vote_id": vote["id"], "message": "关机未执行或未完成，请检查权限和资源状态",
            })
    else:
        _emit_vote_event(vote, "vm_shutdown_cancelled", {
            "vote_id": vote["id"], "reason": "参与者拒绝关机",
        })
        _audit_shutdown("vm_shutdown.cancel", "success", vote, reason="参与者拒绝关机")
    _cleanup_stale_votes()


def _on_vote_timeout(vote_id):
    with _vm_shutdown_lock:
        vote = _vm_shutdown_votes.get(vote_id)
        if not vote or vote["completed"]:
            return
        for info in vote["voters"].values():
            if not info["voted"]:
                info.update(voted=True, agree=True)
        _emit_vote_result(vote_id, vote, all(info["agree"] for info in vote["voters"].values()))
    _complete_vote(vote)


def _create_vote(vote, online_students):
    actor_id = vote["initiator"]["id"]
    vote_id = _gen_vote_id()
    vote.update(
        id=vote_id, voters={
            user.id: {
                "name": getattr(user, "name", None) or user.username,
                "username": user.username, "voted": user.id == actor_id,
                "agree": True if user.id == actor_id else None,
            } for user in online_students
        },
        started_at=time.time(), timeout=30, completed=False, cancelled=False, timer=None,
    )
    with _vm_shutdown_lock:
        key = _shutdown_key(vote)
        if key in _vm_shutdown_pending_vms:
            raise ValueError("已有待确认关机")
        _vm_shutdown_votes[vote_id] = vote
        _vm_shutdown_pending_vms[key] = vote_id
    payload = {
        "vote_id": vote_id, "type": vote["type"], "cluster_name": vote["cluster_name"],
        "initiator_name": vote["initiator"]["name"], "remaining": 30,
        "node": vote.get("node"), "vmid": vote.get("vmid"),
        "vm_name": vote.get("vm_name"), "vm_list": vote.get("vm_list", []),
    }
    _emit_vote_event(vote, "vm_shutdown_request", payload)
    emit("vm_shutdown_pending", {
        **payload, "total_voters": len(vote["voters"]), "agreed": 1,
    })
    timer = threading.Timer(30, _on_vote_timeout, args=[vote_id])
    timer.daemon = True
    vote["timer"] = timer
    timer.start()
    _broadcast_countdown(vote_id)
    return vote_id


def _initiate_shutdown(data, actor, vote_type):
    cluster_name = data.get("cluster_name")
    node = data.get("node") if vote_type == "single" else None
    vmid = data.get("vmid") if vote_type == "single" else None
    if vote_type == "single" and (node is None or vmid is None):
        raise ValueError("虚拟机参数无效")
    actor, cluster = _authorize_shutdown(actor.id, cluster_name, node, vmid)
    vote = {
        "type": vote_type, "cluster_name": cluster_name,
        "pve_server_id": cluster.get("pve_server_id"), "group_id": cluster.get("group_id"),
        "node": node, "vmid": vmid, "identity": _shutdown_identity(cluster),
        "initiator": {"id": actor.id, "role": actor.role,
                      "name": getattr(actor, "name", None) or actor.username},
        "voters": {},
        "vm_list": sorted((cluster.get("vms") or {}).keys()),
        "vm_name": next((name for name, info in (cluster.get("vms") or {}).items()
                         if info.get("node") == node and info.get("vmid") == vmid), None),
    }
    g.shutdown_resource = vote
    with _vm_shutdown_lock:
        if _shutdown_key(vote) in _vm_shutdown_pending_vms:
            raise ValueError("已有待确认关机")
    online_students = []
    if actor.role == "student" and cluster.get("group_id"):
        for member in _get_online_students_in_group(cluster["group_id"]):
            user = _fresh_user(member["id"])
            if user and user.role == "student" and _is_allowed_for_user(user, Actions.CLUSTER_VM_ACTION, cluster):
                online_students.append(user)
    if actor.role == "student" and len(online_students) >= 2:
        if actor.id not in {user.id for user in online_students}:
            raise AuthorizationDenied("发起人不属于当前分组")
        _create_vote(vote, online_students)
    else:
        _execute_vote_action(None, vote)
        emit("vm_shutdown_allowed", {
            "type": vote_type, "cluster_name": cluster_name, "node": node, "vmid": vmid,
            "vm_name": vote["vm_name"], "vm_list": vote["vm_list"],
        })


@socketio.on("vm_shutdown_initiate", namespace="/state")
@_shutdown_event("vm_shutdown.initiate")
def handle_vm_shutdown_initiate(data, actor):
    _initiate_shutdown(data, actor, "single")


@socketio.on("vm_shutdown_cluster_initiate", namespace="/state")
@_shutdown_event("vm_shutdown.cluster_initiate")
def handle_vm_shutdown_cluster_initiate(data, actor):
    _initiate_shutdown(data, actor, "cluster")


@socketio.on("vm_shutdown_vote", namespace="/state")
@_shutdown_event("vm_shutdown.vote")
def handle_vm_shutdown_vote(data, actor):
    vote_id = data.get("vote_id")
    with _vm_shutdown_lock:
        vote = _vm_shutdown_votes.get(vote_id)
    if not vote:
        raise AuthorizationDenied("投票不存在")
    g.shutdown_resource = vote
    actor, _cluster = _authorize_shutdown(
        actor.id, vote["cluster_name"], vote.get("node"), vote.get("vmid"), expected=vote["identity"],
    )
    if actor.role != "student" or actor.id not in vote["voters"]:
        raise AuthorizationDenied("无权投票")
    if type(data.get("agree")) is not bool:
        raise ValueError("投票参数无效")
    with _vm_shutdown_lock:
        if vote["completed"] or vote["voters"][actor.id]["voted"]:
            raise ValueError("投票已结束或已投票")
        vote["voters"][actor.id].update(voted=True, agree=data["agree"])
        all_voted = all(info["voted"] for info in vote["voters"].values())
        if all_voted:
            _emit_vote_result(vote_id, vote, all(info["agree"] for info in vote["voters"].values()))
    emit("vm_shutdown_voted", {"vote_id": vote_id, "agree": data["agree"]})
    if all_voted:
        _complete_vote(vote)


@socketio.on("vm_shutdown_cancel", namespace="/state")
@_shutdown_event("vm_shutdown.cancel")
def handle_vm_shutdown_cancel(data, actor):
    with _vm_shutdown_lock:
        vote = _vm_shutdown_votes.get(data.get("vote_id"))
    if not vote:
        raise AuthorizationDenied("投票不存在")
    g.shutdown_resource = vote
    if vote["initiator"]["id"] != actor.id:
        raise AuthorizationDenied("只有发起人可以取消")
    _authorize_shutdown(actor.id, vote["cluster_name"], vote.get("node"), vote.get("vmid"), expected=vote["identity"])
    with _vm_shutdown_lock:
        if vote["completed"]:
            raise ValueError("投票已结束")
        _emit_vote_result(vote["id"], vote, False)
    _emit_vote_event(vote, "vm_shutdown_cancelled", {
        "vote_id": vote["id"], "reason": "发起人已取消关机",
    })
    _cleanup_stale_votes()


def _emit_task_update(task_id, status, progress, message, created_by, queue):
    data = {
        "task_id": task_id,
        "status": status,
        "progress": progress,
        "message": _sanitize_text(message),
        "queue": queue,
    }
    task = get_task_status(task_id)
    if task:
        _emit_state("task_update", data, lambda user: _is_allowed_for_user(user, Actions.TASK_READ, task))


set_on_task_update(_emit_task_update)
ssh_manager.set_on_session_terminated(lambda cluster_name, user_id: _emit_session_update(cluster_name, user_id, "terminated"))
ssh_manager.set_on_owner_disconnect(lambda cluster_name, user_id: _emit_session_update(cluster_name, user_id, "disconnected"))


def _emit_session_update(cluster_name, user_id, action, extra=None):
    data = {"cluster_name": cluster_name, "user_id": user_id, "action": action}
    if extra:
        data.update(extra)
    owner_user = _fresh_user(user_id)
    cluster = get_cluster(cluster_name)
    if not owner_user or not cluster or not _is_allowed_for_user(owner_user, Actions.WEBSSH_CONNECT, cluster):
        return
    resource = {"owner_user_id": user_id, "owner_teacher_id": getattr(owner_user, "created_by", None)}
    _emit_state("session_update", data, lambda user: (
        (user.id == user_id and _is_allowed_for_user(user, Actions.WEBSSH_CONNECT, cluster))
        or _is_allowed_for_user(user, Actions.WEBSSH_OBSERVE, resource)
    ))


def _on_takeover_released(session):
    ssh_manager.emit_to(session, "takeover_released", {}, session.owner_sid)
    _emit_session_update(session.cluster_name, session.owner["user_id"], "takeover_released")


ssh_manager.set_on_takeover_released(_on_takeover_released)


# ── WebSSH REST API ──

@app.route("/admin/webssh")
@login_required
@admin_required
def admin_webssh():
    return render_template("admin_webssh.html")

@app.route("/api/webssh/sessions", methods=["GET"])
@login_required
def api_webssh_sessions():
    if current_user.role == "admin":
        sessions = ssh_manager.list_sessions()
    elif current_user.role == "teacher":
        sessions = [
            session
            for session in ssh_manager.list_sessions(filter_role="student")
            if _is_allowed(Actions.WEBSSH_OBSERVE, _session_authz_resource(session))
        ]
    else:
        sessions = ssh_manager.list_sessions(filter_user_id=current_user.id)
    return jsonify({"sessions": sessions})

@app.route("/api/webssh/my-sessions", methods=["GET"])
@login_required
def api_webssh_my_sessions():
    sessions = ssh_manager.list_sessions(filter_user_id=current_user.id)
    return jsonify({"sessions": sessions})

@app.route("/api/webssh/cluster/<name>", methods=["GET"])
@login_required
def api_webssh_cluster_status(name):
    if not _check_cluster_access(name):
        return jsonify({"error": "无权访问"}), 403
    session = ssh_manager.get_session_by_cluster(current_user.id, name)
    if session:
        return jsonify({"has_session": True, "session": session.to_dict()})
    return jsonify({"has_session": False})

@app.route("/api/teacher/student-sessions", methods=["GET"])
@login_required
def api_teacher_student_sessions():
    if current_user.role == "student":
        return _forbidden()
    sessions = ssh_manager.list_sessions(filter_role="student")
    if current_user.role == "teacher":
        sessions = [
            session
            for session in sessions
            if _is_allowed(Actions.WEBSSH_OBSERVE, _session_authz_resource(session))
        ]
    return jsonify({"sessions": sessions})

@app.route("/api/admin/webssh/config", methods=["GET"])
@login_required
@admin_required
def api_webssh_config_get():
    return jsonify(ssh_manager.get_config())

@app.route("/api/admin/webssh/config", methods=["PUT"])
@login_required
@admin_required
def api_webssh_config_set():
    data = request.get_json() or {}
    ssh_manager.set_config(data)
    return jsonify({"message": "配置已保存"})

@app.route("/api/admin/webssh/sessions/<session_id>", methods=["DELETE"])
@login_required
@admin_required
def api_admin_webssh_terminate(session_id):
    session = ssh_manager.get_session(session_id)
    ok = ssh_manager.terminate_session(session_id)
    if ok:
        if session:
            _emit_session_update(session.cluster_name, session.owner["user_id"], "terminated")
        return jsonify({"message": "会话已终止"})
    return jsonify({"error": "会话不存在"}), 404

@app.route("/api/webssh/sessions/<cluster_name>/terminate", methods=["POST"])
@login_required
def api_webssh_terminate_session(cluster_name):
    session = ssh_manager.get_session_by_cluster(current_user.id, cluster_name)
    if not session:
        return jsonify({"error": "会话不存在"}), 404
    if not _is_allowed(
        Actions.WEBSSH_TERMINATE_OWN,
        _session_authz_resource(session),
    ):
        return _forbidden("无权终止")
    targets = list(session._get_all_targets())
    ssh_manager.terminate_by_owner(current_user.id, cluster_name)
    for sid in targets:
        socketio.emit("session_terminated", {"cluster": cluster_name}, to=sid, namespace="/webssh")
    _emit_session_update(cluster_name, current_user.id, "terminated")
    return jsonify({"message": "会话已终止"})


if __name__ == "__main__":
    socketio.run(app, debug=True, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
