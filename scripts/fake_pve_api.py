"""Loopback-only PVE API simulator; in-memory state, no real PVE access.

Run: python scripts/fake_pve_api.py --port 8006 [--manual]
API: http://127.0.0.1:8006/api2/json; template=9000, node=pve.
GET /_fake/state lists tasks; POST /_fake/tasks/{upid} with
{"exitstatus":"OK"} completes a task. POST /_fake/reset resets this instance.
POST /_fake/fault sets {method,path,mode}: reject or disconnect_after_accept.
Authentication, TLS, disks and guest execution are deliberately not simulated.
"""

import argparse
from copy import deepcopy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import socket
import threading
import time
from urllib.parse import parse_qs, unquote, urlsplit


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message


def integer(value, name, minimum=0, maximum=None):
    if isinstance(value, bool) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ApiError(400, f"Invalid {name}")
    result = int(value)
    if result < minimum or (maximum is not None and result > maximum):
        raise ApiError(400, f"Invalid {name}")
    return result


def allowed(parameters, fields):
    if set(parameters) - set(fields):
        raise ApiError(400, "Unsupported parameter")


class FakeState:
    def __init__(self, auto_polls):
        self.lock = threading.RLock()
        self.auto_polls = auto_polls
        self.sequence = 0
        self.reset()

    def reset(self):
        with self.lock:
            self.vms = {9000: {"vmid": 9000, "node": "pve", "template": 1,
                "status": "stopped", "config": {"name": "fake-template", "cores": 1, "memory": 512}, "pending": {}}}
            self.tasks = {}
            self.calls = []
            self.fault = None

    def view(self):
        return {"simulated": True, "vms": deepcopy(list(self.vms.values())),
            "tasks": [self.task_status(t) for t in self.tasks.values()], "calls": deepcopy(self.calls)}

    def task_status(self, task):
        result = {k: task[k] for k in ("upid", "node", "type", "id", "user", "status", "starttime")}
        if task["status"] == "stopped":
            result["exitstatus"] = task["exitstatus"]
        return result

    def vm(self, vmid):
        if vmid not in self.vms:
            raise ApiError(404, "VM not found")
        return self.vms[vmid]

    def busy(self, vmid):
        return any(t["status"] == "running" and t["vmid"] == vmid for t in self.tasks.values())

    def task(self, upid):
        if upid not in self.tasks:
            raise ApiError(404, "Task not found")
        return self.tasks[upid]

    def submit(self, action, vmid, parameters):
        self.sequence += 1
        upid = f"UPID:pve:{self.sequence:08X}:00000001:{int(time.time()):08X}:qm{action}:{vmid}:fake@pve:"
        task = {"upid": upid, "node": "pve", "type": "qm" + action, "id": str(vmid),
            "vmid": vmid, "user": "fake@pve", "status": "running", "starttime": int(time.time()),
            "action": action, "parameters": deepcopy(parameters), "polls": 0}
        self.tasks[upid] = task
        return upid

    def apply_config(self, vm, parameters):
        target = vm["pending"] if vm["status"] == "running" else vm["config"]
        target.update(parameters)
        if vm["status"] == "stopped":
            for key in parameters:
                vm["pending"].pop(key, None)

    def complete(self, upid, result):
        task = self.task(upid)
        if task["status"] == "stopped":
            if task["exitstatus"] != result:
                raise ApiError(409, "Task already completed with another result")
            return self.task_status(task)
        action, vmid, parameters = task["action"], task["vmid"], task["parameters"]
        if result == "OK":
            if action == "clone":
                self.vms[vmid] = {"vmid": vmid, "node": "pve", "template": 0, "status": "stopped",
                    "config": deepcopy(parameters["config"]), "pending": {}}
            elif action == "delete":
                del self.vms[vmid]
            elif action == "config":
                self.apply_config(self.vm(vmid), parameters)
            else:
                vm = self.vm(vmid)
                vm["status"] = "stopped" if action in {"stop", "shutdown"} else "running"
                if action in {"start", "reboot"}:
                    vm["config"].update(vm["pending"])
                    vm["pending"].clear()
        task.update(status="stopped", exitstatus=result)
        return self.task_status(task)

    def digest(self, vm):
        # This is a deterministic fake revision, not the digest of a PVE config file.
        return hashlib.sha1(json.dumps([vm["config"], vm["pending"]], sort_keys=True).encode()).hexdigest()

    def config_parameters(self, vm, parameters):
        allowed(parameters, {"name", "cores", "memory", "sshkeys", "ciuser", "nameserver", "net0", "ipconfig0", "digest", "background_delay"})
        result = {k: v for k, v in parameters.items() if k not in {"digest", "background_delay"}}
        if "digest" in parameters and parameters["digest"] != self.digest(vm):
            raise ApiError(409, "Configuration digest mismatch")
        for name, minimum in (("cores", 1), ("memory", 16)):
            if name in result:
                result[name] = integer(result[name], name, minimum)
        for name, value in result.items():
            if name not in {"cores", "memory"} and not isinstance(value, str):
                raise ApiError(400, "Invalid configuration value")
        if "sshkeys" in result:
            result["sshkeys"] = unquote(result["sshkeys"])
        if "background_delay" in parameters:
            integer(parameters["background_delay"], "background_delay", 1, 30)
        return result

    def route(self, method, path, parameters):
        if path == "/_fake/state" and method == "GET":
            return self.view()
        if path == "/_fake/reset" and method == "POST":
            self.reset()
            return None
        if path == "/_fake/fault" and method == "POST":
            allowed(parameters, {"method", "path", "mode"})
            if not isinstance(parameters.get("mode"), str) or parameters["mode"] not in {"reject", "disconnect_after_accept"}:
                raise ApiError(400, "Invalid fault mode")
            if not isinstance(parameters.get("method"), str) or parameters["method"] not in {"GET", "POST", "PUT", "DELETE"}:
                raise ApiError(400, "Invalid fault method")
            if not isinstance(parameters.get("path"), str) or not parameters["path"].startswith("/api2/json/"):
                raise ApiError(400, "Fault path must target this fake API")
            self.fault = dict(parameters)
            return None
        if path.startswith("/_fake/tasks/") and method == "POST":
            allowed(parameters, {"exitstatus"})
            result = parameters.get("exitstatus", "OK")
            if not isinstance(result, str) or not result or len(result) > 256:
                raise ApiError(400, "Invalid exitstatus")
            return self.complete(path[len("/_fake/tasks/"):], result)
        if not path.startswith("/api2/json/"):
            raise ApiError(404, "Unknown fake endpoint")
        route = path[len("/api2/json"):]
        if route == "/version" and method == "GET":
            return {"version": "7.4-20", "release": "7.4", "repoid": "fake", "simulated": True}
        if route == "/nodes" and method == "GET":
            return [{"node": "pve", "status": "online"}]
        if route == "/cluster/nextid" and method == "GET":
            used = set(self.vms) | {t["vmid"] for t in self.tasks.values() if t["status"] == "running"}
            if "vmid" in parameters:
                vmid = integer(parameters["vmid"], "vmid", 1)
                if vmid in used:
                    raise ApiError(409, "VMID already exists")
                return vmid
            vmid = 100
            while vmid in used:
                vmid += 1
            return vmid
        if route in {"/cluster/resources", "/nodes/pve/qemu"} and method == "GET":
            return [{"vmid": vm["vmid"], "node": "pve", "type": "qemu", "template": vm["template"],
                "name": vm["config"]["name"], "status": vm["status"]} for vm in self.vms.values()]
        match = re.fullmatch(r"/nodes/pve/tasks/(UPID:[^/]+)/(status|log)", route)
        if match and method == "GET":
            task = self.task(match[1])
            if match[2] == "status":
                if task["status"] == "running" and self.auto_polls is not None:
                    if task["polls"] >= self.auto_polls:
                        self.complete(task["upid"], "OK")
                    else:
                        task["polls"] += 1
                return self.task_status(task)
            return [{"n": 1, "t": "TASK " + (task.get("exitstatus") or "RUNNING")}]
        match = re.fullmatch(r"/nodes/pve/qemu/([0-9]+)(?:/(.*))?", route)
        if not match:
            raise ApiError(404, "Unknown fake endpoint")
        vmid = integer(match[1], "vmid", 1)
        suffix = match[2] or ""
        vm = self.vm(vmid)
        if method == "GET":
            if suffix == "status/current":
                return {"vmid": vmid, "status": vm["status"], "qmpstatus": vm["status"],
                    "cpus": vm["config"]["cores"], "maxmem": vm["config"]["memory"] * 1024 * 1024}
            if suffix == "config":
                current = integer(parameters.get("current", 0), "current", 0, 1)
                return {**vm["config"], **({} if current else vm["pending"]), "digest": self.digest(vm)}
            if suffix == "pending":
                return [{"key": k, "value": str(vm["config"].get(k, "")), "pending": str(v)} for k, v in vm["pending"].items()]
            raise ApiError(404, "Unknown fake endpoint")
        if vm["template"] and not (method == "POST" and suffix == "clone"):
            raise ApiError(403, "Seed template is read-only in this simulator")
        if self.busy(vmid):
            raise ApiError(409, "VM locked by fake task")
        if method == "POST" and suffix == "clone":
            allowed(parameters, {"newid", "name", "full", "storage", "target", "description"})
            newid = integer(parameters.get("newid", ""), "newid", 1)
            if newid in self.vms or self.busy(newid):
                raise ApiError(409, "VMID already exists")
            integer(parameters.get("full", 0), "full", 0, 1)
            if parameters.get("target", "pve") != "pve":
                raise ApiError(400, "Simulator only has node pve")
            if not isinstance(parameters.get("name", "clone"), str):
                raise ApiError(400, "Invalid name")
            return self.submit("clone", newid, {"config": {**vm["config"], "name": parameters.get("name", f"fake-{newid}")}})
        if suffix == "config" and method in {"PUT", "POST"}:
            config = self.config_parameters(vm, parameters)
            if method == "PUT":
                if "background_delay" in parameters:
                    raise ApiError(400, "background_delay requires POST")
                self.apply_config(vm, config)
                return None
            upid = self.submit("config", vmid, config)
            if "background_delay" in parameters:
                self.complete(upid, "OK")
                return None
            return upid
        if method == "POST" and suffix in {"status/start", "status/stop", "status/shutdown", "status/reboot"}:
            allowed(parameters, {"timeout", "forceStop"} if suffix == "status/shutdown" else {"timeout"})
            if "timeout" in parameters:
                integer(parameters["timeout"], "timeout")
            if "forceStop" in parameters:
                integer(parameters["forceStop"], "forceStop", 0, 1)
            action = suffix.split("/")[1]
            if action == "reboot" and vm["status"] != "running":
                raise ApiError(409, "VM is not running")
            return self.submit(action, vmid, {})
        if method == "DELETE" and not suffix:
            allowed(parameters, {"purge", "destroy-unreferenced-disks"})
            for key, value in parameters.items():
                integer(value, key, 0, 1)
            if vm["status"] == "running":
                raise ApiError(409, "VM is running")
            return self.submit("delete", vmid, {})
        raise ApiError(404, "Unknown fake endpoint")


class Handler(BaseHTTPRequestHandler):
    server_version = "FakePVE/1.0"

    def handle_request(self):
        try:
            if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
                raise ApiError(403, "Use the loopback address printed at startup")
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_port}":
                raise ApiError(403, "Cross-origin requests are forbidden")
            if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
                raise ApiError(403, "Cross-origin requests are forbidden")
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "Transfer encoding is unsupported")
            size = integer(self.headers.get("Content-Length", 0), "Content-Length")
            if size > 65536:
                raise ApiError(413, "Request body too large")
            url = urlsplit(self.path)
            if re.search(r"%2f|%5c", url.path, re.I):
                raise ApiError(400, "Encoded path separator is unsupported")
            path = unquote(url.path)
            parameters = {k: v[-1] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            if size:
                raw = self.rfile.read(size).decode("utf-8")
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0]
                if content_type == "application/json":
                    body = json.loads(raw)
                    if not isinstance(body, dict):
                        raise ApiError(400, "JSON body must be an object")
                    parameters.update(body)
                elif content_type == "application/x-www-form-urlencoded":
                    parameters.update({k: v[-1] for k, v in parse_qs(raw, keep_blank_values=True).items()})
                else:
                    raise ApiError(415, "Use JSON or form encoding")
            with self.server.state.lock:
                state = self.server.state
                state.calls.append({"method": self.command, "path": path})
                fault = state.fault
                if fault and (fault["method"], fault["path"]) == (self.command, path):
                    state.fault = None
                else:
                    fault = None
                if fault and fault["mode"] == "reject":
                    raise ApiError(503, "Injected fake rejection")
                result = state.route(self.command, path, parameters)
                if fault and fault["mode"] == "disconnect_after_accept":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
            self.respond(200, {"data": result})
        except ApiError as exc:
            self.respond(exc.status, {"data": None, "errors": {"message": exc.message}})
        except (ValueError, UnicodeError):
            self.respond(400, {"data": None, "errors": {"message": "Malformed request"}})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def respond(self, status, payload):
        body = json.dumps(payload, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = handle_request

    def log_message(self, *args):
        pass


def make_server(*, port=0, auto_polls=1):
    if auto_polls is not None and (type(auto_polls) is not int or auto_polls < 0):
        raise ValueError("auto_polls must be a nonnegative integer or None")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.state = FakeState(auto_polls)
    return server


def main():
    parser = argparse.ArgumentParser(description="Local HTTP-only fake PVE 7.4-20 API (no real VM operations).", epilog="Template 9000, node pve. Controls: GET /_fake/state; POST /_fake/tasks/{upid} with exitstatus; POST /_fake/reset; POST /_fake/fault.")
    parser.add_argument("--port", type=int, default=8006)
    parser.add_argument("--manual", action="store_true", help="Only complete tasks using /_fake/tasks/{upid}")
    args = parser.parse_args()
    with make_server(port=args.port, auto_polls=None if args.manual else 1) as server:
        print(f"Fake PVE (HTTP, no authentication): http://127.0.0.1:{server.server_port}/api2/json", flush=True)
        print("In-memory only; not a real PVE instance. Ctrl+C to stop.", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
