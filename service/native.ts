// Validate native IPC once before projecting it into web controls. Native policy remains authoritative.
import type { ChatRuntime, NativeWork, Question } from "../web/shared/types.ts";
import { nativeQuestionId } from "../core/native-runtime.js";

export type NativeChoice = { id?: string; label: string; description?: string; value?: unknown };
export type NativePart = { id: string; title: string; header?: string; multi: boolean; choices: NativeChoice[] };
export type NativeRequest = { id: string; kind: "plan" | "question" | "permission"; title: string; detail?: string;
  choices: NativeChoice[]; answerable: boolean; other: boolean; at?: number; questions: NativePart[] };
export type NativeState = { sessionId: string; sessionFile?: string; working: boolean | null; settled: boolean | null;
  pendingAsyncWork: boolean | null; capabilities: ChatRuntime["capabilities"]; pendingRequests: NativeRequest[];
  jobs: NativeWork[]; subagents: NativeWork[] };

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

function work(value: unknown): NativeWork[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item: unknown) => {
    const row = object(item);
    if (!row || typeof row.id !== "string") return [];
    return [{ id: row.id, title: typeof row.title === "string" ? row.title : typeof row.label === "string" ? row.label : row.id,
      ...(typeof row.kind === "string" ? { kind: row.kind } : {}), ...(typeof row.status === "string" ? { status: row.status } : {}),
      ...(typeof row.parentId === "string" ? { parentId: row.parentId } : {}),
      ...(Array.isArray(row.dependencies) ? { dependencies: row.dependencies.filter((id): id is string => typeof id === "string") } : {}),
      ...(Object.hasOwn(row, "details") ? { details: row.details } : {}) }];
  });
}

export function nativeState(value: unknown): NativeState | null {
  const state = object(value);
  if (!state || typeof state.sessionId !== "string" || !state.sessionId) return null;
  const caps = object(state.capabilities) ?? {};
  const capabilities: ChatRuntime["capabilities"] = {
    history: caps.history === true, send: caps.send === true, interrupt: caps.interrupt === true,
    configure: caps.configure === true, answer: caps.answer === true, fork: caps.fork === true,
    ...(typeof caps.answerPlan === "boolean" ? { answerPlan: caps.answerPlan } : {}),
    ...(typeof caps.steer === "boolean" ? { steer: caps.steer } : {}),
    ...(typeof caps.images === "boolean" ? { images: caps.images } : {}),
  };
  const requests: NativeRequest[] = [];
  for (const raw of Array.isArray(state.pendingRequests) ? state.pendingRequests : []) {
    const request = object(raw);
    if (!request || typeof request.id !== "string" || typeof request.title !== "string") continue;
    const choices = (Array.isArray(request.choices) ? request.choices : []).flatMap((rawChoice: unknown): NativeChoice[] => {
      const choice = object(rawChoice);
      if (!choice || typeof choice.label !== "string") return [];
      return [{ label: choice.label, ...(typeof choice.id === "string" ? { id: choice.id } : {}),
        ...(typeof choice.description === "string" ? { description: choice.description } : {}),
        ...(Object.hasOwn(choice, "value") ? { value: choice.value } : {}) }];
    });
    const questions: NativePart[] = [];
    for (const rawPart of Array.isArray(request.questions) ? request.questions : []) {
      const part = object(rawPart);
      if (!part || typeof part.id !== "string" || typeof part.question !== "string") continue;
      const options: NativeChoice[] = (Array.isArray(part.options) ? part.options : []).flatMap((rawOption: unknown) => {
        const option = object(rawOption);
        return option && typeof option.label === "string"
          ? [{ label: option.label, value: option.label, ...(typeof option.description === "string" ? { description: option.description } : {}) }] : [];
      });
      questions.push({ id: part.id, title: part.question, ...(typeof part.header === "string" ? { header: part.header } : {}),
        multi: part.multiSelect === true, choices: options });
    }
    const kind = request.kind === "plan" || request.kind === "permission" ? request.kind : "question";
    requests.push({ id: request.id, kind, title: request.title, choices, questions, answerable: request.answerable === true,
      other: request.other === true, ...(typeof request.detail === "string" ? { detail: request.detail } : {}),
      ...(typeof request.at === "number" ? { at: request.at } : {}) });
  }
  return { sessionId: state.sessionId, ...(typeof state.sessionFile === "string" ? { sessionFile: state.sessionFile } : {}),
    working: typeof state.working === "boolean" ? state.working : null,
    settled: typeof state.settled === "boolean" ? state.settled : null,
    pendingAsyncWork: typeof state.pendingAsyncWork === "boolean" ? state.pendingAsyncWork : null,
    capabilities, pendingRequests: requests, jobs: work(state.jobs), subagents: work(state.subagents) };
}

export function nativeQuestion(request: NativeRequest, key: string, state: NativeState): Question {
  const refine = request.choices.findIndex((choice) => choice.id === "refine" || choice.value === "refine"
    || object(choice.value)?.choice === "refine");
  return { id: nativeQuestionId(state.sessionId, request.id), title: request.title, kind: request.kind === "plan" ? "Plan" : "Question",
    to: key, native: { requestId: request.id, answerable: request.answerable,
      ...(request.questions.length ? { questions: request.questions.map((part) => ({ id: part.id, multi: part.multi })) } : {}) }, terminal: !request.answerable,
    choices: request.choices.map((choice) => ({ label: choice.label, description: choice.description,
      value: Object.hasOwn(choice, "value") ? choice.value : choice.id ?? choice.label })),
    ...(request.questions.length ? { parts: request.questions.map((part) => ({ title: part.title, header: part.header,
      multi: part.multi, choices: part.choices })) } : {}),
    other: request.answerable && request.other, ...(request.detail ? { plan: request.detail } : {}),
    ...(refine >= 0 ? { noteRequiredFor: refine } : {}), ...(request.kind === "permission" ? { noNote: true } : {}) };
}

/** Translate only answers to the still-open native request; preserve opaque choice values. */
export function nativeAnswer(request: NativeRequest, value: unknown): unknown {
  const payload = object(value);
  if (!payload || !request.answerable) throw new Error("This request must be answered in the native terminal.");
  if (payload.dismiss || payload.talk) throw new Error("Use the native request's explicit choices.");
  if (request.questions.length) {
    const submitted = object(payload.nativeAnswers);
    if (!submitted) throw new Error("Submit this native form's structured answers.");
    const allowed = new Map(request.questions.map((part) => [part.id, part]));
    const answers: Record<string, { selected: string[]; text?: string }> = {};
    for (const [id, raw] of Object.entries(submitted)) {
      const part = allowed.get(id), answer = object(raw);
      if (!part || !answer || !Array.isArray(answer.selected)) throw new Error("That native form changed; reopen it before answering.");
      const offered = new Set(part.choices.map((choice) => choice.label));
      if (!answer.selected.every((selected): selected is string => typeof selected === "string" && offered.has(selected))
        || new Set(answer.selected).size !== answer.selected.length || (!part.multi && answer.selected.length > 1))
        throw new Error("Pick only this native question's offered choices.");
      const text = typeof answer.text === "string" ? answer.text.slice(0, 4000) : undefined;
      answers[id] = { selected: answer.selected, ...(text ? { text } : {}) };
    }
    if (!Object.values(answers).some((answer) => answer.selected.length || answer.text?.trim()))
      throw new Error("Answer at least one native question.");
    return { answers };
  }
  const valueAt = (index: unknown) => {
    if (!Number.isInteger(index) || Number(index) < 0 || Number(index) >= request.choices.length)
      throw new Error("Pick one of this native request's choices.");
    const selected = request.choices[Number(index)];
    return Object.hasOwn(selected, "value") ? selected.value : selected.id ?? selected.label;
  };
  let answer: unknown;
  if (typeof payload.other === "string" && payload.other.trim() && request.other) answer = payload.other.slice(0, 4000);
  else if (payload.choice !== undefined) answer = valueAt(payload.choice);
  else throw new Error("Answer this request through its native choices.");
  if (request.kind === "plan") {
    const picked = object(answer) ?? { choice: answer };
    return { ...picked, ...(typeof payload.note === "string" ? { feedback: payload.note.slice(0, 4000) } : {}) };
  }
  return answer;
}
