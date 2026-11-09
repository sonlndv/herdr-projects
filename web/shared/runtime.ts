import type { AgentPick, Chat, PlaceTab, Question } from './types.ts'

/** Known names are display metadata only, never proof of supported controls. */
const NAMES: Record<string, string> = { claude: 'Claude Code', codex: 'Codex', omp: 'OMP', omo: 'OMO', omh: 'OMH', hermes: 'Hermes', pi: 'Pi' }
export const runtimeName = (chat?: Chat) => chat?.runtime?.name || NAMES[chat?.runtime?.kind ?? chat?.agent?.kind ?? chat?.cli ?? ''] || chat?.cli || 'Agent'

/** Legacy readers predate capability metadata. New runtimes must advertise history explicitly. */
export const tabHasChat = (tab?: Pick<PlaceTab, 'cli' | 'chatSupported'>, chat?: Chat) =>
  tab?.chatSupported ?? chat?.runtime?.capabilities.history ?? (tab?.cli === 'claude' || tab?.cli === 'codex')

export const canAnswerQuestion = (q: Question, chat: Chat) =>
  q.native ? q.native.answerable && (chat.runtime?.capabilities.answer === true || q.kind === 'Plan' && chat.runtime?.capabilities.answerPlan === true) : !chat.runtime

/** Never translate full-access permission ids into an unrelated runtime's policy. */
export function permissionForAgent(was: AgentPick, agent: string, modes: { id: string }[], defaultMode?: string | null) {
  if (was.agent === agent) return modes.some((m) => m.id === was.perm) ? was.perm ?? null : defaultMode ?? null
  const legacy = (was.agent === 'claude' || was.agent === 'codex') && (agent === 'claude' || agent === 'codex')
  const full = legacy && (was.perm === 'bypassPermissions' || was.perm === 'full')
  const wanted = full ? agent === 'claude' ? 'bypassPermissions' : 'full' : defaultMode
  return modes.some((m) => m.id === wanted) ? wanted ?? null : null
}

/** Native forms preserve option values and stable question ids; display summaries are never their wire format. */
export function nativeFormAnswers(q: Question, picks: number[][], others: (string | null)[]): Record<string, { selected: string[]; text?: string }> {
  const questions = q.native?.questions
  if (!questions || !q.parts || questions.length !== q.parts.length) throw new Error('Native form identity is incomplete.')
  const answers: Record<string, { selected: string[]; text?: string }> = Object.create(null)
  for (const [index, question] of questions.entries()) {
    if (!question.id || Object.hasOwn(answers, question.id)) throw new Error('Native form question ids must be unique.')
    const part = q.parts[index]
    const text = others[index]?.trim() ?? ''
    const selected = picks[index]?.map((choiceIndex) => {
      const choice = part.choices[choiceIndex]
      if (!choice) throw new Error('The native form option is no longer available.')
      if (choice.value !== undefined && typeof choice.value !== 'string') throw new Error('The native form option has an unsupported value.')
      return choice.value ?? choice.label
    }) ?? []
    if (!question.multi && selected.length > 1) throw new Error('This native question accepts one option.')
    answers[question.id] = { selected: text && !question.multi ? [] : selected, ...(text ? { text } : {}) }
  }
  return answers
}
