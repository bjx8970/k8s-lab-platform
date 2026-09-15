-- Legacy schema normalization is additive and transactionally versioned.
ALTER TABLE users ADD COLUMN IF NOT EXISTS name VARCHAR(128) DEFAULT '';
ALTER TABLE pve_servers ADD COLUMN IF NOT EXISTS template_vmid INTEGER DEFAULT 9000;
ALTER TABLE pve_servers ADD COLUMN IF NOT EXISTS ow_host VARCHAR(128) DEFAULT '';
ALTER TABLE pve_servers ADD COLUMN IF NOT EXISTS ow_port INTEGER DEFAULT 22;
ALTER TABLE pve_servers ADD COLUMN IF NOT EXISTS ow_username VARCHAR(64) DEFAULT '';
ALTER TABLE pve_servers ADD COLUMN IF NOT EXISTS ow_password TEXT DEFAULT '';
ALTER TABLE pve_servers ALTER COLUMN token_value TYPE TEXT;
ALTER TABLE pve_servers ALTER COLUMN ow_password TYPE TEXT;
ALTER TABLE group_members ADD COLUMN IF NOT EXISTS student_number INTEGER;
ALTER TABLE groups ADD COLUMN IF NOT EXISTS max_students INTEGER DEFAULT 0;
ALTER TABLE clusters ADD COLUMN IF NOT EXISTS students TEXT DEFAULT '{}';
CREATE INDEX IF NOT EXISTS ix_clusters_group_id ON clusters(group_id);
CREATE INDEX IF NOT EXISTS ix_clusters_created_by ON clusters(created_by);
CREATE INDEX IF NOT EXISTS ix_clusters_pve_server_id ON clusters(pve_server_id);
CREATE INDEX IF NOT EXISTS ix_vms_cluster_id ON vms(cluster_id);
CREATE INDEX IF NOT EXISTS ix_vms_node ON vms(node);
CREATE INDEX IF NOT EXISTS ix_group_members_user_id ON group_members(user_id);
CREATE INDEX IF NOT EXISTS ix_group_members_group_id ON group_members(group_id);
CREATE INDEX IF NOT EXISTS ix_group_members_class_id ON group_members(class_id);

CREATE SEQUENCE cp_resource_version_seq AS BIGINT;

CREATE TABLE cp_environments (
 uid UUID PRIMARY KEY, api_version VARCHAR(64) NOT NULL DEFAULT 'lab.platform/v1',
 kind VARCHAR(64) NOT NULL DEFAULT 'Environment', name VARCHAR(253) NOT NULL,
 namespace VARCHAR(253) NOT NULL DEFAULT 'default', labels JSONB NOT NULL DEFAULT '{}',
 annotations JSONB NOT NULL DEFAULT '{}', spec JSONB NOT NULL, status JSONB NOT NULL DEFAULT '{}',
 owner_scope VARCHAR(128) NOT NULL, owner_id VARCHAR(128) NOT NULL, authorization_ref TEXT NOT NULL,
 generation BIGINT NOT NULL DEFAULT 1 CHECK (generation > 0), observed_generation BIGINT NOT NULL DEFAULT 0,
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE,
 deletion_timestamp TIMESTAMPTZ, finalizers TEXT[] NOT NULL DEFAULT ARRAY['lab.platform/environment-cleanup'],
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(namespace,name), CHECK (observed_generation BETWEEN 0 AND generation));

CREATE TABLE cp_placements (
 uid UUID PRIMARY KEY, environment_uid UUID NOT NULL REFERENCES cp_environments(uid), revision INTEGER NOT NULL CHECK(revision>0),
 scheduling_input_digest CHAR(64) NOT NULL, candidate_summary JSONB NOT NULL DEFAULT '[]', binding_result JSONB NOT NULL,
 phase VARCHAR(24) NOT NULL CHECK(phase IN('proposed','reserved','bound','failed','superseded')), status JSONB NOT NULL DEFAULT '{}',
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(environment_uid,revision));

CREATE TABLE cp_allocations (
 uid UUID PRIMARY KEY, scope VARCHAR(255) NOT NULL, kind VARCHAR(32) NOT NULL CHECK(kind IN('vmid','ip','subnet','vlan','port')),
 value VARCHAR(255) NOT NULL, environment_uid UUID NOT NULL REFERENCES cp_environments(uid), placement_uid UUID REFERENCES cp_placements(uid),
 state VARCHAR(24) NOT NULL CHECK(state IN('reserved','assigned','quarantined','released')), quarantine_reason TEXT,
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE, reserved_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 assigned_at TIMESTAMPTZ, quarantined_at TIMESTAMPTZ, released_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CHECK((state<>'assigned' OR assigned_at IS NOT NULL) AND (state<>'quarantined' OR quarantined_at IS NOT NULL)
   AND (state<>'released' OR released_at IS NOT NULL)));
CREATE UNIQUE INDEX uq_cp_allocation_live ON cp_allocations(scope,kind,value) WHERE state<>'released';

CREATE TABLE cp_plan_revisions (
 uid UUID PRIMARY KEY, environment_uid UUID NOT NULL REFERENCES cp_environments(uid), revision INTEGER NOT NULL CHECK(revision>0),
 source_generation BIGINT NOT NULL CHECK(source_generation>0), plan_input_digest CHAR(64) NOT NULL, content_digest CHAR(64) NOT NULL,
 spec JSONB NOT NULL, phase VARCHAR(24) NOT NULL CHECK(phase IN('proposed','active','superseded','blocked')), status JSONB NOT NULL DEFAULT '{}',
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(environment_uid,revision));
CREATE UNIQUE INDEX uq_cp_plan_active ON cp_plan_revisions(environment_uid) WHERE phase='active';
CREATE FUNCTION cp_reject_plan_spec_change() RETURNS trigger AS $$ BEGIN
 IF NEW.spec IS DISTINCT FROM OLD.spec OR NEW.content_digest IS DISTINCT FROM OLD.content_digest
 OR NEW.source_generation IS DISTINCT FROM OLD.source_generation THEN RAISE EXCEPTION 'PlanRevision spec is immutable'; END IF;
 RETURN NEW; END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_cp_plan_spec_immutable BEFORE UPDATE ON cp_plan_revisions
 FOR EACH ROW EXECUTE FUNCTION cp_reject_plan_spec_change();

CREATE TABLE rf_connections (
 uid UUID PRIMARY KEY, domain_id VARCHAR(255) NOT NULL, connection_type VARCHAR(128) NOT NULL,
 credential_ref TEXT NOT NULL, configuration JSONB NOT NULL DEFAULT '{}', revision BIGINT NOT NULL DEFAULT 1 CHECK(revision>0),
 active BOOLEAN NOT NULL DEFAULT TRUE, resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE rf_resources (
 uid UUID PRIMARY KEY, resource_type VARCHAR(128) NOT NULL, driver_id VARCHAR(128) NOT NULL,
 registration_state VARCHAR(16) NOT NULL DEFAULT 'active' CHECK(registration_state IN('active','closed')),
 existence_state VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK(existence_state IN('pending','present','absent','unknown')),
 attributes JSONB NOT NULL DEFAULT '{}', status JSONB NOT NULL DEFAULT '{}', revision BIGINT NOT NULL DEFAULT 1 CHECK(revision>0),
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE rf_bindings (
 uid UUID PRIMARY KEY, resource_uid UUID NOT NULL REFERENCES rf_resources(uid), connection_uid UUID NOT NULL REFERENCES rf_connections(uid),
 domain_id VARCHAR(255) NOT NULL, driver_id VARCHAR(128) NOT NULL, external_key VARCHAR(255) NOT NULL,
 external_identity JSONB NOT NULL DEFAULT '{}', locator JSONB NOT NULL DEFAULT '{}', external_identity_digest CHAR(64) NOT NULL,
 revision BIGINT NOT NULL DEFAULT 1 CHECK(revision>0), provisional BOOLEAN NOT NULL DEFAULT FALSE,
 existence_state VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK(existence_state IN('pending','present','absent','unknown')),
 active BOOLEAN NOT NULL DEFAULT TRUE, resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, closed_at TIMESTAMPTZ);
CREATE UNIQUE INDEX uq_rf_binding_resource ON rf_bindings(resource_uid) WHERE active;
CREATE UNIQUE INDEX uq_rf_binding_identity ON rf_bindings(domain_id,driver_id,external_key) WHERE active;
CREATE TABLE rf_observations (
 uid UUID PRIMARY KEY, resource_uid UUID NOT NULL REFERENCES rf_resources(uid), binding_uid UUID NOT NULL REFERENCES rf_bindings(uid),
 binding_revision BIGINT NOT NULL, sample_sequence BIGINT NOT NULL CHECK(sample_sequence>0),
 existence_state VARCHAR(16) NOT NULL CHECK(existence_state IN('pending','present','absent','unknown')),
 data JSONB NOT NULL DEFAULT '{}', observed_at TIMESTAMPTZ NOT NULL, stale BOOLEAN NOT NULL DEFAULT FALSE,
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(resource_uid,sample_sequence));

CREATE TABLE rf_operations (
 uid UUID PRIMARY KEY, resource_uid UUID NOT NULL REFERENCES rf_resources(uid), action VARCHAR(128) NOT NULL,
 phase VARCHAR(24) NOT NULL DEFAULT 'pending' CHECK(phase IN('pending','running','pending_external','succeeded','failed','cancelling','cancelled','unknown')),
 is_mutating BOOLEAN NOT NULL DEFAULT TRUE, normalized_input JSONB NOT NULL, server_scope VARCHAR(255) NOT NULL,
 request_id VARCHAR(255) NOT NULL, request_digest CHAR(64) NOT NULL, operation_key VARCHAR(255), attempt BIGINT,
 source_type VARCHAR(16) NOT NULL CHECK(source_type IN('plan','direct')),
 correlation_id VARCHAR(255) NOT NULL,
 target_snapshot JSONB NOT NULL, binding_uid UUID NOT NULL REFERENCES rf_bindings(uid), binding_revision BIGINT NOT NULL,
 connection_uid UUID NOT NULL REFERENCES rf_connections(uid), connection_revision BIGINT NOT NULL, secret_version_ref TEXT,
 plugin_id VARCHAR(128) NOT NULL, plugin_version VARCHAR(64) NOT NULL, driver_id VARCHAR(128) NOT NULL,
 lease_owner VARCHAR(255), lease_until TIMESTAMPTZ, claim_revision BIGINT NOT NULL DEFAULT 0 CHECK(claim_revision>=0),
 external_task_ref TEXT, remote_job_id VARCHAR(255), remote_status_path TEXT, remote_exit_path TEXT, remote_log_path TEXT,
 exec_data JSONB NOT NULL DEFAULT '{}', result JSONB, error JSONB, cancellation_requested BOOLEAN NOT NULL DEFAULT FALSE,
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CONSTRAINT ck_rf_operation_source_type CHECK(source_type IN('plan','direct')),
 CONSTRAINT ck_rf_operation_source_fields CHECK(
   (source_type='plan' AND operation_key IS NOT NULL AND attempt IS NOT NULL)
   OR (source_type='direct' AND operation_key IS NULL AND attempt IS NULL)),
 CONSTRAINT ck_rf_operation_attempt_nonnegative CHECK(attempt IS NULL OR attempt>=0),
 CONSTRAINT ck_rf_operation_lease_pair CHECK((lease_owner IS NULL AND lease_until IS NULL) OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)),
 CONSTRAINT ck_rf_operation_pending_external_ref CHECK(phase <> 'pending_external' OR external_task_ref IS NOT NULL),
 UNIQUE(server_scope,request_id), UNIQUE(operation_key,attempt));
CREATE UNIQUE INDEX uq_rf_operation_mutation ON rf_operations(resource_uid)
 WHERE is_mutating AND phase IN('pending','running','pending_external','cancelling');
CREATE INDEX ix_rf_operation_claim ON rf_operations(phase,lease_until,created_at);

CREATE TABLE cp_environment_resources (
 uid UUID PRIMARY KEY, environment_uid UUID NOT NULL REFERENCES cp_environments(uid), logical_key VARCHAR(255) NOT NULL,
 resource_uid UUID NOT NULL REFERENCES rf_resources(uid), origin VARCHAR(16) NOT NULL CHECK(origin IN('created','adopted')),
 cleanup_responsibility VARCHAR(16) NOT NULL CHECK(cleanup_responsibility IN('delete','unregister','retain')),
 active_from TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, active_until TIMESTAMPTZ,
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE);
CREATE UNIQUE INDEX uq_cp_environment_resource ON cp_environment_resources(environment_uid,logical_key) WHERE active_until IS NULL;
CREATE TABLE cp_task_views (
 uid UUID PRIMARY KEY, environment_uid UUID REFERENCES cp_environments(uid), source_generation BIGINT,
 plan_revision_uid UUID REFERENCES cp_plan_revisions(uid), request_summary JSONB NOT NULL DEFAULT '{}', projection JSONB NOT NULL DEFAULT '{}',
 phase VARCHAR(24) NOT NULL, resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE cp_request_keys (
 server_scope VARCHAR(255) NOT NULL, entrypoint VARCHAR(255) NOT NULL, request_id VARCHAR(255) NOT NULL,
 request_digest CHAR(64) NOT NULL, result_kind VARCHAR(64) NOT NULL, result_uid UUID NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMPTZ,
 PRIMARY KEY(server_scope,entrypoint,request_id));
CREATE TABLE cp_outbox (
 id BIGSERIAL PRIMARY KEY, object_kind VARCHAR(64) NOT NULL, object_uid UUID NOT NULL, resource_version BIGINT NOT NULL,
 event_type VARCHAR(64) NOT NULL, payload JSONB NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 published_at TIMESTAMPTZ, attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0), next_attempt_at TIMESTAMPTZ,
 UNIQUE(object_kind,object_uid,resource_version,event_type));
CREATE INDEX ix_cp_outbox_pending ON cp_outbox(id) WHERE published_at IS NULL;
CREATE TABLE cp_conditions (
 uid UUID PRIMARY KEY, object_kind VARCHAR(64) NOT NULL, object_uid UUID NOT NULL, type VARCHAR(64) NOT NULL,
 status VARCHAR(8) NOT NULL CHECK(status IN('True','False','Unknown')), observed_generation BIGINT, reason VARCHAR(128) NOT NULL,
 message TEXT NOT NULL DEFAULT '', last_transition_time TIMESTAMPTZ NOT NULL,
 resource_version BIGINT NOT NULL DEFAULT nextval('cp_resource_version_seq') UNIQUE, UNIQUE(object_kind,object_uid,type));
CREATE TABLE cp_logs (
 id BIGSERIAL PRIMARY KEY, operation_uid UUID REFERENCES rf_operations(uid), task_uid UUID REFERENCES cp_task_views(uid),
 sequence BIGINT NOT NULL CHECK(sequence>=0), chunk TEXT NOT NULL, byte_count INTEGER NOT NULL CHECK(byte_count BETWEEN 0 AND 65536),
 redacted BOOLEAN NOT NULL CHECK(redacted), created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, expires_at TIMESTAMPTZ NOT NULL,
 CONSTRAINT ck_cp_log_exactly_one_owner CHECK((operation_uid IS NOT NULL) <> (task_uid IS NOT NULL)),
 CONSTRAINT ck_cp_log_byte_count CHECK(byte_count = octet_length(chunk)),
 CONSTRAINT ck_cp_log_redacted CHECK(redacted));
CREATE TABLE cp_log_owner_counters (
 owner_kind VARCHAR(16) NOT NULL CHECK(owner_kind IN('operation','task')),
 owner_uid UUID NOT NULL,
 used_bytes BIGINT NOT NULL DEFAULT 0 CHECK(used_bytes >= 0),
 max_bytes BIGINT NOT NULL DEFAULT 16777216 CHECK(max_bytes > 0),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(owner_kind, owner_uid),
 CONSTRAINT ck_cp_log_owner_quota CHECK(used_bytes <= max_bytes));
CREATE OR REPLACE FUNCTION cp_enforce_log_quota() RETURNS trigger AS $$
DECLARE v_owner_kind VARCHAR(16); v_owner_uid UUID; v_updated BIGINT;
BEGIN
 IF NEW.operation_uid IS NOT NULL THEN
  v_owner_kind := 'operation'; v_owner_uid := NEW.operation_uid;
 ELSE
  v_owner_kind := 'task'; v_owner_uid := NEW.task_uid;
 END IF;
 INSERT INTO cp_log_owner_counters(owner_kind, owner_uid, used_bytes, max_bytes)
  VALUES(v_owner_kind, v_owner_uid, 0, 16777216)
  ON CONFLICT(owner_kind, owner_uid) DO NOTHING;
 UPDATE cp_log_owner_counters
  SET used_bytes = used_bytes + NEW.byte_count, updated_at = CURRENT_TIMESTAMP
  WHERE owner_kind = v_owner_kind AND owner_uid = v_owner_uid
    AND used_bytes <= max_bytes - NEW.byte_count
  RETURNING used_bytes INTO v_updated;
 IF v_updated IS NULL THEN
  RAISE EXCEPTION USING
   ERRCODE = '23514',
   CONSTRAINT_NAME = 'ck_cp_log_owner_quota',
   MESSAGE = 'log quota exceeded for ' || v_owner_kind || ' ' || v_owner_uid;
 END IF;
 RETURN NEW;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_cp_log_quota BEFORE INSERT ON cp_logs FOR EACH ROW EXECUTE FUNCTION cp_enforce_log_quota();
CREATE OR REPLACE FUNCTION cp_decrement_log_counter() RETURNS trigger AS $$
DECLARE v_owner_kind VARCHAR(16); v_owner_uid UUID;
BEGIN
 IF OLD.operation_uid IS NOT NULL THEN
  v_owner_kind := 'operation'; v_owner_uid := OLD.operation_uid;
 ELSE
  v_owner_kind := 'task'; v_owner_uid := OLD.task_uid;
 END IF;
 UPDATE cp_log_owner_counters
  SET used_bytes = GREATEST(used_bytes - OLD.byte_count, 0), updated_at = CURRENT_TIMESTAMP
  WHERE owner_kind = v_owner_kind AND owner_uid = v_owner_uid;
 RETURN OLD;
END; $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_cp_log_decrement AFTER DELETE ON cp_logs FOR EACH ROW EXECUTE FUNCTION cp_decrement_log_counter();
CREATE UNIQUE INDEX uq_cp_log_operation ON cp_logs(operation_uid,sequence) WHERE operation_uid IS NOT NULL;
CREATE UNIQUE INDEX uq_cp_log_task ON cp_logs(task_uid,sequence) WHERE task_uid IS NOT NULL;

CREATE TABLE rf_domain_locks (
 domain_id VARCHAR(255) NOT NULL, lock_class VARCHAR(64) NOT NULL, lease_owner VARCHAR(255), lease_until TIMESTAMPTZ,
 fencing_token BIGINT NOT NULL DEFAULT 0 CHECK(fencing_token>=0), updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
 PRIMARY KEY(domain_id,lock_class), CHECK((lease_owner IS NULL AND lease_until IS NULL) OR (lease_owner IS NOT NULL AND lease_until IS NOT NULL)));
CREATE TABLE cp_reconcile_state (
 controller_name VARCHAR(128) NOT NULL, object_kind VARCHAR(64) NOT NULL, object_uid UUID NOT NULL,
 last_resource_version BIGINT, attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt>=0), next_retry_at TIMESTAMPTZ,
 last_error JSONB, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(controller_name,object_kind,object_uid));
CREATE TABLE cp_controller_settings (
 controller_name VARCHAR(128) PRIMARY KEY, reconcile_key_template VARCHAR(255) NOT NULL,
 base_backoff_seconds INTEGER NOT NULL CHECK(base_backoff_seconds>0), max_backoff_seconds INTEGER NOT NULL CHECK(max_backoff_seconds>=base_backoff_seconds),
 jitter_ratio NUMERIC(4,3) NOT NULL CHECK(jitter_ratio BETWEEN 0 AND 1), full_resync_seconds INTEGER NOT NULL CHECK(full_resync_seconds>0),
 max_concurrency INTEGER NOT NULL CHECK(max_concurrency>0), updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
INSERT INTO cp_controller_settings VALUES
 ('environment','Environment/{uid}',1,300,.2,60,4,CURRENT_TIMESTAMP),('scheduler','Environment/{uid}/{schedulingInputDigest}',1,60,.2,60,2,CURRENT_TIMESTAMP),
 ('executor','Operation/{uid}',1,300,.2,30,8,CURRENT_TIMESTAMP),('observation','Resource/{uid}/{bindingRevision}',2,300,.2,300,8,CURRENT_TIMESTAMP),
 ('finalizer','Environment/{uid}/deletion',2,600,.2,60,2,CURRENT_TIMESTAMP);
CREATE TABLE cp_retention_policies (
 record_kind VARCHAR(64) PRIMARY KEY, retention_days INTEGER NOT NULL CHECK(retention_days>0), max_bytes_per_record BIGINT,
 updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP);
INSERT INTO cp_retention_policies(record_kind,retention_days,max_bytes_per_record) VALUES
 ('operation_log',30,16777216),('task_log',30,16777216),('security_audit',365,NULL);
