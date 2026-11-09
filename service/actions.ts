// What a web page may do, each the same way the window does it (the shared core, hc and herdr-task): send the
// person's words to an agent, stop it, mark an Assistant read, accept a Ready task, read a task's evidence.
// Anything else is refused. Inputs are checked here; commands run with argument lists, never a shell line.
import type { Hub } from "./hub.ts";
import { dirname, join } from "node:path";
import { appHome, writable } from "./paths.ts";
import { appendFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { sendBody, sendNative, steerNow, messageBody, refusal, reactionLine, mentionedIn, taskTag } from "../core/send.js";
import { kindOf } from "../core/chat-reader.js";
import { NATIVE_KINDS, isNative, nativeRequest, nativeTarget, nativeQuestionId, runtimeName } from "../core/native-runtime.js";
import { nativeState, nativeAnswer } from "./native.ts";
import { WORDS } from "../core/chat-view.js";
import * as AGa from "../core/agents.js";
import { answerCodex } from "./codex-ask.ts";
const { sessionOf, forkArgv } = AGa as any;
import * as SP from "../core/spaces.js";
const { READ, withMove, withSection, withoutSection, withSectionName, withIcon, withMachineLook, withOrder, withMachineOrder, DELETED, deletedSection, withRestoredSection } = SP as any;
import type { Compat } from "./compat.ts";
import { worktreeAct } from "./worktrees.ts";
import { noteFailed, once } from "./send-once.ts";
import { execTemplate, nativeAssistantCommand } from "./spaces.ts";
import type { NativeAssistantCommand } from "./spaces.ts";
import * as MA from "../core/machine-add.js";
import { exec, sh as modelSh } from "./model.ts";
import { closeSaved, renameSaved, orderSaved, withoutSpace, splitId } from "./saved-spaces.ts";

// The window's own space log (lib/spacelog.js): a space the service closed or renamed with no window is traced there too
export function logSpace(action: string, id: string, name: string | null, extra: Record<string, unknown> = {}) {
  try {
    // where at each call, and never the real one under test (SHEP-131: a test once logged its fake spaces there)
    const file = writable(join(appHome(), "data", "backups", "space-log.jsonl"));
    mkdirSync(dirname(file), { recursive: true });
    appendFileSync(file, JSON.stringify({ at: new Date().toISOString(), machine: splitId(id).machine, space: id, name, action, ...extra, by: "web, no window (service/saved-spaces.ts)" }) + "\n");
  } catch {}
}
const attachDir = () => join(appHome(), "data", "chat-attach");
/** files in one message at most (the page says so before it sends: web/plugins/chat/client.tsx) */
export const MAX_FILES = 50;
const ADDR = /^([\w.-]+\/)?t[0-9a-z]+$/;
const REF = /^[A-Z][A-Z0-9]*-\d+$/;
const MACHINE = /^[\w.-]+$/;

function runtimeIO(hub: Hub, compat: Compat, address: string) {
  const reader = () => compat.reader?.(address);
  return {
    hc: asPerson(hub),
    sh: (script: string, args: string[], on: string | null) => modelSh(hub, script, args, on),
    sleep: (ms: number) => Bun.sleep(ms),
    status: () => hub.terminals.get(address)?.status?.state ?? null,
    target: () => nativeTarget(hub.terminals.get(address), reader()),
    native: () => reader()?.nativeState ?? reader()?.native,
    machine: address.includes("/") ? address.split("/")[0] : null,
  };
}

async function nativeAction(hub: Hub, compat: Compat, address: string, operation: string, payload: Record<string, unknown> = {}) {
  const terminal = hub.terminals.get(address), reader = compat.reader?.(address);
  if (!terminal || terminal.state !== "live") throw new Error("That native agent isn't running.");
  const kind = kindOf(terminal) ?? reader?.kind;
  if (!isNative(kind)) throw new Error("That session is not a native runtime.");
  const io = runtimeIO(hub, compat, address);
  const target = nativeTarget(terminal, reader);
  if (!target.sessionId) {
    if (reader) throw new Error("This native conversation's identity is unavailable; reopen it before controlling it.");
    const state = await nativeRequest(io, kind, "state", target, io.machine);
    if (typeof state.sessionId !== "string" || !state.sessionId) throw new Error("The native owner did not confirm its session identity.");
    target.sessionId = state.sessionId;
  }
  return nativeRequest(io, kind, operation, { ...target, ...payload }, io.machine);
}

/** Readiness probes are retryable; the first native prompt below is admitted only once. */
async function startedNativeTarget(hub: Hub, compat: Compat, address: string, kind: string) {
  const io = runtimeIO(hub, compat, address);
  const deadline = Date.now() + 30_000;
  let reason = "The native terminal has not published its session yet.";
  while (Date.now() < deadline) {
    try {
      const current = await hc(["get", address, "--json"]);
      if (current.code !== 0) throw new Error((current.stderr || current.stdout).trim());
      const terminal: unknown = JSON.parse(current.stdout);
      const target = nativeTarget(terminal);
      if (!target.pid) throw new Error("The native terminal has no foreground owner yet.");
      const state = nativeState(await nativeRequest(io, kind, "state", target, io.machine));
      if (!state) throw new Error("The native owner has not confirmed its session identity.");
      return { io, target: { ...target, sessionId: state.sessionId, sessionFile: state.sessionFile } };
    } catch (error) {
      reason = error instanceof Error ? error.message : String(error);
      await Bun.sleep(200);
    }
  }
  throw new Error(`The native Assistant started, but its first message was not sent: ${reason}`);
}

/** One page's message to its agent, its files' paths after its words. -> { ok, at } once it is in the agent's terminal
 *  (Claude Code's box clear: core/send.js settleBox), or { waiting } held for the agent's turn; else it throws. */
async function sendNow(hub: Hub, b: any, compat: Compat, again = false) {
  const a = addr(b.addr);
  const text = str(b.text ?? "").trim();
  if (!text && !(Array.isArray(b.attachments) && b.attachments.length)) throw new Error("nothing to send");
  const t = hub.terminals.get(a);
  if (!t || t.state !== "live") throw new Error("That agent isn't running.");
  const io = runtimeIO(hub, compat, a);
  // files sent with it: their paths after the words, as the window's chat sends them (copied first to another machine)
  const sent: string[] = Array.isArray(b.attachments) ? b.attachments : [];
  // never some of them quietly
  if (sent.length > MAX_FILES) throw new Error(`At most ${MAX_FILES} files in one message.`);
  for (const p of sent) if (typeof p !== "string" || !p.startsWith(attachDir() + "/") || p.includes("/../")) throw new Error("That file isn't one sent from here.");
  // all at once
  const files = await Promise.all(sent.map(async (p) => {
    if (!a.includes("/")) return p;
    const put = await hc(["put", a, p, "--json"]);
    let landed: string | null = null;
    try { landed = JSON.parse(put.stdout || "{}").path ?? null; } catch {}
    if (put.code !== 0 || !landed) throw new Error("A file didn't reach that machine.");
    return landed;
  }));
  const body = [messageBody({ text, quote: typeof b.quote === "string" ? b.quote.slice(0, 2000) : null, tags: typeof b.tags === "string" ? b.tags : "" }), ...files].filter(Boolean).join("\n\n");
  const kind = kindOf(t);
  if (isNative(kind)) {
    const at = Date.now();
    const result = await sendNative(io, a, kind, body, { files, ...(b.hold ? { delivery: "followUp" } : {}),
      ...(again ? { again: true } : {}), ...(typeof b.cid === "string" ? { requestId: b.cid.slice(0, 80) } : {}) });
    if (result.code !== 0) throw new Error(result.stderr || "Native message admission was not confirmed; inspect its native terminal before sending again.");
    if (typeof b.cid === "string") compat.noteSend?.(a, { cid: b.cid.slice(0, 80), text, files: sent, at });
    return { ok: true, at };
  }
  // held on purpose (Ctrl+Enter, a long press): it goes once the agent stops working
  const st = (hub.terminals.get(a) as any)?.status?.state;
  if (b.hold && (st === "working" || st === "blocked")) {
    const cid = typeof b.cid === "string" ? b.cid.slice(0, 80) : undefined;
    // once it goes: the chat knows it again by its words (its id and files), so the page sees it went in
    const note = () => { if (cid) compat.noteSend?.(a, { cid, text, files: sent, at: Date.now() }); };
    held.push({ addr: a, kind: kindOf(t) ?? "", body, hub, cid, note });
    drain();
    return { ok: true, waiting: true, waitsFor: "turn" };
  }
  // when it began to go in, by this clock: the page knows it again in the chat by it (web/src/outbox.ts sameAs)
  const at = Date.now();
  // what it was, so the chat knows it again while it waits in the CLI's queue
  if (typeof b.cid === "string") compat.noteSend?.(a, { cid: b.cid.slice(0, 80), text, files: sent, at });
  const r: any = await sendBody(io, a, kindOf(t) ?? "", body, again);
  // (stuck: it stayed in Claude Code's box; sent again with this id it gets only its Enter: send-once.ts)
  if (r.code !== 0) throw Object.assign(new Error(refusal(r).message), { stuck: !!r.stuck });
  return { ok: true, at };
}

// Messages held until their agent is free, sent in order, each once.
const held: { addr: string; kind: string; body: string; hub: Hub; cid?: string; now?: boolean; note?: () => void }[] = [];
let draining: ReturnType<typeof setInterval> | null = null, busy = false;
function drain() {
  draining ??= setInterval(async () => {
    // one pass at a time: a slow send never overlaps the next tick
    if (busy) return;
    busy = true;
    try {
      for (const m of [...held]) {
        const st = (m.hub.terminals.get(m.addr) as any)?.status?.state;
        const alive = m.hub.terminals.get(m.addr)?.state === "live";
        if (alive && !m.now && (st === "working" || st === "blocked")) continue;
        const i = held.indexOf(m);
        if (i < 0) continue;
        held.splice(i, 1);
        // one that can't go now is told to its page (Chat.failed: Not sent, with Retry), never dropped quietly
        if (alive) m.note?.();
        const r: any = alive
          ? await sendBody({ hc: asPerson(m.hub), sleep: (ms: number) => Bun.sleep(ms), status: () => (m.hub.terminals.get(m.addr) as any)?.status?.state ?? null }, m.addr, m.kind, m.body).catch((e: Error) => ({ code: 1, stderr: e.message }))
          : { code: 1, stderr: "That agent isn't running." };
        if (r.code !== 0 && m.cid) noteFailed(m.addr, m.cid, refusal(r).message, !!r.stuck);
      }
    } finally {
      busy = false;
    }
    if (!held.length && draining) { clearInterval(draining); draining = null; }
  }, 1000);
}

// An Assistant's first message for a space whose machine is offline (the window's chat does the same): kept here and
// sent again, so the Assistant starts there once the machine is back. One per space, the latest words; a day at most.
const starts = new Map<string, { b: any; hub: Hub; compat: Compat; at: number }>();
let startTimer: ReturnType<typeof setInterval> | null = null;
function startWhenBack() {
  startTimer ??= setInterval(async () => {
    const up = new Set((await addedMachines()).filter((m: any) => m.state === "connected").map((m: any) => m.name));
    for (const [space, w] of [...starts]) {
      const machine = space.split("/")[0];
      if (Date.now() - w.at > 86_400_000) starts.delete(space);
      else if (up.has(machine)) { starts.delete(space); await act(w.hub, w.b, w.compat).catch(() => null); }
    }
    if (!starts.size && startTimer) { clearInterval(startTimer); startTimer = null; }
  }, 15_000);
}

// An Assistant whose CLI showed a screen of its own (trust this folder) before hc could hand it its first message: the
// template's words and the task go once it is idle past that screen, as the window's chat does (mods/chat follow()).
function firstWhenIdle(hub: Hub, addr: string, text: string) {
  const until = Date.now() + 15 * 60_000;
  const timer = setInterval(async () => {
    const t: any = hub.terminals.get(addr);
    if (!t || t.state !== "live" || (t.turn ?? 0) > 0 || Date.now() > until) return clearInterval(timer);
    if (t.status?.state !== "idle") return;
    clearInterval(timer);
    await hub.send(addr, text);
  }, 2000);
}

interface CommandResult { code: number; stdout: string; stderr: string }
async function run(cmd: string[]) {
  // a program that isn't there (herdr-app on a web-only install) fails as any other: its code, not a throw
  let p: ReturnType<typeof Bun.spawn<"ignore", "pipe", "pipe">>;
  // the window's herdr-app: SHEPHERDR_HERDR_APP in tests (as SHEPHERDR_HC for hc)
  if (cmd[0] === "herdr-app" && process.env.SHEPHERDR_HERDR_APP) cmd = [process.env.SHEPHERDR_HERDR_APP, ...cmd.slice(1)];
  try { p = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe", stdin: "ignore" }); }
  catch (e) { return { code: 127, stdout: "", stderr: String(e) }; }
  const [stdout, stderr] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()]);
  return { code: await p.exited, stdout, stderr };
}

export const hc = (args: string[]) => run([process.env.SHEPHERDR_HC || "hc", ...args]);

// hc for core/send.js, its sends as the person's (Hub.send: no <hc from=…> tag on another machine)
const asPerson = (hub: Hub) => (args: string[]) => args[0] === "send" ? hub.send(args[1], args[2]) : hc(args);

/** The machines added (hc machine ls), as Add machine marks them: not the temporary ones. */
async function addedMachines() {
  const r = await hc(["machine", "ls", "--json"]).catch(() => null);
  try { return r?.code === 0 ? (JSON.parse(r.stdout).machines ?? []).filter((m: any) => !m.temporary) : []; } catch { return []; }
}
const task = (args: string[], folder: string, machine: string | null) =>
  run(["herdr-task", ...(machine ? ["--on", machine] : []), ...args, "--folder", folder, "--json"]);

function str(v: unknown, max = 100_000) {
  if (typeof v !== "string" || v.length > max) throw new Error("bad input");
  return v;
}
function addr(v: unknown) {
  const a = str(v, 80);
  if (!ADDR.test(a)) throw new Error("bad terminal");
  return a;
}
function taskOf(b: any) {
  const ref = str(b.ref, 40), folder = str(b.folder, 4096), machine = b.machine == null ? null : str(b.machine, 80);
  if (!REF.test(ref) || !folder.startsWith("/") || (machine && !MACHINE.test(machine))) throw new Error("bad task");
  return { ref, folder, machine };
}

type Known = { kind: string; name: string; installed: boolean; run?: string[] };
/** The agents a machine has, as the window's knownAgents() gives its + menu and Switch agent: the installed ones with
 *  their command, then the first two that aren't. Only the window can ask Herdr: it keeps them in its shared
 *  "machines.stats" (lib/machines.js). Without the window, Claude Code and Codex, looked for on that machine. */
async function knownAgents(hub: Hub, compat: Compat, machine: string | null): Promise<Known[]> {
  const raw: unknown = compat.shared?.value("machines.stats");
  const recorded: Known[] = [];
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    const rows = raw as Record<string, unknown>;
    const selected = rows[machine ?? ""];
    if (selected && typeof selected === "object" && !Array.isArray(selected)) {
      const stats = selected as Record<string, unknown>;
      for (const [field, installed] of [["agents", true], ["others", false]] as const) {
        for (const entry of Array.isArray(stats[field]) ? stats[field] : []) {
          if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
          const agent = entry as Record<string, unknown>;
          if (typeof agent.key !== "string" || typeof agent.name !== "string") continue;
          const command = Array.isArray(agent.run) && agent.run.every((part): part is string => typeof part === "string") ? agent.run : undefined;
          recorded.push({ kind: agent.key, name: agent.name, installed, ...(command ? { run: command } : {}) });
        }
      }
    }
  }
  const AGENTS: Record<string, { name: string; run: string[] }> = {
    claude: { name: "Claude Code", run: ["claude"] }, codex: { name: "Codex", run: ["codex"] },
    ...Object.fromEntries(NATIVE_KINDS.map((kind) => [kind, { name: runtimeName(kind), run: [kind] }])),
  };
  const missing = Object.keys(AGENTS).filter((kind) => !recorded.some((agent) => agent.kind === kind));
  const r = missing.length ? await exec(hub, ["sh", "-c", 'for c in "$@"; do command -v "$c" >/dev/null 2>&1 && echo "$c"; done', "agents", ...missing], machine).catch(() => null) : null;
  const have = new Set((r?.stdout ?? "").split("\n").filter(Boolean));
  return [...recorded, ...missing.map((kind) => ({ kind, name: AGENTS[kind].name, installed: have.has(kind), run: AGENTS[kind].run }))];
}

export async function act(hub: Hub, b: any, compat: Compat): Promise<unknown> {
  // 2.1's screen names a chat by its key (compat.ts): its terminal here.
  const chat = (key: unknown) => {
    const a = typeof key === "string" ? compat.addrOf(key) : null;
    if (!a) throw new Error("That chat isn't here any more.");
    return a;
  };
  if (b?.type === "answer" && typeof b.question === "string" && b.question.startsWith("scr-")) {
    // a CLI's start screen (service/watch.ts): a tap presses that choice, only on the screen the card was drawn from;
    // "Answer in terminal" just shows the terminal
    if (b.terminal === true) return { ok: true };
    const why = await (compat as any).watch?.press(str(b.question, 300), typeof b.choice === "number" ? b.choice : -1);
    return why ? { ok: false, error: why } : { ok: true };
  }
  if (b?.type === "answer" && typeof b.question === "string" && b.question.startsWith("cq:")) {
    // Codex's own questions (compat.ts codexQuestion): answered on its screen, as a person would
    const a = chat(b.chat);
    const item = (compat.reader?.(a)?.conv?.items ?? []).find((x: any) => x.kind === "ask" && x.id === b.question.slice(3));
    if (!item?.questions?.length || item.closed) throw new Error("That question was answered already.");
    const why = await answerCodex(hc, a, item.questions, b);
    if (why) throw new Error(why);
    return { ok: true };
  }
  if (b?.type === "answer" && typeof b.question === "string" && b.question.startsWith("native:")) {
    const address = chat(b.chat), reader = compat.reader?.(address);
    const state = nativeState(reader?.nativeState ?? reader?.native);
    const request = state?.pendingRequests.find((q) => nativeQuestionId(state.sessionId, q.id) === b.question);
    if (!state || !request) throw new Error("That native question is no longer open.");
    if (b.terminal === true) {
      const shown = await run(["herdr-app", "do", "terminal.show", "--terminal", address]);
      if (shown.code !== 0) throw new Error("Open this agent's native terminal to answer.");
      return { ok: true, terminal: true };
    }
    const answer = nativeAnswer(request, b);
    await nativeAction(hub, compat, address, "answer", { requestId: request.id, answer });
    return { ok: true };
  }
  if (b?.type === "answer" && typeof b.question === "string") {
    // a hook ask's card (its id is the request's): answered as the window's card answers it
    if (!compat.asks) throw new Error("No questions here.");
    await (compat.asks as any).answer(str(b.question, 200), { choice: typeof b.choice === "number" ? b.choice : undefined,
      choices: Array.isArray(b.choices) ? b.choices.filter((n: unknown) => typeof n === "number") : undefined,
      note: typeof b.note === "string" ? b.note.slice(0, 4000) : undefined, other: typeof b.other === "string" ? b.other.slice(0, 4000) : undefined,
      answers: Array.isArray(b.answers) ? b.answers.slice(0, 20).map((x: unknown) => String(x ?? "").slice(0, 4000)) : undefined,
      terminal: b.terminal === true, talk: b.talk === true, dismiss: b.dismiss === true });
    return { ok: true };
  }
  if (b?.type === "react" && typeof b.chat === "string" && typeof b.message === "string" && typeof b.emoji === "string" && b.emoji.length <= 16) {
    // a reaction toggles, as the window's: added unless it is yours already; the agent reads the core's reaction line
    const a = chat(b.chat), r = compat.reader?.(a);
    const item = r?.conv?.items?.find((x: any) => x.id === b.message);
    if (!item?.text) throw new Error("That message isn't in the chat any more.");
    const mine = (r.conv.reactions.get(b.message) ?? []).some((x: any) => x.by === "you" && x.emoji === b.emoji);
    if ((hub.terminals.get(a) as any)?.status?.state === "blocked") throw new Error("It's asking you something: answer that first.");
    const res = await hub.send(a, reactionLine(b.emoji, item.text, !mine));
    if (res.code !== 0) throw new Error(refusal(res).message);
    return { ok: true };
  }
  // the person's shared choices (pins, the sidebar, icons): in the service's document, in the window's own format
  const sh = compat.shared;
  if (b?.type === "pin" && typeof b.chat === "string" && b.chat.endsWith("/worker") && sh) {
    const t = compat.taskOf(b.chat.slice(0, -7));
    if (!t) throw new Error("That task isn't here any more.");
    const now = new Set<string>(Array.isArray(sh.value("tasks.pinned")) ? sh.value("tasks.pinned") : []);
    if (b.on) now.add(t.pin); else now.delete(t.pin);
    await sh.set("tasks.pinned", [...now]);
    return { ok: true };
  }
  const bar = () => sh!.value("spaces-chat") ?? {};
  const sec = (id: unknown) => (typeof id === "string" && id.startsWith("s:") ? id.slice(2) : null);
  if (b?.type === "move" && typeof b.id === "string" && typeof b.to === "string" && sh) {
    const to = b.to === "pinned" ? "pinned" : sec(b.to);
    await sh.set("spaces-chat", withMove(bar(), b.id, to, typeof b.before === "string" ? b.before : null));
    return { ok: true };
  }
  if (b?.type === "order" && sh && typeof b.under === "string" && Array.isArray(b.ids)) {
    // a dragged sidebar list, kept where the window keeps it: Pinned's and each section's spaces, and the sections'
    // order, in the shared sidebar data; a machine's own spaces in the window's order (space.move)
    const ids = b.ids.filter((x: unknown) => typeof x === "string").slice(0, 500) as string[];
    const d = structuredClone(bar());
    d.sections ??= []; d.pinned ??= [];
    const same = (a: string[], c: string[]) => a.length === c.length && a.every((x) => c.includes(x));
    if (b.under === "pinned") { if (!same(ids, d.pinned)) throw new Error("Pinned changed meanwhile."); d.pinned = ids; }
    else if (sec(b.under)) {
      const x = d.sections.find((y: any) => y.id === sec(b.under));
      if (!x || !same(ids, x.spaces ?? [])) throw new Error("That section changed meanwhile.");
      x.spaces = ids;
    } else if (b.under === "sections" || b.under.startsWith("sections:")) {
      // the sections at the top (Pinned among them, which isn't one of the data's) or a machine's: their new order
      const order = ids.map(sec).filter(Boolean) as string[];
      d.sections = [...order.map((id) => d.sections.find((y: any) => y.id === id)).filter(Boolean), ...d.sections.filter((y: any) => !order.includes(y.id))];
    } else if (b.under.startsWith("m:")) {
      // the order is the sidebar's own (core/spaces.js withOrder), so it holds on every device and for spaces Herdr
      // doesn't keep (the service's own, always last before SHEP-130); the rest of the machine's stay where they were
      const key = b.under.slice(2), machine = key === compat.host ? null : key;
      const all: any[] = compat.allSpaces?.() ?? [];
      const now = all.filter((s) => (s.machine ?? null) === machine).map((s) => s.id);
      await sh.set("spaces-chat", withOrder(bar(), machine, ids, now));
      // and Herdr's own order too, best effort, for the window and a release before this one: its spaces only
      const own = new Set(all.filter((s) => s.own).map((s) => s.id));
      const mine = ids.filter((id) => !own.has(id));
      try {
        for (const [i, id] of mine.entries()) {
          const r = await run(["herdr-app", "do", "space.move", "--id", id, "--index", String(i + 1)]);
          if (r.code !== 0) {
            // no window: the machine's saved spaces in that order (service/saved-spaces.ts)
            await orderSaved(hub, machine, mine);
            break;
          }
        }
      } catch {}
      return { ok: true };
    } else if (b.under === "all") {
      // the machines (m:<key>, this computer by its name): their order in the sidebar's data, "" for this computer, so
      // every device and the window show it (core/spaces.js withMachineOrder, SHEP-153)
      const keys = ids.filter((id) => id.startsWith("m:")).map((id) => (id.slice(2) === compat.host ? "" : id.slice(2)));
      await sh.set("spaces-chat", withMachineOrder(bar(), keys));
      return { ok: true };
    } else throw new Error("That list keeps its order.");
    await sh.set("spaces-chat", d);
    return { ok: true };
  }
  if (b?.type === "section" && sh) {
    if (b.op === "new" && typeof b.name === "string" && b.name.trim()) {
      const machine = typeof b.machine === "string" && b.machine !== compat.host ? b.machine : null;
      await sh.set("spaces-chat", withSection(bar(), { id: `sec-${Date.now().toString(36)}`, name: b.name.trim().slice(0, 60), machine, icon: typeof b.emoji === "string" ? b.emoji.slice(0, 16) : null }));
      return { ok: true };
    }
    if (b.op === "delete" && sec(b.id)) {
      // as the window's: at once, kept for its toast's Undo (shared "spaces-chat.deleted")
      const gone = deletedSection(bar(), sec(b.id));
      if (gone) await sh.set(DELETED, gone);
      await sh.set("spaces-chat", withoutSection(bar(), sec(b.id)));
      return { ok: true };
    }
    if (b.op === "restore") {
      const gone = sh.value(DELETED);
      if (!gone?.section) return { ok: false, error: "There is nothing to undo." };
      await sh.set(DELETED, null);
      await sh.set("spaces-chat", withRestoredSection(bar(), gone));
      return { ok: true };
    }
  }
  if (b?.type === "rename" && typeof b.id === "string" && typeof b.name === "string" && sh) {
    const name = b.name.trim().slice(0, 80);
    const icon = typeof b.emoji === "string" ? b.emoji.slice(0, 16) || null : undefined;
    if (sec(b.id)) { await sh.set("spaces-chat", withSectionName(bar(), sec(b.id), name, icon)); return { ok: true }; }
    if (b.id.startsWith("m:")) {
      // a machine's own name and emoji in the sidebar, as the window's Rename… (this computer is the snapshot's host)
      const key = b.id.slice(2);
      await sh.set("spaces-chat", withMachineLook(bar(), key === compat.host ? null : key, name, icon ?? null));
      return { ok: true };
    }
    // a space: its icon is ours; its name is Herdr's (the window renames it, when it runs)
    if (icon !== undefined) await sh.set("spaces-chat", withIcon(bar(), b.id, icon));
    // an empty name gives it Herdr's own back, as the window's Rename dialog does
    if (name || b.unname === true) {
      const r = await run(["herdr-app", "do", "space.rename", "--id", b.id, "--name", name]);
      // no window: its saved name (Herdr's own name back is the window's to give)
      if (r.code !== 0) {
        if (!name) throw new Error("Giving a space Herdr's own name back needs the Herdr window open.");
        const done = await renameSaved(hub, b.id, name);
        if (!done) throw new Error("That space isn't here any more.");
        logSpace("rename", b.id, done.was, { to: name });
      }
    }
    return { ok: true };
  }
  if (b?.type === "mode" && typeof b.place === "string" && (b.mode === "autopilot" || b.mode === "regular")) {
    // Autopilot: herdr-task's, per space (a plain switch, as the window's header); another machine's ("vm/sp1") is
    // switched by herdr-task there, told its folder from here, and its own scheduler wakes it
    if (!/^([\w.-]+\/)?[\w-]+$/.test(b.place)) throw new Error("bad space");
    const folder = compat.spaceOf(b.place)?.folder;
    const r = await run(["herdr-task", "autopilot", b.mode === "autopilot" ? "on" : "off", "--space", b.place,
      ...(b.place.includes("/") && folder ? ["--space-folder", folder] : []), "--json"]);
    if (r.code !== 0) throw new Error((r.stderr || r.stdout).trim().split("\n").pop() || "Autopilot didn't change.");
    return { ok: true };
  }
  if (b?.type === "model-pin" && compat.agents && ["claude", "codex", ...NATIVE_KINDS].includes(b.agent) && typeof b.model === "string" && /^[\w./:@\[\]-]{1,200}$/.test(b.model)) {
    // the model menu's star: pinned to the top of its agent's list (core/agents.js PICKS_FILE, as the window's)
    return { ok: true, picks: (compat.agents as any).pin(b.agent, b.model) };
  }
  if ((b?.type === "restart-info" || b?.type === "model-menu" || b?.type === "restart") && typeof b.chat === "string" && compat.agents) {
    // the model menu: what it runs on and its choices (model-menu: the window's, mods/chat-models); a restart on
    // another model, effort, speed, permission mode or CLI
    const a = chat(b.chat), reader = compat.reader?.(a);
    if (b.type === "restart-info") return compat.agents.info(a, reader);
    if (b.type === "model-menu") return { ok: true, menu: await (compat.agents as any).menu(a, reader) };
    if (!["claude", "codex", ...NATIVE_KINDS].includes(b.kind)) throw new Error("Pick an installed agent runtime.");
    const opt = (v: unknown) => (typeof v === "string" && /^[\w./:@\[\]-]{1,200}$/.test(v) ? v : undefined);
    const next = await compat.agents.restart(a, reader, { kind: b.kind, model: opt(b.model), effort: opt(b.effort), speed: opt(b.speed), perm: opt(b.perm) });
    return { ok: true, terminal: next };
  }
  if (b?.type === "machine-hosts") {
    // Add machine, as the window's (mods/machine-add-chat, core/machine-add.js): the hosts in ~/.ssh/config, and the
    // machines added already (hc machine ls)
    const text = await Bun.file(join(homedir(), ".ssh", "config")).text().catch(() => null);
    return { hosts: MA.parseConfig(text), added: await addedMachines() };
  }
  if (b?.type === "machine-add" && typeof b.target === "string") {
    // another machine by ssh (hc machine add); an added Mac gets its Full Disk Access note
    const target = b.target.trim();
    if (!target || target.startsWith("-") || /\s/.test(target) || target.length > 200) return { error: MA.WORDS.failed };
    const r = await hc(["machine", "add", target, "--json"]).catch((e) => ({ code: 1, stderr: String(e), stdout: "" }));
    const why = MA.failure(r);
    if (!why) {
      const added = await addedMachines();
      const m = added.find((x: any) => x.target === target || x.name === target);
      const mac = m ? await exec(hub, ["sh", "-c", MA.MAC_CHECK], m.name).catch(() => null) : null;
      const [os, core] = (mac?.stdout ?? "").trim().split("\n");
      return { ok: true, added, ...(m && os === "Darwin" ? { mac: { name: m.name, core: core ?? "~/.local/bin/herdr-core" } } : {}) };
    }
    // the window answers it in a small terminal window of its own; the web has none to open, so it gets the window's
    // own fallback for that: the command to copy and run in any terminal
    if (MA.needsTerminal(why)) return { manual: true };
    return { error: why.message || MA.WORDS.failed };
  }
  if (b?.type === "machine-mac-open" && typeof b.machine === "string") {
    // the Mac's Full Disk Access setting, opened on its own screen
    await exec(hub, ["open", MA.MAC_SETTING], b.machine).catch(() => null);
    return { ok: true };
  }
  if (b?.type === "worktree" && typeof b.place === "string" && compat.shared) {
    const sp = compat.spaceOf(b.place);
    if (!sp) throw new Error("That space isn't here any more.");
    return worktreeAct(hub, sp, b, compat.shared, () => act(hub, { type: "space-remove", place: b.place }, compat).catch(() => null), compat.allSpaces());
  }
  if (b?.type === "assistant-close" && typeof b.place === "string") {
    // 3.0's Close Assistant: its tab closes; "stop" ends the workers it started first, "keep" lets them finish
    const key = `p:${b.place.replace("/", ":")}`, a = compat.addrOf(key);
    const t: any = a ? hub.terminals.get(a) : null;
    if (!t) throw new Error("This space has no Assistant running.");
    const workers = [...hub.terminals.values()].filter((x: any) => x.state === "live" && x.parent === a).length;
    if (b.mode === "count") return { ok: true, workers };
    const tab = t.labels?.["herdr.coordinator.tab"];
    const script = join(appHome(), "mods", "coordinator", "close.sh");
    if (!tab || !existsSync(script)) throw new Error("Closing an Assistant needs Shepherdr's window layout installed.");
    const r = await run(["bash", script, tab, a!, b.mode === "stop" ? "stop" : "keep"]);
    if (r.code !== 0) throw new Error("It didn't close: the Herdr window needs to be open.");
    return { ok: true };
  }
  if ((b?.type === "held-now" || b?.type === "held-drop") && typeof b.cid === "string") {
    // a queued message (the window's Send now and ×): sent at once, or taken back unsent
    const m = held.find((x) => x.cid === b.cid);
    if (!m) throw new Error("That message has gone in already.");
    if (b.type === "held-drop") held.splice(held.indexOf(m), 1);
    else { m.now = true; drain(); }
    return { ok: true };
  }
  if (b?.type === "tab-rename" && (typeof b.place === "string" || typeof b.tab === "string") && typeof b.name === "string") {
    // a tab's Rename… (3.0's tab menu, or a double-click on it): Herdr names the tab (an empty name: it is unnamed again,
    // as the window's prompt does). tab: its id; place: the space whose Assistant's tab it is.
    const a = typeof b.place === "string" ? compat.addrOf(`p:${b.place.replace("/", ":")}`) : null, t: any = a ? hub.terminals.get(a) : null;
    const tab = typeof b.tab === "string" ? b.tab : t?.labels?.["herdr.coordinator.tab"], name = b.name.trim().slice(0, 80);
    if (!tab || !/^[\w./:-]{1,200}$/.test(tab)) throw new Error("This space has no Assistant tab.");
    const r = await run(["herdr-app", "do", "tab.rename", "--id", tab, "--name", name]);
    if (r.code !== 0) {
      // no window: its saved name; a tab's place reads the space's machine ("vm/…"), the Assistant's label its own id
      if (!name) throw new Error("Giving a tab Herdr's own name back needs the Herdr window open.");
      const there = a?.includes("/") && !tab.includes("/") ? `${a.split("/")[0]}/${tab}` : tab;
      if (!(await renameSaved(hub, there, name))) throw new Error("That tab isn't here any more.");
    }
    return { ok: true };
  }
  if ((b?.type === "tab-close" || b?.type === "tab-move" || b?.type === "tab-end") && typeof b.tab === "string" && /^[\w./:-]{1,200}$/.test(b.tab)) {
    // the tab bar's ×, a tab dragged onto another (index: from 1), and the pane header's End agent (its agent ends, its
    // tab closes), as the window's
    if (b.type === "tab-end" && typeof b.chat === "string") { const a = compat.addrOf(b.chat); if (a) await hc(["kill", a]); }
    const r = b.type === "tab-move"
      ? await run(["herdr-app", "do", "tab.move", "--id", b.tab, "--index", String(Math.max(1, Math.floor(Number(b.index) || 1)))])
      : await run(["herdr-app", "do", "tab.close", "--id", b.tab, "--confirmed", "true"]);
    if (r.code !== 0) throw new Error("Tabs are the Herdr window's: it needs the window open.");
    return { ok: true };
  }
  if ((b?.type === "tab-agents" || b?.type === "tab-new") && typeof b.place === "string") {
    // the tab bar's + (3.0's new-menu): a terminal, or an agent, as a new tab of the space, in its folder
    const sp = compat.spaceOf(b.place);
    if (!sp) throw new Error("That space isn't here any more.");
    const known = await knownAgents(hub, compat, sp.machine ?? null);
    if (b.type === "tab-agents") return { ok: true, agents: known.map(({ kind, name, installed }) => ({ kind, name, installed })) };
    const a = typeof b.kind === "string" ? known.find((x) => x.kind === b.kind && x.installed && x.run?.length) : null;
    if (b.kind != null && !a) throw new Error("Not an agent Shepherdr knows.");
    const command = a && isNative(a.kind) ? AGa.argvFor({ agent: a.kind }, { agent: a.kind }).argv : a?.run;
    const r = await run(["herdr-app", "do", "tab.new", "--space", sp.id, "--folder", sp.folder || "~", ...(a ? ["--name", a.name, "--argv", JSON.stringify(command)] : [])]);
    if (r.code !== 0) throw new Error("A new tab opens in the Herdr window: it needs the window open.");
    if (a && isNative(a.kind)) {
      const opened: unknown = JSON.parse(r.stdout || "{}");
      if (!opened || typeof opened !== "object" || !("terminal" in opened) || typeof opened.terminal !== "string")
        throw new Error("The new native tab did not return its terminal identity.");
      const labelled = await hc(["label", opened.terminal, `herdr.agent=${a.kind}`]);
      if (labelled.code !== 0) throw new Error("The native tab started but its runtime identity could not be recorded.");
    }
    return { ok: true };
  }
  if (b?.type === "space-commit" && typeof b.place === "string" && typeof b.message === "string") {
    // Herdr's Changes panel's Commit, for the space's folder: everything (git add -A), with the message typed
    const sp = compat.spaceOf(b.place), message = b.message.trim().slice(0, 10_000);
    if (!sp) throw new Error("That space isn't here any more.");
    if (!message) throw new Error("Type a commit message first.");
    const r = await exec(hub, ["sh", "-c", 'cd "$1" && cd "$(git rev-parse --show-toplevel)" && git add -A && git commit -m "$2"', "commit", sp.folder, message], sp.machine ?? null);
    if (r.code !== 0) throw new Error(`Commit failed: ${(r.stderr || r.stdout).trim().split("\n").pop() ?? ""}`);
    return { ok: true };
  }
  if (b?.type === "tab-switch" && typeof b.chat === "string") {
    // a tab's Switch agent (3.0's: restart.sh with the agent's own command), and Restart for an agent that isn't Claude
    // Code or Codex (no kind: as it runs now; an Assistant again from its template)
    const a = compat.addrOf(b.chat), t: any = a ? hub.terminals.get(a) : null;
    if (!a || !t) throw new Error("Its agent isn't running.");
    const known = await knownAgents(hub, compat, a.includes("/") ? a.split("/")[0] : null);
    const base = (s: unknown) => String(s ?? "").split(/[\\/]/).pop();
    const now = known.find((x) => x.kind === t.labels?.["herdr.agent"]) ?? known.find((x) => base(x.run?.[0]) === base(t.foreground?.argv?.[0] ?? t.argv?.[0]));
    const to = typeof b.kind === "string" ? known.find((x) => x.kind === b.kind && x.installed) : null;
    if (b.kind != null && !to?.run?.length) throw new Error("Not an agent installed there.");
    const assistant = t.labels?.["herdr.role"] === "coordinator" && !t.labels?.["herdr.agent"];
    const kind = kindOf(t);
    if (isNative(kind)) {
      if (!to) throw new Error("Use the native terminal to restart this session; model changes apply without restarting it.");
      if (to.kind === kind) return { ok: true };
      const state = nativeState(await nativeAction(hub, compat, a, "state"));
      if (state?.settled !== true || state.pendingAsyncWork !== false)
        throw new Error("Native work is active or its settlement is unknown; its owning terminal cannot be replaced.");
    }
    const argv = to && isNative(to.kind) ? AGa.argvFor({ agent: to.kind }, { agent: to.kind }).argv
      : to?.run ?? (assistant ? null : now?.run ?? null);
    const script = join(appHome(), "mods", "coordinator", "restart.sh");
    if (!existsSync(script)) throw new Error("Switching agents needs Shepherdr's window layout installed.");
    const r = await run(["bash", script, a, ...(argv ? [JSON.stringify(argv)] : [])]);
    if (r.code !== 0) throw new Error("It didn't restart: the Herdr window needs to be open.");
    return { ok: true };
  }
  if (b?.type === "space-remove" && typeof b.place === "string" && compat.shared) {
    // Close a space: the window closes its own (herdr-app do space.close), or with no window the service closes it in
    // the window's saved spaces (service/saved-spaces.ts); the service forgets one of its own and ends its Assistant
    const own = Array.isArray(compat.shared.value("spaces")) ? compat.shared.value("spaces") : [];
    const mine = own.find((x: any) => (x.machine ? `${x.machine}/${x.id}` : x.id) === b.place);
    if (mine) {
      await compat.shared.set("spaces", own.filter((x: any) => x !== mine));
      for (const t of hub.terminals.values()) if (t.labels?.["herdr.coordinator.space"] === mine.id && (t.address.includes("/") ? t.address.split("/")[0] : null) === (mine.machine ?? null)) await hc(["kill", t.address]);
      return { ok: true };
    }
    const r = await run(["herdr-app", "do", "space.close", "--id", b.place, "--confirmed", "true"]);
    if (r.code !== 0) {
      const gone = await closeSaved(hub, b.place);
      if (!gone) throw new Error("That space isn't here any more.");
      logSpace("close", b.place, gone.name);
      const bar = compat.shared.value("spaces-chat") ?? {}, next = withoutSpace(bar, b.place);
      if (next !== bar) await compat.shared.set("spaces-chat", next);
    }
    return { ok: true };
  }
  if (b?.type === "rewind" && typeof b.chat === "string" && typeof b.message === "string") {
    // the CLI's own rewind on that message, as the window's: Claude Code's /rewind lists the prompts newest last (Up
    // once per later prompt); Codex's Esc Esc browses back from the last (Left). Then its terminal shows it.
    const a = chat(b.chat), t: any = hub.terminals.get(a), r = compat.reader?.(a);
    if (isNative(kindOf(t))) throw new Error("Use this runtime's native terminal to rewind its conversation.");
    if (t?.status?.state === "working") throw new Error("The agent is working: stop it first, then rewind.");
    const users = (r?.conv?.items ?? []).filter((x: any) => x.kind === "user" && !x.from && !/^\//.test(x.text ?? ""));
    const i = users.findIndex((u: any) => u.id === b.message), back = i < 0 ? 1 : users.length - i;
    if ((kindOf(t) ?? "claude") === "claude") { await hub.send(a, "/rewind"); await Bun.sleep(700); await hc(["keys", a, ...Array(back).fill("up")]); }
    else { await hc(["keys", a, "esc", "esc"]); await Bun.sleep(500); if (back > 1) await hc(["keys", a, ...Array(back - 1).fill("left")]); }
    return { ok: true, terminal: true };
  }
  if (b?.type === "earlier" && typeof b.chat === "string") {
    // the chat scrolled to its top ("Loading earlier…"): the log's page before what it shows
    await (compat as any).earlier?.(chat(b.chat));
    return { ok: true };
  }
  if (b?.type === "fork" && typeof b.chat === "string") {
    // a copy of the conversation in a new tab of its space, through the CLI's own fork (the window makes the tab)
    const a = chat(b.chat), t: any = hub.terminals.get(a), r = compat.reader?.(a);
    const kind = kindOf(t) ?? r?.kind;
    if (isNative(kind)) {
      const home = r?.nativeState?.hermesHome;
      if ((kind === "omh" || kind === "hermes") && typeof home !== "string")
        throw new Error("The owning Hermes profile is unavailable; use its native terminal to fork.");
      const result = await nativeAction(hub, compat, a, "fork");
      if (kind === "omh" || kind === "hermes") {
        if (typeof result.sessionId !== "string" || !result.sessionId)
          throw new Error("The native fork did not return its exact session identity.");
        const machine = a.includes("/") ? a.split("/")[0] : null;
        const ownSpace = ["herdr.space", "herdr.task.space", "herdr.coordinator.space"].map((label) => t?.labels?.[label]).find(Boolean);
        const space = ownSpace && machine && !ownSpace.includes("/") ? `${machine}/${ownSpace}` : ownSpace;
        const argv = ["shepherdr-native-run", kind, JSON.stringify({ sessionId: result.sessionId, hermesHome: home })];
        const opened = await run(["herdr-app", "do", "tab.new", ...(space ? ["--space", space] : []),
          "--folder", t?.foreground?.cwd ?? t?.cwd ?? "", "--argv", JSON.stringify(argv)]);
        if (opened.code !== 0) throw new Error(`Native fork ${result.sessionId} exists, but its tab could not open; resume it in its native terminal.`);
        const tab: unknown = JSON.parse(opened.stdout || "{}");
        if (!tab || typeof tab !== "object" || !("terminal" in tab) || typeof tab.terminal !== "string")
          throw new Error(`Native fork ${result.sessionId} opened without a confirmed terminal identity.`);
        const labelled = await hc(["label", tab.terminal, `herdr.agent=${kind}`]);
        if (labelled.code !== 0) throw new Error(`Native fork ${result.sessionId} opened, but its runtime identity could not be recorded.`);
      }
      return { ok: true, ...result };
    }
    if (kind !== "claude" && kind !== "codex") throw new Error("This runtime does not provide a conversation fork.");
    const session = sessionOf(kind, r?.path ?? null);
    if (!session) throw new Error("This conversation has no saved session to fork.");
    const space = ["herdr.space", "herdr.task.space", "herdr.coordinator.space"].map((k) => t?.labels?.[k]).find(Boolean);
    const res = await run(["herdr-app", "do", "tab.new", ...(space ? ["--space", space] : []), "--folder", t?.foreground?.cwd ?? t?.cwd ?? "", "--argv", JSON.stringify(forkArgv(kind, t?.foreground?.argv ?? t?.argv ?? [], session))]);
    if (res.code !== 0) throw new Error("A fork opens as a tab in the Herdr window: it needs the window open.");
    return { ok: true };
  }
  if (b?.type === "keys" && typeof b.chat === "string" && Array.isArray(b.keys) && b.keys.length <= 32 && b.keys.every((k: unknown) => typeof k === "string" && k.length <= 40)) {
    const res = await hc(["keys", chat(b.chat), ...b.keys]);
    return { ok: res.code === 0 };
  }
  if (b?.type === "send" && typeof b.chat === "string" && b.chat.startsWith("p:") && !compat.addrOf(b.chat)) {
    // a space's Assistant chat with no Assistant yet: the first message starts it (the assistant template) with the
    // message as its task, as the window's empty Assistant chat does (mods/chat startAgent): in the window's slot for
    // this space (its tab's chat pane takes it), else as the service's own (spaces.ts, a web-only install)
    const space = b.chat.slice(2).replace(":", "/"), sp = compat.spaceOf(space);
    if (!sp) throw new Error("That space isn't here any more.");
    const bare = String(sp.id).split("/").pop()!;
    const slots: Record<string, any> = compat.shared?.value("coordinator.slots") ?? {};
    const [slot, rec] = Object.entries(slots).find(([, r]) => r && (r.space === sp.id || r.space === bare)) ?? [];
    const text = str(b.text ?? "").trim();
    if (!text) throw new Error("nothing to send");
    const attachments: unknown[] = Array.isArray(b.attachments) ? b.attachments : [];
    if (attachments.length > MAX_FILES) throw new Error(`At most ${MAX_FILES} files in one message.`);
    const files = attachments.map((path) => {
      if (typeof path !== "string" || !path.startsWith(attachDir() + "/") || path.includes("/../"))
        throw new Error("That file isn't one sent from here.");
      return path;
    });
    const task = [text, ...files].join("\n\n");
    // its machine offline: it waits (the web shows it waiting to be reached) and starts once the machine is back
    if (sp.machine && !(await addedMachines()).some((m: any) => m.name === sp.machine && m.state === "connected")) {
      const was = starts.get(space)?.b.text;
      starts.set(space, { b: { ...b, text: was ? `${was}\n\n${text}` : text }, hub, compat, at: Date.now() });
      startWhenBack();
      return { ok: true, waiting: true };
    }
    const labels = slot
      ? ["--label", `herdr.chat=${SP.chatLabel(rec.pane)}`, "--label", `herdr.chat.slot=${slot}`, "--label", `herdr.chat.origin=slot:${slot}`, "--label", "herdr.role=coordinator",
        "--label", `herdr.coordinator.space=${bare}`, ...(rec.tab ? ["--label", `herdr.coordinator.tab=${String(rec.tab).split("/").pop()}`] : [])]
      : ["--label", "herdr.role=coordinator", "--label", `herdr.coordinator.space=${bare}`, "--label", `herdr.space=${bare}`];
    const tpl = await execTemplate(hub, "assistant", sp.machine ?? null);
    const stageRemoteFiles = Boolean(sp.machine && files.length);
    let native: NativeAssistantCommand | null;
    let r: CommandResult;
    try {
      native = await nativeAssistantCommand(hub, {
        space: sp.id, folder: sp.folder || "", machine: sp.machine ?? null,
        words: stageRemoteFiles ? "" : tpl.words, message: stageRemoteFiles ? "" : text,
        files: sp.machine ? [] : files,
      });
      r = await hc(["run", "--bg", "--json", "--no-parent", "--keep", ...(sp.machine ? ["--on", sp.machine] : []), ...(sp.folder ? ["--cwd", sp.folder] : []), ...labels,
        ...(native ? ["--label", `herdr.agent=${native.kind}`, "--", ...native.argv] : [tpl.name, task])]);
    } finally { tpl.done(); }
    let out: any = {};
    try { out = JSON.parse(r.stdout || "{}"); } catch {}
    if (r.code !== 0 || !out.address) throw new Error((r.stderr || r.stdout).trim().split("\n").pop() || "The Assistant didn't start.");
    // what the person asked, for its chat to show apart from the template (the window's data/chat-first)
    const first = join(appHome(), "data", "chat-first");
    const addr = sp.machine && !String(out.address).includes("/") ? `${sp.machine}/${out.address}` : String(out.address);
    if (native && stageRemoteFiles) {
      const landed = await Promise.all(files.map(async (path) => {
        const copy = await hc(["put", addr, path, "--json"]);
        const result: unknown = JSON.parse(copy.stdout || "{}");
        if (copy.code !== 0 || !result || typeof result !== "object" || !("path" in result) || typeof result.path !== "string")
          throw new Error("The native Assistant started, but an attachment did not reach its machine; the message was not sent.");
        return result.path;
      }));
      const { io, target } = await startedNativeTarget(hub, compat, addr, native.kind);
      const sent = await nativeRequest(io, native.kind, "send", {
        ...target, text: [tpl.words, text, ...landed].filter(Boolean).join("\n\n"), files: landed,
        ...(typeof b.cid === "string" ? { requestId: b.cid } : {}),
      }, io.machine);
      if (sent.admitted !== true && sent.accepted !== true)
        throw new Error("The native Assistant did not confirm its first message; it will not be replayed automatically.");
    } else if (!native && out.first_message?.sent === false && tpl.words) firstWhenIdle(hub, addr, `${tpl.words}\n\n${task}`);
    try { mkdirSync(first, { recursive: true }); writeFileSync(writable(join(first, `${String(slot ?? addr).replace(/[^\w.-]/g, "_")}.txt`)), text); } catch {}
    return { ok: true, started: true };
  }
  if (b?.type === "send" && typeof b.chat === "string") {
    // what it replies to (2.1 names the message by id) and the tasks it mentions, as the window's chat sends them
    const a = chat(b.chat), r = compat.reader?.(a);
    const quoted = typeof b.reply === "string" ? r?.conv?.items?.find((x: any) => x.id === b.reply)?.text ?? null : null;
    const tags = mentionedIn(String(b.text ?? ""), compat.tasksFor(b.chat)).map((x: any) => taskTag(x, x.ref ?? `#${x.id}`)).join("");
    b = { ...b, addr: a, quote: quoted, tags };
  }
  else if ((b?.type === "stop" || b?.type === "steer") && typeof b.chat === "string") b = { ...b, addr: chat(b.chat) };
  else if (b?.type === "stop-job" && typeof b.chat === "string" && typeof b.job === "string") {
    // Stop on a background shell or watcher (core/bg-jobs.js): its processes end; its CLI then tells the agent
    const r = compat.reader?.(chat(b.chat));
    if (!r || !b.job.startsWith("b:") || !(await r.stopJob(b.job.slice(2)))) throw new Error("It isn't running any more.");
    return { ok: true };
  }
  else if ((b?.type === "close" || b?.type === "delete" || b?.type === "archive" || b?.type === "reopen" || b?.type === "task-resume" || b?.type === "schedule") && typeof b.chat === "string") {
    // a task's row menu: its chat key is "<task>/worker" (or 2.1's "/Agent")
    const t = compat.taskOf(b.chat.split("/")[0]);
    if (!t) throw new Error("That task isn't here any more.");
    b = { ...b, ref: t.ref, folder: t.folder, machine: t.machine };
  }
  else if ((b?.type === "evidence" || b?.type === "accept") && typeof b.task === "string") {
    const t = compat.taskOf(b.task);
    if (!t) throw new Error("That task isn't here any more.");
    b = { ...b, ref: t.ref, folder: t.folder, machine: t.machine };
  }
  else if (b?.type === "read" && b.chats && typeof b.chats === "object") {
    for (const key of Object.keys(b.chats).slice(0, 20)) { const a = compat.addrOf(key); if (a) await act(hub, { type: "read", addr: a }, compat); }
    return { ok: true };
  }
  // a call started or ended on the page: its audio goes over /api/voice; nothing to keep here
  if (b?.type === "call") return { ok: true };
  if (b?.type === "artifact-delete" && typeof b.artifact === "string") {
    // an artifact deleted from its ⋯, as herdr-artifact rm (it and its versions)
    if (!/^\w[\w.-]{0,119}$/.test(b.artifact)) throw new Error("Not an artifact.");
    const r = await run(["herdr-artifact", "rm", "--json", "--", b.artifact]);
    if (r.code !== 0) return { ok: false, error: (r.stderr || r.stdout).trim().split("\n").pop() || "It wasn't deleted." };
    return { ok: true };
  }
  if ((b?.type === "artifact-send" || b?.type === "artifact-note" || b?.type === "artifact-say") && typeof b.artifact === "string" && compat.artifacts) {
    // a recipe's answer or a note on one of its blocks: to the agent that keeps the artifact
    const text = str(b.text ?? b.note, 4000).trim();
    if (!text) throw new Error("Nothing to send.");
    return compat.artifacts.say(str(b.artifact, 100), Number(b.v) || 1, text, typeof b.target === "string" ? b.target.slice(0, 300) : undefined);
  }
  switch (b?.type) {
    case "send": {
      // one page's send asked again (its answer lost on the way, a reload, Retry): the same one, never twice
      const cid = typeof b.cid === "string" && b.cid ? b.cid.slice(0, 80) : null;
      if (!cid) return sendNow(hub, b, compat);
      return once(cid, (again) => sendNow(hub, b, compat, again));
    }
    case "steer": {
      // Send now on a message in Claude Code's own queue (#662): its Ctrl+Enter, only while its screen offers it
      const a = addr(b.addr), t = hub.terminals.get(a);
      if (!t || t.state !== "live") throw new Error("That agent isn't running.");
      if (isNative(kindOf(t))) throw new Error("Use the native terminal to promote queued messages.");
      const io = runtimeIO(hub, compat, a);
      if (!(await steerNow(io, a))) throw new Error(WORDS.notNow);
      return { ok: true };
    }
    case "stop": {
      const address = addr(b.addr), terminal = hub.terminals.get(address);
      if (isNative(kindOf(terminal))) {
        await nativeAction(hub, compat, address, "interrupt");
        return { ok: true };
      }
      const r = await hc(["interrupt", addr(b.addr)]);
      return { ok: r.code === 0 };
    }
    case "read": {
      const a = addr(b.addr), t: any = hub.terminals.get(a);
      if (!t || t.labels?.["herdr.role"] !== "coordinator" || !(t.turn > 0)) return { ok: true };
      const r = await hc(["label", a, `${READ}=${t.turn}`]);
      return { ok: r.code === 0 };
    }
    case "evidence": {
      const { ref, folder, machine } = taskOf(b);
      const r = await task(["show", ref, "--evidence"], folder, machine);
      if (r.code !== 0) throw new Error(r.stderr.trim() || "herdr-task show failed");
      return JSON.parse(r.stdout);
    }
    case "accept": {
      const { ref, folder, machine } = taskOf(b);
      const r = await task(["accept", ref], folder, machine);
      let out: any = null;
      try { out = JSON.parse(r.stdout); } catch {}
      if (r.code !== 0 || out?.error) throw new Error(out?.error || r.stderr.trim() || "herdr-task accept failed");
      return out ?? { ok: true };
    }
    case "task-resume": {
      // an ended task's Resume (the window's): herdr-task starts its agent again on its session
      const { ref, folder, machine } = taskOf(b);
      const r = await task(["resume", ref], folder, machine);
      let out: any = null;
      try { out = JSON.parse(r.stdout); } catch {}
      if (r.code !== 0 || out?.error || !out?.terminal) throw new Error(out?.error || r.stderr.trim().split("\n").pop() || "Its agent didn't start.");
      return { ok: true, terminal: out.terminal };
    }
    // a task's Start later (SHEP-38): herdr-task schedules it, takes that back, or starts it now; its words refused come back
    case "schedule": {
      const { ref, folder, machine } = taskOf(b);
      const args = b.now ? ["start", ref, "--now"] : b.off ? ["edit", ref, "--at", ""]
        : typeof b.in === "string" ? ["edit", ref, "--in", str(b.in, 80)] : ["edit", ref, "--at", str(b.at, 80)];
      const r = await task(args, folder, machine);
      let out: any = null;
      try { out = JSON.parse(r.stdout); } catch {}
      if (r.code !== 0 || out?.error) return { ok: false, error: out?.error || r.stderr.trim().split("\n").pop() || "herdr-task refused it." };
      return { ok: true, why: out?.why ?? null };
    }
    // the person's own calls on a task, as herdr-task makes them: done, removed, or back from the archive
    case "close": case "delete": case "archive": case "reopen": {
      if (b.type === "archive" && b.on !== false) throw new Error("Done tasks archive themselves: Settings › Tasks › Archive Done after.");
      const { ref, folder, machine } = taskOf(b);
      const r = await task([b.type === "close" ? "done" : b.type === "delete" ? "rm" : b.type === "reopen" ? "reopen" : "unarchive", ref], folder, machine);
      let out: any = null;
      try { out = JSON.parse(r.stdout); } catch {}
      if (r.code !== 0 || out?.error) return { ok: false, error: out?.error || r.stderr.trim().split("\n").pop() || "herdr-task refused it." };
      return { ok: true };
    }
  }
  // 2.1 has more buttons than this service answers yet: they say so plainly
  throw new Error("Not on the web yet: use the Herdr window for this.");
}
