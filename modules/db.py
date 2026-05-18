import json
import os
from datetime import datetime

from sqlalchemy import (
    Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine, func, text as sa_text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(DB_DIR, "k8s_lab.db")
DB_CONFIG_PATH = os.path.join(DB_DIR, ".db_config.json")


def _load_db_config():
    if os.path.exists(DB_CONFIG_PATH):
        try:
            with open(DB_CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"type": "sqlite"}


def _save_db_config(cfg):
    with open(DB_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _create_engine():
    cfg = _load_db_config()
    if cfg.get("type") == "postgresql":
        url = (f"postgresql+pg8000://{cfg['user']}:{cfg['password']}@"
               f"{cfg['host']}:{cfg.get('port', 5432)}/{cfg['database']}")
        return create_engine(url, echo=False)
    return create_engine(f"sqlite:///{DB_PATH}", echo=False)


engine = _create_engine()
SessionLocal = sessionmaker(bind=engine)
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

    vms = relationship("Vm", back_populates="cluster", cascade="all, delete-orphan")


class Vm(Base):
    __tablename__ = "vms"
    id = Column(Integer, primary_key=True)
    cluster_id = Column(Integer, ForeignKey("clusters.id"), nullable=False)
    vm_name = Column(String(64), nullable=False)
    vmid = Column(Integer, unique=True, nullable=False)
    node = Column(String(32), nullable=False)
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


def get_config(key):
    session = get_session()
    try:
        row = session.query(Config).filter_by(key=key).first()
        return json.loads(row.value) if row else None
    finally:
        session.close()


def set_config(key, value):
    session = get_session()
    try:
        row = session.query(Config).filter_by(key=key).first()
        val = json.dumps(value, ensure_ascii=False)
        if row:
            row.value = val
        else:
            session.add(Config(key=key, value=val))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_config(key):
    session = get_session()
    try:
        session.query(Config).filter_by(key=key).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def migrate_pve_config():
    session = get_session()
    try:
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
        session.commit()
        delete_config("pve")
    except Exception:
        session.rollback()
    finally:
        session.close()


def list_pve_servers():
    session = get_session()
    try:
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
    finally:
        session.close()


def get_pve_server(server_id):
    session = get_session()
    try:
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
    finally:
        session.close()


def create_pve_server(data):
    session = get_session()
    try:
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
        session.commit()
        return s.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def update_pve_server(server_id, data):
    session = get_session()
    try:
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
        session.commit()
        return s.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_pve_server(server_id):
    session = get_session()
    try:
        s = session.query(PVEServer).filter_by(id=server_id).first()
        if s:
            session.delete(s)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    Base.metadata.create_all(engine)
    cfg = _load_db_config()
    if cfg.get("type", "sqlite") == "sqlite":
        try:
            with engine.connect() as conn:
                conn.execute(sa_text("ALTER TABLE clusters ADD COLUMN k8s_status VARCHAR(16) DEFAULT 'pending'"))
                conn.commit()
        except Exception:
            pass
        try:
            with engine.connect() as conn:
                conn.execute(sa_text("ALTER TABLE clusters ADD COLUMN password VARCHAR(64) DEFAULT 'k8s.1234'"))
                conn.commit()
        except Exception:
            pass
        try:
            with engine.connect() as conn:
                conn.execute(sa_text("ALTER TABLE clusters ADD COLUMN pve_server_id INTEGER DEFAULT 0"))
                conn.commit()
        except Exception:
            pass
        migrate_pve_config()


def get_session():
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
        "vms": {vm.vm_name: {"node": vm.node, "vmid": vm.vmid, "role": vm.role, "mac": vm.mac, "ip": vm.ip}
                for vm in cluster.vms},
    }


def load_clusters():
    session = get_session()
    try:
        clusters = session.query(Cluster).all()
        return {c.name: _cluster_to_dict(c) for c in clusters}
    finally:
        session.close()


def load_cluster(name):
    session = get_session()
    try:
        cluster = session.query(Cluster).filter_by(name=name).first()
        if cluster is None:
            return None
        return _cluster_to_dict(cluster)
    finally:
        session.close()


def save_cluster(name, cluster_data):
    session = get_session()
    try:
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

        session.commit()
        return cluster.id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_cluster_db(name):
    session = get_session()
    try:
        cluster = session.query(Cluster).filter_by(name=name).first()
        if cluster:
            session.delete(cluster)
            session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


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


def get_db_status():
    cfg = _load_db_config()
    result = {"type": cfg.get("type", "sqlite")}
    if result["type"] == "postgresql":
        result["host"] = cfg.get("host", "")
        result["database"] = cfg.get("database", "")
    try:
        session = get_session()
        cluster_count = session.query(Cluster).count()
        session.close()
        result["cluster_count"] = cluster_count
        result["connected"] = True
    except Exception:
        result["connected"] = False
        result["cluster_count"] = 0
    return result


def migrate_data_from_sqlite():
    cfg = _load_db_config()
    if cfg.get("type") != "postgresql":
        raise Exception("当前不是 PostgreSQL 模式")

    if not os.path.exists(DB_PATH):
        raise Exception(f"SQLite 文件不存在: {DB_PATH}")

    sqlite_engine = create_engine(f"sqlite:///{DB_PATH}")
    SQLiteSession = sessionmaker(bind=sqlite_engine)

    pg_session = get_session()
    sq_session = SQLiteSession()

    try:
        for row in sq_session.query(Config).all():
            existing = pg_session.query(Config).filter_by(key=row.key).first()
            if not existing:
                pg_session.add(Config(key=row.key, value=row.value))

        sq_clusters = sq_session.query(Cluster).all()
        for sc in sq_clusters:
            existing = pg_session.query(Cluster).filter_by(id=sc.id).first()
            if not existing:
                c = Cluster(
                    id=sc.id, name=sc.name, status=sc.status,
                    vlan_id=sc.vlan_id, vlan_device=sc.vlan_device,
                    interface=sc.interface, gateway=sc.gateway,
                    netmask=sc.netmask, dnsmasq=sc.dnsmasq,
                    ssh_private_key=sc.ssh_private_key,
                    ssh_public_key=sc.ssh_public_key,
                    pve_node=sc.pve_node, template_vmid=sc.template_vmid,
                    ssh_port=sc.ssh_port, client_mac=sc.client_mac,
                    k8s_status=sc.k8s_status, password=sc.password,
                    pve_server_id=sc.pve_server_id,
                    created_at=sc.created_at, updated_at=sc.updated_at,
                )
                pg_session.add(c)
                pg_session.flush()

                for sv in sc.vms:
                    pg_session.add(Vm(
                        cluster_id=c.id, vm_name=sv.vm_name,
                        vmid=sv.vmid, node=sv.node, role=sv.role,
                        mac=sv.mac, ip=sv.ip,
                    ))

        pg_session.commit()
        return {"message": f"已迁移 {len(sq_clusters)} 条集群记录"}
    except Exception:
        pg_session.rollback()
        raise
    finally:
        sq_session.close()
        pg_session.close()
