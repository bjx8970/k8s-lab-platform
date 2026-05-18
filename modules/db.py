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

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
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


def init_db():
    Base.metadata.create_all(engine)
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
