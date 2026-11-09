// AgentStrip: the three agents (APOLLO, ATHENA, HERMES) with status dot and live label,
// plus the current round and an elapsed timer computed from the first event's timestamp.
// The working agent's card carries a moving light along its foot and a ringing dot (index.css:
// .agent-active, .dot-live); a finished agent's dot settles in once (.dot-done). Each card is a
// bordered card with a numbered mint strip (01-03); its look per state is in index.css
// (.agent-status-card, .agent-status-active / -done / -error). Every value shown comes from derive().

import { useEffect, useState, type ReactNode } from 'react'
import type { AgentStatus, Derived } from '../derive'

const AGENTS = [
  { key: 'apollo', name: 'APOLLO', role: 'Plans · writes' },
  { key: 'athena', name: 'ATHENA', role: 'Searches' },
  { key: 'hermes', name: 'HERMES', role: 'Reviews' },
] as const

const DOT: Record<AgentStatus, string> = {
  idle: 'bg-muted/40',
  active: 'bg-accent-strong',
  done: 'bg-ok',
  error: 'bg-err',
}

const mmss = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export default function AgentStrip({ d, maxRounds }: { d: Derived; maxRounds: number }) {
  const [now, setNow] = useState(() => Date.now())
  const ticking = d.startMs !== null && !d.finished
  useEffect(() => {
    if (!ticking) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [ticking])

  const elapsed = d.startMs === null ? null : (d.endMs ?? now) - d.startMs

  // The layout follows the width the strip is GIVEN (container queries), not the window: the same
  // strip sits full-width on New Research and in a narrow side column on the Dashboard.
  //   narrow  (< 36rem): one full-width row per agent, then Round | Elapsed as two equal cells
  //   medium  (>= 36rem): three equal agent cards, the two stats below them
  //   wide    (>= 56rem): three equal agent cards and a stats column, all one row
  return (
    <div className="@container">
      <section aria-label="Agents" className="grid grid-cols-1 gap-2 @xl:grid-cols-3 @xl:gap-3 @4xl:grid-cols-[repeat(3,minmax(0,1fr))_9rem]">
        {AGENTS.map((a, i) => {
          const status = d[a.key]
          return (
            <div
              key={a.key}
              className={`agent-status-card state-fade flex min-w-0 ${status === 'active' ? 'agent-active agent-status-active' : status === 'done' ? 'agent-status-done' : status === 'error' ? 'agent-status-error' : ''}`}
            >
              <span className="agent-number-strip" aria-hidden="true">{String(i + 1).padStart(2, '0')}</span>
              <div className="min-w-0 flex-1 px-3 py-2.5">
                <div className="flex items-center gap-2">
                  <span className={`state-fade h-2 w-2 shrink-0 rounded-full ${DOT[status]} ${status === 'active' ? 'dot-live' : status === 'done' ? 'dot-done' : ''}`} aria-hidden="true" />
                  <span className="font-display text-sm font-semibold tracking-[0.14em]">{a.name}</span>
                  <span className="agent-status-label state-fade ml-auto shrink-0">{status}</span>
                </div>
                <p className="mt-1 text-xs text-muted">{a.role}</p>
                {/* keyed by the label, so each new activity fades in instead of replacing the old one abruptly */}
                <p key={d.labels[a.key]} className={`feed-in mt-2 truncate font-mono text-xs ${status === 'active' ? 'text-accent-ink' : 'text-muted'}`} aria-live="polite" title={d.labels[a.key]}>
                  {d.labels[a.key]}
                </p>
              </div>
            </div>
          )
        })}
        <div className="grid grid-cols-2 gap-2 @xl:col-span-3 @xl:gap-3 @4xl:col-span-1 @4xl:grid-cols-1 @4xl:gap-0">
          <Stat label="Round" value={<>{d.round || '-'}<span className="text-muted">/{maxRounds}</span></>} />
          <Stat label="Elapsed" value={elapsed === null ? '--:--' : mmss(elapsed)} />
        </div>
      </section>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: ReactNode }) {
  // In the wide layout the two stats share one bordered column; otherwise each has its own box.
  return (
    <div className="agent-stat border-[1.5px] border-ink bg-surface px-4 py-3 @4xl:[&:first-child]:border-b-0">
      <p className="font-mono text-[11px] uppercase text-muted">{label}</p>
      <p className="font-mono text-lg tabular-nums text-ink">{value}</p>
    </div>
  )
}
