import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, func,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker, selectinload

DB_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".db_config.json")


def _load_db_config():
    if os.path.exists(DB_CONFIG_PATH):
        try:
            with open(DB_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_db_config(cfg):
    with open(DB_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _create_engine():
    cfg = _load_db_config()
    if not cfg or cfg.get("type") != "postgresql":
        return None
    missing = [k for k in ("host", "user", "password", "database") if not cfg.get(k)]
    if missing:
        return None
    url = (f"postgresql+pg8000://{cfg['user']}:{cfg['password']}@"
           f"{cfg['host']}:{cfg.get('port', 5432)}/{cfg['database']}")
    try:
        return create_engine(
            url, echo=False,
            pool_size=10, max_overflow=20,
            pool_pre_ping=True, pool_recycle=3600,
        )
    except Exception:
        return None


_engine_lock = threading.Lock()

engine = _create_engine()
SessionLocal = sessionmaker(bind=engine) if engine else None
Base = declarative_base()


class Cluster(Base):
    __tablename__ = "clusters"
    id = Column(Integer, primary_key=True)
    name = Column(String(32), unique=True, nullable=False)
    status = Column(String(16), default="creating")
    vlan_id = Column(Integer)
    vlan_device = Column(String(32))
    interface = Column(String(32))
    gateway = Column(String(16))
    netmask = Column(String(16))
    dnsmasq = Column(String(32))
    ssh_private_key = Column(Text)
    ssh_public_key = Column(Text)
    pve_node = Column(String(32))
    template_vmid = Column(Integer)
    pve_server_id = Column(Integer, default=0)
    ssh_port = Column(Integer)
    client_mac = Column(String(24))
    k8s_status = Column(String(16), default="pending")
    password = Column(String(64), default="k8s.1234")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())

    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True, index=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=True, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)

    vms = relationship("Vm", back_populates="cluster", cascade="all, delete-orphan")
    group_ref = relationship("Group", back_populates="clusters")


class Vm(Base):
    __tablename__ = "vms"
    id = Column(Integer, primary_key=True)
    cluster_id = Column(Integer, ForeignKey("clusters.id"), nullable=False, index=True)
    vm_name = Column(String(64), nullable=False)
    vmid = Column(Integer, unique=True, nullable=False)
    node = Column(String(32), nullable=False, index=True)
    role = Column(String(16))
    mac = Column(String(24))
    ip = Column(String(16))

    cluster = relationship("Cluster", back_populates="vms")


class Config(Base):
    __tablename__ = "config"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False)


class PVEServer(Base):
    __tablename__ = "pve_servers"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), unique=True, nullable=False)
    host = Column(String(128), nullable=False)
    port = Column(Integer, default=8006)
    user = Column(String(64), nullable=False)
    token_name = Column(String(64), nullable=False)
    token_value = Column(String(256), nullable=False)
    node = Column(String(64), default="")
    created_at = Column(DateTime, default=func.now())
    updated_at = Column(DateTime, default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(16), nullable=False, default="student")
    name = Column(String(128), default="")
    is_active = Column(Boolean, default=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=func.now())

    group_memberships = relationship("GroupMember", back_populates="user", cascade="all, delete-orphan")


class SchoolClass(Base):
    __tablename__ = "classes"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    description = Column(Text, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now())

    groups = relationship("Group", back_populates="class_ref", cascade="all, delete-orphan")


class Group(Base):
    __tablename__ = "groups"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=func.now())

    class_ref = relationship("SchoolClass", back_populates="groups")
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")
    clusters = relationship("Cluster", back_populates="group_ref")


class GroupMember(Base):
    __tablename__ = "group_members"
    id = Column(Integer, primary_key=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False, index=True)
    __table_args__ = (
        UniqueConstraint("user_id", "class_id", name="uq_user_class_group"),
    )

    user = relationship("User", back_populates="group_memberships")
    group = relationship("Group", back_populates="members")


def get_config(key):
    with session_scope() as session:
        row = session.query(Config).filter_by(key=key).first()
        return json.loads(row.value) if row else None


def set_config(key, value):
    with session_scope(commit=True) as session:
        row = session.query(Config).filter_by(key=key).first()
        val = json.dumps(value, ensure_ascii=False)
        if row:
            row.value = val
        else:
            session.add(Config(key=key, value=val))


def delete_config(key):
    with session_scope(commit=True) as session:
        session.query(Config).filter_by(key=key).delete()


def migrate_pve_config():
    with session_scope(commit=True) as session:
        existing = session.query(PVEServer).first()
        if existing:
            return
        cfg = get_config("pve")
        if not cfg:
            return
        server = PVEServer(
            name="default",
            host=cfg.get("host", ""),
            port=int(cfg.get("port", 8006)),
            user=cfg.get("user", ""),
            token_name=cfg.get("token_name", ""),
            token_value=cfg.get("token_value", ""),
            node=cfg.get("node", ""),
        )
        session.add(server)
        delete_config("pve")


def list_pve_servers():
    with session_scope() as session:
        servers = session.query(PVEServer).all()
        return [{
            "id": s.id,
            "name": s.name,
            "host": s.host,
            "port": s.port,
            "user": s.user,
            "token_name": s.token_name,
            "token_value": "****",
            "node": s.node,
        } for s in servers]


def get_pve_server(server_id):
    with session_scope() as session:
        s = session.query(PVEServer).filter_by(id=server_id).first()
        if not s:
            return None
        return {
            "id": s.id,
            "name": s.name,
            "host": s.host,
            "port": s.port,
            "user": s.user,
            "token_name": s.token_name,
            "token_value": s.token_value,
            "node": s.node,
        }


def create_pve_server(data):
    with session_scope(commit=True) as session:
        s = PVEServer(
            name=data["name"],
            host=data["host"],
            port=int(data.get("port", 8006)),
            user=data["user"],
            token_name=data["token_name"],
            token_value=data["token_value"],
            node=data.get("node", ""),
        )
        session.add(s)
        return s.id


def update_pve_server(server_id, data):
    with session_scope(commit=True) as session:
        s = session.query(PVEServer).filter_by(id=server_id).first()
        if not s:
            return None
        if "name" in data: s.name = data["name"]
        if "host" in data: s.host = data["host"]
        if "port" in data: s.port = int(data["port"])
        if "user" in data: s.user = data["user"]
        if "token_name" in data: s.token_name = data["token_name"]
        if "token_value" in data and data["token_value"] and data["token_value"] != "****":
            s.token_value = data["token_value"]
        if "node" in data: s.node = data["node"]
        return s.id


def delete_pve_server(server_id):
    with session_scope(commit=True) as session:
        s = session.query(PVEServer).filter_by(id=server_id).first()
        if s:
            session.delete(s)


def init_db():
    with _engine_lock:
        if engine is None:
            raise RuntimeError("数据库未配置")
    Base.metadata.create_all(engine)
    _ensure_db_indexes()
    _migrate_user_name()
    migrate_pve_config()


@contextmanager
def session_scope(commit=False):
    session = get_session()
    try:
        yield session
        if commit:
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session():
    with _engine_lock:
        if SessionLocal is None:
            raise RuntimeError("数据库未配置")
        return SessionLocal()


def _cluster_to_dict(cluster):
    return {
        "status": cluster.status,
        "k8s_status": cluster.k8s_status,
        "vlan_id": cluster.vlan_id,
        "vlan_device": cluster.vlan_device,
        "interface": cluster.interface,
        "gateway": cluster.gateway,
        "netmask": cluster.netmask,
        "dnsmasq": cluster.dnsmasq,
        "ssh_private_key": cluster.ssh_private_key,
        "ssh_public_key": cluster.ssh_public_key,
        "password": cluster.password,
        "pve_node": cluster.pve_node,
        "template_vmid": cluster.template_vmid,
        "pve_server_id": cluster.pve_server_id,
        "ssh_port": cluster.ssh_port,
        "client_mac": cluster.client_mac,
        "group_id": cluster.group_id,
        "class_id": cluster.class_id,
        "created_by": cluster.created_by,
        "vms": {vm.vm_name: {"node": vm.node, "vmid": vm.vmid, "role": vm.role, "mac": vm.mac, "ip": vm.ip}
                for vm in cluster.vms},
    }


def _ensure_db_indexes():
    if engine is None:
        return
    indexes = [
        "CREATE INDEX IF NOT EXISTS ix_clusters_group_id ON clusters (group_id)",
        "CREATE INDEX IF NOT EXISTS ix_clusters_created_by ON clusters (created_by)",
        "CREATE INDEX IF NOT EXISTS ix_clusters_pve_server_id ON clusters (pve_server_id)",
        "CREATE INDEX IF NOT EXISTS ix_vms_cluster_id ON vms (cluster_id)",
        "CREATE INDEX IF NOT EXISTS ix_vms_node ON vms (node)",
        "CREATE INDEX IF NOT EXISTS ix_group_members_user_id ON group_members (user_id)",
        "CREATE INDEX IF NOT EXISTS ix_group_members_group_id ON group_members (group_id)",
        "CREATE INDEX IF NOT EXISTS ix_group_members_class_id ON group_members (class_id)",
    ]
    with engine.connect() as conn:
        for stmt in indexes:
            try:
                conn.execute(text(stmt))
            except Exception:
                pass
        conn.commit()


def _migrate_user_name():
    if engine is None:
        return
    try:
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN name VARCHAR(128) DEFAULT ''"))
            conn.commit()
    except Exception:
        pass


def load_clusters():
    with session_scope() as session:
        clusters = session.query(Cluster).options(selectinload(Cluster.vms)).all()
        class_ids = list(set(c.class_id for c in clusters if c.class_id))
        class_map = {}
        if class_ids:
            for cls in session.query(SchoolClass).filter(SchoolClass.id.in_(class_ids)).all():
                class_map[cls.id] = cls.name
        result = {}
        for c in clusters:
            d = _cluster_to_dict(c)
            d["class_name"] = class_map.get(c.class_id, "")
            result[c.name] = d
        return result


def load_cluster(name):
    with session_scope() as session:
        cluster = session.query(Cluster).filter_by(name=name).first()
        if cluster is None:
            return None
        return _cluster_to_dict(cluster)


def save_cluster(name, cluster_data):
    with session_scope(commit=True) as session:
        cluster = session.query(Cluster).filter_by(name=name).first()
        if cluster is None:
            cluster = Cluster(name=name)
            session.add(cluster)
        cluster.status = cluster_data.get("status", cluster.status or "running")
        cluster.vlan_id = cluster_data.get("vlan_id")
        cluster.vlan_device = cluster_data.get("vlan_device")
        cluster.interface = cluster_data.get("interface")
        cluster.gateway = cluster_data.get("gateway")
        cluster.netmask = cluster_data.get("netmask")
        cluster.dnsmasq = cluster_data.get("dnsmasq")
        cluster.ssh_private_key = cluster_data.get("ssh_private_key")
        cluster.ssh_public_key = cluster_data.get("ssh_public_key")
        cluster.pve_node = cluster_data.get("pve_node")
        cluster.template_vmid = cluster_data.get("template_vmid")
        cluster.pve_server_id = cluster_data.get("pve_server_id", cluster.pve_server_id or 0)
        cluster.ssh_port = cluster_data.get("ssh_port")
        cluster.password = cluster_data.get("password", cluster.password or "k8s.1234")
        cluster.client_mac = cluster_data.get("client_mac")
        cluster.k8s_status = cluster_data.get("k8s_status", cluster.k8s_status or "pending")
        cluster.group_id = cluster_data.get("group_id")
        cluster.class_id = cluster_data.get("class_id")
        cluster.created_by = cluster_data.get("created_by")
        cluster.updated_at = func.now()

        vms = cluster_data.get("vms", {})
        existing = {v.vm_name: v for v in cluster.vms}
        for vm_name, vm_info in vms.items():
            if vm_name in existing:
                vm = existing[vm_name]
            else:
                vm = Vm(cluster_id=cluster.id, vm_name=vm_name)
                session.add(vm)
                cluster.vms.append(vm)
            vm.vmid = vm_info.get("vmid", vm.vmid)
            vm.node = vm_info.get("node", vm.node)
            vm.role = vm_info.get("role", "")
            vm.mac = vm_info.get("mac", "")
            vm.ip = vm_info.get("ip", "")

        return cluster.id


def delete_cluster_db(name):
    with session_scope(commit=True) as session:
        cluster = session.query(Cluster).filter_by(name=name).first()
        if cluster:
            session.delete(cluster)


def find_cluster_by_vm(node, vmid):
    with session_scope() as session:
        vm = session.query(Vm).filter_by(node=node, vmid=vmid).first()
        if not vm:
            return None
        cluster = session.query(Cluster).filter_by(id=vm.cluster_id).first()
        if not cluster:
            return None
        return _cluster_to_dict(cluster)


def migrate_from_json(json_path):
    if not os.path.exists(json_path):
        return
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    for name, entry in data.items():
        existing = load_cluster(name)
        if not existing:
            save_cluster(name, entry)


def migrate_config_from_json(key, json_path):
    if not os.path.exists(json_path):
        return
    existing = get_config(key)
    if existing is not None:
        return
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        set_config(key, data)
    except Exception:
        pass


def get_db_config():
    return _load_db_config()


def set_db_config(cfg):
    _save_db_config(cfg)


def reload_db_engine():
    global engine, SessionLocal
    with _engine_lock:
        engine = _create_engine()
        SessionLocal = sessionmaker(bind=engine) if engine else None


def is_db_configured():
    with _engine_lock:
        return engine is not None


def get_db_status():
    cfg = _load_db_config()
    if not cfg:
        return {"type": "none", "connected": False, "cluster_count": 0}
    result = {"type": "postgresql", "host": cfg.get("host", ""), "database": cfg.get("database", "")}
    try:
        with session_scope() as session:
            cluster_count = session.query(Cluster).count()
        result["cluster_count"] = cluster_count
        result["connected"] = True
    except Exception:
        result["connected"] = False
        result["cluster_count"] = 0
    return result


# ── User CRUD ──

def _user_to_dict(u):
    return {
        "id": u.id,
        "username": u.username,
        "name": u.name,
        "role": u.role,
        "is_active": u.is_active,
        "password_hash": u.password_hash,
        "created_by": u.created_by,
        "created_at": u.created_at.isoformat() if u.created_at else None,
    }


def create_user(data):
    session = get_session()
    try:
        u = User(
            username=data["username"],
            password_hash=data["password_hash"],
            role=data.get("role", "student"),
            name=data.get("name", ""),
            created_by=data.get("created_by"),
        )
        session.add(u)
        session.commit()
        return u.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_user(user_id):
    session = get_session()
    try:
        u = session.query(User).filter_by(id=user_id).first()
        if not u:
            return None
        return _user_to_dict(u)
    finally:
        session.close()


def get_user_by_username(username):
    session = get_session()
    try:
        u = session.query(User).filter_by(username=username).first()
        if not u:
            return None
        return _user_to_dict(u)
    finally:
        session.close()


def list_users(role=None, created_by=None):
    session = get_session()
    try:
        q = session.query(User)
        if role:
            q = q.filter_by(role=role)
        if created_by is not None:
            q = q.filter_by(created_by=created_by)
        users = q.order_by(User.created_at.desc()).all()
        result = []
        for u in users:
            d = _user_to_dict(u)
            d.pop("password_hash", None)
            result.append(d)
        return result
    finally:
        session.close()


def update_user(user_id, data):
    session = get_session()
    try:
        u = session.query(User).filter_by(id=user_id).first()
        if not u:
            return None
        if "username" in data:
            u.username = data["username"]
        if "password_hash" in data:
            u.password_hash = data["password_hash"]
        if "is_active" in data:
            u.is_active = data["is_active"]
        if "name" in data:
            u.name = data["name"]
        session.commit()
        return u.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_user(user_id):
    session = get_session()
    try:
        u = session.query(User).filter_by(id=user_id).first()
        if u:
            session.delete(u)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def count_users(role=None):
    session = get_session()
    try:
        q = session.query(func.count(User.id))
        if role:
            q = q.filter_by(role=role)
        return q.scalar() or 0
    finally:
        session.close()


# ── Class CRUD ──

def create_class(data):
    session = get_session()
    try:
        c = SchoolClass(
            name=data["name"],
            description=data.get("description", ""),
            created_by=data["created_by"],
        )
        session.add(c)
        session.commit()
        return c.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_class(class_id):
    session = get_session()
    try:
        c = session.query(SchoolClass).filter_by(id=class_id).first()
        if not c:
            return None
        return {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "created_by": c.created_by,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
    finally:
        session.close()


def get_class_by_name(name, created_by=None):
    with session_scope() as session:
        q = session.query(SchoolClass).filter_by(name=name)
        if created_by is not None:
            q = q.filter_by(created_by=created_by)
        c = q.first()
        if not c:
            return None
        return {"id": c.id, "name": c.name, "created_by": c.created_by}


def list_classes(created_by=None):
    session = get_session()
    try:
        q = session.query(SchoolClass)
        if created_by is not None:
            q = q.filter_by(created_by=created_by)
        classes = q.order_by(SchoolClass.created_at.desc()).all()
        return [{
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "created_by": c.created_by,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        } for c in classes]
    finally:
        session.close()


def update_class(class_id, data):
    session = get_session()
    try:
        c = session.query(SchoolClass).filter_by(id=class_id).first()
        if not c:
            return None
        if "name" in data:
            c.name = data["name"]
        if "description" in data:
            c.description = data["description"]
        session.commit()
        return c.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_class(class_id):
    session = get_session()
    try:
        c = session.query(SchoolClass).filter_by(id=class_id).first()
        if c:
            session.delete(c)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def count_classes(created_by=None):
    session = get_session()
    try:
        q = session.query(func.count(SchoolClass.id))
        if created_by is not None:
            q = q.filter_by(created_by=created_by)
        return q.scalar() or 0
    finally:
        session.close()


def get_classes_for_student(user_id):
    session = get_session()
    try:
        memberships = session.query(GroupMember).filter_by(user_id=user_id).all()
        if not memberships:
            return []
        class_ids = list(set(m.class_id for m in memberships))
        classes = session.query(SchoolClass).filter(SchoolClass.id.in_(class_ids)).all()
        class_map = {c.id: {"id": c.id, "name": c.name, "description": c.description} for c in classes}
        group_ids = list(set(m.group_id for m in memberships))
        groups = session.query(Group).filter(Group.id.in_(group_ids)).all()
        group_map = {g.id: g for g in groups}
        for m in memberships:
            cid = m.class_id
            if cid in class_map and "groups" not in class_map[cid]:
                class_map[cid]["groups"] = []
            g = group_map.get(m.group_id)
            if g and cid in class_map:
                class_map[cid]["groups"].append({
                    "group_id": g.id,
                    "group_name": g.name,
                })
        return list(class_map.values())
    finally:
        session.close()


# ── Group CRUD ──

def create_group(data):
    session = get_session()
    try:
        g = Group(
            name=data["name"],
            class_id=data["class_id"],
            created_by=data["created_by"],
        )
        session.add(g)
        session.commit()
        return g.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_or_create_group(class_id, group_name, created_by):
    with session_scope(commit=True) as session:
        g = session.query(Group).filter_by(class_id=class_id, name=group_name).first()
        if g:
            return g.id
        g = Group(name=group_name, class_id=class_id, created_by=created_by)
        session.add(g)
        session.flush()
        return g.id


def get_group(group_id):
    session = get_session()
    try:
        g = session.query(Group).filter_by(id=group_id).first()
        if not g:
            return None
        return {
            "id": g.id,
            "name": g.name,
            "class_id": g.class_id,
            "created_by": g.created_by,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        }
    finally:
        session.close()


def list_groups(class_id=None, created_by=None):
    session = get_session()
    try:
        q = session.query(Group)
        if class_id is not None:
            q = q.filter_by(class_id=class_id)
        if created_by is not None:
            q = q.filter_by(created_by=created_by)
        groups = q.order_by(Group.created_at.desc()).all()
        return [{
            "id": g.id,
            "name": g.name,
            "class_id": g.class_id,
            "created_by": g.created_by,
            "created_at": g.created_at.isoformat() if g.created_at else None,
        } for g in groups]
    finally:
        session.close()


def list_groups_batch(class_ids):
    session = get_session()
    try:
        groups = session.query(Group).filter(
            Group.class_id.in_(class_ids)
        ).order_by(Group.created_at.desc()).all()
        from collections import defaultdict
        result = defaultdict(list)
        for g in groups:
            result[g.class_id].append({
                "id": g.id,
                "name": g.name,
                "class_id": g.class_id,
                "created_by": g.created_by,
                "created_at": g.created_at.isoformat() if g.created_at else None,
            })
        return dict(result)
    finally:
        session.close()


def delete_group(group_id):
    session = get_session()
    try:
        g = session.query(Group).filter_by(id=group_id).first()
        if g:
            session.delete(g)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def count_groups(class_id=None):
    session = get_session()
    try:
        q = session.query(func.count(Group.id))
        if class_id is not None:
            q = q.filter_by(class_id=class_id)
        return q.scalar() or 0
    finally:
        session.close()


# ── GroupMember CRUD ──

def add_group_member(group_id, user_id):
    session = get_session()
    try:
        group = session.query(Group).filter_by(id=group_id).first()
        if not group:
            raise ValueError("组不存在")
        existing = session.query(GroupMember).filter_by(
            user_id=user_id, class_id=group.class_id
        ).first()
        if existing:
            raise ValueError("该学生已在本班级的其他组中")
        gm = GroupMember(group_id=group_id, user_id=user_id, class_id=group.class_id)
        session.add(gm)
        session.commit()
        return gm.id
    except ValueError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def remove_group_member(group_id, user_id):
    session = get_session()
    try:
        gm = session.query(GroupMember).filter_by(
            group_id=group_id, user_id=user_id
        ).first()
        if gm:
            session.delete(gm)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def list_group_members(group_id):
    session = get_session()
    try:
        members = session.query(GroupMember).filter_by(group_id=group_id).all()
        user_ids = [m.user_id for m in members]
        if not user_ids:
            return []
        users = session.query(User).filter(User.id.in_(user_ids)).all()
        return [{"id": u.id, "username": u.username, "role": u.role} for u in users]
    finally:
        session.close()


def list_group_members_batch(group_ids):
    session = get_session()
    try:
        members = session.query(GroupMember).filter(GroupMember.group_id.in_(group_ids)).all()
        if not members:
            return {}
        user_ids = list(set(m.user_id for m in members))
        users = session.query(User).filter(User.id.in_(user_ids)).all()
        user_map = {u.id: {"id": u.id, "username": u.username, "role": u.role} for u in users}
        from collections import defaultdict
        result = defaultdict(list)
        for m in members:
            result[m.group_id].append(user_map[m.user_id])
        return result
    finally:
        session.close()


def get_user_groups(user_id):
    session = get_session()
    try:
        memberships = session.query(GroupMember).filter_by(user_id=user_id).all()
        if not memberships:
            return []
        group_ids = [m.group_id for m in memberships]
        groups = session.query(Group).filter(Group.id.in_(group_ids)).all()
        class_ids = list(set(g.class_id for g in groups))
        classes = session.query(SchoolClass).filter(SchoolClass.id.in_(class_ids)).all()
        class_map = {c.id: c for c in classes}
        result = []
        for g in groups:
            c = class_map.get(g.class_id)
            result.append({
                "group_id": g.id,
                "group_name": g.name,
                "class_id": g.class_id,
                "class_name": c.name if c else "未知班级",
            })
        return result
    finally:
        session.close()


def get_user_cluster_ids(user_id):
    session = get_session()
    try:
        memberships = session.query(GroupMember).filter_by(user_id=user_id).all()
        if not memberships:
            return []
        group_ids = [m.group_id for m in memberships]
        if not group_ids:
            return []
        clusters = session.query(Cluster.id).filter(Cluster.group_id.in_(group_ids)).all()
        return [c[0] for c in clusters]
    finally:
        session.close()


def check_user_in_class_group(user_id, class_id):
    session = get_session()
    try:
        existing = session.query(GroupMember).filter_by(
            user_id=user_id, class_id=class_id
        ).first()
        return existing is not None
    finally:
        session.close()


def get_students_created_by(teacher_id):
    session = get_session()
    try:
        users = session.query(User).filter_by(created_by=teacher_id, role="student").all()
        result = []
        for u in users:
            d = _user_to_dict(u)
            d.pop("password_hash", None)
            result.append(d)
        return result
    finally:
        session.close()


def get_student_group_ids(user_id):
    session = get_session()
    try:
        memberships = session.query(GroupMember).filter_by(user_id=user_id).all()
        return [m.group_id for m in memberships]
    finally:
        session.close()
