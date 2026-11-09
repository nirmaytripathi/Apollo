// Live Report: opens as soon as a run is launched and builds the report on screen while the
// agents work. The plan appears first, then each section's sources as Athena finds them, then
// the section text streaming in as Apollo writes it (a "draft" until citation checks finish), then
// the full report before claim verification, and finally the verified report.
// Everything comes from the run's event stream (store.events) and, at the end, GET /report.

import { useEffect, useState, type CSSProperties } from 'react'
import { isKnown, toMs, type AnyEvent, type ClaimStatus, type Report, type Source } from '../api'
import HitlPanel from '../components/HitlPanel'
import PauseEditPanel from '../components/PauseEditPanel'
import ProgressFeed from '../components/ProgressFeed'
import ReportView, { withChips } from '../components/ReportView'
import { go } from '../router'
import { useStore } from '../store'
import { Empty, Legend, Panel, Pill, StackedBar, CLAIM_KEYS, STATUS_COLOR, STOP_REASON, btn, hostOf } from '../ui'

type SectionState = { text: string; draft: boolean; final?: { citations: number; notFound: boolean; writeFailed: boolean } }

interface LiveDoc {
  plan: string[]
  sources: Source[] // in the order they were found
  sourcesByQuery: Map<string, Source[]>
  searching: Set<string>
  hits: Map<string, { title?: string; url: string }[]> // raw search results per query, before pages are read
  sections: Map<number, SectionState>
  markdown: string | null // full report text (before verification)
  verifying: boolean
  checked: { done: number; total: number } | null // claim verification progress
}

/** Fold the event stream into what the document should show right now. */
function build(events: AnyEvent[]): LiveDoc {
  const doc: LiveDoc = { plan: [], sources: [], sourcesByQuery: new Map(), searching: new Set(), hits: new Map(), sections: new Map(), checked: null, markdown: null, verifying: false }
  for (const e of events) {
    if (!isKnown(e)) continue
    switch (e.type) {
      case 'plan':
        doc.plan = e.data.sub_questions ?? doc.plan
        break
      case 'search_started':
        if (e.data.query) doc.searching.add(e.data.query)
        break
      case 'search_results':
        if (e.data.query) doc.hits.set(e.data.query, e.data.results ?? [])
        break
      case 'search_done':
      case 'search_failed': {
        const q = e.data.query ?? ''
        doc.searching.delete(q)
        if (e.type === 'search_done') {
          const found = (e.data.sources ?? []).map((s) => ({ ...s, title: s.title ?? '' }))
          doc.sources.push(...found)
          doc.sourcesByQuery.set(q, [...(doc.sourcesByQuery.get(q) ?? []), ...found])
        }
        break
      }
      case 'section_draft':
        if (e.data.index !== undefined && !doc.sections.get(e.data.index)?.final) doc.sections.set(e.data.index, { text: e.data.text ?? '', draft: true })
        break
      case 'section_written':
        if (e.data.index !== undefined)
          doc.sections.set(e.data.index, {
            text: e.data.text ?? '',
            draft: false,
            final: { citations: e.data.citations ?? 0, notFound: !!e.data.not_found, writeFailed: !!e.data.write_failed },
          })
        break
      case 'claim_checked':
        doc.checked = { done: Math.max(doc.checked?.done ?? 0, e.data.done ?? 0), total: e.data.total ?? 0 }
        break
      case 'report_written':
        doc.markdown = e.data.markdown ?? null
        doc.verifying = true
        break
      case 'verification':
      case 'report_ready':
      case 'complete':
        doc.verifying = false
        break
    }
  }
  return doc
}

const STAGES = ['Plan', 'Search', 'Review', 'Write', 'Verify', 'Done'] as const

function stageOf(events: AnyEvent[], finished: boolean, reviewing: boolean): number {
  if (finished) return 5
  if (reviewing) return 2 // Hermes is reading the round's sources (no event until it finishes)
  let s = 0
  for (const e of events) {
    if (e.type === 'search_started') s = Math.max(s, 1)
    if (e.type === 'review') s = Math.max(s, 2)
    if (e.type === 'search_started' && s === 2) s = 1 // another round
    if (e.type === 'section_draft' || e.type === 'section_written') s = 3
    if (e.type === 'report_written') s = 4
  }
  return s
}

function useClock(running: boolean) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!running) return
    const t = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(t)
  }, [running])
  return now
}

export default function LiveReport() {
  const { runId, selected, events, d, busy, report, showHitl, answer, reconnecting } = useStore()
  const doc = build(events)
  const now = useClock(busy)
  const [showLog, setShowLog] = useState(false)

  if (!runId) {
    return (
      <Panel>
        <div className="py-10 text-center">
          <p className="text-sm text-ink">No research run yet.</p>
          <button onClick={() => go('new')} className={`${btn.primary} mt-4`}>Start a research run</button>
        </div>
      </Panel>
    )
  }

  const start = events.length ? Math.min(...events.map((e) => toMs(e.ts))) : null
  const elapsed = start ? Math.max(0, Math.floor(((d.endMs ?? now) - start) / 1000)) : 0
  const stage = stageOf(events, d.finished, d.hermes === 'active')
  const byId = new Map(doc.sources.map((s) => [s.id, s]))
  const status = reconnecting && !d.finished ? 'Reconnecting to the server…' : d.stopReason === 'stopped_by_user' ? 'Stopped by you' : d.paused ? 'Paused · press Resume to carry on' : d.status === 'failed' ? 'Stopped' : d.finished ? 'Complete' : d.planPending ? 'Waiting for your approval' : d.gapsPending ? 'Waiting for your approval' : stage === 4 && doc.checked ? `Verifying ${doc.checked.done}/${doc.checked.total}…` : `${STAGES[stage]}…`

  return (
    <div>
      {/* status strip; the rail at its foot fills as stages complete */}
      <div className="relative mb-5 border border-line bg-surface px-4 py-3">
        <div className="absolute inset-x-0 bottom-0 h-[2px] bg-surface-2" aria-hidden="true">
          <div className={`grow-x h-full w-full ${d.status === 'failed' ? 'bg-err' : d.finished ? 'bg-ok' : 'bg-accent-strong'}`}
            style={{ '--p': d.finished ? 1 : d.progress / 100 } as CSSProperties} />
        </div>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <p className="label">Live report</p>
          <p className="font-mono text-xs text-ink" aria-live="polite">{status}</p>
          <p className="font-mono text-xs text-ink tabular-nums" aria-label={`${d.progress} percent done`}>{d.progress}%</p>
          <p className="font-mono text-xs text-muted">{String(Math.floor(elapsed / 60)).padStart(2, '0')}:{String(elapsed % 60).padStart(2, '0')}</p>
          <p className="font-mono text-xs text-muted">{doc.sources.length} sources · {[...doc.sections.values()].filter((s) => s.final).length}/{doc.plan.length || '?'} sections</p>
          <ol className="flex flex-wrap gap-1 sm:ml-auto" aria-label="Progress">
            {STAGES.map((s, i) => {
              const done = i < stage || (d.finished && i === stage)
              const current = i === stage && !d.finished
              return (
                <li key={s} aria-current={i === stage ? 'step' : undefined}
                  className={`state-fade border px-2 py-0.5 font-mono text-[10.5px] uppercase ${done ? 'border-ok/50 bg-ok/10 text-ok-ink' : current ? `border-accent-strong bg-accent/15 text-accent-ink ${d.status === 'failed' ? '' : 'sheen'}` : 'border-line text-muted'}`}>
                  {s}
                </li>
              )
            })}
          </ol>
        </div>
      </div>

      <PauseEditPanel />

      {showHitl && (
        <div className="mb-5">
          {d.planPending
            ? <HitlPanel key={d.pendingAt} mode="plan" items={d.subQuestions} onAnswer={answer} />
            : <HitlPanel key={d.pendingAt} mode="gaps" items={d.gapsPending ?? []} onAnswer={answer} />}
        </div>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,1fr)_320px]">
        <article className="min-w-0 border border-line bg-surface px-5 py-7 sm:px-10">
          <p className="label">APOLLO research report{busy ? ' · being written' : ''}</p>
          <h1 className="mt-2 font-display text-[28px] font-semibold leading-tight">{selected?.topic ?? 'Research report'}</h1>

          {report ? (
            <Verified report={report} runId={runId} />
          ) : doc.markdown ? (
            <>
              <div className="mt-3" role="status">
                <p className="flex flex-wrap items-center gap-2 text-xs text-muted">
                  <Pill status="working" label={doc.checked ? `verifying ${doc.checked.done}/${doc.checked.total}` : 'verifying claims'} />
                  Full text is ready to read; each claim is being checked against its source.
                </p>
                {doc.checked && doc.checked.total > 0 && (
                  <div className="mt-2 h-1.5 w-full max-w-sm overflow-hidden bg-surface-2" aria-hidden="true">
                    <div className="grow-x sheen h-full w-full bg-accent-strong" style={{ '--p': doc.checked.done / doc.checked.total } as CSSProperties} />
                  </div>
                )}
              </div>
              <div className="mt-6"><ReportView report={partial(doc.markdown, doc.sources)} runId={runId} toolbar={false} /></div>
            </>
          ) : (
            <Progressive doc={doc} byId={byId} finished={d.finished} />
          )}

          {d.status === 'failed' && <p role="alert" className="mt-6 border border-err/60 bg-err/10 px-3 py-2 text-sm text-err-ink">{d.errorMsg ?? 'The run stopped.'}</p>}
        </article>

        <aside className="min-w-0 space-y-5">
          <Panel label="Sources" title={`Found so far · ${doc.sources.length}`} pad={false}>
            {!doc.sources.length ? (
              doc.hits.size ? (
                <ul className="max-h-[50vh] divide-y divide-hair overflow-y-auto" aria-label="Web results being read">
                  {[...doc.hits.values()].flat().map((h, i) => (
                    <li key={`${h.url}-${i}`} className="feed-in px-4 py-2">
                      <p className="font-mono text-[10.5px] text-muted">{hostOf(h.url)} · reading…</p>
                      <p className="line-clamp-2 text-[13px] text-ink">{h.title || h.url}</p>
                    </li>
                  ))}
                </ul>
              ) : <Empty>{doc.searching.size ? 'Searching the web…' : 'Sources appear as Athena finds them.'}</Empty>
            ) : (
              <ul className="max-h-[50vh] divide-y divide-hair overflow-y-auto">
                {doc.sources.map((s) => (
                  <li key={s.id} className="feed-in px-4 py-2">
                    <p className="font-mono text-[10.5px] text-accent-ink">{s.id} · {hostOf(s.url)}</p>
                    <a href={s.url} target="_blank" rel="noreferrer" className="line-clamp-2 text-[13px] text-ink hover:underline">{s.title || s.url}</a>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
          {d.finished && (
            <Panel label="Next" title="Explore this run">
              <div className="flex flex-wrap gap-2">
                <button className={btn.secondary} onClick={() => go('claims')}>Claims & evidence</button>
                <button className={btn.secondary} onClick={() => go('verification')}>Verification</button>
                <button className={btn.secondary} onClick={() => go('reports')}>Export report</button>
              </div>
            </Panel>
          )}
          <div>
            <button className={btn.ghost} onClick={() => setShowLog((v) => !v)} aria-expanded={showLog}>{showLog ? 'Hide' : 'Show'} event log</button>
            {showLog && <div className="mt-2"><ProgressFeed events={events} /></div>}
          </div>
        </aside>
      </div>
    </div>
  )
}

/** Before the full text exists: the plan as headings, each filling in as its work happens. */
function Progressive({ doc, byId, finished }: { doc: LiveDoc; byId: Map<string, Source>; finished: boolean }) {
  if (!doc.plan.length) {
    return (
      <div className="mt-6 space-y-2" role="status">
        <p className="text-sm text-muted">Apollo is breaking the question into sub-questions…</p>
        <Skeleton lines={3} />
      </div>
    )
  }
  return (
    <div className="mt-6 max-w-[72ch] text-[15px] leading-7">
      <h2 className="font-display text-lg font-semibold">Introduction</h2>
      <p className="mt-1 text-sm text-muted">Written after the sections.</p>
      {doc.plan.map((q, i) => {
        const sec = doc.sections.get(i)
        const found = doc.sourcesByQuery.get(q) ?? []
        const hits = doc.hits.get(q) ?? []
        return (
          <section key={i} className="mt-8">
            <h2 className="font-display text-lg font-semibold">{i + 1}. {q}</h2>
            {sec ? (
              sec.final?.writeFailed ? <p className="mt-2 border-l-2 border-err pl-3 text-err-ink">{sec.text}</p>
              : (
                <div className={`mt-2 ${sec.final?.notFound && !sec.final.citations ? 'border-l-2 border-err pl-3' : ''}`}>
                  {sec.draft && <p className="label mb-1 text-accent-ink">Draft · citations being checked</p>}
                  <p className={sec.draft ? 'text-ink/80' : ''}>
                    {withChips(sec.text, byId)}
                    {sec.draft && <span className="caret ml-0.5 inline-block h-[1.1em] w-[2px] translate-y-[3px] bg-accent-strong" aria-hidden="true" />}
                  </p>
                </div>
              )
            ) : (
              <div className="mt-2">
                <p className="text-sm text-muted" role="status">
                  {doc.searching.has(q) ? (hits.length ? `${hits.length} web results · reading pages…` : 'Searching the web…') : found.length ? `${found.length} source${found.length === 1 ? '' : 's'} found · waiting to write` : finished ? 'No text.' : 'Queued'}
                </p>
                {!found.length && hits.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {hits.slice(0, 6).map((h) => <span key={h.url} className="feed-in border border-line px-1.5 font-mono text-[11px] text-muted" title={h.title}>{hostOf(h.url)}</span>)}
                  </div>
                )}
                {found.length > 0 && (
                  <div className="mt-1.5 flex flex-wrap gap-1.5">
                    {found.map((s) => <span key={s.id} className="feed-in border border-accent/40 bg-accent/5 px-1.5 font-mono text-[11px] text-accent-ink" title={s.title}>{s.id} · {hostOf(s.url)}</span>)}
                  </div>
                )}
                {!finished && <div className="mt-3"><Skeleton lines={2} /></div>}
              </div>
            )}
          </section>
        )
      })}
    </div>
  )
}

function Verified({ report, runId }: { report: Report; runId: string }) {
  const keys = CLAIM_KEYS
  const counts = Object.fromEntries(keys.map((k) => [k, report.verification.filter((c) => c.status === k).length])) as Record<ClaimStatus, number>
  return (
    <>
      <div className="mt-3 border border-line bg-workspace px-3 py-2.5">
        <p className="flex flex-wrap items-center gap-2 text-sm text-ink">
          <Pill status="done" label="verified" />
          {counts.verified} of {report.verification.length} claims verified against their sources
          {report.stop_reason && <span className="text-xs text-muted">· {STOP_REASON[report.stop_reason] ?? report.stop_reason}</span>}
        </p>
        <div className="mt-2"><StackedBar parts={keys.map((k) => ({ value: counts[k], color: STATUS_COLOR[k], label: k }))} /></div>
        <Legend items={keys.map((k) => ({ color: STATUS_COLOR[k], label: k.replace('_', ' '), value: counts[k] }))} />
        {(report.quality ?? []).length > 0 && (() => {
          const q = report.quality ?? []
          const open = q.filter((c) => c.status !== 'pass')
          return (
            <p className="mt-2 flex flex-wrap items-center gap-2 border-t border-hair pt-2 text-sm text-ink">
              <Pill status={q.some((c) => c.status === 'fail') ? 'failed' : open.length ? 'warn' : 'done'} label={`${q.length - open.length}/${q.length} quality checks`} />
              {open.length ? <span className="text-xs text-muted">Not passed: {open.map((c) => c.label.toLowerCase()).join('; ')}. Details at the end of the report.</span> : <span className="text-xs text-muted">All research-quality checks passed.</span>}
              <button onClick={() => go('verification')} className="text-xs text-accent-ink underline underline-offset-2">Verification</button>
            </p>
          )
        })()}
      </div>
      <div className="mt-6"><ReportView report={{ ...report, markdown: body(report.markdown) }} runId={runId} /></div>
    </>
  )
}

/** The page already shows the topic as its title, so the markdown's own "# title" line is dropped. */
const body = (markdown: string) => markdown.replace(/^#\s.*\n+/, '')

/** The report text before verification, in the shape ReportView renders (no claim markers yet). */
const partial = (markdown: string, sources: Source[]): Report => ({ markdown: body(markdown), sources, verification: [], contradictions_html_path: null })

function Skeleton({ lines }: { lines: number }) {
  return (
    <div className="space-y-2" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => <div key={i} className="h-3 animate-pulse bg-surface-2 motion-reduce:animate-none" style={{ width: `${92 - i * 17}%` }} />)}
    </div>
  )
}

