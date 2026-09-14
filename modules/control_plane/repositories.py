"""Column-owned repositories. Callers own the transaction and commit."""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, case, exists, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from .tables import bindings, connections, environments, next_resource_version, operations, outbox, resources, utc_now

FINALIZER = "lab.platform/environment-cleanup"
TERMINAL_PHASES = {"succeeded","failed","cancelled","unknown"}
EXECUTOR_PHASES = {"running","pending_external","succeeded","failed","cancelling","cancelled","unknown"}
EXISTENCE_STATES = {"pending","present","absent","unknown"}

_UNSET = object()  # sentinel: distinguish "not provided" from "explicitly clear"


class ResourceVersionConflict(RuntimeError): pass
class RequestConflict(RuntimeError): pass
class LeaseLost(RuntimeError): pass


def canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _event(session, kind, uid, version, event_type):
    session.execute(insert(outbox).values(object_kind=kind, object_uid=uid,
        resource_version=version, event_type=event_type, payload={}))


class EnvironmentApiRepository:
    """API owns metadata/spec; this class exposes no status mutation."""
    owned_columns = frozenset({"api_version","kind","name","namespace","labels","annotations","spec",
        "owner_scope","owner_id","authorization_ref","generation","resource_version","deletion_timestamp","finalizers","updated_at"})

    def __init__(self, session): self.session = session

    def create(self, *, name, spec, owner_scope, owner_id, authorization_ref,
               namespace="default", labels=None, annotations=None, uid=None):
        uid = uid or uuid4()
        row = self.session.execute(insert(environments).values(uid=uid, api_version="lab.platform/v1", kind="Environment",
            name=name, namespace=namespace, labels=labels or {}, annotations=annotations or {}, spec=spec,
            owner_scope=owner_scope, owner_id=owner_id, authorization_ref=authorization_ref, generation=1,
            observed_generation=0, resource_version=next_resource_version(), finalizers=[FINALIZER],
            created_at=utc_now(), updated_at=utc_now()).returning(environments)).mappings().one()
        result=dict(row); _event(self.session,"Environment",uid,result["resource_version"],"ADDED"); return result

    def update_spec(self, uid, spec, expected_resource_version):
        current = self.session.execute(select(environments.c.spec).where(environments.c.uid==uid)).scalar_one_or_none()
        if current is None: raise KeyError(str(uid))
        values={"spec":spec,"resource_version":next_resource_version(),"updated_at":utc_now()}
        if canonical_digest(current)!=canonical_digest(spec): values["generation"]=environments.c.generation+1
        row=self.session.execute(update(environments).where(and_(environments.c.uid==uid,
            environments.c.resource_version==expected_resource_version,environments.c.deletion_timestamp.is_(None)))
            .values(**values).returning(environments)).mappings().one_or_none()
        if row is None: raise ResourceVersionConflict(str(uid))
        result=dict(row); _event(self.session,"Environment",uid,result["resource_version"],"MODIFIED"); return result

    def mark_for_deletion(self, uid, expected_resource_version, when=None):
        row=self.session.execute(update(environments).where(and_(environments.c.uid==uid,
            environments.c.resource_version==expected_resource_version)).values(deletion_timestamp=when or datetime.now(timezone.utc),
            resource_version=next_resource_version(),updated_at=utc_now()).returning(environments)).mappings().one_or_none()
        if row is None: raise ResourceVersionConflict(str(uid))
        result=dict(row); _event(self.session,"Environment",uid,result["resource_version"],"MODIFIED"); return result


class EnvironmentStatusRepository:
    """Controller owns status; this class exposes no spec mutation."""
    owned_columns = frozenset({"status","observed_generation","resource_version","finalizers","updated_at"})
    def __init__(self, session): self.session=session

    def update(self, uid, *, status, observed_generation, expected_resource_version):
        row=self.session.execute(update(environments).where(and_(environments.c.uid==uid,
            environments.c.resource_version==expected_resource_version,environments.c.generation>=observed_generation,
            environments.c.observed_generation<=observed_generation))
            .values(status=status,observed_generation=observed_generation,resource_version=next_resource_version(),updated_at=utc_now())
            .returning(environments)).mappings().one_or_none()
        if row is None: raise ResourceVersionConflict(str(uid))
        result=dict(row); _event(self.session,"Environment",uid,result["resource_version"],"MODIFIED"); return result

    def remove_cleanup_finalizer(self, uid, *, expected_resource_version, cleanup_confirmed):
        if not cleanup_confirmed: raise ValueError("清理未确认，不能移除 finalizer")
        row=self.session.execute(update(environments).where(and_(environments.c.uid==uid,
            environments.c.resource_version==expected_resource_version,environments.c.deletion_timestamp.is_not(None)))
            .values(finalizers=func.array_remove(environments.c.finalizers, FINALIZER),
            resource_version=next_resource_version(),updated_at=utc_now()).returning(environments)).mappings().one_or_none()
        if row is None: raise ResourceVersionConflict(str(uid))
        result=dict(row); _event(self.session,"Environment",uid,result["resource_version"],"MODIFIED"); return result


class OperationAdmissionRepository:
    """Admission fixes the target snapshot and transport idempotency scope."""
    owned_columns=frozenset({"resource_uid","action","phase","is_mutating","normalized_input","server_scope","request_id",
        "request_digest","target_snapshot","binding_uid","binding_revision","connection_uid","connection_revision",
        "secret_version_ref","plugin_id","plugin_version","driver_id","claim_revision","resource_version"})
    def __init__(self,session): self.session=session

    def create(self, *, server_scope, request_id, action, normalized_input, target_snapshot, plugin_id, plugin_version,
               driver_id, resource_uid=None, binding_uid=None, binding_revision=None, connection_uid=None,
               connection_revision=None, secret_version_ref=None, is_mutating=True, operation_key=None, attempt=None, uid=None):
        digest=canonical_digest({"action":action,"input":normalized_input,"target":target_snapshot})
        # --- transport dedup ---
        existing=self.session.execute(select(operations).where(and_(operations.c.server_scope==server_scope,
            operations.c.request_id==request_id))).mappings().one_or_none()
        if existing:
            if existing["request_digest"]!=digest: raise RequestConflict(request_id)
            return dict(existing)
        # --- plan-item dedup ---
        if operation_key is not None and attempt is not None:
            plan_existing = self.session.execute(select(operations).where(and_(
                operations.c.operation_key==operation_key, operations.c.attempt==attempt))).mappings().one_or_none()
            if plan_existing: return dict(plan_existing)
        # --- target snapshot consistency ---
        if resource_uid is not None:
            if None in (binding_uid,binding_revision,connection_uid,connection_revision):
                raise ValueError("资源 Operation 必须固定 binding/connection revision")
            resource = self.session.execute(select(resources.c.registration_state).where(
                resources.c.uid==resource_uid)).scalar_one_or_none()
            if resource is None: raise ValueError("Resource 不存在")
            binding = self.session.execute(select(bindings).where(and_(
                bindings.c.uid==binding_uid, bindings.c.active==True, bindings.c.resource_uid==resource_uid,
                bindings.c.revision==binding_revision))).mappings().one_or_none()
            if binding is None: raise ValueError("Binding 不存在/不活跃/不属于该 Resource/版本不匹配")
            conn = self.session.execute(select(connections).where(and_(
                connections.c.uid==connection_uid, connections.c.active==True,
                connections.c.revision==connection_revision))).mappings().one_or_none()
            if conn is None: raise ValueError("Connection 不存在/不活跃/版本不匹配")
            if binding["connection_uid"] != connection_uid:
                raise ValueError("Binding 不属于指定 Connection")
            if binding["domain_id"] != conn["domain_id"]:
                raise ValueError("Binding domain 与 Connection domain 不一致")
        uid=uid or uuid4()
        statement=pg_insert(operations).values(uid=uid,resource_uid=resource_uid,action=action,phase="pending",
            is_mutating=is_mutating,normalized_input=normalized_input,server_scope=server_scope,request_id=request_id,
            request_digest=digest,operation_key=operation_key,attempt=attempt,target_snapshot=target_snapshot,
            binding_uid=binding_uid,binding_revision=binding_revision,connection_uid=connection_uid,
            connection_revision=connection_revision,secret_version_ref=secret_version_ref,
            plugin_id=plugin_id,plugin_version=plugin_version,driver_id=driver_id,claim_revision=0,exec_data={},
            cancellation_requested=False,resource_version=next_resource_version(),created_at=utc_now(),updated_at=utc_now())
        statement=statement.on_conflict_do_nothing(index_elements=["server_scope","request_id"]).returning(operations)
        row=self.session.execute(statement).mappings().one_or_none()
        if row is None:
            existing=self.session.execute(select(operations).where(and_(operations.c.server_scope==server_scope,
                operations.c.request_id==request_id))).mappings().one()
            if existing["request_digest"]!=digest: raise RequestConflict(request_id)
            return dict(existing)
        result=dict(row); _event(self.session,"Operation",uid,result["resource_version"],"ADDED"); return result


class OperationExecutorRepository:
    """Executor owns technical facts only and has no Environment write API."""
    owned_operation_columns=frozenset({"phase","lease_owner","lease_until","claim_revision","external_task_ref",
        "remote_job_id","remote_status_path","remote_exit_path","remote_log_path","exec_data","result","error",
        "started_at","finished_at","resource_version","updated_at"})
    owned_resource_columns=frozenset({"existence_state","status","resource_version","updated_at"})
    def __init__(self,session): self.session=session

    def claim(self,uid,*,worker_id,lease_until):
        row=self.session.execute(update(operations).where(and_(operations.c.uid==uid,
            operations.c.phase.in_(("pending","running","pending_external")),
            or_(operations.c.lease_until.is_(None),operations.c.lease_until<utc_now())))
            .values(phase=case((operations.c.phase=="pending_external","pending_external"),else_="running"),
                lease_owner=worker_id,lease_until=lease_until,claim_revision=operations.c.claim_revision+1,
                started_at=func.coalesce(operations.c.started_at,utc_now()),
                resource_version=next_resource_version(),updated_at=utc_now())
            .returning(operations)).mappings().one_or_none()
        if row is None: return None
        result=dict(row); _event(self.session,"Operation",uid,result["resource_version"],"MODIFIED"); return result

    def update_execution(self,uid,*,worker_id,claim_revision,phase,external_task_ref=_UNSET,exec_data=_UNSET,
                         result=None,error=None,remote_job=None):
        if phase not in EXECUTOR_PHASES: raise ValueError(f"Executor 不允许写入 phase={phase}")
        values=dict(phase=phase,resource_version=next_resource_version(),updated_at=utc_now())
        if external_task_ref is not _UNSET: values["external_task_ref"]=external_task_ref
        if exec_data is not _UNSET: values["exec_data"]=exec_data
        if result is not None: values["result"]=result
        if error is not None: values["error"]=error
        if remote_job:
            allowed={"remote_job_id","remote_status_path","remote_exit_path","remote_log_path"}
            if set(remote_job)-allowed: raise ValueError("未知远端作业字段")
            values.update(remote_job)
        if phase in TERMINAL_PHASES: values.update(finished_at=utc_now(),lease_owner=None,lease_until=None)
        row=self.session.execute(update(operations).where(and_(operations.c.uid==uid,operations.c.lease_owner==worker_id,
            operations.c.claim_revision==claim_revision,operations.c.lease_until>utc_now()))
            .values(**values).returning(operations)).mappings().one_or_none()
        if row is None: raise LeaseLost(str(uid))
        out=dict(row); _event(self.session,"Operation",uid,out["resource_version"],"MODIFIED"); return out

    def update_resource_fact(self,uid,*,operation_uid,worker_id,claim_revision,existence_state,status):
        if existence_state not in EXISTENCE_STATES: raise ValueError("无效 existenceState")
        active_claim=exists(select(operations.c.uid).where(and_(operations.c.uid==operation_uid,
            operations.c.resource_uid==uid,operations.c.lease_owner==worker_id,
            operations.c.claim_revision==claim_revision,operations.c.lease_until>utc_now())))
        binding_fresh=exists(select(bindings.c.uid).where(and_(
            bindings.c.resource_uid==uid, bindings.c.active==True,
            bindings.c.uid==operations.c.binding_uid,
            bindings.c.revision==operations.c.binding_revision)))
        row=self.session.execute(update(resources).where(and_(resources.c.uid==uid,active_claim,binding_fresh))
            .values(existence_state=existence_state,status=status,
            resource_version=next_resource_version(),updated_at=utc_now()).returning(resources)).mappings().one_or_none()
        if row is None: raise LeaseLost(str(operation_uid))
        out=dict(row); _event(self.session,"Resource",uid,out["resource_version"],"MODIFIED"); return out
