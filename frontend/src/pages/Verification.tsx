// Verification: how far the report can be trusted. Claim outcomes, citation accuracy (every
// [S#] maps to a retrieved source), unsupported statements the citation filter removed,
// source accessibility, evidence completeness per section, which verifier models ran, and a
// research-quality score that is an explicit checklist (shown with its formula).

import type { ClaimStatus } from '../api'
import { classifyErrors, conflictCheck } from '../insights'
import { useStore } from '../store'
import { Legend, NeedsRun, PageHeader, Panel, Pill, StackedBar, CLAIM_KEYS, STATUS_COLOR, Stat, Unavailable, pct } from '../ui'

const KEYS = CLAIM_KEYS

export default function Verification() {
  const { report, busy, metrics, system } = useStore()
  if (!report) return <><PageHeader kicker="09 · Verification" title="Verification" /><NeedsRun what="Verification" busy={busy} /></>

  const counts = Object.fromEntries(KEYS.map((k) => [k, report.verification.filter((c) => c.status === k).length])) as Record<ClaimStatus, number>
  const total = report.verification.length
  const cc = report.citation_check
  const sections = report.sections ?? []
  const errs = classifyErrors(report.errors)
  const supported = sections.filter((s) => !s.not_found && s.citations > 0).length
  const fullText = report.sources.filter((s) => s.extracted_by !== 'snippet only').length
  const verifierRuns = (metrics?.calls ?? []).filter((c) => c.kind === 'llm' && c.node === 'verify_claims')
  const passes = new Map<string, { ok: number; failed: number }>()
  verifierRuns.forEach((c) => {
    const k = `${c.provider}:${c.model}`
    const row = passes.get(k) ?? { ok: 0, failed: 0 }
    if (c.ok) row.ok += 1
    else row.failed += 1
    passes.set(k, row)
  })

  // Research-quality checklist: each check passes or fails; score = passed / total.
  const checks = [
    { label: 'Every citation maps to a retrieved source', ok: !!cc && cc.dangling.length === 0 },
    { label: 'At least half of checked claims verified', ok: total > 0 && counts.verified / total >= 0.5 },
    { label: 'No claim without support in its source', ok: counts.not_found === 0 },
    { label: 'Every claim was checked', ok: counts.unchecked === 0 },
    { label: 'Every section was written (no LLM outage)', ok: sections.every((s) => !s.write_failed) },
    { label: 'Every sub-question has cited support', ok: sections.length > 0 && supported === sections.length },
    { label: 'At least 80% of sources read in full', ok: report.sources.length > 0 && fullText / report.sources.length >= 0.8 },
    { label: 'Contradiction check ran in every review round', ok: conflictCheck(report.review_health) === 'checked' },
  ]
  // Runs from Phase 3.19 on carry the checks the backend ran before completing (tools/quality.py);
  // older runs fall back to the checklist above, computed here from the report.
  const backend = report.quality ?? []
  const passed = backend.length ? backend.filter((c) => c.status === 'pass').length : checks.filter((c) => c.ok).length
  const ofTotal = backend.length || checks.length
  const dated = report.sources.filter((s) => s.published).length
  const kinds = report.sources.some((s) => s.kind)

  return (
    <div>
      <PageHeader kicker="09 · Verification" title="Verification" />
      <div className="grid grid-cols-2 gap-px border border-line bg-line md:grid-cols-5 [&>*]:border-0">
        <Stat label="Quality checks" value={`${passed}/${ofTotal}`} sub="passed · list below" tone={passed === ofTotal ? 'good' : passed >= ofTotal / 2 ? 'warn' : 'bad'} />
        <Stat label="Verified" value={pct(counts.verified, total)} sub={`${counts.verified} of ${total} claims`} tone="good" />
        <Stat label="Partial" value={counts.partial} sub="discussed, not fully supported" tone="warn" />
        <Stat label="Not found" value={counts.not_found} sub="hallucination risk" tone={counts.not_found ? 'bad' : 'normal'} />
        <Stat label="Unchecked" value={counts.unchecked} sub="verifier unavailable / capped" />
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <Panel label="Claim-level verification" title="Outcomes">
          <StackedBar height="h-3" parts={KEYS.map((k) => ({ value: counts[k], color: STATUS_COLOR[k], label: k }))} />
          <Legend items={KEYS.map((k) => ({ color: STATUS_COLOR[k], label: k.replace('_', ' '), value: counts[k] }))} />
          <p className="label mt-5">{backend.length ? 'Quality checks run before the report was completed' : 'Research-quality checklist (older run)'}</p>
          {backend.length ? (
            <ul className="mt-2 divide-y divide-hair">
              {backend.map((c) => (
                <li key={c.id} className="py-2 text-sm">
                  <div className="flex items-center justify-between gap-3"><span className="text-ink">{c.label}</span>
                    <Pill status={c.status === 'pass' ? 'done' : c.status === 'warn' ? 'warn' : 'failed'} label={c.status === 'pass' ? 'pass' : c.status === 'warn' ? 'warning' : 'fail'} /></div>
                  {c.status !== 'pass' && <p className="mt-1 text-xs text-muted">{c.detail}</p>}
                </li>
              ))}
            </ul>
          ) : (
            <ul className="mt-2 divide-y divide-hair">
              {checks.map((c) => <li key={c.label} className="flex items-center justify-between gap-3 py-2 text-sm"><span className="text-ink">{c.label}</span><Pill status={c.ok ? 'done' : 'failed'} label={c.ok ? 'pass' : 'fail'} /></li>)}
            </ul>
          )}
          <p className="mt-2 text-[11px] text-muted">{passed} of {ofTotal} checks passed. A warning does not stop the report; it says what the evidence cannot support. This is a checklist, not a probability that the report is correct.</p>
        </Panel>

        <Panel label="Citations" title="Citation accuracy & support">
          {!cc ? <p className="text-sm text-muted">Not available for this run.</p> : (
            <dl className="divide-y divide-hair text-sm">
              <Row k="Inline citations in report" v={cc.citations} />
              <Row k="Distinct sources cited" v={`${cc.unique_cited} of ${cc.sources}`} />
              <Row k="Citations to unknown sources" v={cc.dangling.length ? cc.dangling.join(', ') : 0} bad={cc.dangling.length > 0} />
              <Row k="Unsupported statements removed" v={(errs.uncited_dropped ?? []).length} />
              <Row k="Invalid citations removed" v={(errs.bad_citation ?? []).length} />
              <Row k="Claims supported by cited text" v={`${counts.verified + counts.partial} of ${total}`} />
            </dl>
          )}
          <p className="mt-3"><Pill status={cc && !cc.dangling.length ? 'done' : 'failed'} label={cc && !cc.dangling.length ? 'every citation maps to a real URL' : 'dangling citations'} /></p>
        </Panel>
      </div>

      <div className="mt-5 grid gap-5 lg:grid-cols-2">
        <Panel label="Evidence completeness" title="Support per section">
          <ul className="divide-y divide-hair">
            {sections.map((s) => (
              <li key={s.index} className="flex items-start gap-3 py-2.5">
                <span className="font-mono text-xs text-accent-ink">Q{s.index}</span>
                <span className="min-w-0 flex-1 text-sm text-ink">{s.question}</span>
                {s.write_failed ? <Pill status="failed" label="not written" /> : <Pill status={s.not_found ? 'not_found' : s.partly_not_found ? 'partial' : s.citations ? 'verified' : 'partial'} label={s.not_found ? 'not found' : `${s.citations} cited${s.partly_not_found ? ' · part not found' : ''}`} />}
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-muted">{supported} of {sections.length} sections have cited support.</p>
        </Panel>

        <Panel label="Source checks" title="Accessibility & verification passes">
          <dl className="divide-y divide-hair text-sm">
            <Row k="Sources read in full" v={`${fullText} of ${report.sources.length}`} />
            <Row k="Blocked / paywalled pages" v={(errs.blocked ?? []).length} />
            <Row k="Failed downloads (incl. broken links)" v={(errs.fetch_failed ?? []).length} />
            <Row k="Non-HTML (PDF) pages skipped" v={(errs.non_html ?? []).length} />
          </dl>
          <p className="label mt-4">Verifier passes</p>
          <p className="mt-1 font-mono text-[11px] text-muted">Chain: {system?.llm.verify_chain.join(' → ') ?? '–'}</p>
          <ul className="mt-2 space-y-1 text-sm">
            {[...passes.entries()].map(([k, v]) => <li key={k} className="flex justify-between gap-3"><span className="font-mono text-xs">{k}</span><span className="font-mono text-xs">{v.ok} ok{v.failed ? ` · ${v.failed} failed` : ''}</span></li>)}
            {!passes.size && <li className="text-xs text-muted">No metered verifier calls for this run.</li>}
            {(errs.quota ?? []).map((e, i) => <li key={i} className="text-xs text-warn-ink">{e.text}</li>)}
          </ul>
          {kinds ? (
            <dl className="mt-4 divide-y divide-hair border-t border-hair text-sm">
              <Row k="Original research or official sources" v={`${report.sources.filter((s) => s.kind === 'primary').length} of ${report.sources.length}`} />
              <Row k="Sources stating a publication date" v={`${dated} of ${report.sources.length}`} />
              <Row k="Duplicate pages removed" v={(report.duplicates ?? []).length} />
              <Row k="Off-topic pages removed" v={(report.irrelevant ?? []).length} />
            </dl>
          ) : <div className="mt-4"><Unavailable what="Source kind and publication dates" need="a run started after Phase 3.19; older runs recorded neither." /></div>}
        </Panel>
      </div>
    </div>
  )
}

function Row({ k, v, bad }: { k: string; v: string | number; bad?: boolean }) {
  return <div className="flex items-center justify-between gap-4 py-2"><dt className="text-muted">{k}</dt><dd className={`font-mono ${bad ? 'text-err-ink' : 'text-ink'}`}>{v}</dd></div>
}
