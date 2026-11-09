"""Native Hermes/OMH controls; the profile gateway remains the sole execution engine.

The helper runs on Hermes' interpreter. Observation never starts, resumes or
subscribes to a session. Explicit `create` uses Hermes' native daemon bootstrap.
History uses native read-only lineage/compaction readers, cached by per-session
metadata and event watermarks rather than global database/WAL changes.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import uuid

from .common import NativeError, state_dir

CAPABILITIES = dict.fromkeys(("history", "send", "interrupt", "configure", "answer", "fork"), True)


@dataclass(frozen=True)
class _AuthorityIdentity:
    profile_id: str
    instance_id: str
    authority_epoch: int


def _bind_authority(endpoint, described: dict) -> _AuthorityIdentity:
    """Bind the selected profile's authority, not the multiplexer's host epoch.

    Discovery authenticates the control host and selects the served profile.
    Its endpoint carries the host authority epoch, while the ticket-authenticated
    WebSocket describes the selected profile's independent authority epoch.
    """
    if described.get('instance_id') != endpoint.instance_id or described.get('profile_id') != endpoint.profile_id:
        raise NativeError('Hermes gateway identity changed during attachment')
    epoch = described.get('authority_epoch')
    control_home = getattr(endpoint, 'control_home', None)
    multiplexed = control_home is not None and Path(control_home).resolve() != Path(endpoint.profile_id).resolve()
    if type(epoch) is not int or epoch <= 0 or (not multiplexed and epoch != endpoint.authority_epoch):
        raise NativeError('Hermes gateway authority epoch changed during attachment')
    return _AuthorityIdentity(endpoint.profile_id, endpoint.instance_id, epoch)


def _private_text(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise NativeError("Hermes active-session marker must be an owner-only regular file")
        return os.read(fd, 4097).decode().strip()
    finally:
        os.close(fd)


def _process_environments(pid: int) -> list[dict]:
    """Inspect only this foreground process and its descendants, never cwd peers."""
    pending, seen, result = [pid], set(), []
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        proc = Path('/proc') / str(current)
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            env = dict(item.split('=', 1) for item in (proc / 'environ').read_bytes().decode(errors='replace').split('\0') if '=' in item)
            result.append(env)
            pending.extend(int(child) for child in (proc / 'task' / str(current) / 'children').read_text().split())
        except (OSError, ValueError):
            continue
    return result


def _target(payload: dict, operation: str) -> dict:
    pid = payload.get('pid')
    if pid is not None and (type(pid) is not int or pid <= 0):
        raise NativeError('Hermes pid must be a positive integer')
    environments = _process_environments(pid) if pid else []
    markers = set()
    for env in environments:
        marker = env.get('HERMES_TUI_ACTIVE_SESSION_FILE')
        if marker:
            markers.add((env.get('HERMES_HOME', ''), marker, env.get('HERMES_PYTHON_SRC_ROOT', ''),
                         env.get('HERMES_PYTHON', ''), env.get('PYTHONPATH', '')))
    if len({(home, marker) for home, marker, *_ in markers}) > 1:
        raise NativeError('Ambiguous Hermes foreground session/profile; select its exact owning pid')
    selected = next(iter(markers), None)
    if pid is not None and selected is None:
        raise NativeError('Hermes foreground TUI has not published an owning active-session marker yet')
    home = payload.get('hermesHome') or (selected[0] if selected else None)
    if not home and operation == 'create':
        home = os.environ.get('HERMES_HOME') or str(Path.home() / '.hermes')
    if not home:
        raise NativeError('Hermes attachment needs an owning pid with HERMES_HOME or explicit hermesHome and sessionId')
    home = str(Path(home).expanduser().resolve())
    if not Path(home).is_dir():
        raise NativeError('Hermes profile home does not exist; select an installed native profile')
    if selected and selected[0] and str(Path(selected[0]).expanduser().resolve()) != home:
        raise NativeError('Hermes profile does not match the foreground owner')
    marker = payload.get('sessionFile') or (selected[1] if selected else None)
    if selected and payload.get('sessionFile') and Path(marker) != Path(selected[1]):
        raise NativeError('Hermes session marker does not match the foreground owner')
    try:
        raw_marker = _private_text(Path(marker)) if marker else ''
    except (OSError, UnicodeError) as error:
        raise NativeError('Hermes active-session marker is not safely readable') from error
    marked = None
    if raw_marker:
        try:
            record = json.loads(raw_marker)
        except ValueError as error:
            raise NativeError('Hermes active-session marker has not published valid native JSON') from error
        if not isinstance(record, dict) or not isinstance(record.get('session_id'), str):
            raise NativeError('Hermes active-session marker has no native session_id string')
        marked = record['session_id']
    if marker and not marked:
        raise NativeError('Hermes foreground TUI has not opened a native session yet')
    session = payload.get('sessionId') or marked
    if marked and session != marked:
        raise NativeError('Stale Hermes session identity: the terminal switched sessions')
    if operation != 'create' and not session:
        raise NativeError('Hermes attachment requires an exact sessionId or native active-session marker')
    if session and (not isinstance(session, str) or len(session) > 512 or session in {'latest', 'last'}):
        raise NativeError('Hermes requires an exact native session identity, not a latest-session selector')
    root = (selected[2] if selected else '') or os.environ.get('HERMES_PYTHON_SRC_ROOT')
    if not root:
        root = str(Path.home() / '.hermes' / 'hermes-agent')
    python = (selected[3] if selected else '') or os.environ.get('HERMES_PYTHON') or str(Path(root) / 'venv' / 'bin' / 'python')
    if not Path(root).is_dir() or not Path(python).is_file():
        raise NativeError('Hermes native interpreter/source unavailable; provide HERMES_PYTHON and HERMES_PYTHON_SRC_ROOT from its launcher')
    return {'home': home, 'session': session, 'source': root, 'python': python,
            'pythonPath': selected[4] if selected else None, 'pid': pid, 'sessionFile': str(marker) if marker else None}


def handle(operation: str, payload: dict) -> dict:
    if operation not in {'state', 'discover', 'history', 'models', 'send', 'interrupt', 'configure', 'answer', 'fork', 'close', 'create'}:
        raise NativeError(f'Unsupported Hermes operation: {operation}')
    target = _target(payload, operation)
    env = dict(os.environ)
    env['HERMES_HOME'] = target['home']
    env['PYTHONPATH'] = target.get('pythonPath') or target['source']
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    # Session launch policy belongs to session.create, never to every session of
    # an owner that native ensure_gateway_runtime might start.
    for name in ('HERMES_TUI_GATEWAY_URL', 'HERMES_TUI_SIDECAR_URL', 'HERMES_API_KEY', 'HERMES_INFERENCE_API_KEY',
                 'HERMES_TUI_RESUME', 'HERMES_TUI_ACTIVE_SESSION_FILE', 'HERMES_SESSION_ID',
                 'HERMES_MODEL', 'HERMES_INFERENCE_MODEL', 'HERMES_TUI_PROVIDER', 'HERMES_INFERENCE_PROVIDER',
                 'HERMES_TUI_TOOLSETS', 'HERMES_TUI_SKILLS', 'HERMES_CWD', 'TERMINAL_CWD', 'HERMES_YOLO',
                 'HERMES_YOLO_MODE', 'HERMES_IGNORE_RULES', 'HERMES_SAFE_MODE', 'HERMES_IGNORE_USER_CONFIG',
                 'HERMES_ACCEPT_HOOKS', 'HERMES_TUI_CHECKPOINTS', 'HERMES_TUI_PASS_SESSION_ID', 'HERMES_KANBAN_BOARD',
                 'HERMES_TUI_CLIENT_ENV'):
        env.pop(name, None)
    try:
        run = subprocess.run([target['python'], str(Path(__file__).with_name('hermes-attach.py'))],
                             input=json.dumps({'operation': operation, 'payload': payload, 'target': target}),
                             text=True, capture_output=True, env=env, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise NativeError('Hermes native transport unavailable; outcome may be unknown; do not replay a send') from error
    try:
        reply = json.loads(run.stdout)
    except (ValueError, TypeError) as error:
        raise NativeError('Hermes helper returned no valid result; no fallback execution or resend was attempted') from error
    if run.returncode or not reply.get('ok'):
        raise NativeError(reply.get('error') or 'Hermes gateway request failed')
    return reply['data']


def _revision(snapshot: dict, endpoint) -> str:
    return ':'.join(str(value) for value in (endpoint.instance_id, snapshot['authority_epoch'], snapshot['replay_epoch'],
                                           snapshot['session_id'], snapshot['revision'], snapshot['execution_generation'],
                                           snapshot['last_sequence'], snapshot['message_count'],
                                           snapshot.get('history_revision', '')))


def _request_id(session: str, snapshot: dict, prompt: dict, endpoint) -> str:
    # Opaque but self-contained, so an old card cannot accidentally answer a later generation.
    value = [endpoint.profile_id, endpoint.instance_id, snapshot['authority_epoch'], session,
             prompt['execution_generation'], prompt['kind'], prompt['prompt_id']]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode().rstrip('=')


def _state(snapshot: dict, endpoint, target: dict) -> dict:
    info = snapshot.get('info') or {}
    pending = snapshot.get('pending') or []
    prompts = snapshot.get('prompts') or []
    requests = []
    for prompt in prompts:
        if prompt.get('kind') not in {'approval', 'clarify'}:
            continue
        choices = [{'id': str(index), 'label': choice, 'value': choice}
                   for index, choice in enumerate(prompt.get('choices') or [])]
        requests.append({'id': _request_id(snapshot['session_id'], snapshot, prompt, endpoint),
                         'kind': 'permission' if prompt['kind'] == 'approval' else 'question',
                         'title': prompt.get('question') or prompt.get('description') or 'Hermes command approval',
                         'detail': prompt.get('command') or '', 'choices': choices,
                         'answerable': True, 'sessionId': snapshot['session_id'],
                         **({'other': True} if prompt['kind'] == 'clarify' else {})})
    # Server requests requiring secret/sudo/browser interaction stay in the native TUI.
    for request in snapshot.get('open_requests') or []:
        requests.append({'id': request['id'], 'kind': 'permission', 'title': f"Hermes {request['method']}",
                         'detail': 'Answer this native request in the owning Hermes/OMH terminal',
                         'choices': [], 'answerable': False})
    if snapshot.get('dialogReplayUnavailable') and not prompts and not snapshot.get('open_requests'):
        requests.append({'id': f"native-dialog-{snapshot['execution_generation']}", 'kind': 'question',
                         'title': 'Hermes is waiting for a native dialog',
                         'detail': 'The native replay no longer contains this dialog; answer it in the owning terminal',
                         'choices': [], 'answerable': False})
    working = snapshot.get('running') is True or bool(prompts) or any(row.get('status') in {'queued', 'started', 'unknown'} for row in pending)
    return {'sessionId': snapshot['session_id'], 'hermesHome': target['home'], 'profileId': endpoint.profile_id,
            'pid': target.get('pid'), 'sessionFile': target.get('sessionFile'), 'revision': _revision(snapshot, endpoint),
            'model': {'id': info.get('model') or '', 'provider': info.get('provider'), 'contextWindow': info.get('context_length')},
            'thinking': info.get('reasoning_effort'), 'working': working,
            # Idle admission state alone says nothing about native detached DAG/background work.
            'settled': False if working else None, 'pendingAsyncWork': True if pending else None,
            'capabilities': {**CAPABILITIES, 'images': True,
                             'configure': info.get('source') in {'cli', 'tui', 'gui', 'acp', 'tool', 'oneshot'},
                             'fork': info.get('source') in {'cli', 'tui', 'gui', 'acp', 'tool', 'oneshot'}},
            'pendingRequests': requests, 'jobs': pending,
            'limitations': ['Secret, sudo, browser and other terminal-only dialogs remain in the owning native terminal',
                            'Native read-only lineage projection is cached and served in bounded pages',
                            'Gateway idle does not establish native background-work settlement']}


def _content(value):
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return '' if value is None else json.dumps(value, ensure_ascii=False)
    blocks = []
    for block in value:
        if not isinstance(block, dict):
            continue
        kind = block.get('type')
        if kind in {'text', 'input_text', 'output_text'}:
            blocks.append({'type': 'text', 'text': block.get('text') or ''})
        elif kind == 'image':
            source = block.get('source') or {}
            if source.get('type') == 'base64':
                blocks.append({'type': 'image', 'data': source.get('data'), 'mimeType': source.get('media_type')})
            elif source.get('type') == 'url':
                blocks.append({'type': 'image', 'path': source.get('url')})
            else:
                blocks.append({key: value for key, value in block.items() if key in {'type', 'data', 'mimeType', 'path'}})
        elif kind == 'image_url':
            url = block.get('image_url', {})
            url = url.get('url') if isinstance(url, dict) else url
            if isinstance(url, str) and url.startswith('data:') and ';base64,' in url:
                header, data = url.split(';base64,', 1)
                blocks.append({'type': 'image', 'data': data, 'mimeType': header[5:]})
            elif isinstance(url, str):
                blocks.append({'type': 'image', 'path': url})
        elif kind in {'toolCall', 'tool_use'}:
            blocks.append({'type': 'toolCall', 'id': block.get('id'), 'name': block.get('name'),
                           'arguments': block.get('arguments', block.get('input', {}))})
    return blocks


MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _image_mime(data: bytes) -> str | None:
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def _prepare_images(payload: dict) -> list[dict]:
    """Validate the entire batch before staging any bytes or admitting a prompt."""
    prepared, total = [], 0
    for image in payload.get('images') or []:
        if not isinstance(image, dict) or not isinstance(image.get('data'), str):
            raise NativeError('Malformed Hermes image attachment')
        if len(image['data']) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise NativeError('Hermes image attachment exceeds 20 MiB')
        try:
            data = base64.b64decode(image['data'], validate=True)
        except ValueError as error:
            raise NativeError('Malformed Hermes image attachment encoding') from error
        mime = _image_mime(data)
        if not mime or image.get('mimeType', mime) != mime:
            raise NativeError('Unsupported Hermes image type or mismatched MIME type')
        total += len(data)
        prepared.append({'filename': 'attachment', 'content_base64': image['data']})
    for filename in payload.get('files') or []:
        if not isinstance(filename, str):
            raise NativeError('Hermes attachment path must be text')
        path = Path(filename).expanduser()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            node = os.fstat(fd)
            if not stat.S_ISREG(node.st_mode):
                raise NativeError('Hermes attachment must be a regular file')
            prefix = os.read(fd, 16)
            mime = _image_mime(prefix)
            if not mime:
                if path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.heic', '.svg', '.avif'}:
                    raise NativeError('Unsupported Hermes image attachment format')
                continue
            if node.st_size > MAX_IMAGE_BYTES:
                raise NativeError('Hermes image attachment exceeds 20 MiB')
            chunks, size = [prefix], len(prefix)
            while chunk := os.read(fd, min(65536, MAX_IMAGE_BYTES + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise NativeError('Hermes image attachment exceeds 20 MiB')
            data = b''.join(chunks)
            total += len(data)
            prepared.append({'filename': path.name, 'content_base64': base64.b64encode(data).decode('ascii')})
        finally:
            os.close(fd)
    if total > MAX_IMAGE_BYTES:
        raise NativeError('Hermes image attachment batch exceeds 20 MiB')
    return prepared


def normalize_history(rows: list, session: str) -> list:
    messages = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get('display_kind') == 'hidden':
            continue
        role = {'tool': 'toolResult', 'function': 'toolResult'}.get(row.get('role'), row.get('role'))
        if role not in {'user', 'assistant', 'toolResult'}:
            continue
        content = _content(row['content'] if row.get('content') is not None else row.get('text', ''))
        calls = row.get('tool_calls') or []
        if calls:
            content = [{'type': 'text', 'text': content}] if isinstance(content, str) and content else list(content) if isinstance(content, list) else []
            for call in calls:
                function = call.get('function') or call
                args = function.get('arguments', {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                content.append({'type': 'toolCall', 'id': call.get('id'), 'name': function.get('name'), 'arguments': args})
        native_id = row.get('row_id', row.get('_row_id', row.get('id')))
        if native_id is None:
            native_id = f"unpersisted-{index}-{hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()[:16]}"
        message = {'id': f'{session}:{native_id}', 'role': role, 'content': content}
        for key in ('timestamp', 'model', 'provider', 'usage'):
            if row.get(key) is not None:
                message[key] = row[key]
        if role == 'toolResult':
            message.update(toolCallId=row.get('tool_call_id'), toolName=row.get('name') or row.get('tool_name'), isError=bool(row.get('is_error', row.get('isError', False))))
        messages.append(message)
    return messages


def _cache(endpoint, session: str):
    directory = state_dir() / 'hermes'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise NativeError('Hermes history cache directory must be owner-only')
    key = hashlib.sha256(f'{endpoint.profile_id}\0{session}'.encode()).hexdigest()
    path = directory / f'{key}.sqlite'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        node = os.fstat(fd)
        if not stat.S_ISREG(node.st_mode) or node.st_uid != os.getuid() or node.st_mode & 0o077:
            raise NativeError('Unsafe Hermes history cache')
    finally:
        os.close(fd)
    db = sqlite3.connect(path, timeout=10)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS messages (ordinal INTEGER PRIMARY KEY, id TEXT UNIQUE, body TEXT)')
        return db
    except BaseException:
        db.close()
        raise


def _page(db, revision: str, payload: dict) -> dict:
    limit = payload.get('limit', 200)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise NativeError('Hermes history limit must be between 1 and 500')
    before = payload.get('before')
    if before:
        found = db.execute('SELECT ordinal FROM messages WHERE id=?', (before,)).fetchone()
        if not found:
            raise NativeError('Hermes history boundary is stale; reload the active branch')
        rows = db.execute('SELECT ordinal, body FROM messages WHERE ordinal<? ORDER BY ordinal DESC LIMIT ?', (found[0], limit)).fetchall()
        reset = False
    elif payload.get('cursor') == revision:
        earlier = db.execute('SELECT 1 FROM messages ORDER BY ordinal DESC LIMIT 1 OFFSET ?', (limit,)).fetchone()
        return {'messages': [], 'cursor': revision, 'revision': revision, 'hasEarlier': bool(earlier), 'reset': False}
    else:
        rows = db.execute('SELECT ordinal, body FROM messages ORDER BY ordinal DESC LIMIT ?', (limit,)).fetchall()
        reset = True
    rows.reverse()
    earlier = bool(rows and db.execute('SELECT 1 FROM messages WHERE ordinal<? LIMIT 1', (rows[0][0],)).fetchone())
    return {'messages': [json.loads(row[1]) for row in rows], 'cursor': revision, 'revision': revision, 'hasEarlier': earlier, 'reset': reset}


def _read_store(target: dict, session: str, history: bool = False) -> dict:
    """Use Hermes' explicit read-only store, never its writer or attachment lifecycle.

    The watermark reads only indexed session/receipt/admission metadata. Native
    transcript projection is invoked only for a changed history page request.
    """
    from hermes_state import SessionDB
    from hermes_state_local import POLICY_PREFIX
    from hermes_state_runtime import list_session_admissions
    with closing(SessionDB(db_path=Path(target['home']) / 'state.db', read_only=True)) as store:
        # One SQLite snapshot spans the receipt, lineage and optional transcript.
        # BEGIN/ROLLBACK on this mode=ro handle never changes the native store.
        with store._read_ctx() as connection:
            connection.execute('BEGIN')
        try:
            row = store.get_session(session)
            if row is None:
                raise NativeError('Hermes exact session no longer exists')
            saved = store.get_meta(POLICY_PREFIX + session)
            receipt = json.loads(saved) if saved else None
            physical = receipt['entry']['session_id'] if receipt else store.get_compression_tip(session)
            ids = store._resume_lineage_ids(physical)
            facts = [tuple(store._read_one(
                'SELECT id, transcript_revision, message_count, parent_session_id, end_reason FROM sessions WHERE id=?',
                (sid,))) for sid in ids]
            epoch = store._read_one('SELECT epoch FROM runtime_epoch WHERE singleton=1')
            if epoch is None:
                raise NativeError('Hermes read-only authority metadata is unavailable')
            pending = list_session_admissions(store, session_id=session)
            result = {'session_id': session, 'authority_epoch': epoch[0],
                      'revision': row['runtime_revision'], 'execution_generation': row['runtime_generation'],
                      'message_count': sum(fact[2] or 0 for fact in facts),
                      'history_revision': hashlib.sha256(json.dumps([physical, facts]).encode()).hexdigest(),
                      'pending': [{key: admission.get(key) for key in ('admission_id', 'status', 'generation', 'seq')}
                                  for admission in pending]}
            if history:
                # These are the same real native projections the owning gateway
                # uses, on a read_only=True store (no index backfill writes).
                from gateway.session_local_recovery import _merge_live_in_stored_order
                live = store.get_messages_as_conversation(physical, include_row_ids=True)
                display = store.get_messages_as_conversation(
                    physical, include_ancestors=True, include_compacted=True, include_row_ids=True)
                result['messages'] = _merge_live_in_stored_order(display, live)
            return result
        finally:
            with store._read_ctx() as connection:
                connection.execute('ROLLBACK')


async def _read_snapshot(client, endpoint, target: dict) -> dict:
    session = target['session']
    active = await client.rpc('session.active_list', current_session_id=session)
    matches = [row for row in active.get('sessions') or [] if row.get('id') == session]
    if len(matches) != 1:
        raise NativeError('Hermes exact session is no longer live; observing never reopens or restores it')
    stored = await asyncio.to_thread(_read_store, target, session)
    if stored['authority_epoch'] != endpoint.authority_epoch:
        raise NativeError('Hermes profile authority changed during the read')
    with closing(_cache(endpoint, session)) as cache, cache:
        cache.execute('BEGIN IMMEDIATE')
        saved = cache.execute("SELECT value FROM metadata WHERE key='observer'").fetchone()
        observer = json.loads(saved[0]) if saved else {}
        identity = [endpoint.instance_id, endpoint.authority_epoch, stored['execution_generation']]
        same = observer.get('identity') == identity
        # Approval policy can change without advancing transcript/runtime revision;
        # read native presentation state, never refresh/reapply a launch policy.
        native_info = await client.rpc('session.info', session_id=session)
        if native_info.get('profile_id') not in (None, endpoint.profile_id):
            raise NativeError('Hermes returned information for a different profile')
        info = {key: native_info.get(key) for key in (
            'profile_id', 'source', 'model', 'provider', 'context_length', 'reasoning_effort', 'reasoning_effort_wire', 'yolo')}
        sequence = observer.get('sequence', 0) if same else 0
        replay_epoch = observer.get('replay_epoch') if same else None
        replay = await client.rpc('session.events.since', session_id=session,
                                  last_sequence=sequence, **({'replay_epoch': replay_epoch} if replay_epoch else {}))
        if replay_epoch is None:
            # The first nonattaching read discovers the ring's epoch, then asks
            # it for retained events. No session.resume/subscription is involved.
            replay_epoch = replay['replay_epoch']
            replay = await client.rpc('session.events.since', session_id=session,
                                      last_sequence=0, replay_epoch=replay_epoch)
        gap = replay.get('snapshot_required') is True
        prompts = dict(observer.get('prompts') or {}) if same and not gap else {}
        for event in replay.get('events') or []:
            if (event.get('session_id') != session
                    or event.get('authority_epoch', endpoint.authority_epoch) != endpoint.authority_epoch
                    or event.get('execution_generation') != stored['execution_generation']):
                continue
            body, kind = event.get('payload') or {}, event.get('type')
            if kind in {'clarify.request', 'approval.request'} and body.get('prompt_id'):
                prompts[body['prompt_id']] = {**body, 'kind': kind.split('.')[0],
                                               'execution_generation': stored['execution_generation']}
            elif kind in {'clarify.settled', 'approval.settled'}:
                prompts.pop(body.get('prompt_id'), None)
        approvals = await client.rpc('approval.pending', session_id=session)
        prompts = {key: prompt for key, prompt in prompts.items() if prompt.get('kind') != 'approval'}
        for approval in approvals.get('approvals') or []:
            prompt_id = approval['request_id']
            prompts[prompt_id] = {**approval, 'kind': 'approval', 'prompt_id': prompt_id,
                                  'execution_generation': stored['execution_generation']}
        observer = {'identity': identity, 'prompts': prompts,
                    'sequence': replay['last_sequence'], 'replay_epoch': replay['replay_epoch']}
        cache.execute("INSERT OR REPLACE INTO metadata VALUES ('observer', ?)", (json.dumps(observer),))
    status = matches[0].get('status')
    return {**stored, 'running': status in {'working', 'streaming', 'waiting', 'starting'},
            'info': info, 'prompts': list(prompts.values()), 'open_requests': replay.get('open_requests') or [],
            'replay_epoch': replay['replay_epoch'], 'last_sequence': replay['last_sequence'],
            'dialogReplayUnavailable': status == 'waiting' and not prompts, 'messages': []}


async def _history(client, endpoint, snapshot: dict, payload: dict, target: dict) -> tuple[dict, dict]:
    session = snapshot['session_id']
    with closing(_cache(endpoint, session)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        metadata = dict(db.execute('SELECT key, value FROM metadata'))
        previous = json.loads(metadata.get('stamp', 'null'))
        identity = [endpoint.instance_id, snapshot['authority_epoch'], snapshot['replay_epoch']]
        # Polls may race: never overwrite a newer page with a late snapshot.
        if previous and previous['identity'] == identity and previous['sequence'] > snapshot['last_sequence']:
            return _page(db, metadata['revision'], payload), snapshot
        revision = _revision(snapshot, endpoint)
        if metadata.get('revision') == revision:
            return _page(db, revision, payload), snapshot
        durable = [*identity, snapshot.get('history_revision'), snapshot['revision'], snapshot['execution_generation'],
                   snapshot['message_count'], snapshot.get('running')]
        if previous is None or previous.get('durable') != durable:
            full = await asyncio.to_thread(_read_store, target, session, True)
            if full['authority_epoch'] != endpoint.authority_epoch:
                raise NativeError('Hermes profile authority changed during the history read')
            snapshot = {**snapshot, **full}
            revision = _revision(snapshot, endpoint)
            identity = [endpoint.instance_id, snapshot['authority_epoch'], snapshot['replay_epoch']]
            durable = [*identity, snapshot.get('history_revision'), snapshot['revision'], snapshot['execution_generation'],
                       snapshot['message_count'], snapshot.get('running')]
            messages = normalize_history(full.get('messages') or [], session)
            db.execute('DELETE FROM messages')
            db.executemany('INSERT INTO messages VALUES (?, ?, ?)',
                           ((index, row['id'], json.dumps(row)) for index, row in enumerate(messages)))
        else:
            db.execute("DELETE FROM messages WHERE id LIKE ?", (f'{session}:stream:%',))
        generation = snapshot['execution_generation']
        same_stream = previous and previous['identity'] == identity and previous.get('generation') == generation
        stream = previous.get('stream', '') if same_stream else ''
        complete = previous.get('complete', True) if same_stream else True
        segment = previous.get('segment', 0) if same_stream else 0
        sequence = previous['sequence'] if same_stream else 0
        replay = await client.rpc('session.events.since', session_id=session,
                                  last_sequence=sequence, replay_epoch=snapshot['replay_epoch'])
        if replay.get('snapshot_required') is True:
            stream, complete = '', False
        for event in replay.get('events') or []:
            if event.get('session_id') != session or event.get('authority_epoch', snapshot['authority_epoch']) != snapshot['authority_epoch']:
                continue
            if event.get('execution_generation') != generation:
                continue
            kind, body = event.get('type'), event.get('payload') or {}
            if kind == 'message.start':
                stream, complete, segment = '', True, 0
            elif kind == 'message.delta':
                if body.get('text') is None:
                    stream, segment = '', segment + 1
                elif isinstance(body.get('text'), str):
                    stream += body['text']
            elif kind == 'message.complete':
                stream = ''
        if stream and complete and snapshot.get('running'):
            message = {'id': f'{session}:stream:{generation}:{segment}', 'role': 'assistant',
                       'content': stream, 'model': (snapshot.get('info') or {}).get('model')}
            ordinal = db.execute('SELECT COALESCE(MAX(ordinal), -1) + 1 FROM messages').fetchone()[0]
            db.execute('INSERT INTO messages VALUES (?, ?, ?)', (ordinal, message['id'], json.dumps(message)))
        stamp = {'identity': identity, 'sequence': replay.get('last_sequence', snapshot['last_sequence']),
                 'durable': durable, 'generation': generation, 'stream': stream, 'complete': complete, 'segment': segment}
        db.executemany('INSERT OR REPLACE INTO metadata VALUES (?, ?)',
                       [('stamp', json.dumps(stamp)), ('revision', revision)])
        page = _page(db, revision, payload)
        if not complete:
            page['streamingUnavailable'] = 'Hermes event replay has a gap; completed native history remains authoritative'
        return page, snapshot


async def _dispatch(client, endpoint, operation: str, payload: dict, target: dict) -> dict:
    described = await client.rpc('runtime.describe')
    endpoint = _bind_authority(endpoint, described)
    if operation == 'create':
        params = {'source': 'tui', 'request_id': payload.get('requestId') or uuid.uuid4().hex}
        for name in ('cwd', 'model', 'provider'):
            if payload.get(name):
                params[name] = payload[name]
        if params.get('model') and ':' in params['model']:
            provider, model = params['model'].split(':', 1)
            if params.get('provider') and params['provider'] != provider:
                raise NativeError('Hermes model/provider selection conflicts')
            params['provider'], params['model'] = provider, model
        if payload.get('effort'):
            params['reasoning'] = payload['effort']
        created = await client.rpc('session.create', **params)
        try:
            return {**_state(created, endpoint, target), 'created': True}
        finally:
            await client.rpc('session.detach', session_id=created['session_id'], subscription_id=created['subscription_id'])
    if operation == 'close':
        # Reads own no native subscription: closing this short-lived transport
        # cannot end a session, arm orphan close, or cancel a pending user close.
        return {'sessionId': target['session'], 'detached': False, 'observerOnly': True}
    attached = operation not in {'state', 'discover', 'history', 'models'}
    snapshot = (await client.rpc('session.resume', session_id=target['session'], omit_messages=True) if attached
                else await _read_snapshot(client, endpoint, target))
    if snapshot.get('authority_epoch') != endpoint.authority_epoch or (snapshot.get('info', {}).get('profile_id') not in (None, endpoint.profile_id)):
        raise NativeError('Hermes resumed a different profile/authority')
    session = snapshot['session_id']
    params = {'session_id': session}
    try:
        if operation in {'state', 'discover'}:
            return _state(snapshot, endpoint, target)
        if operation == 'history':
            page, snapshot = await _history(client, endpoint, snapshot, payload, target)
            return page
        if operation == 'models':
            inventory = await client.rpc('model.options', **params)
            models = []
            for provider in inventory.get('providers') or []:
                for model in provider.get('models') or []:
                    if not isinstance(model, str):
                        continue
                    caps = (provider.get('capabilities') or {}).get(model) or {}
                    models.append({'id': f"{provider['slug']}:{model}", 'label': f"{model} ({provider.get('name') or provider['slug']})",
                                   'efforts': target.get('reasoningEfforts', []) if caps.get('reasoning') else [],
                                   'fast': caps.get('fast') is True})
            info = snapshot.get('info') or {}
            current = inventory.get('model') or info.get('model')
            provider = inventory.get('provider') or info.get('provider')
            return {'models': models, 'model': f'{provider}:{current}' if provider and current else current,
                    'effort': info.get('reasoning_effort'), 'perms': [{'id': 'ask', 'label': 'Native approvals'}, {'id': 'yolo', 'label': 'Native session YOLO'}],
                    'mode': 'yolo' if info.get('yolo') is True else 'ask'}
        if operation == 'send':
            text = payload.get('text')
            if not isinstance(text, str) or not text:
                raise NativeError('Hermes send requires text')
            delivery = payload.get('delivery', 'prompt')
            if delivery not in {'prompt', 'steer', 'followUp'}:
                raise NativeError('Unsupported Hermes delivery mode')
            images = _prepare_images(payload)
            if delivery == 'steer':
                if images:
                    raise NativeError('Hermes native steering supports text only')
                result = await client.rpc('session.steer', **params, text=text, execution_generation=snapshot['execution_generation'])
                return {**result, 'admitted': result.get('status') in {'queued', 'redirected'} and result.get('delivered') is not False}
            attachments = []
            for image in images:
                staged = await client.rpc('image.attach_bytes', **params, **image)
                attachments.append({'path': staged['path'], 'mime': staged['mime']})
            result = await client.rpc('prompt.submit', **params, text=text, input_id=payload.get('requestId') or uuid.uuid4().hex,
                                      queued=delivery == 'followUp', **({'attachments': attachments} if attachments else {}))
            return {**result, 'admitted': bool(result.get('admission_id')) and result.get('status') in {'queued', 'started', 'terminal'}}
        if operation == 'interrupt':
            return await client.rpc('session.interrupt', **params, execution_generation=snapshot['execution_generation'])
        if operation in {'configure', 'fork'}:
            async def mutate(kind, body):
                nonlocal snapshot
                result = await client.rpc('session.mutate', **params, request_id=uuid.uuid4().hex,
                                          expected_revision=snapshot['revision'], expected_generation=snapshot['execution_generation'],
                                          operation=kind, payload=body)
                snapshot = await client.rpc('session.resume', **params, omit_messages=True)
                return result
            if operation == 'fork':
                result = await mutate('branch', {})
                child = result.get('branched_session_id')
                if not child:
                    raise NativeError('Hermes did not confirm a native fork identity')
                # The owner auto-attaches the child for branching viewers; leave it without ending it.
                forked = await client.rpc('session.resume', session_id=child, omit_messages=True)
                await client.rpc('session.detach', session_id=child, subscription_id=forked['subscription_id'])
                return {**_state(forked, endpoint, {**target, 'pid': None, 'sessionFile': None}), 'forked': True}
            if payload.get('perm') not in (None, 'ask', 'yolo'):
                raise NativeError('Hermes supports only native session ask/yolo controls here')
            if payload.get('model'):
                model = payload['model']
                if ':' not in model:
                    raise NativeError('Select a provider-qualified Hermes model from models')
                provider, model = model.split(':', 1)
                body = {'model': model, 'provider': provider}
                if payload.get('effort'):
                    body['reasoning'] = payload['effort']
                await mutate('model', body)
            elif payload.get('effort'):
                await mutate('reasoning', {'effort': payload['effort']})
            if payload.get('perm'):
                expected = '1' if payload['perm'] == 'yolo' else '0'
                applied = await client.rpc('config.set', **params, key='yolo', value=expected)
                if applied.get('value') != expected or applied.get('scope') != 'session':
                    raise NativeError('Hermes did not confirm the requested session approval policy')
                snapshot = await client.rpc('session.resume', **params, omit_messages=True)
            info = snapshot.get('info') or {}
            if payload.get('model') and (info.get('model') != model or info.get('provider') != provider):
                selected = await client.rpc('model.options', **params)
                if selected.get('model') != model or selected.get('provider') != provider:
                    raise NativeError('Hermes accepted the selection but has not confirmed it applied; inspect the native session before retrying')
            if payload.get('effort') and payload['effort'] not in {info.get('reasoning_effort'), info.get('reasoning_effort_wire')}:
                raise NativeError('Hermes has not confirmed the requested reasoning effort applied')
            return _state(snapshot, endpoint, target)
        if operation == 'answer':
            # Generation/profile/instance fencing precedes any native response callback.
            request_id = payload.get('requestId', '')
            try:
                identity = json.loads(base64.urlsafe_b64decode(request_id + '=' * (-len(request_id) % 4)))
                profile, instance, epoch, requested_session, generation, kind, prompt_id = identity
            except (ValueError, TypeError, UnicodeError):
                raise NativeError('Unsupported native dialog or malformed Hermes request identity')
            if (profile, instance, epoch, requested_session, generation) != (endpoint.profile_id, endpoint.instance_id, endpoint.authority_epoch, session, snapshot['execution_generation']):
                raise NativeError('Stale Hermes approval: session/profile/authority/generation changed')
            prompt = next((row for row in snapshot.get('prompts') or [] if row.get('prompt_id') == prompt_id and row.get('kind') == kind), None)
            answer = payload.get('answer')
            base = {**params, 'execution_generation': generation, 'prompt_id': prompt_id}
            if kind == 'approval':
                choice = answer.get('choice') if isinstance(answer, dict) else answer
                if choice not in {'once', 'session', 'always', 'deny'} or (prompt and choice not in prompt.get('choices', [])):
                    raise NativeError('Hermes approval choice was not offered by the native owner')
                return await client.rpc('approval.respond', **base, choice=choice)
            if kind == 'clarify':
                answer = answer.get('answer') if isinstance(answer, dict) else answer
                if not isinstance(answer, str):
                    raise NativeError('Hermes clarify requires a text answer')
                return await client.rpc('clarify.respond', **base, answer=answer)
            raise NativeError('Unsupported Hermes native dialog')
        raise NativeError(f'Unsupported Hermes operation: {operation}')
    finally:
        # No end_reason, no denial, no interrupt: other viewers and held dialogs survive.
        if attached:
            await client.rpc('session.detach', session_id=session, subscription_id=snapshot['subscription_id'])


async def _execute(operation: str, payload: dict, target: dict) -> dict:
    from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint, ensure_gateway_runtime
    from hermes_cli.gateway_client import connect_gateway
    from hermes_constants import VALID_REASONING_EFFORTS
    target = {**target, 'reasoningEfforts': ['none', *VALID_REASONING_EFFORTS]}
    home = Path(target['home'])
    receipt = (await asyncio.to_thread(ensure_gateway_runtime, home, idle_exit=True) if operation == 'create'
               else await asyncio.to_thread(discover_gateway_endpoint, home))
    if not attachable(receipt):
        raise NativeError(f'Hermes owning gateway is {receipt.state}: {receipt.reason_code}; open the native terminal first')
    async with connect_gateway(endpoint=receipt.endpoint, max_size=64 * 1024 * 1024) as client:
        return await _dispatch(client, receipt.endpoint, operation, payload, target)
