import { expect, test } from "bun:test";
import { argvFor, current, line, menuFor, modelSections, parseDefaults, parsePicks, pickKey, withPin, withRecent } from "./agents.js";

const codex = { models: Array.from({ length: 250 }, (_, i) => ({ id: `gpt-${i}`, label: `GPT ${i}`, efforts: ["low", "high"] })) };
const menus = { claude: menuFor("claude", {}), codex: menuFor("codex", codex) };

test("Codex lists every model it has, not its first five", () => {
  expect(menus.codex.models.length).toBe(250);
});

test("nothing typed: pinned, then recent, then the rest of the agent shown", () => {
  const picks = withRecent(withPin(parsePicks(""), pickKey("claude", "haiku")), pickKey("claude", "sonnet"));
  const s = modelSections(menus, "claude", picks, "");
  expect(s.map((x) => x.title)).toEqual(["Pinned", "Recent", "All models"]);
  expect(s[0].rows.map((r) => r.id)).toEqual(["haiku"]);
  expect(s[1].rows.map((r) => r.id)).toEqual(["sonnet"]);
  expect(s[2].rows.some((r) => r.id === "haiku" || r.id === "sonnet")).toBe(false);
});

test("a long list is capped and counts the rest", () => {
  const [all] = modelSections(menus, "codex", null, "");
  expect(all.rows.length).toBe(200);
  expect(all.more).toBe(50);
});

test("typed: every agent's matches, and a row to use the id typed", () => {
  const s = modelSections(menus, "codex", null, "opus 1m");
  expect(s.map((x) => x.key)).toEqual(["claude"]);
  expect(s[0].rows.map((r) => r.id)).toEqual(["opus[1m]"]);
  const t = modelSections(menus, "codex", null, "gpt-7-preview").find((x) => x.key === "typed");
  expect(t.rows.map((r) => [r.agent, r.id, r.typed])).toEqual([["codex", "gpt-7-preview", true], ["claude", "gpt-7-preview", true]]);
  expect(modelSections(menus, "codex", null, "gpt-999").map((x) => x.key)).toEqual(["typed"]);
  expect(modelSections(menus, "codex", null, "gpt-12").find((x) => x.key === "typed").rows.map((r) => r.agent)).toEqual(["claude"]);
});

test("an exact id typed needs no extra row; picks survive bad files", () => {
  expect(parsePicks("not json")).toEqual({ pinned: [], recent: [] });
  expect(withPin(withPin(parsePicks(""), "a:b"), "a:b").pinned).toEqual([]);
});

test("a running model typed by id stays in its agent's list", () => {
  expect(menuFor("claude", {}, "claude-opus-4-8").models[0]).toMatchObject({ id: "claude-opus-4-8", name: "Opus 4.8" });
});

test("a pinned or recent model typed by id stays on offer", () => {
  const picks = withRecent(withPin(parsePicks(""), "claude:claude-opus-4-8"), "codex:gpt-x");
  expect(modelSections(menus, "claude", picks, "")[0].rows[0]).toMatchObject({ id: "claude-opus-4-8", name: "Opus 4.8", pinned: true });
  expect(modelSections(menus, "codex", picks, "")[0]).toMatchObject({ title: "Recent", rows: [{ id: "gpt-x" }] });
  expect(modelSections(menus, "claude", picks, "gpt-x").map((x) => x.key)).toEqual(["codex", "typed"]);
});

// SHEP-41: Fast was hidden (and stuck on) for a Codex whose config.toml set service_tier.
test("every agent with a fast mode shows the Fast switch, and turning it off sticks", () => {
  const cache = JSON.stringify({ models: [{ slug: "gpt-x", display_name: "GPT-X", supported_reasoning_levels: [{ effort: "high" }], service_tiers: [{ id: "priority" }] },
    { slug: "gpt-slow", display_name: "GPT-Slow", service_tiers: [] }] });
  const dflt = parseDefaults("codex", `model\tgpt-x\nservice_tier\tfast\nmodels\t${cache}\n`);
  const codexMenu = menuFor("codex", dflt);
  expect(codexMenu.models.find((m) => m.id === "gpt-x").fast).toBe(true);
  expect(codexMenu.models.find((m) => m.id === "gpt-slow").fast).toBe(false);
  expect(codexMenu.fastLocked).toBeUndefined();
  expect(menuFor("claude", {}).models.find((m) => m.id === "opus").fast).toBe(true);
  // off: said on the command line, so config.toml's fast loses, and the label reads it back
  const off = argvFor({ agent: "codex", model: "gpt-x", effort: "high", fast: false }, { agent: "codex", argv: ["codex", "-c", 'service_tier="fast"'] }).argv;
  expect(off.filter((a) => a.startsWith("service_tier"))).toEqual(['service_tier="default"']);
  expect(current("codex", off, {}, dflt).fast).toBe(false);
  expect(line(current("codex", off, {}, dflt))).toBe("Codex · GPT-X · High");
  const on = argvFor({ agent: "codex", model: "gpt-x", fast: true }, { agent: "codex", argv: off }).argv;
  expect(current("codex", on, {}, dflt).fast).toBe(true);
  const claudeOff = argvFor({ agent: "claude", model: "opus", fast: false }, { agent: "claude", argv: ["claude"] }).argv;
  expect(current("claude", claudeOff, {}, { fast: true }).fast).toBe(false);
});

test("native catalogs retain runtime models without invented effort or permission modes", () => {
  for (const agent of ["omp", "omo", "omh", "hermes"]) {
    const data = parseDefaults(agent, JSON.stringify({ models: [{ id: "provider/model", label: "Native model", efforts: ["off", "high"] }] }));
    expect(menuFor(agent, data).models[0]).toMatchObject({ id: "provider/model", efforts: ["off", "high"], fast: false });
    expect(menuFor(agent, data).perms).toEqual([]);
    expect(menuFor(agent, { models: [{ id: "native" }] }).models[0].efforts).toEqual([]);
    expect(current(agent, [], { model: { id: "actual", contextWindow: 32000 }, thinking: "off" }, data))
      .toMatchObject({ agent, model: "actual", effort: "off", window: 32000, perm: null });
  }
});

test("native launch uses its native TUI launcher and exact session file, never Claude flags", () => {
  for (const agent of ["omp", "omo", "omh", "hermes"]) {
    const built = argvFor({ agent, model: "provider/model", effort: "high", profile: agent === "omo" ? undefined : "work" }, { agent: "claude", argv: ["claude", "--dangerously-skip-permissions"] });
    expect(built.base.slice(0, 2)).toEqual(["shepherdr-native-run", agent]);
    expect(JSON.parse(built.base[2])).toMatchObject({ model: "provider/model", effort: "high" });
    expect(built.resume).toBe(false);
    expect(built.argv.join(" ")).not.toContain("dangerously");
  }
  const resumed = argvFor({ agent: "omp" }, { agent: "omp", sessionFile: "/exact/one.jsonl" });
  expect(JSON.parse(resumed.argv[2]).sessionFile).toBe("/exact/one.jsonl");
  expect(() => argvFor({ agent: "omp", perm: "full" }, {})).toThrow();
  expect(() => argvFor({ agent: "mystery" }, {})).toThrow();
  expect(() => current("mystery", [], {}, {})).toThrow();
});

test("native menus cannot invent typed or formerly pinned native models", () => {
  const native = { omp: menuFor("omp", { models: [{ id: "known", label: "Known" }] }) };
  const saved = { pinned: ["omp:removed"], recent: ["omp:removed"] };
  expect(modelSections(native, "omp", saved, "").flatMap((s) => s.rows).map((r) => r.id)).toEqual(["known"]);
  expect(modelSections(native, "omp", saved, "unknown")).toEqual([]);
});

test("native default is an explicit launch choice, not a fabricated observed model", () => {
  const menu = menuFor("omo", { error: "Native catalog needs a session" });
  expect(menu.models[0]).toMatchObject({ id: "", name: "OMO defaults", nativeDefault: true, efforts: [] });
  expect(menu.error).toBe("Native catalog needs a session");
  expect(menu.capabilities.configure).toBe(false);
  expect(current("omo", [], {}, {}).model).toBeNull();
  expect(JSON.parse(argvFor({ agent: "omo", model: "" }, {}).argv[2])).not.toHaveProperty("model");
  expect(menuFor("omo", { native: { capabilities: { configure: true } } }, "actual").capabilities.configure).toBe(true);
});

test("a session-targeted native model keeps its provider-qualified identity for effort-only Apply", () => {
  const meta = { model: { id: "model-a", provider: "native" }, thinking: "high" };
  const selected = current("hermes", [], meta, { model: "native:model-a", models: [{ id: "native:model-a", label: "Model A" }] });
  expect(selected).toMatchObject({ model: "native:model-a", label: "Model A", effort: "high" });
  expect(current("hermes", [], meta, { model: null }).model).toBe("model-a");
});
