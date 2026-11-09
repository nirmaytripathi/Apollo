// Hook: opens an EventSource for a run, accumulates its events, and closes the stream on a
// terminal event (complete / error).
//
// If the connection drops (network blip, server restart on a small host, redeploy) it reconnects
// by itself, with growing pauses, and asks the server to continue after the last event it saw
// (?last_event_id=), so nothing is repeated or missed. Before reconnecting it checks the run:
// if the server restarted and lost the run's progress, it replays the run's stored ending (the
// backend marks such runs "interrupted") instead of retrying forever. Only after GIVE_UP_MS of
// failures does it report a lost connection.
//
// A connection can also die without an error event (the server process is gone but a proxy keeps
// the socket open). The backend sends a "ping" event every 15 s while a run is quiet, so a silence
// longer than SILENCE_MS is treated as a drop.

import { useEffect, useState } from 'react'
import { ApiError, eventsUrl, getRunStatus, isTransient, parseEvent, type AnyEvent } from './api'

export interface RunStream {
  events: AnyEvent[]
  done: boolean
  reconnecting: boolean
  connectionError: string | null
}

const EMPTY: RunStream = { events: [], done: false, reconnecting: false, connectionError: null }
const GIVE_UP_MS = 2 * 60 * 1000 // a small free server can take a minute or more to restart
const MAX_PAUSE_MS = 10_000
const SILENCE_MS = 40_000 // the backend pings every 15 s
const WATCH_EVERY_MS = 5_000

export function useRunEvents(runId: string | null): RunStream {
  // Keyed by run id, so a new run never shows the previous run's events, even for one render.
  const [state, setState] = useState<{ runId: string | null; stream: RunStream }>({ runId: null, stream: EMPTY })

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let source: EventSource | null = null
    let timer: number | undefined
    let lastId = -1 // id of the last event received
    let attempt = 0
    let failingSince: number | null = null
    let lastHeard = Date.now()
    let watchdog: number | undefined
    let waiting = false // between a drop and the next connection attempt

    const update = (fn: (s: RunStream) => RunStream) =>
      setState((prev) => ({ runId, stream: fn(prev.runId === runId ? prev.stream : EMPTY) }))
    const stop = () => {
      stopped = true
      source?.close()
      window.clearTimeout(timer)
      window.clearInterval(watchdog)
    }
    const giveUp = (message: string) => {
      stop()
      update((s) => (s.done ? { ...s, reconnecting: false } : { ...s, reconnecting: false, connectionError: message }))
    }

    const open = () => {
      waiting = false
      source = new EventSource(lastId >= 0 ? `${eventsUrl(runId)}?last_event_id=${lastId}` : eventsUrl(runId))
      lastHeard = Date.now()
      source.addEventListener('ping', () => {
        lastHeard = Date.now()
      })
      source.onopen = () => {
        lastHeard = Date.now()
        attempt = 0
        failingSince = null
        update((s) => ({ ...s, reconnecting: false, connectionError: null }))
      }
      source.onmessage = (msg: MessageEvent<string>) => {
        lastHeard = Date.now()
        const id = Number(msg.lastEventId)
        if (msg.lastEventId !== '' && Number.isFinite(id)) lastId = id
        const event = parseEvent(msg.data)
        const terminal = event.type === 'complete' || event.type === 'error'
        update((s) => ({ ...s, events: [...s.events, event], done: s.done || terminal }))
        if (terminal) stop()
      }
      source.onerror = () => {
        if (stopped) return
        source?.close()
        retryLater()
      }
    }

    const retryLater = () => {
      failingSince ??= Date.now()
      if (Date.now() - failingSince > GIVE_UP_MS) {
        giveUp('Lost the live connection to the server. Reload the page to try again.')
        return
      }
      waiting = true
      update((s) => ({ ...s, reconnecting: true }))
      const pause = Math.min(1000 * 2 ** attempt, MAX_PAUSE_MS)
      attempt += 1
      timer = window.setTimeout(async () => {
        if (stopped) return
        try {
          const status = await getRunStatus(runId)
          // Fewer events on the server than we already have: it restarted and only kept the stored
          // ending of this run. Read that from the start (it says why the run stopped).
          if (status.events <= lastId) lastId = -1
        } catch (e) {
          if (isTransient(e)) return retryLater() // still restarting: keep waiting
          if (e instanceof ApiError && e.status === 404) {
            giveUp('This run was interrupted because the server restarted. Please start it again.')
            return
          }
          return retryLater()
        }
        if (!stopped) open()
      }, pause)
    }

    open()
    watchdog = window.setInterval(() => {
      // Silent for too long while connected: the connection is dead even though no error arrived.
      if (stopped || waiting) return
      if (Date.now() - lastHeard > SILENCE_MS) {
        source?.close()
        lastHeard = Date.now()
        retryLater()
      }
    }, WATCH_EVERY_MS)
    return stop
  }, [runId])

  return state.runId === runId ? state.stream : EMPTY
}
