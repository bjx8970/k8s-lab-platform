import argparse
import json
import signal
import sys
import threading

from .config import ConfigurationError, RuntimeSettings
from .runtime import (
    ControlPlaneRuntime,
    CoordinatorConflict,
    DatabaseUnavailable,
    LeadershipLost,
    StartupDeadlineExceeded,
    StartupInterrupted,
)

EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_COORDINATOR_CONFLICT = 3
EXIT_RUNTIME_FAILURE = 4


class _StartupDeadline:
    """Bounds the whole startup handshake.

    pg8000's timeout applies to individual socket operations, so a server that
    keeps sending protocol messages can keep a handshake alive indefinitely.
    """

    def __init__(self, seconds: float):
        self._seconds = seconds
        self.active = False
        self._previous_handler = None
        self._timer_supported = (
            hasattr(signal, "SIGALRM")
            and hasattr(signal, "setitimer")
            and threading.current_thread() is threading.main_thread()
        )

    def __enter__(self):
        if self._timer_supported:
            try:
                self._previous_handler = signal.getsignal(signal.SIGALRM)
                signal.signal(signal.SIGALRM, self._alarm)
                self.active = True
                signal.setitimer(signal.ITIMER_REAL, self._seconds)
            except BaseException:
                self.active = False
                try:
                    signal.setitimer(signal.ITIMER_REAL, 0)
                except Exception:
                    pass
                if self._previous_handler is not None:
                    try:
                        signal.signal(signal.SIGALRM, self._previous_handler)
                    except (ValueError, OSError):
                        pass
                raise
        else:
            self.active = True
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.active = False
        if self._timer_supported:
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            finally:
                if self._previous_handler is not None:
                    signal.signal(signal.SIGALRM, self._previous_handler)
        return False

    def _alarm(self, signum, frame):
        if self.active:
            raise StartupDeadlineExceeded("startup deadline exceeded")


def _install_signal_handlers(stop_event, deadline_ref) -> None:
    def request_stop(signum, frame):
        already_stopping = stop_event.is_set()
        stop_event.set()
        deadline = deadline_ref[0]
        if not already_stopping and deadline is not None and deadline.active:
            raise StartupInterrupted("startup interrupted by signal")

    for signal_name in ("SIGINT", "SIGTERM"):
        signal_number = getattr(signal, signal_name, None)
        if signal_number is None:
            continue
        try:
            signal.signal(signal_number, request_stop)
        except (ValueError, OSError):
            pass


def _write_event(event: str, *, error: bool = False, **fields) -> None:
    payload = {"event": event, **fields}
    print(
        json.dumps(payload),
        file=sys.stderr if error else sys.stdout,
        flush=True,
    )


def _stop_quietly(runtime) -> None:
    try:
        runtime.stop()
    except Exception:
        pass


def _run_start() -> int:
    stop_event = threading.Event()
    deadline_ref = [None]
    _install_signal_handlers(stop_event, deadline_ref)

    try:
        settings = RuntimeSettings.from_env()
    except ConfigurationError:
        print("configuration error", file=sys.stderr, flush=True)
        return EXIT_CONFIGURATION

    deadline = _StartupDeadline(settings.connect_timeout_seconds)
    deadline_ref[0] = deadline
    runtime = ControlPlaneRuntime(settings)
    if stop_event.is_set():
        return EXIT_OK
    try:
        with deadline:
            if stop_event.is_set():
                return EXIT_OK
            runtime.start()
    except StartupInterrupted:
        _stop_quietly(runtime)
        return EXIT_OK
    except StartupDeadlineExceeded:
        _stop_quietly(runtime)
        print("startup deadline exceeded", file=sys.stderr, flush=True)
        return EXIT_RUNTIME_FAILURE
    except CoordinatorConflict:
        if stop_event.is_set():
            return EXIT_OK
        print("coordinator already active", file=sys.stderr, flush=True)
        return EXIT_COORDINATOR_CONFLICT
    except DatabaseUnavailable:
        if stop_event.is_set():
            return EXIT_OK
        print("database unavailable", file=sys.stderr, flush=True)
        return EXIT_RUNTIME_FAILURE
    except Exception:
        if stop_event.is_set():
            return EXIT_OK
        print("control plane startup failed", file=sys.stderr, flush=True)
        return EXIT_RUNTIME_FAILURE

    exit_code = EXIT_OK
    ready_announced = False
    try:
        if not stop_event.is_set():
            _write_event("ready", lock_backend_pid=runtime.lock_backend_pid)
            ready_announced = True
            while not stop_event.wait(settings.lock_poll_seconds):
                try:
                    runtime.poll()
                except LeadershipLost:
                    _write_event("leadership_lost", error=True)
                    exit_code = EXIT_RUNTIME_FAILURE
                    break
    except Exception:
        if exit_code == EXIT_OK:
            exit_code = EXIT_RUNTIME_FAILURE
        print("control plane runtime failed", file=sys.stderr, flush=True)
    finally:
        try:
            runtime.stop()
        except Exception:
            if exit_code == EXIT_OK:
                exit_code = EXIT_RUNTIME_FAILURE
            print("shutdown failed", file=sys.stderr, flush=True)
    if ready_announced and exit_code == EXIT_OK:
        _write_event("stopped")
    return exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="control_plane")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("start", help="start the control plane runtime")
    parser.parse_args(argv)
    return _run_start()
