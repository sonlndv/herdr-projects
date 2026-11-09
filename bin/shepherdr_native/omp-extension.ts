// Loaded with `omp --extension /absolute/bin/shepherdr_native/omp-extension.ts`.
// This bridge observes the owning TUI; it never opens a session writer or intercepts tools.
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import os from "node:os";
import { createHash, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import type { ExtensionAPI, ExtensionContext, PlanReviewChoice } from "@oh-my-pi/pi-coding-agent";

const MAX_FRAME = 8 * 1024 * 1024;
type ObjectValue = Record<string, unknown>;
const object = (value: unknown): value is ObjectValue => value !== null && typeof value === "object" && !Array.isArray(value);
type HistoryEntry = {
  id: string; type: string; timestamp?: number | string; message?: unknown;
  attribution?: unknown; content?: unknown; summary?: unknown; shortSummary?: unknown;
};

/** Fence each authenticated request against both process reuse and in-process session switches. */
export function assertTarget(payload: ObjectValue, expected: { pid: number; sessionId: string; instanceId: string; sessionFile?: string }): void {
  if (payload.pid !== expected.pid || payload.sessionId !== expected.sessionId || payload.instanceId !== expected.instanceId || (payload.sessionFile !== undefined && payload.sessionFile !== expected.sessionFile)) {
    throw new Error("Stale OMP process/session identity; rediscover before sending");
  }
}

export function privateDirectory(folder: string): void {
  fs.mkdirSync(folder, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(folder);
  if (!info.isDirectory() || info.uid !== process.getuid?.() || (info.mode & 0o077)) {
    throw new Error("OMP bridge directory must be owner-only and owned by this user");
  }
}

function stateDirectory(): string {
  if (process.env.SHEPHERDR_HOME) return path.resolve(process.env.SHEPHERDR_HOME, "state/native/omp");
  return path.resolve(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local/state"), "shepherdr/native/omp");
}

function processStart(pid: number): string | undefined {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, "utf8");
    return stat.slice(stat.lastIndexOf(")") + 2).split(/\s+/)[19];
  } catch { return undefined; }
}

export function terminalIdentity(environment: NodeJS.ProcessEnv = process.env): string | undefined {
  for (const name of ["HC_TERMINAL_ID", "HERDR_PANE_ID"]) {
    const value = environment[name];
    if (value) return value.split("/").pop();
  }
  for (const name of ["ZELLIJ_PANE_ID", "TMUX_PANE", "CMUX_SURFACE_ID", "KITTY_WINDOW_ID", "WEZTERM_PANE", "TERM_SESSION_ID", "WT_SESSION"]) {
    if (environment[name]) return environment[name];
  }
  try { return fs.readlinkSync("/proc/self/fd/0"); } catch { return undefined; }
}

function safeUnlink(file: string, socket = false): void {
  try {
    const info = fs.lstatSync(file);
    if (info.uid === process.getuid?.() && !(info.mode & 0o077) && (socket ? info.isSocket() : info.isFile())) fs.unlinkSync(file);
  } catch { /* Already absent or unsafe: never remove someone else's path. */ }
}

export function nativeMessage(entry: HistoryEntry): ObjectValue | undefined {
  const source = entry.type === "message" && object(entry.message) ? entry.message : undefined;
  let role: string, content: unknown;
  if (object(source)) {
    if (typeof source.role !== "string" || !["user", "assistant", "toolResult", "developer", "bashExecution", "pythonExecution", "fileMention", "custom", "hookMessage"].includes(source.role)) return undefined;
    role = source.role === "developer" || source.role === "fileMention" ? "user"
      : source.role === "bashExecution" || source.role === "pythonExecution" ? "toolResult"
      : source.role === "custom" || source.role === "hookMessage" ? "assistant" : source.role;
    content = source.content ?? source.output ?? "";
  } else if (entry.type === "custom_message") {
    role = entry.attribution === "user" ? "user" : "assistant";
    content = entry.content;
  } else if (entry.type === "compaction" || entry.type === "branch_summary") {
    role = "assistant";
    content = entry.summary || entry.shortSummary || "";
  } else return undefined;
  const result: ObjectValue = { id: entry.id, role, content: projectContent(content), timestamp: source?.timestamp ?? entry.timestamp };
  if (source) {
    for (const key of ["model", "provider", "usage", "toolCallId", "toolName", "isError"]) if (source[key] !== undefined) result[key] = source[key];
    if (source.role !== role) result.nativeRole = source.role;
    if (role === "toolResult" && !result.toolCallId) result.toolCallId = entry.id;
  } else result.nativeType = entry.type;
  return result;
}

function projectContent(content: unknown): unknown {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return typeof content === "undefined" ? "" : JSON.stringify(content);
  return content.flatMap<ObjectValue>(block => {
    if (!object(block)) return [];
    if (block.type === "text") return [{ type: "text", text: block.text ?? "" }];
    if (block.type === "thinking") return [{ type: "thinking", thinking: block.thinking ?? "" }];
    if (block.type === "toolCall") return [{ type: "toolCall", id: block.id, name: block.name, arguments: block.arguments }];
    if (block.type === "image") {
      // Native session loading already resolves ordinary blobs. Leave unresolved references opaque;
      // never resolve a history-provided path, URL, or blob hash as an arbitrary filesystem read.
      const image: ObjectValue = { type: "image", mimeType: block.mimeType };
      if (typeof block.data === "string" && block.data.startsWith("blob:sha256:")) image.path = block.data;
      else if (typeof block.data === "string") image.data = block.data;
      if (typeof block.path === "string") image.path = block.path;
      return [image];
    }
    return [];
  });
}

/** Active-branch transcript, not physical journal order. Cursors are opaque, bounded and instance-local. */
export class BranchHistory {
  private session = "";
  private entries: HistoryEntry[] = [];
  private epoch = 0;
  private serial = 0;
  private streamVersion = 0;
  private stream?: ObjectValue;
  private cursors = new Map<string, { epoch: number; end: number; last?: string; streamVersion: number }>();
  get revision(): string { return `${this.session}:${this.epoch}:${this.serial}`; }
  replace(session: string, branch: readonly HistoryEntry[]): void {
    let boundary = -1;
    for (let i = branch.length - 1; i >= 0; i--) if (branch[i].type === "reset_boundary") { boundary = i; break; }
    const entries = branch.slice(boundary + 1).filter(entry => entry.type === "message" || entry.type === "custom_message" || entry.type === "compaction" || entry.type === "branch_summary");
    if (session !== this.session || entries.length < this.entries.length || this.entries.some((entry, i) => entry.id !== entries[i]?.id)) this.epoch++;
    this.session = session;
    this.entries = entries;
    this.serial++;
  }
  streaming(message?: ObjectValue): void {
    if (this.stream && !message) this.epoch++;
    this.stream = message;
    this.streamVersion++;
    this.serial++;
  }
  page(payload: ObjectValue): ObjectValue {
    const limit = payload.limit === undefined ? 100 : payload.limit;
    if (typeof limit !== "number" || !Number.isInteger(limit) || limit < 1 || limit > 200) throw new Error("OMP history limit must be between 1 and 200");
    if (payload.cursor !== undefined && typeof payload.cursor !== "string") throw new Error("Invalid OMP history cursor");
    if (payload.before !== undefined && typeof payload.before !== "string") throw new Error("Invalid OMP history before identity");
    const count = this.entries.length + (this.stream ? 1 : 0);
    let end = count, start = Math.max(0, end - limit), reset = true;
    if (payload.before) {
      const index = this.entries.findIndex(entry => entry.id === payload.before);
      if (index >= 0) { end = index; start = Math.max(0, end - limit); reset = false; }
      else reset = true;
    } else if (typeof payload.cursor === "string") {
      const cursor = this.cursors.get(payload.cursor);
      if (cursor && cursor.epoch === this.epoch && cursor.end <= count && (cursor.end === 0 || this.entries[cursor.end - 1]?.id === cursor.last)) {
        start = cursor.end; end = Math.min(count, start + limit); reset = false;
        if (this.stream && cursor.streamVersion !== this.streamVersion) start = Math.min(start, this.entries.length);
      }
    }
    const messages: ObjectValue[] = [];
    for (let i = start; i < end; i++) {
      const entry = i < this.entries.length ? this.entries[i] : { type: "message", id: `${this.session}:stream`, message: this.stream };
      const message = nativeMessage(entry);
      if (message) messages.push(message);
    }
    const cursor = randomUUID();
    // Earlier-page fetches must not move the live delta cursor back into already-loaded history.
    const position = payload.before ? count : end;
    const last = this.entries[position - 1]?.id;
    this.cursors.set(cursor, { epoch: this.epoch, end: position, last: typeof last === "string" ? last : undefined, streamVersion: this.streamVersion });
    if (this.cursors.size > 256) this.cursors.delete(this.cursors.keys().next().value!);
    return { messages, cursor, hasEarlier: start > 0, hasMore: end < count, revision: this.revision, reset };
  }
}

/** One request per authenticated local connection. Exported for real transport-boundary tests. */
export async function createBridgeServer(endpoint: string, token: string, dispatch: (operation: string, payload: ObjectValue) => Promise<ObjectValue>): Promise<net.Server> {
  const server = net.createServer(socket => {
    let buffer = Buffer.alloc(0), handled = false;
    socket.setTimeout(10_000, () => socket.destroy());
    socket.on("error", () => {});
    socket.on("data", chunk => {
      if (handled) { socket.destroy(); return; }
      if (buffer.length + chunk.length > MAX_FRAME) { handled = true; socket.destroy(); return; }
      buffer = Buffer.concat([buffer, chunk]);
      const newline = buffer.indexOf(10);
      if (newline < 0) return;
      handled = true;
      void (async () => {
        let id: unknown = null;
        try {
          if (buffer.subarray(newline + 1).toString("utf8").trim()) throw new Error("Only one JSONL request is allowed");
          const frame = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(buffer.subarray(0, newline)));
          if (!object(frame)) throw new Error("Invalid OMP IPC request");
          id = typeof frame.id === "string" ? frame.id : null;
          const supplied = Buffer.from(typeof frame.token === "string" ? frame.token : "");
          const expected = Buffer.from(token);
          if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected)) throw new Error("OMP IPC authentication rejected");
          if (frame.version !== 1 || !id || typeof frame.operation !== "string" || !object(frame.payload)) throw new Error("Invalid OMP IPC request");
          const data = await dispatch(frame.operation, frame.payload);
          const reply = JSON.stringify({ id, ok: true, data }) + "\n";
          if (Buffer.byteLength(reply) > MAX_FRAME) throw new Error("OMP response exceeds transport limit; request a smaller history page");
          socket.end(reply);
        } catch (error) {
          socket.end(JSON.stringify({ id, ok: false, error: error instanceof Error ? error.message : "OMP IPC request rejected" }) + "\n");
        }
      })().catch(() => socket.destroy());
    });
  });
  const { promise, resolve, reject } = Promise.withResolvers<void>();
  server.once("error", reject);
  server.listen(endpoint, () => { server.removeListener("error", reject); resolve(); });
  await promise;
  fs.chmodSync(endpoint, 0o600);
  server.on("error", () => {}); // A failed observer must never terminate the native TUI.
  server.unref();
  return server;
}

/** Public user events carry content/timestamp, not the bridge's request id. */
export function userFingerprint(message: unknown): string | undefined {
  if (!object(message) || message.role !== "user" || message.synthetic || message.attribution === "agent") return undefined;
  const content = message.content;
  let text: string;
  const images: { data: string; mimeType: string }[] = [];
  if (typeof content === "string") text = content;
  else if (Array.isArray(content)) {
    const texts: string[] = [];
    for (const block of content) {
      if (!object(block)) return undefined;
      if (block.type === "text" && typeof block.text === "string") texts.push(block.text);
      else if (block.type === "image" && typeof block.data === "string" && typeof block.mimeType === "string") images.push({ data: block.data, mimeType: block.mimeType });
      else return undefined;
    }
    text = texts.join("\n");
  } else return undefined;
  return createHash("sha256").update(JSON.stringify({ text, images })).digest("hex");
}

/** Remote configuration accepts catalog identities, never fuzzy patterns or model-role aliases. */
export function exactModel<T extends { provider: string; id: string }>(models: readonly T[], selector: unknown): T {
  if (typeof selector !== "string") throw new Error("OMP model must be an exact provider-qualified catalog identity");
  const model = models.find(candidate => `${candidate.provider}/${candidate.id}` === selector);
  if (!model) throw new Error("OMP model must be an exact provider-qualified catalog identity");
  return model;
}

type AdmissionTask = {
  sessionId: string; fingerprint: string; receiptId: string; submit: () => unknown;
  resolve: (data: ObjectValue) => void; reject: (error: Error) => void; cancel: () => void;
};

/** Serialize external submissions; only an observed native user event can confirm delivery. */
export class NativeAdmission {
  private queue: AdmissionTask[] = [];
  private active?: AdmissionTask;
  private uncertain = new Map<string, { sessionId: string; fingerprint: string; receiptId: string }>();
  private lateReceipts = new Map<string, ObjectValue>();
  constructor(private schedule: (timeout: () => void) => () => void) {}
  receipt(receiptId: string): ObjectValue | undefined { return this.lateReceipts.get(receiptId); }
  submit(sessionId: string, fingerprint: string, submit: () => unknown, receiptId = randomUUID()): Promise<ObjectValue> {
    if (this.uncertain.has(`${sessionId}:${fingerprint}`)) return Promise.reject(new Error("OMP identical send has an unknown earlier outcome; do not replay"));
    if (this.queue.length >= 256 || this.uncertain.size >= 256) return Promise.reject(new Error("OMP admission tracking capacity reached; no native message was sent"));
    const { promise, resolve, reject } = Promise.withResolvers<ObjectValue>();
    const task: AdmissionTask = { sessionId, fingerprint, receiptId, submit, resolve, reject, cancel: () => {} };
    task.cancel = this.schedule(() => {
      const wasActive = this.active === task;
      if (wasActive) {
        this.active = undefined;
        this.uncertain.set(`${task.sessionId}:${task.fingerprint}`, { sessionId: task.sessionId, fingerprint: task.fingerprint, receiptId: task.receiptId });
      }
      this.queue = this.queue.filter(queued => queued !== task);
      task.reject(new Error(wasActive
        ? "OMP send outcome unknown: native user admission was not observed; do not replay"
        : "OMP send expired before submission; no native message was sent"));
      this.pump();
    });
    this.queue.push(task);
    this.pump();
    return promise;
  }
  private pump(): void {
    if (this.active || !this.queue.length) return;
    const task = this.queue.shift()!;
    if (this.uncertain.has(`${task.sessionId}:${task.fingerprint}`) || this.uncertain.size >= 256) {
      task.cancel(); task.reject(new Error("OMP admission is unresolved or at capacity; no native message was sent"));
      this.pump(); return;
    }
    this.active = task;
    try {
      const result: unknown = task.submit();
      // Some OMP versions return void, others a promise. Neither is admission evidence.
      if (result instanceof Promise) result.catch(error => {
        if (this.active !== task) return;
        this.active = undefined; task.cancel();
        this.uncertain.set(`${task.sessionId}:${task.fingerprint}`, { sessionId: task.sessionId, fingerprint: task.fingerprint, receiptId: task.receiptId });
        task.reject(error instanceof Error ? error : new Error("OMP native send rejected"));
        this.pump();
      });
    } catch (error) {
      if (this.active !== task) return;
      this.active = undefined; task.cancel();
      // A synchronous API refusal returned no work to await: permit a new explicit request.
      task.reject(error instanceof Error ? error : new Error("OMP native send rejected before submission"));
      this.pump();
    }
  }
  observe(sessionId: string, message: unknown): void {
    if (!object(message) || typeof message.timestamp !== "number" && typeof message.timestamp !== "string") return;
    const fingerprint = userFingerprint(message);
    if (!fingerprint) return;
    const observed = { admitted: true, sessionId, admissionEvidence: { event: "message_start", timestamp: message.timestamp, contentHash: fingerprint } };
    const key = `${sessionId}:${fingerprint}`;
    const uncertain = this.uncertain.get(key);
    if (uncertain) {
      this.uncertain.delete(key);
      this.lateReceipts.set(uncertain.receiptId, observed);
      if (this.lateReceipts.size > 256) this.lateReceipts.delete(this.lateReceipts.keys().next().value!);
      // Late evidence updates the original receipt only; never submit or replay anything.
      return;
    }
    const task = this.active;
    if (!task || task.sessionId !== sessionId || fingerprint !== task.fingerprint) return;
    this.active = undefined; task.cancel(); task.resolve(observed);
    queueMicrotask(() => this.pump());
  }
  cancel(reason: string): void {
    const tasks = this.active ? [this.active, ...this.queue] : this.queue;
    if (this.active) this.uncertain.set(`${this.active.sessionId}:${this.active.fingerprint}`, { sessionId: this.active.sessionId, fingerprint: this.active.fingerprint, receiptId: this.active.receiptId });
    this.active = undefined; this.queue = [];
    for (const task of tasks) { task.cancel(); task.reject(new Error(reason)); }
  }
}

export default function (pi: ExtensionAPI): void {
  let ctx: ExtensionContext | undefined, server: net.Server | undefined;
  let folder = "", endpoint = "", metadata = "";
  const token = randomBytes(32).toString("hex"), instanceId = randomUUID();
  const history = new BranchHistory();
  let branchDirty = true, lastLeaf: string | null | undefined, sessionId = "", sessionFile: string | undefined;
  const pending = new Map<string, ObjectValue>();
  const admitted = new Map<string, { fingerprint: string; receiptId: string; result: Promise<ObjectValue> }>();
  const admission = new NativeAdmission(timeout => {
    if (!ctx) throw new Error("OMP TUI session unavailable");
    const owner = ctx;
    const timer = owner.setTimeout(timeout, 8_000);
    return () => owner.clearTimer(timer);
  });
  const root = (context: ExtensionContext) => context.mode === "tui" && context.agent.kind === "main";
  function sync(context: ExtensionContext): void {
    if (!root(context)) return;
    ctx = context;
    const nextId = context.sessionManager.getSessionId();
    const nextFile = context.sessionManager.getSessionFile();
    if (nextId !== sessionId || nextFile !== sessionFile) {
      admission.cancel("OMP session identity changed before native admission confirmation; do not replay");
      sessionId = nextId; sessionFile = nextFile; pending.clear(); admitted.clear(); branchDirty = true; history.streaming();
      if (server) publish();
    }
  }
  function publish(): void {
    if (!ctx || !metadata) return;
    const data = { version: 1, runtime: "omp", pid: process.pid, processStart: processStart(process.pid), instanceId, sessionId, sessionFile, cwd: ctx.cwd, terminal: terminalIdentity(), endpoint, token };
    const temporary = `${metadata}.${instanceId}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify(data), { mode: 0o600, flag: "wx" });
    try { fs.renameSync(temporary, metadata); } finally { safeUnlink(temporary); }
  }
  function refreshHistory(): void {
    if (!ctx) throw new Error("OMP TUI session unavailable");
    const leaf = ctx.sessionManager.getLeafId();
    if (branchDirty || leaf !== lastLeaf) {
      history.replace(sessionId, ctx.sessionManager.getBranch());
      branchDirty = false; lastLeaf = leaf;
    }
  }
  function state(): ObjectValue {
    if (!ctx) throw new Error("OMP TUI session unavailable");
    refreshHistory();
    const snapshot = ctx.getAsyncJobSnapshot();
    const working = !ctx.isIdle();
    const queued = ctx.hasPendingMessages();
    const pendingAsyncWork = snapshot ? snapshot.running.length > 0 || snapshot.delivery.queued > 0 || snapshot.delivery.delivering || snapshot.delivery.pendingJobIds.length > 0 : null;
    const plan = ctx.getPendingPlanReview();
    const pendingRequests = [...pending.values()];
    if (plan) pendingRequests.push({
      id: plan.reviewId, kind: "plan", title: plan.title, detail: plan.planContent,
      choices: plan.options.filter(option => !option.disabled).map(option => ({ id: option.id, label: option.label, value: { choice: option.id } })),
      answerable: true, tiers: plan.tiers, planFilePath: plan.planFilePath,
    });
    const model = ctx.models.current();
    return {
      sessionId, sessionFile, pid: process.pid, revision: history.revision,
      model: model && { id: model.id, provider: model.provider, name: model.name, contextWindow: model.contextWindow }, thinking: pi.getThinkingLevel(),
      working, settled: working || queued || pendingAsyncWork || pendingRequests.length ? false : null, pendingAsyncWork,
      asyncSnapshot: snapshot, jobs: snapshot ? [...snapshot.running, ...snapshot.recent] : [],
      subagents: snapshot ? [...snapshot.running, ...snapshot.recent].filter(job => job.type === "task") : [], pendingRequests,
      capabilities: { history: true, send: true, images: true, interrupt: true, configure: true, answer: true, fork: false, answerPlan: true, answerQuestion: false, answerToolApproval: false, permissions: false, settlement: false, slashCommands: false, asyncSnapshot: true },
      limitations: {
        fork: "Only command contexts can branch; no event-context fork API",
        answerQuestion: "Native ask answers are terminal-only",
        answerToolApproval: "Native tool approval events are observational only",
        permissions: "No public extension permission-policy setter",
        settlement: "ExtensionContext has no authoritative settled predicate",
        interrupt: "abort() requests native cancellation; no public await-cancellation API",
        slashCommands: "sendUserMessage submits literal text without input hooks or slash expansion",
        pendingRequests: "Question/tool approval events before extension loading cannot be queried",
        sendAdmission: "Observed native user content/images and timestamp, not a source-tagged request id; identical simultaneous terminal input cannot be uniquely attributed",
      },
    };
  }
  async function dispatch(operation: string, payload: ObjectValue): Promise<ObjectValue> {
    if (!ctx) throw new Error("OMP TUI session unavailable");
    sync(ctx);
    assertTarget(payload, { pid: process.pid, sessionId, instanceId, sessionFile });
    if (operation === "state" || operation === "discover") return state();
    if (operation === "history") { refreshHistory(); return history.page(payload); }
    if (operation === "models") {
      const current = ctx.models.current();
      return { models: ctx.models.list().map(model => ({ id: `${model.provider}/${model.id}`, label: model.name || model.id, efforts: ["off", ...(model.thinking?.efforts || [])], defaultEffort: model.thinking?.defaultLevel ?? null })), model: current ? `${current.provider}/${current.id}` : null, effort: pi.getThinkingLevel() ?? null, perms: [], mode: null };
    }
    if (operation === "close") return { detached: true, sessionId, pid: process.pid };
    if (operation === "fork") throw new Error("OMP fork is terminal-only: no public event-context fork API");
    if (operation === "interrupt") { ctx.abort(); return { requested: true, sessionId, working: !ctx.isIdle() }; }
    if (operation === "configure") {
      if (payload.perm !== undefined) throw new Error("OMP permission configuration has no public extension API; use the native terminal");
      const model = payload.model === undefined ? ctx.models.current() : exactModel(ctx.models.list(), payload.model);
      const efforts = ["off", ...(model?.thinking?.efforts || [])];
      if (payload.effort !== undefined && (typeof payload.effort !== "string" || !efforts.includes(payload.effort))) throw new Error("Unsupported thinking level for selected OMP model");
      const pinned = sessionId;
      // Native v18.18.3's extension action calls session.setModel(model) without `persist`;
      // ModelControls writes global defaults only when options.persist is explicitly true.
      if (payload.model !== undefined && model && !await pi.setModel(model)) throw new Error("OMP model change rejected by native credential policy");
      if (ctx.sessionManager.getSessionId() !== pinned) throw new Error("OMP session switched during model configuration; outcome must be rediscovered");
      if (payload.effort !== undefined) {
        pi.setThinkingLevel(payload.effort as Parameters<ExtensionAPI["setThinkingLevel"]>[0]);
        if (pi.getThinkingLevel() !== payload.effort) throw new Error("OMP native thinking policy did not apply requested level");
      }
      const current = ctx.models.current();
      if (payload.model !== undefined && model && (current?.id !== model.id || current.provider !== model.provider)) throw new Error("OMP model changed concurrently; rediscover applied state");
      return { model: current ? `${current.provider}/${current.id}` : null, effort: pi.getThinkingLevel() ?? null, sessionId };
    }
    if (operation === "answer") {
      const review = ctx.getPendingPlanReview();
      if (typeof payload.requestId !== "string" || !review || payload.requestId !== review.reviewId) throw new Error("OMP request is stale or terminal-only; question/tool approvals cannot be answered by extensions");
      const answer = typeof payload.answer === "string" ? { choice: payload.answer } : payload.answer;
      if (!object(answer) || typeof answer.choice !== "string" || !["execute", "compact", "keep", "refine", "save"].includes(answer.choice)) throw new Error("Plan answer requires {choice, feedback?, role?}");
      if (answer.feedback !== undefined && typeof answer.feedback !== "string" || answer.role !== undefined && typeof answer.role !== "string") throw new Error("Invalid OMP plan answer");
      const accepted = ctx.resolvePlanReview(review.reviewId, answer.choice as PlanReviewChoice, { feedback: answer.feedback as string | undefined, role: answer.role as string | undefined });
      if (!accepted) throw new Error("OMP plan review answer rejected: stale, disabled, or invalid choice/tier");
      return { accepted: true, requestId: review.reviewId, sessionId };
    }
    if (operation === "send") {
      const text = payload.text, delivery = payload.delivery, requestId = payload.requestId;
      if (typeof text !== "string") throw new Error("OMP send requires text or images");
      if (delivery !== undefined && delivery !== "prompt" && delivery !== "steer" && delivery !== "followUp") throw new Error("Invalid OMP delivery mode");
      if (requestId !== undefined && (typeof requestId !== "string" || !requestId || requestId.length > 256)) throw new Error("Invalid OMP send request identity");
      const images: { type: "image"; data: string; mimeType: string }[] = [];
      if (payload.images !== undefined) {
        if (!Array.isArray(payload.images) || payload.images.length > 32) throw new Error("Invalid OMP image attachments");
        for (const image of payload.images) {
          if (!object(image) || typeof image.data !== "string" || typeof image.mimeType !== "string" || !/^image\/[a-zA-Z0-9.+-]+$/.test(image.mimeType) || !/^[A-Za-z0-9+/]*={0,2}$/.test(image.data)) throw new Error("Invalid OMP image attachment");
          images.push({ type: "image", data: image.data, mimeType: image.mimeType });
        }
      }
      if (!text.trim() && !images.length) throw new Error("OMP send requires text or images");
      const fingerprint = createHash("sha256").update(JSON.stringify({ text, images, delivery })).digest("hex");
      const previous = typeof requestId === "string" ? admitted.get(requestId) : undefined;
      if (previous) {
        if (previous.fingerprint !== fingerprint) throw new Error("OMP send request identity was reused with different content");
        const late = admission.receipt(previous.receiptId);
        if (late) return { ...late, requestId, delivery: delivery || "prompt", slashExpansion: false };
        return previous.result;
      }
      if (admitted.size >= 4096) throw new Error("OMP session send identity capacity reached; use the native terminal");
      const content = images.length ? [{ type: "text" as const, text }, ...images] : text;
      const expectedFingerprint = userFingerprint({ role: "user", content })!;
      const receiptId = randomUUID();
      const result = admission.submit(sessionId, expectedFingerprint, () => {
        if (!ctx) throw new Error("OMP TUI session unavailable");
        sync(ctx);
        assertTarget(payload, { pid: process.pid, sessionId, instanceId, sessionFile });
        return pi.sendUserMessage(content, delivery === "steer" || delivery === "followUp" ? { deliverAs: delivery } : undefined);
      }, receiptId).then(observed => ({ ...observed, requestId, delivery: delivery || "prompt", slashExpansion: false }));
      if (typeof requestId === "string") admitted.set(requestId, { fingerprint, receiptId, result });
      return result;
    }
    throw new Error(`Unsupported OMP operation: ${operation}`);
  }
  pi.on("session_start", async (_event, context) => {
    if (!root(context)) return;
    sync(context);
    if (server) return;
    folder = stateDirectory(); privateDirectory(folder);
    endpoint = path.join(folder, `${process.pid}-${instanceId.slice(0, 8)}.sock`);
    if (Buffer.byteLength(endpoint) > 103) throw new Error("OMP private socket path is too long; set a shorter SHEPHERDR_HOME");
    metadata = path.join(folder, `${process.pid}.json`);
    // Remove only dead-process endpoints described by private, valid metadata in our directory.
    for (const name of fs.readdirSync(folder)) {
      if (!/^\d+\.json$/.test(name)) continue;
      const file = path.join(folder, name);
      try {
        const info = fs.lstatSync(file);
        if (!info.isFile() || info.uid !== process.getuid?.() || info.mode & 0o077 || info.size > 65536) continue;
        const old = JSON.parse(fs.readFileSync(file, "utf8"));
        if (!Number.isInteger(old.pid) || old.pid < 1 || name !== `${old.pid}.json`) continue;
        try { process.kill(old.pid, 0); continue; } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ESRCH") continue; }
        if (typeof old.endpoint === "string" && path.dirname(old.endpoint) === folder && /^\d+-[a-f0-9]{8}\.sock$/.test(path.basename(old.endpoint))) safeUnlink(old.endpoint, true);
        safeUnlink(file);
      } catch { /* Untrusted/stale metadata never authorizes broad cleanup. */ }
    }
    server = await createBridgeServer(endpoint, token, dispatch);
    try { publish(); } catch (error) { server.close(); safeUnlink(endpoint, true); server = undefined; throw error; }
  });
  const branchChanged = (_event: unknown, context: ExtensionContext): void => {
    if (!root(context)) return;
    sync(context); branchDirty = true; history.streaming();
    if (server) publish();
  };
  pi.on("session_switch", branchChanged);
  pi.on("session_branch", branchChanged);
  pi.on("session_tree", branchChanged);
  pi.on("session_compact", branchChanged);
  pi.on("message_start", (event, context) => {
    if (!root(context)) return;
    sync(context);
    admission.observe(context.sessionManager.getSessionId(), event.message);
  });
  pi.on("message_update", (event, context) => { if (root(context)) { sync(context); history.streaming({ ...event.message }); } });
  pi.on("message_end", (_event, context) => { if (root(context)) { sync(context); branchDirty = true; history.streaming(); } });
  pi.on("tool_approval_requested", (event, context) => {
    if (!root(context)) return; sync(context);
    pending.set(event.toolCallId, { id: event.toolCallId, kind: "permission", title: `${event.toolName} approval`, detail: event.reason, choices: [], answerable: false });
  });
  pi.on("tool_approval_resolved", (event, context) => { if (root(context)) pending.delete(event.toolCallId); });
  pi.on("tool_execution_start", (event, context) => {
    if (!root(context) || event.toolName !== "ask") return; sync(context);
    pending.set(event.toolCallId, { id: event.toolCallId, kind: "question", title: "OMP question", choices: [], questions: object(event.args) && Array.isArray(event.args.questions) ? event.args.questions : [], answerable: false });
  });
  pi.on("tool_execution_end", (event, context) => { if (root(context)) pending.delete(event.toolCallId); });
  pi.on("session_shutdown", (_event, context) => {
    if (!root(context)) return;
    admission.cancel("OMP session shut down before native admission confirmation; do not replay");
    server?.close(); server = undefined;
    safeUnlink(endpoint, true);
    try { if (JSON.parse(fs.readFileSync(metadata, "utf8")).instanceId === instanceId) safeUnlink(metadata); } catch {}
    ctx = undefined;
  });
}
