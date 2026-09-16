-- Frozen legacy schema baseline. Do not modify after v0001 is merged.
-- Source: modules/db.py ORM as of P0 contract freeze (8fd10d0).
-- Future schema changes must use v0002+.

CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(256) NOT NULL,
    role VARCHAR(16) NOT NULL DEFAULT 'student',
    name VARCHAR(128) DEFAULT '',
    is_active BOOLEAN DEFAULT TRUE,
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS classes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    description TEXT DEFAULT '',
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS groups (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    class_id INTEGER NOT NULL REFERENCES classes(id),
    max_students INTEGER DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS group_members (
    id SERIAL PRIMARY KEY,
    group_id INTEGER NOT NULL REFERENCES groups(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    class_id INTEGER NOT NULL REFERENCES classes(id),
    student_number INTEGER,
    CONSTRAINT uq_user_class_group UNIQUE (user_id, class_id)
);

CREATE TABLE IF NOT EXISTS pve_servers (
    id SERIAL PRIMARY KEY,
    name VARCHAR(64) NOT NULL UNIQUE,
    host VARCHAR(128) NOT NULL,
    port INTEGER DEFAULT 8006,
    "user" VARCHAR(64) NOT NULL,
    token_name VARCHAR(64) NOT NULL,
    token_value TEXT NOT NULL,
    node VARCHAR(64) DEFAULT '',
    template_vmid INTEGER DEFAULT 9000,
    ow_host VARCHAR(128) DEFAULT '',
    ow_port INTEGER DEFAULT 22,
    ow_username VARCHAR(64) DEFAULT '',
    ow_password TEXT DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS clusters (
    id SERIAL PRIMARY KEY,
    name VARCHAR(32) NOT NULL UNIQUE,
    status VARCHAR(16) DEFAULT 'creating',
    vlan_id INTEGER,
    vlan_device VARCHAR(32),
    interface VARCHAR(32),
    gateway VARCHAR(16),
    netmask VARCHAR(16),
    dnsmasq VARCHAR(32),
    ssh_private_key TEXT,
    ssh_public_key TEXT,
    pve_node VARCHAR(32),
    template_vmid INTEGER,
    pve_server_id INTEGER DEFAULT 0,
    ssh_port INTEGER,
    client_mac VARCHAR(24),
    k8s_status VARCHAR(16) DEFAULT 'pending',
    password VARCHAR(64) DEFAULT 'k8s.1234',
    students TEXT DEFAULT '{}',
    group_id INTEGER REFERENCES groups(id),
    class_id INTEGER REFERENCES classes(id),
    created_by INTEGER REFERENCES users(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS vms (
    id SERIAL PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    vm_name VARCHAR(64) NOT NULL,
    vmid INTEGER NOT NULL UNIQUE,
    node VARCHAR(32) NOT NULL,
    role VARCHAR(16),
    mac VARCHAR(24),
    ip VARCHAR(16)
);

CREATE TABLE IF NOT EXISTS config (
    key VARCHAR(64) PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_users_username ON users(username);
CREATE INDEX IF NOT EXISTS ix_users_created_by ON users(created_by);
CREATE INDEX IF NOT EXISTS ix_classes_created_by ON classes(created_by);
CREATE INDEX IF NOT EXISTS ix_groups_class_id ON groups(class_id);
CREATE INDEX IF NOT EXISTS ix_groups_created_by ON groups(created_by);
CREATE INDEX IF NOT EXISTS ix_group_members_group_id ON group_members(group_id);
CREATE INDEX IF NOT EXISTS ix_group_members_user_id ON group_members(user_id);
CREATE INDEX IF NOT EXISTS ix_group_members_class_id ON group_members(class_id);
CREATE INDEX IF NOT EXISTS ix_clusters_group_id ON clusters(group_id);
CREATE INDEX IF NOT EXISTS ix_clusters_class_id ON clusters(class_id);
CREATE INDEX IF NOT EXISTS ix_clusters_created_by ON clusters(created_by);
CREATE INDEX IF NOT EXISTS ix_clusters_pve_server_id ON clusters(pve_server_id);
CREATE INDEX IF NOT EXISTS ix_vms_cluster_id ON vms(cluster_id);
CREATE INDEX IF NOT EXISTS ix_vms_node ON vms(node);
