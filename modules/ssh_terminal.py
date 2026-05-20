import socket
import threading
import time

import paramiko


class SSHConnectionError(Exception):
    pass


class TooManyConnectionsError(Exception):
    pass


class SSHConnection:
    def __init__(self, sid, host, port, username, password, cluster_name, on_data, on_close, timeout=10):
        self.sid = sid
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.cluster_name = cluster_name
        self.on_data = on_data
        self.on_close = on_close
        self.timeout = timeout
        self.transport = None
        self.channel = None
        self.reader_thread = None
        self.last_activity = time.time()
        self.closed = False

    def connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self.transport = paramiko.Transport(sock)
            self.transport.set_keepalive(30)
            self.transport.connect(
                username=self.username,
                password=self.password,
            )
        except paramiko.AuthenticationException:
            raise SSHConnectionError("SSH 认证失败，请检查密码")
        except paramiko.SSHException as e:
            raise SSHConnectionError(f"SSH 连接失败: {e}")
        except (OSError, socket.timeout) as e:
            raise SSHConnectionError(f"无法连接到虚拟机 ({self.host}:{self.port}): {e}")

        self.channel = self.transport.open_session()
        self.channel.get_pty(term="xterm", width=80, height=24)
        self.channel.invoke_shell()

        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()

    def _reader(self):
        self.channel.settimeout(1.0)
        try:
            while not self.closed:
                try:
                    data = self.channel.recv(4096)
                    if data:
                        self.last_activity = time.time()
                        self.on_data(self.sid, data.decode("utf-8", errors="replace"))
                    else:
                        break
                except socket.timeout:
                    continue
                except (EOFError, OSError):
                    break
        finally:
            if not self.closed:
                self.on_close(self.sid)

    def write(self, data):
        if self.channel and not self.closed:
            self.last_activity = time.time()
            try:
                self.channel.send(data)
            except Exception:
                pass

    def resize(self, cols, rows):
        if self.channel and not self.closed:
            try:
                self.channel.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def close(self):
        self.closed = True
        if self.channel:
            try:
                self.channel.close()
            except Exception:
                pass
        if self.transport:
            try:
                self.transport.close()
            except Exception:
                pass


class SSHManager:
    def __init__(self, max_connections=64, idle_timeout=900):
        self._connections = {}
        self._user_map = {}
        self._max_connections = max_connections
        self._idle_timeout = idle_timeout
        self._lock = threading.Lock()
        self._socketio = None
        self._cleanup_thread = None
        self._cleanup_running = False

    def init_app(self, socketio):
        self._socketio = socketio
        self._start_cleanup_thread()

    def _start_cleanup_thread(self):
        self._cleanup_running = True
        t = threading.Thread(target=self._cleanup_loop, daemon=True)
        t.start()

    def _cleanup_loop(self):
        while self._cleanup_running:
            time.sleep(300)
            self._cleanup_idle()

    def create(self, sid, host, port, username, password, cluster_name, timeout=10, user_info=None):
        with self._lock:
            if len(self._connections) >= self._max_connections:
                raise TooManyConnectionsError("服务器繁忙，请稍后重试")
            self._remove_locked(sid)
            conn = SSHConnection(
                sid, host, port, username, password, cluster_name,
                self._on_data, self._on_close, timeout=timeout,
            )
            conn.connect()
            self._connections[sid] = conn
            if user_info:
                self._user_map[sid] = user_info

    def get(self, sid):
        return self._connections.get(sid)

    def write(self, sid, data):
        conn = self._connections.get(sid)
        if conn:
            conn.write(data)

    def resize(self, sid, cols, rows):
        conn = self._connections.get(sid)
        if conn:
            conn.resize(cols, rows)

    def remove(self, sid):
        with self._lock:
            self._remove_locked(sid)

    def _remove_locked(self, sid):
        conn = self._connections.pop(sid, None)
        if conn:
            conn.close()
        self._user_map.pop(sid, None)

    def list_connections(self):
        with self._lock:
            result = []
            for sid, conn in self._connections.items():
                idle_seconds = int(time.time() - conn.last_activity)
                user_info = self._user_map.get(sid, {})
                result.append({
                    "sid": sid,
                    "cluster": conn.cluster_name,
                    "host": conn.host,
                    "port": conn.port,
                    "username": conn.username,
                    "user": user_info.get("username", ""),
                    "user_id": user_info.get("id", 0),
                    "user_role": user_info.get("role", ""),
                    "connected_at": conn.last_activity,
                    "idle_seconds": idle_seconds,
                })
            return result

    def disconnect_by_sid(self, sid):
        with self._lock:
            conn = self._connections.pop(sid, None)
            if conn:
                conn.close()
            self._user_map.pop(sid, None)
            return conn is not None

    def _on_data(self, sid, data):
        if self._socketio:
            self._socketio.emit(
                "ssh_output", {"data": data}, to=sid, namespace="/webssh",
            )

    def _on_close(self, sid):
        with self._lock:
            conn = self._connections.pop(sid, None)
            if conn:
                conn.close()
            self._user_map.pop(sid, None)
        if self._socketio:
            self._socketio.emit(
                "ssh_disconnected", {"reason": "连接已断开"}, to=sid, namespace="/webssh",
            )

    def _cleanup_idle(self):
        now = time.time()
        to_remove = []
        with self._lock:
            for sid, conn in self._connections.items():
                if now - conn.last_activity > self._idle_timeout:
                    to_remove.append(sid)
        for sid in to_remove:
            self._remove_locked(sid)
            if self._socketio:
                self._socketio.emit(
                    "ssh_disconnected", {"reason": "闲置超时"}, to=sid, namespace="/webssh",
                )

    def stop(self):
        self._cleanup_running = False
        with self._lock:
            for sid in list(self._connections):
                self._remove_locked(sid)
