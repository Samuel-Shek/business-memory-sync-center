"""Version-checked local Codex transport. Never invokes a model outside its UI.

app-server is used only for durable metadata/history; the desktop owner receives
the user turn through its IPC transport and retains approvals and tool handling.
"""
from __future__ import annotations

from archive_paths import path as _archive_path
from archive_guard import require_opt_in as _archive_require_opt_in
_archive_require_opt_in()


import json
import os
import queue
import re
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path

PROJECT_ID = "example-project-id"
PROJECT_NAME = "业务记忆同步中心"
CODEX_DIRECTORY = Path.home() / ".codex"
PROJECT_ROOT = CODEX_DIRECTORY / ".chatgpt-projects" / PROJECT_ID
APP_PATH = Path("/Applications/ChatGPT.app")
BINARY_RELATIVES = (
    "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
    "Contents/Resources/codex",
)
THREAD_RE = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")


class NativeError(RuntimeError):
    def __init__(self, code, message=None):
        super().__init__(message or code)
        self.code = code


def resolve_binary(app_path=None):
    """Resolve only executable candidates contained in the selected app bundle."""
    app = Path(app_path) if app_path is not None else APP_PATH
    try:
        root = app.resolve(strict=True)
        for relative in BINARY_RELATIVES:
            candidate = app / relative
            if not os.path.lexists(candidate):
                continue
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_file() or not os.access(resolved, os.X_OK):
                raise NativeError("codex_missing")
            return resolved
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, NativeError):
            raise
        raise NativeError("codex_missing") from exc
    raise NativeError("codex_missing")


def thread_id(value):
    if not isinstance(value, str) or not THREAD_RE.fullmatch(value):
        raise NativeError("invalid_thread_id")
    return value


class AppServer:
    def __enter__(self):
        binary = resolve_binary()
        self.messages = queue.Queue()
        self.sequence = 0
        env = dict(os.environ)
        for key in ("CODEX_THREAD_ID", "CODEX_TURN_ID", "NODE_REPL_REQUEST_META"):
            env.pop(key, None)
        self.process = subprocess.Popen(
            [str(binary), "app-server", "--stdio"], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", env=env,
        )
        def reader():
            try:
                for line in self.process.stdout:
                    self.messages.put(json.loads(line))
            finally:
                self.messages.put(None)
        threading.Thread(target=reader, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "sync_center_handoff", "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.process.stdin.write('{"method":"initialized","params":{}}\n')
            self.process.stdin.flush()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def call(self, method, params, timeout=25):
        self.sequence += 1
        request_id = self.sequence
        self.process.stdin.write(json.dumps({"id": request_id, "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                result = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise NativeError("codex_rpc_timeout") from exc
            if result is None:
                raise NativeError("codex_rpc_closed")
            if result.get("id") == request_id:
                if "error" in result:
                    raise NativeError("codex_rpc_rejected", str(result["error"]))
                return result["result"]
            if time.monotonic() >= deadline:
                raise NativeError("codex_rpc_timeout")

    def __exit__(self, *_):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=4)
        self.process.stdout.close()

    def project(self):
        # Resolve the current native identity from the actual app migration map.
        state = json.loads((CODEX_DIRECTORY / ".codex-global-state.json").read_text())
        mapping = state.get("app-server-project-id-by-legacy-project-id-by-host", {})
        native_id = mapping.get("local:" + str(CODEX_DIRECTORY), {}).get(PROJECT_ID)
        if not native_id:
            raise NativeError("codex_project_unavailable")
        value = self.call("project/read", {"projectId": native_id})["project"]
        roots = [row["path"] for row in value.get("roots", [])]
        if value.get("name") != PROJECT_NAME or roots != [str(PROJECT_ROOT)]:
            raise NativeError("codex_project_mismatch")
        return native_id

    def read(self, identifier):
        return self.call("thread/read", {"threadId": thread_id(identifier), "includeTurns": True})["thread"]

    def initial_collaboration_mode(self):
        """Resolve first-turn defaults before the desktop can inherit an empty mode."""
        try:
            result = self.call("config/read", {"cwd": str(PROJECT_ROOT), "includeLayers": False})
        except NativeError as exc:
            raise NativeError("codex_settings_unavailable") from exc
        config = result.get("config") if isinstance(result, dict) else None
        model = config.get("model") if isinstance(config, dict) else None
        effort = config.get("model_reasoning_effort") if isinstance(config, dict) else None
        if (not isinstance(model, str) or not model.strip() or not isinstance(effort, str)
                or effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}):
            raise NativeError("codex_settings_unavailable")
        return {"mode": "default", "settings": {"model": model, "reasoning_effort": effort,
                                                  "developer_instructions": None}}

    def create(self, native_project_id, name, on_created):
        result = self.call("thread/start", {"cwd": str(PROJECT_ROOT), "projectId": native_project_id,
                                           "ephemeral": False})
        identifier = thread_id(result["thread"]["id"])
        on_created(identifier)
        # No turn is started here. This process exits before the desktop resumes.
        self.call("thread/name/set", {"threadId": identifier, "name": name})
        return identifier


class DesktopIPC:
    def __enter__(self):
        path = CODEX_DIRECTORY / "ipc/ipc.sock"
        try:
            info, parent = path.lstat(), path.parent.lstat()
        except OSError as exc:
            raise NativeError("codex_not_ready") from exc
        if (not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid()
                or parent.st_uid != os.getuid() or info.st_mode & 0o077 or parent.st_mode & 0o077):
            raise NativeError("codex_ipc_ownership")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(6)
        self.client_id = "initializing-client"
        try:
            self.socket.connect(str(path))
            result = self.call("initialize", {"clientType": "sync-center-handoff"}, version=0)
            self.client_id = result["result"]["clientId"]
        except BaseException:
            self.socket.close()
            raise
        return self

    def read_exact(self, length):
        result = b""
        while len(result) < length:
            value = self.socket.recv(length - len(result))
            if not value:
                raise NativeError("codex_ipc_closed")
            result += value
        return result

    def call(self, method, params, *, version, target=None, timeout=6):
        identifier = str(uuid.uuid4())
        message = {"type": "request", "requestId": identifier, "sourceClientId": self.client_id,
                   "version": version, "method": method, "params": params, "timeoutMs": int(timeout * 1000)}
        if target:
            message["targetClientId"] = target
        data = json.dumps(message, ensure_ascii=False).encode()
        self.socket.sendall(struct.pack("<I", len(data)) + data)
        deadline = time.monotonic() + timeout + 1
        while True:
            if time.monotonic() >= deadline:
                raise NativeError("codex_ipc_timeout")
            self.socket.settimeout(max(0.1, deadline - time.monotonic()))
            size = struct.unpack("<I", self.read_exact(4))[0]
            if size > 8 * 1024 * 1024:
                raise NativeError("codex_ipc_incompatible")
            response = json.loads(self.read_exact(size))
            if response.get("requestId") == identifier:
                if response.get("resultType") != "success":
                    raise NativeError("codex_ipc_rejected", str(response.get("error", "unknown")))
                return response

    def owner(self, identifier):
        response = self.call("thread-owner-discovery", {"hostId": "local", "conversationId": thread_id(identifier)}, version=1)
        if not response.get("handledByClientId") or response.get("result", {}).get("supportsUntrustedAppInput") is not True:
            raise NativeError("codex_ipc_incompatible")
        return response["handledByClientId"]

    def submit(self, identifier, owner, message_id, prompt, *, collaboration_mode=None):
        request = {"threadId": thread_id(identifier),
                   "input": [{"type": "text", "text": prompt, "text_elements": []}],
                   "clientUserMessageId": message_id}
        if collaboration_mode is not None:
            request["collaborationMode"] = collaboration_mode
        return self.call("thread-follower-start-turn", {
            "conversationId": thread_id(identifier),
            "turnStart": {"request": request,
                          "context": {"inheritThreadSettings": True}},
        }, version=2, target=owner, timeout=40)

    def __exit__(self, *_):
        self.socket.close()


def open_thread(identifier):
    resolve_binary()
    subprocess.run(["/usr/bin/open", "-a", str(APP_PATH), "codex://threads/" + thread_id(identifier)],
                   check=True, timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_owner(identifier, timeout=25):
    deadline = time.monotonic() + timeout
    while True:
        try:
            with DesktopIPC() as ipc:
                return ipc.owner(identifier)
        except (NativeError, OSError) as exc:
            if isinstance(exc, NativeError) and exc.code in {"codex_ipc_ownership", "codex_ipc_incompatible"}:
                raise
            if time.monotonic() >= deadline:
                raise NativeError("codex_not_ready")
            time.sleep(0.5)


def receipt_in_thread(thread, message_id, prompt):
    """Require the exact submitted user text in persisted native history."""
    for turn in thread.get("turns", []):
        for item in turn.get("items", []):
            if item.get("type") != "userMessage":
                continue
            content = item.get("content", [])
            text = "".join(row.get("text", "") for row in content if row.get("type") in {"text", "inputText"})
            if text == prompt and item.get("clientId") == message_id:
                return {"turn_id": turn.get("id"), "user_message_id": item.get("id"),
                        "native_status": turn.get("status")}
    return None
