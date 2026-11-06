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
)

EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_COORDINATOR_CONFLICT = 3
EXIT_RUNTIME_FAILURE = 4


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum, frame):
        stop_event.set()

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


def _run_start() -> int:
    stop_event = threading.Event()
    _install_signal_handlers(stop_event)

    try:
        settings = RuntimeSettings.from_env()
    except ConfigurationError:
        print("configuration error", file=sys.stderr, flush=True)
        return EXIT_CONFIGURATION

    runtime = ControlPlaneRuntime(settings)
    if stop_event.is_set():
        return EXIT_OK
    try:
        runtime.start()
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
