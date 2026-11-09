"""OMO protocol/identity tests: only deterministic local transports, never a model."""
import home_guard  # noqa: F401
import base64
import contextlib
import hashlib
import hmac
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from shepherdr_native import launch, omo
from shepherdr_native.common import NativeError


class Wire:
    def __init__(self, handler):
        self.handler = handler
        self.sent = []
        self.chunks = []
        self.closed = False

    def settimeout(self, timeout):
        pass

    def connect(self, endpoint):
        self.endpoint = endpoint

    def sendall(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        replies = self.handler(frame)
        for reply in replies:
            raw = (json.dumps(reply, ensure_ascii=False) + "\r\n").encode()
            # Every UTF-8 character and LF may straddle a transport read.
            self.chunks.extend(raw[index:index + 7] for index in range(0, len(raw), 7))

    def recv(self, size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        self.closed = True


def response(frame, data=None, **fields):
    if frame["type"] == "attach_session":
        data = {**(data or {}), "sessionId": frame["sessionId"], "attached": True}
    return {"type": "response", "id": frame["id"], "command": frame["type"],
            "sessionId": frame.get("sessionId"), "success": True,
            "data": data or {}, **fields}


def transport(handler, route="rpc-1"):
    rpc = object.__new__(omo.Rpc)
    rpc.socket = Wire(handler)
    rpc.buffer = bytearray()
    rpc.events = []
    rpc.route = route
    rpc.timeout = 1
    rpc.attached = False
    rpc.info = {"instanceId": "host-one", "mode": "multi", "capabilities": ["attach_session", "release_attachment", "durable_client_message_id", "entries_tail", "ui_answer_ledger"]}
    return rpc


class Framing(unittest.TestCase):
    def test_tui_single_session_untagged_replies_and_events_are_bound_locally(self):
        rpc = transport(lambda frame: [
            {"type": "message_start", "message": {"role": "assistant", "content": "partial"}},
            response(frame, {"sessionId": "durable"}, sessionId=None),
        ], route="durable")
        rpc.info["mode"] = "tui"
        self.assertEqual(rpc.scoped("get_state")["sessionId"], "durable")
        self.assertEqual(rpc.events[0]["sessionId"], "durable")

    def test_shared_host_never_accepts_an_untagged_routed_reply(self):
        rpc = transport(lambda frame: [response(frame, sessionId=None)])
        with self.assertRaisesRegex(NativeError, "routing identity"):
            rpc.scoped("get_state")

    def test_fragmented_unicode_crlf_and_other_client_records(self):
        def replies(frame):
            return [{"type": "message_start", "sessionId": "rpc-other", "message": {"content": "foreign"}},
                    {"type": "message_start", "sessionId": "rpc-1", "message": {"content": "one\u2028two\u2029three"}},
                    response(frame, {"value": "résumé\u2028next"})]
        rpc = transport(replies)
        self.assertEqual(rpc.scoped("get_state")["value"], "résumé\u2028next")
        self.assertEqual(len(rpc.events), 1)
        self.assertEqual(rpc.events[0]["sessionId"], "rpc-1")
        self.assertEqual(rpc.socket.sent[0]["sessionId"], "rpc-1")

    def test_mismatched_response_session_is_refused(self):
        rpc = transport(lambda frame: [response(frame, sessionId="rpc-other")])
        with self.assertRaisesRegex(NativeError, "routing identity"):
            rpc.scoped("prompt", message="never replay")
        self.assertEqual(len(rpc.socket.sent), 1)

    def test_unknown_send_outcome_is_not_replayed(self):
        rpc = transport(lambda frame: [])
        with self.assertRaisesRegex(NativeError, "outcome is unknown"):
            rpc.scoped("prompt", message="once")
        self.assertEqual(len(rpc.socket.sent), 1)

    def test_overflow_is_not_success(self):
        rpc = transport(lambda frame: [{"type": "overflow"}, response(frame)])
        with self.assertRaisesRegex(NativeError, "output was lost"):
            rpc.scoped("prompt", message="once")
        self.assertEqual(len(rpc.socket.sent), 1)

    def test_native_refusal_keeps_error_code(self):
        rpc = transport(lambda frame: [response(frame, success=False, errorCode="session_output_overflow", error="overflow")])
        with self.assertRaises(omo.RpcError) as caught:
            rpc.scoped("get_state")
        self.assertEqual(caught.exception.code, "session_output_overflow")

    def test_attach_is_live_identity_fenced_and_release_is_not_close(self):
        rpc = transport(lambda frame: [response(frame)])
        row = {"sessionId": "rpc-1", "durableSessionId": "durable", "sessionPath": "/session.jsonl"}
        rpc.attach(row)
        rpc.close()
        calls = rpc.socket.sent
        self.assertEqual(calls[1]["type"], "attach_session")
        self.assertEqual(calls[1]["durableSessionId"], "durable")
        self.assertEqual(calls[1]["expectedHostInstance"], "host-one")
        self.assertEqual(calls[-1]["type"], "release_attachment")
        self.assertNotIn("close_session", [call["type"] for call in calls])
        self.assertNotIn("resume_session", [call["type"] for call in calls])

    def test_legacy_host_cannot_open_parked_path_as_attach(self):
        rpc = transport(lambda frame: [response(frame)])
        rpc.info["capabilities"] = ["expected_host_instance"]
        with self.assertRaisesRegex(NativeError, "identity-fenced"):
            rpc.attach({"sessionId": "rpc-1", "durableSessionId": "durable", "sessionPath": "/parked.jsonl"})
        self.assertNotIn("open_session", [call["type"] for call in rpc.socket.sent])

    def test_challenge_proof_is_verified_and_key_never_transmitted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            keys = root / "rpc" / "keys"
            keys.mkdir(parents=True, mode=0o700)
            keys.chmod(0o700)
            endpoint = str(root / "host.sock")
            key, launch, server_nonce = "ab" * 32, "launch-one", "cd" * 32
            record = keys / launch
            record.write_text(json.dumps({"version": 1, "launch_id": launch, "key": key, "endpoint": endpoint}))
            record.chmod(0o600)
            def replies(frame):
                if frame["type"] == "get_protocol_info":
                    nonce = frame["auth"]["client_nonce"]
                    proof = hmac.new(bytes.fromhex(key), f"senpi-rpc-server\n{launch}\n{nonce}\n{server_nonce}".encode(), hashlib.sha256).hexdigest()
                    return [response(frame, {"protocolVersion": 1, "mode": "multi", "capabilities": ["socket_auth"], "auth": {"launch_id": launch, "server_nonce": server_nonce, "server_proof": proof}})]
                self.assertEqual(frame["type"], "authenticate")
                return [response(frame)]
            wire = Wire(replies)
            real_private = omo._private
            def private(path, kind="file", owner_only=True):
                if str(path) != endpoint:
                    return real_private(path, kind, owner_only)
            with patch.object(omo.socket, "socket", return_value=wire), patch.object(omo, "_private", side_effect=private):
                rpc = omo.Rpc(endpoint, root)
                rpc.close()
            self.assertEqual([frame["type"] for frame in wire.sent], ["get_protocol_info", "authenticate"])
            self.assertNotIn(key, json.dumps(wire.sent))


    def test_tui_secret_is_raw_and_not_a_jsonl_record(self):
        with tempfile.TemporaryDirectory() as directory:
            endpoint = str(Path(directory) / "tui.sock")
            secret = bytes(range(32))
            credential = Path(endpoint + ".secret")
            credential.write_bytes(secret)
            credential.chmod(0o600)
            class TerminalWire(Wire):
                def sendall(self, data):
                    if not hasattr(self, "secret"):
                        self.secret = data
                        return
                    super().sendall(data)
            wire = TerminalWire(lambda frame: [response(frame, {"protocolVersion": 1, "mode": "tui", "capabilities": ["tui_control"]})])
            real_private = omo._private
            def private(path, kind="file", owner_only=True):
                if str(path) != endpoint:
                    return real_private(path, kind, owner_only)
            with patch.object(omo.socket, "socket", return_value=wire), patch.object(omo, "_private", side_effect=private):
                rpc = omo.Rpc(endpoint, Path(directory))
                rpc.close()
            self.assertEqual(wire.secret, secret)
            self.assertEqual(wire.sent[0]["type"], "get_protocol_info")


class Identity(unittest.TestCase):
    def test_actual_tui_list_shape_uses_session_id_as_durable_id(self):
        target = {"pid": 44, "sessionId": "durable", "sessionFile": "/fresh.jsonl"}
        rpc = transport(lambda frame: [response(frame, {"sessions": [
            {"sessionId": "durable", "sessionPath": "/fresh.jsonl", "surface": "tui", "attachments": 1}
        ]})])
        rpc.info["mode"] = "tui"
        with patch.object(omo, "_metadata", return_value=target), patch.object(omo, "_agent_dir", return_value=Path("/agent")), patch.object(omo, "_endpoints", return_value=[{"socket": "/native.sock", "instance": "host-one"}]), patch.object(omo, "Rpc", return_value=rpc):
            with omo._bound(target) as (bound, row, _target):
                self.assertEqual(row["durableSessionId"], "durable")
                self.assertEqual(bound.route, "durable")

    def test_actual_raw_ps_start_format_normalizes_whitespace_but_not_identity(self):
        process = {"pid": 44, "start": "123"}
        with patch.object(omo.subprocess, "run") as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "Mon Nov  9 22:07:38 2026\n"
            self.assertTrue(omo._same_start("Mon Nov  9 22:07:38 2026", process))
            self.assertTrue(omo._same_start("ps-lstart:Mon Nov 9 22:07:38 2026", process))
            self.assertFalse(omo._same_start("Mon Nov 9 22:07:37 2026", process))

    def test_tui_launch_loads_registration_bridge_without_disabling_native_features(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(launch.shutil, "which", return_value="/native/omo"), patch.object(launch, "state_dir", return_value=Path(directory)):
                prepared = launch.prepare("omo", {})
        argv = prepared["argv"]
        self.assertIn("--extension", argv)
        self.assertEqual(argv[argv.index("--extension") + 1], str(ROOT / "bin" / "shepherdr_native" / "omo-extension.ts"))
        self.assertIn("--session", argv)
        self.assertNotIn("--no-tools", argv)
        self.assertNotIn("--no-extensions", argv)

    def test_registration_failure_is_explicit_not_a_missing_session_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "omo" / "44.json"
            publication.parent.mkdir()
            publication.write_text(json.dumps({"pid": 44, "processStartTime": "123", "error": "native API unavailable"}))
            publication.chmod(0o600)
            with patch.object(omo, "state_dir", return_value=root), patch.object(omo, "_process", return_value={"start": "123"}):
                with self.assertRaisesRegex(NativeError, "native control registration failed"):
                    omo._metadata({"pid": 44})

    def test_cwd_or_terminal_alone_never_selects_session(self):
        for payload in ({"cwd": "/same"}, {"terminal": "t1"}):
            with self.assertRaisesRegex(NativeError, "exact native"):
                omo._metadata(payload)

    def test_private_publication_fences_pid_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publication = root / "omo" / "44.json"
            publication.parent.mkdir()
            publication.write_text(json.dumps({"pid": 44, "processStartTime": "old", "sessionId": "original"}))
            publication.chmod(0o600)
            with patch.object(omo, "state_dir", return_value=root), patch.object(omo, "_process", return_value={"start": "new"}):
                with self.assertRaisesRegex(NativeError, "process identity changed"):
                    omo._metadata({"pid": 44})

    def test_native_routing_handle_is_not_a_durable_identity(self):
        row = {"sessionId": "rpc-1", "durableSessionId": "durable-a", "sessionPath": "/a.jsonl", "status": "idle"}
        self.assertFalse(omo._matches(row, {"sessionId": "rpc-1"}))
        self.assertTrue(omo._matches(row, {"sessionId": "durable-a"}))
        self.assertFalse(omo._matches(row, {"sessionId": "durable-a", "sessionFile": "/b.jsonl"}))

    def test_explicit_endpoint_cannot_impersonate_a_pid_without_session_identity(self):
        with patch.object(omo, "_process", return_value={"start": "123"}), patch.object(omo.Path, "exists", return_value=False):
            with self.assertRaisesRegex(NativeError, "durable session identity"):
                omo._metadata({"pid": 44, "socket": "/some-other-tui.sock"})

    def test_native_registry_checks_process_start_and_endpoint_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = Path(directory)
            owner = agent / "rpc-host-daemon" / "endpoint"
            generation = owner / "generations" / "instance-one"
            generation.mkdir(parents=True)
            records = {
                owner / "endpoint.json": {"socket": "/exact.sock"},
                owner / "host.pid": {"instance_id": "instance-one", "generation_dir": "generations/instance-one"},
                generation / "host.pid": {"pid": 44, "processStartTime": "123", "instance_id": "instance-one", "socket": "/exact.sock"},
            }
            for path, value in records.items():
                path.write_text(json.dumps(value))
                path.chmod(0o600)
            with patch.object(omo, "_process", return_value={"pid": 44, "parent": 1, "start": "123"}):
                endpoints = omo._endpoints(agent, {"pid": 44})
                self.assertEqual(endpoints[0]["socket"], "/exact.sock")
                self.assertEqual(endpoints[0]["owner"], 44)
                fallback = omo._endpoints(agent, {"pid": 44, "sessionId": "durable", "socket": str(agent / "superseded.sock")})
                self.assertEqual(fallback[0]["socket"], "/exact.sock")
                self.assertEqual(omo._endpoints(agent, {"pid": 55, "sessionId": "durable", "socket": str(agent / "superseded.sock")}), [])
            with patch.object(omo, "_process", return_value={"pid": 44, "parent": 1, "start": "456"}):
                self.assertEqual(omo._endpoints(agent, {"pid": 44}), [])

    def test_native_linux_start_prefix_is_not_mistaken_for_a_reused_pid(self):
        process = {"pid": 44, "start": "123"}
        self.assertTrue(omo._same_start("linux-proc-start-ticks:123", process))
        self.assertTrue(omo._same_start("123", process))
        self.assertFalse(omo._same_start("linux-proc-start-ticks:456", process))

    def test_foreign_replacement_cannot_invalidate_this_clients_binding(self):
        rpc = transport(lambda frame: [response(frame, {"sessionId": "durable-a"})])
        rpc.events = [{"type": "session_replaced", "sessionId": "rpc-other", "durableSessionId": "foreign"}]
        self.assertEqual(omo._state(rpc, {"durableSessionId": "durable-a"})["sessionId"], "durable-a")

    def test_replacement_after_attachment_blocks_operation(self):
        rpc = transport(lambda frame: [response(frame, {"sessionId": "durable-b", "sessionFile": "/b.jsonl"})])
        with self.assertRaisesRegex(NativeError, "session changed"):
            omo._state(rpc, {"durableSessionId": "durable-a", "sessionPath": "/a.jsonl"})
        self.assertEqual([frame["type"] for frame in rpc.socket.sent], ["get_state"])

    def test_no_implicit_settlement_from_idle_or_unpersisted_snapshot(self):
        rpc = transport(lambda frame: [response(frame, {"entries": [], "leafId": None})])
        base = {"sessionId": "durable", "isStreaming": False}
        self.assertIsNone(omo._project_state(rpc, {}, {}, base)["settled"])
        state = {**base, "lifecycle": {"state": "done", "restoring": False, "persisted": False}}
        self.assertIsNone(omo._project_state(rpc, {}, {}, state)["settled"])
        state["lifecycle"]["persisted"] = True
        self.assertTrue(omo._project_state(rpc, {}, {}, state)["settled"])

    def test_revision_tracks_branch_not_observation_wall_clock(self):
        state = {"sessionId": "a", "lifecycle": {"seq": 3, "observedAt": "first"}}
        other = {**state, "lifecycle": {"seq": 3, "observedAt": "later"}}
        self.assertEqual(omo._revision(state, {"leafId": "x"}), omo._revision(other, {"leafId": "x"}))
        self.assertNotEqual(omo._revision(state, {"leafId": "x"}), omo._revision(state, {"leafId": "y"}))


class History(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patch = patch.object(omo, "state_dir", return_value=Path(self.directory.name))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.entries = [self.entry("u", None, "user", "question"),
                        self.entry("a", "u", "assistant", [{"type": "toolCall", "id": "call-1", "name": "read", "arguments": {"path": "x"}}]),
                        self.entry("t", "a", "toolResult", "contents", toolCallId="call-1", toolName="read")]
        self.leaf = "t"
        self.state = {"sessionId": "durable", "sessionFile": "/native/session.jsonl", "messageCount": 3}
        self.row = {"sessionId": "rpc-1", "durableSessionId": "durable", "sessionPath": "/native/session.jsonl"}
        self.rpc = transport(self.replies)

    @staticmethod
    def entry(identifier, parent, role, content, **metadata):
        return {"type": "message", "id": identifier, "parentId": parent, "message": {"role": role, "content": content, **metadata}}

    def replies(self, frame):
        if frame["type"] == "get_state":
            return [response(frame, self.state)]
        self.assertEqual(frame["type"], "get_entries")
        self.assertLessEqual(frame["limit"], 500)
        entries = self.entries
        if frame.get("since"):
            ids = [entry["id"] for entry in entries]
            entries = entries[ids.index(frame["since"]) + 1:]
        return [response(frame, {"entries": entries, "leafId": self.leaf, "hasMore": False})]

    def history(self, **payload):
        return omo._history(self.rpc, self.row, {}, payload, self.state)

    def test_stable_ids_tool_associations_and_bounded_earlier_page(self):
        newest = self.history(limit=2)
        self.assertEqual([message["id"] for message in newest["messages"]], ["a", "t"])
        self.assertEqual(newest["messages"][1]["toolCallId"], "call-1")
        self.assertTrue(newest["hasEarlier"])
        earlier = self.history(before="a", limit=2)
        self.assertEqual([message["id"] for message in earlier["messages"]], ["u"])

    def test_unchanged_history_uses_exclusive_native_cursor(self):
        first = self.history()
        second = self.history(cursor=first["cursor"])
        self.assertEqual(second["messages"], [])
        self.assertFalse(second["reset"])
        reads = [frame for frame in self.rpc.socket.sent if frame["type"] == "get_entries"]
        self.assertEqual(reads[-1]["since"], "t")

    def test_append_delta_then_branch_switch_resets_without_abandoned_messages(self):
        first = self.history()
        self.entries.append(self.entry("next", "t", "assistant", "next"))
        self.leaf = "next"
        added = self.history(cursor=first["cursor"])
        self.assertEqual([message["id"] for message in added["messages"]], ["next"])
        self.assertFalse(added["reset"])
        self.entries.append(self.entry("alternate", "u", "assistant", "other branch"))
        self.leaf = "alternate"
        switched = self.history(cursor=added["cursor"])
        self.assertTrue(switched["reset"])
        self.assertEqual([message["id"] for message in switched["messages"]], ["u", "alternate"])

    def test_compaction_is_not_flattened_and_hidden_custom_messages_stay_hidden(self):
        self.entries.extend([{"type": "compaction", "id": "c", "parentId": "t", "summary": "retained summary", "firstKeptEntryId": "a"},
                             {"type": "custom_message", "id": "hidden", "parentId": "c", "display": False, "content": "internal"}])
        self.leaf = "hidden"
        projected = self.history()["messages"]
        self.assertEqual([message["id"] for message in projected], ["u", "a", "t", "c"])
        self.assertEqual(projected[-1]["nativeType"], "compaction")

    def test_stream_snapshot_resets_and_has_stable_partial_identity(self):
        self.state["isStreaming"] = True
        self.rpc.events = [{"type": "message_start", "sessionId": "rpc-1", "message": {"role": "assistant", "timestamp": 123, "content": "partial"}}]
        first = self.history()
        self.assertTrue(first["reset"])
        self.assertEqual(first["messages"][-1]["id"], "live:durable:123")
        self.rpc.events[-1]["message"]["content"] = "more partial"
        second = self.history(cursor=first["cursor"])
        self.assertTrue(second["reset"])
        self.assertEqual(second["messages"][-1]["id"], "live:durable:123")
        self.assertEqual(second["messages"][-1]["content"], "more partial")

    def test_same_route_replacement_during_history_never_returns_other_session(self):
        self.state["sessionId"] = "replacement"
        with self.assertRaisesRegex(NativeError, "session changed"):
            self.history()

    def test_first_use_models_refuses_without_creating_an_engine(self):
        with patch.object(omo, "_bound") as bound:
            with self.assertRaisesRegex(NativeError, "exact live native session"):
                omo.handle("models", {})
        bound.assert_not_called()


class Controls(unittest.TestCase):
    def bind(self, rpc, state):
        @contextlib.contextmanager
        def bound(payload):
            yield rpc, {"sessionId": "rpc-1", "durableSessionId": state["sessionId"]}, {"pid": 123}
        return patch.object(omo, "_bound", side_effect=bound)

    def test_shared_model_configuration_never_changes_global_default(self):
        state = {"sessionId": "durable"}
        rpc = transport(lambda frame: [response(frame, state)])
        with self.bind(rpc, state), self.assertRaisesRegex(NativeError, "global defaults"):
            omo.handle("configure", {"model": "anthropic/native"})
        self.assertEqual([frame["type"] for frame in rpc.socket.sent], ["get_state"])
        self.assertFalse(omo._capabilities(rpc)["configure"])

    def test_state_does_not_attach_resume_or_clear_held_work(self):
        state = {"sessionId": "durable", "paused": True, "holds": [{"kind": "unattended"}]}
        rpc = transport(lambda frame: [response(frame, {"entries": [], "leafId": None} if frame["type"] == "get_entries" else state)])
        with self.bind(rpc, state):
            result = omo.handle("state", {})
        self.assertTrue(result["paused"])
        self.assertEqual([frame["type"] for frame in rpc.socket.sent], ["get_state", "get_entries"])

    def test_send_acknowledges_native_admission_once(self):
        state = {"sessionId": "durable"}
        rpc = transport(lambda frame: [response(frame, {"disposition": "started", "admission": {"clientMessageId": "human-1"}} if frame["type"] == "prompt" else state)])
        with self.bind(rpc, state):
            result = omo.handle("send", {"text": "human", "requestId": "human-1"})
        self.assertTrue(result["admitted"])
        prompts = [frame for frame in rpc.socket.sent if frame["type"] == "prompt"]
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["clientMessageId"], "human-1")

    def test_tui_prompt_does_not_assume_steer_receipt_fields_are_supported(self):
        state = {"sessionId": "durable"}
        rpc = transport(lambda frame: [response(frame, {"disposition": "started"} if frame["type"] == "prompt" else state)])
        rpc.info = {"mode": "tui", "instanceId": "host-one", "capabilities": [],
                    "commands": ["get_state", "set_client_info", "attach_session", "prompt"]}
        with self.bind(rpc, state):
            result = omo.handle("send", {"text": "human", "requestId": "human-1"})
        prompt = next(frame for frame in rpc.socket.sent if frame["type"] == "prompt")
        self.assertNotIn("clientMessageId", prompt)
        self.assertTrue(result["admitted"])
        self.assertEqual(result["requestId"], "human-1")
        self.assertFalse(result["nativeRequestIdSupported"])

    def test_tui_steer_keeps_documented_native_receipt_identity(self):
        state = {"sessionId": "durable"}
        rpc = transport(lambda frame: [response(frame, {"disposition": "queued"} if frame["type"] == "steer" else state)])
        rpc.info = {"mode": "tui", "instanceId": "host-one", "capabilities": [],
                    "commands": ["get_state", "set_client_info", "attach_session", "steer"]}
        with self.bind(rpc, state):
            result = omo.handle("send", {"text": "human", "delivery": "steer", "requestId": "human-1"})
        steer = next(frame for frame in rpc.socket.sent if frame["type"] == "steer")
        self.assertEqual(steer["clientMessageId"], "human-1")
        self.assertTrue(result["nativeRequestIdSupported"])

    def test_tui_staged_image_refusal_precedes_attachment_and_send(self):
        state = {"sessionId": "durable"}
        rpc = transport(lambda frame: [response(frame, state)])
        rpc.info["mode"] = "tui"
        with self.bind(rpc, state), self.assertRaisesRegex(NativeError, "does not accept images"):
            omo.handle("send", {"text": "image", "files": ["/staged/image.PNG"]})
        self.assertEqual([frame["type"] for frame in rpc.socket.sent], ["get_state"])

    def test_staged_images_are_native_base64_and_non_images_remain_text(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\nnative image bytes")
            result = omo._images({"files": [str(image), str(image.with_suffix('.txt'))]}, "multi")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(result[0]["data"]), b"\x89PNG\r\n\x1a\nnative image bytes")

    def test_staged_images_reject_symlinks_and_mime_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            link = Path(directory) / "link.png"
            link.symlink_to(source)
            with self.assertRaisesRegex(NativeError, "could not read"):
                omo._images({"files": [str(link)]}, "multi")
            source.write_bytes(b"not an image")
            with self.assertRaisesRegex(NativeError, "signature"):
                omo._images({"files": [str(source)]}, "multi")

    def test_all_staged_images_are_checked_before_any_native_admission(self):
        state = {"sessionId": "durable"}
        rpc = transport(lambda frame: [response(frame, state)])
        with tempfile.TemporaryDirectory() as directory:
            valid = Path(directory) / "valid.png"
            valid.write_bytes(b"\x89PNG\r\n\x1a\nimage")
            invalid = Path(directory) / "invalid.png"
            invalid.write_bytes(b"not an image")
            with self.bind(rpc, state), self.assertRaisesRegex(NativeError, "signature"):
                omo.handle("send", {"text": "images", "files": [str(valid), str(invalid)]})
        self.assertEqual([frame["type"] for frame in rpc.socket.sent], ["get_state"])

    def test_pending_requests_preserve_choice_values_and_secret_refusal(self):
        state = {"pendingDialogs": [{"id": "allow", "method": "select", "title": "Permission required: bash", "options": ["Allow once", "Deny"]},
                                     {"id": "secret", "method": "input", "secret": True}]}
        pending = omo._pending(state)
        self.assertEqual(pending[0]["id"], "allow")
        self.assertEqual(pending[0]["choices"][0]["value"], "Allow once")
        self.assertFalse(pending[1]["answerable"])
        rpc = transport(lambda frame: [response(frame)])
        with self.assertRaisesRegex(NativeError, "secret prompts"):
            omo._answer(rpc, state, {"requestId": "secret", "answer": "never sent"})
        self.assertEqual(rpc.socket.sent, [])

    def test_answer_uses_exact_native_ui_request_id_and_honors_first_answer_wins(self):
        state = {"pendingDialogs": [{"id": "request-one", "method": "select", "options": ["Allow", "Deny"]}]}
        answered = False
        def replies(frame):
            nonlocal answered
            if answered:
                return [response(frame, success=False, errorCode="question_already_resolved")]
            answered = True
            return [response(frame, {"disposition": "applied"})]
        rpc = transport(replies)
        self.assertTrue(omo._answer(rpc, state, {"requestId": "request-one", "answer": "Allow"})["answered"])
        self.assertEqual(rpc.socket.sent[0]["uiRequestId"], "request-one")
        self.assertNotEqual(rpc.socket.sent[0]["id"], "request-one")
        with self.assertRaises(omo.RpcError):
            omo._answer(rpc, state, {"requestId": "request-one", "answer": "Deny"})
        self.assertEqual(len(rpc.socket.sent), 2)

    def test_multi_question_answer_keeps_native_shape(self):
        questions = [{"id": "q1", "header": "Database", "question": "Which?", "options": [{"label": "SQLite"}], "multiSelect": False}]
        state = {"pendingQuestions": [{"id": "ui-q", "method": "question", "questions": questions}]}
        rpc = transport(lambda frame: [response(frame)])
        answer = {"answers": {"q1": {"selected": ["SQLite"], "text": "typed"}}, "comment": "comment"}
        omo._answer(rpc, state, {"requestId": "ui-q", "answer": answer})
        self.assertEqual(rpc.socket.sent[0]["answers"], answer["answers"])
        self.assertEqual(omo._pending(state)[0]["questions"], questions)


if __name__ == "__main__":
    unittest.main()
