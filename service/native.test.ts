import { expect, test } from "bun:test";
import { nativeAnswer, nativeQuestion, nativeState } from "./native.ts";

function stateWith(request: unknown, sessionId = "native-session") {
  const state = nativeState({ sessionId, capabilities: { history: true, answer: true, answerPlan: true }, pendingRequests: [request] });
  if (!state || !state.pendingRequests[0]) throw new Error("Invalid test native request");
  return state;
}

test("unobserved native settlement stays unknown and truthy external values do not enable controls", () => {
  const state = nativeState({ sessionId: "s", capabilities: { send: "true", interrupt: 1, answer: true }, working: "false" });
  expect(state).toMatchObject({ working: null, settled: null, pendingAsyncWork: null,
    capabilities: { send: false, interrupt: false, answer: true, configure: false, fork: false } });
  expect(nativeState({ sessionId: "", capabilities: { send: true } })).toBeNull();
});

test("native approval choices retain false values and cannot be implicitly dismissed", () => {
  const state = stateWith({ id: "approval", kind: "permission", title: "Run a command?", answerable: true,
    choices: [{ label: "Allow", value: true }, { label: "Deny", value: false }] });
  const request = state.pendingRequests[0];
  expect(nativeAnswer(request, { choice: 1 })).toBe(false);
  expect(() => nativeAnswer(request, { dismiss: true })).toThrow();
  expect(() => nativeAnswer(request, { choice: 2 })).toThrow();
  expect(() => nativeAnswer({ ...request, answerable: false }, { choice: 0 })).toThrow();
});

test("native plan feedback preserves the offered action object instead of nesting its choice", () => {
  const state = stateWith({ id: "plan", kind: "plan", title: "Review plan", answerable: true,
    choices: [{ label: "Refine", value: { choice: "refine" } }] });
  const request = state.pendingRequests[0];
  expect(nativeQuestion(request, "t:t1", state).noteRequiredFor).toBe(0);
  expect(nativeAnswer(request, { choice: 0, note: "Add verification" })).toEqual({ choice: "refine", feedback: "Add verification" });
});

test("native form answers use stable question identity and preserve comma-containing option labels", () => {
  const state = stateWith({ id: "form", kind: "question", title: "Deploy?", answerable: true, choices: [], questions: [
    { id: "regions", question: "Regions", multiSelect: true, options: [{ label: "Paris, France" }, { label: "Berlin" }] },
    { id: "strategy", question: "Strategy", options: [{ label: "Canary" }, { label: "Rolling" }] },
  ] });
  const request = state.pendingRequests[0];
  expect(nativeAnswer(request, { nativeAnswers: { regions: { selected: ["Paris, France", "Berlin"], text: "Only EU" } } }))
    .toEqual({ answers: { regions: { selected: ["Paris, France", "Berlin"], text: "Only EU" } } });
  expect(() => nativeAnswer(request, { nativeAnswers: { unknown: { selected: ["Berlin"] } } })).toThrow();
  expect(() => nativeAnswer(request, { nativeAnswers: { regions: { selected: ["Unlisted"] } } })).toThrow();
  expect(() => nativeAnswer(request, { nativeAnswers: { strategy: { selected: ["Canary", "Rolling"] } } })).toThrow();
});

test("native dialog identity changes with the owning session and graph dependencies survive projection", () => {
  const request = { id: "same-id", kind: "permission", title: "Proceed?", choices: [], answerable: false };
  const before = stateWith(request, "before"), after = stateWith(request, "after");
  expect(nativeQuestion(before.pendingRequests[0], "t:t1", before).id).not.toBe(nativeQuestion(after.pendingRequests[0], "t:t1", after).id);
  expect(nativeQuestion(before.pendingRequests[0], "t:t1", before)).toMatchObject({ terminal: true, native: { answerable: false } });
  expect(nativeState({ sessionId: "s", subagents: [{ id: "verify", title: "Verify", parentId: "run", dependencies: ["build"], details: { model: "native" } }] })?.subagents)
    .toEqual([{ id: "verify", title: "Verify", parentId: "run", dependencies: ["build"], details: { model: "native" } }]);
});

test("native free-text questions accept the UI's unselected choice sentinel without approving an offered choice", () => {
  const state = stateWith({ id: "clarify", kind: "question", title: "What should change?", answerable: true, other: true, choices: [] });
  const request = state.pendingRequests[0];
  expect(nativeAnswer(request, { choice: -1, other: "Keep the existing database" })).toBe("Keep the existing database");
  expect(() => nativeAnswer(request, { choice: -1, other: "  " })).toThrow();
  expect(() => nativeAnswer({ ...request, other: false }, { choice: -1, other: "Allow" })).toThrow();
});
