// Analytics: research activity and quality across sessions, filtered by date range and
// shown per day / week / month. Everything is computed from GET /api/runs.

import { useState, type ReactNode } from 'react'
import type { ClaimStatus, RunSummary } from '../api'
import { useStore } from '../store'
import {
  BarChart, Empty, Legend, Meter, PageHeader, Panel, Segmented, StackedBar, CLAIM_KEYS, STATUS_COLOR, Stat, STOP_REASON,
  bucketize, btn, download, fmtInt, fmtMs, fmtTime, pct,
} from '../ui'

const KEYS = CLAIM_KEYS
type Range = '7d' | '30d' | '90d' | 'all'
type Unit = 'day' | 'week' | 'month'
const RANGE_DAYS: Record<Range, number> = { '7d': 7, '30d': 30, '90d': 90, all: Infinity }
const BUCKETS: Record<Unit, number> = { day: 14, week: 12, month: 6 }

const dur = (r: RunSummary) => (r.finished_at ?? r.created_at) - r.created_at
const total = (r: RunSummary) => KEYS.reduce((a, k) => a + (r.claims[k] ?? 0), 0)

function toCsv(rows: RunSummary[]): string {
  const head = ['run_id', 'topic', 'status', 'started', 'duration_s', 'rounds', 'sources', ...KEYS, 'contradictions', 'llm_calls', 'prompt_tokens', 'completion_tokens', 'searches', 'searches_failed', 'stop_reason']
  const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
  return [head.join(','), ...rows.map((r) => [
    r.run_id, r.topic, r.status, new Date(r.created_at).toISOString(), Math.round(dur(r) / 1000), r.rounds, r.sources, ...KEYS.map((k) => (r.claims[k] ?? 0)),
    r.contradictions, r.usage.llm_calls, r.usage.prompt_tokens, r.usage.completion_tokens, r.usage.searches, r.usage.searches_failed, r.stop_reason,
  ].map(esc).join(','))].join('\n')
}

export default function Analytics() {
  const { runs, select } = useStore()
  const [range, setRange] = useState<Range>('30d')
  const [unit, setUnit] = useState<Unit>('day')
  const cutoff = range === 'all' ? 0 : Date.now() - RANGE_DAYS[range] * 86_400_000
  const inRange = runs.filter((r) => r.created_at >= cutoff)
  const done = inRange.filter((r) => r.finished_at)

  const sum = (xs: RunSummary[], f: (r: RunSummary) => number) => xs.reduce((a, r) => a + f(r), 0)
  const claims = Object.fromEntries(KEYS.map((k) => [k, sum(done, (r) => (r.claims[k] ?? 0))])) as Record<ClaimStatus, number>
  const claimTotal = KEYS.reduce((a, k) => a + claims[k], 0)
  const searches = sum(done, (r) => r.usage.searches)
  const searchFails = sum(done, (r) => r.usage.searches_failed)
  const metered = done.filter((r) => r.usage.llm_calls + r.usage.searches > 0)
  const stops = new Map<string, number>()
  done.forEach((r) => r.stop_reason && stops.set(r.stop_reason, (stops.get(r.stop_reason) ?? 0) + 1))
  const topics = new Map<string, number>()
  inRange.forEach((r) => topics.set(r.topic, (topics.get(r.topic) ?? 0) + 1))
  const trend = [...done].sort((a, b) => a.created_at - b.created_at).slice(-12)

  return (
    <div>
      <PageHeader kicker="03 · Analytics" title="Research activity & quality">
        <Segmented<Range> label="Date range" value={range} onChange={setRange} options={[{ value: '7d', label: '7D' }, { value: '30d', label: '30D' }, { value: '90d', label: '90D' }, { value: 'all', label: 'All' }]} />
        <button className={btn.secondary} disabled={!inRange.length} onClick={() => download(`apollo-analytics-${range}.csv`, toCsv(inRange), 'text/csv')}>Export CSV</button>
      </PageHeader>

      {!inRange.length ? <Panel><Empty>No research sessions in this date range.</Empty></Panel> : (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-px border border-line bg-line md:grid-cols-3 xl:grid-cols-6 [&>*]:border-0">
            <Stat label="Sessions" value={inRange.length} sub={`${done.length} finished`} />
            <Stat label="Completion rate" value={pct(inRange.filter((r) => r.status === 'complete').length, inRange.length)} sub="ended with a report" tone="good" />
            <Stat label="Verified claims" value={pct(claims.verified, claimTotal)} sub={`${claims.verified} of ${claimTotal}`} tone="good" />
            <Stat label="Search success" value={pct(searches - searchFails, searches)} sub={`${searchFails} failed of ${searches}`} tone={searchFails ? 'warn' : 'normal'} />
            <Stat label="Avg duration" value={done.length ? fmtMs(sum(done, dur) / done.length) : '–'} sub="start to report" />
            <Stat label="Steps / execution" value={metered.length ? Math.round(sum(metered, (r) => r.usage.llm_calls + r.usage.searches) / metered.length) : '–'} sub="LLM calls + searches" />
          </div>

          <Panel label="Activity overview" title={`Sessions per ${unit}`} aside={<Segmented<Unit> label="Granularity" value={unit} onChange={setUnit} options={[{ value: 'day', label: 'Daily' }, { value: 'week', label: 'Weekly' }, { value: 'month', label: 'Monthly' }]} />}>
            <BarChart label={`Research sessions per ${unit}`} data={bucketize(inRange.map((r) => r.created_at), unit, BUCKETS[unit])} />
          </Panel>

          <div className="grid gap-5 lg:grid-cols-2">
            <Panel label="Deep research insights" title="Verified-claims analytics">
              <StackedBar height="h-3" parts={KEYS.map((k) => ({ value: claims[k], color: STATUS_COLOR[k], label: k }))} />
              <Legend items={KEYS.map((k) => ({ color: STATUS_COLOR[k], label: k.replace('_', ' '), value: claims[k] }))} />
              <p className="label mt-5">Why sessions stopped</p>
              <ul className="mt-2 space-y-2">
                {[...stops.entries()].sort((a, b) => b[1] - a[1]).map(([k, n]) => (
                  <li key={k} className="grid grid-cols-[minmax(0,1fr)_2.5rem] items-center gap-3 text-sm">
                    <div><p className="text-ink">{STOP_REASON[k] ?? k}</p><Meter value={n} max={done.length} /></div>
                    <span className="text-right font-mono text-xs">{n}</span>
                  </li>
                ))}
                {!stops.size && <li className="text-sm text-muted">No finished sessions.</li>}
              </ul>
            </Panel>
            <Panel label="Popular research queries" title="Most researched topics">
              <ol className="space-y-2">
                {[...topics.entries()].sort((a, b) => b[1] - a[1]).slice(0, 8).map(([t, n], i) => (
                  <li key={t} className="grid grid-cols-[1.5rem_minmax(0,1fr)_2rem] items-start gap-2 text-sm">
                    <span className="font-mono text-xs text-muted">{String(i + 1).padStart(2, '0')}</span>
                    <span className="text-ink">{t}</span>
                    <span className="text-right font-mono text-xs">{n}×</span>
                  </li>
                ))}
              </ol>
            </Panel>
          </div>

          <Panel label="Historical performance" title="Per-session trend" aside="oldest → newest, last 12" pad={false}>
            {!trend.length ? <Empty>No finished sessions.</Empty> : (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[820px] text-left text-sm">
                  <thead className="bg-workspace">
                    <tr className="border-b border-line [&>th]:label [&>th]:px-4 [&>th]:py-2 [&>th]:font-normal">
                      <th>Session</th><th className="w-40">Duration</th><th className="w-36">Sources</th><th className="w-48">Claims</th><th className="w-40">Tokens</th><th className="w-24">Searches</th>
                    </tr>
                  </thead>
                  <tbody>
                    {trend.map((r) => {
                      const tok = r.usage.prompt_tokens + r.usage.completion_tokens
                      return (
                        <tr key={r.run_id} className="border-b border-hair [&>td]:px-4 [&>td]:py-2.5">
                          <td><button onClick={() => select(r.run_id)} className="block max-w-[260px] truncate text-left text-ink hover:text-accent-ink hover:underline" title={r.topic}>{r.topic}</button><span className="font-mono text-[10.5px] text-muted">{fmtTime(r.created_at)}</span></td>
                          <td><Cell text={fmtMs(dur(r))}><Meter value={dur(r)} max={Math.max(...trend.map(dur))} /></Cell></td>
                          <td><Cell text={String(r.sources)}><Meter value={r.sources} max={Math.max(...trend.map((x) => x.sources))} color="bg-ok" /></Cell></td>
                          <td>{r.has_report ? <Cell text={`${r.claims.verified}/${total(r)}`}><StackedBar height="h-1.5" parts={KEYS.map((k) => ({ value: (r.claims[k] ?? 0), color: STATUS_COLOR[k], label: k }))} /></Cell> : <span className="text-xs text-muted">no report</span>}</td>
                          <td><Cell text={fmtInt(tok)}><Meter value={tok} max={Math.max(...trend.map((x) => x.usage.prompt_tokens + x.usage.completion_tokens))} color="bg-ink/60" /></Cell></td>
                          <td className="font-mono text-xs">{r.usage.searches}{r.usage.searches_failed ? <span className="text-warn-ink"> · {r.usage.searches_failed} failed</span> : ''}</td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
            <p className="px-4 py-2 text-[11px] text-muted">Sessions created before usage metering was added show 0 tokens and searches.</p>
          </Panel>
        </div>
      )}
    </div>
  )
}

function Cell({ text, children }: { text: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-14 shrink-0 font-mono text-xs">{text}</span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  )
}
