import { expect, test } from 'bun:test'
import { canAnswerQuestion, nativeFormAnswers, permissionForAgent, runtimeName, tabHasChat } from './runtime.ts'
import type { Chat, Question } from './types.ts'

const native = (kind = 'omp', capabilities: Partial<NonNullable<Chat['runtime']>['capabilities']> = {}): Chat => ({
  key: 't:machine/t1', cli: kind, messages: [], runtime: { kind, name: kind.toUpperCase(), sessionId: 'exact-native-session', capabilities: { history: true, send: true, interrupt: true, configure: true, answer: false, fork: false, ...capabilities } },
})
const question = (change: Partial<Question> = {}): Question => ({ id: 'native-request-1', title: 'Approve?', kind: 'Question', to: 'agent', choices: [{ label: 'Allow', value: { approved: true } }], native: { requestId: 'native-request-1', answerable: true }, ...change })

test('native display identity never becomes Claude', () => {
  for (const kind of ['omp', 'omo', 'omh', 'hermes']) expect(runtimeName(native(kind))).toBe(kind.toUpperCase())
  expect(runtimeName({ key: 'k', cli: '', messages: [] })).toBe('Agent')
  expect(runtimeName({ key: 'k', cli: 'claude', messages: [] })).toBe('Claude Code')
  expect(runtimeName({ key: 'k', cli: 'codex', messages: [] })).toBe('Codex')
})

test('native chat support is explicit and uses the same terminal session', () => {
  expect(tabHasChat({ cli: 'omp' })).toBe(false)
  expect(tabHasChat({ cli: 'omp', chatSupported: true })).toBe(true)
  const chat = native()
  expect(tabHasChat({ cli: 'omp' }, chat)).toBe(true)
  expect(chat.key).toBe('t:machine/t1')
  expect(chat.runtime?.sessionId).toBe('exact-native-session')
  expect(tabHasChat({ cli: 'omp', chatSupported: false }, chat)).toBe(false)
  expect(tabHasChat({ cli: 'claude' })).toBe(true)
  expect(tabHasChat({ cli: 'codex' })).toBe(true)
})

test('unsupported and native-only requests never offer a web answer', () => {
  expect(canAnswerQuestion(question(), native())).toBe(false)
  expect(canAnswerQuestion(question(), native('omo', { answer: true }))).toBe(true)
  expect(canAnswerQuestion(question({ native: { requestId: 'r', answerable: false } }), native('omo', { answer: true }))).toBe(false)
  expect(canAnswerQuestion(question({ native: undefined }), native('omo', { answer: true }))).toBe(false)
  const legacy: Chat = { key: 'k', cli: 'claude', messages: [] }
  expect(canAnswerQuestion(question({ native: undefined }), legacy)).toBe(true)
})

test('plan approval is not tool approval permission', () => {
  const chat = native('omp', { answerPlan: true })
  expect(canAnswerQuestion(question({ kind: 'Plan' }), chat)).toBe(true)
  expect(canAnswerQuestion(question(), chat)).toBe(false)
})

test('permission switching preserves legacy behavior without granting native full access', () => {
  expect(permissionForAgent({ agent: 'claude', perm: 'bypassPermissions' }, 'codex', [{ id: 'full' }], 'safe')).toBe('full')
  expect(permissionForAgent({ agent: 'codex', perm: 'full' }, 'claude', [{ id: 'bypassPermissions' }], 'default')).toBe('bypassPermissions')
  expect(permissionForAgent({ agent: 'claude', perm: 'bypassPermissions' }, 'omp', [{ id: 'full' }, { id: 'ask' }], 'ask')).toBe('ask')
  expect(permissionForAgent({ agent: 'omo', perm: 'full' }, 'hermes', [], 'full')).toBe(null)
  expect(permissionForAgent({ agent: 'omp', perm: 'native-policy' }, 'omp', [{ id: 'native-policy' }])).toBe('native-policy')
})

test('native forms preserve comma-containing values, stable ids, and multi-select free text', () => {
  const q = question({
    native: { requestId: 'form-request', answerable: true, questions: [{ id: 'network,question', multi: true }, { id: 'second', multi: false }] },
    parts: [
      { title: 'Pick services', multi: true, choices: [{ label: 'Display A', value: 'api, worker' }, { label: 'database, cache' }] },
      { title: 'Explain', choices: [{ label: 'Existing choice' }] },
    ],
  })
  expect(nativeFormAnswers(q, [[0, 1], [0]], ['  custom, service  ', '  another answer  '])).toEqual({
    'network,question': { selected: ['api, worker', 'database, cache'], text: 'custom, service' },
    second: { selected: [], text: 'another answer' },
  })
})

test('a single native form part keeps its stable question identity', () => {
  const q = question({
    native: { requestId: 'request', answerable: true, questions: [{ id: 'only-question' }] },
    parts: [{ title: 'Choose', choices: [{ label: 'A, B', value: 'native,a,b' }] }],
  })
  expect(nativeFormAnswers(q, [[0]], [null])).toEqual({ 'only-question': { selected: ['native,a,b'] } })
  expect(() => nativeFormAnswers(q, [[2]], [null])).toThrow('no longer available')
})
