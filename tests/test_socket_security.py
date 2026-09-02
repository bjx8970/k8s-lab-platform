"""SocketIO actions use real in-memory clients and isolated provider/SSH resources."""

import json
import threading
import time
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

from tests.test_security_contract import app_module as m
from tests.test_http_security import LiveUser
from modules.ssh_terminal import SSHManager, SSHSession


class FakeTimer:
    def __init__(self, interval, function, args=()):
        self.function, self.args, self.daemon = function, args, False
        self.cancelled = False

    def start(self):
        pass

    def cancel(self):
        self.cancelled = True


class SocketSecurityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.clients = []
        self.users = {
            uid: LiveUser(id=uid, username=f"user-{uid}", name=f"用户{uid}", role=role,
                          active=True, created_by=owner)
            for uid, role, owner in ((1, "admin", None), (2, "teacher", None), (3, "teacher", None),
                                     (4, "student", 2), (5, "student", 2), (6, "student", 3))
        }
        self.groups = {4: [10], 5: [10], 6: [20]}
        self.clusters = {
            "k8s_10": {"name": "k8s_10", "created_by": 2, "group_id": 10, "pve_server_id": 7,
                       "status": "running", "password": "FAKE-TEACHER",
                       "students": {"student1": {"password": "FAKE-STUDENT"}},
                       "vms": {"client": {"node": "node", "vmid": 101, "role": "client"}}},
            "k8s_20": {"name": "k8s_20", "created_by": 3, "group_id": 20, "pve_server_id": 8,
                       "status": "running", "vms": {"client": {"node": "node", "vmid": 101, "role": "client"}}},
        }
        self.stack.enter_context(patch("modules.db.session_scope", side_effect=AssertionError("DB access forbidden")))
        self.stack.enter_context(patch("modules.ssh_terminal.db_get_config", return_value=None))
        self.stack.enter_context(patch.object(m, "get_user", side_effect=lambda uid: self.users.get(int(uid)) if uid is not None else None))
        self.stack.enter_context(patch.object(m, "get_student_group_ids", side_effect=lambda uid: self.groups.get(uid, [])))
        self.stack.enter_context(patch.object(m, "get_cluster", side_effect=lambda name: self.clusters.get(name)))
        self.stack.enter_context(patch.object(m, "list_group_members", side_effect=lambda gid: [
            {"id": uid} for uid, gids in self.groups.items() if gid in gids]))
        self.stack.enter_context(patch.object(m, "get_pve_server", return_value={"ow_host": "mock-host"}))
        self.stack.enter_context(patch.object(m, "get_group_member", return_value={"student_number": 1}))
        self.provider = MagicMock()
        self.get_provider = self.stack.enter_context(patch.object(m, "get_pve_client", return_value=self.provider))
        self.stack.enter_context(patch.object(m, "update_vm_status"))
        self.stack.enter_context(patch.object(m.threading, "Timer", FakeTimer))
        self.audit = self.stack.enter_context(patch.object(m, "security_audit"))
        callback = m.login_manager._user_callback
        self.stack.callback(m.login_manager.user_loader, callback)
        m.login_manager.user_loader(lambda uid: self.users.get(int(uid)))
        self.manager = SSHManager(m.socketio)
        self.manager.set_authorizer(m._authorize_webssh_binding)
        self.manager.set_on_takeover_released(m._on_takeover_released)
        self.stack.enter_context(patch.object(m, "ssh_manager", self.manager))
        for registry in (m._online_users, m._state_sid_users, m._webssh_sid_users,
                         m._webssh_connect_times, m._vm_shutdown_votes, m._vm_shutdown_pending_vms):
            registry.clear()

    def tearDown(self):
        for client, namespace in self.clients:
            if client.is_connected(namespace):
                client.disconnect(namespace=namespace)
        self.manager.stop()
        for registry in (m._online_users, m._state_sid_users, m._webssh_sid_users,
                         m._webssh_connect_times, m._vm_shutdown_votes, m._vm_shutdown_pending_vms):
            registry.clear()
        self.stack.close()

    def socket(self, uid, namespace="/webssh"):
        client = m.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"], session["_fresh"] = str(uid), True
        registry = m._webssh_sid_users if namespace == "/webssh" else m._state_sid_users
        before = set(registry)
        socket_client = m.socketio.test_client(m.app, namespace=namespace, flask_test_client=client)
        self.assertTrue(socket_client.is_connected(namespace))
        self.clients.append((socket_client, namespace))
        sid = (set(registry) - before).pop()
        socket_client.get_received(namespace)
        return socket_client, sid

    def terminal(self, owner=4, actor=None, role="owner"):
        actor = owner if actor is None else actor
        client, sid = self.socket(actor)
        user = self.users[owner]
        terminal = self.manager.create_session("k8s_10", {
            "user_id": owner, "username": user.username, "role": user.role,
        }, "mock-host", 22, "mock-user", "mock-password")
        terminal.channel = MagicMock(closed=False)
        terminal.status = "connected"
        terminal.log_buffer.append("previous output\n")
        self.assertTrue(self.manager.bind_ws(sid, terminal.session_id, role))
        return terminal, client, sid

    @staticmethod
    def names(client, namespace="/webssh"):
        return [item["name"] for item in client.get_received(namespace)]

    def assert_audit(self, action, outcome):
        self.assertTrue(any(call.args[:2] == (action, outcome) for call in self.audit.call_args_list),
                        (action, outcome, self.audit.call_args_list))

    def test_vm_identity_is_bound_to_authorized_cluster_and_provider(self):
        teacher, _ = self.socket(2, "/state")
        for data in ({"cluster_name": "k8s_20", "node": "node", "vmid": 101},
                     {"cluster_name": "k8s_10", "node": "node", "vmid": 999}):
            teacher.emit("vm_shutdown_initiate", data, namespace="/state")
            self.assertIn("vm_shutdown_error", self.names(teacher, "/state"))
            self.get_provider.assert_not_called()
        teacher.emit("vm_shutdown_initiate", {"cluster_name": "k8s_10", "node": "node", "vmid": 101}, namespace="/state")
        self.assertIn("vm_shutdown_allowed", self.names(teacher, "/state"))
        self.get_provider.assert_called_once_with(7)
        self.provider.stop_vm.assert_called_once_with("node", 101)
        self.assert_audit("vm_shutdown.initiate", "denied")
        self.assert_audit("vm_shutdown.execute", "success")

    def test_cluster_shutdown_uses_current_owner_and_provider(self):
        other_teacher, _ = self.socket(3, "/state")
        other_teacher.emit("vm_shutdown_cluster_initiate", {"cluster_name": "k8s_10"}, namespace="/state")
        self.get_provider.assert_not_called()
        teacher, _ = self.socket(2, "/state")
        teacher.emit("vm_shutdown_cluster_initiate", {"cluster_name": "k8s_10"}, namespace="/state")
        self.assertIn("vm_shutdown_allowed", self.names(teacher, "/state"))
        self.get_provider.assert_called_once_with(7)
        self.provider.stop_vm.assert_called_once_with("node", 101)
        self.assert_audit("vm_shutdown.cluster_initiate", "denied")
        self.assert_audit("vm_shutdown.cluster_initiate", "success")

    def start_vote(self):
        initiator, _ = self.socket(4, "/state")
        voter, _ = self.socket(5, "/state")
        initiator.emit("vm_shutdown_initiate", {"cluster_name": "k8s_10", "node": "node", "vmid": 101}, namespace="/state")
        self.assertIn("vm_shutdown_pending", self.names(initiator, "/state"))
        vote_id = next(iter(m._vm_shutdown_votes))
        self.get_provider.assert_not_called()
        return initiator, voter, vote_id

    def test_vote_notifications_and_valid_completion_are_scoped(self):
        outsiders = [self.socket(uid, "/state")[0] for uid in (3, 6)]
        admin, _ = self.socket(1, "/state")
        initiator, voter, vote_id = self.start_vote()
        for outsider in outsiders:
            self.assertFalse(any(name.startswith("vm_shutdown_") for name in self.names(outsider, "/state")))
        self.assertIn("vm_shutdown_request", self.names(voter, "/state"))
        voter.emit("vm_shutdown_vote", {"vote_id": vote_id, "agree": True}, namespace="/state")
        self.provider.stop_vm.assert_called_once_with("node", 101)
        self.assertIn("vm_shutdown_proceed", self.names(initiator, "/state"))
        self.assertIn("vm_shutdown_proceed", self.names(admin, "/state"))
        for outsider in outsiders:
            self.assertFalse(any(name.startswith("vm_shutdown_") for name in self.names(outsider, "/state")))
        self.assertEqual(m._vm_shutdown_pending_vms, {})
        self.assert_audit("vm_shutdown.vote", "success")

    def test_timer_rechecks_membership_and_clears_provider_qualified_key(self):
        _initiator, _voter, vote_id = self.start_vote()
        self.assertIn(("vm", 7, "node", 101), m._vm_shutdown_pending_vms)
        self.groups[4] = []
        m._on_vote_timeout(vote_id)
        self.get_provider.assert_not_called()
        self.assertEqual(m._vm_shutdown_pending_vms, {})
        self.assert_audit("vm_shutdown.execute", "denied")

    def test_timer_rechecks_provider_and_vm_identity(self):
        _initiator, _voter, vote_id = self.start_vote()
        self.clusters["k8s_10"]["pve_server_id"] = 8
        m._on_vote_timeout(vote_id)
        self.get_provider.assert_not_called()
        self.assert_audit("vm_shutdown.execute", "denied")

    def test_timer_rechecks_vm_identity(self):
        _initiator, _voter, vote_id = self.start_vote()
        self.clusters["k8s_10"]["vms"]["client"]["vmid"] = 102
        m._on_vote_timeout(vote_id)
        self.get_provider.assert_not_called()
        self.assert_audit("vm_shutdown.execute", "denied")

    def test_timer_rechecks_disabled_initiator(self):
        _initiator, _voter, vote_id = self.start_vote()
        self.users[4].active = False
        m._on_vote_timeout(vote_id)
        self.get_provider.assert_not_called()
        self.assert_audit("vm_shutdown.execute", "denied")

    def test_vote_cancel_rechecks_authority_and_audits(self):
        initiator, voter, vote_id = self.start_vote()
        outsider, _ = self.socket(3, "/state")
        outsider.emit("vm_shutdown_cancel", {"vote_id": vote_id}, namespace="/state")
        self.assert_audit("vm_shutdown.cancel", "denied")
        self.groups[5] = []
        voter.emit("vm_shutdown_vote", {"vote_id": vote_id, "agree": True}, namespace="/state")
        self.assert_audit("vm_shutdown.vote", "denied")
        initiator.emit("vm_shutdown_cancel", {"vote_id": vote_id}, namespace="/state")
        self.assert_audit("vm_shutdown.cancel", "success")
        self.get_provider.assert_not_called()
        self.assertEqual(m._vm_shutdown_pending_vms, {})

    def test_valid_create_reconnect_input_resize_and_replay(self):
        client, sid = self.socket(4)
        def connect(terminal, socketio):
            terminal.channel = MagicMock(closed=False)
            terminal.status = "connected"
        with patch.object(SSHSession, "connect", connect):
            client.emit("session_create", {"cluster": "k8s_10"}, namespace="/webssh")
        self.assertIn("session_created", self.names(client))
        terminal = self.manager.get_session_by_cluster(4, "k8s_10")
        self.assertIsNotNone(terminal)
        client.emit("ssh_data", {"data": "id\n"}, namespace="/webssh")
        terminal.channel.send.assert_called_once_with("id\n")
        client.emit("ssh_resize", {"cols": 100, "rows": 40}, namespace="/webssh")
        terminal.channel.resize_pty.assert_called_once_with(width=100, height=40)
        terminal.log_buffer.append("safe history")
        client.emit("session_reconnect", {"cluster": "k8s_10"}, namespace="/webssh")
        names = self.names(client)
        self.assertIn("session_reconnected", names)
        self.assertIn("log_replay", names)
        self.assert_audit("webssh.reconnect", "success")
        client.emit("session_terminate", {"cluster": "k8s_10"}, namespace="/webssh")
        self.assertIn("session_terminated", self.names(client))
        self.assertIsNone(self.manager.get_session(terminal.session_id))
        self.assert_audit("webssh.terminate_own", "success")

    def test_view_started_automatic_resize_preserves_readonly_connection(self):
        terminal, owner, _ = self.terminal()
        teacher, sid = self.socket(2)
        teacher.emit("view_start", {"session_id": terminal.session_id}, namespace="/webssh")
        self.assertIn("view_started", self.names(teacher))
        teacher.emit("ssh_resize", {"cols": 100, "rows": 40}, namespace="/webssh")
        terminal.channel.resize_pty.assert_not_called()
        self.assertNotIn("ssh_error", self.names(teacher))
        self.assertTrue(teacher.is_connected("/webssh"))
        terminal._emit(m.socketio, "ssh_output", {"data": "visible output"}, to=sid)
        self.assertIn("ssh_output", self.names(teacher))
        self.assert_audit("webssh.resize", "ignored")
        self.users[2].role = "student"
        teacher.emit("ssh_resize", {"cols": 100, "rows": 40}, namespace="/webssh")
        terminal._emit(m.socketio, "ssh_output", {"data": "hidden output"}, to=sid)
        self.assertNotIn("ssh_output", self.names(teacher))
        self.assertNotIn(sid, self.manager._ws_to_session)
        terminal.channel.resize_pty.assert_not_called()
        self.assert_audit("webssh.resize", "denied")

    def test_terminate_by_owner_finishes_without_nested_lock_deadlock(self):
        manager = SSHManager()
        terminal = manager.create_session("isolated", {"user_id": 4, "role": "student"},
                                          "mock-host", 22, "mock-user", "mock-password")
        completed = threading.Event()
        result = []
        def terminate():
            try:
                result.append(manager.terminate_by_owner(4, "isolated"))
            finally:
                completed.set()
        worker = threading.Thread(target=terminate, daemon=True)
        worker.start()
        self.assertTrue(completed.wait(1), "terminate_by_owner must release the lookup lock before terminate_session")
        self.assertEqual(result, [True])
        self.assertIsNone(manager.get_session(terminal.session_id))

    def assert_revoked(self, terminal, client, sid):
        client.get_received("/webssh")
        terminal.channel.reset_mock()
        client.emit("ssh_data", {"data": "DO_NOT_LOG_SECRET_INPUT"}, namespace="/webssh")
        client.emit("ssh_resize", {"cols": 100, "rows": 40}, namespace="/webssh")
        terminal.channel.send.assert_not_called()
        terminal.channel.resize_pty.assert_not_called()
        self.assertFalse(self.manager.replay(sid))
        self.assertEqual(terminal.get_log_replay(sid), "")
        terminal._emit(m.socketio, "ssh_output", {"data": "hidden"}, to=sid)
        self.assertNotIn(sid, terminal._get_all_targets())
        names = self.names(client)
        self.assertNotIn("ssh_output", names)
        self.assertNotIn("log_replay", names)
        self.assertNotIn(sid, self.manager._ws_to_session)
        self.assert_audit("webssh.write", "denied")
        self.assertNotIn("DO_NOT_LOG_SECRET_INPUT", str(self.audit.call_args_list))

    def test_student_unassigned_loses_existing_channel_and_reconnect(self):
        terminal, client, sid = self.terminal()
        self.groups[4] = []
        self.assert_revoked(terminal, client, sid)
        client.emit("session_reconnect", {"cluster": "k8s_10"}, namespace="/webssh")
        self.assertNotIn("session_reconnected", self.names(client))
        self.assert_audit("webssh.reconnect", "denied")

    def test_disabled_owner_loses_existing_channel(self):
        terminal, client, sid = self.terminal()
        self.users[4].active = False
        self.assert_revoked(terminal, client, sid)

    def test_teacher_cluster_reassigned_loses_existing_channel(self):
        terminal, client, sid = self.terminal(owner=2)
        self.clusters["k8s_10"]["created_by"] = 3
        self.assert_revoked(terminal, client, sid)

    def test_observer_owner_teacher_changed_loses_output_and_replay(self):
        terminal, client, sid = self.terminal(actor=2, role="viewer")
        self.users[4].created_by = 3
        self.assert_revoked(terminal, client, sid)

    def test_observer_demotion_loses_output_and_replay(self):
        terminal, client, sid = self.terminal(actor=2, role="viewer")
        self.users[2].role = "student"
        self.assert_revoked(terminal, client, sid)

    def test_disabled_owner_also_revokes_observers(self):
        terminal, client, sid = self.terminal(actor=2, role="viewer")
        self.users[4].active = False
        self.assert_revoked(terminal, client, sid)

    def test_offline_owner_takeover_never_broadcasts(self):
        terminal, owner, owner_sid = self.terminal()
        owner.disconnect(namespace="/webssh")
        self.assertIsNone(terminal.owner_sid)
        unrelated = [self.socket(uid)[0] for uid in (3, 6)]
        teacher, sid = self.socket(2)
        teacher.emit("takeover_start", {"session_id": terminal.session_id}, namespace="/webssh")
        self.assertIn("takeover_started", self.names(teacher))
        self.assertEqual(terminal.takeover_sid, sid)
        for client in unrelated:
            self.assertEqual(self.names(client), [])
        terminal._emit(m.socketio, "takeover_notify", {}, to=None)
        for client in unrelated:
            self.assertEqual(self.names(client), [])

    def test_control_release_and_reconnect_response_use_live_authority(self):
        terminal, owner, _ = self.terminal()
        teacher, teacher_sid = self.socket(2)
        data = {"session_id": terminal.session_id}
        teacher.emit("takeover_start", data, namespace="/webssh")
        owner.emit("reconnect_request", data, namespace="/webssh")
        self.assertIn("reconnect_request_notify", self.names(teacher))
        teacher.emit("reconnect_response", {**data, "action": "accept"}, namespace="/webssh")
        self.assertFalse(terminal.takeover_active)
        self.assertIn("reconnect_response", self.names(owner))
        self.assert_audit("webssh.control_response", "success")
        teacher.emit("view_start", data, namespace="/webssh")
        self.assertIn("view_started", self.names(teacher))
        teacher.emit("view_stop", data, namespace="/webssh")
        self.assert_audit("webssh.view_stop", "success")
        teacher.emit("takeover_start", data, namespace="/webssh")
        self.users[2].role = "student"
        teacher.emit("takeover_stop", data, namespace="/webssh")
        self.assert_audit("webssh.takeover_stop", "denied")
        self.assertNotIn(teacher_sid, self.manager._ws_to_session)

    def test_demotion_stops_admin_task_and_session_updates(self):
        admin, _ = self.socket(1, "/state")
        teacher, _ = self.socket(2, "/state")
        unrelated, _ = self.socket(3, "/state")
        self.users[1].role = "student"
        for client in (admin, teacher, unrelated):
            client.get_received("/state")
        with patch.object(m, "get_task_status", return_value={"created_by": 2, "owner_teacher_id": 2}):
            m._emit_task_update("job", "running", 50, "safe", 2, "create")
        self.assertIn("task_update", self.names(teacher, "/state"))
        self.assertNotIn("task_update", self.names(admin, "/state"))
        self.assertNotIn("task_update", self.names(unrelated, "/state"))
        m._emit_session_update("k8s_10", 4, "created")
        self.assertIn("session_update", self.names(teacher, "/state"))
        self.assertNotIn("session_update", self.names(admin, "/state"))
        self.assertNotIn("session_update", self.names(unrelated, "/state"))

    def test_default_and_exception_authorizers_fail_closed(self):
        terminal, client, sid = self.terminal()
        self.manager.set_authorizer(lambda *_: (_ for _ in ()).throw(RuntimeError("mock DB unavailable")))
        self.assertFalse(self.manager.write(sid, "id\n"))
        terminal.channel.send.assert_not_called()
        self.assertEqual(terminal._get_all_targets(), set())
        self.assertFalse(self.manager.replay(sid))
        self.manager.set_authorizer(None)
        self.assertFalse(self.manager.bind_ws(sid, terminal.session_id))

    def test_private_key_stream_is_redacted_before_buffer_and_output(self):
        terminal, client, sid = self.terminal()
        terminal.channel.recv.side_effect = [b"normal\n-----BE", b"GIN OPENSSH PRIVATE KEY-----\n",
                                             b"FAKE_PRIVATE_BODY\n-----END OPENSSH PRIVATE KEY-----\nafter\n", b""]
        terminal._reader(m.socketio)
        wire = json.dumps(client.get_received("/webssh"))
        self.assertNotIn("FAKE_PRIVATE_BODY", wire)
        self.assertNotIn("FAKE_PRIVATE_BODY", "".join(terminal.log_buffer))
        self.assertIn("after", wire)


if __name__ == "__main__":
    unittest.main()
