import os
from functools import wraps

from flask import Flask, render_template, request, jsonify, session, redirect, g, make_response
from werkzeug.security import check_password_hash, generate_password_hash

from modules.db import (
    add_group_member, check_user_in_class_group, count_classes, count_groups, count_users,
    create_class, create_group, create_pve_server, create_user, delete_class, delete_group,
    delete_pve_server, delete_user, find_cluster_by_vm, get_class, get_class_by_name, get_classes_for_student,
    get_config, get_db_config, get_db_status, get_group, get_pve_server,
    get_student_group_ids, get_students_created_by, get_user, get_user_by_username,
    get_user_cluster_ids, get_user_groups, init_db, is_db_configured, list_classes,
    list_group_members, list_group_members_batch, list_groups, list_groups_batch, list_pve_servers, list_users,
    get_or_create_group, migrate_config_from_json,
    migrate_from_json, reload_db_engine, remove_group_member, save_cluster,
    set_config, set_db_config, update_class, update_pve_server, update_user,
)
from modules.pve_client import PVEClient, PVEError
from modules.openwrt_client import OpenWrtClient, OpenWrtError
from modules.k8s_manager import create_cluster, create_cluster_async, deploy_k8s_async, delete_cluster_async, batch_create_clusters_async, list_clusters, get_cluster, delete_cluster, get_task_status, list_tasks, cancel_task, K8sError, force_delete_cluster
from modules.pg_client import PGClient, PGError

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-in-production")

_base_dir = os.path.dirname(os.path.abspath(__file__))
if is_db_configured():
    init_db()
    migrate_from_json(os.path.join(_base_dir, ".k8s_clusters.json"))
    migrate_config_from_json("pve", os.path.join(_base_dir, ".pve_config.json"))
    migrate_config_from_json("openwrt", os.path.join(_base_dir, ".openwrt_config.json"))


@app.context_processor
def inject_globals():
    return dict(get_config=get_config)


# ── Auth helpers ──

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "未登录，请先登录"}), 401
            return redirect("/login")
        g.user = get_user(session["user_id"])
        if not g.user:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "用户不存在"}), 401
            return redirect("/login")
        if not g.user.get("is_active"):
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify({"error": "用户已被禁用"}), 403
            return redirect("/login")
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if g.user.get("role") not in roles:
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


@app.before_request
def before_request():
    if request.path.startswith("/static"):
        return
    db_setup_paths = ("/db-config", "/api/db-config")
    if not is_db_configured() and request.path not in db_setup_paths:
        return redirect("/db-config")
    if request.path in db_setup_paths:
        return
    if _needs_setup() and request.path not in ("/setup", "/api/setup"):
        return redirect("/setup")
    if request.path in ("/login", "/api/login", "/setup", "/api/setup"):
        return
    if "user_id" not in session:
        if request.path.startswith("/api/"):
            return jsonify({"error": "未登录"}), 401
        return redirect("/login")
    g.user = get_user(session["user_id"])
    if not g.user or not g.user.get("is_active"):
        session.clear()
        return redirect("/login")


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
    session["user_id"] = uid
    return jsonify({"message": "初始化完成", "user_id": uid})


@app.route("/login")
def login_page():
    if "user_id" in session:
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
    if not user.get("is_active"):
        return jsonify({"error": "账号已被禁用"}), 403
    if not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "用户名或密码错误"}), 401
    if user["role"] == "student":
        groups = get_user_groups(user["id"])
        if not groups:
            return jsonify({"error": "您尚未被分配到课程组，请联系教师"}), 403
    session["user_id"] = user["id"]
    return jsonify({"message": "登录成功", "user": {
        "id": user["id"], "username": user["username"], "role": user["role"],
    }})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
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
        return jsonify({"error": f"连接失败: {e}"}), 400
    except Exception as e:
        return jsonify({"error": f"连接失败: {e}"}), 400

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
    u = g.user
    result = {"id": u["id"], "username": u["username"], "name": u.get("name", ""), "role": u["role"]}
    if u["role"] == "student":
        result["classes"] = get_classes_for_student(u["id"])
    elif u["role"] == "teacher":
        result["classes"] = list_classes(created_by=u["id"])
    return jsonify(result)


@app.route("/api/users", methods=["GET"])
@login_required
def api_list_users():
    role_filter = request.args.get("role")
    if g.user["role"] == "admin":
        users = list_users(role=role_filter)
    elif g.user["role"] == "teacher":
        users = list_users(role="student")
    else:
        return jsonify({"error": "权限不足"}), 403
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
    if g.user["role"] == "teacher" and role != "student":
        return jsonify({"error": "教师只能创建学生用户"}), 403
    if g.user["role"] != "admin" and role in ("admin", "teacher"):
        return jsonify({"error": "权限不足"}), 403
    existing = get_user_by_username(username)
    if existing:
        return jsonify({"error": "用户名已存在"}), 400
    uid = create_user({
        "username": username,
        "password_hash": generate_password_hash(password),
        "role": role,
        "name": name,
        "created_by": g.user["id"],
    })
    return jsonify({"id": uid, "message": "用户创建成功"}), 201


@app.route("/api/users/template", methods=["GET"])
@login_required
@teacher_or_admin_required
def api_users_template():
    import csv, io
    output = io.StringIO()
    writer = csv.writer(output)
    if g.user["role"] == "admin":
        writer.writerow(["username", "password", "name", "role"])
        writer.writerow(["zhangsan", "123456", "张三", "student"])
    else:
        writer.writerow(["username", "password", "name"])
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
    required_cols_teacher = {"username", "password", "name"}
    required_cols_admin = {"username", "password", "name", "role"}
    if g.user["role"] == "admin":
        if not reader.fieldnames or not required_cols_admin.issubset(reader.fieldnames):
            return jsonify({"error": "CSV 格式错误，需要列: username, password, name, role"}), 400
    else:
        if not reader.fieldnames or not required_cols_teacher.issubset(reader.fieldnames):
            return jsonify({"error": "CSV 格式错误，需要列: username, password, name"}), 400

    result = {"created": 0, "skipped": 0, "errors": []}
    for row_num, row in enumerate(reader, start=2):
        username = (row.get("username") or "").strip()
        password = (row.get("password") or "").strip()
        name = (row.get("name") or "").strip()
        role = (row.get("role") or "student").strip()

        if not username or not password:
            result["errors"].append(f"第 {row_num} 行: username 和 password 不能为空")
            continue
        if g.user["role"] == "teacher":
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
                "created_by": g.user["id"],
            })
            result["created"] += 1
        except Exception as e:
            result["errors"].append(f"第 {row_num} 行: {str(e)}")
    return jsonify(result)


@app.route("/api/users/<int:uid>", methods=["PUT"])
@login_required
def api_update_user(uid):
    data = request.get_json() or {}
    target = get_user(uid)
    if not target:
        return jsonify({"error": "用户不存在"}), 404
    if g.user["role"] == "teacher":
        if target.get("created_by") != g.user["id"]:
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
    if g.user["role"] == "teacher":
        if target.get("created_by") != g.user["id"]:
            return jsonify({"error": "只能删除自己创建的学生"}), 403
    delete_user(uid)
    return jsonify({"message": "用户已删除"})


# ── Class API ──

@app.route("/api/classes", methods=["GET"])
@login_required
def api_list_classes():
    if g.user["role"] == "admin":
        classes = list_classes()
    elif g.user["role"] == "teacher":
        classes = list_classes(created_by=g.user["id"])
    else:
        classes = get_classes_for_student(g.user["id"])
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
        "created_by": g.user["id"],
    })
    return jsonify({"id": cid, "message": "课程创建成功"}), 201


@app.route("/api/classes/<int:cid>", methods=["GET"])
@login_required
def api_get_class(cid):
    cls = get_class(cid)
    if not cls:
        return jsonify({"error": "课程不存在"}), 404
    if g.user["role"] == "teacher" and cls.get("created_by") != g.user["id"]:
        return jsonify({"error": "权限不足"}), 403
    if g.user["role"] == "student":
        ok = check_user_in_class_group(g.user["id"], cid)
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
    if g.user["role"] == "teacher" and cls.get("created_by") != g.user["id"]:
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
    if g.user["role"] == "teacher" and cls.get("created_by") != g.user["id"]:
        return jsonify({"error": "只能删除自己创建的课程"}), 403
    delete_class(cid)
    return jsonify({"message": "课程已删除"})


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
        created_by_filter = g.user["id"] if g.user["role"] == "teacher" else None
        class_obj = get_class_by_name(class_name, created_by=created_by_filter)
        if not class_obj:
            result["errors"].append(f"第 {row_num} 行: 课程 '{class_name}' 不存在")
            continue
        user_obj = get_user_by_username(username)
        if not user_obj:
            result["errors"].append(f"第 {row_num} 行: 用户 '{username}' 不存在")
            continue
        try:
            group_id = get_or_create_group(class_obj["id"], group_name, g.user["id"])
            add_group_member(group_id, user_obj["id"])
            result["created"] += 1
        except ValueError as e:
            result["skipped"] += 1
        except Exception as e:
            result["errors"].append(f"第 {row_num} 行: {str(e)}")
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

    if g.user["role"] == "student":
        groups = get_user_groups(g.user["id"])
        result = {}
        for grp in groups:
            cid = str(grp["class_id"])
            result.setdefault(cid, []).append({
                "id": grp["group_id"],
                "name": grp["group_name"],
                "class_id": grp["class_id"],
            })
        return jsonify(result)

    if g.user["role"] == "teacher":
        teacher_classes = list_classes(created_by=g.user["id"])
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
    if g.user["role"] == "teacher" and cls.get("created_by") != g.user["id"]:
        return jsonify({"error": "权限不足"}), 403
    if g.user["role"] == "student":
        groups = get_user_groups(g.user["id"])
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
    if g.user["role"] == "teacher" and cls.get("created_by") != g.user["id"]:
        return jsonify({"error": "只能在自己创建的课程中创建组"}), 403
    gid = create_group({
        "name": name,
        "class_id": class_id,
        "created_by": g.user["id"],
    })
    return jsonify({"id": gid, "message": "组创建成功"}), 201


@app.route("/api/groups/<int:gid>", methods=["DELETE"])
@login_required
@teacher_or_admin_required
def api_delete_group(gid):
    grp = get_group(gid)
    if not grp:
        return jsonify({"error": "组不存在"}), 404
    if g.user["role"] == "teacher" and grp.get("created_by") != g.user["id"]:
        return jsonify({"error": "只能删除自己创建的组"}), 403
    delete_group(gid)
    return jsonify({"message": "组已删除"})


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
    if target["role"] != "student":
        return jsonify({"error": "只能将学生加入组"}), 400
    try:
        add_group_member(gid, user_id)
        return jsonify({"message": "已加入组"}), 201
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


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


def _check_vm_access(node, vmid):
    cluster = find_cluster_by_vm(node, vmid)
    if cluster:
        g._vm_cluster = cluster
    if g.user["role"] == "admin":
        return True
    if not cluster:
        return False
    if g.user["role"] == "teacher":
        return cluster.get("created_by") == g.user["id"]
    if g.user["role"] == "student":
        student_group_ids = get_student_group_ids(g.user["id"])
        return cluster.get("group_id") in student_group_ids
    return False


def api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except PVEError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
    return wrapper


@app.route("/")
@login_required
def index():
    if g.user["role"] == "student":
        return redirect("/k8s")
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
    safe = {k: v for k, v in cfg.items() if k != "token_value"}
    return jsonify(safe)


@app.route("/api/pve/servers", methods=["GET"])
@login_required
@api_error_handler
def pve_list_servers():
    return jsonify(list_pve_servers())


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
            result["vms_by_node"][node_name] = f"Error: {e}"
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
    client = get_pve_client(getattr(g, "_vm_cluster", {}).get("pve_server_id"))
    return jsonify(client.get_vm_status(node, vmid))


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
    if not _check_vm_access(node, vmid):
        return jsonify({"error": "无权访问该虚拟机"}), 403
    client = get_pve_client(getattr(g, "_vm_cluster", {}).get("pve_server_id"))
    result = client.start_vm(node, vmid)
    return jsonify(result)


@app.route("/api/pve/vms/<node>/<int:vmid>/stop", methods=["POST"])
@login_required
@api_error_handler
def pve_stop_vm(node, vmid):
    if not _check_vm_access(node, vmid):
        return jsonify({"error": "无权访问该虚拟机"}), 403
    data = request.get_json() or {}
    force = data.get("force", False)
    client = get_pve_client(getattr(g, "_vm_cluster", {}).get("pve_server_id"))
    result = client.stop_vm(node, vmid, force)
    return jsonify(result)


@app.route("/api/pve/vms/<node>/<int:vmid>", methods=["DELETE"])
@login_required
@api_error_handler
def pve_release_vm(node, vmid):
    if not _check_vm_access(node, vmid):
        return jsonify({"error": "无权访问该虚拟机"}), 403
    data = request.get_json() or {}
    purge = data.get("purge", True)
    client = get_pve_client(getattr(g, "_vm_cluster", {}).get("pve_server_id"))
    result = client.release_vm(node, vmid, purge)
    return jsonify(result)


@app.route("/openwrt")
@login_required
@admin_required
def openwrt():
    cfg = get_config("openwrt") or {}
    return render_template("openwrt.html", config=cfg)


def get_openwrt_client():
    cfg = get_config("openwrt")
    if not cfg:
        raise OpenWrtError("OpenWrt 未配置，请先在页面中保存配置")
    missing = [k for k in ("host", "username", "password") if not cfg.get(k)]
    if missing:
        raise OpenWrtError(f"OpenWrt 配置不完整: {', '.join(missing)}")
    return OpenWrtClient(
        host=cfg["host"],
        username=cfg["username"],
        password=cfg["password"],
        port=int(cfg.get("port", 22)),
    )


def openwrt_api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except OpenWrtError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
    return wrapper


@app.route("/api/openwrt/config", methods=["GET", "POST"])
@login_required
@admin_required
@openwrt_api_error_handler
def openwrt_config():
    if request.method == "POST":
        data = request.get_json() or {}
        if not data.get("host") or not data.get("username") or not data.get("password"):
            return jsonify({"error": "host、username、password 均为必填项"}), 400
        set_config("openwrt", data)
        return jsonify({"message": "配置已保存"})
    cfg = get_config("openwrt") or {}
    safe = {k: v for k, v in cfg.items() if k != "password"}
    return jsonify(safe)


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
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
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



def _check_cluster_access(name):
    """Check if current user can access the named cluster."""
    if g.user["role"] == "admin":
        return True
    cluster = get_cluster(name)
    if not cluster:
        return False
    if g.user["role"] == "teacher":
        return cluster.get("created_by") == g.user["id"]
    if g.user["role"] == "student":
        student_group_ids = get_student_group_ids(g.user["id"])
        return cluster.get("group_id") in student_group_ids
    return False


def _filter_clusters(clusters_dict):
    """Filter clusters dict based on user role."""
    if g.user["role"] == "admin":
        return clusters_dict
    if g.user["role"] == "teacher":
        uid = g.user["id"]
        return {k: v for k, v in clusters_dict.items() if v.get("created_by") == uid}
    if g.user["role"] == "student":
        group_ids = get_student_group_ids(g.user["id"])
        return {k: v for k, v in clusters_dict.items() if v.get("group_id") in group_ids}
    return {}


def k8s_api_error_handler(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except (K8sError, PVEError, OpenWrtError) as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Unexpected error: {e}"}), 500
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
        safe[name] = entry
    return jsonify(safe)


@app.route("/api/k8s/clusters", methods=["POST"])
@login_required
@teacher_or_admin_required
@k8s_api_error_handler
def k8s_create_cluster():
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
        password=password,
        pve_server_id=pve_server_id,
    )
    save_cluster(name, {**cluster, "created_by": g.user["id"]})
    safe = {k: v for k, v in cluster.items() if k != "ssh_private_key"}
    return jsonify({"name": name, "cluster": safe}), 201


@app.route("/api/k8s/clusters/<name>", methods=["DELETE"])
@login_required
@k8s_api_error_handler
def k8s_delete_cluster(name):
    if not _check_cluster_access(name):
        return jsonify({"error": "无权操作该集群"}), 403
    task_id = delete_cluster_async(name, created_by=g.user["id"])
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/clusters/<name>/force", methods=["DELETE"])
@login_required
@admin_required
def k8s_force_delete_cluster(name):
    force_delete_cluster(name)
    return jsonify({"message": "集群已强制删除"})


@app.route("/api/k8s/create", methods=["POST"])
@login_required
@teacher_or_admin_required
@k8s_api_error_handler
def k8s_create_cluster_async_route():
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
    group_id = data.get("group_id")
    class_id = data.get("class_id")

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
        password=password,
        pve_server_id=pve_server_id,
        group_id=group_id,
        class_id=class_id,
        created_by=g.user["id"],
    )
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/batch-create", methods=["POST"])
@login_required
@teacher_or_admin_required
@k8s_api_error_handler
def k8s_batch_create_clusters():
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
    password = data.get("password", "k8s.1234")
    pve_server_id = int(data.get("pve_server_id", 0))
    class_id = data.get("class_id")

    task_ids = batch_create_clusters_async(
        group_ids, master_count, node_count,
        master_cores, master_memory,
        node_cores, node_memory,
        pve_node,
        password=password,
        pve_server_id=pve_server_id,
        created_by=g.user["id"],
        class_id=class_id,
    )
    return jsonify({"task_ids": task_ids, "count": len(task_ids)}), 202


@app.route("/api/k8s/tasks", methods=["GET"])
@login_required
@k8s_api_error_handler
def k8s_list_tasks():
    created_by = None if g.user["role"] == "admin" else g.user["id"]
    return jsonify({"tasks": list_tasks(created_by=created_by)})


@app.route("/api/k8s/tasks/<task_id>", methods=["GET"])
@login_required
@k8s_api_error_handler
def k8s_get_task(task_id):
    status = get_task_status(task_id)
    if not status:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(status)


@app.route("/api/k8s/tasks/<task_id>/cancel", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_cancel_task(task_id):
    ok = cancel_task(task_id)
    if not ok:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify({"message": "取消请求已发送"})


@app.route("/api/k8s/clusters/<name>/ssh-key", methods=["GET"])
@login_required
@k8s_api_error_handler
def k8s_get_ssh_key(name):
    if not _check_cluster_access(name):
        return jsonify({"error": "无权访问该集群"}), 403
    cluster = get_cluster(name)
    if not cluster:
        return jsonify({"error": "Cluster not found"}), 404
    return jsonify({
        "private_key": cluster.get("ssh_private_key", ""),
        "public_key": cluster.get("ssh_public_key", ""),
    })


@app.route("/api/k8s/clusters/<name>/deploy", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_deploy_cluster(name):
    if not _check_cluster_access(name):
        return jsonify({"error": "无权操作该集群"}), 403
    cluster = get_cluster(name)
    if not cluster:
        return jsonify({"error": "集群不存在"}), 404
    if cluster.get("status") != "running":
        return jsonify({"error": "集群状态异常，无法部署 K8s"}), 400
    task_id = deploy_k8s_async(name, created_by=g.user["id"])
    return jsonify({"task_id": task_id}), 202


@app.route("/api/k8s/clusters/<name>/vm-action-all", methods=["POST"])
@login_required
@k8s_api_error_handler
def k8s_cluster_vm_action_all(name):
    if not _check_cluster_access(name):
        return jsonify({"error": "无权操作该集群"}), 403
    cluster = get_cluster(name)
    if not cluster:
        return jsonify({"error": "集群不存在"}), 404
    if g.user["role"] == "student":
        return jsonify({"error": "无权操作"}), 403
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
            else:
                client.stop_vm(vm_info["node"], vm_info["vmid"])
            results.append({"vm": vm_name, "status": "ok"})
        except Exception as e:
            results.append({"vm": vm_name, "status": "failed", "error": str(e)})
    ok = sum(1 for r in results if r["status"] == "ok")
    return jsonify({"results": results, "message": f"{ok}/{len(results)}"})


@app.route("/api/k8s/clusters/<name>/upload-ssh-key", methods=["POST"])


@app.route("/k8s/logs/<task_id>")
@login_required
@k8s_api_error_handler
def k8s_logs_page(task_id):
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
    safe = {k: v for k, v in cfg.items() if k != "password"}
    return jsonify({"config": safe})


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
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        client.close()


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
