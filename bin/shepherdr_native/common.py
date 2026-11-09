"""Shared private IPC primitives for native runtime adapters."""
import json
import os
from pathlib import Path
import socket
import stat
import time
import uuid

MAX_FRAME_BYTES = 8 * 1024 * 1024


class NativeError(Exception):
    """A native operation could not be performed; callers must not infer success."""


def state_dir():
    base = os.environ.get("SHEPHERDR_HOME")
    if base:
        return Path(base).expanduser() / "state" / "native"
    return Path(os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")) / "shepherdr" / "native"


def socket_request(endpoint, token, operation, payload, timeout=10):
    """One authenticated request to the owning process, with no automatic replay."""
    if not isinstance(endpoint, str) or not os.path.isabs(endpoint):
        raise NativeError("The native socket endpoint must be an absolute path")
    if not isinstance(token, str) or not token:
        raise NativeError("The native socket has no authentication token")
    if not hasattr(socket, "AF_UNIX"):
        raise NativeError("This platform does not provide the native Unix socket transport")
    info = os.lstat(endpoint)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise NativeError("The native socket must be an owner-only socket owned by this user")
    request_id = uuid.uuid4().hex
    body = (json.dumps({"version": 1, "token": token, "id": request_id, "operation": operation, "payload": payload}, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(body) > MAX_FRAME_BYTES:
        raise NativeError("The native request exceeds the transport limit")
    deadline = time.monotonic() + timeout
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(endpoint)
        client.sendall(body)
        chunks, length = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeError("The native request timed out; its outcome is unknown")
            client.settimeout(remaining)
            chunk = client.recv(min(65536, MAX_FRAME_BYTES + 1 - length))
            if not chunk:
                raise NativeError("The native connection ended before replying; its outcome is unknown")
            chunks.append(chunk)
            length += len(chunk)
            if length > MAX_FRAME_BYTES:
                raise NativeError("The native response exceeds the transport limit")
            if b"\n" in chunk:
                raw = b"".join(chunks)
                frame, extra = raw.split(b"\n", 1)
                if extra.strip():
                    raise NativeError("The native adapter returned unexpected trailing data")
                try:
                    reply = json.loads(frame.decode("utf-8"))
                except (UnicodeError, ValueError) as exc:
                    raise NativeError("The native adapter returned malformed JSON") from exc
                if not isinstance(reply, dict) or reply.get("id") != request_id:
                    raise NativeError("The native adapter returned a mismatched request identity")
                if reply.get("ok") is not True:
                    raise NativeError(str(reply.get("error") or "The native operation was rejected"))
                data = reply.get("data")
                if not isinstance(data, dict):
                    raise NativeError("The native adapter returned an invalid result")
                return data
