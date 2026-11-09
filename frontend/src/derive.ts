// derive(events): the ONLY view of a run the UI reads. Pure function over the
// event list; it never sees graph state and tolerates missing or unknown fields.

import { isKnown, toMs, type AnyEvent, type RunEvent } from './api'

export type AgentStatus = 'idle' | 'active' | 'done' | 'error'

export interface Derived {
  round: number
  subQuestions: string[]
  planPending: boolean
  gapsPending: string[] | null
  pendingAt: number // event index of the decision being waited for (-1 if none)
  apollo: AgentStatus
  athena: AgentStatus
  hermes: AgentStatus
  labels: { apollo: string; athena: string; hermes: string }
  searchDone: number
  searchTotal: number
  failed: number
  finished: boolean
  status: 'idle' | 'running' | 'complete' | 'failed'
  errorMsg: string | null
  reportReady: boolean
  startMs: number | null
  endMs: number | null
  /** 0-100 from the backend's own events (searches, sections, claims checked), never a timer. */
  progress: number
  stopReason: string | null // from the final "complete" event; "stopped_by_user" after Stop
  paused: boolean // the user paused the run and it is waiting (resume carries the same run on)
}

// Share of the bar each stage owns, in run order. A stage's share fills as its own events arrive:
// searches done of searches started, sections written of sub-questions, claims checked of claims to check.
const BAND = { plan: 8, search: 40, write: 25, verify: 22 } as const

type Phase = 'planning' | 'awaiting_plan' | 'searching' | 'reviewing' | 'awaiting_gaps' | 'writing' | 'verifying' | 'finishing' | 'done' | 'failed'

export function derive(events: AnyEvent[], maxRounds = 2): Derived {
  const known: { e: RunEvent; i: number }[] = []
  events.forEach((e, i) => isKnown(e) && known.push({ e, i }))

  let round = 0
  let subQuestions: string[] = []
  let planPending = false
  let gapsPending: string[] | null = null
  let pendingAt = -1
  let searchTotal = 0
  let searchDone = 0
  let failed = 0
  let sections = 0
  let reportReady = false
  let errorMsg: string | null = null
  let status: Derived['status'] = events.length ? 'running' : 'idle'
  let phase: Phase = 'planning'
  let lastReviewRound = 0
  let claimsDone = 0
  let claimsTotal = 0
  let searchHits = 0 // queries whose results arrived (live, per query) in the current round
  let paused = false
  let stopReason: string | null = null

  for (const { e, i } of known) {
    switch (e.type) {
      case 'plan':
        subQuestions = Array.isArray(e.data.sub_questions) ? e.data.sub_questions : subQuestions
        planPending = !!e.data.awaiting_approval
        pendingAt = planPending ? i : -1
        phase = planPending ? 'awaiting_plan' : 'planning'
        break
      case 'search_started': {
        planPending = false
        gapsPending = null
        pendingAt = -1
        const r = e.data.round ?? round + 1
        if (e.data.index === 1 || r !== round) {
          searchDone = 0 // index 1 starts a new batch
          searchHits = 0
        }
        round = Math.max(round, r)
        searchTotal = e.data.total ?? Math.max(searchTotal, e.data.index ?? 0)
        phase = 'searching'
        break
      }
      case 'paused':
        paused = true
        break
      case 'resumed':
        paused = false
        break
      case 'search_results': // arrives as each query's search API answers, before its pages are read
        searchHits += 1
        break
      case 'search_done':
      case 'search_failed':
        searchDone += 1
        if (e.type === 'search_failed') failed += 1
        phase = searchTotal && searchDone >= searchTotal ? 'reviewing' : 'searching'
        break
      case 'review': {
        lastReviewRound = e.data.round ?? round
        const gaps = Array.isArray(e.data.gaps) ? e.data.gaps : []
        // Next: another round (search_started / gaps_pending arrives) or writing.
        phase = e.data.complete || gaps.length === 0 || lastReviewRound >= maxRounds ? 'writing' : 'searching'
        break
      }
      case 'gaps_pending':
        gapsPending = Array.isArray(e.data.gaps) ? e.data.gaps : []
        pendingAt = i
        phase = 'awaiting_gaps'
        break
      case 'section_written':
        gapsPending = null
        pendingAt = -1
        sections += 1
        phase = subQuestions.length && sections >= subQuestions.length ? 'verifying' : 'writing'
        break
      case 'claim_checked':
        claimsDone = Math.max(claimsDone, e.data.done ?? claimsDone + 1)
        claimsTotal = Math.max(claimsTotal, e.data.total ?? 0)
        phase = 'verifying'
        break
      case 'verification':
        phase = 'finishing'
        break
      case 'report_ready':
        reportReady = true
        break
      case 'error':
        errorMsg = e.data.message || 'The run stopped with an error.'
        status = 'failed'
        phase = 'failed'
        break
      case 'complete':
        status = e.data.status === 'failed' ? 'failed' : 'complete'
        phase = status === 'failed' ? 'failed' : 'done'
        stopReason = e.data.stop_reason ?? null
        paused = false
        planPending = false
        gapsPending = null
        pendingAt = -1
        break
    }
  }

  const finished = status === 'complete' || status === 'failed'
  const started = events.length > 0

  // Agent status from the current phase.
  const after = (p: Phase[]) => p.includes(phase)
  let apollo: AgentStatus = !started ? 'idle' : after(['planning', 'writing', 'verifying', 'finishing']) ? 'active' : after(['done']) ? 'done' : 'idle'
  let athena: AgentStatus = phase === 'searching' ? 'active' : round > 0 ? 'done' : 'idle'
  let hermes: AgentStatus = phase === 'reviewing' ? 'active' : lastReviewRound > 0 || phase === 'awaiting_gaps' ? 'done' : 'idle'
  if (status === 'complete') apollo = athena = hermes = 'done'
  if (status === 'failed') {
    // mark the agent that was working when the run stopped
    if (athena === 'active') athena = 'error'
    else if (hermes === 'active') hermes = 'error'
    else apollo = 'error'
  }

  const labels = {
    apollo:
      !started ? 'idle' :
      status === 'failed' && apollo === 'error' ? 'stopped' :
      phase === 'planning' ? (subQuestions.length ? `planned ${subQuestions.length} sub-questions` : 'planning') :
      phase === 'awaiting_plan' ? 'waiting for your approval' :
      phase === 'writing' ? (sections ? `writing · ${sections}/${subQuestions.length || '?'} sections` : 'writing sections') :
      phase === 'verifying' ? 'verifying claims' :
      phase === 'finishing' ? 'assembling report' :
      phase === 'done' ? 'report ready' :
      subQuestions.length ? `${subQuestions.length} sub-questions` : 'idle',
    athena:
      athena === 'error' ? 'stopped' :
      phase === 'searching' ? `searching ${Math.min(searchDone + 1, searchTotal || 1)}/${searchTotal || '?'}` :
      round > 0 ? `${round} round${round === 1 ? '' : 's'} searched${failed ? ` · ${failed} failed` : ''}` :
      'idle',
    hermes:
      hermes === 'error' ? 'stopped' :
      phase === 'reviewing' ? `reviewing round ${round}` :
      phase === 'awaiting_gaps' ? 'gaps await your approval' :
      lastReviewRound > 0 ? `reviewed round ${lastReviewRound}` :
      'idle',
  }

  // Progress: each finished stage adds its whole band; the stage in progress adds its done/total share.
  const share = (done: number, total: number) => (total > 0 ? Math.min(1, done / total) : 0)
  const rounds = Math.max(1, maxRounds)
  // A round's searches: half for the API answering each query (live), half for its pages read (end of the round).
  const roundShare = (share(Math.min(searchHits, searchTotal), searchTotal) + share(searchDone, searchTotal)) / 2
  const searched = lastReviewRound >= round && round > 0 ? round : Math.max(0, round - 1) + roundShare
  const writingDone = ['verifying', 'finishing', 'done'].includes(phase)
  let progress =
    (subQuestions.length && !planPending ? BAND.plan : 0) +
    (['writing', 'verifying', 'finishing', 'done'].includes(phase) ? BAND.search : BAND.search * share(searched, rounds)) +
    (writingDone ? BAND.write : BAND.write * share(sections, subQuestions.length)) +
    (['finishing', 'done'].includes(phase) ? BAND.verify : BAND.verify * share(claimsDone, claimsTotal))
  if (reportReady) progress = Math.max(progress, 99)
  if (status === 'complete') progress = 100
  progress = Math.round(Math.min(100, progress))

  const times = events.map((e) => toMs(e.ts)).filter((t) => Number.isFinite(t))
  return {
    round,
    subQuestions,
    planPending,
    gapsPending,
    pendingAt,
    apollo,
    athena,
    hermes,
    labels,
    searchDone,
    searchTotal,
    failed,
    finished,
    status,
    errorMsg,
    reportReady,
    startMs: times.length ? Math.min(...times) : null,
    endMs: finished && times.length ? Math.max(...times) : null,
    progress,
    stopReason,
    paused,
  }
}
