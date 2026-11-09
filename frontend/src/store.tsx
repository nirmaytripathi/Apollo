// Run store: the one place pages get run data from. Holds the run list (polled), the
// selected run, its live events and derive() view, and its report and metrics once available.
// Pages never fetch run data themselves, so switching pages never restarts a stream.

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import {
  continueRun, getMetrics, getReport, getRunStatus, getSystem, isTransient, listRuns, pauseRun, restartRun, resumeRun, startRun, stopRun,
  type AnyEvent, type Metrics, type Report, type ResumeAction, type RunSummary, type SystemInfo,
} from './api'
import { derive, type Derived } from './derive'
import { runFromUrl } from './router'
import { useRunEvents } from './useRunEvents'

const LIST_POLL_MS = 4000
// A brief outage (server restarting, Render waking up) should not flash an error: only report the run
// list as unreachable after this many failed polls in a row (about 12 s).
const LIST_FAILURES_BEFORE_ERROR = 3
const METRICS_POLL_MS = 3000

export interface Store {
  runs: RunSummary[]
  runId: string | null
  selected: RunSummary | null
  select: (id: string) => void
  events: AnyEvent[]
  d: Derived
  maxRounds: number
  busy: boolean
  report: Report | null
  metrics: Metrics | null
  problem: string | null
  setProblem: (m: string | null) => void
  start: (topic: string, rounds: number, hitl: boolean, documents?: string[]) => Promise<boolean>
  answer: (action: ResumeAction, payload?: { sub_questions?: string[]; gaps?: string[] }) => Promise<void>
  /** Stop: ask the server to end the selected run at its next safe point (it can then be restarted). */
  stop: () => Promise<void>
  /** True from pressing Stop until the run has actually stopped. */
  stopping: boolean
  /** Pause: the run waits after its current step. */
  pause: () => Promise<void>
  /** True from pressing Pause until the run reports it is paused. */
  pausing: boolean
  /** Resume a paused run: the same run carries on from where it stopped. */
  unpause: () => Promise<void>
  /** Stop & Edit: pause the run (unless it already waits for approval) and open the topic editor. */
  openEdit: () => Promise<void>
  /** True while the topic editor is open for the selected run. */
  editing: boolean
  /** Resume from the editor: close it and carry the same run on with its old topic. */
  closeEdit: () => Promise<void>
  /** Update & restart: end the selected run if it is still alive, then start a new run with `topic`. */
  restartEdited: (topic: string) => Promise<boolean>
  /** Start a new run with an edited topic and the selected (stopped) run's settings; selects it. */
  restart: (topic: string) => Promise<boolean>
  showHitl: boolean
  refreshRuns: () => void
  system: SystemInfo | null
  refreshSystem: () => void
  backendOk: boolean | null // null until the first run-list request answers
  reconnecting: boolean // the live stream dropped and is reconnecting by itself
}

const Ctx = createContext<Store | null>(null)

export function useStore(): Store {
  const s = useContext(Ctx)
  if (!s) throw new Error('useStore outside StoreProvider')
  return s
}

export function StoreProvider({ children }: { children: ReactNode }) {
  const [runs, setRuns] = useState<RunSummary[]>([])
  const [runId, setRunId] = useState<string | null>(runFromUrl) // a shared link opens its run
  const [report, setReport] = useState<Report | null>(null)
  const [metrics, setMetrics] = useState<Metrics | null>(null)
  const [answeredAt, setAnsweredAt] = useState(-1) // event index of the last decision we sent
  const [problem, setProblem] = useState<string | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const [system, setSystem] = useState<SystemInfo | null>(null)
  const [sysTick, setSysTick] = useState(0)
  const [backendOk, setBackendOk] = useState<boolean | null>(null)

  const stream = useRunEvents(runId)
  const selected = runs.find((r) => r.run_id === runId) ?? null
  const maxRounds = selected?.max_rounds ?? 2
  const d = derive(stream.events, maxRounds)
  const busy = runId !== null && !d.finished && !stream.connectionError // still busy while reconnecting
  const refreshRuns = useCallback(() => setTick((t) => t + 1), [])

  // Run list: polled, so runs started elsewhere (CLI tests, another tab) and status changes show up.
  useEffect(() => {
    let live = true
    let failures = 0
    const load = () =>
      listRuns()
        .then((list) => {
          if (!live) return
          failures = 0
          setRuns(list)
          setListError(null) // a poll that works again clears the earlier failure
          setBackendOk(true)
          setRunId((cur) => cur ?? list[0]?.run_id ?? null) // default: the newest run
        })
        .catch((e: unknown) => {
          if (!live) return
          failures += 1
          if (failures < LIST_FAILURES_BEFORE_ERROR) return // keep showing the last list; the next poll retries
          setBackendOk(false)
          setListError(isTransient(e) ? "Can't reach the server right now. Retrying automatically." : e instanceof Error ? e.message : 'Could not load your runs.')
        })
    load()
    const t = window.setInterval(load, LIST_POLL_MS)
    return () => {
      live = false
      window.clearInterval(t)
    }
  }, [tick])

  // System info (provider, key presence, limits, security checks): once, and on request.
  useEffect(() => {
    let live = true
    getSystem().then((s) => live && setSystem(s)).catch(() => live && setSystem(null))
    return () => {
      live = false
    }
  }, [sysTick])
  const refreshSystem = useCallback(() => setSysTick((t) => t + 1), [])

  // Report: once the selected run says it is ready.
  useEffect(() => {
    setReport(null)
    if (!runId || !d.reportReady) return
    let live = true
    getReport(runId)
      .then((r) => live && setReport(r))
      .catch((e: unknown) => live && setProblem(e instanceof Error ? `Report: ${e.message}` : 'Could not load the report.'))
    return () => {
      live = false
    }
  }, [runId, d.reportReady])

  // Metrics: polled while the run is going, fetched once more when it ends.
  useEffect(() => {
    setMetrics(null)
    if (!runId) return
    let live = true
    const load = () => getMetrics(runId).then((m) => live && setMetrics(m)).catch(() => undefined)
    load()
    const t = busy ? window.setInterval(load, METRICS_POLL_MS) : undefined
    return () => {
      live = false
      if (t) window.clearInterval(t)
    }
  }, [runId, busy])

  const select = useCallback((id: string) => {
    setAnsweredAt(-1)
    setRunId(id)
  }, [])

  // A ?run= link pasted into an open tab selects that run too.
  useEffect(() => {
    const on = () => {
      const id = runFromUrl()
      if (id) select(id)
    }
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [select])

  const start = async (topic: string, rounds: number, hitl: boolean, documents: string[] = []) => {
    setProblem(null)
    try {
      const { run_id } = await startRun(topic, rounds, hitl, documents)
      setAnsweredAt(-1)
      setRunId(run_id)
      refreshRuns()
      return true
    } catch (e) {
      setProblem(e instanceof Error ? e.message : 'Could not start the run.')
      return false
    }
  }

  const answer = async (action: ResumeAction, payload?: { sub_questions?: string[]; gaps?: string[] }) => {
    if (!runId) return
    setAnsweredAt(d.pendingAt) // hide the panel now; the next events confirm the decision
    try {
      await resumeRun(runId, action, payload)
    } catch (e) {
      setAnsweredAt(-1)
      setProblem(e instanceof Error ? e.message : 'Could not send the decision.')
    }
  }

  const [stopRequestedFor, setStopRequestedFor] = useState<string | null>(null)
  const stopping = stopRequestedFor !== null && stopRequestedFor === runId && busy

  const stop = async () => {
    if (!runId) return
    setProblem(null)
    setStopRequestedFor(runId)
    try {
      await stopRun(runId)
    } catch (e) {
      setStopRequestedFor(null)
      setProblem(e instanceof Error ? e.message : 'Could not pause the run.')
    }
  }

  const [pauseRequestedFor, setPauseRequestedFor] = useState<string | null>(null)
  const pausing = pauseRequestedFor !== null && pauseRequestedFor === runId && busy && !d.paused

  const pause = async () => {
    if (!runId) return
    setProblem(null)
    setPauseRequestedFor(runId)
    try {
      await pauseRun(runId)
    } catch (e) {
      setPauseRequestedFor(null)
      setProblem(e instanceof Error ? e.message : 'Could not pause the run.')
    }
  }

  const unpause = async () => {
    if (!runId) return
    setProblem(null)
    setPauseRequestedFor(null)
    try {
      await continueRun(runId)
    } catch (e) {
      setProblem(e instanceof Error ? e.message : 'Could not resume the run.')
    }
  }

  const [editFor, setEditFor] = useState<string | null>(null)
  const editing = editFor !== null && editFor === runId

  const openEdit = async () => {
    if (!runId) return
    setEditFor(runId)
    // A run waiting for approval is already waiting for the user: it is not paused, Resume just closes the editor.
    if (busy && !d.paused && !d.planPending && !d.gapsPending) await pause()
  }

  const closeEdit = async () => {
    setEditFor(null)
    if (busy && (d.paused || pausing)) await unpause()
  }

  const restartEdited = async (topic: string) => {
    if (!runId) return false
    if (busy) {
      // The run is paused (or about to be): end it first; /restart needs a finished run.
      try {
        await stopRun(runId)
      } catch (e) {
        setProblem(e instanceof Error ? e.message : 'Could not stop the run.')
        return false
      }
      const until = Date.now() + 90_000
      for (;;) {
        const { status } = await getRunStatus(runId).catch(() => ({ status: 'running' }))
        if (!['running', 'paused', 'awaiting_input'].includes(status)) break
        if (Date.now() > until) {
          setProblem('The run is taking long to stop; try Update & restart again in a moment.')
          return false
        }
        await new Promise((r) => setTimeout(r, 700))
      }
    }
    const ok = await restart(topic)
    if (ok) setEditFor(null)
    return ok
  }

  const restart = async (topic: string) => {
    if (!runId) return false
    setProblem(null)
    try {
      const out = await restartRun(runId, topic)
      if (out.documents_lost) setProblem('The restarted run has no attached documents: the server no longer had them. Attach them again in a new run if you need them.')
      setAnsweredAt(-1)
      setStopRequestedFor(null)
      setRunId(out.run_id)
      refreshRuns()
      return true
    } catch (e) {
      setProblem(e instanceof Error ? e.message : 'Could not restart the research.')
      return false
    }
  }

  const showHitl = !stopping && d.pendingAt > answeredAt && (d.planPending || d.gapsPending !== null)
  const connProblem = stream.connectionError ?? (d.status === 'failed' ? d.errorMsg : null)

  return (
    <Ctx.Provider
      value={{
        runs, runId, selected, select, events: stream.events, d, maxRounds, busy, report, metrics,
        problem: problem ?? listError ?? connProblem, setProblem, start, answer, stop, stopping, pause, pausing, unpause, openEdit, editing, closeEdit, restartEdited, restart, showHitl, refreshRuns,
        system, refreshSystem, backendOk, reconnecting: stream.reconnecting,
      }}
    >
      {children}
    </Ctx.Provider>
  )
}
