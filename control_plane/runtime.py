from .config import COORDINATOR_LOCK_KEY
from .coordination import CoordinatorLease
from .database import create_database_engine


class CoordinatorConflict(RuntimeError):
    """Another process already holds the coordinator lock."""


class DatabaseUnavailable(RuntimeError):
    """The database could not be reached or the lock could not be checked."""


class LeadershipLost(RuntimeError):
    """The coordinator lock session is no longer alive."""


class ControlPlaneRuntime:
    def __init__(self, settings):
        self.settings = settings
        self._engine = None
        self._lease = None
        self._state = "stopped"

    def start(self) -> None:
        if self._state == "running":
            raise RuntimeError("control plane runtime is already running")
        engine = create_database_engine(self.settings)
        lease = CoordinatorLease(engine, COORDINATOR_LOCK_KEY)
        try:
            acquired = lease.acquire()
        except Exception as exc:
            engine.dispose()
            raise DatabaseUnavailable("database connection failed") from exc
        if not acquired:
            engine.dispose()
            raise CoordinatorConflict("another coordinator is active")
        self._engine = engine
        self._lease = lease
        self._state = "running"

    def poll(self) -> None:
        if self._state != "running" or self._lease is None:
            raise LeadershipLost("coordinator is not running")
        if not self._lease.verify():
            self._state = "lost"
            raise LeadershipLost("coordinator lock session was lost")

    def stop(self) -> None:
        if self._state == "stopped":
            return
        lease = self._lease
        engine = self._engine
        self._lease = None
        self._engine = None
        self._state = "stopped"
        if lease is not None:
            lease.release()
        if engine is not None:
            engine.dispose()

    @property
    def lock_backend_pid(self) -> int:
        if self._lease is None or self._lease.backend_pid is None:
            raise RuntimeError("coordinator is not running")
        return self._lease.backend_pid
