// Claims & Evidence: every checked claim, the claim-evidence graph (claims ↔ cited sources),
// and a detail panel with the full evidence chain: claim → status and similarity → the exact
// best-matching source sentence → the source page. Filter by status, search, sort.

import { useState } from 'react'
import type { Claim, Source } from '../api'
import { useStore } from '../store'
import { Empty, NeedsRun, PageHeader, Panel, Pill, Segmented, field, fmtDate, hostOf } from '../ui'

const FILTERS = ['all', 'verified', 'partial', 'contradicted', 'not_found', 'unchecked'] as const
type Filter = (typeof FILTERS)[number]
type Sort = 'report' | 'confidence' | 'status'
const RANK = { contradicted: 0, not_found: 1, unchecked: 2, partial: 3, verified: 4 } as const

export default function Claims() {
  const { report, busy, selected } = useStore()
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<Sort>('report')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<number | null>(null)
  if (!report) return <><PageHeader kicker="04 · Claims & evidence" title="Claim-level evidence" /><NeedsRun what="Claims and evidence" busy={busy} /></>

  const claims = report.verification ?? []
  const byId = new Map(report.sources.map((s) => [s.id, s]))
  const count = (f: Filter) => (f === 'all' ? claims.length : claims.filter((c) => c.status === f).length)
  const needle = q.trim().toLowerCase()
  const shown = claims
    .map((c, i) => ({ c, i }))
    .filter(({ c }) => (filter === 'all' || c.status === filter) && (!needle || c.claim.toLowerCase().includes(needle) || (c.evidence ?? '').toLowerCase().includes(needle)))
    .sort((a, b) => sort === 'confidence' ? (b.c.similarity ?? -1) - (a.c.similarity ?? -1) : sort === 'status' ? RANK[a.c.status] - RANK[b.c.status] : a.i - b.i)
  const active = open !== null ? claims[open] : undefined

  return (
    <div>
      <PageHeader kicker="04 · Claims & evidence" title="Claim-level evidence">
        <span className="font-mono text-xs text-muted">{claims.length} claims · {report.sources.length} sources</span>
      </PageHeader>

      <Panel label="Claim-evidence graph" title="Which sources each claim rests on" aside="click a claim">
        <EvidenceGraph claims={claims} sources={report.sources} open={open} onOpen={setOpen} />
      </Panel>

      <div className="mt-5 grid grid-cols-[minmax(0,1fr)] gap-5 xl:grid-cols-[minmax(0,1fr)_400px]">
        <Panel pad={false}>
          <div className="flex flex-wrap items-center gap-2 border-b border-hair p-3">
            <div role="group" aria-label="Filter claims by status" className="flex flex-wrap gap-1.5">
              {FILTERS.map((f) => (
                <button key={f} onClick={() => setFilter(f)} aria-pressed={filter === f} className={`border px-2 py-0.5 font-mono text-[11px] uppercase ${filter === f ? 'border-ink bg-ink text-white' : 'border-line text-muted hover:text-ink'}`}>
                  {f.replace('_', ' ')} · {count(f)}
                </button>
              ))}
            </div>
            <Segmented<Sort> label="Sort claims" value={sort} onChange={setSort} options={[{ value: 'report', label: 'Report order' }, { value: 'confidence', label: 'Similarity' }, { value: 'status', label: 'Weakest first' }]} />
            <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search claims or evidence" aria-label="Search claims or evidence" className={`${field} py-1 sm:ml-auto sm:w-56`} />
          </div>
          {!shown.length ? <Empty>No claims match.</Empty> : (
            <ul className="divide-y divide-hair">
              {shown.map(({ c, i }) => (
                <li key={i}>
                  <button onClick={() => setOpen(open === i ? null : i)} aria-expanded={open === i} className={`grid w-full grid-cols-[2.25rem_minmax(0,1fr)_auto] items-start gap-2 px-3 py-2.5 text-left hover:bg-workspace ${open === i ? 'bg-accent/5' : ''}`}>
                    <span className="font-mono text-[11px] text-muted">C{i + 1}</span>
                    <span className="text-sm text-ink">{c.claim}</span>
                    <span className="flex flex-col items-end gap-1">
                      <Pill status={c.status} />
                      {c.similarity !== undefined && <span className="font-mono text-[10.5px] text-muted">sim {c.similarity.toFixed(2)}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <div className="min-w-0 xl:sticky xl:top-16 xl:self-start">
          {active && open !== null ? <ClaimDetail claim={active} n={open + 1} byId={byId} retrieved={selected?.created_at} /> : (
            <Panel label="Evidence chain" title="Select a claim"><Empty>Choose a claim to see its evidence chain.</Empty></Panel>
          )}
        </div>
      </div>
    </div>
  )
}

function ClaimDetail({ claim, n, byId, retrieved }: { claim: Claim; n: number; byId: Map<string, Source>; retrieved?: number }) {
  const ids = claim.source_ids ?? []
  const unknown = ids.filter((id) => !byId.has(id))
  return (
    <Panel label={`Claim C${n}`} title="Evidence chain" aside={<Pill status={claim.status} />}>
      <ol className="space-y-4 text-sm">
        <li>
          <p className="label">1 · Claim (from the report)</p>
          <p className="mt-1 text-ink">{claim.claim}</p>
        </li>
        <li>
          <p className="label">2 · Verification</p>
          <p className="mt-1 text-ink">
            {claim.status === 'verified' ? 'Supported by the source text.' : claim.status === 'partial' ? 'The sources back only part of this claim.' : claim.status === 'contradicted' ? 'A cited source states the opposite of part of this claim.' : claim.status === 'not_found' ? 'No supporting text found in the cited source.' : 'Not checked (verifier unavailable or claim cap reached).'}
          </p>
          {claim.similarity !== undefined && <p className="mt-1 font-mono text-[11px] text-muted">Semantic similarity to best sentence: {claim.similarity.toFixed(3)} (0–1; not a probability)</p>}
        </li>
        <li>
          <p className="label">3 · Exact source passage</p>
          <blockquote className="mt-1 border-l-2 border-accent-strong bg-workspace px-3 py-2 text-[13px] leading-relaxed text-ink">{claim.evidence ? `“${claim.evidence}”` : 'No matching sentence in the retrieved source text.'}</blockquote>
        </li>
        <li>
          <p className="label">4 · Cited sources · citation check</p>
          <p className="mt-1 text-xs">{unknown.length ? <span className="text-err-ink">Unknown source ids: {unknown.join(', ')}</span> : <span className="text-ok-ink">✓ every citation maps to a retrieved URL</span>}</p>
          <ul className="mt-2 space-y-2">
            {ids.map((id) => {
              const s = byId.get(id)
              if (!s) return null
              return (
                <li key={id} className="border border-line p-2.5">
                  <p className="font-mono text-[11px] text-accent-ink">{id} · {hostOf(s.url)}</p>
                  <a href={s.url} target="_blank" rel="noreferrer" className="mt-0.5 block text-sm text-ink underline decoration-line underline-offset-2 hover:decoration-ink">{s.title || s.url}</a>
                  {s.snippet && <p className="mt-1 text-xs text-muted">{s.snippet}</p>}
                  <p className="mt-1.5 font-mono text-[10.5px] text-muted">Text: {s.extracted_by} · Retrieved {retrieved ? fmtDate(retrieved) : '–'} · Published: not available</p>
                </li>
              )
            })}
          </ul>
        </li>
      </ol>
    </Panel>
  )
}

// Bipartite graph: claims (left) → sources (right), edges coloured and dashed by claim status.
function EvidenceGraph({ claims, sources, open, onOpen }: { claims: Claim[]; sources: Source[]; open: number | null; onOpen: (i: number | null) => void }) {
  const used = sources.filter((s) => claims.some((c) => c.source_ids?.includes(s.id)))
  if (!claims.length || !used.length) return <Empty>No claim-to-source links.</Empty>
  const rowH = 22
  const H = Math.max(claims.length, used.length) * rowH + 20
  const W = 760, lx = 120, rx = W - 170
  const cy = (i: number) => 14 + i * ((H - 28) / Math.max(1, claims.length - 1 || 1))
  const sy = (i: number) => 14 + i * ((H - 28) / Math.max(1, used.length - 1 || 1))
  // Theme colours (theme-dark.css): the light values are the ones used before; dark mode lightens them.
  const color = { verified: 'var(--graph-verified)', partial: 'var(--graph-partial)', contradicted: 'var(--graph-contradicted)', not_found: 'var(--graph-not-found)', unchecked: 'var(--graph-unchecked)' } as const
  const dash = { verified: undefined, partial: '5 3', contradicted: undefined, not_found: '2 3', unchecked: '1 4' } as const
  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto min-w-[640px] w-full" role="img" aria-label={`Graph of ${claims.length} claims linked to ${used.length} sources`}>
        {claims.map((c, i) =>
          (c.source_ids ?? []).map((id) => {
            const j = used.findIndex((s) => s.id === id)
            if (j < 0) return null
            const on = open === null || open === i
            return <path key={`${i}-${id}`} d={`M${lx},${cy(i)} C${(lx + rx) / 2},${cy(i)} ${(lx + rx) / 2},${sy(j)} ${rx},${sy(j)}`} fill="none" style={{ stroke: color[c.status] }} strokeDasharray={dash[c.status]} strokeWidth={open === i ? 2 : 1.1} opacity={on ? 0.9 : 0.12} />
          }),
        )}
        {claims.map((c, i) => (
          <g key={i} onClick={() => onOpen(open === i ? null : i)} className="cursor-pointer" role="button" tabIndex={0} onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && onOpen(open === i ? null : i)} aria-label={`Claim C${i + 1}, ${c.status.replace('_', ' ')}`}>
            <rect x={lx - 108} y={cy(i) - 8} width={104} height={16} style={{ fill: open === i ? 'var(--graph-node-active)' : 'var(--graph-node)', stroke: 'var(--graph-node-stroke)' }} />
            <text x={lx - 100} y={cy(i) + 4} fontFamily="JetBrains Mono, monospace" fontSize="9" style={{ fill: open === i ? 'var(--graph-node-active-ink)' : 'var(--graph-node-ink)' }}>C{i + 1} · {c.status.replace('_', ' ')}</text>
            <circle cx={lx} cy={cy(i)} r={3.5} style={{ fill: color[c.status] }} />
          </g>
        ))}
        {used.map((s, j) => (
          <g key={s.id}>
            <circle cx={rx} cy={sy(j)} r={3.5} style={{ fill: 'var(--graph-source)' }} />
            <text x={rx + 8} y={sy(j) + 4} fontFamily="JetBrains Mono, monospace" fontSize="9" style={{ fill: 'var(--graph-label)' }}>{s.id} · {hostOf(s.url).slice(0, 22)}</text>
          </g>
        ))}
      </svg>
      <p className="mt-1 text-[11px] text-muted">Line style shows status: solid verified · long dash partial · short dash not found · dotted unchecked.</p>
    </div>
  )
}
