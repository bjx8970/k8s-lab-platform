import json
import os
import subprocess
import sys
import time
import unittest
from dataclasses import FrozenInstanceError

from control_plane.config import (
    COORDINATOR_LOCK_KEY,
    ConfigurationError,
    RuntimeSettings,
)
from control_plane.runtime import ControlPlaneRuntime
from tests_control_plane.pg_harness import (
    ROOT,
    ChattyPostgresServer,
    CliProcess,
    SilentTcpServer,
    TestDatabaseIsolationError,
    enable_short_transaction_timeout,
    public_table_count,
    reset_transaction_timeout,
    require_disposable_database_url,
    run_cli_once,
    terminate_backend,
    try_advisory_lock,
    validate_disposable_database_url,
    wait_for_one_ready,
)


def parse_events(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "event" in payload:
            events.append(payload)
    return events


def ready_events(text: str) -> list[dict]:
    return [event for event in parse_events(text) if event.get("event") == "ready"]


class ImportBoundaryTests(unittest.TestCase):
    def test_top_level_import_does_not_load_database_or_start_threads(self):
        env = os.environ.copy()
        env["CONTROL_PLANE_DATABASE_URL"] = (
            "postgresql+pg8000://sentinel:secret@203.0.113.1:1/"
            "control_plane_test_import"
        )
        code = (
            "import sys, threading\n"
            "import control_plane\n"
            "assert 'sqlalchemy' not in sys.modules, "
            "sorted(name for name in sys.modules if name.startswith('sqlalchemy'))\n"
            "assert len(threading.enumerate()) == 1, "
            "[thread.name for thread in threading.enumerate()]\n"
            "print('IMPORT_OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("IMPORT_OK", result.stdout)


class ConfigurationTests(unittest.TestCase):
    def test_missing_database_url_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            RuntimeSettings.from_env({})

    def test_non_postgresql_url_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            RuntimeSettings.from_env(
                {"CONTROL_PLANE_DATABASE_URL": "sqlite:///control_plane_test.db"}
            )

    def test_unsupported_postgresql_driver_is_rejected(self):
        with self.assertRaises(ConfigurationError):
            RuntimeSettings.from_env(
                {
                    "CONTROL_PLANE_DATABASE_URL": (
                        "postgresql+psycopg://user:secret@localhost/control_plane_test_x"
                    )
                }
            )

    def test_bare_postgresql_url_is_normalized_to_pg8000(self):
        settings = RuntimeSettings.from_env(
            {
                "CONTROL_PLANE_DATABASE_URL": (
                    "postgresql://user:secret@localhost/control_plane_test_x"
                )
            }
        )
        self.assertTrue(settings.database_url.startswith("postgresql+pg8000://"))
        self.assertNotIn("secret", repr(settings))

    def test_numeric_bounds_are_enforced(self):
        base = {
            "CONTROL_PLANE_DATABASE_URL": (
                "postgresql+pg8000://user:secret@localhost/control_plane_test_x"
            )
        }
        for name, value in (
            ("CONTROL_PLANE_LOCK_POLL_SECONDS", "0"),
            ("CONTROL_PLANE_LOCK_POLL_SECONDS", "61"),
            ("CONTROL_PLANE_LOCK_POLL_SECONDS", "nan"),
            ("CONTROL_PLANE_CONNECT_TIMEOUT_SECONDS", "0"),
        ):
            with self.subTest(name=name, value=value):
                with self.assertRaises(ConfigurationError):
                    RuntimeSettings.from_env({**base, name: value})

    def test_repr_does_not_expose_database_credentials(self):
        settings = RuntimeSettings.from_env(
            {
                "CONTROL_PLANE_DATABASE_URL": (
                    "postgresql+pg8000://alice:top-secret@localhost/"
                    "control_plane_test_x"
                )
            }
        )
        rendered = repr(settings)
        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("alice", rendered)
        self.assertNotIn("control_plane_test_x", rendered)


class CliConfigurationTests(unittest.TestCase):
    def test_cli_without_database_url_exits_configuration_error(self):
        env = os.environ.copy()
        env.pop("CONTROL_PLANE_DATABASE_URL", None)
        result = subprocess.run(
            [sys.executable, "-B", "-m", "control_plane", "start"],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(parse_events(result.stdout))

    def test_cli_with_non_postgresql_url_exits_configuration_error(self):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "control_plane", "start"],
            cwd=str(ROOT),
            env={
                **os.environ,
                "CONTROL_PLANE_DATABASE_URL": "sqlite:///control_plane_test.db",
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertFalse(parse_events(result.stdout))


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = require_disposable_database_url()

    def start_leader(self, **kwargs):
        process = CliProcess(self.url, **kwargs)
        self.addCleanup(process.kill)
        payload = process.read_event("ready")
        return process, payload

    def stop_leader(self, process):
        process.send_stop()
        code, stdout, stderr = process.wait_for_exit(timeout=10)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertIn({"event": "stopped"}, parse_events(stdout))
        self.assertNotIn("postgresql", stderr.lower())
        self.assertNotIn("control_plane_test", stderr)

    def test_empty_database_acquires_coordination(self):
        self.assertEqual(public_table_count(self.url), 0)
        process, payload = self.start_leader()
        self.assertGreater(int(payload["lock_backend_pid"]), 0)
        self.assertFalse(try_advisory_lock(self.url, COORDINATOR_LOCK_KEY))
        self.stop_leader(process)
        self.assertTrue(try_advisory_lock(self.url, COORDINATOR_LOCK_KEY))

    def test_duplicate_start_is_rejected(self):
        leader, _ = self.start_leader()
        code, stdout, stderr = run_cli_once(self.url, timeout=10)
        self.assertEqual(code, 3, stdout + stderr)
        self.assertFalse(ready_events(stdout))
        self.assertIsNone(leader.proc.poll())
        self.stop_leader(leader)

    def test_concurrent_start_has_single_leader(self):
        processes = [CliProcess(self.url) for _ in range(3)]
        for process in processes:
            self.addCleanup(process.kill)
        leader, _ = wait_for_one_ready(processes, timeout=15)
        for process in processes:
            if process is leader:
                continue
            code, stdout, stderr = process.wait_for_exit(timeout=15)
            self.assertEqual(code, 3, stdout + stderr)
            self.assertFalse(ready_events(stdout))
        self.stop_leader(leader)

    def test_sigterm_releases_coordination_and_allows_restart(self):
        leader, _ = self.start_leader()
        self.stop_leader(leader)
        self.assertTrue(try_advisory_lock(self.url, COORDINATOR_LOCK_KEY))
        second, _ = self.start_leader()
        self.stop_leader(second)

    def test_stop_signal_before_ready_is_bounded(self):
        server = SilentTcpServer().start()
        self.addCleanup(server.close)
        url = (
            f"postgresql+pg8000://postgres@127.0.0.1:{server.port}/"
            "control_plane_test_prestop"
        )
        process = CliProcess(url, connect_timeout_seconds=3.0)
        self.addCleanup(process.kill)
        self.assertTrue(
            server.wait_for_connection(timeout=5), "CLI did not attempt a connection"
        )
        process.send_stop()
        code, stdout, stderr = process.wait_for_exit(timeout=10)
        self.assertEqual(code, 0, stdout + stderr)
        self.assertFalse(ready_events(stdout))
        self.assertNotIn("leadership_lost", stderr)

    def test_stop_signal_interrupts_chatty_handshake(self):
        server = ChattyPostgresServer().start()
        self.addCleanup(server.close)
        url = (
            f"postgresql+pg8000://postgres@127.0.0.1:{server.port}/"
            "control_plane_test_chatty_stop"
        )
        process = CliProcess(url, connect_timeout_seconds=5.0)
        self.addCleanup(process.kill)
        self.assertTrue(
            server.wait_for_connection(timeout=5), "CLI did not attempt a connection"
        )
        started = time.monotonic()
        process.send_stop()
        code, stdout, stderr = process.wait_for_exit(timeout=6)
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, stdout + stderr)
        self.assertFalse(ready_events(stdout))
        self.assertLess(elapsed, 3.0)

    def test_chatty_handshake_respects_startup_deadline(self):
        server = ChattyPostgresServer().start()
        self.addCleanup(server.close)
        url = (
            f"postgresql+pg8000://postgres@127.0.0.1:{server.port}/"
            "control_plane_test_chatty_deadline"
        )
        started = time.monotonic()
        code, stdout, stderr = run_cli_once(
            url, connect_timeout_seconds=0.5, timeout=5
        )
        elapsed = time.monotonic() - started
        self.assertEqual(code, 4, stdout + stderr)
        self.assertFalse(ready_events(stdout))
        self.assertLess(elapsed, 3.0)

    def test_heartbeat_survives_transaction_timeout(self):
        enable_short_transaction_timeout(self.url)
        self.addCleanup(reset_transaction_timeout, self.url)
        leader, _ = self.start_leader(lock_poll_seconds=0.1)
        time.sleep(2.6)
        self.assertIsNone(leader.proc.poll())
        self.stop_leader(leader)

    def test_unreachable_database_fails_closed_without_secret_leak(self):
        sentinel = "p0-unreachable-sentinel"
        url = (
            f"postgresql+pg8000://postgres:{sentinel}@127.0.0.1:1/"
            "control_plane_test_unreachable"
        )
        started = time.monotonic()
        code, stdout, stderr = run_cli_once(
            url, connect_timeout_seconds=1.0, timeout=15
        )
        elapsed = time.monotonic() - started
        self.assertEqual(code, 4, stdout + stderr)
        self.assertFalse(ready_events(stdout))
        self.assertNotIn(sentinel, stderr)
        self.assertNotIn("postgresql+pg8000://", stderr)
        self.assertLess(elapsed, 6.0)

    def test_silent_tcp_endpoint_respects_connect_timeout(self):
        server = SilentTcpServer().start()
        self.addCleanup(server.close)
        sentinel = "p0-blackhole-sentinel"
        url = (
            f"postgresql+pg8000://postgres:{sentinel}@127.0.0.1:{server.port}/"
            "control_plane_test_blackhole"
        )
        started = time.monotonic()
        code, stdout, stderr = run_cli_once(
            url, connect_timeout_seconds=1.0, timeout=10
        )
        elapsed = time.monotonic() - started
        self.assertEqual(code, 4, stdout + stderr)
        self.assertFalse(ready_events(stdout))
        self.assertNotIn(sentinel, stderr)
        self.assertLess(elapsed, 6.0)

    def test_startup_does_not_create_schema(self):
        self.assertEqual(public_table_count(self.url), 0)
        leader, _ = self.start_leader()
        self.stop_leader(leader)
        self.assertEqual(public_table_count(self.url), 0)


class LeadershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = require_disposable_database_url()

    def test_terminated_lock_backend_forces_shutdown(self):
        process = CliProcess(self.url, lock_poll_seconds=0.1)
        self.addCleanup(process.kill)
        payload = process.read_event("ready")
        terminate_backend(self.url, int(payload["lock_backend_pid"]))
        started = time.monotonic()
        code, stdout, stderr = process.wait_for_exit(timeout=6)
        elapsed = time.monotonic() - started
        self.assertEqual(code, 4, stdout + stderr)
        self.assertIn("leadership_lost", stderr)
        self.assertEqual(len(ready_events(stdout)), 1)
        self.assertNotIn("postgresql", stderr.lower())
        self.assertNotIn("control_plane_test", stderr)
        self.assertLess(elapsed, 5.1)
        self.assertTrue(try_advisory_lock(self.url, COORDINATOR_LOCK_KEY))


class LifecycleUnitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.url = require_disposable_database_url()

    def test_runtime_restart_and_idempotent_stop(self):
        settings = RuntimeSettings.from_env(
            {"CONTROL_PLANE_DATABASE_URL": self.url}
        )
        runtime = ControlPlaneRuntime(settings)
        try:
            runtime.start()
            self.assertGreater(runtime.lock_backend_pid, 0)
            runtime.stop()
            runtime.stop()
            runtime.start()
            self.assertGreater(runtime.lock_backend_pid, 0)
        finally:
            runtime.stop()

    def test_stop_without_start_is_safe(self):
        settings = RuntimeSettings.from_env(
            {"CONTROL_PLANE_DATABASE_URL": self.url}
        )
        ControlPlaneRuntime(settings).stop()

    def test_settings_are_immutable(self):
        settings = RuntimeSettings.from_env(
            {"CONTROL_PLANE_DATABASE_URL": self.url}
        )
        with self.assertRaises(FrozenInstanceError):
            settings.lock_poll_seconds = 2.0


class TestDatabaseGuardTests(unittest.TestCase):
    def test_accepts_loopback_disposable_database(self):
        url = "postgresql+pg8000://postgres@127.0.0.1/control_plane_test_p0"
        self.assertEqual(validate_disposable_database_url(url), url)

    def test_rejects_non_postgresql_url(self):
        with self.assertRaises(TestDatabaseIsolationError):
            validate_disposable_database_url("sqlite:///control_plane_test.db")

    def test_rejects_remote_host(self):
        with self.assertRaises(TestDatabaseIsolationError):
            validate_disposable_database_url(
                "postgresql+pg8000://postgres@db.example.com/control_plane_test_x"
            )

    def test_rejects_shared_database_name(self):
        with self.assertRaises(TestDatabaseIsolationError):
            validate_disposable_database_url(
                "postgresql+pg8000://postgres@127.0.0.1/production"
            )

    def test_rejects_query_parameter_overrides(self):
        with self.assertRaises(TestDatabaseIsolationError):
            validate_disposable_database_url(
                "postgresql+pg8000://postgres@127.0.0.1/"
                "control_plane_test_x?host=10.0.0.9"
            )

    def test_normalizes_bare_postgresql_url(self):
        url = validate_disposable_database_url(
            "postgresql://postgres@127.0.0.1/control_plane_test_x"
        )
        self.assertTrue(url.startswith("postgresql+pg8000://"))

    def test_required_environment_is_not_silently_skipped(self):
        with self.assertRaises(TestDatabaseIsolationError):
            require_disposable_database_url(
                {"CONTROL_PLANE_REQUIRE_TEST_DATABASE": "1"}
            )

    def test_missing_environment_is_skipped_not_treated_as_valid(self):
        with self.assertRaises(unittest.SkipTest):
            require_disposable_database_url({})


if __name__ == "__main__":
    unittest.main()
