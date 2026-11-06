from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
TEST_URL_ENV = "CONTROL_PLANE_TEST_DATABASE_URL"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


class TestDatabaseIsolationError(RuntimeError):
    pass


def _normalized_url(raw: str):
    url = make_url(raw)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+pg8000")
    return url


def validate_disposable_database_url(raw: str) -> str:
    try:
        url = _normalized_url(raw)
    except Exception:
        raise TestDatabaseIsolationError("invalid database URL") from None
    if not url.drivername.startswith("postgresql"):
        raise TestDatabaseIsolationError("only PostgreSQL test databases are allowed")
    if not url.database or not url.database.startswith("control_plane_test_"):
        raise TestDatabaseIsolationError(
            "test database name must start with control_plane_test_"
        )
    if url.host is not None and url.host not in _ALLOWED_HOSTS:
        raise TestDatabaseIsolationError("test database must be loopback or a Unix socket")
    if url.query:
        raise TestDatabaseIsolationError(
            "test database URL must not contain query parameters"
        )
    return url.render_as_string(hide_password=False)


def require_disposable_database_url(environ=None) -> str:
    env = os.environ if environ is None else environ
    raw = env.get(TEST_URL_ENV, "").strip()
    if not raw:
        if env.get("CONTROL_PLANE_REQUIRE_TEST_DATABASE") == "1":
            raise TestDatabaseIsolationError(
                f"{TEST_URL_ENV} is required for this test run"
            )
        raise unittest.SkipTest(f"{TEST_URL_ENV} is not set")
    return validate_disposable_database_url(raw)


def engine_for(url: str):
    return create_engine(
        _normalized_url(url).render_as_string(hide_password=False),
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={"timeout": 3},
    )


def try_advisory_lock(url: str, key: int) -> bool:
    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            acquired = bool(
                conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ).scalar_one()
            )
            if acquired:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"), {"key": key}
                )
            return acquired
    finally:
        engine.dispose()


def public_table_count(url: str) -> int:
    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            return int(
                conn.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def terminate_backend(url: str, backend_pid: int) -> None:
    engine = engine_for(url)
    try:
        with engine.connect() as conn:
            conn.execute(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid}
            )
    finally:
        engine.dispose()


class CliProcess:
    def __init__(
        self,
        url: str,
        *,
        extra_env: dict[str, str] | None = None,
        lock_poll_seconds: float = 0.2,
        connect_timeout_seconds: float = 2.0,
    ):
        env = os.environ.copy()
        env.update(extra_env or {})
        env.update(
            {
                "CONTROL_PLANE_DATABASE_URL": url,
                "CONTROL_PLANE_LOCK_POLL_SECONDS": str(lock_poll_seconds),
                "CONTROL_PLANE_CONNECT_TIMEOUT_SECONDS": str(
                    connect_timeout_seconds
                ),
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.proc = subprocess.Popen(
            [sys.executable, "-B", "-m", "control_plane", "start"],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.stdout_lines: list[str] = []
        self._stdout_eof = False

    def read_event(self, event: str, timeout: float = 10.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stdout_eof:
                break
            remaining = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select(
                [self.proc.stdout], [], [], min(remaining, 0.2)
            )
            if not readable:
                continue
            line = self.proc.stdout.readline()
            if line == "":
                self._stdout_eof = True
                break
            self.stdout_lines.append(line)
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("event") == event:
                return payload
        raise AssertionError(
            f"process did not emit {event!r}; "
            f"stdout={''.join(self.stdout_lines)!r}; "
            f"returncode={self.proc.poll()!r}"
        )

    def wait_for_exit(self, timeout: float = 15.0) -> tuple[int, str, str]:
        try:
            stdout_tail, stderr = self.proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            stdout_tail, stderr = self.proc.communicate()
            raise AssertionError(
                f"process did not exit within {timeout}s; "
                f"stdout={''.join(self.stdout_lines) + stdout_tail!r}; "
                f"stderr={stderr!r}"
            ) from None
        return (
            int(self.proc.returncode),
            "".join(self.stdout_lines) + stdout_tail,
            stderr,
        )

    def send_stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(15)

    def close(self) -> None:
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            try:
                self.proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        self.close()


def run_cli_once(
    url: str,
    *,
    extra_env: dict[str, str] | None = None,
    connect_timeout_seconds: float = 2.0,
    timeout: float = 15.0,
) -> tuple[int, str, str]:
    process = CliProcess(
        url,
        extra_env=extra_env,
        connect_timeout_seconds=connect_timeout_seconds,
    )
    try:
        return process.wait_for_exit(timeout=timeout)
    finally:
        process.kill()


def wait_for_one_ready(
    processes: list[CliProcess], timeout: float = 15.0
) -> tuple[CliProcess, dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for process in processes:
            if process._stdout_eof:
                continue
            readable, _, _ = select.select([process.proc.stdout], [], [], 0)
            if not readable:
                continue
            line = process.proc.stdout.readline()
            if line == "":
                process._stdout_eof = True
                continue
            process.stdout_lines.append(line)
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("event") == "ready":
                return process, payload
        if all(process._stdout_eof for process in processes):
            break
        time.sleep(0.05)
    details = "; ".join(
        f"pid={process.proc.pid} code={process.proc.poll()} "
        f"stdout={''.join(process.stdout_lines)!r}"
        for process in processes
    )
    raise AssertionError(f"no process became ready; {details}")


class SilentTcpServer:
    """Accepts TCP connections but never completes a PostgreSQL handshake."""

    def __init__(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = int(self._listener.getsockname()[1])
        self._stop = threading.Event()
        self.connected = threading.Event()
        self._connections: list[socket.socket] = []
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> "SilentTcpServer":
        self._thread.start()
        return self

    def wait_for_connection(self, timeout: float = 5.0) -> bool:
        return self.connected.wait(timeout)

    def _run(self) -> None:
        self._listener.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except (socket.timeout, OSError):
                continue
            self.connected.set()
            self._connections.append(connection)

    def close(self) -> None:
        self._stop.set()
        try:
            self._listener.close()
        except OSError:
            pass
        for connection in self._connections:
            try:
                connection.close()
            except OSError:
                pass
        self._thread.join(timeout=2)
