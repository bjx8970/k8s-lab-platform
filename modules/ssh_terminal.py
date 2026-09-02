import socket
import threading
import time
import uuid
from collections import deque

import paramiko

from modules.db import get_config as db_get_config, set_config as db_set_config
from modules.audit import SecretTextSanitizer, sanitize_text


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
        self._binding_authorizer = None
        self._text_sanitizer = SecretTextSanitizer()

    def set_binding_authorizer(self, callback):
        """Set the callback used to authorize every bound WebSocket.

        The callback is deliberately kept outside this class's lock.  The
        application callback may need to consult the database, and invoking it
        while holding the session/manager locks would make disconnect and
        cleanup paths susceptible to lock inversion.
        """
        self._binding_authorizer = callback

    def _emit(self, socketio, event, data, to=None):
        if not socketio or not to or not self._allowed(to):
            return
        socketio.emit(event, data, to=to, namespace="/webssh")

    def _role_for(self, sid):
        if sid and sid == self.owner_sid:
            return "owner"
        if sid and sid == self.takeover_sid:
            return "takeover"
        if sid and sid in self.viewer_sids:
            return "viewer"
        return None

    def _allowed(self, sid):
        role = self._role_for(sid)
        try:
            return bool(role and self._binding_authorizer
                        and self._binding_authorizer(sid, self, role))
        except Exception:
            return False

    def connect(self, socketio):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=10)
            self.transport = paramiko.Transport(sock)
            self.transport.set_keepalive(30)
            self.transport.connect(username=self.ssh_user, password=self.ssh_pass)
        except paramiko.AuthenticationException:
            raise SSHConnectionError("SSH 认证失败，请检查密码")
        except paramiko.SSHException:
            raise SSHConnectionError("SSH 连接失败") from None
        except (OSError, socket.timeout):
            raise SSHConnectionError("无法连接到虚拟机") from None

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
                        text = sanitize_text(self._text_sanitizer.feed(
                            data.decode("utf-8", errors="replace")
                        ))
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
        bindings = []
        if self.owner_sid:
            bindings.append((self.owner_sid, "owner"))
        if self.takeover_sid:
            bindings.append((self.takeover_sid, "takeover"))
        bindings.extend((sid, "viewer") for sid in tuple(self.viewer_sids))

        targets = set()
        for sid, role in bindings:
            authorizer = self._binding_authorizer
            if authorizer is None:
                continue
            try:
                allowed = bool(authorizer(sid, self, role))
            except Exception:
                allowed = False
            if allowed:
                targets.add(sid)
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
        if self._closed or not self.channel or not self._allowed(from_sid):
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
            # Chunk large data to avoid PTY buffer issues
            chunk_size = 4096
            if len(data) <= chunk_size:
                self.channel.send(data)
            else:
                for i in range(0, len(data), chunk_size):
                    if not self._allowed(from_sid):
                        return False
                    chunk = data[i:i + chunk_size]
                    self.channel.send(chunk)
                    time.sleep(0.01)
            return True
        except Exception:
            return False

    def resize(self, cols, rows, from_sid):
        if self._closed or not self.channel or not self._allowed(from_sid):
            return False
        if self.takeover_active:
            if from_sid != self.takeover_sid:
                return False
        else:
            if from_sid != self.owner_sid:
                return False
        try:
            self.channel.resize_pty(width=cols, height=rows)
            return True
        except Exception:
            return False

    def bind_owner(self, sid):
        self.owner_sid = sid
        if self.channel and not self.channel.closed:
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
        self.clear_reconnect_request()

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

    def get_log_replay(self, sid=None):
        if not self._allowed(sid):
            return ""
        return sanitize_text("".join(self.log_buffer))

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
        self._ws_roles = {}
        self._authorizer = None
        self._socketio = socketio
        self._on_session_terminated = None
        self._on_owner_disconnect = None
        self._on_takeover_released = None

        # Configurable limits
        self.global_max = 64
        self.student_max = 3
        self.teacher_max = 8
        self.idle_timeout = 1800
        self.retention_time = 1800

        persisted = db_get_config("webssh")
        if persisted:
            self.set_config(persisted)

        self._lock = threading.Lock()
        self._cleanup_running = False
        self._cleanup_thread = None

    def set_on_session_terminated(self, callback):
        self._on_session_terminated = callback

    def set_on_owner_disconnect(self, callback):
        self._on_owner_disconnect = callback

    def set_on_takeover_released(self, callback):
        self._on_takeover_released = callback

    def set_authorizer(self, callback):
        """Install the application authorization callback.

        No callback means deny all sensitive WebSSH bindings.  This fail-closed
        default also makes standalone manager use safe until the application
        has wired the current-user/resource policy.
        """
        self._authorizer = callback
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.set_binding_authorizer(self._authorize_binding)

    def _revoke_ws(self, sid, session):
        with self._lock:
            if self._ws_to_session.get(sid) != session.session_id:
                return
            self._ws_to_session.pop(sid, None)
            self._ws_roles.pop(sid, None)
        if session.owner_sid == sid:
            session.unbind_owner(sid)
        elif session.takeover_sid == sid:
            session.unbind_takeover(sid)
        else:
            session.unbind_viewer(sid)

    def _authorize_binding(self, sid, session, role):
        callback = self._authorizer
        if callback is None:
            allowed = False
        else:
            try:
                allowed = bool(callback(sid, session, role))
            except Exception:
                allowed = False
        if not allowed:
            self._revoke_ws(sid, session)
        return allowed

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
                    to_remove.append((sid, session.cluster_name, session.owner["user_id"]))
                elif session.status == "disconnected" and session.disconnected_at:
                    if now - session.disconnected_at > self.retention_time:
                        to_remove.append((sid, session.cluster_name, session.owner["user_id"]))
                elif session.status == "connected":
                    if now - session.last_input_time > self.idle_timeout:
                        to_remove.append((sid, session.cluster_name, session.owner["user_id"]))
        for sid, cluster_name, user_id in to_remove:
            self._remove_session(sid)
            if self._on_session_terminated:
                self._on_session_terminated(cluster_name, user_id)

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
            session.set_binding_authorizer(self._authorize_binding)
            self._sessions[session_id] = session
            self._user_cluster_map[key] = session_id
            return session

    def connect_session(self, session_id, sid=None):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            raise SSHConnectionError("会话不存在")
        if not self.authorize_ws(sid, session_id, "owner"):
            raise SSHConnectionError("无权连接该会话")
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

    def authorize_ws(self, sid, session_id=None, role=None):
        with self._lock:
            bound_id = self._ws_to_session.get(sid)
            bound_role = self._ws_roles.get(sid)
            session = self._sessions.get(bound_id)
        if not session or (session_id is not None and session_id != bound_id):
            return False
        if role is not None and role != bound_role:
            return False
        return self._authorize_binding(sid, session, bound_role)

    def bound_session(self, sid):
        with self._lock:
            return self._sessions.get(self._ws_to_session.get(sid))

    def emit_to(self, session, event, data, sid):
        if not sid or not self.authorize_ws(sid, session.session_id):
            return False
        session._emit(self._socketio, event, data, to=sid)
        return True

    def replay(self, sid):
        session = self.bound_session(sid)
        if not session or not self.authorize_ws(sid, session.session_id):
            return False
        replay = session.get_log_replay(sid)
        if replay:
            return self.emit_to(session, "log_replay", {"lines": replay}, sid)
        return True

    def bind_ws(self, sid, session_id, role="owner"):
        if role not in {"owner", "takeover", "viewer"}:
            return False
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        if not self._authorize_binding(sid, session, role):
            return False

        with self._lock:
            previous_id = self._ws_to_session.get(sid)
            previous_role = self._ws_roles.get(sid)
        if previous_id and (previous_id != session_id or previous_role != role):
            self.unbind_ws(sid)
        displaced_sid = session.owner_sid if role == "owner" else (
            session.takeover_sid if role == "takeover" else None
        )
        takeover_by = session.takeover_by
        if displaced_sid and displaced_sid != sid:
            self._revoke_ws(displaced_sid, session)
        with self._lock:
            self._ws_to_session[sid] = session_id
            self._ws_roles[sid] = role
        if role == "owner":
            session.bind_owner(sid)
        elif role == "takeover":
            session.bind_takeover(sid, takeover_by or {})
        elif role == "viewer":
            session.bind_viewer(sid)
        return True

    def unbind_ws(self, sid):
        with self._lock:
            session_id = self._ws_to_session.pop(sid, None)
            self._ws_roles.pop(sid, None)
        if session_id:
            session = self._sessions.get(session_id)
            if session:
                if session.owner_sid == sid:
                    session.unbind_owner(sid)
                    if self._on_owner_disconnect:
                        self._on_owner_disconnect(session.cluster_name, session.owner["user_id"])
                elif session.takeover_sid == sid:
                    session.unbind_takeover(sid)
                    if self._on_takeover_released:
                        self._on_takeover_released(session)
                else:
                    session.unbind_viewer(sid)

    def write(self, sid, data):
        with self._lock:
            session_id = self._ws_to_session.get(sid)
            role = self._ws_roles.get(sid)
        if session_id:
            session = self._sessions.get(session_id)
            if session and role and self._authorize_binding(sid, session, role):
                return session.write(data, sid)
        return False

    def resize(self, sid, cols, rows):
        with self._lock:
            session_id = self._ws_to_session.get(sid)
            role = self._ws_roles.get(sid)
        if session_id:
            session = self._sessions.get(session_id)
            if session and role and self._authorize_binding(sid, session, role):
                return session.resize(cols, rows, sid)
        return False

    def terminate_session(self, session_id):
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session:
                key = f"{session.owner['user_id']}:{session.cluster_name}"
                self._user_cluster_map.pop(key, None)
                bound_sids = [sid for sid, value in self._ws_to_session.items()
                              if value == session_id]
                for sid in bound_sids:
                    self._ws_to_session.pop(sid, None)
                    self._ws_roles.pop(sid, None)
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

    def takeover_session(self, session_id, user_info, sid=None):
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False, "会话不存在"
        if not sid or not self._authorize_binding(sid, session, "takeover"):
            return False, "权限不足"
        if session.takeover_active and session.takeover_by != user_info:
            return False, "该会话已被其他用户接管"
        session.takeover_active = True
        session.takeover_by = user_info
        if not self.bind_ws(sid, session_id, "takeover"):
            session.release_takeover()
            return False, "权限不足"
        return True, session

    def release_takeover(self, session_id, sid=None):
        if not self.authorize_ws(sid, session_id, "takeover"):
            return False
        with self._lock:
            session = self._sessions.get(session_id)
        if session:
            self.unbind_ws(sid)
            return True
        return False

    def view_session(self, session_id, sid):
        return self.bind_ws(sid, session_id, role="viewer")

    def unview_session(self, sid):
        self.unbind_ws(sid)

    def request_reconnect(self, session_id, user_info, sid=None):
        if not self.authorize_ws(sid, session_id, "owner"):
            return False, "权限不足"
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False, "会话不存在"
        session.request_reconnect(user_info)
        return True, session

    def respond_reconnect(self, session_id, accepted, sid=None):
        if not self.authorize_ws(sid, session_id, "takeover"):
            return False
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return False
        if accepted:
            self.unbind_ws(sid)
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
        db_set_config("webssh", {
            "global_max": self.global_max,
            "student_max": self.student_max,
            "teacher_max": self.teacher_max,
            "idle_timeout": self.idle_timeout,
            "retention_time": self.retention_time,
        })

    def _remove_session(self, session_id):
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session:
            key = f"{session.owner['user_id']}:{session.cluster_name}"
            self._user_cluster_map.pop(key, None)
            with self._lock:
                bound_sids = [sid for sid, value in self._ws_to_session.items()
                              if value == session_id]
                for sid in bound_sids:
                    self._ws_to_session.pop(sid, None)
                    self._ws_roles.pop(sid, None)
            session.close()

    def stop(self):
        self._cleanup_running = False
        with self._lock:
            session_ids = list(self._sessions)
        for sid in session_ids:
            self._remove_session(sid)
