"""Column-owned repositories. Callers own the transaction and commit."""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, case, exists, func, insert, or_, select, update
from sqlalchemy.exc import IntegrityError
from .tables import bindings, connections, environments, next_resource_version, operations, outbox, resources, utc_now

FINALIZER = "lab.platform/environment-cleanup"
TERMINAL_PHASES = {"succeeded","failed","cancelled","unknown"}
EXECUTOR_PHASES = {"running","pending_external","succeeded","failed","cancelling","cancelled","unknown"}
EXISTENCE_STATES = {"pending","present","absent","unknown"}

_UNSET = object()  # sentinel: distinguish "not provided" from "explicitly clear"

UQ_TRANSPORT = "uq_rf_operation_transport"
UQ_PLAN_ITEM = "uq_rf_operation_plan_item"
UQ_MUTATION = "uq_rf_operation_mutation"


class ResourceVersionConflict(RuntimeError): pass
class RequestConflict(RuntimeError): pass
class LeaseLost(RuntimeError): pass


def canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",",":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _constraint_name(exc):
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None) if diag else None


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

    def _resolve_target(self, resource_uid, binding_uid, binding_revision, connection_uid, connection_revision,
                        driver_id, plugin_id, plugin_version, *, require_active):
        """Load and verify Resource/Binding/Connection, then build the canonical target snapshot."""
        if None in (resource_uid,binding_uid,binding_revision,connection_uid,connection_revision):
            raise ValueError("Operation 必须固定 binding/connection revision")
        if not plugin_id: raise ValueError("plugin_id 不能为空")
        if not plugin_version: raise ValueError("plugin_version 不能为空")
        if not driver_id: raise ValueError("driver_id 不能为空")

        resource=self.session.execute(select(resources).where(
            resources.c.uid==resource_uid).with_for_update()).mappings().one_or_none()
        if resource is None: raise ValueError("Resource 不存在")
        if resource["driver_id"] != driver_id:
            raise ValueError("driver_id 与 Resource 不一致")

        binding_conditions = and_(bindings.c.uid==binding_uid, bindings.c.resource_uid==resource_uid,
            bindings.c.revision==binding_revision)
        if require_active:
            binding_conditions = and_(binding_conditions, bindings.c.active==True)
        binding=self.session.execute(select(bindings).where(binding_conditions)
            .with_for_update()).mappings().one_or_none()
        if binding is None: raise ValueError("Binding 不存在/不属于该 Resource/版本不匹配/不活跃")
        if binding["driver_id"] != driver_id:
            raise ValueError("driver_id 与 Binding 不一致")
        if binding["connection_uid"] != connection_uid:
            raise ValueError("Binding 不属于指定 Connection")

        conn_conditions = and_(connections.c.uid==connection_uid, connections.c.revision==connection_revision)
        if require_active:
            conn_conditions = and_(conn_conditions, connections.c.active==True)
        conn=self.session.execute(select(connections).where(conn_conditions)
            .with_for_update()).mappings().one_or_none()
        if conn is None: raise ValueError("Connection 不存在/版本不匹配/不活跃")
        if binding["domain_id"] != conn["domain_id"]:
            raise ValueError("Binding domain 与 Connection domain 不一致")
        if not conn["secret_version_ref"]:
            raise ValueError("Connection 缺少 secret_version_ref")
        if not isinstance(binding["locator"], dict):
            raise ValueError("Binding locator 必须为对象")

        return {
            "resourceId": str(resource_uid),
            "bindingId": str(binding_uid),
            "bindingRevision": int(binding_revision),
            "domainId": binding["domain_id"],
            "connectionId": str(connection_uid),
            "connectionRevision": int(connection_revision),
            "secretVersionRef": conn["secret_version_ref"],
            "pluginId": plugin_id,
            "pluginVersion": plugin_version,
            "driverId": driver_id,
            "externalIdentity": {"externalKey": binding["external_key"]},
            "locator": binding["locator"],
        }

    def create(self, *, server_scope, request_id, action, normalized_input, plugin_id, plugin_version,
               driver_id, resource_uid, binding_uid, binding_revision, connection_uid,
               connection_revision, source_type, correlation_id,
               is_mutating=True, operation_key=None, attempt=None, uid=None):
        # --- source_type validation ---
        if source_type not in ("plan", "direct"):
            raise ValueError("source_type 必须为 plan 或 direct")
        if source_type == "plan":
            if operation_key is None or attempt is None:
                raise ValueError("plan Operation 必须提供 operation_key 和 attempt")
            if not isinstance(attempt, int) or attempt < 0:
                raise ValueError("attempt 必须为非负整数")
        else:
            if operation_key is not None or attempt is not None:
                raise ValueError("direct Operation 不得携带 operation_key/attempt")
        if not correlation_id:
            raise ValueError("correlation_id 不能为空")

        # Historical relationship is validated first so an already-created Operation can be replayed
        # even after its Binding was retired; active checks happen only for a real new admission.
        snapshot=self._resolve_target(resource_uid, binding_uid, binding_revision, connection_uid,
            connection_revision, driver_id, plugin_id, plugin_version, require_active=False)
        digest=canonical_digest({"action":action,"input":normalized_input,"target":snapshot})

        def _replay_check(existing, where):
            if existing["request_digest"]!=digest or existing["source_type"]!=source_type:
                raise RequestConflict(where)
            if source_type=="plan" and (existing["operation_key"]!=operation_key or existing["attempt"]!=attempt):
                raise RequestConflict(where)
            return dict(existing)

        # --- transport dedup ---
        existing=self.session.execute(select(operations).where(and_(operations.c.server_scope==server_scope,
            operations.c.request_id==request_id))).mappings().one_or_none()
        if existing: return _replay_check(existing, request_id)
        # --- plan-item dedup ---
        if source_type == "plan":
            plan_existing = self.session.execute(select(operations).where(and_(
                operations.c.operation_key==operation_key, operations.c.attempt==attempt))).mappings().one_or_none()
            if plan_existing: return _replay_check(plan_existing, f"operation_key={operation_key}/attempt={attempt}")

        # --- new admission: require a currently usable target ---
        snapshot=self._resolve_target(resource_uid, binding_uid, binding_revision, connection_uid,
            connection_revision, driver_id, plugin_id, plugin_version, require_active=True)
        digest=canonical_digest({"action":action,"input":normalized_input,"target":snapshot})

        uid=uid or uuid4()
        values=dict(uid=uid,resource_uid=resource_uid,action=action,phase="pending",
            is_mutating=is_mutating,normalized_input=normalized_input,server_scope=server_scope,request_id=request_id,
            request_digest=digest,operation_key=operation_key,attempt=attempt,source_type=source_type,
            correlation_id=correlation_id,target_snapshot=snapshot,
            binding_uid=binding_uid,binding_revision=binding_revision,connection_uid=connection_uid,
            connection_revision=connection_revision,secret_version_ref=snapshot["secretVersionRef"],
            plugin_id=plugin_id,plugin_version=plugin_version,driver_id=driver_id,claim_revision=0,exec_data={},
            cancellation_requested=False,resource_version=next_resource_version(),created_at=utc_now(),updated_at=utc_now())
        try:
            with self.session.begin_nested():
                row=self.session.execute(insert(operations).values(**values).returning(operations)).mappings().one()
        except IntegrityError as exc:
            name=_constraint_name(exc)
            if name==UQ_TRANSPORT:
                existing=self.session.execute(select(operations).where(and_(operations.c.server_scope==server_scope,
                    operations.c.request_id==request_id))).mappings().one()
                return _replay_check(existing, request_id)
            if name==UQ_PLAN_ITEM:
                existing=self.session.execute(select(operations).where(and_(
                    operations.c.operation_key==operation_key, operations.c.attempt==attempt))).mappings().one()
                return _replay_check(existing, f"operation_key={operation_key}/attempt={attempt}")
            raise
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
        # Single correlated EXISTS: claim validity AND binding freshness must come from the SAME operation row.
        # This prevents O1(old binding) from passing because O2(new binding) satisfies the binding_fresh check.
        claim_and_fencing = exists(
            select(operations.c.uid)
            .select_from(operations.join(bindings, and_(
                bindings.c.uid == operations.c.binding_uid,
                bindings.c.revision == operations.c.binding_revision,
                bindings.c.resource_uid == operations.c.resource_uid,
                bindings.c.active == True
            )))
            .where(and_(
                operations.c.uid == operation_uid,
                operations.c.resource_uid == uid,
                operations.c.lease_owner == worker_id,
                operations.c.claim_revision == claim_revision,
                operations.c.lease_until > utc_now()
            ))
        )
        row=self.session.execute(update(resources).where(and_(resources.c.uid==uid, claim_and_fencing))
            .values(existence_state=existence_state,status=status,
            resource_version=next_resource_version(),updated_at=utc_now()).returning(resources)).mappings().one_or_none()
        if row is None: raise LeaseLost(str(operation_uid))
        out=dict(row); _event(self.session,"Resource",uid,out["resource_version"],"MODIFIED"); return out
