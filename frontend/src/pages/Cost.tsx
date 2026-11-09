// Cost & Execution: the selected run's usage meter (LLM calls, tokens per model and per
// agent node, web searches, time per stage, call log), its stop condition and the limits that
// bound every run. The token budget is
// a viewer-side reference (saved in this browser); the backend does not stop on budget.

import { useState } from 'react'
import { isKnown } from '../api'
import { useStore } from '../store'
import { Empty, Meter, PageHeader, Panel, Pill, Stat, STOP_REASON, field, fmtInt, fmtMs, fmtSec } from '../ui'

const STAGE_COLOR: Record<string, string> = {
  planning: 'bg-ink', searching: 'bg-accent-strong', reviewing: 'bg-accent', 'waiting for you': 'bg-muted/40',
  writing: 'bg-ok', verifying: 'bg-warn', finishing: 'bg-ink/50',
}
const BUDGET_KEY = 'apollo.tokenBudget'

export default function Cost() {
  const { metrics, runId, runs, selected, events, system } = useStore()
  const [budget, setBudget] = useState<number>(() => {
    try {
      return Number(localStorage.getItem(BUDGET_KEY)) || 60000
    } catch {
      return 60000
    }
  })
  if (!runId) return <><PageHeader kicker="08 · Cost & execution" title="Cost & execution" /><Panel><Empty>Start a run to see what it costs.</Empty></Panel></>
  if (!metrics) return <><PageHeader kicker="08 · Cost & execution" title="Cost & execution" /><Panel><Empty>Loading usage…</Empty></Panel></>

  const s = metrics.summary
  const used = s.prompt_tokens + s.completion_tokens
  const stageTotal = metrics.stages.reduce((a, x) => a + x.ms, 0)
  const llmRows = metrics.by_model.filter((r) => r.name !== 'web search')
  const maxCalls = Math.max(0, ...llmRows.map((r) => r.calls))
  const maxNodeTok = Math.max(0, ...metrics.by_node.map((r) => r.prompt_tokens + r.completion_tokens))
  const peers = runs.filter((r) => r.finished_at && r.run_id !== runId && r.max_rounds === selected?.max_rounds)
  const estimate = peers.length ? peers.reduce((a, r) => a + ((r.finished_at ?? 0) - r.created_at), 0) / peers.length : null

  // Diminishing returns: new sources found per round. Duplicate searches: repeated queries.
  const perRound = new Map<number, { queries: number; found: number }>()
  for (const e of events) {
    if (!isKnown(e) || (e.type !== 'search_done' && e.type !== 'search_failed')) continue
    const r = e.data.round ?? 1
    const row = perRound.get(r) ?? { queries: 0, found: 0 }
    row.queries += 1
    row.found += e.type === 'search_done' ? e.data.result_count ?? 0 : 0
    perRound.set(r, row)
  }
  const queries = metrics.calls.filter((c) => c.kind === 'search').map((c) => (c.query ?? '').trim().toLowerCase())
  const dupes = queries.length - new Set(queries).size
  const saveBudget = (v: number) => {
    setBudget(v)
    try {
      localStorage.setItem(BUDGET_KEY, String(v))
    } catch {
      /* private mode */
    }
  }

  return (
    <div>
      <PageHeader kicker="08 · Cost & execution" title="Cost & execution" />
      {/* five tiles: the last spans both columns on phones, so no empty cell is left */}
      <div className="grid grid-cols-2 gap-px border border-line bg-line md:grid-cols-5 [&>*]:border-0 [&>*:last-child]:col-span-2 md:[&>*:last-child]:col-span-1">
        <Stat label="Actual time" value={fmtMs(metrics.wall_ms)} sub={estimate ? `est. ${fmtMs(estimate)} (avg of ${peers.length})` : 'no estimate yet'} />
        <Stat label="LLM calls" value={s.llm_calls} sub={s.llm_failed ? `${s.llm_failed} failed / retried` : 'none failed'} tone={s.llm_failed ? 'warn' : 'normal'} />
        <Stat label="Input tokens" value={fmtInt(s.prompt_tokens)} tone="accent" />
        <Stat label="Output tokens" value={fmtInt(s.completion_tokens)} tone="accent" />
        <Stat label="Search API calls" value={s.searches} sub={s.searches_failed ? `${s.searches_failed} failed` : `${fmtSec(s.search_seconds)} total`} tone={s.searches_failed ? 'warn' : 'normal'} />
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel label="Budget" title="Token budget (reference)">
          <label htmlFor="budget" className="label">Maximum tokens per run</label>
          <input id="budget" type="number" min={1000} step={1000} value={budget} onChange={(e) => saveBudget(Math.max(0, Number(e.target.value) || 0))} className={`${field} mt-1.5 w-40 font-mono`} />
          <div className="mt-3 flex items-center gap-2"><Meter value={used} max={budget} color={used > budget ? 'bg-err' : 'bg-accent-strong'} /><span className="font-mono text-xs">{Math.round((used / Math.max(1, budget)) * 100)}%</span></div>
          <p className="mt-1 text-sm text-ink">{used > budget ? `${fmtInt(used - budget)} tokens over budget` : `${fmtInt(budget - used)} tokens remaining`}</p>
          <p className="mt-2 text-[11px] text-muted">Saved in this browser. The backend does not enforce a budget; runs are bounded by the limits on the right.</p>
        </Panel>
        <Panel label="Automatic stop conditions" title="Runaway-loop prevention">
          <p className="text-sm text-ink">This run stopped: <strong>{selected?.stop_reason ? STOP_REASON[selected.stop_reason] ?? selected.stop_reason : 'still running'}</strong></p>
          <dl className="mt-3 grid grid-cols-2 gap-x-6 text-sm">
            {[
              ['Iteration limit', `${system?.limits.max_rounds ?? 2} rounds`],
              ['Research timeout', `${system?.limits.timeout_seconds ?? '–'}s`],
              ['Follow-ups / round', `${system?.limits.max_gap_queries ?? '–'}`],
              ['Retries / call', `${system?.limits.search_max_retries ?? '–'} search · ${system?.limits.llm_max_retries ?? '–'} LLM`],
              ['Graph step cap', `${system?.limits.recursion_limit ?? '–'}`],
              ['Duplicate searches', `${dupes} (repeats are skipped)`],
            ].map(([k, v]) => <div key={k} className="flex justify-between gap-3 border-b border-hair py-1.5"><dt className="text-muted">{k}</dt><dd className="font-mono text-xs">{v}</dd></div>)}
          </dl>
        </Panel>
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <Panel label="Diminishing returns" title="New sources per round">
          {!perRound.size ? <Empty>No searches yet.</Empty> : (
            <ul className="space-y-2">
              {[...perRound.entries()].map(([r, v]) => (
                <li key={r} className="grid grid-cols-[4.5rem_minmax(0,1fr)_6rem] items-center gap-3 text-sm">
                  <span className="label">Round {r}</span>
                  <Meter value={v.found} max={Math.max(...[...perRound.values()].map((x) => x.found))} />
                  <span className="text-right font-mono text-xs">{v.found} new / {v.queries} q</span>
                </li>
              ))}
            </ul>
          )}
          <p className="mt-2 text-[11px] text-muted">Round 2 queries are Hermes' refinements of round 1. Fewer new sources per query means less to gain from another round.</p>
        </Panel>
        <Panel label="Execution time" title="Time per stage" aside="from event timestamps">
          {!metrics.stages.length ? <Empty>No stages yet.</Empty> : (
            <>
              <div className="flex h-4 w-full overflow-hidden bg-surface-2" role="img" aria-label={metrics.stages.map((st) => `${st.stage} ${fmtMs(st.ms)}`).join(', ')}>
                {metrics.stages.map((st, i) => st.ms > 0 && <div key={i} className={STAGE_COLOR[st.stage] ?? 'bg-muted'} style={{ width: `${(st.ms / Math.max(1, stageTotal)) * 100}%` }} title={`${st.stage}: ${fmtMs(st.ms)}`} />)}
              </div>
              <ul className="mt-3 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
                {metrics.stages.map((st, i) => (
                  <li key={i} className="flex items-center justify-between gap-3">
                    <span className="inline-flex items-center gap-2"><span className={`h-2 w-2 ${STAGE_COLOR[st.stage] ?? 'bg-muted'}`} aria-hidden="true" />{st.stage}</span>
                    <span className="font-mono text-xs">{fmtMs(st.ms)}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Panel>
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <Panel label="Agent-level cost" title="Tokens per graph node" pad={false}>
          {!metrics.by_node.length ? <Empty>No metered calls.</Empty> : (
            <table className="w-full text-left text-sm">
              <tbody>
                {metrics.by_node.map((r) => {
                  const t = r.prompt_tokens + r.completion_tokens
                  return (
                    <tr key={r.name} className="border-b border-hair [&>td]:px-4 [&>td]:py-2">
                      <td className="font-mono text-xs">{r.name}</td>
                      <td className="w-1/3"><Meter value={t} max={maxNodeTok} /></td>
                      <td className="text-right font-mono text-xs">{fmtInt(t)}</td>
                      <td className="text-right font-mono text-xs text-muted">{r.calls} calls</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </Panel>
        <Panel label="Models" title="LLM usage by model" pad={false}>
          {!llmRows.length ? <Empty>No LLM calls recorded.</Empty> : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[480px] text-left text-sm">
                <thead className="bg-workspace"><tr className="border-b border-line [&>th]:label [&>th]:px-4 [&>th]:py-2 [&>th]:font-normal"><th>Provider : model</th><th className="w-32">Calls</th><th>In</th><th>Out</th><th>Cost</th></tr></thead>
                <tbody>
                  {llmRows.map((r) => (
                    <tr key={r.name} className="border-b border-hair [&>td]:px-4 [&>td]:py-2">
                      <td className="font-mono text-xs">{r.name}</td>
                      <td><div className="flex items-center gap-2"><span className="w-6 font-mono text-xs">{r.calls}</span><Meter value={r.calls} max={maxCalls} /></div></td>
                      <td className="font-mono text-xs">{fmtInt(r.prompt_tokens)}</td>
                      <td className="font-mono text-xs">{fmtInt(r.completion_tokens)}</td>
                      <td className="font-mono text-xs">{r.cost_usd === undefined ? '–' : `$${r.cost_usd.toFixed(4)}`}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>

      <div className="mt-5">
        <Panel label="Call log" title="Every metered call" aside={`${metrics.calls.length}`} pad={false}>
          {!metrics.calls.length ? <Empty>No calls yet.</Empty> : (
            <div className="max-h-[420px] overflow-auto">
              <table className="w-full min-w-[720px] text-left text-xs">
                <thead className="sticky top-0 bg-workspace"><tr className="border-b border-line [&>th]:label [&>th]:px-4 [&>th]:py-2 [&>th]:font-normal"><th>Time</th><th>Node</th><th>Call</th><th>Tokens / results</th><th>Took</th><th>Status</th></tr></thead>
                <tbody className="font-mono">
                  {metrics.calls.map((c, i) => (
                    <tr key={i} className="border-b border-hair align-top [&>td]:px-4 [&>td]:py-1.5">
                      <td className="text-muted">{new Date(c.ts).toLocaleTimeString('en-IN')}</td>
                      <td>{c.node || '–'}</td>
                      <td className="max-w-[280px] truncate" title={c.kind === 'search' ? c.query : c.model}>{c.kind === 'search' ? `search · ${c.query ?? ''}` : `${c.provider}:${c.model}`}</td>
                      <td>{c.kind === 'search' ? `${c.results ?? 0} results` : `${fmtInt(c.prompt_tokens ?? 0)} → ${fmtInt(c.completion_tokens ?? 0)}`}</td>
                      <td>{fmtSec(c.seconds)}</td>
                      <td title={c.error}>{c.ok ? <Pill status="done" label="ok" /> : <Pill status="failed" label="failed" />}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </div>
  )
}
