"""Attach to the authenticated bridge inside the owning OMP TUI, never a second RPC writer."""
import base64
import json
import os
from pathlib import Path
import stat
import subprocess

from .common import NativeError, socket_request, state_dir


def _private(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise NativeError("OMP bridge paths must be owner-only and owned by this user")
    return info


def _process_start(pid):
    """Linux PID reuse fence; other platforms still authenticate the live instance token."""
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
        return value[value.rfind(")") + 2:].split()[19]
    except (OSError, IndexError):
        return None


def _process_identity(pid):
    """PID, parent and Linux start ticks together, restricted to this user's live processes."""
    try:
        folder = Path(f"/proc/{pid}")
        if folder.stat().st_uid != os.getuid():
            return None
        value = (folder / "stat").read_text()
        fields = value[value.rfind(")") + 2:].split()
        if fields[0] == "Z":
            return None
        return (pid, int(fields[1]), fields[19])
    except (OSError, ValueError, IndexError):
        return None


def _descendant_chain(foreground, owner_pid, owner_start):
    """Prove a stable same-user ancestor chain, never a cwd/newest-file association."""
    if foreground is None or owner_start is None:
        return None
    current = _process_identity(owner_pid)
    if current is None or current[2] != owner_start:
        return None
    chain, seen = [], set()
    while current is not None and len(chain) < 256 and current[0] not in seen:
        chain.append(current)
        seen.add(current[0])
        if current[0] == foreground[0]:
            if current != foreground or any(_process_identity(item[0]) != item for item in chain):
                return None
            return chain
        if current[1] <= 1:
            return None
        current = _process_identity(current[1])
    return None


def _check_process_chain(target):
    chain = target.get("_processChain")
    if chain and any(_process_identity(item[0]) != item for item in chain):
        raise NativeError("OMP foreground/owner process ancestry changed; rediscover before sending")


def _metadata(path):
    _private(path.parent, directory=True)
    before = _private(path)
    if before.st_size > 65536:
        raise NativeError("OMP bridge metadata is oversized")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        current = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (current.st_dev, current.st_ino):
            raise NativeError("OMP bridge metadata changed while opening")
        value = json.load(stream)
    if not isinstance(value, dict) or value.get("version") != 1:
        raise NativeError("Invalid OMP bridge metadata")
    pid = value.get("pid")
    if type(pid) is not int or pid <= 0 or path.name != f"{pid}.json":
        raise NativeError("Invalid OMP bridge process identity")
    if not isinstance(value.get("sessionId"), str) or not value["sessionId"]:
        raise NativeError("OMP bridge metadata lacks session identity")
    endpoint = value.get("endpoint")
    if not isinstance(endpoint, str) or not os.path.isabs(endpoint) or Path(endpoint).parent != path.parent:
        raise NativeError("OMP bridge endpoint is outside its private directory")
    if not isinstance(value.get("token"), str) or len(value["token"]) < 32:
        raise NativeError("OMP bridge metadata lacks authentication")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError as exc:
        raise NativeError("OMP process belongs to another user") from exc
    start = _process_start(pid)
    if value.get("processStart") is not None and start is not None and value["processStart"] != start:
        return None
    return value


def _target(payload):
    folder = state_dir() / "omp"
    if not folder.exists():
        raise NativeError("No OMP native bridge; launch OMP with --extension <bin>/shepherdr_native/omp-extension.ts")
    _private(folder, directory=True)
    for key in ("sessionId", "sessionFile", "terminal", "cwd"):
        if key in payload and (not isinstance(payload[key], str) or not payload[key]):
            raise NativeError(f"OMP {key} must be a non-empty string")
    if "terminal" in payload and not payload["terminal"].startswith("/dev/"):
        payload = {**payload, "terminal": payload["terminal"].rsplit("/", 1)[-1]}
    pid = payload.get("pid")
    if pid is not None and (type(pid) is not int or pid <= 0):
        raise NativeError("OMP pid must be a positive integer")
    # A working directory is a filter, never a native session identity.
    if not any(payload.get(key) for key in ("pid", "sessionId", "sessionFile", "terminal")):
        raise NativeError("OMP attachment requires pid, sessionId, sessionFile, or terminal; cwd alone is unsafe")
    direct = folder / f"{pid}.json" if pid else None
    descendant = bool(pid and not os.path.lexists(direct))
    foreground = _process_identity(pid) if descendant else None
    if descendant and not any(payload.get(key) for key in ("sessionId", "sessionFile", "terminal")):
        raise NativeError("OMP descendant attachment requires an exact terminal or session fence")
    if descendant and foreground is None:
        raise NativeError("Cannot verify the OMP foreground process ancestry on this platform")
    paths = [direct] if direct and not descendant else sorted(folder.glob("*.json"))
    matches = []
    for path in paths:
        try:
            item = _metadata(path)
        except (OSError, ValueError, NativeError) as exc:
            if pid and not descendant:
                raise NativeError(f"Cannot attach OMP process {pid}: {exc}") from exc
            continue
        if item is None or not all(item.get(key) == payload[key] for key in ("sessionId", "sessionFile", "terminal", "cwd") if key in payload):
            continue
        if descendant:
            chain = _descendant_chain(foreground, item["pid"], item.get("processStart"))
            if chain is None:
                continue
            item = {**item, "_foregroundPid": pid, "_processChain": chain}
        elif pid and item["pid"] != pid:
            continue
        matches.append(item)
    if not matches:
        raise NativeError("No live OMP bridge matches the exact requested session identity")
    if len(matches) != 1:
        raise NativeError("OMP session identity is ambiguous; supply the foreground process pid")
    return matches[0]


def _attachments(request):
    """Read only explicitly submitted image files; ordinary attachment paths stay in text."""
    files = request.pop("files", [])
    if not isinstance(files, list) or len(files) > 32 or any(not isinstance(file, str) or not os.path.isabs(file) for file in files):
        raise NativeError("OMP attachment files must be absolute paths (at most 32)")
    images = request.get("images", [])
    if not isinstance(images, list):
        raise NativeError("OMP images must be an array")
    images = list(images)
    total = sum(len(image.get("data", "")) for image in images if isinstance(image, dict) and isinstance(image.get("data"), str))
    for file in files:
        suffix = Path(file).suffix.lower()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}.get(suffix)
        if mime is None:
            continue
        descriptor = os.open(file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise NativeError("OMP image attachment is not a regular file")
            raw = stream.read(5 * 1024 * 1024 + 1)
        if len(raw) > 5 * 1024 * 1024:
            raise NativeError("OMP image attachment exceeds 5 MiB")
        valid = (mime == "image/png" and raw.startswith(b"\x89PNG\r\n\x1a\n")
                 or mime == "image/jpeg" and raw.startswith(b"\xff\xd8\xff")
                 or mime == "image/webp" and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"
                 or mime == "image/gif" and raw[:6] in (b"GIF87a", b"GIF89a"))
        if not valid:
            raise NativeError("OMP image attachment signature does not match its MIME type")
        data = base64.b64encode(raw).decode("ascii")
        total += len(data)
        if total > 7 * 1024 * 1024:
            raise NativeError("OMP image attachments exceed the private IPC byte limit")
        images.append({"data": data, "mimeType": mime})
    if images:
        request["images"] = images


def _catalog_models(value):
    """Project only public model fields, never provider transport/auth configuration."""
    if not isinstance(value, dict) or not isinstance(value.get("models"), list):
        raise NativeError("OMP native model catalog returned an invalid models array")
    models = []
    for model in value["models"]:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not isinstance(model.get("provider"), str):
            raise NativeError("OMP native model catalog returned an invalid model identity")
        thinking = model.get("thinking", [])
        if not isinstance(thinking, list) or any(not isinstance(effort, str) for effort in thinking):
            raise NativeError("OMP native model catalog returned invalid thinking levels")
        models.append({"id": f'{model["provider"]}/{model["id"]}', "label": model.get("name") or model["id"],
                       "efforts": ["off", *thinking], "defaultEffort": None})
    return {"models": models, "model": None, "effort": None, "perms": [], "mode": None, "source": "native-catalog"}


def _standalone_models(payload):
    """The native models subcommand discovers configuration without owning a session."""
    command = [os.environ.get("SHEPHERDR_OMP") or "omp"]
    if "profile" in payload:
        if not isinstance(payload["profile"], str) or not payload["profile"]:
            raise NativeError("OMP profile must be a non-empty string")
        command.extend(["--profile", payload["profile"]])
    if "cwd" in payload and (not isinstance(payload["cwd"], str) or not os.path.isabs(payload["cwd"])):
        raise NativeError("OMP catalog cwd must be an absolute path")
    command.extend(["models", "--json"])
    try:
        result = subprocess.run(command, cwd=payload.get("cwd"), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeError(f"OMP native model discovery failed: {exc}") from exc
    if result.returncode:
        # Native errors may contain configuration values; do not relay provider diagnostics.
        raise NativeError(f"OMP native model discovery exited with status {result.returncode}")
    try:
        return _catalog_models(json.loads(result.stdout))
    except ValueError as exc:
        raise NativeError("OMP native model discovery returned malformed JSON") from exc


def handle(operation, payload):
    if operation not in {"state", "discover", "history", "models", "send", "interrupt", "configure", "answer", "fork", "close"}:
        raise NativeError(f"Unsupported OMP operation: {operation}")
    if not isinstance(payload, dict):
        raise NativeError("OMP payload must be an object")
    if operation == "models" and not any(key in payload for key in ("pid", "sessionId", "sessionFile", "terminal")):
        return _standalone_models(payload)
    target = _target(payload)
    request = dict(payload)
    if operation == "send":
        try:
            _attachments(request)
        except OSError as exc:
            raise NativeError(f"Cannot read OMP attachment: {exc}") from exc
    # Pin every request, including pid-only discovery, against session switches after metadata was read.
    request.update(pid=target["pid"], sessionId=target["sessionId"], instanceId=target.get("instanceId"))
    if target.get("sessionFile"):
        request["sessionFile"] = target["sessionFile"]
    _check_process_chain(target)
    try:
        result = socket_request(target["endpoint"], target["token"], operation, request)
        _check_process_chain(target)
        if target.get("_foregroundPid"):
            result = {**result, "pid": target["_foregroundPid"], "ownerPid": target["pid"]}
        return result
    except (OSError, ValueError) as exc:
        raise NativeError(f"OMP private IPC failed (not retried): {exc}") from exc
