"""Attach to OMO's existing CLI JSONL host; never start another engine.

The CLI socket is not the separate CBOR pi-protocol harness. Authentication,
live-only attachment and entry-tree projection follow OMO's bundled docs/rpc.md.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import time
import uuid

from .common import NativeError, state_dir

MAX_REQUEST_CHARS = 16_777_216
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class RpcError(NativeError):
    def __init__(self, command, reply):
        self.code = reply.get("errorCode")
        self.reply = reply
        super().__init__(f"OMO {command}: {reply.get('errorCode') or reply.get('error') or 'native refusal'}")


def _private(path, kind="file", owner_only=True):
    info = os.lstat(path)
    valid = {"file": stat.S_ISREG, "directory": stat.S_ISDIR, "socket": stat.S_ISSOCK}[kind]
    if not valid(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & (0o077 if owner_only else 0o022):
        raise NativeError(f"Unsafe OMO {kind}: {path}")
    return info


def _read_json(path):
    _private(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NativeError("Unsafe OMO identity record")
        return json.load(stream)


def _process(pid):
    """Linux's start ticks fence PID reuse; ppid supports a native launcher child."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": int(pid), "parent": int(data[1]), "start": data[19]}
    except (OSError, ValueError, IndexError):
        return None


def _same_start(recorded, process):
    if str(recorded) in (process["start"], "linux-proc-start-ticks:" + process["start"]):
        return True
    # Native POSIX releases also publish ps's lstart identity. Never substitute
    # file mtime, registration age, or PID liveness for a process-start match.
    if isinstance(recorded, str) and (recorded.startswith("ps-lstart:") or re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s", recorded)):
        try:
            result = subprocess.run(["ps", "-p", str(process["pid"]), "-o", "lstart="],
                                    capture_output=True, text=True, timeout=2)
            actual = " ".join(result.stdout.split())
            expected = " ".join(recorded.removeprefix("ps-lstart:").split())
            return result.returncode == 0 and actual == expected
        except (OSError, subprocess.TimeoutExpired):
            pass
    return False


def _in_process_family(owner, pid):
    seen = set()
    while owner and owner not in seen:
        if owner == pid:
            return True
        seen.add(owner)
        process = _process(owner)
        owner = process["parent"] if process else None
    return False


def _agent_dir(payload):
    explicit = payload.get("agentDir")
    if explicit:
        if not isinstance(explicit, str) or not os.path.isabs(explicit):
            raise NativeError("OMO agentDir must be absolute")
        return Path(explicit)
    # Ask the native, side-effect-free directory selector. No auth/model startup.
    executable = shutil.which("omo")
    if executable:
        try:
            result = subprocess.run([executable, "config", "agent-dir", "--json"],
                                    cwd=payload.get("cwd") or None, capture_output=True, text=True, timeout=4)
            selected = json.loads(result.stdout)
            if result.returncode == 0 and selected.get("version") == 1 and selected.get("ok") is True and os.path.isabs(selected.get("dir", "")):
                return Path(selected["dir"])
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        raise NativeError("OMO's native agent-directory selector failed; provide the exact agentDir")
    selected = os.environ.get("OMO_CODING_AGENT_DIR") or os.environ.get("PI_CODING_AGENT_DIR")
    return Path(selected).expanduser() if selected else Path.home() / ".omo" / "agent"


def _metadata(payload):
    target = dict(payload)
    pid = target.get("pid")
    if pid is not None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise NativeError("OMO pid must be a positive process ID")
        process = _process(pid)
        if not process:
            raise NativeError("The targeted OMO process is no longer alive")
        record_path = state_dir() / "omo" / f"{pid}.json"
        if record_path.exists():
            record = _read_json(record_path)
            if record.get("pid") != pid or not _same_start(record.get("processStartTime"), process):
                raise NativeError("Stale OMO session publication (process identity changed)")
            if record.get("error"):
                raise NativeError(f"OMO native control registration failed: {record['error']}")
            for key in ("sessionId", "sessionFile", "socket", "hostInstance", "agentDir"):
                if key not in target and record.get(key):
                    target[key] = record[key]
    if target.get("socket") and not target.get("sessionId") and not target.get("sessionFile"):
        raise NativeError("An explicit OMO socket requires its durable session identity; PID-only discovery uses the native registry")
    if not target.get("pid") and not target.get("sessionId") and not target.get("sessionFile"):
        raise NativeError("OMO requires an exact native PID or session identity; cwd/terminal alone is not identity")
    return target


def _endpoints(agent_dir, target):
    stale_publication = False
    if target.get("socket"):
        try:
            _private(target["socket"], "socket")
        except FileNotFoundError:
            # Native extension registration can supersede its initial endpoint
            # without replacing the TUI/session owner. Resolve the native holder,
            # never another conversation or a parked transcript.
            stale_publication = True
        else:
            return [{"socket": target["socket"], "instance": target.get("hostInstance"), "published": True}]
    found = []
    root = agent_dir / "rpc-host-daemon"
    if not root.exists():
        return found
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            endpoint = _read_json(directory / "endpoint.json")
            pointer = _read_json(directory / "host.pid")
            generation = pointer.get("generation_dir")
            if not isinstance(generation, str) or not generation.startswith("generations/") or ".." in Path(generation).parts:
                continue
            owner = _read_json(directory / generation / "host.pid")
            process = _process(owner.get("pid"))
            if not process or not _same_start(owner.get("processStartTime"), process):
                continue
            instance = pointer.get("instance_id")
            if owner.get("instance_id") != instance or owner.get("socket") != endpoint.get("socket"):
                continue
            if target.get("hostInstance") and instance != target["hostInstance"]:
                continue
            if target.get("pid") and (stale_publication or not (target.get("sessionId") or target.get("sessionFile"))) and not _in_process_family(owner["pid"], target["pid"]):
                continue
            found.append({"socket": endpoint["socket"], "instance": instance, "owner": owner["pid"]})
        except (OSError, ValueError, TypeError, NativeError):
            continue
    return found


class Rpc:
    """A single connection, strict LF records, correlated replies, no mutation retry."""
    def __init__(self, endpoint, agent_dir, timeout=12):
        self.endpoint = endpoint.removeprefix("unix://")
        if not os.path.isabs(self.endpoint):
            raise NativeError("OMO socket must be an absolute Unix endpoint")
        _private(self.endpoint, "socket")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.buffer = bytearray()
        self.events = []
        self.route = None
        self.attached = False
        self.timeout = timeout
        self.info = {}
        try:
            self.socket.settimeout(timeout)
            self.socket.connect(self.endpoint)
            # TUI endpoints use their documented, separate raw-secret handshake.
            secret_path = Path(self.endpoint + ".secret")
            if secret_path.exists():
                _private(secret_path)
                fd = os.open(secret_path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    secret = stream.read(129)
                if len(secret) != 32 and not (len(secret) == 64 and HEX64.fullmatch(secret.decode("ascii", errors="replace"))):
                    raise NativeError("Invalid OMO terminal socket credential")
                self.socket.sendall(secret)
                self.info = self.request("get_protocol_info", observe=True)
            else:
                nonce = os.urandom(32).hex()
                self.info = self.request("get_protocol_info", observe=True, auth={"client_nonce": nonce})
                auth = self.info.get("auth")
                if auth:
                    launch = auth.get("launch_id")
                    if not isinstance(launch, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", launch):
                        raise NativeError("Invalid OMO authentication launch identity")
                    for path, private in ((agent_dir, False), (agent_dir / "rpc", False), (agent_dir / "rpc" / "keys", True)):
                        _private(path, "directory", private)
                    record = _read_json(agent_dir / "rpc" / "keys" / launch)
                    server_nonce, proof = auth.get("server_nonce", ""), auth.get("server_proof", "")
                    if record.get("version") != 1 or record.get("launch_id") != launch or not HEX64.fullmatch(record.get("key", "")) or not HEX64.fullmatch(server_nonce) or not HEX64.fullmatch(proof):
                        raise NativeError("Invalid OMO authentication record")
                    if record.get("endpoint") not in (self.endpoint, "unix://" + self.endpoint):
                        raise NativeError("OMO authentication endpoint mismatch")
                    key = bytes.fromhex(record["key"])
                    expected = hmac.new(key, f"senpi-rpc-server\n{launch}\n{nonce}\n{server_nonce}".encode(), hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(expected, proof):
                        raise NativeError("OMO host_identity_unproven")
                    client_proof = hmac.new(key, f"senpi-rpc-client\n{launch}\n{server_nonce}\n{nonce}".encode(), hashlib.sha256).hexdigest()
                    self.request("authenticate", client_proof=client_proof)
                elif "socket_auth" in self.info.get("capabilities", []):
                    raise NativeError("OMO advertised socket authentication without proving host identity")
            if self.info.get("protocolVersion") != 1 or self.info.get("mode") not in ("multi", "tui"):
                raise NativeError("OMO endpoint is not an attachable native CLI host")
        except BaseException:
            self.socket.close()
            raise

    def request(self, command, **fields):
        request_id = uuid.uuid4().hex
        frame = {"id": request_id, "type": command, **fields}
        body = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        if len(body) > MAX_REQUEST_CHARS:
            raise NativeError("OMO request exceeds its native frame limit")
        deadline = time.monotonic() + self.timeout
        try:
            self.socket.sendall((body + "\n").encode())
            while True:
                while b"\n" not in self.buffer:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    self.socket.settimeout(remaining)
                    chunk = self.socket.recv(65536)
                    if not chunk:
                        raise NativeError("OMO disconnected; operation outcome is unknown, do not replay")
                    self.buffer.extend(chunk)
                    if len(self.buffer) > MAX_RESPONSE_BYTES:
                        raise NativeError("OMO response exceeds native frame limit")
                raw, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                reply = json.loads(raw.rstrip(b"\r").decode("utf-8"))
                if not isinstance(reply, dict):
                    raise NativeError("OMO sent a non-object protocol record")
                if reply.get("type") == "response" and reply.get("id") == request_id:
                    routed = reply.get("sessionId")
                    missing_tui_tag = routed is None and self.info.get("mode") == "tui"
                    if reply.get("command") != command or ("sessionId" in fields and routed != fields["sessionId"] and not missing_tui_tag):
                        raise NativeError("OMO response routing identity mismatch")
                    if reply.get("success") is not True:
                        raise RpcError(command, reply)
                    data = reply.get("data")
                    return {} if data is None else data
                if reply.get("type") in ("overflow", "resync_required"):
                    raise NativeError("OMO output was lost; reconcile state before any further operation, never replay")
                if self.info.get("mode") == "tui" and self.route is not None and reply.get("sessionId") is None and reply.get("type") != "response":
                    # A terminal control endpoint hosts exactly one native session;
                    # installed TUI wire events are untagged, unlike shared-host events.
                    reply = {**reply, "sessionId": self.route}
                if reply.get("sessionId") == self.route or (self.route is None and reply.get("type") != "response"):
                    self.events.append(reply)
                    if len(self.events) > 4096:
                        raise NativeError("OMO event burst exceeded the observer bound")
        except (OSError, TimeoutError) as exc:
            raise NativeError("OMO transport failed; operation outcome is unknown, do not replay") from exc
        except (UnicodeError, ValueError) as exc:
            raise NativeError("OMO returned invalid JSON framing") from exc

    def scoped(self, command, **fields):
        return self.request(command, sessionId=self.route, **fields)

    def attach(self, row):
        caps = self.info.get("capabilities", [])
        # Setting roles does not admit input or release engine holds.
        self.request("set_client_info", width=120, capabilities=["interactive_user", "question"])
        if "attach_session" in caps or "attach_session" in self.info.get("commands", []):
            attached = self.request("attach_session", sessionId=row["sessionId"], durableSessionId=row["durableSessionId"], expectedHostInstance=self.info["instanceId"])
            if attached.get("sessionId") != row["sessionId"] or attached.get("attached") is not True:
                raise NativeError("OMO native attachment did not confirm the selected routing identity")
        else:
            raise NativeError("OMO lacks identity-fenced live attachment; use the native terminal")
        self.attached = True

    def close(self):
        try:
            if self.attached and "release_attachment" in self.info.get("capabilities", []):
                self.scoped("release_attachment")
            elif self.attached and "detach_connection" in self.info.get("capabilities", []):
                self.request("detach_connection")
        except NativeError:
            pass  # Socket close has disconnect semantics; never send close_session.
        finally:
            self.socket.close()


def _matches(row, target):
    if target.get("sessionId") and target["sessionId"] != row.get("durableSessionId"):
        return False
    if target.get("sessionFile") and os.path.realpath(target["sessionFile"]) != os.path.realpath(row.get("sessionPath") or "/"):
        return False
    return row.get("status") not in ("opening", "closing", "closed", "unloaded")


@contextlib.contextmanager
def _bound(payload):
    target = _metadata(payload)
    agent_dir = _agent_dir(target)
    matches = []
    failures = []
    try:
        for endpoint in _endpoints(agent_dir, target):
            rpc = None
            try:
                rpc = Rpc(endpoint["socket"], agent_dir)
                if endpoint.get("instance") and rpc.info.get("instanceId") != endpoint["instance"]:
                    raise NativeError("OMO host generation changed; rediscover native identity")
                rows = rpc.request("list_sessions", include_workers=True, observe=True).get("sessions", [])
                if rpc.info.get("mode") == "tui":
                    # TUI controls publish the durable ID directly as their sole
                    # routing ID, unlike a shared host's ephemeral rpc-N handle.
                    rows = [{**row, "durableSessionId": row.get("durableSessionId") or row.get("sessionId")}
                            for row in rows if isinstance(row, dict)]
                selected = [row for row in rows if isinstance(row, dict) and row.get("durableSessionId") and _matches(row, target)]
                if target.get("pid") and not (target.get("sessionId") or target.get("sessionFile")) and rpc.info.get("mode") != "tui":
                    raise NativeError("A shared OMO host PID is not a session identity; publish its durable session ID/path")
                for row in selected:
                    matches.append((rpc, row, target))
                if not selected:
                    rpc.close()
            except (OSError, NativeError) as exc:
                failures.append(str(exc))
                if rpc:
                    rpc.close()
        if len(matches) != 1:
            if matches:
                raise NativeError("Ambiguous OMO native identity: multiple live sessions match")
            raise NativeError("No exact live OMO session found" + (": " + "; ".join(failures) if failures else "; parked sessions are never opened automatically"))
        rpc, row, target = matches[0]
        rpc.route = row["sessionId"]
        yield rpc, row, target
    finally:
        for rpc in {match[0] for match in matches}:
            rpc.close()


def _state(rpc, row):
    state = rpc.scoped("get_state", observe=True)
    if state.get("sessionId") != row["durableSessionId"] or (row.get("sessionPath") and os.path.realpath(state.get("sessionFile") or "/") != os.path.realpath(row["sessionPath"])):
        raise NativeError("OMO native session changed; rediscover before acting")
    if any(event.get("sessionId") == rpc.route and event.get("type") in ("session_replaced", "session_closed", "session_parked") for event in rpc.events):
        raise NativeError("OMO native identity changed during attachment; rediscover before acting")
    return state


def _supports(rpc, command):
    commands = rpc.info.get("commands")
    return command in commands if isinstance(commands, list) else rpc.info.get("mode") == "multi"


def _capabilities(rpc):
    fenced = "attach_session" in rpc.info.get("capabilities", []) or "attach_session" in rpc.info.get("commands", [])
    return {"history": _supports(rpc, "get_entries"), "send": fenced and _supports(rpc, "prompt"),
            "interrupt": fenced and _supports(rpc, "interrupt"),
            "configure": fenced and rpc.info.get("mode") == "tui" and _supports(rpc, "set_model"),
            "answer": fenced and _supports(rpc, "extension_ui_response"),
            "fork": fenced and _supports(rpc, "clone"), "images": rpc.info.get("mode") != "tui"}


def _requests(state):
    return [{**request, "method": request.get("method", "question" if field == "pendingQuestions" else "")}
            for field in ("pendingQuestions", "pendingDialogs")
            for request in state.get(field) or [] if isinstance(request, dict)]


def _pending(state):
    result = []
    for request in _requests(state):
        request_id = request.get("id") or request.get("requestId")
        if not request_id:
            continue
        method = request.get("method", "question")
        choices = []
        if method == "select":
            for option in request.get("options") or []:
                if isinstance(option, str):
                    choices.append({"label": option, "value": option})
        elif method == "confirm":
            choices = [{"label": "Confirm", "value": True}, {"label": "Cancel", "value": False}]
        projected = {"id": request_id, "kind": "question" if method == "question" else "permission",
                     "title": request.get("title") or ("Question" if method == "question" else "Native OMO request"),
                     "choices": choices, "answerable": not request.get("secret", False)}
        if request.get("message"):
            projected["detail"] = request["message"]
        if request.get("askedAtMs") is not None:
            projected["at"] = request["askedAtMs"]
        if method == "question":
            projected["questions"] = request.get("questions") or []
        projected["method"] = method
        result.append(projected)
    return result


def _revision(state, tail):
    lifecycle = state.get("lifecycle")
    if isinstance(lifecycle, dict):
        lifecycle = {key: value for key, value in lifecycle.items() if key != "observedAt"}
    entries = tail.get("entries") or []
    history = [tail.get("leafId"), tail.get("last") or (entries[-1].get("id") if entries else None)]
    data = [state.get("sessionId"), state.get("messageCount"), state.get("state_version"), lifecycle, state.get("pendingMessageCount"), state.get("pendingModelSwitch"), state.get("model", {}).get("id") if isinstance(state.get("model"), dict) else None, state.get("thinkingLevel"), state.get("paused"), _pending(state), history]
    if state.get("isStreaming") or state.get("isCompacting"):
        data.append(time.time_ns())  # A partial message may change without a durable entry.
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _project_state(rpc, row, target, state):
    tail = rpc.scoped("get_entries", tail=1, maxBytes=1024 * 1024, observe=True) if "entries_tail" in rpc.info.get("capabilities", []) else {}
    lifecycle = state.get("lifecycle") or {}
    trustworthy = lifecycle.get("restoring") is False and lifecycle.get("persisted") is True
    settled = lifecycle.get("state") in ("done", "failed", "cancelled") if trustworthy else None
    waiting = lifecycle.get("waitingOn") or {}
    background = waiting.get("background")
    pending_async = (bool(waiting.get("children")) or any(item.get("activeCount", 0) > 0 for item in background or [])) if isinstance(background, list) else None
    model = state.get("model")
    result = {"sessionId": state["sessionId"], "sessionFile": state.get("sessionFile"), "pid": target.get("pid"),
              "revision": _revision(state, tail), "model": {key: model[key] for key in ("id", "provider", "name", "contextWindow") if key in model} if isinstance(model, dict) else None,
              "thinking": state.get("thinkingLevel"), "working": bool(state.get("isStreaming") or state.get("isCompacting") or lifecycle.get("state") in ("running", "waiting_children", "waiting_background")),
              "settled": settled, "pendingAsyncWork": pending_async, "capabilities": _capabilities(rpc),
              "pendingRequests": _pending(state), "jobs": background or [], "subagents": waiting.get("childReports") or [],
              "paused": bool(state.get("paused")), "holds": state.get("holds") or [], "lifecycle": lifecycle}
    return result


def _message(entry):
    kind = entry.get("type")
    if kind == "message":
        message = entry.get("message") or {}
    elif kind in ("compaction", "branch_summary"):
        message = {"role": "assistant", "content": [{"type": "text", "text": entry.get("summary", "")}], "nativeType": kind}
    elif kind == "custom_message" and entry.get("display") is not False:
        message = {"role": "user", "content": entry.get("content", ""), "nativeType": entry.get("customType")}
    elif kind == "custom" and entry.get("customType") == "session_control_delivery":
        details = entry.get("data") or entry.get("details") or {}
        message = {"role": "user", "content": details.get("text", ""), "nativeType": "session_control_delivery"}
    else:
        return None
    role = message.get("role")
    if role not in ("user", "assistant", "toolResult"):
        if role == "custom" and message.get("display") is not False:
            role = "user"
        elif role == "bashExecution":
            role = "toolResult"
            message = {**message, "content": message.get("output", ""), "toolCallId": entry["id"], "toolName": "bash", "isError": bool(message.get("exitCode"))}
        else:
            return None
    result = {key: message[key] for key in ("content", "timestamp", "model", "provider", "usage", "toolCallId", "toolName", "isError", "nativeType") if key in message}
    result.update(id=entry["id"], role=role)
    if "usage" in entry and "usage" not in result:
        result["usage"] = entry["usage"]
    result.setdefault("timestamp", entry.get("timestamp"))
    result.setdefault("content", "")
    return result


def _branch(entries, leaf):
    path, seen = [], set()
    while leaf is not None:
        if leaf in seen or leaf not in entries:
            raise NativeError("OMO history has an incomplete or cyclic active branch")
        seen.add(leaf)
        entry = entries[leaf]
        path.append(entry)
        leaf = entry.get("parentId")
    path.reverse()
    # Render the active transcript, not model context: compaction summaries stay at
    # their actual position and earlier messages remain pageable, as in native UI.
    return [message for entry in path if (message := _message(entry)) is not None]


def _cursor(data):
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(value):
    try:
        data = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError, UnicodeError):
        return {}


def _history(rpc, row, target, payload, state):
    limit = payload.get("limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise NativeError("OMO history limit must be 1..1000")
    root = state_dir() / "omo" / "history"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _private(root, "directory")
    identity = hashlib.sha256((state["sessionId"] + "\n" + str(state.get("sessionFile"))).encode()).hexdigest()
    database = root / (identity + ".sqlite3")
    fd = os.open(database, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    _private(database)
    with contextlib.closing(sqlite3.connect(database, timeout=12)) as cache, cache:
        cache.execute("PRAGMA journal_mode=DELETE")
        cache.execute("CREATE TABLE IF NOT EXISTS entries (id TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cache.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        cache.execute("BEGIN IMMEDIATE")
        meta = dict(cache.execute("SELECT key,value FROM meta"))
        since = meta.get("since")
        changed = False
        while True:
            fields = {"limit": 500, "maxBytes": 8 * 1024 * 1024, "observe": True}
            if since:
                fields["since"] = since
            try:
                page = rpc.scoped("get_entries", **fields)
            except RpcError as exc:
                if since and exc.code == "not_found":
                    cache.execute("DELETE FROM entries")
                    since = None
                    changed = True
                    continue
                raise
            entries = page.get("entries", [])
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    raise NativeError("OMO history entry has no durable identity")
                cache.execute("INSERT OR REPLACE INTO entries VALUES (?,?)", (entry["id"], json.dumps(entry, separators=(",", ":"))))
            changed |= bool(entries)
            previous = since
            if entries:
                since = entries[-1]["id"]
            leaf = page.get("leafId")
            if not page.get("hasMore"):
                break
            if since == previous:
                raise NativeError("OMO history cursor did not advance")
        if changed or meta.get("leaf") != json.dumps(leaf) or "messages" not in meta:
            indexed = {key: json.loads(value) for key, value in cache.execute("SELECT id,value FROM entries")}
            messages = _branch(indexed, leaf)
            old = json.loads(meta.get("messages", "[]"))
            generation = int(meta.get("generation", "0")) + (0 if messages[:len(old)] == old else 1)
        else:
            messages = json.loads(meta["messages"])
            generation = int(meta.get("generation", "0"))
        for key, value in {"since": since or "", "leaf": json.dumps(leaf), "messages": json.dumps(messages, separators=(",", ":")), "generation": str(generation)}.items():
            cache.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        # Identity changes roll back the cache too; a replacement's entries must
        # never contaminate the previously bound durable session's projection.
        latest = _state(rpc, row)
    partial = None
    for event in rpc.events:
        if event.get("sessionId") != rpc.route:
            continue
        if event.get("type") in ("message_start", "message_update") and isinstance(event.get("message"), dict):
            partial = event["message"]
        elif event.get("type") in ("message_end", "agent_end"):
            partial = None
    has_partial = bool(partial and latest.get("isStreaming"))
    if has_partial:
        partial_entry = {"id": f"live:{state['sessionId']}:{partial.get('timestamp', latest.get('turn_epoch', 'current'))}", "type": "message", "message": partial}
        projected = _message(partial_entry)
        if projected:
            messages.append(projected)
    cursor = _decode_cursor(payload.get("cursor", ""))
    reset = not cursor or cursor.get("session") != state["sessionId"] or cursor.get("generation") != generation or cursor.get("partial") or has_partial
    start, end = max(0, len(messages) - limit), len(messages)
    ids = [message["id"] for message in messages]
    if payload.get("before"):
        if payload["before"] not in ids:
            raise NativeError("OMO history before cursor is not on the active branch")
        end = ids.index(payload["before"])
        start = max(0, end - limit)
        reset = False
    elif not reset:
        last = cursor.get("last")
        if last in ids:
            start = ids.index(last) + 1
            end = min(len(messages), start + limit)
        elif last is None and not messages:
            start = end = 0
        else:
            reset = True
    selected = messages[start:end]
    last = selected[-1]["id"] if selected else cursor.get("last") if not reset else None
    return {"messages": selected, "cursor": _cursor({"session": state["sessionId"], "generation": generation, "last": last, "partial": has_partial}),
            "hasEarlier": start > 0, "reset": bool(reset), "revision": _revision(latest, {"leafId": leaf, "last": since})}


def _models(rpc, state):
    rows = rpc.scoped("get_available_models").get("models", [])
    active = state.get("model") or {}
    current_levels = rpc.scoped("get_available_thinking_levels").get("levels", [])
    models = []
    for model in rows:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not isinstance(model.get("provider"), str):
            continue
        levels = model.get("supportedThinkingLevels") or model.get("thinkingLevels") or (current_levels if (model["id"], model["provider"]) == (active.get("id"), active.get("provider")) else [])
        if isinstance(levels, dict):
            levels = list(levels)
        levels = [level for level in levels if isinstance(level, str)]
        models.append({"id": model["provider"] + "/" + model["id"], "label": model.get("name") or model["id"], "efforts": levels,
                       "defaultEffort": model.get("defaultThinkingLevel"), "fast": model.get("serviceTier") == "priority"})
    return {"models": models, "model": f"{active['provider']}/{active['id']}" if active.get("provider") and active.get("id") else None,
            "effort": state.get("thinkingLevel"), "perms": [], "mode": None}


def _answer(rpc, state, payload):
    request_id = payload.get("requestId")
    requests = _requests(state)
    request = next((item for item in requests if (item.get("id") or item.get("requestId")) == request_id), None)
    if not request:
        raise NativeError("OMO approval/question is stale or already answered")
    if request.get("secret"):
        raise NativeError("OMO secret prompts must be answered in the native terminal")
    answer = payload.get("answer")
    fields = {"uiRequestId": request_id}
    if isinstance(answer, dict) and answer.get("cancelled") is True:
        fields["cancelled"] = True
    elif request.get("method") == "confirm" and isinstance(answer, bool):
        fields["confirmed"] = answer
    elif request.get("method") == "question" and isinstance(answer, dict):
        fields.update({key: answer[key] for key in ("answers", "comment") if key in answer})
    elif request.get("method") in ("select", "input", "editor") and isinstance(answer, str):
        fields["value"] = answer
    else:
        raise NativeError("OMO answer does not match the native dialog method")
    if "ui_answer_ledger" in rpc.info.get("capabilities", []):
        fields["answerId"] = uuid.uuid4().hex
    return {"answered": True, "requestId": request_id, **rpc.scoped("extension_ui_response", **fields)}


def _images(payload, mode):
    files = payload.get("files") or []
    images = payload.get("images") or []
    if not isinstance(files, list) or len(files) > 32 or not all(isinstance(path, str) and os.path.isabs(path) for path in files) or not isinstance(images, list):
        raise NativeError("OMO attachments require at most 32 absolute file paths or native image records")
    result = []
    encoded_bytes = 0
    for image in images:
        if not isinstance(image, dict) or not isinstance(image.get("data"), str) or not isinstance(image.get("mimeType"), str):
            raise NativeError("Malformed OMO native image attachment")
        if image["mimeType"] not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
            raise NativeError("OMO supports PNG, JPEG, WebP and GIF image attachments")
        encoded_bytes += len(image["data"])
        if encoded_bytes > (8 * 1024 * 1024 * 4 // 3 + 4):
            raise NativeError("OMO attachments exceed the combined 8 MiB image limit")
        result.append({"type": "image", "data": image["data"], "mimeType": image["mimeType"]})
    types = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
    for filename in files:
        mime = types.get(Path(filename).suffix.lower())
        if not mime:
            continue
        if not os.path.isabs(filename):
            raise NativeError("OMO staged image paths must be absolute")
        if mode == "tui":
            raise NativeError("OMO terminal control does not accept images; attach them in the native terminal")
        try:
            descriptor = os.open(filename, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 8 * 1024 * 1024:
                    raise NativeError("OMO image is not a regular file within the 8 MiB image limit")
                if encoded_bytes + ((info.st_size + 2) // 3) * 4 > (8 * 1024 * 1024 * 4 // 3 + 4):
                    raise NativeError("OMO attachments exceed the combined 8 MiB image limit")
                content = stream.read(8 * 1024 * 1024 + 1)
                if len(content) > 8 * 1024 * 1024:
                    raise NativeError("OMO image exceeds the 8 MiB image limit")
        except OSError as exc:
            raise NativeError("OMO could not read a staged image") from exc
        valid = (mime == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n")
                 or mime == "image/jpeg" and content.startswith(b"\xff\xd8\xff")
                 or mime == "image/webp" and content.startswith(b"RIFF") and content[8:12] == b"WEBP"
                 or mime == "image/gif" and content[:6] in (b"GIF87a", b"GIF89a"))
        if not valid:
            raise NativeError("OMO image attachment signature does not match its MIME type")
        data = base64.b64encode(content).decode("ascii")
        encoded_bytes += len(data)
        result.append({"type": "image", "data": data, "mimeType": mime})
    if encoded_bytes > (8 * 1024 * 1024 * 4 // 3 + 4):
        raise NativeError("OMO attachments exceed the combined 8 MiB image limit")
    if result and mode == "tui":
        raise NativeError("OMO terminal control does not accept images; attach them in the native terminal")
    return result


def handle(operation, payload):
    if not isinstance(payload, dict):
        raise NativeError("OMO payload must be an object")
    if operation == "models" and not any(payload.get(key) for key in ("pid", "sessionId", "sessionFile")):
        raise NativeError("OMO structured model inventory requires an exact live native session; before first launch use omo --list-models in the terminal")
    with _bound(payload) as (rpc, row, target):
        state = _state(rpc, row)
        if operation in ("state", "discover"):
            return _project_state(rpc, row, target, state)
        if operation == "models":
            return _models(rpc, state)
        if operation == "close":
            return {"detached": True, "sessionId": state["sessionId"]}
        if operation not in ("history", "send", "interrupt", "configure", "answer", "fork"):
            raise NativeError(f"Unknown OMO operation: {operation}")
        if operation == "configure" and payload.get("model") and rpc.info.get("mode") != "tui":
            raise NativeError("OMO shared-host model selection persists global defaults; change it in the native terminal")
        if operation == "configure" and payload.get("perm") is not None:
            raise NativeError("OMO has no session-only permission-setting control; use the native terminal")
        if operation == "fork" and not _capabilities(rpc)["fork"]:
            raise NativeError("OMO terminal has no native fork control; use /fork or /clone in that terminal")
        images = _images(payload, rpc.info.get("mode")) if operation == "send" else []
        rpc.attach(row)
        state = _state(rpc, row)
        if operation == "history":
            if not _supports(rpc, "get_entries"):
                raise NativeError("OMO endpoint has no native history control")
            return _history(rpc, row, target, payload, state)
        if operation == "send":
            text = payload.get("text")
            if not isinstance(text, str):
                raise NativeError("OMO send requires text")
            delivery = payload.get("delivery", "prompt")
            command = {"prompt": "prompt", "steer": "steer", "followUp": "follow_up"}.get(delivery)
            if not command:
                raise NativeError("Invalid OMO native delivery mode")
            if not _supports(rpc, command):
                raise NativeError("OMO endpoint has no native send control")
            fields = {"message": text}
            if images:
                fields["images"] = images
            native_request_ids = "durable_client_message_id" in rpc.info.get("capabilities", []) or (
                rpc.info.get("mode") == "tui" and command in ("steer", "follow_up"))
            if payload.get("requestId"):
                request_id = payload["requestId"]
                if not isinstance(request_id, str) or not 1 <= len(request_id) <= 256:
                    raise NativeError("OMO requestId must be a string of 1..256 characters")
                if native_request_ids:
                    fields["clientMessageId"] = request_id
                elif rpc.info.get("mode") != "tui":
                    raise NativeError("OMO host cannot acknowledge durable send identities")
            result = rpc.scoped(command, **fields)
            return {"sessionId": state["sessionId"], "admitted": True, "requestId": payload.get("requestId"),
                    "nativeRequestIdSupported": native_request_ids, **result}
        if operation == "interrupt":
            if not _supports(rpc, "interrupt"):
                raise NativeError("OMO endpoint has no native interrupt control")
            fields = {"intent": "stop"}
            if payload.get("turnId") is not None:
                fields["turnId"] = payload["turnId"]
            return rpc.scoped("interrupt", **fields)
        if operation == "answer":
            if not _supports(rpc, "extension_ui_response"):
                raise NativeError("OMO endpoint has no native approval/question control")
            return _answer(rpc, state, payload)
        if operation == "configure":
            if payload.get("model") and not _supports(rpc, "set_model"):
                raise NativeError("OMO endpoint has no native model-setting control")
            if payload.get("effort") is not None and not _supports(rpc, "set_thinking_level"):
                raise NativeError("OMO endpoint has no native thinking-level control")
            if payload.get("model"):
                options = _models(rpc, state)["models"]
                selected = [model for model in options if model["id"] == payload["model"]]
                if len(selected) != 1:
                    raise NativeError("OMO model must be an exact provider-qualified native model ID")
                provider, model_id = selected[0]["id"].split("/", 1)
                rpc.scoped("set_model", provider=provider, modelId=model_id)
            if payload.get("effort") is not None:
                rpc.scoped("set_thinking_level", level=payload["effort"], scope="turn")
            applied = _state(rpc, row)
            active = applied.get("model") or {}
            actual_model = f"{active.get('provider')}/{active.get('id')}"
            if payload.get("model") and actual_model != payload["model"]:
                raise NativeError("OMO model switch is pending or was not applied")
            if payload.get("effort") is not None and applied.get("thinkingLevel") != payload["effort"]:
                raise NativeError("OMO thinking level was not applied")
            return {"model": actual_model, "effort": applied.get("thinkingLevel"), "confirmed": True}
        # Fork/clone runs in the actual native owner, never a second session writer.
        fields = {"entryId": payload["entryId"]} if payload.get("entryId") else {}
        outcome = rpc.scoped("fork" if fields else "clone", **fields)
        if outcome.get("cancelled"):
            return {"cancelled": True, "sessionId": state["sessionId"]}
        forked = rpc.scoped("get_state", observe=True)
        if not forked.get("sessionId") or forked["sessionId"] == state["sessionId"]:
            raise NativeError("OMO fork did not publish a new native session identity")
        return {"sessionId": forked["sessionId"], "sessionFile": forked.get("sessionFile"), "pid": target.get("pid"), "cancelled": False}
