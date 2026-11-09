// Source Intelligence: what the report rests on. Real per-source facts (how the text was
// obtained, whether it is cited, how claims citing it were verified), a domain-name heuristic
// for source category, and independence / duplication checks across sources. No credibility
// or authority score is shown: the backend computes none, and none is estimated here.

import { useState } from 'react'
import type { Source } from '../api'
import { CATEGORIES, classifyErrors, sourceCategory, type SourceCategory } from '../insights'
import { useStore } from '../store'
import { Empty, NeedsRun, PageHeader, Panel, Pill, Segmented, StackedBar, CLAIM_KEYS, STATUS_COLOR, Stat, field, fmtDate, hostOf, pct } from '../ui'

const KEYS = CLAIM_KEYS
type Sort = 'id' | 'claims' | 'domain'

export default function Sources() {
  const { report, busy } = useStore()
  const [cat, setCat] = useState<SourceCategory | 'all'>('all')
  const [sort, setSort] = useState<Sort>('id')
  const [q, setQ] = useState('')
  if (!report) return <><PageHeader kicker="07 · Source intelligence" title="Source analysis" /><NeedsRun what="Source intelligence" busy={busy} /></>

  const sources = report.sources
  const fullText = sources.filter((s) => s.extracted_by && s.extracted_by !== 'snippet only').length
  const cited = sources.filter((s) => s.cited).length
  const domains = new Map<string, Source[]>()
  sources.forEach((s) => domains.set(hostOf(s.url), [...(domains.get(hostOf(s.url)) ?? []), s]))
  const titles = new Map<string, Source[]>()
  sources.forEach((s) => {
    const t = (s.title ?? '').trim().toLowerCase()
    if (t) titles.set(t, [...(titles.get(t) ?? []), s])
  })
  const dupTitles = [...titles.values()].filter((g) => g.length > 1)
  const sameDomain = [...domains.entries()].filter(([, g]) => g.length > 1)
  const errs = classifyErrors(report.errors)
  const catCount = (c: SourceCategory) => sources.filter((s) => sourceCategory(s.url) === c).length
  const claimsOf = (s: Source) => KEYS.reduce((a, k) => a + (s.claims?.[k] ?? 0), 0)
  const needle = q.trim().toLowerCase()
  const shown = sources
    .filter((s) => (cat === 'all' || sourceCategory(s.url) === cat) && (!needle || `${s.title} ${s.url}`.toLowerCase().includes(needle)))
    .sort((a, b) => sort === 'claims' ? claimsOf(b) - claimsOf(a) : sort === 'domain' ? hostOf(a.url).localeCompare(hostOf(b.url)) : Number(a.id.slice(1)) - Number(b.id.slice(1)))

  return (
    <div>
      <PageHeader kicker="07 · Source intelligence" title="Source analysis" />
      <div className="grid grid-cols-2 gap-px border border-line bg-line md:grid-cols-4 [&>*]:border-0">
        <Stat label="Sources" value={sources.length} tone="accent" />
        <Stat label="Independent domains" value={domains.size} sub={`${sameDomain.length} domain${sameDomain.length === 1 ? '' : 's'} used more than once`} />
        <Stat label="Full text read" value={pct(fullText, sources.length)} sub={`${sources.length - fullText} snippet only`} />
        <Stat label="Cited in report" value={pct(cited, sources.length)} sub={`${cited} of ${sources.length}`} tone="good" />
      </div>

      <div className="mt-5 grid grid-cols-[minmax(0,1fr)] gap-5 lg:grid-cols-2">
        <Panel label="Category" title="Source categories" aside="domain heuristic">
          <ul className="space-y-1.5">
            {CATEGORIES.map((c) => (
              <li key={c}>
                <button onClick={() => setCat(cat === c ? 'all' : c)} aria-pressed={cat === c} className={`flex w-full items-center justify-between border px-3 py-1.5 text-sm ${cat === c ? 'border-ink bg-ink text-white' : 'border-line text-ink hover:border-ink'}`}>
                  <span>{c}</span><span className="font-mono text-xs">{catCount(c)}</span>
                </button>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-[11px] text-muted">Categorised from the domain name only (.gov → Government, .edu/.ac → Academic, known news sites → News). It describes the kind of site, not whether its content is correct. "Official reports" fall under Government.</p>
        </Panel>
        <Panel label="Independence & duplication" title="Source relationships">
          <ul className="space-y-3 text-sm">
            <li>
              <p className="text-ink">Same-domain clusters</p>
              {sameDomain.length ? <p className="mt-0.5 font-mono text-xs text-muted">{sameDomain.map(([h, g]) => `${h} (${g.map((s) => s.id).join(', ')})`).join(' · ')}</p> : <p className="text-xs text-muted">Every source is from a different domain.</p>}
            </li>
            <li>
              <p className="text-ink">Duplicate titles (possible syndicated / SEO copies)</p>
              {dupTitles.length ? <p className="mt-0.5 font-mono text-xs text-muted">{dupTitles.map((g) => g.map((s) => s.id).join(' = ')).join(' · ')}</p> : <p className="text-xs text-muted">No two sources share a title.</p>}
            </li>
            <li>
              <p className="text-ink">Extraction problems</p>
              <p className="text-xs text-muted">{(errs.blocked ?? []).length} blocked or paywalled · {(errs.non_html ?? []).length} PDF / non-HTML skipped · {(errs.no_main_text ?? []).length} without main text</p>
            </li>
          </ul>
        </Panel>
      </div>

      <div className="mt-5">
        <Panel label="Sources" title={cat === 'all' ? 'All sources' : `${cat} sources`} pad={false}
          aside={<span className="inline-flex flex-wrap items-center gap-2"><Segmented<Sort> label="Sort sources" value={sort} onChange={setSort} options={[{ value: 'id', label: 'ID' }, { value: 'claims', label: 'Most claims' }, { value: 'domain', label: 'Domain' }]} /></span>}>
          <div className="border-b border-hair p-3">
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search title or URL" aria-label="Search sources" className={`${field} py-1 sm:w-72`} />
          </div>
          {!shown.length ? <Empty>No sources match.</Empty> : (
            <ul className="divide-y divide-hair">
              {shown.map((s) => (
                <li key={s.id} className="grid gap-2 px-4 py-3 md:grid-cols-[3rem_minmax(0,1fr)_auto]">
                  <span className="font-mono text-xs text-accent-ink">{s.id}</span>
                  <div className="min-w-0">
                    {s.kind === 'document' ? (
                      <>
                        <p className="text-sm text-ink [overflow-wrap:anywhere]">{s.title}</p>
                        <p className="mt-0.5 font-mono text-[10.5px] text-muted">Attached file{s.pages ? ` · ${s.pages} pages` : ''}; used by every sub-question</p>
                      </>
                    ) : (
                      <>
                        <a href={s.url} target="_blank" rel="noreferrer" className="text-sm text-ink underline decoration-line underline-offset-2 hover:decoration-ink [overflow-wrap:anywhere]">{s.title || s.url}</a>
                        <p className="mt-0.5 truncate font-mono text-[10.5px] text-muted">{s.url}</p>
                      </>
                    )}
                    {s.question && <p className="mt-1 text-xs text-muted">Retrieved for Q{s.section}: {s.question}</p>}
                    {s.kind === 'video' && <p className="mt-1 text-xs text-muted">The video you shared, read from {s.extracted_by === 'gemini_transcript' ? 'a Gemini transcription (no usable captions)' : 'its captions'}; used by every sub-question.</p>}
                    {s.retrieved_at && s.kind !== 'video' && s.kind !== 'document' && (
                      <p className="mt-0.5 font-mono text-[10.5px] text-muted">
                        Published: {s.published || 'not stated by the page'} · Researched: {fmtDate(s.retrieved_at * 1000)}
                      </p>
                    )}
                    {claimsOf(s) > 0 && <div className="mt-2 max-w-xs"><StackedBar height="h-1.5" parts={KEYS.map((k) => ({ value: s.claims?.[k] ?? 0, color: STATUS_COLOR[k], label: k }))} /></div>}
                  </div>
                  <div className="flex flex-wrap content-start gap-1.5 md:max-w-[260px] md:justify-end">
                    {s.kind === 'primary' && <Pill status="working" label="original research / official" />}
                    {s.kind === 'video' ? <Pill status="working" label="your video · transcript" />
                      : s.kind === 'document' ? <Pill status="working" label="your document" />
                      : <Pill status="idle" label={sourceCategory(s.url)} />}
                    <Pill status={s.extracted_by === 'snippet only' ? 'partial' : 'done'} label={s.extracted_by === 'snippet only' ? 'snippet only' : 'full text'} />
                    <Pill status={s.cited ? 'verified' : 'unchecked'} label={s.cited ? 'cited' : 'not cited'} />
                    {KEYS.filter((k) => s.claims?.[k]).map((k) => <Pill key={k} status={k} label={`${s.claims?.[k]} ${k.replace('_', ' ')}`} />)}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </div>
    </div>
  )
}
