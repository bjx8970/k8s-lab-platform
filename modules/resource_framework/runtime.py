"""Explicitly driven Operation worker; no import-time execution."""

import re
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select, update
from sqlalchemy.orm import Session

from modules.audit import sanitize
from modules.control_plane.repositories import LeaseLost, OperationExecutorRepository, _event
from modules.control_plane.tables import bindings, connections, next_resource_version, operations, resources, utc_now
from .handlers import OutcomeUnknown


class OperationWorker:
    def __init__(self, engine, registry, *, worker_id, lease_seconds=30):
        if lease_seconds < 3:
            raise ValueError("lease_seconds 至少为 3")
        self.engine = engine
        self.registry = registry
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds

    def _deadline(self):
        return datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)

    def _heartbeat(self, uid, claim_revision, stop, lost):
        while not stop.wait(self.lease_seconds / 3):
            try:
                with Session(self.engine) as session, session.begin():
                    OperationExecutorRepository(session).renew(uid, worker_id=self.worker_id,
                        claim_revision=claim_revision, lease_until=self._deadline())
            except Exception:
                lost.set()
                return

    def _target_valid(self, session, op):
        binding = session.execute(select(bindings).where(bindings.c.uid == op["binding_uid"])).mappings().one_or_none()
        connection = session.execute(select(connections).where(connections.c.uid == op["connection_uid"])).mappings().one_or_none()
        resource = session.execute(select(resources).where(resources.c.uid == op["resource_uid"])).mappings().one_or_none()
        snapshot = op["target_snapshot"]
        return bool(binding and connection and resource and binding["active"] and connection["active"]
            and snapshot["resourceId"] == str(resource["uid"])
            and snapshot["bindingId"] == str(binding["uid"])
            and snapshot["connectionId"] == str(connection["uid"])
            and snapshot["bindingRevision"] == op["binding_revision"]
            and snapshot["connectionRevision"] == op["connection_revision"]
            and snapshot["secretVersionRef"] == op["secret_version_ref"]
            and binding["revision"] == op["binding_revision"]
            and connection["revision"] == op["connection_revision"]
            and binding["connection_uid"] == connection["uid"]
            and binding["resource_uid"] == resource["uid"]
            and binding["domain_id"] == connection["domain_id"] == snapshot["domainId"]
            and binding["driver_id"] == resource["driver_id"] == op["driver_id"]
            and binding["external_key"] == snapshot["externalIdentity"]["externalKey"]
            and binding["locator"] == snapshot["locator"]
            and connection["secret_version_ref"] == op["secret_version_ref"]
            and snapshot["pluginId"] == op["plugin_id"]
            and snapshot["pluginVersion"] == op["plugin_version"])

    def _persist(self, uid, claim_revision, response):
        if response.phase not in {"pending_external", "succeeded", "failed", "unknown", "cancelled"}:
            raise ValueError("无效的 handler 结果")
        with Session(self.engine) as session, session.begin():
            repo = OperationExecutorRepository(session)
            op = session.execute(select(operations).where(operations.c.uid == uid)).mappings().one()
            if response.phase == "pending_external" and not (
                    response.external_task_ref or op["external_task_ref"]):
                raise ValueError("异步任务缺少 externalTaskRef")
            if response.existence_state is not None:
                repo.update_resource_fact(op["resource_uid"], operation_uid=uid,
                    worker_id=self.worker_id, claim_revision=claim_revision,
                    existence_state=response.existence_state, status=sanitize(response.output or {}))
                binding = session.execute(update(bindings).where(and_(
                    bindings.c.uid == op["binding_uid"], bindings.c.revision == op["binding_revision"],
                    bindings.c.active.is_(True)))
                    .values(existence_state=response.existence_state,
                        provisional=False if op["action"] == "create" and response.phase == "succeeded"
                            else bindings.c.provisional,
                        resource_version=next_resource_version()).returning(bindings)).mappings().one()
                _event(session, "Binding", binding["uid"], binding["resource_version"], "MODIFIED")
            options = {}
            if response.external_task_ref is not None:
                options["external_task_ref"] = response.external_task_ref
            if response.exec_data:
                options["exec_data"] = sanitize(response.exec_data)
            code = response.error_code
            if code and not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", code):
                code = "ProviderError"
            return repo.update_execution(uid, worker_id=self.worker_id, claim_revision=claim_revision,
                phase=response.phase, result=sanitize(response.output) if response.output else None,
                error={"code": code} if code else None, **options)

    def run_once(self):
        with Session(self.engine) as session:
            candidates = OperationExecutorRepository(session).candidates()
        for uid in candidates:
            with Session(self.engine) as session, session.begin():
                repo = OperationExecutorRepository(session)
                if repo.recover_uncertain(uid):
                    return uid
                op = repo.claim(uid, worker_id=self.worker_id, lease_until=self._deadline())
            if op is None:
                continue
            claim_revision = op["claim_revision"]
            try:
                with Session(self.engine) as session, session.begin():
                    if OperationExecutorRepository(session).cancel_before_submit(uid,
                            worker_id=self.worker_id, claim_revision=claim_revision):
                        return uid
                    valid = self._target_valid(session, op)
                if not valid:
                    phase = "unknown" if op["external_task_ref"] else "failed"
                    with Session(self.engine) as session, session.begin():
                        OperationExecutorRepository(session).update_execution(uid,
                            worker_id=self.worker_id, claim_revision=claim_revision, phase=phase,
                            error={"code":"TargetRevisionConflict"})
                    return uid
                try:
                    handler = self.registry.get(op["plugin_id"], op["plugin_version"], op["driver_id"])
                except ValueError:
                    with Session(self.engine) as session, session.begin():
                        OperationExecutorRepository(session).update_execution(uid,
                            worker_id=self.worker_id, claim_revision=claim_revision,
                            phase="unknown" if op["external_task_ref"] else "failed",
                            error={"code":"PluginUnavailable"})
                    return uid
                stop, lost = threading.Event(), threading.Event()
                heartbeat = threading.Thread(target=self._heartbeat,
                    args=(uid, claim_revision, stop, lost), daemon=True)
                heartbeat.start()
                try:
                    if op["external_task_ref"]:
                        response = handler.poll(op["target_snapshot"], op["external_task_ref"], op["exec_data"])
                    else:
                        response = handler.execute(op["target_snapshot"], op["action"], op["normalized_input"])
                except OutcomeUnknown:
                    from .handlers import ExecutionResult
                    response = ExecutionResult("unknown", error_code="ExecutionOutcomeUnknown")
                except Exception:
                    from .handlers import ExecutionResult
                    response = ExecutionResult("unknown" if not op["external_task_ref"] else "pending_external",
                        external_task_ref=op["external_task_ref"],
                        error_code="ExecutionOutcomeUnknown" if not op["external_task_ref"] else "PollUnavailable")
                finally:
                    stop.set()
                    heartbeat.join()
                if lost.is_set():
                    raise LeaseLost(str(uid))
                self._persist(uid, claim_revision, response)
            except LeaseLost:
                pass
            return uid
        return None

    def run_forever(self, stop_event, *, interval=1):
        while not stop_event.is_set():
            if self.run_once() is None:
                stop_event.wait(interval)
