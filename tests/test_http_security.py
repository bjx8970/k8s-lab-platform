"""HTTP authentication and real Engine.IO transport contracts, without a network."""

import json
import os
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask_login import UserMixin
from werkzeug.security import generate_password_hash

from tests.test_security_contract import app_module as m


class LiveUser(UserMixin, SimpleNamespace):
    @property
    def is_active(self):
        return self.active


class HttpSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password_hash = generate_password_hash("test-password")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.users = {
            1: LiveUser(id=1, username="admin", name="管理员", role="admin", active=True,
                        created_by=None, password_hash=self.password_hash),
            2: LiveUser(id=2, username="teacher", name="教师", role="teacher", active=True,
                        created_by=None, password_hash=self.password_hash),
        }
        self.stack.enter_context(patch("modules.db.session_scope", side_effect=AssertionError("DB access forbidden")))
        self.stack.enter_context(patch.object(m, "get_user", side_effect=lambda uid: self.users.get(int(uid))))
        self.stack.enter_context(patch.object(m, "get_user_by_username", side_effect=lambda name: next(
            (user for user in self.users.values() if user.username == name), None)))
        self.stack.enter_context(patch.object(m, "list_classes", return_value=[]))
        callback = m.login_manager._user_callback
        self.stack.callback(m.login_manager.user_loader, callback)
        m.login_manager.user_loader(lambda uid: self.users.get(int(uid)))
        self.stack.enter_context(patch.object(m, "_allowed_origins", ["https://dev.example"]))
        self.stack.enter_context(patch.object(m, "security_audit"))
        self.stack.enter_context(patch.object(m.socketio.server.eio, "start_background_task", return_value=MagicMock()))
        self.client = m.app.test_client()
        m._online_users.clear()
        m._state_sid_users.clear()
        m._webssh_sid_users.clear()
        self.addCleanup(m._online_users.clear)
        self.addCleanup(m._state_sid_users.clear)
        self.addCleanup(m._webssh_sid_users.clear)

    def csrf(self):
        response = self.client.get("/api/csrf-token")
        self.assertEqual(response.status_code, 200)
        token = response.get_json()["csrf_token"]
        self.assertTrue(token)
        self.assertNotEqual(token, "[REDACTED]")
        return token

    def login(self, username="admin", password="test-password"):
        return self.client.post("/api/login", json={"username": username, "password": password},
                                headers={"X-CSRFToken": self.csrf()})

    def test_real_login_current_user_socket_and_logout(self):
        self.assertEqual(self.client.get("/api/users/me").status_code, 401)
        self.assertEqual(self.login().status_code, 200)
        response = self.client.get("/api/users/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["id"], 1)
        self.assertNotIn("password", response.get_data(as_text=True))
        self.assertTrue(m.app.config["SESSION_COOKIE_HTTPONLY"])
        socket_client = m.socketio.test_client(m.app, namespace="/state", flask_test_client=self.client)
        self.assertTrue(socket_client.is_connected("/state"))
        socket_client.disconnect(namespace="/state")
        token = self.csrf()
        response = self.client.post("/api/logout", headers={"X-CSRFToken": token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/api/users/me").status_code, 401)
        anonymous_socket = m.socketio.test_client(m.app, namespace="/webssh", flask_test_client=self.client)
        self.assertFalse(anonymous_socket.is_connected("/webssh"))

    def test_login_csrf_password_disabled_and_role_errors(self):
        data = {"username": "admin", "password": "test-password"}
        self.assertEqual(self.client.post("/api/login", json=data).status_code, 400)
        self.assertEqual(self.client.post("/api/login", json=data, headers={"X-CSRFToken": "invalid"}).status_code, 400)
        self.assertEqual(self.login(password="wrong-password").status_code, 401)
        self.users[1].active = False
        self.assertEqual(self.login().status_code, 403)
        self.assertEqual(self.login(username="teacher").status_code, 200)
        with patch.object(m, "get_pve_client") as provider:
            response = self.client.post("/api/pve/clone", json={}, headers={"X-CSRFToken": self.csrf()})
        self.assertEqual(response.status_code, 403)
        provider.assert_not_called()
        self.users[2].active = False
        self.assertIn(self.client.get("/api/users/me").status_code, (401, 403))
        disabled_socket = m.socketio.test_client(m.app, namespace="/state", flask_test_client=self.client)
        self.assertFalse(disabled_socket.is_connected("/state"))

    def test_http_and_engineio_exact_origins_with_nonempty_allowlist(self):
        for origin, allowed in (("https://backend.example", True), ("https://dev.example", True),
                                ("https://evil.example", False), ("https://dev.example.evil", False),
                                (None, True)):
            with self.subTest(origin=origin):
                headers = {"Origin": origin} if origin else {}
                http = self.client.get("/api/csrf-token", base_url="https://backend.example", headers=headers)
                transport = self.client.get("/socket.io/?EIO=4&transport=polling", base_url="https://backend.example", headers=headers)
                self.assertEqual(http.status_code, 200 if allowed else 403)
                self.assertEqual(transport.status_code, 200 if allowed else 400)
                if allowed:
                    self.assertTrue(transport.get_data(as_text=True).startswith("0"))
                    sid = json.loads(transport.get_data(as_text=True)[1:])["sid"]
                    m.socketio.server.eio.sockets.pop(sid, None)
                if allowed and origin:
                    self.assertEqual(http.headers["Access-Control-Allow-Origin"], origin)
                    self.assertEqual(transport.headers["Access-Control-Allow-Origin"], origin)
                    self.assertEqual(transport.headers["Access-Control-Allow-Credentials"], "true")

    def test_forwarded_headers_cannot_create_an_allowed_origin(self):
        headers = {"Origin": "https://evil.example", "X-Forwarded-Host": "evil.example",
                   "X-Forwarded-Proto": "https"}
        for path in ("/api/csrf-token", "/socket.io/?EIO=4&transport=polling"):
            with self.subTest(path=path):
                response = self.client.get(path, base_url="https://backend.example", headers=headers)
                self.assertIn(response.status_code, (400, 403))
        for configured in ("*", "https://*.example", "https://user:pass@example", "https://example/path"):
            with self.subTest(configured=configured), patch.dict(os.environ, {"K8S_LAB_ALLOWED_ORIGINS": configured}):
                with self.assertRaises(RuntimeError):
                    m._load_allowed_origins()


if __name__ == "__main__":
    unittest.main()
