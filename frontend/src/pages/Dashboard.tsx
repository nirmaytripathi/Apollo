// Dashboard: the Research Command Center. Start a run, pick a suggested topic, see the
// selected run's live summary, platform statistics, recent sessions and activity over time.

import { useState } from 'react'
import type { ClaimStatus } from '../api'
import AgentStrip from '../components/AgentStrip'
import TopicForm from '../components/TopicForm'
import { go } from '../router'
import { useStore } from '../store'
import { BarChart, Empty, Legend, PageHeader, Panel, Pill, StackedBar, CLAIM_KEYS, STATUS_COLOR, Stat, bucketize, btn, fmtInt, fmtTime, pct } from '../ui'

const KEYS = CLAIM_KEYS

export default function Dashboard() {
  const { runs, d, maxRounds, selected, select, runId, busy, start, events } = useStore()
  const [topic, setTopic] = useState('')

  const claims = Object.fromEntries(KEYS.map((k) => [k, runs.reduce((a, r) => a + (r.claims[k] ?? 0), 0)])) as Record<ClaimStatus, number>
  const totalClaims = KEYS.reduce((a, k) => a + claims[k], 0)
  const sources = runs.reduce((a, r) => a + r.sources, 0)
  const tokens = runs.reduce((a, r) => a + r.usage.prompt_tokens + r.usage.completion_tokens, 0)

  // Live summary of the selected run, from its events.
  const searched = events.filter((e) => e.type === 'search_done' || e.type === 'search_failed').length
  const written = events.filter((e) => e.type === 'section_written').length
  const selTotal = selected ? KEYS.reduce((a, k) => a + (selected.claims[k] ?? 0), 0) : 0

  return (
    <div>
      <PageHeader kicker="01 · Dashboard" title="Research Command Center">
        <button className={btn.secondary} onClick={() => go('analytics')}>Analytics</button>
        <button className={btn.secondary} onClick={() => go('reports')}>Final reports</button>
      </PageHeader>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,1fr)_340px]">
        <div className="min-w-0 space-y-5">
          <TopicForm
            busy={busy}
            topic={topic}
            onTopic={setTopic}
            onStart={async (t, r, h, docs) => {
              if (await start(t, r, h, docs)) go('live')
            }}
          />

          <div className="grid grid-cols-2 gap-px border border-line bg-line md:grid-cols-4 [&>*]:border-0">
            <Stat label="Research sessions" value={runs.length} sub={`${runs.filter((r) => r.status === 'complete').length} complete`} />
            <Stat label="Sources analyzed" value={fmtInt(sources)} sub="all sessions" tone="accent" />
            <Stat label="Claims checked" value={fmtInt(totalClaims)} sub={`${pct(claims.verified, totalClaims)} verified`} tone="good" />
            <Stat label="LLM tokens" value={fmtInt(tokens)} sub={`${runs.reduce((a, r) => a + r.usage.llm_calls, 0)} metered calls`} />
          </div>

          <Panel label="Historical activity" title="Research sessions per day" aside="last 14 days">
            {runs.length ? <BarChart label="Research sessions per day, last 14 days" data={bucketize(runs.map((r) => r.created_at), 'day', 14)} /> : <Empty>No sessions yet.</Empty>}
          </Panel>

          <Panel label="Sessions" title="Recent research sessions" aside={`${runs.length} total`} pad={false}>
            {!runs.length ? <Empty>Sessions you start appear here and are kept after a server restart.</Empty> : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[640px] text-left text-sm">
                  <thead className="bg-workspace">
                    <tr className="border-b border-line [&>th]:label [&>th]:px-4 [&>th]:py-2 [&>th]:font-normal">
                      <th>Topic</th><th>Status</th><th>Rounds</th><th>Sources</th><th>Verified</th><th>Started</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.slice(0, 8).map((r) => {
                      const total = KEYS.reduce((a, k) => a + (r.claims[k] ?? 0), 0)
                      return (
                        <tr key={r.run_id} className={`border-b border-hair [&>td]:px-4 [&>td]:py-2.5 ${r.run_id === runId ? 'bg-accent/5' : ''}`}>
                          <td><button onClick={() => select(r.run_id)} className="block max-w-[320px] truncate text-left text-ink hover:text-accent-ink hover:underline" title={r.topic}>{r.topic}</button></td>
                          <td><Pill status={r.status} /></td>
                          <td className="font-mono text-xs">{r.rounds}/{r.max_rounds}</td>
                          <td className="font-mono text-xs">{r.sources}</td>
                          <td className="font-mono text-xs">{r.has_report ? `${r.claims.verified}/${total}` : '–'}</td>
                          <td className="font-mono text-xs text-muted">{fmtTime(r.created_at)}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>

        <div className="min-w-0 space-y-5">
          <Panel label="Live intelligence" title={selected ? 'Selected session' : 'No session selected'} aside={selected ? <Pill status={selected.status} /> : undefined}>
            {!selected ? <Empty>Start a session to see live intelligence.</Empty> : (
              <div className="space-y-4">
                <p className="text-sm text-ink">{selected.topic}</p>
                <dl className="grid grid-cols-3 gap-px border border-line bg-line text-center">
                  {[
                    ['Sources', selected.sources],
                    ['Tasks done', searched + written],
                    ['Confidence', selected.has_report ? pct(selected.claims.verified, selTotal) : '–'],
                  ].map(([k, v]) => (
                    <div key={k} className="bg-surface px-2 py-2.5">
                      <dt className="label">{k}</dt>
                      <dd className="mt-1 font-display text-xl font-semibold tabular-nums">{v}</dd>
                    </div>
                  ))}
                </dl>
                <p className="text-[11px] text-muted">Tasks = searches finished + sections written. Confidence = share of checked claims verified against source text.</p>
                <AgentStrip d={d} maxRounds={maxRounds} />
                {selected.has_report && (
                  <div>
                    <StackedBar parts={KEYS.map((k) => ({ value: (selected.claims[k] ?? 0), color: STATUS_COLOR[k], label: k }))} />
                    <Legend items={KEYS.map((k) => ({ color: STATUS_COLOR[k], label: k.replace('_', ' '), value: (selected.claims[k] ?? 0) }))} />
                  </div>
                )}
                <div className="flex flex-wrap gap-2">
                  {selected.has_report && <button className={btn.primary} onClick={() => go('reports')}>Open report</button>}
                  <button className={btn.secondary} onClick={() => go('verification')}>Verification</button>
                </div>
              </div>
            )}
          </Panel>

        </div>
      </div>
    </div>
  )
}
