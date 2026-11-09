// Final Reports: the selected run's report as a structured research document: summary,
// methodology, plan, findings (the cited report text), claim-by-claim evidence, source
// analysis, contradictions, unverified findings, limitations, conclusions and citations.
// Exports: Markdown (backend /report.md), Word (backend /report.docx), PDF (browser print of this page),
// a public read-only share link (backend /share: anyone with the link can read this one report), copy.

import { useEffect, useState, type ReactNode } from 'react'
import { createShare, deleteShare, getShare, isAttached, reportDocxUrl, reportDownloadUrl } from '../api'
import ReportView from '../components/ReportView'
import { CATEGORIES, CONFLICT_CHECK_TEXT, conflictCheck, classifyErrors, sourceCategory, ERROR_LABEL, type ErrorKind } from '../insights'
import { sharedUrl } from '../router'
import { useStore } from '../store'
import { Empty, NeedsRun, PageHeader, Panel, Pill, STOP_REASON, btn, fmtDate, fmtMs, fmtTime, hostOf } from '../ui'

/** Plain text of one "## Heading" section of the report markdown. */
function section(md: string, heading: RegExp): string {
  const lines = md.split('\n')
  const start = lines.findIndex((l) => /^##\s/.test(l) && heading.test(l))
  if (start < 0) return ''
  const end = lines.findIndex((l, i) => i > start && /^##?\s/.test(l))
  return lines.slice(start + 1, end < 0 ? undefined : end).join('\n').trim()
}

export default function Reports() {
  const { runs, runId, select, report, busy, selected, metrics, system } = useStore()
  const [copied, setCopied] = useState<string | null>(null)
  const withReport = runs.filter((r) => r.has_report)
  // The selected run's public link token: undefined while unknown, null when the link is off.
  const [share, setShare] = useState<string | null | undefined>(undefined)
  const [sharing, setSharing] = useState(false)

  useEffect(() => {
    setShare(undefined)
    if (!runId || !report) return
    let live = true
    getShare(runId).then((r) => live && setShare(r.token)).catch(() => live && setShare(null))
    return () => {
      live = false
    }
  }, [runId, report])

  const copy = async (what: string, text: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(what)
    } catch {
      // The async clipboard can be refused (page not focused, permission blocked): try the older copy command.
      const area = document.createElement('textarea')
      area.value = text
      area.setAttribute('readonly', '')
      area.style.position = 'fixed'
      area.style.opacity = '0'
      document.body.appendChild(area)
      area.select()
      const ok = document.execCommand('copy')
      area.remove()
      setCopied(ok ? what : 'Could not copy automatically: select the text and copy it yourself')
    }
    window.setTimeout(() => setCopied(null), 2500)
  }

  const shareLink = async () => {
    if (!runId) return
    setSharing(true)
    try {
      const { token } = await createShare(runId)
      setShare(token)
      await copy('Public link copied: anyone with it can read this report', sharedUrl(token))
    } catch (e) {
      setCopied(e instanceof Error ? `Share failed: ${e.message}` : 'Share failed')
      window.setTimeout(() => setCopied(null), 4000)
    } finally {
      setSharing(false)
    }
  }

  const stopSharing = async () => {
    if (!runId) return
    setSharing(true)
    try {
      await deleteShare(runId)
      setShare(null)
      setCopied('Link turned off: it no longer opens the report')
    } catch (e) {
      setCopied(e instanceof Error ? `Could not turn the link off: ${e.message}` : 'Could not turn the link off')
    } finally {
      setSharing(false)
      window.setTimeout(() => setCopied(null), 3000)
    }
  }

  return (
    <div>
      <div className="no-print">
        <PageHeader kicker="10 · Final reports" title="Research reports">
          {report && runId && (
            <>
              <a href={reportDownloadUrl(runId)} download className={btn.primary}>Markdown</a>
              <button className={btn.secondary} onClick={() => window.print()}>PDF (print)</button>
              <a href={reportDocxUrl(runId)} download className={btn.secondary}>DOCX</a>
              <button className={btn.secondary} onClick={() => void shareLink()} disabled={sharing}>{share ? 'Copy share link' : 'Share link'}</button>
              {share && <button className={btn.secondary} onClick={() => void stopSharing()} disabled={sharing}>Turn off link</button>}
              <button className={btn.secondary} onClick={() => copy('Markdown copied', report.markdown)}>Copy text</button>
            </>
          )}
        </PageHeader>
        {copied && <p role="status" className="-mt-3 mb-4 font-mono text-xs text-ok-ink">{copied}</p>}
        {report && <p className="-mt-2 mb-4 text-[11px] text-muted">{share
          ? 'Public link is on: anyone with it can read this report (read-only, no sign-in). Turn it off to stop the link working.'
          : 'Share link makes a public read-only link to this report: anyone with it can read it without signing in. You can turn it off any time.'}</p>}
        {report && share && (
          <div className="-mt-2 mb-4 flex max-w-[720px] items-center gap-2">
            <label htmlFor="share-url" className="label shrink-0">Public link</label>
            <input
              id="share-url"
              readOnly
              value={sharedUrl(share)}
              onFocus={(e) => e.currentTarget.select()}
              className="min-w-0 flex-1 border border-line bg-surface px-2 py-1 font-mono text-[11px] text-ink focus:border-accent-strong focus:outline-none"
            />
          </div>
        )}
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[280px_minmax(0,1fr)]">
        <div className="no-print xl:sticky xl:top-16 xl:self-start">
          <Panel label="Saved sessions" title="Reports" aside={`${withReport.length}`} pad={false}>
            {!withReport.length ? <Empty>No reports yet.</Empty> : (
              <ul className="max-h-[70vh] divide-y divide-hair overflow-y-auto">
                {withReport.map((r) => (
                  <li key={r.run_id}>
                    <button onClick={() => select(r.run_id)} aria-current={r.run_id === runId ? 'true' : undefined} className={`w-full px-4 py-2.5 text-left ${r.run_id === runId ? 'bg-accent/10' : 'hover:bg-workspace'}`}>
                      <p className="line-clamp-2 text-sm text-ink">{r.topic}</p>
                      <p className="mt-1 font-mono text-[10.5px] text-muted">{fmtTime(r.created_at)} · {r.sources} src · {r.claims.verified} verified</p>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>

        <div className="min-w-0">
          {!(report && runId && selected) ? <NeedsRun what="The report" busy={busy} /> : renderDocument()}
        </div>
      </div>
    </div>
  )

  function renderDocument() {
    if (!report || !runId || !selected) return null
    const intro = section(report.markdown, /introduction/i)
    const conclusion = section(report.markdown, /conclusion/i)
    const errs = classifyErrors(report.errors)
    const unverified = report.verification.filter((c) => c.status !== 'verified')
    const byId = new Map(report.sources.map((s) => [s.id, s]))
    const H = ({ n, children }: { n: number; children: ReactNode }) => <h2 className="mt-10 border-b border-line pb-1.5 font-display text-lg font-semibold">{n}. {children}</h2>

    return (
      <article className="neo-report border border-line bg-surface px-5 py-8 sm:px-10 print:border-0 print:p-0">
        <p className="label">APOLLO research report · {fmtDate(selected.created_at)}</p>
        <h1 className="mt-2 font-display text-[28px] font-semibold leading-tight">{selected.topic}</h1>
        <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
          <Pill status={report.status ?? 'complete'} />
          {report.stop_reason && <span>{STOP_REASON[report.stop_reason] ?? report.stop_reason}</span>}
          <span>· {report.sources.length} sources · {report.verification.length} claims checked</span>
        </p>

        <H n={1}>Executive summary</H>
        <p className="mt-3 max-w-[72ch] text-[15px] leading-7">{intro.replace(/\[S\d+\]/g, '').replace(/\s+/g, ' ') || 'The report has no introduction section.'}</p>

        <H n={2}>Research methodology</H>
        <ul className="mt-3 max-w-[72ch] list-disc space-y-1 pl-5 text-[15px] leading-7">
          <li>Apollo split the question into {report.plan?.length ?? 0} sub-questions; Athena searched the open web (Tavily, Serper fallback), added specialist sources where a sub-question called for them (papers, medical literature, statistics, news, code), and extracted page text.</li>
          <li>Hermes reviewed coverage after each round; {report.rounds ?? selected.rounds} of at most {selected.max_rounds} round{selected.max_rounds === 1 ? '' : 's'} ran{selected.hitl ? ', with human approval of the plan and follow-up queries' : ''}.</li>
          <li>Each section was written only from retrieved passages; uncited sentences were removed ({(errs.uncited_dropped ?? []).length} this run).</li>
          <li>Claims were checked against source sentences (embeddings + LLM, chain {system?.llm.verify_chain.join(' → ') ?? 'n/a'}).</li>
          {metrics && <li>Execution: {fmtMs(metrics.wall_ms)}, {metrics.summary.llm_calls} LLM calls, {metrics.summary.searches} searches.</li>}
        </ul>

        <H n={3}>Research plan</H>
        <ol className="mt-3 list-decimal space-y-1 pl-5 text-[15px] leading-7">{(report.plan ?? []).map((q, i) => <li key={i}>{q}</li>)}</ol>

        <H n={4}>Key findings (evidence-linked)</H>
        <div className="mt-4"><ReportView report={report} runId={runId} /></div>

        <H n={5}>Claim-by-claim evidence</H>
        <div className="mt-3 overflow-x-auto">
          <table className="w-full min-w-[560px] text-left text-sm">
            <thead><tr className="border-b border-line [&>th]:label [&>th]:py-2 [&>th]:pr-3 [&>th]:font-normal"><th>Claim</th><th>Status</th><th>Sources</th></tr></thead>
            <tbody>
              {report.verification.map((c, i) => (
                <tr key={i} className="border-b border-hair align-top [&>td]:py-2 [&>td]:pr-3">
                  <td>{c.claim}{c.evidence && <span className="mt-1 block text-xs text-muted">“{c.evidence}”</span>}</td>
                  <td><Pill status={c.status} /></td>
                  <td className="font-mono text-xs">{(c.source_ids ?? []).join(' ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <H n={6}>Source analysis</H>
        <p className="mt-3 text-sm">Categories (domain heuristic, not a credibility rating): {CATEGORIES.map((c) => `${c} ${report.sources.filter((s) => sourceCategory(s.url) === c).length}`).join(' · ')}.</p>
        <p className="mt-1 text-sm">{report.sources.filter((s) => s.extracted_by !== 'snippet only').length} of {report.sources.length} sources were read in full; {report.citation_check?.unique_cited ?? 0} are cited. No credibility scores are computed.</p>

        <H n={7}>Contradictions and disagreements</H>
        {(report.contradictions ?? []).length ? (
          <ul className="mt-3 list-disc space-y-1 pl-5 text-sm">{(report.contradictions ?? []).map((c, i) => <li key={i}>{c.source_a} vs {c.source_b}: {c.claim}</li>)}</ul>
        ) : conflictCheck(report.review_health) === 'not_checked'
          ? <p className="mt-3 text-sm text-err-ink">{CONFLICT_CHECK_TEXT.not_checked}</p>
          : <p className="mt-3 text-sm">No conflicts found in sources.{conflictCheck(report.review_health) === 'partial' ? ` ${CONFLICT_CHECK_TEXT.partial}` : ''}</p>}

        <H n={8}>Unverified findings</H>
        {unverified.length ? (
          <ul className="mt-3 space-y-1.5 text-sm">{unverified.map((c, i) => <li key={i} className="flex gap-2"><Pill status={c.status} /><span>{c.claim}</span></li>)}</ul>
        ) : <p className="mt-3 text-sm">Every checked claim was verified.</p>}

        <H n={9}>Research limitations</H>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-sm">
          {(Object.keys(errs) as ErrorKind[]).map((k) => <li key={k}>{ERROR_LABEL[k]}: {errs[k].length}</li>)}
          <li>No publication dates, credibility scores, PDF/OCR extraction, or prompt-injection scanning in this version.</li>
        </ul>

        <H n={10}>Conclusions</H>
        <p className="mt-3 max-w-[72ch] text-[15px] leading-7">{conclusion.replace(/\[S\d+\]/g, '').replace(/\s+/g, ' ') || 'The report has no conclusion section.'}</p>

        <H n={11}>Full citations</H>
        <ol className="mt-3 space-y-1.5 text-sm">
          {(report.references?.length ? report.references : report.sources).map((r) => (
            <li key={r.id} className="grid grid-cols-[3rem_minmax(0,1fr)] gap-2">
              <span className="font-mono text-xs text-accent-ink">[{r.id}]</span>
              <span className="[overflow-wrap:anywhere]">{r.title || hostOf(r.url)}. {isAttached(r.url)
                ? <span className="font-mono text-xs text-muted">attached document</span>
                : <a href={r.url} className="font-mono text-xs text-muted underline">{r.url}</a>}{byId.get(r.id)?.extracted_by === 'snippet only' && <em className="text-muted"> (snippet only)</em>}</span>
            </li>
          ))}
        </ol>
        <p className="mt-6 font-mono text-[10.5px] text-muted">Run {runId} · retrieved {fmtTime(selected.created_at)}</p>
      </article>
    )
  }
}
