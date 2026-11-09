"""OMP branch/identity invariants and real private IPC boundaries, without invoking a model."""
import home_guard  # noqa: F401 (first: isolate HOME before importing the backend)
import base64
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from shepherdr_native import omp
from shepherdr_native.common import NativeError, socket_request

EXTENSION = ROOT / "bin/shepherdr_native/omp-extension.ts"
BUN = shutil.which("bun")


class OmpCatalog(unittest.TestCase):
    def test_native_catalog_shape_is_provider_qualified_without_transport_secrets(self):
        result = omp._catalog_models({"models": [
            {"provider": "anthropic", "id": "model", "selector": "anthropic/model", "name": "Native model",
             "thinking": ["low", "high"], "headers": {"Authorization": "secret"}, "apiKey": "secret"},
            {"provider": "other", "id": "model", "name": "Other model", "thinking": []},
        ]})
        self.assertEqual([model["id"] for model in result["models"]], ["anthropic/model", "other/model"])
        self.assertEqual(result["models"][0]["efforts"], ["off", "low", "high"])
        self.assertNotIn("secret", json.dumps(result))
        self.assertIsNone(result["model"])
        self.assertIsNone(result["effort"])

    def test_invalid_native_catalog_is_not_a_fabricated_empty_catalog(self):
        for value in ({}, {"models": {}}, {"models": [None]}, {"models": [{"provider": "p", "id": "m", "thinking": "high"}]}):
            with self.subTest(value=value), self.assertRaises(NativeError):
                omp._catalog_models(value)
        self.assertEqual(omp._catalog_models({"models": []})["models"], [])


class OmpIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omp-boundary-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.environ = patch.dict(os.environ, {"SHEPHERDR_HOME": str(self.home)})
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.folder = self.home / "state/native/omp"
        self.folder.mkdir(parents=True, mode=0o700)

    def metadata(self, pid=None, **changes):
        pid = pid or os.getpid()
        endpoint = self.folder / f"{pid}-12345678.sock"
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(endpoint))
        endpoint.chmod(0o600)
        self.addCleanup(server.close)
        value = {"version": 1, "pid": pid, "sessionId": "session-a", "sessionFile": "/sessions/a.jsonl",
                 "instanceId": "instance-a", "processStart": omp._process_start(pid), "cwd": "/project", "terminal": "pane-a",
                 "endpoint": str(endpoint), "token": "a" * 64}
        value.update(changes)
        file = self.folder / f"{pid}.json"
        file.write_text(json.dumps(value))
        file.chmod(0o600)
        return file, value

    def test_pid_and_session_must_both_match(self):
        self.metadata()
        self.assertEqual(omp._target({"pid": os.getpid(), "sessionId": "session-a"})["sessionId"], "session-a")
        for target in ({"pid": os.getpid(), "sessionId": "session-b"}, {"pid": os.getpid(), "sessionFile": "/sessions/b.jsonl"}, {"pid": True}, {"pid": -1}, {"sessionId": ""}):
            with self.subTest(target=target), self.assertRaises(NativeError):
                omp._target(target)

    def test_cwd_never_selects_a_session(self):
        self.metadata()
        with self.assertRaisesRegex(NativeError, "cwd alone"):
            omp._target({"cwd": "/project"})
        self.assertEqual(omp._target({"terminal": "local/pane-a"})["pid"], os.getpid())
        self.assertEqual(omp._target({"terminal": "remote://pane-a"})["pid"], os.getpid())
        self.assertEqual(omp._target({"terminal": "pane-a", "cwd": "/project"})["pid"], os.getpid())

    def test_ambiguous_session_refuses_newest_guess(self):
        self.metadata()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        self.metadata(child.pid)
        with self.assertRaisesRegex(NativeError, "ambiguous"):
            omp._target({"sessionId": "session-a"})
        self.assertEqual(omp._target({"sessionId": "session-a", "pid": child.pid})["pid"], child.pid)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux process ancestry required")
    def test_foreground_wrapper_resolves_only_verified_fenced_descendant(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.terminate)
        self.metadata(child.pid)
        target = omp._target({"pid": os.getpid(), "terminal": "pane-a", "sessionId": "session-a"})
        self.assertEqual(target["pid"], child.pid)
        self.assertEqual(target["_foregroundPid"], os.getpid())
        omp._check_process_chain(target)
        self.assertIsNone(omp._descendant_chain(omp._process_identity(os.getpid()), child.pid, "stale-start"))
        stale = list(target["_processChain"])
        stale[-1] = (stale[-1][0], stale[-1][1], "stale-start")
        with self.assertRaisesRegex(NativeError, "ancestry changed"):
            omp._check_process_chain({"_processChain": stale})
        with self.assertRaisesRegex(NativeError, "terminal or session fence"):
            omp._target({"pid": os.getpid()})
        with self.assertRaisesRegex(NativeError, "No live"):
            omp._target({"pid": os.getpid(), "terminal": "other-pane"})
        sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(sibling.wait)
        self.addCleanup(sibling.terminate)
        with self.assertRaisesRegex(NativeError, "No live"):
            omp._target({"pid": sibling.pid, "sessionId": "session-a"})
        self.metadata(sibling.pid)
        with self.assertRaisesRegex(NativeError, "ambiguous"):
            omp._target({"pid": os.getpid(), "sessionId": "session-a", "terminal": "pane-a"})

    def test_pid_reuse_fence_rejects_stale_metadata_without_deleting_it(self):
        file, _ = self.metadata(processStart="not-this-process")
        if omp._process_start(os.getpid()) is None:
            self.skipTest("Linux process-start identity unavailable")
        with self.assertRaisesRegex(NativeError, "No live"):
            omp._target({"pid": os.getpid()})
        self.assertTrue(file.exists())

    def test_metadata_symlink_and_public_permissions_rejected(self):
        file, _ = self.metadata()
        file.chmod(0o644)
        with self.assertRaisesRegex(NativeError, "owner-only"):
            omp._target({"pid": os.getpid()})
        file.chmod(0o600)
        actual = self.folder / "actual"
        file.rename(actual)
        file.symlink_to(actual)
        with self.assertRaisesRegex(NativeError, "owner-only"):
            omp._target({"pid": os.getpid()})

    def test_endpoint_cannot_escape_private_directory(self):
        self.metadata(endpoint="/tmp/unrelated.sock")
        with self.assertRaisesRegex(NativeError, "outside"):
            omp._target({"pid": os.getpid()})

    def test_attachment_bytes_and_mime_not_file_text_are_sent(self):
        image = self.home / "fixture.png"
        raw = b"\x89PNG\r\n\x1a\n" + b"binary-image-data"
        image.write_bytes(raw)
        request = {"text": "inspect image and /missing.txt", "files": [str(image), "/missing.txt"]}
        omp._attachments(request)
        self.assertEqual(request["images"], [{"data": base64.b64encode(raw).decode(), "mimeType": "image/png"}])
        self.assertEqual(request["text"], "inspect image and /missing.txt")
        self.assertNotIn("files", request)

    def test_attachment_special_files_signature_and_size_rejected(self):
        image = self.home / "wrong.png"
        image.write_bytes(b"not-an-image")
        with self.assertRaisesRegex(NativeError, "signature"):
            omp._attachments({"files": [str(image)]})
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (5 * 1024 * 1024))
        with self.assertRaisesRegex(NativeError, "5 MiB"):
            omp._attachments({"files": [str(image)]})
        fifo = self.home / "pipe.png"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(NativeError, "regular"):
            omp._attachments({"files": [str(fifo)]})


@unittest.skipUnless(BUN, "Bun is required for native admission-event tests")
class OmpNativeAdmission(unittest.TestCase):
    def test_void_submission_needs_session_fenced_native_user_event(self):
        script = r'''
import assert from "node:assert/strict";
import {NativeAdmission,userFingerprint} from MODULE;
const schedule = callback => {const timer=setTimeout(callback,80); return ()=>clearTimeout(timer);};
const tracker = new NativeAdmission(schedule);
const user = {role:"user",content:"actual native text",timestamp:12345};
const fingerprint = userFingerprint(user);
let invocations=0, confirmed=false;
const result = tracker.submit("session-a",fingerprint,()=>{invocations++; /* Native API returns void. */});
result.then(()=>{confirmed=true;});
await Promise.resolve();
assert.equal(confirmed,false);
tracker.observe("session-a",{...user,role:"assistant"});
tracker.observe("session-b",user);
tracker.observe("session-a",{...user,content:"different"});
tracker.observe("session-a",{role:"user",content:user.content});
await Promise.resolve();
assert.equal(confirmed,false);
tracker.observe("session-a",user);
const observed=await result;
assert.equal(observed.admitted,true);
assert.equal(observed.admissionEvidence.event,"message_start");
assert.equal(observed.admissionEvidence.timestamp,12345);
assert.equal(invocations,1);
// A resolved native return value alone is also insufficient evidence.
const unresolved=tracker.submit("session-a",userFingerprint({...user,content:"not observed"}),()=>Promise.resolve(),"late-request");
await assert.rejects(unresolved,/outcome unknown/);
await assert.rejects(tracker.submit("session-a",userFingerprint({...user,content:"not observed"}),()=>{invocations++;}),/do not replay/);
assert.equal(invocations,1);
tracker.observe("session-b",{...user,content:"not observed"});
assert.equal(tracker.receipt("late-request"),undefined);
tracker.observe("session-a",{...user,content:"not observed",timestamp:12347});
assert.equal(tracker.receipt("late-request").admitted,true);
assert.equal(tracker.receipt("late-request").admissionEvidence.timestamp,12347);
assert.equal(invocations,1); // Late evidence must not replay the request.
const explicitRepeat=tracker.submit("session-a",userFingerprint({...user,content:"not observed"}),()=>{
  invocations++; tracker.observe("session-a",{...user,content:"not observed",timestamp:12348});
});
assert.equal((await explicitRepeat).admitted,true);
const refusedMessage={...user,content:"definitely refused"};
await assert.rejects(tracker.submit("session-a",userFingerprint(refusedMessage),()=>{throw new Error("not sent");}),/not sent/);
const afterRefusal=tracker.submit("session-a",userFingerprint(refusedMessage),()=>{tracker.observe("session-a",refusedMessage);});
assert.equal((await afterRefusal).admitted,true);
const images=[{type:"image",data:"YWJj",mimeType:"image/png"}];
const imageUser={role:"user",content:[{type:"text",text:"image"},...images],timestamp:12346};
const imageResult=tracker.submit("session-a",userFingerprint(imageUser),()=>{invocations++;});
tracker.observe("session-a",{...imageUser,content:[{type:"text",text:"image"},{type:"image",data:"eHl6",mimeType:"image/png"}]});
tracker.observe("session-a",imageUser);
assert.equal((await imageResult).admitted,true);
// Native user events can arrive synchronously from a void-returning implementation.
const synchronous=tracker.submit("session-a",userFingerprint({...user,content:"sync"}),()=>{
  tracker.observe("session-a",{...user,content:"sync"});
});
assert.equal((await synchronous).admitted,true);
const order=[];
const first=tracker.submit("session-a",userFingerprint({...user,content:"first"}),()=>{order.push("first");});
const second=tracker.submit("session-a",userFingerprint({...user,content:"second"}),()=>{order.push("second");});
assert.deepEqual(order,["first"]);
tracker.observe("session-a",{...user,content:"first"});
await first; await Promise.resolve();
assert.deepEqual(order,["first","second"]);
tracker.observe("session-a",{...user,content:"second"});
await second;
const stale=tracker.submit("session-a",fingerprint,()=>{invocations++;});
let queuedInvoked=false;
const queued=tracker.submit("session-a",userFingerprint({...user,content:"queued"}),()=>{queuedInvoked=true;});
const staleRejected=assert.rejects(stale,/session switched/);
const queuedRejected=assert.rejects(queued,/session switched/);
tracker.cancel("session switched; do not replay");
await Promise.all([staleRejected,queuedRejected]);
tracker.observe("session-a",user);
assert.equal(queuedInvoked,false);
let timerCount=0;
const expiring=new NativeAdmission(callback=>{
  const timer=setTimeout(callback,timerCount++===0?100:10);
  return ()=>clearTimeout(timer);
});
const active=expiring.submit("session-c",fingerprint,()=>{});
const activeRejected=assert.rejects(active,/shutdown/);
const expired=expiring.submit("session-c",userFingerprint({...user,content:"expires"}),()=>{throw new Error("must not submit expired request");});
await assert.rejects(expired,/expired before submission/);
expiring.cancel("shutdown"); await activeRejected;
const timeouts=[];
const bounded=new NativeAdmission(callback=>{timeouts.push(callback);return ()=>{};});
let submitted=0;
for(let index=0;index<256;index++){
  const content="unknown-"+index;
  const pending=bounded.submit("bounded",userFingerprint({...user,content}),()=>{submitted++;});
  const rejected=assert.rejects(pending,/outcome unknown/);
  timeouts.at(-1)(); await rejected;
}
await assert.rejects(bounded.submit("bounded",fingerprint,()=>{submitted++;}),/capacity/);
assert.equal(submitted,256);
bounded.observe("bounded",{...user,content:"unknown-0"});
const freed=bounded.submit("bounded",fingerprint,()=>{submitted++; bounded.observe("bounded",user);});
assert.equal((await freed).admitted,true);
assert.equal(submitted,257);
'''.replace("MODULE", json.dumps(EXTENSION.as_uri()))
        result = subprocess.run([BUN, "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(BUN, "Bun is required for extension-boundary tests")
class OmpBranchHistory(unittest.TestCase):
    def test_actual_history_projection_and_cursor_invariants(self):
        script = r'''
import assert from "node:assert/strict";
import { BranchHistory, nativeMessage, assertTarget, terminalIdentity, exactModel } from MODULE;
const message = (id, role, content, extra = {}) => ({ id, type:"message", parentId:null, message:{role,content,...extra} });
const root = message("root", "user", "question");
const left = message("left", "assistant", [{type:"text",text:"left branch"}]);
const right = message("right", "assistant", [{type:"toolCall",id:"call-1",name:"read",arguments:{path:"a"}}]);
const result = message("result", "toolResult", [{type:"text",text:"contents"}], {toolCallId:"call-1",toolName:"read"});
const history = new BranchHistory();
history.replace("session-a", [root,left]);
let page = history.page({limit:1});
assert.deepEqual(page.messages.map(m => m.id), ["left"]);
assert.equal(page.hasEarlier,true);
const oldCursor = page.cursor;
history.replace("session-a", [root,right,result]); // Native getBranch's selected root-to-leaf path.
page = history.page({cursor:oldCursor,limit:2});
assert.equal(page.reset,true);
assert.deepEqual(page.messages.map(m => m.id), ["right","result"]);
assert.equal(page.messages[1].toolCallId,"call-1");
assert.equal(page.messages[0].content[0].id,"call-1");
let earlier = history.page({before:"right",limit:1});
assert.deepEqual(earlier.messages.map(m => m.id), ["root"]);
assert.equal(earlier.reset,false);
const next = message("next","user","followup");
history.replace("session-a", [root,right,result,next]);
page = history.page({cursor:page.cursor,limit:2});
assert.equal(page.reset,false);
assert.deepEqual(page.messages.map(m => m.id), ["next"]);
const noChange = history.page({cursor:page.cursor});
assert.deepEqual(noChange.messages,[]);
assert.equal(noChange.revision,page.revision);
const compaction = {id:"compact",type:"compaction",summary:"native summary"};
history.replace("session-a",[root,right,result,next,compaction]);
page = history.page({limit:200});
assert.equal(page.messages.at(-1).nativeType,"compaction");
assert.equal(page.messages.at(-1).content,"native summary");
history.replace("session-a",[root,right,result,next,compaction,{id:"clear",type:"reset_boundary"},message("fresh","user","fresh")]);
page = history.page({cursor:page.cursor});
assert.equal(page.reset,true);
assert.deepEqual(page.messages.map(m => m.id),["fresh"]);
const revision = page.revision;
history.streaming({role:"assistant",content:[{type:"text",text:"partial"}]});
page = history.page({cursor:page.cursor});
assert.notEqual(page.revision,revision);
assert.equal(page.messages.at(-1).content[0].text,"partial");
const streamId = page.messages.at(-1).id;
history.streaming({role:"assistant",content:[{type:"text",text:"partial update"}]});
const streamDelta = history.page({cursor:page.cursor});
assert.equal(streamDelta.reset,false);
assert.deepEqual(streamDelta.messages.map(message => message.id),[streamId]);
assert.equal(streamDelta.messages[0].content[0].text,"partial update");
history.replace("session-b",[]);
history.streaming();
assert.deepEqual(history.page({cursor:page.cursor}).messages,[]);
assert.throws(() => history.page({limit:201}), /limit/);
assert.throws(() => history.page({limit:true}), /limit/);
assert.throws(() => history.page({cursor:42}), /cursor/);
assert.throws(() => history.page({before:{}}), /before/);
const blob = nativeMessage(message("image","user",[{type:"image",data:"blob:sha256:"+"a".repeat(64),mimeType:"image/png"}]));
assert.equal(blob.content[0].path,"blob:sha256:"+"a".repeat(64));
assert.equal(blob.content[0].data,undefined);
const expected = {pid:123,sessionId:"s",instanceId:"i",sessionFile:"/s"};
assertTarget({...expected},expected);
const catalog=[{provider:"one",id:"same"},{provider:"two",id:"same"},{provider:"one",id:"family/model"}];
assert.equal(exactModel(catalog,"two/same"),catalog[1]);
assert.equal(exactModel(catalog,"one/family/model"),catalog[2]);
for(const selector of ["same","@slow","one/sam","one/*","unknown/same",null]) assert.throws(()=>exactModel(catalog,selector),/exact provider-qualified/);
assert.equal(terminalIdentity({HC_TERMINAL_ID:"local/t3",HERDR_PANE_ID:"old-pane"}),"t3");
assert.equal(terminalIdentity({HC_TERMINAL_ID:"machine://t9"}),"t9");
assert.equal(terminalIdentity({HERDR_PANE_ID:"legacy/t5"}),"t5");
for (const changed of [{pid:124},{sessionId:"other"},{instanceId:"other"},{sessionFile:"/other"}]) assert.throws(() => assertTarget({...expected,...changed},expected),/Stale/);
'''.replace("MODULE", json.dumps(EXTENSION.as_uri()))
        result = subprocess.run([BUN, "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(BUN, "Bun is required for real Unix IPC boundary tests")
class OmpPrivateTransport(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="omp-ipc-")
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.folder.chmod(0o700)
        self.native = self.folder / "state/native/omp"
        self.native.mkdir(parents=True, mode=0o700)
        self.environment = patch.dict(os.environ, {"SHEPHERDR_HOME": str(self.folder)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.endpoint = str(self.native / "bridge.sock")
        self.token = "a" * 64
        script = r'''
import {createBridgeServer,assertTarget} from MODULE;
const expected = {pid:process.pid,sessionId:"session-a",instanceId:"instance-a",sessionFile:"/session/a"};
let admitted=0;
const server=await createBridgeServer(process.env.TEST_ENDPOINT,process.env.TEST_TOKEN,async(operation,payload)=>{
  assertTarget(payload,expected);
  if(operation==="probe" || operation==="state" || operation==="discover") return {...expected,admitted};
  throw new Error("Unsupported boundary-test operation");
});
server.ref();
console.log(JSON.stringify(expected));
'''.replace("MODULE", json.dumps(EXTENSION.as_uri()))
        self.child = subprocess.Popen([BUN, "-e", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      env={**os.environ, "TEST_ENDPOINT": self.endpoint, "TEST_TOKEN": self.token})
        self.addCleanup(self.child.stdout.close)
        self.addCleanup(self.child.stderr.close)
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.terminate)
        self.identity = json.loads(self.child.stdout.readline())
        metadata = self.native / f'{self.identity["pid"]}.json'
        metadata.write_text(json.dumps({**self.identity, "version": 1, "endpoint": self.endpoint, "token": self.token,
                                        "processStart": omp._process_start(self.identity["pid"]), "terminal": "pane-boundary"}))
        metadata.chmod(0o600)

    def frame(self, value):
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(3)
            connection.connect(self.endpoint)
            connection.sendall(value)
            output = b""
            while b"\n" not in output:
                part = connection.recv(65536)
                if not part:
                    break
                output += part
            return json.loads(output)

    @unittest.skipUnless(Path("/proc/self/stat").exists(), "Linux process ancestry required")
    def test_wrapper_pid_remains_frontend_identity_while_ipc_addresses_owner(self):
        state = omp.handle("discover", {"pid": os.getpid(), "terminal": "pane-boundary"})
        self.assertEqual(state["pid"], os.getpid())
        self.assertEqual(state["ownerPid"], self.identity["pid"])
        self.assertEqual(state["sessionId"], "session-a")
        direct = omp.handle("state", {"pid": self.identity["pid"], "sessionId": "session-a"})
        self.assertEqual(direct["pid"], self.identity["pid"])
        with self.assertRaisesRegex(NativeError, "No live"):
            omp.handle("discover", {"pid": os.getpid(), "terminal": "other-pane"})

    def test_owner_only_endpoint_and_authenticated_identity(self):
        self.assertEqual(os.stat(self.endpoint).st_mode & 0o777, 0o600)
        self.assertEqual(socket_request(self.endpoint, self.token, "probe", self.identity)["sessionId"], "session-a")
        with self.assertRaisesRegex(NativeError, "authentication"):
            socket_request(self.endpoint, "b" * 64, "probe", self.identity)
        with self.assertRaisesRegex(NativeError, "Stale"):
            socket_request(self.endpoint, self.token, "probe", {**self.identity, "sessionId": "old-session"})
        os.chmod(self.endpoint, 0o666)
        with self.assertRaisesRegex(NativeError, "owner-only"):
            socket_request(self.endpoint, self.token, "probe", self.identity)

    def test_malformed_frames_fail_without_native_dispatch_or_server_death(self):
        self.assertFalse(self.frame(b"[]\n")["ok"])
        self.assertFalse(self.frame(b"not-json\n")["ok"])
        self.assertFalse(self.frame(b"\xff\n")["ok"])
        base = {"version": 1, "token": self.token, "id": "boundary", "operation": "probe", "payload": self.identity}
        for extra in ({"version": 2}, {"payload": []}, {"operation": 42}):
            reply = self.frame((json.dumps({**base, **extra}) + "\n").encode())
            self.assertFalse(reply["ok"])
        reply = self.frame((json.dumps(base) + "\n{}\n").encode())
        self.assertFalse(reply["ok"])
        self.assertEqual(socket_request(self.endpoint, self.token, "probe", self.identity)["admitted"], 0)


if __name__ == "__main__":
    unittest.main()
