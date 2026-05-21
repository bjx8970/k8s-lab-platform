import socket
import threading
import time
import uuid
from collections import deque

import paramiko


class SSHConnectionError(Exception):
    pass


class SessionExistsError(Exception):
    pass


class TooManyConnectionsError(Exception):
    pass


class SSHSession:
    def __init__(self, session_id, cluster_name, owner, host, port, ssh_user, ssh_pass):
        self.session_id = session_id
        self.cluster_name = cluster_name
        self.owner = owner  # {user_id, username, role}
        self.host = host
        self.port = port
        self.ssh_user = ssh_user
        self.ssh_pass = ssh_pass

        self.status = "disconnected"
        self.takeover_active = False
        self.takeover_by = None  # {user_id, username, role}

        self.transport = None
        self.channel = None
        self.reader_thread = None

        self.log_buffer = deque(maxlen=10000)
        self.log_buffer_raw = []

        self.owner_sid = None
        self.takeover_sid = None
        self.viewer_sids = set()

        self.created_at = time.time()
        self.last_activity = time.time()
        self.disconnected_at = None
        self.last_input_time = time.time()

        self.reconnect_requestor = None
        self.reconnect_expires_at = None

        self._lock = threading.Lock()
        self._closed = False

    def _emit(self, socketio, event, data, to=None):
        if not socketio:
            return
        if to:
            socketio.emit(event, data, to=to, namespace="/webssh")
        else:
            socketio.emit(event, data, namespace="/webssh")

    def connect(self, socketio):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
            self.transport = paramiko.Transport(sock)
            self.transport.set_keepalive(30)
            self.transport.connect(username=self.ssh_user, password=self.ssh_pass)
        except paramiko.AuthenticationException:
            raise SSHConnectionError("SSH 认证失败，请检查密码")
        except paramiko.SSHException as e:
            raise SSHConnectionError(f"SSH 连接失败: {e}")
        except (OSError, socket.timeout) as e:
            raise SSHConnectionError(f"无法连接到虚拟机 ({self.host}:{self.port}): {e}")

        self.channel = self.transport.open_session()
        self.channel.get_pty(term="xterm", width=80, height=24)
        self.channel.invoke_shell()

        self.status = "connected"
        self.last_activity = time.time()
        self.last_input_time = time.time()
        self.disconnected_at = None

        self.reader_thread = threading.Thread(
            target=self._reader, args=(socketio,), daemon=True
        )
        self.reader_thread.start()

    def _reader(self, socketio):
        self.channel.settimeout(1.0)
        try:
            while not self._closed:
                try:
                    data = self.channel.recv(4096)
                    if data:
                        text = data.decode("utf-8", errors="replace")
                        self.last_activity = time.time()
                        self.log_buffer.append(text)
                        self.log_buffer_raw.append(text)
                        # Send to all bound WS
                        targets = self._get_all_targets()
                        for sid in targets:
                            self._emit(socketio, "ssh_output", {"data": text}, to=sid)
                    else:
                        break
                except socket.timeout:
                    continue
                except (EOFError, OSError):
                    break
        finally:
            if not self._closed:
                self._on_close(socketio)

    def _get_all_targets(self):
        targets = set()
        if self.owner_sid:
            targets.add(self.owner_sid)
        if self.takeover_sid:
            targets.add(self.takeover_sid)
        targets.update(self.viewer_sids)
        return targets

    def _on_close(self, socketio):
        self.status = "disconnected"
        self.disconnected_at = time.time()
        if self.takeover_active:
            self.release_takeover()
        targets = self._get_all_targets()
        for sid in targets:
            self._emit(socketio, "ssh_disconnected", {"reason": "连接已断开"}, to=sid)

    def write(self, data, from_sid):
        if self._closed or not self.channel:
            return False
        # Check if this sid is allowed to send input
        if self.takeover_active:
            if from_sid != self.takeover_sid:
                return False  # Only takeover can send
        else:
            if from_sid != self.owner_sid:
                return False  # Only owner can send
        self.last_activity = time.time()
        self.last_input_time = time.time()
        try:
            self.channel.send(data)
            return True
        except Exception:
            return False

    def resize(self, cols, rows, from_sid):
        if self._closed or not self.channel:
            return
        if self.takeover_active:
            if from_sid != self.takeover_sid:
                return
        else:
            if from_sid != self.owner_sid:
                return
        try:
            self.channel.resize_pty(width=cols, height=rows)
        except Exception:
            pass

    def bind_owner(self, sid):
        self.owner_sid = sid
        self.status = "connected"
        self.disconnected_at = None

    def bind_takeover(self, sid, user_info):
        self.takeover_sid = sid
        self.takeover_active = True
        self.takeover_by = user_info

    def bind_viewer(self, sid):
        self.viewer_sids.add(sid)

    def unbind_owner(self, sid):
        if self.owner_sid == sid:
            self.owner_sid = None
            self.status = "disconnected"
            self.disconnected_at = time.time()

    def unbind_takeover(self, sid):
        if self.takeover_sid == sid:
            self.takeover_sid = None
            self.release_takeover()

    def unbind_viewer(self, sid):
        self.viewer_sids.discard(sid)

    def release_takeover(self):
        self.takeover_active = False
        self.takeover_by = None
        self.takeover_sid = None

    def request_reconnect(self, user_info):
        self.reconnect_requestor = user_info
        self.reconnect_expires_at = time.time() + 10

    def check_reconnect_timeout(self):
        if self.reconnect_expires_at and time.time() >= self.reconnect_expires_at:
            return True
        return False

    def clear_reconnect_request(self):
        self.reconnect_requestor = None
        self.reconnect_expires_at = None

    def get_log_replay(self):
        return "".join(self.log_buffer)

    def close(self):
        self._closed = True
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
        self.status = "terminated"

    def to_dict(self):
        return {
            "session_id": self.session_id,
            "cluster_name": self.cluster_name,
            "owner": self.owner,
            "status": self.status,
            "takeover_active": self.takeover_active,
            "takeover_by": self.takeover_by,
            "host": self.host,
            "port": self.port,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "last_input_time": self.last_input_time,
            "disconnected_at": self.disconnected_at,
            "viewer_count": len(self.viewer_sids),
            "has_reconnect_request": self.reconnect_requestor is not None,
            "reconnect_requestor": self.reconnect_requestor,
            "reconnect_expires_at": self.reconnect_expires_at,
        }


class SSHManager:
    def __init__(self, socketio=None):
        self._sessions = {}
        self._user_cluster_map = {}
        self._ws_to_session = {}
        self._socketio = socketio

        # Configurable limits
        self.global_max = 64
        self.student_max = 3
        self.teacher_max = 8
        self.idle_timeout = 1800
        self.retention_time = 1800

        self._lock = threading.Lock()
        self._cleanup_running = False
        self._cleanup_thread = None

    def init_app(self, socketio):
        self._socketio = socketio
        self._start_cleanup_thread()

    def _start_cleanup_thread(self):
        self._cleanup_running = True
        t = threading.Thread(target=self._cleanup_loop, daemon=True)
        t.start()

    def _cleanup_loop(self):
        while self._cleanup_running:
            time.sleep(30)
            self._cleanup()

    def _cleanup(self):
        now = time.time()
        to_remove = []
        with self._lock:
            for sid, session in self._sessions.items():
                if session.status == "terminated":
                    to_remove.append(sid)
                elif session.status == "disconnected" and session.disconnected_at:
                    if now - session.disconnected_at > self.retention_time:
                        to_remove.append(sid)
                elif session.status == "connected":
                    if now - session.last_input_time > self.idle_timeout:
                        to_remove.append(sid)
        for sid in to_remove:
            self._remove_session(sid)

    def _count_user_sessions(self, user_id, role):
        count = 0
        for session in self._sessions.values():
            if session.owner["user_id"] == user_id and session.status != "terminated":
                count += 1
        return count

    def create_session(self, cluster_name, owner, host, port, ssh_user, ssh_pass):
        user_id = owner["user_id"]
        role = owner["role"]

        key = f"{user_id}:{cluster_name}"
        with self._lock:
            if key in self._user_cluster_map:
                existing_id = self._user_cluster_map[key]
                existing = self._sessions.get(existing_id)
                if existing and existing.status != "terminated":
                    raise SessionExistsError("已有连接存在，请重新连接或终止后重试")
                if existing:
                    existing.close()
                    del self._sessions[existing_id]
                del self._user_cluster_map[key]

            if len(self._sessions) >= self.global_max:
                raise TooManyConnectionsError("服务器连接数已达上限，请稍后重试")

            user_count = self._count_user_sessions(user_id, role)
            max_allowed = self.student_max if role == "student" else self.teacher_max
            if user_count >= max_allowed:
                raise TooManyConnectionsError(
                    f"您已达到最大连接数限制 ({max_allowed})，请先终止不需要的连接"
                )

            session_id = str(uuid.uuid4())
            session = SSHSession(
                session_id, cluster_name, owner, host, port, ssh_user, ssh_pass
            )
            self._sessions[session_id] = session
            self._user_cluster_map[key] = session_id
            return session

    def connect_session(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise SSHConnectionError("会话不存在")
        session.connect(self._socketio)

    def get_session_by_cluster(self, user_id, cluster_name):
        key = f"{user_id}:{cluster_name}"
        with self._lock:
            sid = self._user_cluster_map.get(key)
        if sid:
            return self._sessions.get(sid)
        return None

    def get_session(self, session_id):
        return self._sessions.get(session_id)

    def bind_ws(self, sid, session_id, role="owner"):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        self._ws_to_session[sid] = session_id
        if role == "owner":
            session.bind_owner(sid)
        elif role == "takeover":
            session.takeover_sid = sid
        elif role == "viewer":
            session.bind_viewer(sid)
        return True

    def unbind_ws(self, sid):
        with self._lock:
            session_id = self._ws_to_session.pop(sid, None)
        if session_id:
            session = self._sessions.get(session_id)
            if session:
                if session.owner_sid == sid:
                    session.unbind_owner(sid)
                elif session.takeover_sid == sid:
                    session.unbind_takeover(sid)
                else:
                    session.unbind_viewer(sid)

    def write(self, sid, data):
        with self._lock:
            session_id = self._ws_to_session.get(sid)
        if session_id:
            session = self._sessions.get(session_id)
            if session:
                session.write(data, sid)

    def resize(self, sid, cols, rows):
        with self._lock:
            session_id = self._ws_to_session.get(sid)
        if session_id:
            session = self._sessions.get(session_id)
            if session:
                session.resize(cols, rows, sid)

    def terminate_session(self, session_id):
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                key = f"{session.owner['user_id']}:{session.cluster_name}"
                self._user_cluster_map.pop(key, None)
        if session:
            session.close()
        return session is not None

    def terminate_by_owner(self, user_id, cluster_name):
        key = f"{user_id}:{cluster_name}"
        with self._lock:
            session_id = self._user_cluster_map.get(key)
        if session_id:
            session = self._sessions.get(session_id)
            if session and session.owner["user_id"] == user_id:
                return self.terminate_session(session_id)
        return False

    def takeover_session(self, session_id, user_info):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False, "会话不存在"
        if session.takeover_active:
            return False, "该会话已被其他用户接管"
        session.takeover_active = True
        session.takeover_by = user_info
        return True, session

    def release_takeover(self, session_id):
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            session.release_takeover()
            return True
        return False

    def view_session(self, session_id, sid):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        session.bind_viewer(sid)
        self._ws_to_session[sid] = session_id
        return True

    def unview_session(self, sid):
        self.unbind_ws(sid)

    def request_reconnect(self, session_id, user_info):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False, "会话不存在"
        session.request_reconnect(user_info)
        return True, session

    def respond_reconnect(self, session_id, accepted):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        if accepted:
            session.release_takeover()
            session.clear_reconnect_request()
        else:
            session.clear_reconnect_request()
        return True

    def list_sessions(self, filter_user_id=None, filter_role=None):
        with self._lock:
            result = []
            for session in self._sessions.values():
                if filter_user_id and session.owner["user_id"] != filter_user_id:
                    continue
                if filter_role and session.owner["role"] != filter_role:
                    continue
                result.append(session.to_dict())
            return result

    def get_config(self):
        return {
            "global_max": self.global_max,
            "student_max": self.student_max,
            "teacher_max": self.teacher_max,
            "idle_timeout": self.idle_timeout,
            "retention_time": self.retention_time,
        }

    def set_config(self, config):
        if "global_max" in config:
            self.global_max = int(config["global_max"])
        if "student_max" in config:
            self.student_max = int(config["student_max"])
        if "teacher_max" in config:
            self.teacher_max = int(config["teacher_max"])
        if "idle_timeout" in config:
            self.idle_timeout = int(config["idle_timeout"])
        if "retention_time" in config:
            self.retention_time = int(config["retention_time"])

    def _remove_session(self, session_id):
        session = self._sessions.pop(session_id, None)
        if session:
            key = f"{session.owner['user_id']}:{session.cluster_name}"
            self._user_cluster_map.pop(key, None)
            session.close()

    def stop(self):
        self._cleanup_running = False
        with self._lock:
            for sid in list(self._sessions):
                self._remove_session(sid)
