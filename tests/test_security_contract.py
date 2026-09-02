import importlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask_login import UserMixin


class MemoryUser(UserMixin, SimpleNamespace):
    pass


USERS = {
    "1": MemoryUser(id=1, username="admin", role="admin", created_by=None),
    "2": MemoryUser(id=2, username="teacher-2", role="teacher", created_by=None),
    "3": MemoryUser(id=3, username="teacher-3", role="teacher", created_by=None),
    "4": MemoryUser(id=4, username="student-4", role="student", created_by=2),
}


def import_isolated_app():
    """Import app without database initialization or background monitoring."""
    real_exists = os.path.exists
    def safe_exists(path):
        if os.path.basename(os.fspath(path)) in {".db_config.json", ".secret_key", ".credential_key"}:
            return False
        return real_exists(path)
    with patch("os.path.exists", side_effect=safe_exists):
        import modules.db as db_module
        import modules.ssh_terminal as ssh_terminal_module
        import modules.status_cache as status_cache_module

    sys.modules.pop("app", None)
    with (
        patch.object(db_module, "is_db_configured", return_value=False),
        patch.object(ssh_terminal_module, "db_get_config", return_value=None),
        patch.object(status_cache_module, "start_monitor"),
        patch.object(ssh_terminal_module.SSHManager, "_start_cleanup_thread"),
    ):
        module = importlib.import_module("app")

    module.app.config.update(TESTING=True, WTF_CSRF_ENABLED=True)
    module.is_db_configured = lambda: True
    module._needs_setup = lambda: False
    module.login_manager.user_loader(lambda user_id: USERS.get(str(user_id)))
    module.get_user = lambda user_id: USERS.get(str(user_id))
    module.get_student_group_ids = lambda user_id: []
    return module


app_module = import_isolated_app()


class SecurityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source_path = Path(app_module.__file__).resolve()
        cls.template_path = cls.source_path.parent / "templates" / "k8s.html"

    def setUp(self):
        self._db_guard = patch("modules.db.session_scope", side_effect=AssertionError("real DB access forbidden"))
        self._db_guard.start()
        self.addCleanup(self._db_guard.stop)
        self._original_allowed_origins = list(app_module._allowed_origins)
        app_module._allowed_origins = []
        app_module._online_users.clear()
        app_module._webssh_connect_times.clear()
        app_module._state_sid_users.clear()
        app_module._webssh_sid_users.clear()

    def tearDown(self):
        app_module._allowed_origins = self._original_allowed_origins
        app_module._online_users.clear()
        app_module._webssh_connect_times.clear()

    def client_for(self, user_id=None):
        client = app_module.app.test_client()
        if user_id is not None:
            with client.session_transaction() as session:
                session["_user_id"] = str(user_id)
                session["_fresh"] = True
        return client

    def csrf_for(self, client):
        response = client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["csrf_token"]
        self.assertTrue(token)
        self.assertNotEqual(token, "[REDACTED]")
        return token

    def request_with_csrf(self, client, method, path, *, json_data=None):
        token = self.csrf_for(client)
        return client.open(
            path,
            method=method,
            json=json_data,
            headers={"X-CSRFToken": token},
        )

    def test_01_csrf_and_basic_authentication(self):
        client = self.client_for(2)
        token_response = client.get("/api/csrf-token")
        self.assertEqual(token_response.status_code, 200)
        token = token_response.get_json().get("csrf_token")
        self.assertTrue(token)
        self.assertNotEqual(token, "[REDACTED]")

        response = client.post("/api/logout")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "CSRF 校验失败"})

        response = client.post(
            "/api/logout", headers={"X-CSRFToken": token}
        )
        self.assertEqual(response.status_code, 200)

        anonymous = self.client_for()
        response = anonymous.get("/api/k8s/tasks")
        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.is_json)

    def test_02_http_origin_contract(self):
        client = self.client_for()
        evil_origin = "https://evil.example"
        response = client.get(
            "/api/csrf-token", headers={"Origin": evil_origin}
        )
        self.assertEqual(response.status_code, 403)

        allowed_origin = "https://allowed.example"
        app_module._allowed_origins = [allowed_origin]
        response = client.get(
            "/api/csrf-token", headers={"Origin": allowed_origin}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Origin"), allowed_origin
        )
        self.assertEqual(
            response.headers.get("Access-Control-Allow-Credentials"), "true"
        )
        self.assertIn("Origin", response.headers.get("Vary", ""))
        cors_values = "\n".join(
            value
            for key, value in response.headers.items()
            if key.lower().startswith("access-control-")
        )
        self.assertNotIn("*", cors_values)

        response = client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)

    def test_03_provider_write_permissions(self):
        clone_payload = {
            "node": "pve-1",
            "vmid": 100,
            "newid": 101,
            "name": "clone-101",
            "config": {},
        }
        for user_id in (2, 4):
            with self.subTest(provider="pve", user_id=user_id):
                client = self.client_for(user_id)
                with patch.object(app_module, "get_pve_client") as get_client:
                    response = self.request_with_csrf(
                        client,
                        "POST",
                        "/api/pve/clone",
                        json_data=clone_payload,
                    )
                self.assertEqual(response.status_code, 403)
                get_client.assert_not_called()

            with self.subTest(provider="openwrt", user_id=user_id):
                client = self.client_for(user_id)
                with patch.object(app_module, "get_openwrt_client") as get_client:
                    response = self.request_with_csrf(
                        client, "POST", "/api/openwrt/restart/network"
                    )
                self.assertEqual(response.status_code, 403)
                get_client.assert_not_called()

        admin = self.client_for(1)
        pve_client = MagicMock()
        pve_client.clone_template.return_value = 9001
        with patch.object(
            app_module, "get_pve_client", return_value=pve_client
        ):
            response = self.request_with_csrf(
                admin,
                "POST",
                "/api/pve/clone",
                json_data=clone_payload,
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["newid"], 9001)
        pve_client.clone_template.assert_called_once_with(
            "pve-1", 100, 101, "clone-101", {}
        )

    def test_04_vm_scope_and_multi_pve_delete(self):
        allowed_cluster = {
            "name": "k8s_10",
            "group_id": 10,
            "pve_server_id": 7,
        }
        pve_client = MagicMock()
        pve_client.start_vm.return_value = {"ok": True}
        student = self.client_for(4)
        with (
            patch.object(
                app_module, "find_cluster_by_vm", return_value=allowed_cluster
            ),
            patch.object(
                app_module, "get_student_group_ids", return_value=[10]
            ) as group_ids,
            patch.object(
                app_module, "get_pve_client", return_value=pve_client
            ),
            patch.object(app_module, "update_vm_status"),
        ):
            response = self.request_with_csrf(
                student, "POST", "/api/pve/vms/pve-node/101/start"
            )
        self.assertEqual(response.status_code, 200)
        group_ids.assert_called_with(4)
        pve_client.start_vm.assert_called_once_with("pve-node", 101)

        denied_client = MagicMock()
        student = self.client_for(4)
        with (
            patch.object(
                app_module,
                "find_cluster_by_vm",
                return_value={
                    "name": "k8s_11",
                    "group_id": 11,
                    "pve_server_id": 8,
                },
            ),
            patch.object(
                app_module, "get_student_group_ids", return_value=[10]
            ),
            patch.object(
                app_module, "get_pve_client", return_value=denied_client
            ) as get_client,
        ):
            response = self.request_with_csrf(
                student, "POST", "/api/pve/vms/pve-node/102/start"
            )
        self.assertEqual(response.status_code, 403)
        get_client.assert_not_called()
        denied_client.start_vm.assert_not_called()

        admin = self.client_for(1)
        delete_client = MagicMock()
        delete_client.release_vm.return_value = {"ok": True}
        with (
            patch.object(
                app_module,
                "find_cluster_by_vm",
                return_value={"name": "k8s_1", "pve_server_id": 7},
            ),
            patch.object(
                app_module, "get_pve_client", return_value=delete_client
            ) as get_client,
        ):
            response = self.request_with_csrf(
                admin, "DELETE", "/api/pve/vms/pve-node/103"
            )
        self.assertEqual(response.status_code, 200)
        get_client.assert_called_once_with(7)
        delete_client.release_vm.assert_called_once_with("pve-node", 103, True)

        admin = self.client_for(1)
        with (
            patch.object(app_module, "find_cluster_by_vm", return_value=None),
            patch.object(app_module, "get_pve_client") as get_client,
        ):
            response = self.request_with_csrf(
                admin, "DELETE", "/api/pve/vms/pve-node/404"
            )
        self.assertEqual(response.status_code, 404)
        get_client.assert_not_called()

    def test_05_task_ownership(self):
        task_id = "task-owned-by-2"
        task = {
            "task_id": task_id,
            "created_by": 2,
            "status": "running",
            "logs": [
                {
                    "message": "safe",
                    "private_key": "RAW_PRIVATE_KEY",
                    "password": "RAW_PASSWORD",
                }
            ],
        }
        with patch.object(app_module, "get_task_status", return_value=task):
            response = self.client_for(3).get(f"/api/k8s/tasks/{task_id}")
            self.assertEqual(response.status_code, 403)

            response = self.client_for(2).get(f"/api/k8s/tasks/{task_id}")
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertEqual(payload["logs"][0]["private_key"], "[REDACTED]")
            self.assertEqual(payload["logs"][0]["password"], "[REDACTED]")
            self.assertNotIn("RAW_PRIVATE_KEY", response.get_data(as_text=True))
            self.assertNotIn("RAW_PASSWORD", response.get_data(as_text=True))

            response = self.client_for(1).get(f"/api/k8s/tasks/{task_id}")
            self.assertEqual(response.status_code, 200)

        cancel_mock = MagicMock(return_value=True)
        non_owner = self.client_for(3)
        with (
            patch.object(app_module, "get_task_status", return_value=task),
            patch.object(app_module, "cancel_task", cancel_mock),
        ):
            response = self.request_with_csrf(
                non_owner, "POST", f"/api/k8s/tasks/{task_id}/cancel"
            )
        self.assertEqual(response.status_code, 403)
        cancel_mock.assert_not_called()

        cancel_mock = MagicMock(return_value=True)
        owner = self.client_for(2)
        with (
            patch.object(app_module, "get_task_status", return_value=task),
            patch.object(app_module, "cancel_task", cancel_mock),
        ):
            response = self.request_with_csrf(
                owner, "POST", f"/api/k8s/tasks/{task_id}/cancel"
            )
        self.assertEqual(response.status_code, 200)
        cancel_mock.assert_called_once_with(task_id, actor_id=2)

        owner = self.client_for(2)
        with (
            patch.object(app_module, "get_task_status", return_value=None),
            patch.object(app_module, "cancel_task") as cancel_mock,
        ):
            response = self.request_with_csrf(
                owner, "POST", "/api/k8s/tasks/missing/cancel"
            )
        self.assertEqual(response.status_code, 404)
        cancel_mock.assert_not_called()

        with (
            patch.object(app_module, "get_task_status", return_value=task),
            patch.object(app_module, "render_template", return_value="LOG PAGE"),
        ):
            response = self.client_for(3).get(f"/k8s/logs/{task_id}")
            self.assertEqual(response.status_code, 403)
            response = self.client_for(2).get(f"/k8s/logs/{task_id}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_data(as_text=True), "LOG PAGE")

    def test_06_credentials_do_not_leak(self):
        servers = [
            {
                "id": 7,
                "name": "pve-7",
                "token_value": "RAW_TOKEN",
                "ow_password": "RAW_PASSWORD",
            }
        ]
        with patch.object(app_module, "list_pve_servers", return_value=servers):
            response = self.client_for(2).get("/api/pve/servers")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("RAW_TOKEN", body)
        self.assertNotIn("RAW_PASSWORD", body)

        clusters = {
            "k8s_1": {
                "name": "k8s_1",
                "created_by": 2,
                "pve_server_id": 7,
                "ssh_private_key": "RAW_KEY",
                "password": "RAW_PASSWORD",
                "students": {
                    "student1": {"password": "RAW_STUDENT_PASSWORD"}
                },
            }
        }
        with (
            patch.object(app_module, "list_clusters", return_value=clusters),
            patch.object(
                app_module, "get_pve_server", return_value={"ow_host": "ow-7"}
            ),
        ):
            response = self.client_for(1).get("/api/k8s/clusters")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertNotIn("RAW_KEY", body)
        self.assertNotIn("RAW_PASSWORD", body)
        self.assertNotIn("RAW_STUDENT_PASSWORD", body)
        self.assertNotIn("ssh_private_key", response.get_json()["k8s_1"])

        response = self.client_for(1).get(
            "/api/k8s/clusters/k8s_1/ssh-key"
        )
        self.assertEqual(response.status_code, 404)

        template = self.template_path.read_text(encoding="utf-8")
        for forbidden in ("/ssh-key", "key-private", "private_key"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, template)

    def test_07_webssh_teacher_ownership_filtering(self):
        sessions = [
            {
                "session_id": "session-4",
                "cluster_name": "k8s_4",
                "owner": {
                    "user_id": 4,
                    "username": "student-4",
                    "role": "student",
                },
                "status": "connected",
            },
            {
                "session_id": "session-5",
                "cluster_name": "k8s_5",
                "owner": {
                    "user_id": 5,
                    "username": "student-5",
                    "role": "student",
                },
                "status": "connected",
            },
        ]
        session_users = {
            4: SimpleNamespace(id=4, created_by=2),
            5: SimpleNamespace(id=5, created_by=3),
        }
        with (
            patch.object(
                app_module.ssh_manager, "list_sessions", return_value=sessions
            ),
            patch.object(
                app_module,
                "get_user",
                side_effect=lambda user_id: session_users.get(user_id),
            ),
        ):
            teacher = self.client_for(2)
            for path in (
                "/api/webssh/sessions",
                "/api/teacher/student-sessions",
            ):
                with self.subTest(role="teacher", path=path):
                    response = teacher.get(path)
                    self.assertEqual(response.status_code, 200)
                    returned = response.get_json()["sessions"]
                    self.assertEqual(
                        [item["session_id"] for item in returned], ["session-4"]
                    )

            admin = self.client_for(1)
            for path in (
                "/api/webssh/sessions",
                "/api/teacher/student-sessions",
            ):
                with self.subTest(role="admin", path=path):
                    response = admin.get(path)
                    self.assertEqual(response.status_code, 200)
                    returned = response.get_json()["sessions"]
                    self.assertEqual(
                        [item["session_id"] for item in returned],
                        ["session-4", "session-5"],
                    )

            response = self.client_for(4).get(
                "/api/teacher/student-sessions"
            )
            self.assertEqual(response.status_code, 403)

    def test_08_socketio_authentication_and_origin(self):
        anonymous_flask_client = self.client_for()
        socket_client = app_module.socketio.test_client(
            app_module.app,
            namespace="/webssh",
            flask_test_client=anonymous_flask_client,
        )
        self.assertFalse(socket_client.is_connected(namespace="/webssh"))

        teacher_flask_client = self.client_for(2)
        with patch.object(app_module.socketio, "emit"):
            socket_client = app_module.socketio.test_client(
                app_module.app,
                namespace="/state",
                flask_test_client=teacher_flask_client,
            )
            self.assertTrue(socket_client.is_connected(namespace="/state"))
            socket_client.disconnect(namespace="/state")
        app_module._online_users.clear()

        response = self.client_for().get(
            "/socket.io/?EIO=4&transport=polling",
            headers={"Origin": "https://evil.example"},
        )
        self.assertIn(response.status_code, (400, 403))
        self.assertNotEqual(response.status_code, 200)

    def test_09_security_audit_contract(self):
        clone_payload = {
            "node": "pve-1",
            "vmid": 100,
            "newid": 101,
            "name": "clone-101",
        }
        teacher = self.client_for(2)
        with (
            patch.object(app_module, "security_audit") as audit,
            patch.object(app_module, "get_pve_client") as get_client,
        ):
            response = self.request_with_csrf(
                teacher,
                "POST",
                "/api/pve/clone",
                json_data=clone_payload,
            )
        self.assertEqual(response.status_code, 403)
        get_client.assert_not_called()
        self.assertEqual(audit.call_count, 1)
        args, kwargs = audit.call_args
        self.assertEqual(args[:2], ("http.authorization", "denied"))
        self.assertEqual(
            kwargs["metadata"],
            {"method": "POST", "path": "/api/pve/clone", "status": 403},
        )
        serialized_audit = json.dumps(
            {"args": args, "kwargs": kwargs}, default=str
        ).lower()
        for forbidden in ("cookie", "token", "newid", "clone-101"):
            self.assertNotIn(forbidden, serialized_audit)

        task_id = "task-owned-by-2"
        owner = self.client_for(2)
        with (
            patch.object(
                app_module,
                "get_task_status",
                return_value={"task_id": task_id, "created_by": 2},
            ),
            patch.object(app_module, "cancel_task", return_value=True),
            patch.object(app_module, "security_audit") as audit,
        ):
            response = self.request_with_csrf(
                owner, "POST", f"/api/k8s/tasks/{task_id}/cancel"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(audit.call_count, 1)
        args, kwargs = audit.call_args
        self.assertEqual(args[:2], ("http.write", "success"))
        self.assertEqual(
            kwargs["metadata"],
            {
                "method": "POST",
                "path": f"/api/k8s/tasks/{task_id}/cancel",
                "status": 200,
            },
        )

    def test_10_static_security_regressions_and_openwrt_writes(self):
        source = self.source_path.read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"cors_allowed_origins\s*=\s*['\"]\*['\"]", source)
        )
        self.assertNotIn("@csrf.exempt", source)
        self.assertNotIn("/ssh-key", source)

        private_key_lines = [
            line for line in source.splitlines() if "ssh_private_key" in line
        ]
        self.assertTrue(private_key_lines)
        for line in private_key_lines:
            with self.subTest(private_key_line=line.strip()):
                self.assertTrue(
                    "pop(" in line or ("if k !=" in line and "for k" in line),
                    f"ssh_private_key 只能用于显式过滤: {line.strip()}",
                )

        cases = [
            ("/api/openwrt/config", "POST", {"host": "h", "username": "u", "password": "p"}),
            ("/api/openwrt/test", "POST", {"host": "h", "username": "u", "password": "p"}),
            ("/api/openwrt/vlans", "POST", {"name": "vlan100", "iface": "eth1", "vid": 100}),
            ("/api/openwrt/vlans/vlan100", "DELETE", None),
            ("/api/openwrt/interfaces", "POST", {"name": "lab", "device": "eth1.100"}),
            ("/api/openwrt/interfaces/lab", "PUT", {"ipaddr": "10.0.0.1"}),
            ("/api/openwrt/interfaces/lab", "DELETE", None),
            ("/api/openwrt/dhcp", "POST", {"name": "lab", "interface": "lab"}),
            ("/api/openwrt/dhcp/lab", "DELETE", None),
            ("/api/openwrt/dnsmasq", "POST", {"name": "lab", "interface": "lab", "listen_address": "10.0.0.1", "domain": "lab"}),
            ("/api/openwrt/dnsmasq/lab", "DELETE", None),
            ("/api/openwrt/firewall/lan/interfaces/lab", "POST", None),
            ("/api/openwrt/firewall/lan/interfaces/lab", "DELETE", None),
            ("/api/openwrt/restart/network", "POST", None),
            ("/api/openwrt/restart/firewall", "POST", None),
            ("/api/openwrt/restart/dnsmasq", "POST", None),
        ]
        expected_rules = {
            ("/api/openwrt/config", "POST"),
            ("/api/openwrt/test", "POST"),
            ("/api/openwrt/vlans", "POST"),
            ("/api/openwrt/vlans/<name>", "DELETE"),
            ("/api/openwrt/interfaces", "POST"),
            ("/api/openwrt/interfaces/<name>", "PUT"),
            ("/api/openwrt/interfaces/<name>", "DELETE"),
            ("/api/openwrt/dhcp", "POST"),
            ("/api/openwrt/dhcp/<name>", "DELETE"),
            ("/api/openwrt/dnsmasq", "POST"),
            ("/api/openwrt/dnsmasq/<name>", "DELETE"),
            ("/api/openwrt/firewall/<zone>/interfaces/<interface>", "POST"),
            ("/api/openwrt/firewall/<zone>/interfaces/<interface>", "DELETE"),
            ("/api/openwrt/restart/network", "POST"),
            ("/api/openwrt/restart/firewall", "POST"),
            ("/api/openwrt/restart/dnsmasq", "POST"),
        }
        actual_rules = {
            (rule.rule, method)
            for rule in app_module.app.url_map.iter_rules()
            if rule.rule.startswith("/api/openwrt/")
            for method in rule.methods
            if method in {"POST", "PUT", "DELETE"}
        }
        self.assertEqual(actual_rules, expected_rules)

        for user_id in (2, 4):
            client = self.client_for(user_id)
            token = self.csrf_for(client)
            openwrt_client = MagicMock()
            with (
                patch.object(
                    app_module,
                    "get_openwrt_client",
                    return_value=openwrt_client,
                ) as get_client,
                patch.object(app_module, "OpenWrtClient") as client_class,
            ):
                for path, method, json_data in cases:
                    with self.subTest(
                        user_id=user_id, method=method, path=path
                    ):
                        response = client.open(
                            path,
                            method=method,
                            json=json_data,
                            headers={"X-CSRFToken": token},
                        )
                        self.assertEqual(response.status_code, 403)
            get_client.assert_not_called()
            client_class.assert_not_called()
            self.assertEqual(openwrt_client.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
