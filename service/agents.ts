// The model menu and restart from the web (2.1's restart sheet): what an agent runs on now and what its CLIs offer
// (core/agents.js, as the window's model menu), and restarting it on another model, effort or speed the window's way:
// herdr-task switch for a task's agent, the Assistant's restart.sh for an Assistant, parked behind its chat.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { join } from "node:path";
import type { Hub } from "./hub.ts";
import { HOME, appHome, where, writable } from "./paths.ts";
import { sh } from "./model.ts";
import * as AG from "../core/agents.js";
import { chatLabel } from "../core/spaces.js";
import { kindOf } from "../core/chat-reader.js";
import { isNative, nativeRequest, nativeTarget } from "../core/native-runtime.js";

const { DEFAULTS, KINDS, parseDefaults, current, menuFor, rowFor, argvFor, sessionOf, NAMES, line, PICKS_FILE, parsePicks, pickKey, withPin, withRecent } = AG;

async function run(cmd: string[]) {
  const p = Bun.spawn(cmd, { stdout: "pipe", stderr: "pipe", stdin: "ignore" });
  const [out, err] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()]);
  return { code: await p.exited, out, err };
}

export class Agents {
  constructor(private hub: Hub) {}

  private io = { sh: (script: string, args: string[], machine: string | null = null) => sh(this.hub, script, args, machine) };

  private async installed(machine: string | null) {
    const r = await sh(this.hub, 'for c in "$@"; do command -v "$c" >/dev/null 2>&1 && echo "$c"; done', ["agents", ...KINDS], machine);
    return new Set(String(r.stdout ?? "").split("\n").filter(Boolean));
  }

  private async allDefaults(machine: string | null, targetKind?: string, target = {}) {
    const have = await this.installed(machine);
    const entries = await Promise.all(KINDS.filter((k: string) => have.has(k)).map(async (k: string) =>
      [k, await this.defaults(k, machine, k === targetKind ? target : {}).catch((e: Error) => ({ models: [], error: e.message, capabilities: { configure: false, customModel: false } }))]));
    return Object.fromEntries(entries);
  }

  /** The person's pinned and recent models (core/agents.js PICKS_FILE), as the window's model menu keeps them here */
  private picksPath = where()["model-picks"];
  picks() {
    try { return parsePicks(readFileSync(this.picksPath, "utf8")); } catch { return parsePicks(""); }
  }
  private savePicks(p: unknown) {
    mkdirSync(dirname(writable(this.picksPath)), { recursive: true });
    writeFileSync(this.picksPath, JSON.stringify(p, null, 2));
  }
  /** the star on a model's row: pinned to the top of its agent's list, or not any more */
  pin(agent: string, model: string) {
    const p = withPin(this.picks(), pickKey(agent, model));
    this.savePicks(p);
    return p;
  }

  private async defaults(kind: string, machine: string | null, target = {}) {
    if (isNative(kind)) return nativeRequest(this.io, kind, "models", target, machine);
    if (!NAMES[kind]) throw new Error(`Unsupported agent runtime: ${kind}`);
    const r = await sh(this.hub, DEFAULTS, ["defaults", kind], machine).catch(() => null);
    return parseDefaults(kind, r?.stdout ?? "");
  }

  /** A CLI's own settings on a machine as last read (the chat's model button reads them on every change, as the
   *  window's chat keeps them: this.dflt), read again in the background at most once a minute. */
  private known = new Map<string, { at: number; value: any }>();
  cached(kind: string, machine: string | null) {
    const k = `${machine ?? ""}|${kind}`, was = this.known.get(k);
    if (!was || Date.now() - was.at > 60_000) {
      this.known.set(k, { at: Date.now(), value: was?.value ?? null });
      void this.defaults(kind, machine).then((value) => this.known.set(k, { at: Date.now(), value }), () => undefined);
    }
    return was?.value ?? null;
  }

  /** The window's model menu (mods/chat openPicker → mods/chat-models) for the agent at addr: what it runs on, each
   *  installed CLI's models, levels, Fast and permission modes, the person's pinned and recent ones; offline: the
   *  machine it runs on when that machine can't be reached now (a restart there waits for it) */
  async menu(addr: string, reader: any) {
    const t: any = this.hub.terminals.get(addr);
    if (!t) throw new Error("That agent isn't here any more.");
    const machine = addr.includes("/") ? addr.split("/")[0] : null;
    const kind = kindOf(t) ?? reader?.kind;
    if (!NAMES[kind]) throw new Error("That terminal has no recognized agent runtime.");
    const dflts = await this.allDefaults(machine, kind, isNative(kind) ? nativeTarget(t, reader) : {});
    let meta = reader?.conv?.meta ?? {};
    // a resumed conversation's log names the model it ran on before: until this process writes, its arguments say
    if (t.created_ms && meta.at && meta.at < t.created_ms) meta = { used: meta.used, window: meta.window };
    if (isNative(kind)) meta = { ...meta, ...(reader?.nativeState ?? {}) };
    const c = current(kind, t.foreground?.argv ?? t.argv ?? [], meta, dflts[kind]);
    const row = rowFor(kind, c);
    const menus = Object.fromEntries(Object.keys(dflts).map((k) => [k, menuFor(k, { ...dflts[k], ...(k === kind ? { native: reader?.nativeState } : {}) }, k === kind ? row : null)]));
    return { current: { agent: kind, model: row, effort: c.effort ?? null, fast: !!c.fast, perm: c.perm ?? null }, menus,
      permDefault: Object.fromEntries(Object.keys(dflts).map((k) => [k, k === "claude" ? dflts[k]?.mode ?? "manual" : k === "codex" ? "auto" : dflts[k]?.mode ?? null])),
      picks: this.picks(), offline: machine && !(await this.reachable(machine)) ? machine : null };
  }

  /** whether another machine answers now (hc machine ls: connected) */
  private async reachable(machine: string) {
    const r = await run([process.env.SHEPHERDR_HC || "hc", "machine", "ls", "--json"]).catch(() => null);
    try { return (JSON.parse(r?.out ?? "{}").machines ?? []).some((m: any) => m.name === machine.split("/")[0] && m.state === "connected"); } catch { return true; }
  }

  /** Each CLI's own settings here (Settings › Agents' model menu: core/settings.js choicesFrom). */
  async ownDefaults() {
    return this.allDefaults(null);
  }

  /** What an unset role runs, in the window's words ("Opus 5.5 · 1M · High"): each CLI's own defaults here. */
  async defaultLines(): Promise<Record<string, string>> {
    const out: Record<string, string> = {};
    const defaults = await this.allDefaults(null);
    for (const k of Object.keys(defaults)) {
      const d = defaults[k];
      if (d) out[k] = line({ agent: k, model: d.model, effort: d.effort ?? d.per?.[String(d.model ?? "").replace(/\[1m\]$/, "")] ?? null, fast: d.fast }).split(" · ").slice(1).join(" · ");
    }
    return out;
  }

  /** What each CLI offers on a machine (null: here), as the restart sheet lists them. */
  async choices(machine: string | null) {
    const dflt = await this.allDefaults(machine);
    return Object.keys(dflt).map((k) => {
      const m = menuFor(k, dflt[k], null);
      // its permission modes, and the one a new agent gets unless picked (the CLI's own: Claude Code's settings mode)
      return { kind: k, name: NAMES[k], models: m.models.map((x) => ({ id: x.id, label: x.name, nativeDefault: x.nativeDefault === true, efforts: x.efforts, ...(x.defaultEffort ? { defaultEffort: x.defaultEffort } : {}) })),
        speeds: m.models.some((x) => x.fast) ? ["normal", "fast"] : ["normal"], perms: m.perms ?? [],
        permDefault: k === "claude" ? dflt[k]?.mode ?? "manual" : k === "codex" ? "auto" : dflt[k]?.mode ?? null,
        capabilities: m.capabilities, error: dflt[k]?.error ?? null };
    });
  }

  /** What the restart sheet shows for the agent at addr (log: its reader, for the session and what the log says). */
  async info(addr: string, reader: any) {
    const t: any = this.hub.terminals.get(addr);
    if (!t) throw new Error("That agent isn't here any more.");
    const machine = addr.includes("/") ? addr.split("/")[0] : null;
    const kind = kindOf(t) ?? reader?.kind;
    if (!NAMES[kind]) throw new Error("That terminal has no recognized agent runtime.");
    const dflt = await this.allDefaults(machine, kind, isNative(kind) ? nativeTarget(t, reader) : {});
    const c = current(kind, t.foreground?.argv ?? t.argv ?? [], { ...reader?.conv?.meta, ...(isNative(kind) ? reader?.nativeState : {}) }, dflt[kind]);
    const clis = Object.keys(dflt).map((k) => {
      const m = menuFor(k, { ...dflt[k], ...(k === kind ? { native: reader?.nativeState } : {}) }, k === kind ? c.model : null);
      return { kind: k, name: NAMES[k], models: m.models.map((x) => ({ id: x.id, label: x.name, nativeDefault: x.nativeDefault === true, efforts: x.efforts, ...(x.defaultEffort ? { defaultEffort: x.defaultEffort } : {}) })),
        speeds: m.models.some((x) => x.fast) ? ["normal", "fast"] : ["normal"], capabilities: m.capabilities,
        perms: m.perms ?? [], error: dflt[k]?.error ?? null };
    });
    const on = { kind, model: rowFor(kind, c) ?? undefined, effort: c.effort ?? undefined, speed: c.fast ? "fast" : "normal" };
    return { role: t.labels?.["herdr.task"] ? "task" : "coordinator", on, onKnown: true, onWords: line(c), want: on, clis,
      capabilities: isNative(kind) ? reader?.nativeState?.capabilities : undefined,
      working: isNative(kind) ? reader?.nativeState?.settled !== true || reader?.nativeState?.pendingAsyncWork !== false : t.status?.state === "working" };
  }

  /** Restarts it on the picked CLI, model, effort and speed; resolves with the new agent's address. */
  async restart(addr: string, reader: any, pick: { kind: string; model?: string; effort?: string; speed?: string; perm?: string }) {
    const t: any = this.hub.terminals.get(addr);
    if (!t || t.state !== "live") throw new Error("That agent isn't running.");
    const kind = kindOf(t) ?? reader?.kind;
    if (!NAMES[kind] || !NAMES[pick.kind]) throw new Error("Unsupported agent runtime.");
    const machine = addr.includes("/") ? addr.split("/")[0] : null;
    if (isNative(kind)) {
      const target = nativeTarget(t, reader);
      const state = await nativeRequest(this.io, kind, "state", target, machine);
      if (pick.kind === kind) {
        if (!state.capabilities?.configure) throw new Error("This native runtime exposes configuration only in its terminal.");
        await nativeRequest(this.io, kind, "configure", { ...target, model: pick.model, effort: pick.effort, perm: pick.perm }, machine);
        this.used(pick);
        return addr;
      }
      if (state.settled !== true || state.pendingAsyncWork !== false)
        throw new Error("Native work is still running or its settlement is unknown. Keep its owning terminal open.");
    }
    const old = { agent: kind, argv: t.foreground?.argv ?? t.argv ?? [], session: isNative(kind) ? null : sessionOf(kind, reader?.path ?? null) };
    const built = argvFor({ agent: pick.kind, model: pick.model, effort: pick.effort, fast: pick.speed === "fast",
      perm: pick.perm ?? (pick.kind === kind ? current(kind, old.argv, {}, {}).perm : null) }, old);
    const pane = t.labels?.["herdr.chat"] ?? "";
    const task = t.labels?.["herdr.task"];
    if (task) {
      const r = await run(["herdr-task", "switch", task, "--folder", t.foreground?.cwd ?? t.cwd, "--argv", JSON.stringify(built.base), "--json"]);
      let out: any = {};
      try { out = JSON.parse(r.out || "{}"); } catch {}
      if (r.code !== 0 || !out.terminal) throw new Error((r.err || r.out).trim() || "herdr-task switch failed");
      if (pane) await run([process.env.SHEPHERDR_HC || "hc", "label", out.terminal, `herdr.chat=${chatLabel(pane)}`, `herdr.chat.origin=${t.labels?.["herdr.chat.origin"] || addr}`]);
      await run(["herdr-app", "do", "terminal.park", "--terminal", out.terminal]);
      this.used(pick);
      return out.terminal as string;
    }
    const script = join(appHome(), "mods", "coordinator", "restart.sh");
    if (!existsSync(script)) throw new Error("Restarting an Assistant needs Shepherdr's window layout installed.");
    const p = Bun.spawn(["bash", script, addr, JSON.stringify(built.argv)], { stdout: "pipe", stderr: "pipe", stdin: "ignore",
      env: { ...process.env, HERDR_RESTART_CHAT: pane, ...(built.resume ? { HERDR_RESTART_RESUME: "1" } : {}) } });
    const [out, err] = await Promise.all([new Response(p.stdout).text(), new Response(p.stderr).text()]);
    const next = out.trim().split("\n").pop();
    if ((await p.exited) !== 0 || !next) throw new Error(err.trim() || "The agent didn't restart.");
    this.used(pick);
    return next;
  }

  /** restarted on a model: first among the recent ones */
  private used(pick: { kind: string; model?: string }) {
    if (pick.model) try { this.savePicks(withRecent(this.picks(), pickKey(pick.kind, pick.model))); } catch {}
  }
}
