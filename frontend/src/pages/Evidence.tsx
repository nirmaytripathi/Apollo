// Evidence Explorer: from a report statement to the exact passage behind it and its original source.
// Three panels: claims (with status) → evidence for the selected claim (its parts, each judged
// supported / partial / contradicted / unsupported, and the passages it was checked against,
// quotes highlighted) → details of the selected source. All data comes from
// /api/runs/{id}/evidence (verify/claims.py); quotes were checked by the backend to be in the
// source text. Runs verified before claims had parts show their best-matching sentence only.

import { useEffect, useState, type ReactNode } from 'react'
import { getEvidence, isAttached, videoAt, type ClaimPart, type Contradiction, type Evidence, type EvidenceClaim, type Passage, type PartVerdict, type Source } from '../api'
import { sourceCategory } from '../insights'
import { useStore } from '../store'
import { CLAIM_KEYS, Empty, Meter, NeedsRun, PageHeader, Panel, Pill, fmtDate, hostOf } from '../ui'

const KICKER = '05 · Evidence explorer'
const TITLE = 'From statement to source'

const STATUS_TEXT: Record<string, string> = {
  verified: 'Every part of this claim is backed by a quoted passage.',
  partial: 'Only part of this claim is fully backed; each part’s verdict is below.',
  contradicted: 'A cited source states the opposite of part of this claim.',
  not_found: 'No part of this claim is backed by the passages of its cited sources.',
  unchecked: 'Not checked: the verifier could not be asked (quota, error or claim cap).',
}

// Runs verified before claims had parts: one yes/no per source, "partial" = a similar sentence exists.
const LEGACY_STATUS_TEXT: Record<string, string> = {
  verified: 'The earlier verifier said a cited source supports this claim (one yes/no, not checked part by part).',
  partial: 'The earlier verifier found a similar sentence in a cited source but did not confirm the claim.',
  contradicted: STATUS_TEXT.contradicted!,
  not_found: 'The earlier verifier found no similar sentence in the cited sources.',
  unchecked: STATUS_TEXT.unchecked!,
}

// Part verdicts reuse the claim status pill colours.
const VERDICT_PILL: Record<PartVerdict, string> = {
  supported: 'verified', partial: 'partial', contradicted: 'contradicted', unsupported: 'not_found',
}

export default function EvidenceExplorer() {
  const { report, runId, busy } = useStore()
  const [data, setData] = useState<{ runId: string; evidence: Evidence } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<string>('all')
  const [pick, setPick] = useState<{ claim: number; source: string | null; passage: string | null }>({ claim: 1, source: null, passage: null })

  useEffect(() => {
    if (!runId || !report) return
    let live = true
    setError(null)
    getEvidence(runId)
      .then((evidence) => {
        if (!live) return
        setData({ runId, evidence })
        setPick({ claim: evidence.claims[0]?.n ?? 1, source: null, passage: null })
      })
      .catch((e: unknown) => live && setError(e instanceof Error ? e.message : 'Could not load the evidence.'))
    return () => { live = false }
  }, [runId, report])

  if (!report) return <><PageHeader kicker={KICKER} title={TITLE} /><NeedsRun what="Evidence" busy={busy} /></>
  if (error) return <><PageHeader kicker={KICKER} title={TITLE} /><Panel><Empty>{error}</Empty></Panel></>
  if (!data || data.runId !== runId) return <><PageHeader kicker={KICKER} title={TITLE} /><Panel><Empty>Loading the evidence…</Empty></Panel></>

  const ev = data.evidence
  const claims = ev.claims
  if (!claims.length) return <><PageHeader kicker={KICKER} title={TITLE} /><Panel><Empty>This report has no cited claims.</Empty></Panel></>
  const sources = new Map(ev.sources.map((s) => [s.id, s]))
  const claim = claims.find((c) => c.n === pick.claim) ?? claims[0]! // claims is not empty (checked above)
  const shown = claims.filter((c) => filter === 'all' || c.status === filter)
  const claimSources = claim.source_ids.filter((id) => sources.has(id))
  const sourceId = pick.source && claimSources.includes(pick.source) ? pick.source : claimSources[0] ?? null
  const passage = claim.passages.find((p) => p.label === pick.passage && p.source_id === sourceId)
    ?? claim.passages.find((p) => p.source_id === sourceId && claim.parts.some((pt) => pt.passage === p.label))
    ?? claim.passages.find((p) => p.source_id === sourceId)
    ?? null
  const select = (n: number) => {
    setPick({ claim: n, source: null, passage: null })
    // Below xl the panels are stacked: bring the evidence into view instead of leaving the reader at the list.
    if (!window.matchMedia('(min-width: 1280px)').matches) {
      requestAnimationFrame(() => document.getElementById('evidence-panel')?.scrollIntoView())
    }
  }
  const openPassage = (label: string) => {
    const p = claim.passages.find((x) => x.label === label)
    if (p) setPick({ claim: claim.n, source: p.source_id, passage: p.label })
  }

  return (
    <div>
      <PageHeader kicker={KICKER} title={TITLE}>
        <span className="font-mono text-xs text-muted">{claims.length} claims · {ev.sources.length} sources</span>
      </PageHeader>

      {ev.legacy && (
        <div className="mb-5 border border-warn/70 bg-warn/15 px-4 py-3 text-sm text-warn-ink" role="note">
          This run was verified before claims were split into parts, so only its best-matching sentence is shown and
          "partial" means only that a similar sentence exists. Start a new run for part-by-part evidence.
        </div>
      )}

      <div className="grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[320px_minmax(0,1fr)_320px]">
        <ClaimsPanel claims={claims} shown={shown} filter={filter} onFilter={setFilter} active={claim.n} onSelect={select} />

        <div id="evidence-panel" className="min-w-0 scroll-mt-28">
        <Panel label={`Claim C${claim.n}`} title="Evidence" aside={<Pill status={claim.status} />}>
          <p className="text-[15px] leading-relaxed text-ink">{claim.text}</p>
          <p className="mt-2 text-sm text-muted">{(ev.legacy || !claim.parts.length ? LEGACY_STATUS_TEXT : STATUS_TEXT)[claim.status]}</p>
          {ev.legacy || !claim.parts.length ? (
            <LegacyEvidence claim={claim} legacy={ev.legacy} />
          ) : (
            <Parts parts={claim.parts} onOpen={openPassage} />
          )}

          {claimSources.length > 0 && claim.passages.length > 0 && (
            <div className="mt-5">
              <p className="label">Passages checked{claimSources.length > 1 ? ' · pick a source' : ''}</p>
              {claimSources.length > 1 && (
                <div role="tablist" aria-label="Cited sources" className="mt-2 flex flex-wrap gap-1.5">
                  {claimSources.map((id) => (
                    <button key={id} role="tab" aria-selected={id === sourceId} onClick={() => setPick({ claim: claim.n, source: id, passage: null })}
                      className={`border px-2 py-0.5 font-mono text-[11px] ${id === sourceId ? 'border-ink bg-ink text-white' : 'border-line text-muted hover:text-ink'}`}>
                      {id} · {verdictSummary(claim.parts, id)}
                    </button>
                  ))}
                </div>
              )}
              <ul className="mt-2 space-y-2">
                {claim.passages.filter((p) => p.source_id === sourceId).map((p) => (
                  <PassageItem key={p.label} p={p} parts={claim.parts.filter((pt) => pt.passage === p.label)} active={p.label === passage?.label}
                    onSelect={() => setPick({ claim: claim.n, source: p.source_id, passage: p.label })} />
                ))}
              </ul>
            </div>
          )}
        </Panel>
        </div>

        <div className="min-w-0 xl:sticky xl:top-16 xl:self-start">
          {sourceId && sources.get(sourceId)
            ? <SourcePanel source={sources.get(sourceId)!} passage={passage} claim={claim} conflicts={ev.contradictions.filter((c) => c.source_a === sourceId || c.source_b === sourceId)} />
            : <Panel label="Source" title="No cited source"><Empty>This claim cites no retrieved source.</Empty></Panel>}
        </div>
      </div>
    </div>
  )
}

function ClaimsPanel({ claims, shown, filter, onFilter, active, onSelect }: {
  claims: EvidenceClaim[]; shown: EvidenceClaim[]; filter: string; onFilter: (f: string) => void; active: number; onSelect: (n: number) => void
}) {
  const filters = ['all', ...CLAIM_KEYS.filter((k) => claims.some((c) => c.status === k))]
  return (
    <Panel label="Claims" title="Statements in the report" pad={false}>
      <div role="group" aria-label="Filter claims by status" className="flex flex-wrap gap-1.5 border-b border-hair p-3">
        {filters.map((f) => (
          <button key={f} onClick={() => onFilter(f)} aria-pressed={filter === f}
            className={`border px-2 py-0.5 font-mono text-[11px] uppercase ${filter === f ? 'border-ink bg-ink text-white' : 'border-line text-muted hover:text-ink'}`}>
            {f.replace('_', ' ')} · {f === 'all' ? claims.length : claims.filter((c) => c.status === f).length}
          </button>
        ))}
      </div>
      {!shown.length ? <Empty>No claims with this status.</Empty> : (
        <ul className="max-h-[70vh] divide-y divide-hair overflow-y-auto">
          {shown.map((c) => (
            <li key={c.n}>
              <button onClick={() => onSelect(c.n)} aria-current={c.n === active}
                className={`grid w-full grid-cols-[2rem_minmax(0,1fr)] gap-2 px-3 py-2.5 text-left hover:bg-workspace ${c.n === active ? 'bg-accent/10' : ''}`}>
                <span className="font-mono text-[11px] text-muted">C{c.n}</span>
                <span>
                  <span className="block text-[13px] leading-snug text-ink">{c.text}</span>
                  <span className="mt-1 flex flex-wrap items-center gap-1.5"><Pill status={c.status} />
                    <span className="font-mono text-[10.5px] text-muted">{c.source_ids.join(' ')}</span></span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}

function Parts({ parts, onOpen }: { parts: ClaimPart[]; onOpen: (label: string) => void }) {
  const missing = parts.filter((p) => p.verdict === 'unsupported')
  return (
    <div className="mt-4">
      <p className="label">What the sources back, part by part</p>
      <ol className="mt-2 space-y-2">
        {parts.map((pt, i) => (
          <li key={i} className="border border-line p-2.5">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <p className="min-w-0 flex-1 text-sm text-ink">{pt.text}</p>
              <Pill status={VERDICT_PILL[pt.verdict]} label={pt.verdict} />
            </div>
            {pt.quote && (
              <blockquote className="mt-2 border-l-2 border-accent-strong bg-workspace px-2.5 py-1.5 text-[13px] text-ink">
                “{pt.quote}”
                <button onClick={() => onOpen(pt.passage)} className="ml-2 font-mono text-[11px] text-accent-ink underline underline-offset-2">{pt.passage}</button>
              </blockquote>
            )}
            {pt.note && <p className="mt-1.5 text-xs text-warn-ink">Lowered by Apollo: {pt.note}.</p>}
          </li>
        ))}
      </ol>
      {missing.length > 0 && missing.length < parts.length && (
        <p className="mt-3 border-l-2 border-err pl-3 text-sm text-err-ink">
          Not backed by the sources: {missing.map((p) => `“${p.text}”`).join('; ')}
        </p>
      )}
    </div>
  )
}

function LegacyEvidence({ claim, legacy }: { claim: EvidenceClaim; legacy: boolean }) {
  return (
    <div className="mt-4">
      <p className="label">{legacy ? 'Best-matching source sentence' : 'Closest passage'}</p>
      <blockquote className="mt-2 border-l-2 border-accent-strong bg-workspace px-3 py-2 text-[13px] leading-relaxed text-ink">
        {claim.best_match ? `“${claim.best_match}”` : 'No matching sentence in the retrieved source text.'}
      </blockquote>
      {claim.best_match && <p className="mt-1 font-mono text-[11px] text-muted">Similarity {claim.similarity.toFixed(2)} (0–1, not a probability)</p>}
    </div>
  )
}

function PassageItem({ p, parts, active, onSelect }: { p: Passage; parts: ClaimPart[]; active: boolean; onSelect: () => void }) {
  return (
    <li>
      <button onClick={onSelect} aria-pressed={active}
        className={`block w-full border p-2.5 text-left ${active ? 'border-accent-strong bg-accent/5' : 'border-line hover:bg-workspace'}`}>
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-[11px] text-accent-ink">{p.label}</span>
          {parts.map((pt, i) => <Pill key={i} status={VERDICT_PILL[pt.verdict]} label={pt.verdict} />)}
          {p.heading && <span className="truncate text-[11px] text-muted">§ {p.heading}</span>}
          {p.start != null && <span className="font-mono text-[11px] text-accent-ink">▶ {clock(p.start)}</span>}
          {p.page != null && <span className="font-mono text-[11px] text-accent-ink">p. {p.page}</span>}
          <span className="ml-auto font-mono text-[10.5px] text-muted">sim {p.similarity.toFixed(2)}</span>
        </span>
        <span className="mt-1.5 block text-[13px] leading-relaxed">
          {p.before && <span className="text-muted">{p.before} </span>}
          <span className="text-ink">{highlight(p.text, parts.map((pt) => pt.quote))}</span>
          {p.after && <span className="text-muted"> {p.after}</span>}
        </span>
      </button>
    </li>
  )
}

function SourcePanel({ source, passage, claim, conflicts }: { source: Source; passage: Passage | null; claim: EvidenceClaim; conflicts: Contradiction[] }) {
  const mine = claim.passages.filter((p) => p.source_id === source.id)
  const best = Math.max(0, ...mine.map((p) => p.similarity))
  const video = source.kind === 'video'
  const attached = isAttached(source.url)
  return (
    <Panel label={`Source ${source.id}`} title={source.title || hostOf(source.url)}>
      <dl className="space-y-3 text-sm">
        <Row label="Original page">
          <a href={source.url} target="_blank" rel="noreferrer" className="break-all text-accent-ink underline underline-offset-2">{source.url}</a>
          {passage && video && passage.start != null && (
            <a href={videoAt(source.url, passage.start)} target="_blank" rel="noreferrer" className="mt-1 block text-xs text-accent-ink underline underline-offset-2">
              Watch from {clock(passage.start)}, where passage {passage.label} is said ↗
            </a>
          )}
          {attached && <p className="text-xs text-muted">A file you attached: it has no web address to open.</p>}
          {passage && !video && !attached && (
            <a href={textFragmentUrl(source.url, passage.text)} target="_blank" rel="noreferrer" className="mt-1 block text-xs text-accent-ink underline underline-offset-2">
              Open the page at passage {passage.label} ↗
            </a>
          )}
          {passage && !video && !attached && <p className="mt-0.5 text-[11px] text-muted">Chrome and Edge scroll to and highlight it; if the page has changed, it opens at the top.</p>}
        </Row>
        <Row label="Where in the source">
          {passage ? (
            <>
              {video ? (passage.start != null ? <>Said at {clock(passage.start)} in the video</> : 'Time in the video not found')
                : passage.page != null ? <>On page {passage.page} of the document</>
                : passage.heading ? <>Under “{passage.heading}”</> : 'No heading found above it'}
              {passage.position !== null && <span className="text-muted"> · about {Math.round(passage.position * 100)}% into the {video ? 'transcript' : 'extracted text'}</span>}
              {!video && !attached && <p className="mt-0.5 text-[11px] text-muted">Page numbers: not available (web pages, no PDF pages).</p>}
            </>
          ) : <span className="text-muted">No passage from this source.</span>}
        </Row>
        <Row label="Relevance to this claim">
          {mine.length ? (
            <>
              <div className="flex items-center gap-2"><div className="w-28"><Meter value={best} max={1} /></div><span className="font-mono text-xs">{best.toFixed(2)}</span></div>
              <p className="mt-0.5 text-[11px] text-muted">Embedding similarity of its closest passage (0–1, not a probability).</p>
            </>
          ) : <span className="text-muted">Not available: this run kept no passages per source.</span>}
        </Row>
        <Row label="Source type">
          {attached ? <>Document you attached{source.pages ? ` · ${source.pages} pages` : ''}</>
            : video ? <>Video you shared · {hostOf(source.url)}<p className="mt-0.5 text-[11px] text-muted">Records what the video says, not whether it is true; the other sources check it.</p></> : <>
            {source.kind === 'primary' ? 'Original research or official · ' : source.kind === 'secondary' ? 'Secondary article · ' : ''}
            {sourceCategory(source.url)} · {hostOf(source.url)}
            <p className="mt-0.5 text-[11px] text-muted">A rule-based guess from the web address, not a credibility rating.</p>
          </>}
        </Row>
        <Row label="Dates">
          Published: {source.published || (source.retrieved_at ? 'not stated by the page' : 'not recorded for this run')}
          <br />Researched: {source.retrieved_at ? fmtDate(source.retrieved_at * 1000) : 'not recorded for this run'}
          <p className="mt-0.5 text-[11px] text-muted">The publication date comes from the page itself; the research date is when Apollo read it.</p>
        </Row>
        <Row label="Found for">
          {video ? 'Every sub-question: it is the video the research is about'
            : attached ? 'Every sub-question: you attached it to this research'
            : source.question ? <>Sub-question {source.section}: {source.question}</> : <span className="text-muted">–</span>}
        </Row>
        <Row label="Text read from">
          {TEXT_FROM[source.extracted_by ?? ''] ?? 'Search snippet only (page text unavailable)'}
        </Row>
        {conflicts.length > 0 && (
          <Row label="Conflicts Hermes found">
            <ul className="space-y-1">
              {conflicts.map((c, i) => <li key={i} className="text-xs">{c.source_a} vs {c.source_b}: {c.claim}</li>)}
            </ul>
          </Row>
        )}
      </dl>
    </Panel>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="label">{label}</dt>
      <dd className="mt-1 text-ink">{children}</dd>
    </div>
  )
}

/** "S3 · 1 supported, 1 contradicted" style summary of what one source decided for a claim. */
function verdictSummary(parts: ClaimPart[], sourceId: string): string {
  const mine = parts.filter((p) => p.source_id === sourceId)
  if (!mine.length) return 'no part decided'
  const counts = new Map<string, number>()
  for (const p of mine) counts.set(p.verdict, (counts.get(p.verdict) ?? 0) + 1)
  return [...counts].map(([v, n]) => `${n} ${v}`).join(', ')
}

/** Marks every quote (pieces split at "...") found in `text`, ignoring case. */
function highlight(text: string, quotes: string[]): ReactNode {
  const lower = text.toLowerCase()
  const ranges: [number, number][] = []
  for (const q of quotes) {
    for (const piece of q.split(/\.\.\.|…/).map((s) => s.trim().replace(/^["“”']+|["“”'.,;:]+$/g, '')).filter((s) => s.length > 2)) {
      const at = lower.indexOf(piece.toLowerCase())
      if (at >= 0) ranges.push([at, at + piece.length])
    }
  }
  if (!ranges.length) return text
  ranges.sort((a, b) => a[0] - b[0])
  const merged: [number, number][] = []
  for (const r of ranges) {
    const last = merged[merged.length - 1]
    if (last && r[0] <= last[1]) last[1] = Math.max(last[1], r[1])
    else merged.push([...r])
  }
  const out: ReactNode[] = []
  let pos = 0
  merged.forEach(([a, b], i) => {
    if (a > pos) out.push(text.slice(pos, a))
    out.push(<mark key={i} className="bg-warn/40 text-ink">{text.slice(a, b)}</mark>)
    pos = b
  })
  if (pos < text.length) out.push(text.slice(pos))
  return out
}

const TEXT_FROM: Record<string, string> = {
  trafilatura: 'The page itself',
  tavily_raw: 'The search provider’s copy of the page',
  youtube_captions: 'The video’s own captions',
  pdf_text: 'The PDF’s own text layer, page by page',
  plain_text: 'The text file as written',
  gemini_transcript: 'A Gemini transcription of the video (it had no usable captions): model output, not captions',
  openalex_text: 'The abstract, as OpenAlex returned it',
  semantic_scholar_text: 'The abstract, as Semantic Scholar returned it',
  pubmed_text: 'The abstract, as PubMed returned it',
  europe_pmc_text: 'The abstract, as Europe PMC returned it',
  replay: 'Replay fixture: fixed offline test text, not a real source',
}

/** 75 -> "01:15", 3725 -> "1:02:05" (same format as the transcript's own stamps). */
function clock(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0')
  const ss = String(s % 60).padStart(2, '0')
  return s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${mm}:${ss}`
}

/** Link that opens the page scrolled to `passage` (URL text fragment; supported by Chromium browsers). */
function textFragmentUrl(url: string, passage: string): string {
  const enc = (s: string) => encodeURIComponent(s).replace(/-/g, '%2D').replace(/,/g, '%2C')
  const words = passage.replace(/\s+/g, ' ').trim().split(' ')
  const directive = words.length > 10 ? `${enc(words.slice(0, 5).join(' '))},${enc(words.slice(-5).join(' '))}` : enc(words.join(' '))
  return `${url.split('#')[0]}#:~:text=${directive}`
}
