"""Prepare a real native terminal launch without replacing its engine or policy."""
import base64
import importlib
import os
from pathlib import Path
import mimetypes
import shutil
import uuid

from .common import NativeError, state_dir

KINDS = ("omp", "omo", "omh", "hermes")
SESSION_ENV = ("PI_SESSION_ID", "PI_SESSION_FILE", "PI_SESSION_CWD", "PI_GOAL_STORE_FILE", "PI_TOOL_CALL_ID", "OMPCODE",
               "HERMES_TUI_GATEWAY_URL", "HERMES_TUI_SIDECAR_URL", "HERMES_TUI_RESUME", "HERMES_TUI_ACTIVE_SESSION_FILE", "HERMES_SESSION_ID")


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise NativeError(f"Native launch directory must be private: {path}")
    return path


def _native_args(kind, payload):
    """Keep custom native CLI options, but normalize every session-selection control."""
    payload = dict(payload)
    args = payload.get("nativeArgs") or []
    if not isinstance(args, list) or not all(isinstance(word, str) for word in args):
        raise NativeError("Custom native arguments must be a string array")
    flags = {"--model": "model", "-m": "model", "--thinking": "effort", "--reasoning": "effort",
             "--profile": "profile", "--hermes-home": "hermesHome"}
    if kind in ("omh", "hermes"):
        flags.update({"-p": "profile", "--provider": "provider", "--resume": "sessionId", "-r": "sessionId", "--query": "message", "-q": "message"})
    elif kind == "omp":
        flags.update({"--resume": "sessionFile", "-r": "sessionFile"})
    else:
        flags["--session"] = "sessionFile"
    kept = []
    i = 0
    while i < len(args):
        word = args[i]
        name, equal, inline = word.partition("=")
        if kind == "omp" and name in ("--no-extensions", "--mode"):
            raise NativeError("The native OMP bridge requires the interactive TUI with extensions enabled")
        if name in ("--continue", "-c", "--session-id") or (name in ("--resume", "-r", "--session") and name not in flags):
            raise NativeError("Native launch requires an exact session file or ID; continue/prefix session controls are not safe")
        key = flags.get(name)
        if key:
            if not equal:
                i += 1
                if i >= len(args) or args[i].startswith("-"):
                    raise NativeError(f"Native option {name} needs a value")
                inline = args[i]
            if key in payload and payload[key] not in (None, "", inline):
                raise NativeError(f"Conflicting native launch option: {name}")
            payload[key] = inline
        elif kind == "hermes" and word == "--tui":
            pass
        else:
            kept.append(word)
        i += 1
    return payload, kept


def prepare(kind, payload):
    if kind not in KINDS:
        raise NativeError(f"Unsupported native runtime: {kind}")
    payload, custom = _native_args(kind, payload)
    if payload.get("fast") or payload.get("perm"):
        raise NativeError("Native launch preserves the runtime's own speed and permission policy; configure supported native controls after attachment")
    executable = shutil.which(kind)
    if not executable:
        raise NativeError(f"{kind} is not installed on this machine")
    argv = [executable, *custom]
    model, effort, profile = (payload.get(k) for k in ("model", "effort", "profile"))
    session = payload.get("sessionFile") or payload.get("sessionId")
    message = payload.get("message") or ""
    identity = {}
    images = list(payload.get("images") or [])
    attachments = []
    for filename in payload.get("files") or []:
        path = Path(filename).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise NativeError("Native attachments must be existing absolute files on the runtime's machine")
        mime = mimetypes.guess_type(str(path))[0] or ""
        if mime.startswith("image/"):
            images.append({"data": base64.b64encode(path.read_bytes()).decode("ascii"), "mimeType": mime})
        else:
            attachments.append(str(path))
    if attachments:
        message = "\n\n".join(filter(None, [message, "Attached files (use native tools to read them):\n" + "\n".join(attachments)]))
    if kind == "omo" and images:
        raise NativeError("The native OMO TUI has no image prompt attachment API; use its terminal attachment workflow")
    if kind in ("omp", "omo"):
        if model:
            argv += ["--model", str(model)]
        if effort:
            argv += ["--thinking", str(effort)]
        if profile:
            if kind != "omp":
                raise NativeError("OMO does not expose a native --profile launch option")
            argv += ["--profile", str(profile)]
        if kind == "omp":
            extension = Path(__file__).resolve().parent / "omp-extension.ts"
            if not extension.is_file():
                raise NativeError("The installed OMP native extension is missing")
            argv += ["--extension", str(extension)]
            if session:
                path = Path(str(session)).expanduser()
                if not path.is_absolute() or not path.is_file():
                    raise NativeError("OMP resume requires an exact existing absolute session file, not a prefix")
                argv += ["--resume", str(path)]
                identity["sessionFile"] = str(path)
            else:
                directory = _private_dir(state_dir() / "launch" / "omp")
                config = directory / "fresh.yaml"
                try:
                    fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                except FileExistsError:
                    if config.is_symlink() or config.stat().st_uid != os.getuid() or config.stat().st_mode & 0o077 or config.read_text() != "autoResume: false\n":
                        raise NativeError("The private OMP new-session overlay is unsafe")
                else:
                    with os.fdopen(fd, "w") as stream:
                        stream.write("autoResume: false\n")
                argv += ["--config", str(config)]
        else:
            extension = Path(__file__).resolve().parent / "omo-extension.ts"
            if not extension.is_file():
                raise NativeError("The installed OMO native extension is missing")
            argv += ["--extension", str(extension)]
            if session:
                path = Path(str(session)).expanduser()
                if not path.is_absolute() or not path.is_file():
                    raise NativeError("OMO resume requires an exact existing absolute session file")
            else:
                path = _private_dir(state_dir() / "launch" / "omo") / (str(uuid.uuid4()) + ".jsonl")
            argv += ["--session", str(path)]
            identity["sessionFile"] = str(path)
        if images:
            identity["afterAttach"] = {"message": str(message), "images": images}
        elif message:
            argv.append(str(message))
    else:
        inherited = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
        root = inherited.parent.parent if inherited.parent.name == "profiles" else inherited
        if payload.get("hermesHome"):
            home = Path(str(payload["hermesHome"])).expanduser()
            profile = home.name if home.parent.name == "profiles" else "default"
        elif profile:
            home = root if profile == "default" else root / "profiles" / str(profile)
        elif not os.environ.get("HERMES_HOME"):
            # Native launchers follow the sticky profile only when no home is
            # already pinned. Reading this documented selector starts no runtime.
            try:
                profile = (root / "active_profile").read_text(encoding="utf-8-sig").strip() or "default"
            except (OSError, UnicodeError):
                profile = "default"
            if Path(profile).name != profile or profile in (".", ".."):
                raise NativeError("The native active profile selector is not a profile name")
            home = root if profile == "default" else root / "profiles" / profile
        else:
            home = inherited
            profile = home.name if home.parent.name == "profiles" else "default"
        if not home.is_absolute():
            raise NativeError("Hermes profile home must be an exact absolute path")
        home = home.resolve()
        backend = importlib.import_module(".hermes", __package__)
        if not session:
            created = backend.handle("create", {**payload, "hermesHome": str(home), "profile": profile})
            session = created.get("sessionId")
            if not session:
                raise NativeError("The native Hermes gateway did not return the new session identity")
        argv += ["--profile", profile]
        if kind == "hermes":
            argv.append("--tui")
        else:
            argv += ["--hermes-home", str(home)]
        argv += ["--resume", str(session)]
        identity.update(sessionId=str(session), hermesHome=str(home), env={"HERMES_HOME": str(home)})
        if message or model or effort or images:
            identity["afterAttach"] = {"message": str(message), "model": model, "effort": effort, "images": images}
    return {"argv": argv, "unsetEnv": list(SESSION_ENV), **identity}
