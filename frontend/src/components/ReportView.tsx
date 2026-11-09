// ReportView: the report rendered with react-markdown. [S1] tokens become chips linking to the
// source (hover: title, snippet, URL); paragraphs matching a partial / not_found / contradicted claim get a left border.

import { Children, isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import { isAttached, reportDownloadUrl, type Claim, type Report, type Source } from '../api'

function Chip({ id, source }: { id: string; source?: Source }) {
  if (!source?.url) return <span className="font-mono text-xs text-muted">[{id}]</span>
  const chip = 'mx-0.5 rounded border border-accent/40 bg-accent/10 px-1 font-mono text-[11px] text-accent-ink no-underline hover:bg-ink hover:text-white'
  return (
    <span className="group relative inline-block align-baseline">
      {isAttached(source.url) ? (
        <span tabIndex={0} className={chip}>{id}</span> // an attached file has no web address to open
      ) : (
        <a href={source.url} target="_blank" rel="noreferrer" className={chip}>{id}</a>
      )}
      {/* opens leftwards from the chip's right edge so it stays inside the page */}
      <span
        role="tooltip"
        className="pointer-events-none absolute bottom-full right-0 z-20 mb-2 hidden w-72 max-w-[80vw] rounded-sm border border-line bg-[#11151c] p-3 text-left font-sans text-xs leading-relaxed text-muted shadow-2xl group-focus-within:block group-hover:block"
      >
        <span className="block font-semibold text-ink [overflow-wrap:anywhere]">{source.title || id}</span>
        {source.snippet && <span className="mt-1 block">{source.snippet}</span>}
        <span className="mt-1.5 block font-mono text-[11px] text-accent-ink [overflow-wrap:anywhere]">{isAttached(source.url) ? 'Document you attached' : source.url}</span>
      </span>
    </span>
  )
}

export function withChips(node: ReactNode, sources: Map<string, Source>): ReactNode {
  return Children.map(node, (child) =>
    typeof child === 'string'
      ? child.split(/(\[S\d+\])/).map((part, i) => {
          const m = part.match(/^\[(S\d+)\]$/)
          return m?.[1] ? <Chip key={i} id={m[1]} source={sources.get(m[1])} /> : part
        })
      : child,
  )
}

// react-markdown hands block components arbitrary nested ReactNode children; to compare a
// paragraph with claim text we flatten it to plain text. The props of a nested element are
// untyped here, hence the `any` (the only one in the app).
function textOf(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join('')
  if (isValidElement(node)) return textOf((node.props as any).children)
  return ''
}

const norm = (s: string) => s.replace(/\[S\d+\]/g, '').replace(/\s+/g, ' ').trim().toLowerCase()

const BORDER: Record<string, string> = {
  partial: 'border-l-2 border-warn pl-3',
  not_found: 'border-l-2 border-err pl-3',
  contradicted: 'border-l-4 border-err-ink pl-3',
}

export default function ReportView({ report, runId, toolbar = true }: { report: Report; runId: string; toolbar?: boolean }) {
  const sources = new Map((report.sources ?? []).map((s) => [s.id, s]))
  const flagged: Claim[] = (report.verification ?? []).filter((c) => c.status === 'partial' || c.status === 'not_found' || c.status === 'contradicted')

  // Best-effort: the worst flagged claim contained in the paragraph decides its border.
  const flagFor = (children: ReactNode): string => {
    const text = norm(textOf(children))
    if (!text) return ''
    const hits = flagged.filter((c) => c.claim && text.includes(norm(c.claim)))
    if (hits.some((c) => c.status === 'contradicted')) return BORDER.contradicted ?? ''
    if (hits.some((c) => c.status === 'not_found')) return BORDER.not_found ?? ''
    return hits.length ? (BORDER.partial ?? '') : ''
  }

  return (
    <article>
      {toolbar && <div className="no-print mb-5 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted">
        <a
          href={reportDownloadUrl(runId)}
          download
          className="rounded-sm bg-accent-ink px-3 py-1.5 font-display text-xs font-semibold tracking-[0.1em] text-white hover:bg-ink"
        >
          DOWNLOAD REPORT.MD
        </a>
        <span className="border-l-2 border-warn pl-2">partial support</span>
        <span className="border-l-2 border-err pl-2">not found in source</span>
        <span>Best-effort markers; the Verification tab is the source of truth.</span>
      </div>}
      <div className="max-w-[72ch] space-y-4 text-[15px] leading-7 text-ink [&_h1]:font-display [&_h1]:text-2xl [&_h1]:font-semibold [&_h1]:text-ink [&_h2]:mt-8 [&_h2]:font-display [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:text-ink [&_li]:ml-5 [&_li]:list-disc">
        <ReactMarkdown
          components={{
            p: ({ children }) => <p className={flagFor(children)}>{withChips(children, sources)}</p>,
            li: ({ children }) => <li className={flagFor(children)}>{withChips(children, sources)}</li>,
          }}
        >
          {report.markdown ?? ''}
        </ReactMarkdown>
      </div>
    </article>
  )
}
