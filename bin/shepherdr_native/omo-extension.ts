// Registers OMO's own TUI control surface in the owning engine. No second writer,
// replacement tool set, message queue, or model orchestrator is created here.
import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { randomUUID } from "node:crypto";
import { execFileSync } from "node:child_process";

type WakeResult = { admitted: { delivery_id: string; kind: string }[] };
type Registration =
  | { status: "registered"; socket: string; dispose: () => void | Promise<void> }
  | { status: "unsupported" | "failed"; reason: string };
type NativeContext = {
  cwd: string;
  mode?: string;
  sessionManager: { getSessionId(): string; getSessionFile(): string | undefined };
  ui: { notify(message: string, level: "error"): void };
};
type NativeAPI = {
  session: {
    registerControlEndpoint(options: {
      inboxDir: string;
      drain: () => Promise<WakeResult>;
      isSessionReferenced: () => boolean;
    }): Promise<Registration>;
  };
  on(event: "session_start" | "session_shutdown", handler: (event: unknown, ctx: NativeContext) => Promise<void>): unknown;
};

function privateDirectory(directory: string): void {
  fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
  const info = fs.lstatSync(directory);
  if (!info.isDirectory() || info.uid !== process.getuid?.() || (info.mode & 0o077)) {
    throw new Error("OMO native publication directory must be owner-only and owned by this user");
  }
}

function processStart(): string {
  const text = fs.readFileSync(`/proc/${process.pid}/stat`, "utf8");
  const ticks = text.slice(text.lastIndexOf(")") + 2).trim().split(/\s+/)[19];
  if (!ticks || !/^\d+$/.test(ticks)) throw new Error("OMO native process start identity is unavailable");
  return `linux-proc-start-ticks:${ticks}`;
}

function agentDirectory(cwd: string): string {
  // Native selection is an explicit read-only early CLI route, before provider or
  // extension initialization. It honors the running TUI's brand/profile env.
  const text = execFileSync("omo", ["config", "agent-dir", "--json"], {
    cwd, env: process.env, encoding: "utf8", timeout: 4000,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const value: unknown = JSON.parse(text);
  if (typeof value !== "object" || value === null || !("version" in value) || value.version !== 1
    || !("ok" in value) || value.ok !== true || !("dir" in value) || typeof value.dir !== "string" || !path.isAbsolute(value.dir)) {
    throw new Error("OMO native agent-directory selector returned an invalid identity");
  }
  return value.dir;
}

export default function (pi: NativeAPI): void {
  const root = process.env.SHEPHERDR_HOME
    ? path.resolve(process.env.SHEPHERDR_HOME, "state/native/omo")
    : path.resolve(process.env.XDG_STATE_HOME || path.join(os.homedir(), ".local/state"), "shepherdr/native/omo");
  const publication = path.join(root, `${process.pid}.json`);
  const bridgeId = randomUUID();
  let registration: Registration | undefined;
  let generation = 0;

  function publish(data: Record<string, unknown>): void {
    privateDirectory(root);
    const record = { pid: process.pid, processStartTime: processStart(), bridgeId, ...data };
    const temporary = `${publication}.${randomUUID()}.tmp`;
    const descriptor = fs.openSync(temporary, "wx", 0o600);
    try {
      fs.writeFileSync(descriptor, JSON.stringify(record));
      fs.fsyncSync(descriptor);
    } finally { fs.closeSync(descriptor); }
    try { fs.renameSync(temporary, publication); }
    finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  }

  pi.on("session_start", async (_event, ctx) => {
    const current = ++generation;
    try {
      if (typeof pi.session?.registerControlEndpoint !== "function") {
        throw new Error("Installed OMO lacks pi.session.registerControlEndpoint; update the native runtime");
      }
      if (registration?.status === "registered") await registration.dispose();
      registration = undefined;
      privateDirectory(root);
      const created = await pi.session.registerControlEndpoint({
        inboxDir: path.join(root, "inbox", `${process.pid}`),
        // We own no disk mailbox. Native prompt/steer/follow_up RPC calls go
        // straight through the TUI's admission path, not this optional drain.
        drain: async () => ({ admitted: [] }),
        isSessionReferenced: () => true,
      });
      if (current !== generation) {
        if (created.status === "registered") await created.dispose();
        return;
      }
      if (created.status !== "registered") throw new Error(`Native control endpoint ${created.status}: ${created.reason}`);
      registration = created;
      const selectedAgentDir = agentDirectory(ctx.cwd);
      const sessionId = ctx.sessionManager.getSessionId();
      if (!sessionId) throw new Error("OMO control registration did not publish its durable session identity");
      publish({ sessionId, sessionFile: ctx.sessionManager.getSessionFile(), socket: created.socket,
        agentDir: selectedAgentDir, cwd: ctx.cwd });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Native OMO control registration failed";
      publish({ error: message });
      ctx.ui.notify(`Shepherdr: ${message}`, "error");
    }
  });

  pi.on("session_shutdown", async () => {
    generation++;
    if (registration?.status === "registered") await registration.dispose();
    registration = undefined;
    try {
      const info = fs.lstatSync(publication);
      if (info.isFile() && info.uid === process.getuid?.() && !(info.mode & 0o077)) {
        const record: unknown = JSON.parse(fs.readFileSync(publication, "utf8"));
        if (typeof record === "object" && record !== null && "bridgeId" in record && record.bridgeId === bridgeId) fs.unlinkSync(publication);
      }
    } catch { /* Native teardown may already have removed this generation. */ }
  });
}
