"""Transactional resource registration and command admission."""

from uuid import uuid4

from sqlalchemy import and_, func, insert, select, update

from modules.audit import sanitize
from modules.control_plane.repositories import (
    OperationAdmissionRepository, OperationExecutorRepository, RequestConflict, _event, canonical_digest,
)
from modules.control_plane.tables import (
    bindings, connections, next_resource_version, operations, resources, utc_now,
)


class ResourceService:
    def __init__(self, session, registry):
        self.session = session
        self.registry = registry

    def add_connection(self, *, domain_id, connection_type, configuration, secret_version_ref):
        if not domain_id or not secret_version_ref or sanitize(configuration) != configuration:
            raise ValueError("Connection 配置不能包含凭据正文")
        uid = uuid4()
        row = self.session.execute(insert(connections).values(uid=uid, domain_id=domain_id,
            connection_type=connection_type, configuration=configuration,
            secret_version_ref=secret_version_ref, revision=1, active=True,
            resource_version=next_resource_version(), created_at=utc_now(),
            updated_at=utc_now()).returning(connections)).mappings().one()
        _event(self.session, "Connection", uid, row["resource_version"], "ADDED")
        return dict(row)

    def _connection(self, uid):
        connection = self.session.execute(select(connections).where(
            connections.c.uid == uid).with_for_update()).mappings().one_or_none()
        if connection is None or not connection["active"]:
            raise ValueError("Connection 不可用")
        return connection

    def _handler(self, plugin_id, plugin_version, driver_id, action):
        handler = self.registry.get(plugin_id, plugin_version, driver_id)
        if action not in handler.actions:
            raise ValueError("不支持的资源动作")
        return handler

    def _input(self, normalized_input):
        if not isinstance(normalized_input, dict) or sanitize(normalized_input) != normalized_input:
            raise ValueError("Operation 参数不能包含凭据正文")

    def _bind(self, *, connection, resource_type, driver_id, external_key, locator,
              identity_evidence, attributes, provisional):
        resource_uid, binding_uid = uuid4(), uuid4()
        resource = self.session.execute(insert(resources).values(
            uid=resource_uid, resource_type=resource_type, driver_id=driver_id,
            registration_state="active", existence_state="pending" if provisional else "present",
            attributes=attributes, status={}, revision=1, resource_version=next_resource_version(),
            created_at=utc_now(), updated_at=utc_now()).returning(resources)).mappings().one()
        binding = self.session.execute(insert(bindings).values(
            uid=binding_uid, resource_uid=resource_uid, connection_uid=connection["uid"],
            domain_id=connection["domain_id"], driver_id=driver_id, external_key=external_key,
            external_identity=identity_evidence, locator=locator,
            external_identity_digest=canonical_digest(identity_evidence),
            revision=1, provisional=provisional,
            existence_state="pending" if provisional else "present", active=True,
            resource_version=next_resource_version(), created_at=utc_now()).returning(bindings)).mappings().one()
        _event(self.session, "Resource", resource_uid, resource["resource_version"], "ADDED")
        _event(self.session, "Binding", binding_uid, binding["resource_version"], "ADDED")
        return resource, binding

    def register(self, *, connection_uid, resource_type, plugin_id, plugin_version,
                 driver_id, external_key, locator, identity_evidence, attributes=None):
        self.registry.get(plugin_id, plugin_version, driver_id)
        attributes = {} if attributes is None else attributes
        connection = self._connection(connection_uid)
        if (not external_key or not isinstance(locator, dict) or not isinstance(identity_evidence, dict)
                or not identity_evidence or not isinstance(attributes, dict)
                or sanitize(locator) != locator or sanitize(identity_evidence) != identity_evidence
                or sanitize(attributes) != attributes):
            raise ValueError("外部身份、定位或资源属性无效")
        resource, _ = self._bind(connection=connection, resource_type=resource_type,
            driver_id=driver_id, external_key=external_key, locator=locator,
            identity_evidence=identity_evidence, attributes=attributes or {}, provisional=False)
        return resource

    def create(self, *, connection_uid, resource_type, plugin_id, plugin_version,
               driver_id, external_key, locator, identity_evidence, attributes,
               normalized_input, server_scope, request_id, correlation_id):
        self._handler(plugin_id, plugin_version, driver_id, "create")
        self._input(normalized_input)
        self.session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(
            str(len(server_scope)) + ":" + server_scope + request_id, 0)))).scalar_one()
        existing = self.session.execute(select(operations).where(and_(
            operations.c.server_scope == server_scope, operations.c.request_id == request_id))).mappings().one_or_none()
        if existing is not None:
            target = existing["target_snapshot"]
            resource = self.get_resource(existing["resource_uid"])
            binding = self.session.execute(select(bindings).where(
                bindings.c.uid == existing["binding_uid"])).mappings().one()
            if (existing["action"] != "create" or target["connectionId"] != str(connection_uid)
                    or target["driverId"] != driver_id or target["pluginId"] != plugin_id
                    or target["pluginVersion"] != plugin_version
                    or target["externalIdentity"]["externalKey"] != external_key
                    or target["locator"] != locator or binding["external_identity"] != identity_evidence
                    or resource["resource_type"] != resource_type or resource["attributes"] != attributes
                    or existing["normalized_input"] != normalized_input):
                raise RequestConflict(request_id)
            return dict(existing)
        connection = self._connection(connection_uid)
        if (not external_key or not isinstance(locator, dict) or not isinstance(identity_evidence, dict)
                or not identity_evidence or not isinstance(attributes, dict)
                or sanitize(locator) != locator or sanitize(identity_evidence) != identity_evidence
                or sanitize(attributes) != attributes):
            raise ValueError("外部身份、定位或资源属性无效")
        resource, binding = self._bind(connection=connection, resource_type=resource_type,
            driver_id=driver_id, external_key=external_key, locator=locator,
            identity_evidence=identity_evidence, attributes=attributes, provisional=True)
        return OperationAdmissionRepository(self.session).create(
            server_scope=server_scope, request_id=request_id, correlation_id=correlation_id,
            source_type="direct", resource_uid=resource["uid"], binding_uid=binding["uid"],
            binding_revision=binding["revision"], connection_uid=connection_uid,
            connection_revision=connection["revision"], plugin_id=plugin_id,
            plugin_version=plugin_version, driver_id=driver_id,
            action="create", normalized_input=normalized_input)

    def execute(self, resource_uid, *, action, normalized_input, plugin_id,
                plugin_version, server_scope, request_id, correlation_id):
        self._input(normalized_input)
        existing = self.session.execute(select(operations).where(and_(
            operations.c.server_scope == server_scope,
            operations.c.request_id == request_id))).mappings().one_or_none()
        if existing is not None:
            return OperationAdmissionRepository(self.session).create(
                server_scope=server_scope, request_id=request_id, correlation_id=correlation_id,
                source_type="direct", resource_uid=resource_uid,
                binding_uid=existing["binding_uid"], binding_revision=existing["binding_revision"],
                connection_uid=existing["connection_uid"],
                connection_revision=existing["connection_revision"], plugin_id=plugin_id,
                plugin_version=plugin_version, driver_id=existing["driver_id"],
                action=action, normalized_input=normalized_input)
        resource = self.get_resource(resource_uid)
        if resource["registration_state"] != "active":
            raise ValueError("Resource 已关闭登记")
        binding = self.session.execute(select(bindings).where(and_(
            bindings.c.resource_uid == resource_uid, bindings.c.active.is_(True)))).mappings().one()
        connection = self._connection(binding["connection_uid"])
        self._handler(plugin_id, plugin_version, resource["driver_id"], action)
        return OperationAdmissionRepository(self.session).create(
            server_scope=server_scope, request_id=request_id, correlation_id=correlation_id,
            source_type="direct", resource_uid=resource_uid, binding_uid=binding["uid"],
            binding_revision=binding["revision"], connection_uid=connection["uid"],
            connection_revision=connection["revision"], plugin_id=plugin_id,
            plugin_version=plugin_version, driver_id=resource["driver_id"],
            action=action, normalized_input=normalized_input)

    def get_resource(self, uid):
        row = self.session.execute(select(resources).where(resources.c.uid == uid)).mappings().one_or_none()
        if row is None:
            raise KeyError(str(uid))
        return dict(row)

    def get_operation(self, uid):
        row = self.session.execute(select(operations).where(operations.c.uid == uid)).mappings().one_or_none()
        if row is None:
            raise KeyError(str(uid))
        return dict(row)

    def cancel(self, uid):
        return OperationExecutorRepository(self.session).request_cancel(uid)

    def list_resources(self, *, limit=100):
        return [dict(row) for row in self.session.execute(select(resources)
            .order_by(resources.c.created_at, resources.c.uid).limit(limit)).mappings()]

    def unregister(self, uid):
        row = self.session.execute(update(resources).where(and_(resources.c.uid == uid,
            resources.c.registration_state == "active"))
            .values(registration_state="closed", revision=resources.c.revision + 1,
                resource_version=next_resource_version(), updated_at=utc_now())
            .returning(resources)).mappings().one_or_none()
        if row is None:
            raise KeyError(str(uid))
        _event(self.session, "Resource", uid, row["resource_version"], "MODIFIED")
        return dict(row)
