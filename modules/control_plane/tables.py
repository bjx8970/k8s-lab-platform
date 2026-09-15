"""Core table declarations, separate from the legacy SQLite-test metadata."""

from sqlalchemy import ARRAY, BIGINT, BOOLEAN, TIMESTAMP, Column, MetaData, String, Table, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID

metadata = MetaData()

environments = Table("cp_environments", metadata,
 Column("uid",UUID(as_uuid=True),primary_key=True), Column("api_version",String(64)), Column("kind",String(64)),
 Column("name",String(253)), Column("namespace",String(253)), Column("labels",JSONB), Column("annotations",JSONB),
 Column("spec",JSONB), Column("status",JSONB), Column("owner_scope",String(128)), Column("owner_id",String(128)),
 Column("authorization_ref",Text), Column("generation",BIGINT), Column("observed_generation",BIGINT),
 Column("resource_version",BIGINT), Column("deletion_timestamp",TIMESTAMP(timezone=True)), Column("finalizers",ARRAY(Text)),
 Column("created_at",TIMESTAMP(timezone=True)), Column("updated_at",TIMESTAMP(timezone=True)))
resources = Table("rf_resources", metadata,
 Column("uid",UUID(as_uuid=True),primary_key=True), Column("resource_type",String(128)), Column("driver_id",String(128)),
 Column("registration_state",String(16)), Column("existence_state",String(16)), Column("attributes",JSONB), Column("status",JSONB),
 Column("revision",BIGINT), Column("resource_version",BIGINT), Column("created_at",TIMESTAMP(timezone=True)), Column("updated_at",TIMESTAMP(timezone=True)))
operations = Table("rf_operations", metadata,
 Column("uid",UUID(as_uuid=True),primary_key=True), Column("resource_uid",UUID(as_uuid=True)), Column("action",String(128)),
 Column("phase",String(24)), Column("is_mutating",BOOLEAN), Column("normalized_input",JSONB), Column("server_scope",String(255)),
 Column("request_id",String(255)), Column("request_digest",String(64)), Column("operation_key",String(255)), Column("attempt",BIGINT),
 Column("source_type",String(16)), Column("correlation_id",String(255)),
 Column("target_snapshot",JSONB), Column("binding_uid",UUID(as_uuid=True)), Column("binding_revision",BIGINT),
 Column("connection_uid",UUID(as_uuid=True)), Column("connection_revision",BIGINT), Column("secret_version_ref",Text),
 Column("plugin_id",String(128)), Column("plugin_version",String(64)), Column("driver_id",String(128)),
 Column("lease_owner",String(255)), Column("lease_until",TIMESTAMP(timezone=True)), Column("claim_revision",BIGINT),
 Column("external_task_ref",Text), Column("remote_job_id",String(255)), Column("remote_status_path",Text),
 Column("remote_exit_path",Text), Column("remote_log_path",Text), Column("exec_data",JSONB), Column("result",JSONB),
 Column("error",JSONB), Column("cancellation_requested",BOOLEAN), Column("resource_version",BIGINT),
 Column("created_at",TIMESTAMP(timezone=True)), Column("started_at",TIMESTAMP(timezone=True)),
 Column("finished_at",TIMESTAMP(timezone=True)), Column("updated_at",TIMESTAMP(timezone=True)))
outbox = Table("cp_outbox", metadata,
 Column("id",BIGINT,primary_key=True), Column("object_kind",String(64)), Column("object_uid",UUID(as_uuid=True)),
 Column("resource_version",BIGINT), Column("event_type",String(64)), Column("payload",JSONB),
 Column("created_at",TIMESTAMP(timezone=True)), Column("published_at",TIMESTAMP(timezone=True)))
bindings = Table("rf_bindings", metadata,
 Column("uid",UUID(as_uuid=True),primary_key=True), Column("resource_uid",UUID(as_uuid=True)),
 Column("connection_uid",UUID(as_uuid=True)), Column("domain_id",String(255)), Column("driver_id",String(128)),
 Column("external_key",String(255)), Column("external_identity",JSONB), Column("locator",JSONB),
 Column("external_identity_digest",String(64)), Column("revision",BIGINT), Column("provisional",BOOLEAN),
 Column("existence_state",String(16)), Column("active",BOOLEAN), Column("resource_version",BIGINT),
 Column("created_at",TIMESTAMP(timezone=True)), Column("closed_at",TIMESTAMP(timezone=True)))
connections = Table("rf_connections", metadata,
 Column("uid",UUID(as_uuid=True),primary_key=True), Column("domain_id",String(255)), Column("connection_type",String(128)),
 Column("secret_version_ref",Text), Column("configuration",JSONB), Column("revision",BIGINT), Column("active",BOOLEAN),
 Column("resource_version",BIGINT), Column("created_at",TIMESTAMP(timezone=True)), Column("updated_at",TIMESTAMP(timezone=True)))


def next_resource_version():
    return func.nextval("cp_resource_version_seq")


def utc_now():
    return func.current_timestamp()
