import { expect, test } from "bun:test";
import { ChatReader, kindOf } from "./chat-reader.js";
import { nativeStatus } from "./chat-view.js";
import { sendBody } from "./send.js";
import { BgJobs } from "./bg-jobs.js";

const terminal = (kind = "omp", pid = 42) => ({ address: "t1", foreground: { name: kind, pid, cwd: "/work" } });
const user = (id, text = id) => ({ id, role: "user", content: text });
function nativeIO() {
  const fixture = {
    state: { sessionId: "one", pid: 42, revision: "1", working: false, settled: true, pendingAsyncWork: false,
      capabilities: { history: true, send: true, interrupt: true, configure: true, answer: false, fork: false } },
    history: { messages: [user("u1")], cursor: "c1", revision: "1", hasEarlier: false },
    calls: [], failure: null,
  };
  fixture.io = { sh: async (_script, args, machine) => {
    const [kind, operation, body] = args.slice(-3);
    const payload = JSON.parse(body);
    fixture.calls.push({ kind, operation, payload, machine });
    if (fixture.failure) return { code: 1, stdout: JSON.stringify({ error: fixture.failure }) };
    const data = operation === "state" ? fixture.state : operation === "history" ? fixture.history : { admitted: true };
    return { code: 0, stdout: JSON.stringify(data) };
  } };
  return fixture;
}

test("native readers bind exact foreground identities and do not rescan unchanged history", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "remote/t1");
  await reader.locate(terminal());
  await reader.pull();
  expect(reader.nativeState).toBe(reader.native);
  expect(reader.target()).toMatchObject({ pid: 42, sessionId: "one" });
  expect(f.calls[0]).toMatchObject({ operation: "state", payload: { pid: 42 }, machine: "remote" });
  const conv = reader.conv;
  f.state = { ...f.state, pendingAsyncWork: true };
  await reader.locate(terminal());
  expect(await reader.pull()).toBe(false);
  expect(reader.conv).toBe(conv);
  expect(f.calls.filter((call) => call.operation === "history")).toHaveLength(1);
  expect(reader.nativeStatus()).toBe("working");
});

test("native branch resets discard old calls, results and messages", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "t1");
  f.history.messages.push({ id: "a1", role: "assistant", content: [{ type: "toolCall", id: "old-call", name: "bash", arguments: { command: "old" } }] });
  await reader.locate(terminal()); await reader.pull();
  f.state.revision = "2";
  f.history = { messages: [user("branch-user")], cursor: "branch-cursor", revision: "2", reset: true, hasEarlier: false };
  await reader.locate(terminal()); await reader.pull();
  expect(f.calls.at(-1).payload.cursor).toBe("c1");
  expect(reader.conv.items.map((item) => item.id)).toEqual(["branch-user"]);
  expect(reader.conv.open.size).toBe(0);
  expect(reader.conv.nativeResults.size).toBe(0);
  expect(reader.cursor).toBe("branch-cursor");
});

test("native session switches invalidate history even when the process stays", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "t1");
  await reader.locate(terminal()); await reader.pull();
  f.state = { ...f.state, sessionId: "two", revision: "1" };
  f.history = { messages: [user("u2")], cursor: "c2", hasEarlier: true };
  await reader.locate(terminal());
  expect(reader.conv).toBeNull();
  await reader.pull();
  expect(f.calls.at(-1).payload.cursor).toBeUndefined();
  expect(reader.conv.items.map((item) => item.id)).toEqual(["u2"]);
  f.history = { messages: [user("earlier")], hasEarlier: false };
  await reader.earlier();
  expect(f.calls.at(-1).payload.before).toBe("u2");
  expect(reader.conv.items.map((item) => item.id)).toEqual(["earlier", "u2"]);
});

test("streaming native messages replace stable IDs without duplicating output", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "t1");
  f.history.messages = [{ id: "a1", role: "assistant", content: "first" }];
  await reader.locate(terminal()); await reader.pull();
  f.state.revision = "2";
  f.history = { messages: [{ id: "a1", role: "assistant", content: "first second" }], cursor: "c2", revision: "2" };
  await reader.locate(terminal()); await reader.pull();
  expect(reader.conv.items).toHaveLength(1);
  expect(reader.conv.items[0]).toMatchObject({ id: "a1", text: "first second" });
});

test("missing native history is an explicit error, not an empty archived conversation", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "t1").fixed("/archive/session.jsonl", "hermes");
  f.failure = "Native archive is unavailable";
  await expect(reader.pull()).rejects.toThrow("Native archive is unavailable");
  expect(reader.conv).toBeNull();
  expect(f.calls[0]).toMatchObject({ kind: "hermes", operation: "history", payload: { sessionFile: "/archive/session.jsonl" } });
});

test("native settlement cannot be inferred from terminal Ready or absent async information", () => {
  expect(nativeStatus({ settled: null, working: false })).toBe("working");
  expect(nativeStatus({ settled: true, pendingAsyncWork: true })).toBe("working");
  expect(nativeStatus({ settled: true, working: false, pendingAsyncWork: false })).toBe("idle");
  expect(nativeStatus({ settled: true, pendingRequests: [{ id: "permission" }] })).toBe("blocked");
  expect(kindOf(terminal("omo"))).toBe("omo");
  expect(kindOf({ labels: { "herdr.agent": "omp" }, foreground: { name: "bash", pid: 42 } })).toBeNull();
});

test("native sends use admission once and never type or autoanswer terminal dialogs", async () => {
  const f = nativeIO();
  let typed = 0;
  const io = { ...f.io, target: () => ({ pid: 42, sessionId: "one" }), native: () => f.state, hc: async () => { typed++; throw new Error("must not type"); } };
  expect((await sendBody(io, "t1", "omp", "hello")).code).toBe(0);
  expect(f.calls[0]).toMatchObject({ kind: "omp", operation: "send", payload: { pid: 42, text: "hello" } });
  f.failure = "connection ended before admission was confirmed";
  expect((await sendBody(io, "t1", "omp", "uncertain")).uncertain).toBe(true);
  expect((await sendBody(io, "t1", "omp", "uncertain", true)).uncertain).toBe(true);
  expect(f.calls).toHaveLength(2);
  f.state.capabilities.send = false;
  expect((await sendBody(io, "t1", "omp", "terminal only")).code).toBe(1);
  expect(typed).toBe(0);
});

test("native background work never falls through to process-tree cancellation", async () => {
  const jobs = new BgJobs();
  const conv = { native: { jobs: [{ id: "job", name: "Watch", command: "watch", status: "working" }] } };
  expect(jobs.list("omo", conv)[0]).toMatchObject({ id: "job", native: true, stoppable: false });
  await expect(jobs.stop(() => { throw new Error("must not inspect processes"); }, "omo", 42, { id: "job", task: "job" })).rejects.toThrow("Open Terminal");
});

test("native requests and unsupported images are rejected without attempting prompt admission", async () => {
  const f = nativeIO();
  const io = { ...f.io, target: { pid: 42, sessionId: "one" }, native: () => f.state };
  f.state.pendingRequests = [{ id: "permission", answerable: false }];
  expect((await sendBody(io, "t1", "omp", "yes")).code).toBe(1);
  expect(f.calls).toHaveLength(0);
  f.state.pendingRequests = [];
  const { sendNative } = await import("./send.js");
  expect((await sendNative(io, "t1", "omo", "look", { images: [{ data: "AAAA", mimeType: "image/png" }] })).code).toBe(1);
  expect(f.calls).toHaveLength(0);
});

test("a pinned native archive is read once, and empty admission replies do not claim success", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "t1").fixed("/archive/exact", "omh");
  await reader.pull();
  expect(await reader.pull()).toBe(false);
  expect(f.calls.filter((call) => call.operation === "history")).toHaveLength(1);
  const io = { sh: async () => ({ code: 0, stdout: "{}" }), target: { pid: 42 }, native: f.state };
  expect((await sendBody(io, "t1", "omh", "hello")).uncertain).toBe(true);
});

test("native remote identity uses the terminal's local id and interpreter labels do not override real agents", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "remote/t1");
  await reader.locate(terminal());
  expect(f.calls[0].payload.terminal).toBe("t1");
  expect(reader.target().terminal).toBe("t1");
  expect(kindOf({ labels: { "herdr.agent": "omh" }, foreground: { name: "python3", pid: 42 } })).toBe("omh");
  expect(kindOf({ labels: { "herdr.agent": "omp" }, foreground: { name: "codex", pid: 42 } })).toBe("codex");
});

test("native stream replay gaps remain visible without discarding authoritative completed history", async () => {
  const f = nativeIO(), reader = new ChatReader(f.io, "t1");
  f.history.streamingUnavailable = true;
  await reader.locate(terminal()); await reader.pull();
  expect(reader.state).toBe("ready");
  expect(reader.conv.items[0].text).toBe("u1");
  expect(reader.error).toContain("could not be replayed");
  await reader.locate(terminal());
  expect(await reader.pull()).toBe(false);
  expect(reader.error).toContain("could not be replayed");
});

test("a native history burst drains every bounded page before accepting its revision as complete", async () => {
  const fixture = nativeIO(), reader = new ChatReader(fixture.io, "t1");
  fixture.history.hasMore = true;
  await reader.locate(terminal());
  await reader.pull();
  fixture.history = { messages: [user("u2")], cursor: "c2", revision: "1", hasMore: false };
  expect(await reader.pull()).toBe(true);
  expect(reader.conv.items.filter((item) => item.kind === "user").map((item) => item.id)).toEqual(["u1", "u2"]);
  expect(await reader.pull()).toBe(false);
});
