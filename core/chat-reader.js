// Following an agent's conversation from its own session log, as the window's chat does (mods/chat: find the
// log, read the newest page, then what it gains; earlier pages when asked; keep at most KEEP records):
// the service uses it for the web app. io.sh(script, args, machine) -> Promise<{ code, stdout }> runs a shell
// script on the agent's machine (null: here).
// Shared core: the source is core/chat-reader.js.

import { FIND, LOG, Conversation, records, resultText } from "./chat-log.js";
import { BgJobs, nativeJobs } from "./bg-jobs.js";
import { isNative, nativeRequest } from "./native-runtime.js";
import { nativeStatus } from "./chat-view.js";

export const PAGE = 200;
export const KEEP = 2000;
export const KINDS = { "claude-code": "claude", claude: "claude", codex: "codex", omp: "omp", omo: "omo", omh: "omh", hermes: "hermes" };

// The CLI a terminal runs, as the chat knows them ("claude", "codex"), else null.
export const kindOf = (t) => {
  const foreground = KINDS[t?.foreground?.name], reported = KINDS[t?.status?.app];
  if (foreground || reported) return foreground ?? reported;
  const labelled = t?.labels?.["herdr.agent"];
  // Native launchers may retain their interpreter's process name, but a shell is not its ended agent.
  return isNative(labelled) && /^(?:python(?:3(?:\.\d+)?)?|node|bun|shepherdr-native-run)$/.test(t?.foreground?.name ?? "") ? labelled : null;
};

export class ChatReader {
  constructor(io, addr) {
    this.io = io;
    this.addr = addr;
    this.machine = addr.includes("/") ? addr.split("/")[0] : null;
    this.reset();
  }

  reset() {
    this.pinned = false;
    this.path = null;
    this.kind = null;
    this.pid = null;
    this.start = null;   // the first byte read (0: the log's very start)
    this.end = 0;
    this.records = [];
    this.conv = null;
    this.bg = new BgJobs(); // what it left running in the background
    this.native = null;
    this.capabilities = {};
    this.error = null;
    this.cursor = null;
    this.historyNotice = null;
    this.historyRevision = null;
    this.messages = [];
    this.identity = null;
    this.state = "finding"; // finding | pending (no log yet) | ready | none (not a chat agent) | missing
  }

  log(args) { return this.io.sh(LOG, ["log", ...args], this.machine); }

  // An ended agent's own log (a finished task's last agent, as the Tasks panel opens it): read as it is, never
  // looked for again. -> this reader.
  fixed(path, kind) {
    this.reset();
    this.path = path;
    this.kind = kind;
    this.pinned = true;
    return this;
  }

  get nativeState() { return this.native; }

  target() {
    return { ...(this.pid ? { pid: this.pid } : {}), ...(this.native?.sessionId ? { sessionId: this.native.sessionId } : {}),
      ...(this.path ? { sessionFile: this.path } : {}), terminal: this.addr.split("/").pop() };
  }

  request(operation, payload = {}) {
    if (!isNative(this.kind)) throw new Error("This is not a native runtime.");
    return nativeRequest(this.io, this.kind, operation, { ...this.target(), ...payload }, this.machine);
  }

  nativeStatus() { return nativeStatus(this.native); }

  async locateNative(t, kind, pid) {
    const sameProcess = this.kind === kind && this.pid === pid;
    if (!sameProcess) { this.reset(); this.kind = kind; this.pid = pid; }
    const previous = JSON.stringify(this.native);
    try {
      // Discovery targets the foreground process, never the last session observed in this terminal.
      const state = await nativeRequest(this.io, kind, "state", { pid, terminal: this.addr.split("/").pop(), cwd: t.foreground?.cwd ?? t.cwd }, this.machine);
      if (this.kind !== kind || this.pid !== pid) return false;
      if (!state.sessionId || (state.pid != null && Number(state.pid) !== Number(pid))) throw new Error("Native runtime returned a different foreground identity.");
      const identity = `${kind}:${pid}:${state.sessionId}:${state.sessionFile ?? ""}`;
      if (this.identity !== identity) {
        this.path = state.sessionFile ?? null;
        this.start = null;
        this.cursor = null;
        this.messages = [];
        this.conv = null;
        this.historyRevision = null;
        this.identity = identity;
      }
      this.native = state;
      this.capabilities = state.capabilities ?? {};
      this.error = this.historyNotice;
      this.state = this.capabilities.history ? (this.conv ? "ready" : "finding") : "missing";
      this.conv?.applyNative(state);
      return !sameProcess || previous !== JSON.stringify(state);
    } catch (e) {
      if (this.kind !== kind || this.pid !== pid) return false;
      this.error = String(e?.message ?? e);
      this.native = null;
      this.capabilities = {};
      this.state = "missing";
      return previous !== "null" || !sameProcess;
    }
  }

  async pullNative(before = null) {
    if (!this.capabilities.history && !this.pinned) return false;
    if (!before && this.pinned && this.conv) return false;
    if (!before && this.conv && this.native?.revision != null && this.historyRevision === this.native.revision) return false;
    const identity = this.identity, kind = this.kind, path = this.path;
    const page = await this.request("history", { limit: PAGE, ...(before ? { before } : this.cursor != null ? { cursor: this.cursor } : {}) });
    if (identity !== this.identity || kind !== this.kind || path !== this.path) return false;
    if (!Array.isArray(page.messages)) throw new Error("Native runtime did not return conversation history.");
    const reset = page.reset === true;
    const incoming = page.messages;
    const previous = reset ? [] : this.messages;
    const ids = new Set(previous.map((message) => message.id));
    const updated = incoming.some((message) => ids.has(message.id));
    const combined = before && !reset ? [...incoming, ...previous] : [...previous, ...incoming];
    const messages = new Map();
    for (const message of combined) {
      if (typeof message?.id !== "string" || !message.id) throw new Error("Native history message has no stable identity.");
      // Repeated ids replace a streaming message in its original position.
      messages.set(message.id, message);
    }
    this.messages = [...messages.values()];
    let trimmed = false;
    if (!before && this.messages.length > KEEP) { this.messages = this.messages.slice(-Math.floor(KEEP * .75)); trimmed = true; }
    if (!before || reset) {
      this.cursor = page.cursor ?? null;
      this.historyRevision = page.hasMore === true ? null : page.revision ?? this.native?.revision ?? null;
    }
    if (before || reset || this.start === null) this.start = page.hasEarlier ? (this.messages[0]?.id ?? null) : 0;
    if (trimmed) this.start = this.messages[0]?.id ?? 0;
    if (!this.conv || before || reset || trimmed || updated) {
      this.conv = new Conversation(this.kind);
      this.conv.feedNative(this.messages);
    } else this.conv.feedNative(incoming);
    this.conv.applyNative(this.native);
    this.state = "ready";
    this.historyNotice = page.streamingUnavailable === true ? "Some live native output could not be replayed. Completed history is authoritative; open Terminal for the live stream." : null;
    this.error = this.historyNotice;
    return true;
  }

  // Finds the log of the agent this terminal runs now (t: its herdr-core record). -> true when it changed.
  async locate(t) {
    if (this.pinned) return false;
    const kind = kindOf(t);
    const pid = t?.foreground?.pid;
    if (!kind || !pid) { const was = this.state; if (was !== "none") { this.reset(); this.state = "none"; } return was !== "none"; }
    if (isNative(kind)) return this.locateNative(t, kind, pid);
    if (this.path && this.kind === kind && this.pid === pid) return false;
    const f = await this.io.sh(FIND, ["chat", kind, String(pid)], this.machine);
    const path = String(f.stdout ?? "").trim().split("\n").pop() ?? "";
    if (path === "pending" || !path) {
      const was = this.state;
      this.reset();
      this.kind = kind;
      this.pid = pid;
      this.state = path === "pending" ? "pending" : "none";
      if (this.state === "pending") { this.conv = new Conversation(kind); this.start = 0; }
      return was !== this.state;
    }
    if (path === this.path && kind === this.kind) { this.pid = pid; return false; }
    this.reset();
    this.path = path;
    this.kind = kind;
    this.pid = pid;
    return true;
  }

  // ---- what it left running in the background (core/bg-jobs.js) ----

  run(script, args) { return this.io.sh(script, args, this.machine); }

  // A look at the agent's processes when one is due. -> true when what shows changed.
  async lookJobs() {
    if (this.pinned || !this.pid || !this.conv || !this.bg.due(this.kind, this.conv)) return false;
    if (isNative(this.kind)) return false; // Native state owns jobs; process guesses cannot prove settlement.
    return this.bg.look((s, a) => this.run(s, a), this.kind, this.pid, this.conv);
  }

  // Its shells and watchers now (none for a log read as it is).
  jobs() { return this.pinned || !this.conv ? [] : isNative(this.kind) ? nativeJobs(this.native) : this.bg.list(this.kind, this.conv); }

  // One of them stopped. -> false when it's gone already.
  async stopJob(id) {
    if (isNative(this.kind)) throw new Error("This native runtime has no per-job cancellation API. Open Terminal to manage its jobs.");
    const job = this.jobs().find((j) => j.id === id);
    return job ? this.bg.stop((s, a) => this.run(s, a), this.kind, this.pid, job) : false;
  }

  // What one of them printed last (Claude Code's), or null.
  async jobOutput(id) {
    if (isNative(this.kind)) return null;
    const job = this.jobs().find((j) => j.id === id);
    return job ? this.bg.output((s, a) => this.run(s, a), job) : null;
  }

  rebuild() {
    this.conv = new Conversation(this.kind);
    this.conv.feed(this.records);
  }

  // The newest page first; after that, what the log gained since. -> true when there's something new.
  async pull() {
    if (isNative(this.kind)) return this.pullNative();
    if (!this.path) return false;
    const path = this.path;
    if (this.start === null) {
      const r = await this.log(["page", path, "999999999999999", String(PAGE)]);
      if (path !== this.path) return false;
      if (String(r.stdout).startsWith("missing")) { this.path = null; this.state = "missing"; return true; }
      const { head, records: recs } = records(r.stdout);
      if (head[0] !== "P") return false;
      this.start = Number(head[1]);
      this.end = Number(head[2]);
      this.records = recs;
      this.rebuild();
      this.state = "ready";
      return true;
    }
    const r = await this.log(["tail", path, String(this.end)]);
    if (path !== this.path) return false;
    if (String(r.stdout).startsWith("missing")) { this.path = null; this.state = "missing"; return true; }
    const { head, records: recs } = records(r.stdout);
    if (head[0] !== "T") return false;
    // Shorter than what was read: the file was replaced. Start over from its end.
    if (Number(head[2]) < this.end) { const p = this.path, k = this.kind, pid = this.pid; this.reset(); Object.assign(this, { path: p, kind: k, pid }); return this.pull(); }
    this.end = Number(head[3]);
    if (!recs.length) return false;
    this.records.push(...recs);
    if (this.records.length > KEEP) {
      // Down to three quarters, so the rebuild is now and then, not on every message.
      this.records = this.records.slice(-Math.floor(KEEP * 0.75));
      this.start = this.records[0].off;
      this.rebuild();
    } else this.conv.feed(recs);
    return true;
  }

  // What a tools row's calls printed, read from the log only when the row opens (as the window's chat
  // loadResults()): -> Map step id -> its first lines ("" when none).
  async results(steps) {
    const out = new Map();
    if (isNative(this.kind)) {
      for (const step of steps ?? []) if (this.conv?.nativeResults.has(step.id)) out.set(step.id, this.conv.nativeResults.get(step.id));
      return out;
    }
    const want = (steps ?? []).filter((s) => s.out !== undefined);
    if (!want.length || !this.path) return out;
    const offs = [...new Set(want.map((s) => s.out))].slice(0, 40);
    const r = await this.log(["results", this.path, ...offs.map(String)]);
    for (const part of String(r.stdout ?? "").split("\x1e").slice(1)) {
      const nl = part.indexOf("\n");
      const off = Number(part.slice(0, nl));
      const line = part.slice(nl + 1).replace(/\n$/, "");
      for (const s of want) if (s.out === off) out.set(s.id, resultText(line, s.id) ?? "");
    }
    return out;
  }

  // The page before what is read. -> true when there was one.
  async earlier() {
    if (isNative(this.kind)) return this.start ? this.pullNative(this.messages[0]?.id) : false;
    if (!this.start || !this.path) return false;
    const path = this.path, before = this.start;
    const r = await this.log(["page", path, String(before), String(PAGE)]);
    const { head, records: recs } = records(r.stdout);
    if (path !== this.path || this.start !== before || head[0] !== "P") return false;
    this.start = Number(head[1]);
    this.records = [...recs, ...this.records];
    this.rebuild();
    return true;
  }
}
