"""Deterministic Hermes attachment invariants; no native runtime or model calls."""
import home_guard  # noqa: F401
import base64
from copy import deepcopy
from contextlib import closing, contextmanager
import json
import importlib.util
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bin'))
from shepherdr_native import hermes
from shepherdr_native.common import NativeError


def endpoint(profile='profile-a', instance='owner-a', epoch=7, control_home=None):
    return SimpleNamespace(profile_id=profile, instance_id=instance, authority_epoch=epoch, control_home=control_home)


def snapshot(**changes):
    result = {'session_id': 'session-a', 'stored_session_id': 'session-a', 'revision': 3,
              'authority_epoch': 7, 'execution_generation': 9, 'replay_epoch': 'replay-a',
              'last_sequence': 20, 'message_count': 2, 'subscription_id': 'viewer-a',
              'running': False, 'pending': [], 'prompts': [], 'messages': [],
              'info': {'profile_id': 'profile-a', 'source': 'tui', 'model': 'model-a', 'provider': 'provider-a', 'reasoning_effort': 'high'}}
    result.update(changes)
    return result


class Client:
    def __init__(self, state=None, replies=None, owner=None):
        self.state = state or snapshot()
        self.replies = replies or {}
        self.owner = owner or endpoint()
        self.calls = []
        self.native_reads = []

    async def rpc(self, method, **params):
        self.calls.append((method, deepcopy(params)))
        if method in self.replies:
            result = self.replies[method]
            if isinstance(result, Exception):
                raise result
            if callable(result):
                return result(params)
            if method == 'session.events.since':
                return {'replay_epoch': self.state['replay_epoch'], 'snapshot_required': False,
                        'open_requests': deepcopy(self.state.get('open_requests') or []), **deepcopy(result)}
            return deepcopy(result)
        if method == 'runtime.describe':
            return {'profile_id': self.owner.profile_id, 'instance_id': self.owner.instance_id,
                    'authority_epoch': self.owner.authority_epoch}
        if method in {'session.resume', 'session.create'}:
            return deepcopy(self.state)
        if method == 'session.active_list':
            status = 'waiting' if self.state.get('prompts') or self.state.get('open_requests') else 'working' if self.state.get('running') else 'idle'
            return {'sessions': [{'id': self.state['session_id'], 'status': status}]}
        if method == 'session.info':
            return deepcopy(self.state['info'])
        if method == 'approval.pending':
            return {'approvals': [{'request_id': prompt['prompt_id'], 'command': prompt.get('command', ''),
                                   'description': prompt.get('description', ''), 'choices': prompt.get('choices', [])}
                                  for prompt in self.state.get('prompts') or [] if prompt['kind'] == 'approval']}
        if method == 'session.detach':
            return {**params, 'detached': True}
        if method == 'session.events.since':
            events = [{'type': prompt['kind'] + '.request', 'session_id': self.state['session_id'],
                       'authority_epoch': self.state['authority_epoch'], 'execution_generation': prompt['execution_generation'],
                       'seq': 1, 'payload': deepcopy(prompt)} for prompt in self.state.get('prompts') or []]
            return {'events': events if params.get('last_sequence', 0) == 0 else [],
                    'last_sequence': self.state['last_sequence'], 'replay_epoch': self.state['replay_epoch'],
                    'open_requests': deepcopy(self.state.get('open_requests') or []), 'snapshot_required': False}
        raise AssertionError(f'Unexpected native request: {method}')


TARGET = {'home': 'profile-a', 'session': 'session-a', 'pid': 123, 'sessionFile': None}


class Identity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.python = self.root / 'python'
        self.python.write_text('native interpreter fixture')
        self.marker = self.root / 'active-session'
        self.marker.write_text(json.dumps({'session_id': 'session-a'}))
        self.marker.chmod(0o600)
        (self.root / 'profile-a').mkdir()
        self.env = {'HERMES_HOME': str(self.root / 'profile-a'), 'HERMES_TUI_ACTIVE_SESSION_FILE': str(self.marker),
                    'HERMES_PYTHON_SRC_ROOT': str(self.root), 'HERMES_PYTHON': str(self.python)}

    def target(self, payload, envs=None):
        with patch.object(hermes, '_process_environments', return_value=envs or [self.env]):
            return hermes._target(payload, 'discover')

    def test_exact_foreground_marker_selects_profile(self):
        target = self.target({'pid': 123, 'cwd': '/irrelevant'})
        self.assertEqual(target['session'], 'session-a')
        self.assertEqual(target['home'], str(self.root / 'profile-a'))
        self.assertEqual(target['pid'], 123)
        self.assertEqual(target['pythonPath'], self.env.get('PYTHONPATH', ''))

    def test_owning_dependency_pythonpath_is_preserved_for_the_transport(self):
        self.env['PYTHONPATH'] = os.pathsep.join([str(self.root), str(self.root / 'selected' / 'site-packages')])
        target = self.target({'pid': 123})
        self.assertEqual(target['pythonPath'], self.env['PYTHONPATH'])
        target.update(python=str(self.python), source=str(self.root))
        response = SimpleNamespace(returncode=0, stdout=json.dumps({'ok': True, 'data': {'sessionId': 'session-a'}}))
        with patch.object(hermes, '_target', return_value=target), patch.object(hermes.subprocess, 'run', return_value=response) as run:
            hermes.handle('state', {'pid': 123})
        self.assertEqual(run.call_args.kwargs['env']['PYTHONPATH'], self.env['PYTHONPATH'])

    def test_same_marker_shared_by_descendants_is_not_ambiguous(self):
        self.assertEqual(self.target({'pid': 123}, [self.env, self.env.copy()])['session'], 'session-a')

    def test_other_profile_and_changed_session_are_refused(self):
        for changes in ({'hermesHome': str(self.root / 'profile-b')}, {'sessionId': 'session-b'}):
            with self.subTest(changes=changes), self.assertRaises(NativeError):
                self.target({'pid': 123, **changes})

    def test_multiple_native_descendants_are_ambiguous(self):
        other = {**self.env, 'HERMES_TUI_ACTIVE_SESSION_FILE': str(self.root / 'other-session')}
        with self.assertRaisesRegex(NativeError, 'Ambiguous'):
            self.target({'pid': 123}, [self.env, other])

    def test_no_cwd_or_latest_session_fallback(self):
        with patch.object(hermes, '_process_environments', return_value=[]):
            with self.assertRaises(NativeError):
                hermes._target({'cwd': '/workspace'}, 'discover')
        with self.assertRaises(NativeError):
            self.target({'pid': 123, 'sessionId': 'latest'})

    def test_marker_symlinks_and_world_readable_files_refused(self):
        self.marker.chmod(0o644)
        with self.assertRaises(NativeError):
            self.target({'pid': 123})
        self.marker.chmod(0o600)
        link = self.root / 'link'
        link.symlink_to(self.marker)
        with self.assertRaises(OSError):
            hermes._private_text(link)


    def test_unopened_terminal_cannot_be_mistaken_for_ready_attachment(self):
        self.marker.write_text('')
        with self.assertRaisesRegex(NativeError, 'has not opened'):
            self.target({'pid': 123, 'sessionId': 'session-a'})
        with patch.object(hermes, '_process_environments', return_value=[]):
            with self.assertRaisesRegex(NativeError, 'has not published'):
                hermes._target({'pid': 123, 'sessionId': 'session-a', 'hermesHome': self.env['HERMES_HOME']}, 'discover')


    def test_native_json_marker_is_decoded_before_session_identity_checks(self):
        session = 'local-' + 'a' * 64
        self.marker.write_text(json.dumps({'session_id': session}))
        target = self.target({'pid': 123, 'sessionId': session})
        self.assertEqual(target['session'], session)
        self.marker.write_text(json.dumps({'session_id': 'local-' + 'b' * 64}))
        with self.assertRaisesRegex(NativeError, 'terminal switched sessions'):
            self.target({'pid': 123, 'sessionId': session})

    def test_invalid_marker_shapes_are_never_coerced_into_native_session_ids(self):
        for value in ('session-a', {}, [], {'session_id': 123}, {'session_id': True}):
            self.marker.write_text(json.dumps(value))
            with self.subTest(value=value), self.assertRaises(NativeError):
                self.target({'pid': 123})
        self.marker.write_text('session-a')
        with self.assertRaisesRegex(NativeError, 'valid native JSON'):
            self.target({'pid': 123})


class NormalizedHistory(unittest.TestCase):
    def test_raw_tool_calls_images_and_results_preserve_associations(self):
        rows = [{'role': 'user', 'row_id': 1, 'content': [{'type': 'text', 'text': 'inspect'},
                 {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aGVsbG8='}}]},
                {'role': 'assistant', 'row_id': 2, 'content': 'working', 'model': 'native-model',
                 'tool_calls': [{'id': 'call-a', 'function': {'name': 'terminal', 'arguments': '{"command":"pwd"}'}}]},
                {'role': 'tool', 'row_id': 3, 'tool_call_id': 'call-a', 'name': 'terminal', 'content': 'workspace'},
                {'role': 'system', 'content': 'private rules'},
                {'role': 'user', 'row_id': 4, 'display_kind': 'hidden', 'content': 'internal'}]
        messages = hermes.normalize_history(rows, 'session-a')
        self.assertEqual([row['id'] for row in messages], ['session-a:1', 'session-a:2', 'session-a:3'])
        self.assertEqual(messages[0]['content'][1], {'type': 'image', 'data': 'aGVsbG8=', 'mimeType': 'image/png'})
        self.assertEqual(messages[1]['content'][1], {'type': 'toolCall', 'id': 'call-a', 'name': 'terminal', 'arguments': {'command': 'pwd'}})
        self.assertEqual(messages[2]['role'], 'toolResult')
        self.assertEqual(messages[2]['toolCallId'], 'call-a')
        self.assertEqual(messages[2]['toolName'], 'terminal')
        self.assertEqual(messages, hermes.normalize_history(rows, 'session-a'))

    def test_duplicate_unpersisted_messages_remain_distinct(self):
        rows = [{'role': 'assistant', 'content': 'same'}] * 2
        messages = hermes.normalize_history(rows, 'session-a')
        self.assertNotEqual(messages[0]['id'], messages[1]['id'])
        self.assertNotEqual(messages[0]['id'], hermes.normalize_history(rows, 'session-b')[0]['id'])

    def test_image_batch_rejected_before_staging(self):
        good = {'data': base64.b64encode(b'\x89PNG\r\n\x1a\nbytes').decode(), 'mimeType': 'image/png'}
        with self.assertRaises(NativeError):
            hermes._prepare_images({'images': [good, {'data': '%%%'}]})
        with self.assertRaises(NativeError):
            hermes._prepare_images({'images': [{**good, 'mimeType': 'image/jpeg'}]})

    def test_native_file_images_and_nonimage_prompt_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'picture.png'
            image.write_bytes(b'\x89PNG\r\n\x1a\nbytes')
            text = Path(temporary) / 'notes.txt'
            text.write_text('native path remains in prompt')
            attachments = hermes._prepare_images({'files': [str(image), str(text)]})
            self.assertEqual(len(attachments), 1)
            self.assertEqual(attachments[0]['filename'], 'picture.png')


class Routing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        cache = patch.object(hermes, 'state_dir', return_value=Path(temporary.name))
        cache.start()
        self.addCleanup(cache.stop)

    async def invoke(self, operation, payload=None, client=None, owner=None):
        self.client = client or Client()
        def read_store(target, session, history=False):
            self.client.native_reads.append(history)
            state = self.client.state
            return {'session_id': session, 'authority_epoch': state['authority_epoch'],
                    'revision': state['revision'], 'execution_generation': state['execution_generation'],
                    'message_count': state['message_count'], 'pending': deepcopy(state.get('pending') or []),
                    'history_revision': state.get('history_revision', f"{state['revision']}:{state['message_count']}"),
                    **({'messages': deepcopy(state.get('messages') or [])} if history else {})}
        with patch.object(hermes, '_read_store', side_effect=read_store):
            return await hermes._dispatch(self.client, owner or endpoint(), operation, payload or {}, dict(TARGET))

    async def test_state_binds_exact_session_and_does_not_claim_settlement(self):
        result = await self.invoke('state')
        self.assertEqual(result['sessionId'], 'session-a')
        self.assertIsNone(result['settled'])
        self.assertEqual(self.client.calls[1], ('session.active_list', {'current_session_id': 'session-a'}))
        self.assertFalse(any(name in {'session.resume', 'session.detach'} for name, _ in self.client.calls))
        self.assertFalse(any(name in {'prompt.submit', 'session.interrupt', 'approval.respond'} for name, _ in self.client.calls))

    async def test_owner_or_profile_change_refused_before_controls(self):
        client = Client(owner=endpoint('profile-b'))
        with self.assertRaisesRegex(NativeError, 'identity changed'):
            await self.invoke('send', {'text': 'never send'}, client)
        self.assertEqual([name for name, _ in client.calls], ['runtime.describe'])

    async def test_multiplexed_profile_binds_its_own_epoch_not_the_control_hosts(self):
        transport = endpoint(epoch=16, control_home='control-host')
        authority = endpoint(epoch=14)
        prompt = {'kind': 'approval', 'prompt_id': 'prompt-a', 'execution_generation': 9,
                  'command': 'native tool', 'choices': ['once', 'deny']}
        client = Client(snapshot(authority_epoch=14, prompts=[prompt]), owner=authority,
                        replies={'approval.respond': {'status': 'resolved', 'prompt_id': 'prompt-a'}})
        state = await self.invoke('state', client=client, owner=transport)
        card = state['pendingRequests'][0]
        answer = await self.invoke('answer', {'requestId': card['id'], 'answer': 'once'}, client, transport)
        self.assertEqual(answer['status'], 'resolved')
        self.assertIn(':14:', state['revision'])
        self.assertIn(('approval.respond', {'session_id': 'session-a', 'execution_generation': 9,
                                           'prompt_id': 'prompt-a', 'choice': 'once'}), client.calls)
        host_card = hermes._request_id('session-a', snapshot(authority_epoch=16), prompt, transport)
        calls_before = sum(name == 'approval.respond' for name, _ in client.calls)
        with self.assertRaisesRegex(NativeError, 'Stale Hermes approval'):
            await self.invoke('answer', {'requestId': host_card, 'answer': 'once'}, client, transport)
        self.assertEqual(sum(name == 'approval.respond' for name, _ in client.calls), calls_before)

    async def test_standalone_and_same_home_control_epoch_mismatches_are_refused(self):
        for control_home in (None, 'profile-a'):
            transport = endpoint(epoch=16, control_home=control_home)
            client = Client(snapshot(authority_epoch=14), owner=endpoint(epoch=14))
            with self.subTest(control_home=control_home), self.assertRaisesRegex(NativeError, 'authority epoch changed'):
                await self.invoke('state', client=client, owner=transport)
            self.assertEqual([name for name, _ in client.calls], ['runtime.describe'])

    async def test_multiplexing_never_relaxes_profile_or_instance_fences(self):
        transport = endpoint(epoch=16, control_home='control-host')
        for authority in (endpoint(profile='profile-b', epoch=14), endpoint(instance='other-owner', epoch=14)):
            client = Client(owner=authority)
            with self.assertRaisesRegex(NativeError, 'identity changed'):
                await self.invoke('state', client=client, owner=transport)
            self.assertEqual([name for name, _ in client.calls], ['runtime.describe'])

    async def test_multiplexed_profile_epoch_change_fences_previous_approval_cards(self):
        transport = endpoint(epoch=16, control_home='control-host')
        prompt = {'kind': 'approval', 'prompt_id': 'prompt-a', 'execution_generation': 9, 'choices': ['once', 'deny']}
        request = hermes._request_id('session-a', snapshot(authority_epoch=14), prompt, endpoint(epoch=14))
        client = Client(snapshot(authority_epoch=15, prompts=[prompt]), owner=endpoint(epoch=15))
        with self.assertRaisesRegex(NativeError, 'Stale Hermes approval'):
            await self.invoke('answer', {'requestId': request, 'answer': 'once'}, client, transport)
        self.assertFalse(any(name == 'approval.respond' for name, _ in client.calls))

    async def test_send_reports_only_native_admission_and_never_replays(self):
        client = Client(replies={'prompt.submit': {'admission_id': 'admit-a', 'status': 'queued'}})
        result = await self.invoke('send', {'text': 'native work', 'delivery': 'followUp', 'requestId': 'input-a'}, client)
        self.assertTrue(result['admitted'])
        submit = next(params for name, params in client.calls if name == 'prompt.submit')
        self.assertEqual(submit, {'session_id': 'session-a', 'text': 'native work', 'input_id': 'input-a', 'queued': True})
        failed = Client(replies={'prompt.submit': RuntimeError('unknown outcome')})
        with self.assertRaisesRegex(RuntimeError, 'unknown outcome'):
            await self.invoke('send', {'text': 'never replay'}, failed)
        self.assertEqual(sum(name == 'prompt.submit' for name, _ in failed.calls), 1)
        self.assertEqual(failed.calls[-1][0], 'session.detach')

    async def test_unknown_native_outcome_is_not_claimed_admitted(self):
        client = Client(replies={'prompt.submit': {'admission_id': 'admit-a', 'status': 'unknown'}})
        self.assertFalse((await self.invoke('send', {'text': 'work'}, client))['admitted'])

    async def test_invalid_image_batch_never_stages_or_submits(self):
        client = Client()
        with self.assertRaises(NativeError):
            await self.invoke('send', {'text': 'work', 'images': [{'data': '%%%'}]}, client)
        self.assertFalse(any(name in {'image.attach_bytes', 'prompt.submit'} for name, _ in client.calls))

    async def test_interrupt_is_execution_generation_fenced(self):
        client = Client(replies={'session.interrupt': {'execution_state': 'idle'}})
        await self.invoke('interrupt', client=client)
        self.assertIn(('session.interrupt', {'session_id': 'session-a', 'execution_generation': 9}), client.calls)

    async def test_approval_answers_use_native_first_answer_wins(self):
        prompt = {'kind': 'approval', 'prompt_id': 'prompt-a', 'execution_generation': 9,
                  'command': 'native tool', 'choices': ['once', 'deny']}
        state = snapshot(prompts=[prompt])
        card = hermes._state(state, endpoint(), TARGET)['pendingRequests'][0]
        self.assertEqual(card['kind'], 'permission')
        self.assertEqual([choice['value'] for choice in card['choices']], ['once', 'deny'])
        for outcome in ('resolved', 'already_resolved'):
            client = Client(state, {'approval.respond': {'status': outcome, 'prompt_id': 'prompt-a'}})
            result = await self.invoke('answer', {'requestId': card['id'], 'answer': 'once'}, client)
            self.assertEqual(result['status'], outcome)
            self.assertIn(('approval.respond', {'session_id': 'session-a', 'execution_generation': 9,
                                               'prompt_id': 'prompt-a', 'choice': 'once'}), client.calls)

    async def test_stale_profile_session_owner_epoch_and_generation_answers_refused(self):
        prompt = {'kind': 'approval', 'prompt_id': 'prompt-a', 'execution_generation': 9, 'choices': ['once', 'deny']}
        for owner, state in ((endpoint('profile-b'), snapshot()), (endpoint(instance='old-owner'), snapshot()),
                             (endpoint(), snapshot(session_id='session-b')), (endpoint(), snapshot(authority_epoch=6)),
                             (endpoint(), snapshot(execution_generation=8))):
            request = hermes._request_id(state['session_id'], state, prompt, owner)
            if state['execution_generation'] == 8:
                changed_prompt = {**prompt, 'execution_generation': 8}
                request = hermes._request_id(state['session_id'], state, changed_prompt, owner)
            client = Client(snapshot(prompts=[prompt]))
            with self.subTest(owner=owner, state=state), self.assertRaises(NativeError):
                await self.invoke('answer', {'requestId': request, 'answer': 'once'}, client)
            self.assertFalse(any(name == 'approval.respond' for name, _ in client.calls))

    async def test_detach_preserves_held_dialog_and_never_answers_it(self):
        prompt = {'kind': 'clarify', 'prompt_id': 'q-a', 'execution_generation': 9,
                  'question': 'Choose', 'choices': ['one', 'two']}
        client = Client(snapshot(prompts=[prompt], open_requests=[{'id': 'secret-a', 'method': 'secret', 'params': {}}]))
        state = await self.invoke('state', client=client)
        self.assertTrue(state['pendingRequests'][0]['other'])
        self.assertFalse(state['pendingRequests'][1]['answerable'])
        self.assertFalse(any(name in {'clarify.respond', 'approval.respond', 'session.interrupt'} for name, _ in client.calls))
        self.assertNotIn('end_reason', client.calls[-1][1])

    async def test_create_uses_canonical_policy_without_disabling_defaults(self):
        client = Client()
        result = await self.invoke('create', {'model': 'provider-a:model-a', 'effort': 'high', 'requestId': 'fresh-a'}, client)
        self.assertTrue(result['created'])
        create = next(params for name, params in client.calls if name == 'session.create')
        self.assertEqual(create, {'source': 'tui', 'request_id': 'fresh-a', 'model': 'model-a', 'provider': 'provider-a', 'reasoning': 'high'})
        self.assertNotIn('session.resume', [name for name, _ in client.calls])

    async def test_cache_pages_active_branch_and_skips_unchanged_native_history(self):
        rows = [{'role': 'user' if i % 2 else 'assistant', 'row_id': i, 'content': f'row {i}'} for i in range(1, 7)]
        client = Client(snapshot(messages=rows, message_count=6))
        with tempfile.TemporaryDirectory() as temporary, patch.object(hermes, 'state_dir', return_value=Path(temporary)):
            newest = await self.invoke('history', {'limit': 2}, client)
            self.assertEqual([row['id'] for row in newest['messages']], ['session-a:5', 'session-a:6'])
            self.assertTrue(newest['hasEarlier'])
            self.assertTrue(newest['reset'])
            before = await self.invoke('history', {'limit': 2, 'before': 'session-a:5'}, client)
            self.assertEqual([row['id'] for row in before['messages']], ['session-a:3', 'session-a:4'])
            unchanged = await self.invoke('history', {'cursor': newest['cursor']}, client)
            self.assertEqual(unchanged['messages'], [])
            self.assertEqual(client.native_reads.count(True), 1)
            self.assertFalse(any(name in {'session.resume', 'session.detach'} for name, _ in client.calls))
            client.state.update(revision=4, last_sequence=21, messages=rows[:2], message_count=2)
            rewound = await self.invoke('history', {'cursor': newest['cursor']}, client)
            self.assertTrue(rewound['reset'])
            self.assertEqual([row['id'] for row in rewound['messages']], ['session-a:1', 'session-a:2'])

    async def test_streaming_event_fences_ignore_old_generations_and_other_sessions(self):
        events = [{'type': 'message.delta', 'session_id': session, 'authority_epoch': 7,
                   'execution_generation': generation, 'payload': {'text': text}}
                  for session, generation, text in [('session-b', 9, 'wrong session'), ('session-a', 8, 'stale'), ('session-a', 9, 'native output')]]
        client = Client(snapshot(running=True), {'session.events.since': {'events': events, 'last_sequence': 20}})
        with tempfile.TemporaryDirectory() as temporary, patch.object(hermes, 'state_dir', return_value=Path(temporary)):
            result = await self.invoke('history', client=client)
        self.assertEqual(result['messages'][-1]['content'], 'native output')
        self.assertEqual(result['messages'][-1]['id'], 'session-a:stream:9:0')

    async def test_model_inventory_filters_credentials_and_qualifies_collisions(self):
        inventory = {'model': 'same', 'provider': 'one', 'providers': [
            {'slug': slug, 'name': slug, 'models': ['same'], 'api_key': 'never-export', 'api_url': 'private-endpoint'}
            for slug in ('one', 'two')]}
        client = Client(replies={'model.options': inventory})
        result = await self.invoke('models', client=client)
        self.assertEqual([row['id'] for row in result['models']], ['one:same', 'two:same'])
        self.assertNotIn('never-export', json.dumps(result))
        self.assertNotIn('private-endpoint', json.dumps(result))


    async def test_session_only_permissions_are_confirmed_without_global_scope(self):
        client = Client()

        def apply(params):
            client.state['info']['yolo'] = params['value'] == '1'
            return {'scope': 'session', 'value': params['value']}

        client.replies['config.set'] = apply
        state = await self.invoke('configure', {'perm': 'yolo'}, client)
        self.assertEqual(state['sessionId'], 'session-a')
        self.assertIn(('config.set', {'session_id': 'session-a', 'key': 'yolo', 'value': '1'}), client.calls)
        self.assertFalse(any('scope' in params for _, params in client.calls))

    async def test_model_mutation_is_revision_fenced_and_pending_is_not_blind_success(self):
        client = Client(replies={'session.mutate': {'operation': 'model', 'revision': 4},
                                 'model.options': {'model': 'model-a', 'provider': 'provider-a'}})
        with self.assertRaisesRegex(NativeError, 'not confirmed'):
            await self.invoke('configure', {'model': 'provider-b:model-b'}, client)
        mutation = next(params for name, params in client.calls if name == 'session.mutate')
        self.assertEqual(mutation['expected_revision'], 3)
        self.assertEqual(mutation['expected_generation'], 9)
        self.assertEqual(mutation['payload'], {'model': 'model-b', 'provider': 'provider-b'})
        self.assertFalse(any(name == 'config.set' for name, _ in client.calls))

    async def test_native_fork_returns_new_identity_without_taking_terminal_ownership(self):
        client = Client(replies={'session.mutate': {'branched_session_id': 'child-a'}})
        client.replies['session.resume'] = lambda params: snapshot(session_id=params['session_id'], subscription_id=params['session_id'] + '-view')
        result = await self.invoke('fork', client=client)
        self.assertEqual(result['sessionId'], 'child-a')
        self.assertIsNone(result['pid'])
        self.assertIsNone(result['sessionFile'])
        mutation = next(params for name, params in client.calls if name == 'session.mutate')
        self.assertEqual(mutation['operation'], 'branch')
        self.assertEqual(mutation['payload'], {})
        self.assertEqual({params['session_id'] for name, params in client.calls if name == 'session.detach'}, {'session-a', 'child-a'})

    async def test_close_owns_no_native_subscription_and_never_reopens_work(self):
        client = Client(replies={'session.detach': {'detached': False}})
        result = await self.invoke('close', client=client)
        self.assertFalse(result['detached'])
        self.assertEqual(sum(name == 'session.detach' for name, _ in client.calls), 0)
        self.assertFalse(any(name in {'session.interrupt', 'session.close'} for name, _ in client.calls))

    async def test_native_replay_gap_is_explicit_instead_of_fabricated_stream_text(self):
        client = Client(snapshot(running=True), {'session.events.since': {'snapshot_required': True, 'events': [], 'last_sequence': 20}})
        with tempfile.TemporaryDirectory() as temporary, patch.object(hermes, 'state_dir', return_value=Path(temporary)):
            result = await self.invoke('history', client=client)
        self.assertEqual(result['messages'], [])
        self.assertIn('streamingUnavailable', result)


    async def test_all_observation_operations_never_attach_or_detach(self):
        client = Client(replies={'model.options': {'model': 'model-a', 'provider': 'provider-a', 'providers': []}})
        for operation in ('state', 'discover', 'history', 'models', 'close'):
            await self.invoke(operation, client=client)
        self.assertFalse(any(method in {'session.resume', 'session.detach', 'session.close', 'session.mutate',
                                        'approval.respond', 'clarify.respond'} for method, _ in client.calls))

    async def test_user_closed_session_is_never_reopened_by_observation(self):
        client = Client(replies={'session.active_list': {'sessions': []}})
        for operation in ('state', 'discover', 'history', 'models'):
            with self.subTest(operation=operation), self.assertRaisesRegex(NativeError, 'no longer live'):
                await self.invoke(operation, client=client)
        self.assertFalse(client.native_reads)
        self.assertFalse(any(method in {'session.resume', 'session.detach', 'session.info'} for method, _ in client.calls))

    async def test_stream_watermark_does_not_reproject_unchanged_transcript(self):
        client = Client(snapshot(running=True), {'session.events.since': {
            'events': [{'type': 'message.delta', 'session_id': 'session-a', 'authority_epoch': 7,
                        'execution_generation': 9, 'payload': {'text': 'first'}}],
            'last_sequence': 20}})
        first = await self.invoke('history', client=client)
        client.state['last_sequence'] = 21
        client.replies['session.events.since'] = {
            'events': [{'type': 'message.delta', 'session_id': 'session-a', 'authority_epoch': 7,
                        'execution_generation': 9, 'payload': {'text': ' second'}}],
            'last_sequence': 21}
        second = await self.invoke('history', {'cursor': first['cursor']}, client)
        self.assertEqual(second['messages'][-1]['content'], 'first second')
        self.assertEqual(client.native_reads.count(True), 1)
        self.assertEqual(sum(method == 'session.info' for method, _ in client.calls), 2)


class ReadOnlyNativeStore(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.path = self.home / 'state.db'
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.executescript('''
                CREATE TABLE sessions(id TEXT PRIMARY KEY, runtime_revision INTEGER, runtime_generation INTEGER,
                    transcript_revision INTEGER, message_count INTEGER, parent_session_id TEXT, end_reason TEXT);
                CREATE TABLE state_meta(key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE runtime_epoch(singleton INTEGER PRIMARY KEY, epoch INTEGER);
                CREATE TABLE session_admissions(admission_id TEXT, target_session_id TEXT, status TEXT, generation INTEGER, seq INTEGER);
                INSERT INTO sessions VALUES ('session-a', 3, 9, 0, 0, NULL, NULL);
                INSERT INTO sessions VALUES ('segment-a', 0, 0, 5, 2, NULL, NULL);
                INSERT INTO runtime_epoch VALUES (1, 7);
            ''')
            connection.execute('INSERT INTO state_meta VALUES (?, ?)', (
                'gateway.local_policy.v1:session-a', json.dumps({'entry': {'session_id': 'segment-a'}})))
        self.readers = []
        case = self

        class Store:
            def __init__(self, db_path, read_only):
                case.assertTrue(read_only)
                case.assertEqual(db_path, case.path)
                self.connection = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
                self.connection.row_factory = sqlite3.Row
                self.closed = False
                self.queries = []
                self.projections = []
                self.connection.set_trace_callback(self.queries.append)
                case.readers.append(self)
                case.addCleanup(self.close)

            def close(self):
                if not self.closed:
                    self.connection.close()
                    self.closed = True

            @contextmanager
            def _read_ctx(self):
                yield self.connection

            def _read_one(self, sql, params=()):
                return self.connection.execute(sql, params).fetchone()

            def get_session(self, session):
                row = self._read_one('SELECT * FROM sessions WHERE id=?', (session,))
                return dict(row) if row else None

            def get_meta(self, key):
                row = self._read_one('SELECT value FROM state_meta WHERE key=?', (key,))
                return row[0] if row else None

            def _resume_lineage_ids(self, physical):
                case.assertEqual(physical, 'segment-a')
                return [physical]

            def get_messages_as_conversation(self, physical, **options):
                self.projections.append((physical, options))
                return []

        self.modules = {
            'hermes_state': SimpleNamespace(SessionDB=Store),
            'hermes_state_local': SimpleNamespace(POLICY_PREFIX='gateway.local_policy.v1:'),
            'hermes_state_runtime': SimpleNamespace(list_session_admissions=lambda store, session_id: [
                dict(row) for row in store.connection.execute(
                    'SELECT * FROM session_admissions WHERE target_session_id=?', (session_id,))]),
            'gateway': SimpleNamespace(__path__=[]),
            'gateway.session_local_recovery': SimpleNamespace(_merge_live_in_stored_order=lambda display, live: display),
        }

    def read(self, history=False):
        with patch.dict(sys.modules, self.modules):
            return hermes._read_store({'home': str(self.home)}, 'session-a', history)

    def test_metadata_watermark_is_session_scoped_without_transcript_queries(self):
        before = self.read()
        self.assertTrue(self.readers[-1].closed)
        self.assertEqual(self.readers[-1].projections, [])
        self.assertFalse(any('FROM MESSAGES' in sql.upper() or 'COUNT(' in sql.upper()
                             for sql in self.readers[-1].queries))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO sessions VALUES ('unrelated', 999, 999, 999, 1000000, NULL, NULL)")
        self.assertEqual(before['history_revision'], self.read()['history_revision'])
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE sessions SET transcript_revision=6 WHERE id='segment-a'")
        self.assertNotEqual(before['history_revision'], self.read()['history_revision'])

    def test_changed_history_uses_real_native_projection_interface_read_only(self):
        result = self.read(history=True)
        self.assertEqual(result['session_id'], 'session-a')
        self.assertEqual(result['authority_epoch'], 7)
        self.assertEqual(self.readers[-1].projections, [
            ('segment-a', {'include_row_ids': True}),
            ('segment-a', {'include_ancestors': True, 'include_compacted': True, 'include_row_ids': True})])
        self.assertTrue(self.readers[-1].closed)
        self.assertIn('ROLLBACK', self.readers[-1].queries)


class NativeDependencyHandoff(unittest.TestCase):
    def setUp(self):
        path = Path(hermes.__file__).with_name('hermes-attach.py')
        spec = importlib.util.spec_from_file_location('native_hermes_attach_test', path)
        self.helper = importlib.util.module_from_spec(spec)
        with patch.object(sys, 'path', list(sys.path)), patch.object(sys, 'dont_write_bytecode', True):
            spec.loader.exec_module(self.helper)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.package = self.source / 'selected' / 'site-packages'
        self.package.mkdir(parents=True)

    def test_owner_pinned_dependencies_never_consult_a_new_pm_selection(self):
        def forbid_selection(source):
            raise AssertionError('an existing owner must keep its pinned dependencies')
        modules = {'pm': SimpleNamespace(__path__=[]),
                   'pm.environments': SimpleNamespace(committed_venv=forbid_selection, site_packages=None)}
        target = {'source': str(self.source), 'pythonPath': os.pathsep.join([str(self.source), str(self.package)])}
        with patch.dict(sys.modules, modules), patch.object(sys, 'path', list(sys.path)), patch.object(self.helper.site, 'addsitedir') as add:
            self.helper._native_imports(target)
            add.assert_called_once_with(str(self.package))
            self.assertEqual(sys.path[:2], [str(self.source), str(self.package)])

    def test_cold_helper_only_reads_the_existing_committed_dependency_tree(self):
        calls = []
        environment = self.package.parent
        def committed(source):
            calls.append(('committed', source))
            return environment
        def packages(selected):
            calls.append(('site_packages', selected))
            return self.package
        modules = {'pm': SimpleNamespace(__path__=[]),
                   'pm.environments': SimpleNamespace(committed_venv=committed, site_packages=packages)}
        before = set(sys.modules)
        with patch.dict(sys.modules, modules), patch.object(sys, 'path', list(sys.path)), patch.object(self.helper.site, 'addsitedir') as add:
            self.helper._native_imports({'source': str(self.source)})
            add.assert_called_once_with(str(self.package))
        self.assertEqual(calls, [('committed', self.source), ('site_packages', environment)])
        self.assertNotIn('hermes_bootstrap', set(sys.modules) - before)

    def test_missing_pinned_dependencies_are_explicit_not_installed_or_replaced(self):
        target = {'source': str(self.source), 'pythonPath': str(self.source / 'missing' / 'site-packages')}
        with patch.object(sys, 'path', list(sys.path)), patch.object(self.helper.site, 'addsitedir') as add:
            with self.assertRaisesRegex(RuntimeError, 'no install or fallback'):
                self.helper._native_imports(target)
            add.assert_not_called()


class NativeBootstrapBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_only_explicit_create_can_ensure_a_native_owner(self):
        calls = []
        absent = SimpleNamespace(state='absent', reason_code='not_running')
        runtime = SimpleNamespace(
            attachable=lambda receipt: False,
            discover_gateway_endpoint=lambda home: calls.append(('discover', home)) or absent,
            ensure_gateway_runtime=lambda home, **options: calls.append(('ensure', home, options)) or absent)
        modules = {'hermes_cli': SimpleNamespace(__path__=[]),
                   'hermes_cli.gateway_runtime': runtime,
                   'hermes_cli.gateway_client': SimpleNamespace(connect_gateway=None),
                   'hermes_constants': SimpleNamespace(VALID_REASONING_EFFORTS=('low', 'high'))}
        with patch.dict(sys.modules, modules):
            with self.assertRaises(NativeError):
                await hermes._execute('state', {}, TARGET)
            self.assertEqual(calls, [('discover', Path('profile-a'))])
            calls.clear()
            with self.assertRaises(NativeError):
                await hermes._execute('create', {}, TARGET)
            self.assertEqual(calls, [('ensure', Path('profile-a'), {'idle_exit': True})])


if __name__ == '__main__':
    unittest.main()
