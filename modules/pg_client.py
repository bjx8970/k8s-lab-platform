import pg8000


class PGError(Exception):
    pass


class PGClient:
    def __init__(self, host, port, user, password, database="postgres"):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.database = database
        self._conn = None

    def connect(self):
        try:
            self._conn = pg8000.connect(
                host=self.host, port=self.port,
                user=self.user, password=self.password,
                database=self.database,
            )
            return self._conn.parameter_statuses.get('server_version', 'unknown')
        except Exception as e:
            raise PGError(str(e)) from e

    def close(self):
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
