from sqlalchemy import text


class CoordinatorLease:
    """A session-scoped PostgreSQL advisory lock held by one dedicated connection."""

    def __init__(self, engine, lock_key: int):
        self._engine = engine
        self._lock_key = lock_key
        self._connection = None
        self._backend_pid: int | None = None

    def acquire(self) -> bool:
        if self._connection is not None:
            raise RuntimeError("coordinator lease is already held")
        connection = self._engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            acquired = bool(
                connection.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": self._lock_key},
                ).scalar_one()
            )
            if not acquired:
                connection.close()
                return False
            backend_pid = int(
                connection.execute(text("SELECT pg_backend_pid()")).scalar_one()
            )
        except Exception:
            connection.close()
            raise
        self._connection = connection
        self._backend_pid = backend_pid
        return True

    def verify(self) -> bool:
        if self._connection is None:
            return False
        try:
            self._connection.execute(text("SELECT 1")).scalar_one()
            return True
        except Exception:
            self._close()
            return False

    def release(self) -> None:
        connection = self._connection
        self._connection = None
        self._backend_pid = None
        if connection is None:
            return
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": self._lock_key}
            )
        except Exception:
            self._invalidate(connection)
        finally:
            connection.close()

    @property
    def backend_pid(self) -> int | None:
        return self._backend_pid

    def _close(self) -> None:
        connection = self._connection
        self._connection = None
        self._backend_pid = None
        if connection is not None:
            self._invalidate(connection)
            connection.close()

    @staticmethod
    def _invalidate(connection) -> None:
        try:
            connection.invalidate()
        except Exception:
            pass
