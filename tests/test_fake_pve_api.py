"""Simulator acceptance v1: real loopback HTTP, isolated in-memory state, no PVE access."""

import http.client
import json
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode, quote
from urllib.request import Request, urlopen

from scripts.fake_pve_api import make_server


class FakePveApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(port=0, auto_polls=None)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.state.reset()

    def request(self, method, path, data=None, status=200, form=False):
        body = None if data is None else (urlencode(data).encode() if form else json.dumps(data).encode())
        headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
        req = Request(self.base + path, data=body, method=method, headers=headers)
        try:
            response = urlopen(req, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            self.assertEqual(status, response.status)
            payload = json.load(response)
        if status != 200:
            self.assertIn("errors", payload)
        return payload["data"]

    def api(self, method, path, data=None, **kwargs):
        return self.request(method, "/api2/json" + path, data, **kwargs)

    def clone(self, vmid=101):
        return self.api("POST", "/nodes/pve/qemu/9000/clone", {"newid": vmid, "name": f"test-{vmid}", "full": 1})

    def finish(self, upid, result="OK"):
        return self.request("POST", "/_fake/tasks/" + quote(upid, safe=""), {"exitstatus": result})

    def create_vm(self, vmid=101):
        upid = self.clone(vmid)
        self.finish(upid)
        return upid

    def current(self, vmid=101):
        return self.api("GET", f"/nodes/pve/qemu/{vmid}/status/current")

    def power(self, action, **params):
        return self.api("POST", "/nodes/pve/qemu/101/status/" + action, params)

    def test_version_and_readonly_discovery(self):
        version = self.api("GET", "/version")
        self.assertEqual("7.4-20", version["version"])
        self.assertTrue(version["simulated"])
        self.assertEqual("pve", self.api("GET", "/nodes")[0]["node"])
        self.assertEqual(9000, self.api("GET", "/nodes/pve/qemu")[0]["vmid"])
        self.assertEqual(1, self.api("GET", "/cluster/resources", None)[0]["template"])
        self.assertEqual([], self.request("GET", "/_fake/state")["tasks"])

    def test_clone_is_async_and_does_not_start(self):
        upid = self.clone()
        self.assertTrue(upid.startswith("UPID:pve:"))
        self.assertEqual("running", self.api("GET", f"/nodes/pve/tasks/{upid}/status")["status"])
        self.api("GET", "/nodes/pve/qemu/101/config", status=404)
        self.finish(upid)
        self.assertEqual("stopped", self.current()["status"])
        self.assertEqual("OK", self.api("GET", f"/nodes/pve/tasks/{upid}/status")["exitstatus"])
        self.assertEqual("TASK OK", self.api("GET", f"/nodes/pve/tasks/{upid}/log")[-1]["t"])
        self.assertEqual(1, len(self.request("GET", "/_fake/state")["tasks"]))

    def test_power_and_delete_lifecycle(self):
        self.create_vm()
        start = self.power("start")
        self.assertEqual("stopped", self.current()["status"])
        self.finish(start)
        reboot = self.power("reboot")
        self.assertEqual("running", self.current()["status"])
        self.finish(reboot)
        self.api("DELETE", "/nodes/pve/qemu/101", status=409)
        self.assertEqual("running", self.current()["status"])
        self.finish(self.power("shutdown", forceStop=0))
        delete = self.api("DELETE", "/nodes/pve/qemu/101", {})
        self.assertEqual("stopped", self.current()["status"])
        self.finish(delete)
        self.api("GET", "/nodes/pve/qemu/101/status/current", status=404)
        self.assertEqual(5, len(self.request("GET", "/_fake/state")["tasks"]))

    def test_failed_graceful_shutdown_does_not_force_stop(self):
        self.create_vm()
        self.finish(self.power("start"))
        task = self.power("shutdown", forceStop=0)
        self.finish(task, "shutdown timeout")
        self.assertEqual("running", self.current()["status"])
        self.assertEqual("shutdown timeout", self.api("GET", f"/nodes/pve/tasks/{task}/status")["exitstatus"])
        self.assertEqual(3, len(self.request("GET", "/_fake/state")["tasks"]))
        self.finish(self.power("stop"))
        self.assertEqual("stopped", self.current()["status"])

    def test_sync_async_and_background_config_responses(self):
        self.create_vm()
        self.assertIsNone(self.api("PUT", "/nodes/pve/qemu/101/config", {"cores": 2}))
        task = self.api("POST", "/nodes/pve/qemu/101/config", {"memory": 1024})
        self.assertEqual(512, self.api("GET", "/nodes/pve/qemu/101/config")["memory"])
        self.finish(task)
        self.assertEqual(1024, self.api("GET", "/nodes/pve/qemu/101/config")["memory"])
        self.assertIsNone(self.api("POST", "/nodes/pve/qemu/101/config", {"cores": 3, "background_delay": 1}))
        self.assertEqual(3, self.api("GET", "/nodes/pve/qemu/101/config")["cores"])

    def test_current_pending_digest_and_public_key_encoding(self):
        self.create_vm()
        keys = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest+key/one a\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest+key/two b\n"
        self.api("PUT", "/nodes/pve/qemu/101/config", {"sshkeys": quote(keys, safe="")}, form=True)
        self.assertEqual(keys, self.api("GET", "/nodes/pve/qemu/101/config")["sshkeys"])
        digest = self.api("GET", "/nodes/pve/qemu/101/config")["digest"]
        self.finish(self.power("start"))
        self.api("PUT", "/nodes/pve/qemu/101/config", {"cores": 4, "digest": digest})
        self.assertEqual(4, self.api("GET", "/nodes/pve/qemu/101/config")["cores"])
        self.assertEqual(1, self.api("GET", "/nodes/pve/qemu/101/config?current=1")["cores"])
        self.assertIn({"key": "cores", "value": "1", "pending": "4"}, self.api("GET", "/nodes/pve/qemu/101/pending"))
        self.api("PUT", "/nodes/pve/qemu/101/config", {"cores": 5, "digest": digest}, status=409)
        self.finish(self.power("reboot"))
        self.assertEqual(4, self.api("GET", "/nodes/pve/qemu/101/config?current=1")["cores"])
        self.assertEqual([], self.api("GET", "/nodes/pve/qemu/101/pending"))

    def test_duplicate_vmid_and_busy_vm_are_rejected(self):
        task = self.clone()
        self.api("POST", "/nodes/pve/qemu/9000/clone", {"newid": 101}, status=409)
        self.finish(task)
        start = self.power("start")
        self.api("PUT", "/nodes/pve/qemu/101/config", {"memory": 1024}, status=409)
        self.assertEqual(2, len(self.request("GET", "/_fake/state")["tasks"]))
        self.finish(start)

    def test_reboot_stopped_and_template_delete_rejected(self):
        self.create_vm()
        self.api("POST", "/nodes/pve/qemu/101/status/reboot", {}, status=409)
        self.api("DELETE", "/nodes/pve/qemu/9000", {}, status=403)
        self.assertEqual(1, len(self.request("GET", "/_fake/state")["tasks"]))

    def test_task_failure_preserves_existing_state(self):
        failed = self.clone()
        self.finish(failed, "storage unavailable")
        self.api("GET", "/nodes/pve/qemu/101/config", status=404)
        self.create_vm()
        task = self.api("POST", "/nodes/pve/qemu/101/config", {"cores": 7})
        self.finish(task, "configuration rejected")
        self.assertEqual(1, self.api("GET", "/nodes/pve/qemu/101/config")["cores"])
        self.assertEqual("configuration rejected", self.api("GET", f"/nodes/pve/tasks/{task}/status")["exitstatus"])

    def test_bad_inputs_are_atomic_and_return_json(self):
        self.create_vm()
        for params in ({"memory": 15}, {"cores": True}, {"cores": 3, "unknown": 1}, {"cores": 0}, {"memory": "bad"}):
            with self.subTest(params=params):
                self.api("PUT", "/nodes/pve/qemu/101/config", params, status=400)
        self.assertEqual(1, self.api("GET", "/nodes/pve/qemu/101/config")["cores"])
        self.api("POST", "/nodes/other/qemu/9000/clone", {"newid": 102}, status=404)
        self.api("GET", "/nodes/pve/tasks/missing/status", status=404)
        self.api("GET", "/not-supported", status=404)
        self.api("GET", "/cluster/nextid?vmid=101", status=409)
        self.assertNotEqual(101, self.api("GET", "/cluster/nextid"))

    def test_accept_then_disconnect_keeps_task_once(self):
        path = "/api2/json/nodes/pve/qemu/9000/clone"
        self.request("POST", "/_fake/fault", {"method": "POST", "path": path, "mode": "disconnect_after_accept"})
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("POST", path, json.dumps({"newid": 101}), {"Content-Type": "application/json"})
            with self.assertRaises(http.client.RemoteDisconnected):
                connection.getresponse()
        finally:
            connection.close()
        tasks = self.request("GET", "/_fake/state")["tasks"]
        self.assertEqual(1, len(tasks))
        self.finish(tasks[0]["upid"])
        self.assertEqual("stopped", self.current()["status"])
        self.api("POST", "/nodes/pve/qemu/9000/clone", {"newid": 101}, status=409)
        self.assertEqual(1, len(self.request("GET", "/_fake/state")["tasks"]))

    def test_poll_failure_is_one_shot_and_does_not_erase_task(self):
        task = self.clone()
        path = f"/api2/json/nodes/pve/tasks/{task}/status"
        self.request("POST", "/_fake/fault", {"method": "GET", "path": path, "mode": "reject"})
        self.request("GET", path, status=503)
        self.assertEqual("running", self.request("GET", path)["status"])
        self.finish(task)
        self.assertEqual("OK", self.request("GET", path)["exitstatus"])

    def test_manual_completion_is_idempotent_but_conflicts_rejected(self):
        task = self.clone()
        self.finish(task)
        self.finish(task)
        self.request("POST", "/_fake/tasks/" + quote(task, safe=""), {"exitstatus": "different"}, status=409)
        self.assertEqual(1, len(self.request("GET", "/_fake/state")["vms"]) - 1)

    def test_instances_are_isolated_and_no_headers_in_ledger(self):
        other = make_server(port=0, auto_polls=None)
        try:
            self.create_vm()
            self.assertNotIn(101, other.state.vms)
            req = Request(self.base + "/api2/json/version", headers={"Authorization": "PVEAPIToken=fake-secret-marker"})
            with urlopen(req, timeout=3) as response:
                self.assertEqual(200, response.status)
            self.assertNotIn("fake-secret-marker", json.dumps(self.request("GET", "/_fake/state")))
            self.assertEqual("127.0.0.1", other.server_address[0])
        finally:
            other.server_close()

    def test_automatic_poll_completion(self):
        auto = make_server(port=0, auto_polls=1)
        thread = threading.Thread(target=auto.serve_forever, daemon=True)
        thread.start()
        old = self.base
        self.base = f"http://127.0.0.1:{auto.server_port}"
        try:
            task = self.clone()
            self.assertEqual("running", self.api("GET", f"/nodes/pve/tasks/{task}/status")["status"])
            self.assertEqual("stopped", self.api("GET", f"/nodes/pve/tasks/{task}/status")["status"])
            self.assertEqual("stopped", self.current()["status"])
        finally:
            self.base = old
            auto.shutdown()
            auto.server_close()
            thread.join()

    def test_malformed_json_and_body_size_rejected(self):
        for body, expected in ((b"{", 400), (b"[]", 400), (b"x" * 65537, 413)):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
            try:
                connection.request("POST", "/api2/json/nodes/pve/qemu/9000/clone", body, {"Content-Type": "application/json"})
                response = connection.getresponse()
                self.assertEqual(expected, response.status)
                self.assertIn("errors", json.loads(response.read()))
            finally:
                connection.close()
        self.assertEqual([], self.request("GET", "/_fake/state")["tasks"])

    def test_stopped_config_supersedes_old_pending(self):
        self.create_vm()
        self.finish(self.power("start"))
        self.api("PUT", "/nodes/pve/qemu/101/config", {"cores": 4})
        self.finish(self.power("stop"))
        self.api("PUT", "/nodes/pve/qemu/101/config", {"cores": 2})
        self.finish(self.power("start"))
        self.assertEqual(2, self.api("GET", "/nodes/pve/qemu/101/config?current=1")["cores"])
        self.assertEqual([], self.api("GET", "/nodes/pve/qemu/101/pending"))

    def test_fault_validation_and_rejected_mutation_are_atomic(self):
        for data in ({"mode": []}, {"mode": "reject", "method": []}):
            self.request("POST", "/_fake/fault", data, status=400)
        path = "/api2/json/nodes/pve/qemu/9000/clone"
        self.request("POST", "/_fake/fault", {"method": "POST", "path": path, "mode": "reject"})
        self.clone_request_for_status(101, 503)
        self.assertEqual([], self.request("GET", "/_fake/state")["tasks"])
        self.create_vm()
        self.assertEqual("stopped", self.current()["status"])

    def clone_request_for_status(self, vmid, status):
        return self.api("POST", "/nodes/pve/qemu/9000/clone", {"newid": vmid}, status=status)

    def test_cross_origin_control_request_is_rejected(self):
        self.create_vm()
        req = Request(self.base + "/_fake/reset", data=b"", method="POST", headers={"Origin": "https://untrusted.invalid"})
        with self.assertRaises(HTTPError) as context:
            urlopen(req, timeout=3)
        self.assertEqual(403, context.exception.code)
        context.exception.close()
        self.assertEqual("stopped", self.current()["status"])

    def test_concurrent_clone_and_completion_are_serialized(self):
        from concurrent.futures import ThreadPoolExecutor

        def clone_status(_):
            req = Request(self.base + "/api2/json/nodes/pve/qemu/9000/clone", data=b'{"newid":101}', method="POST", headers={"Content-Type": "application/json"})
            try:
                response = urlopen(req, timeout=3)
            except HTTPError as exc:
                response = exc
            with response:
                return response.status

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual([200, 409, 409, 409], sorted(pool.map(clone_status, range(4))))
        tasks = self.request("GET", "/_fake/state")["tasks"]
        self.assertEqual(1, len(tasks))
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.finish(tasks[0]["upid"]), range(4)))
        self.assertEqual("stopped", self.current()["status"])

    def test_old_task_completion_does_not_recreate_deleted_vm(self):
        task = self.create_vm()
        delete = self.api("DELETE", "/nodes/pve/qemu/101", {})
        self.finish(delete, "delete refused")
        self.assertEqual("stopped", self.current()["status"])
        delete = self.api("DELETE", "/nodes/pve/qemu/101", {})
        self.finish(delete)
        self.finish(task)
        self.api("GET", "/nodes/pve/qemu/101/config", status=404)
        self.create_vm()
        self.finish(delete)
        self.assertEqual("stopped", self.current()["status"])

    def test_cli_help_is_standalone(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/fake_pve_api.py"), "--help"], capture_output=True, text=True, timeout=5)
        self.assertEqual(0, result.returncode)
        self.assertIn("--manual", result.stdout)
        self.assertIn("--port", result.stdout)


if __name__ == "__main__":
    unittest.main()
