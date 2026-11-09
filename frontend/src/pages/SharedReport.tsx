// Public, read-only view of a shared report (#/shared/<token>). Opens without signing in: it reads only
// GET /api/shared/<token>, which returns the report itself and nothing else of the account.

import { useEffect, useState } from 'react'
import { getShared, isAttached, sharedDocxUrl, sharedMdUrl, type SharedReport as Shared } from '../api'
import ReportView from '../components/ReportView'
import { Pill, STOP_REASON, btn, fmtDate, hostOf } from '../ui'

export default function SharedReport({ token }: { token: string }) {
  const [report, setReport] = useState<Shared | null>(null)
  const [problem, setProblem] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    setReport(null)
    setProblem(null)
    getShared(token)
      .then((r) => live && setReport(r))
      .catch((e: unknown) => live && setProblem(e instanceof Error ? e.message : 'Could not load this report.'))
    return () => {
      live = false
    }
  }, [token])

  useEffect(() => {
    if (!report) return
    const before = document.title
    document.title = `${report.topic} · APOLLO report`
    return () => {
      document.title = before // leaving the shared view (e.g. "Go to APOLLO") restores the app's title
    }
  }, [report])

  return (
    <div className="min-h-screen bg-workspace">
      <header className="no-print border-b border-line bg-surface">
        <div className="mx-auto flex max-w-[960px] flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
          <span className="neo-brand-mark" aria-hidden="true">A</span>
          <div className="min-w-0 flex-1">
            <p className="font-display text-[17px] font-bold leading-none tracking-[0.18em] text-ink">APOLLO</p>
            <p className="label mt-1">Shared research report · read-only</p>
          </div>
          {report && (
            // Phones: the buttons take their own full-width row (three equal columns), so the brand text keeps its
            // width instead of breaking one word per line. From sm (640px) up the layout is unchanged.
            <div className="flex flex-wrap gap-2 max-sm:grid max-sm:basis-full max-sm:grid-cols-3">
              <a href={sharedDocxUrl(token)} download className={btn.primary}>DOCX</a>
              <a href={sharedMdUrl(token)} download className={btn.secondary}>Markdown</a>
              <button className={btn.secondary} onClick={() => window.print()}>PDF (print)</button>
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-[960px] px-4 py-6 sm:px-6">
        {problem && (
          <div role="alert" className="border border-line bg-surface px-5 py-8 text-center">
            <p className="font-display text-lg font-semibold text-ink">This report can't be opened</p>
            <p className="mt-2 text-sm text-muted">{problem}</p>
            <a href="/" className={`${btn.secondary} mt-5 inline-flex`}>Go to APOLLO</a>
          </div>
        )}
        {!report && !problem && <p className="text-sm text-muted" role="status">Loading the report…</p>}
        {report && (
          <article className="neo-report border border-line bg-surface px-5 py-8 sm:px-10 print:border-0 print:p-0">
            <p className="label">APOLLO research report · {fmtDate(report.created_at)}</p>
            <h1 className="mt-2 font-display text-[28px] font-semibold leading-tight">{report.topic}</h1>
            <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
              <Pill status={report.status ?? 'complete'} />
              {report.stop_reason && <span>{STOP_REASON[report.stop_reason] ?? report.stop_reason}</span>}
              <span>· {report.sources.length} sources · {report.verification.length} claims checked</span>
            </p>

            <div className="mt-6">
              <ReportView report={{ ...report, contradictions_html_path: null }} runId="" toolbar={false} />
            </div>

            <h2 className="mt-10 border-b border-line pb-1.5 font-display text-lg font-semibold">Claim-by-claim evidence</h2>
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

            <h2 className="mt-10 border-b border-line pb-1.5 font-display text-lg font-semibold">Full citations</h2>
            <ol className="mt-3 space-y-1.5 text-sm">
              {(report.references?.length ? report.references : report.sources).map((r) => (
                <li key={r.id} className="grid grid-cols-[3rem_minmax(0,1fr)] gap-2">
                  <span className="font-mono text-xs text-accent-ink">[{r.id}]</span>
                  <span className="[overflow-wrap:anywhere]">{r.title || hostOf(r.url)}. {isAttached(r.url)
                    ? <span className="font-mono text-xs text-muted">attached document</span>
                    : <a href={r.url} className="font-mono text-xs text-muted underline">{r.url}</a>}</span>
                </li>
              ))}
            </ol>
          </article>
        )}
      </main>
    </div>
  )
}
