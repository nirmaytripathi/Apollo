// ProgressFeed: the run's event log, one tagged line per event with a monospace
// timestamp relative to run start. Warnings amber, gaps blue, auto-scrolls to newest.

import { useEffect, useRef } from 'react'
import { isKnown, toMs, type AnyEvent } from '../api'

type Tone = 'normal' | 'warn' | 'gap' | 'good'
type Line = { tag: string; text: string; tone: Tone; details?: string[] }

const STOP: Record<string, string> = {
  hermes_complete: 'Hermes judged the sources sufficient',
  max_rounds_reached: 'round limit reached',
  timeout: 'time limit reached',
  no_follow_up_queries: 'no new follow-up queries',
  gaps_skipped_by_user: 'gap queries skipped',
  plan_rejected_by_user: 'plan rejected',
  stopped_by_user: 'stopped by you',
  error: 'stopped by an error',
  interrupted: 'interrupted by a server restart',
  video_unreadable: 'the video could not be read',
  llm_quota: 'every AI model is out of quota',
}

const n = (v: unknown, fallback = 0) => (typeof v === 'number' && Number.isFinite(v) ? v : fallback)

function describe(e: AnyEvent): Line {
  if (!isKnown(e)) return { tag: 'EVENT', text: e.type, tone: 'normal' }
  switch (e.type) {
    case 'video': {
      const how = e.data.extracted_by === 'gemini_transcript' ? 'transcribed by Gemini (no usable captions)' : 'its captions'
      const upTo = e.data.seconds ? ` · transcript to ${clock(e.data.seconds * 1000)}` : ''
      return { tag: 'VIDEO', text: `read ${e.data.title ?? 'the video'} from ${how}${upTo}${e.data.truncated ? ' (cut: video too long)' : ''}`, tone: 'good' }
    }
    case 'documents': {
      const docs = e.data.documents ?? []
      return {
        tag: 'DOCS',
        text: `read ${docs.length} attached document${docs.length === 1 ? '' : 's'}`,
        tone: 'good',
        details: docs.map((d) => `${d.id} ${d.name}${d.pages ? ` · ${d.pages} pages` : ''}${d.truncated ? ' · cut at the length limit' : ''}`),
      }
    }
    case 'plan': {
      const qs = e.data.sub_questions ?? []
      return e.data.awaiting_approval
        ? { tag: 'PLAN', text: `${qs.length} sub-questions await your approval`, tone: 'gap' }
        : { tag: 'PLAN', text: `${qs.length} sub-questions`, tone: 'normal', details: qs }
    }
    case 'search_started':
      return { tag: 'SEARCH', text: `[r${n(e.data.round, 1)} ${n(e.data.index, 1)}/${n(e.data.total, 1)}] ${e.data.query ?? ''}`, tone: 'normal' }
    case 'search_done':
      return { tag: 'FOUND', text: `${n(e.data.result_count)} new · ${e.data.query ?? ''}`, tone: 'good' }
    case 'search_failed':
      return { tag: 'WARN', text: `no results · ${e.data.query ?? ''}`, tone: 'warn', details: e.data.reason ? [e.data.reason] : undefined }
    case 'review': {
      const gaps = e.data.gaps ?? []
      // Only complete=true means "covered"; complete=false without gaps means Hermes gave no usable verdict.
      if (e.data.complete) return { tag: 'REVIEW', text: `round ${n(e.data.round, 1)}: sources cover the plan`, tone: 'normal' }
      if (gaps.length === 0) return { tag: 'REVIEW', text: `round ${n(e.data.round, 1)}: not complete, but no follow-up queries`, tone: 'normal' }
      return { tag: 'GAP', text: `round ${n(e.data.round, 1)}: ${gaps.length} gap${gaps.length === 1 ? '' : 's'} found`, tone: 'gap', details: gaps }
    }
    case 'gaps_pending':
      return { tag: 'GAP', text: `${(e.data.gaps ?? []).length} follow-up queries await your approval`, tone: 'gap' }
    case 'section_written': {
      const c = n(e.data.citations)
      if (e.data.write_failed) return { tag: 'WARN', text: `${e.data.section ?? 'section'} · not written: language model unavailable`, tone: 'warn' }
      const tail = c ? `${c} citation${c === 1 ? '' : 's'}` : 'not found in sources'
      return { tag: 'WRITE', text: `${e.data.section ?? 'section'} · ${tail}`, tone: 'normal' }
    }
    case 'verification': {
      const u = n(e.data.unchecked)
      return {
        tag: 'VERIFY',
        text: `${n(e.data.verified)} verified · ${n(e.data.partial)} partial${n(e.data.contradicted) ? ` · ${n(e.data.contradicted)} contradicted` : ''} · ${n(e.data.not_found)} not found${u ? ` · ${u} unchecked` : ''}`,
        tone: 'normal',
      }
    }
    case 'quality': {
      const checks = e.data.checks ?? []
      const bad = checks.filter((c) => c.status !== 'pass')
      return {
        tag: 'QUALITY',
        text: bad.length ? `${checks.length - bad.length}/${checks.length} checks passed · ${bad.map((c) => c.label.toLowerCase()).join('; ')}` : `all ${checks.length} quality checks passed`,
        tone: bad.length ? 'warn' : 'good',
      }
    }
    case 'contradictions': {
      const c = n(e.data.count)
      const failed = n(e.data.reviews_failed)
      const reviews = e.data.reviews ?? null // absent in runs recorded before this field existed
      if (reviews !== null && (reviews === 0 || failed >= reviews))
        return { tag: 'CONFLICT', text: 'contradictions not checked: every Hermes review failed', tone: 'warn' }
      const tail = failed ? ` (${failed} of ${reviews} reviews failed)` : ''
      return { tag: 'CONFLICT', text: (c ? `${c} conflict${c === 1 ? '' : 's'} between sources` : 'no conflicts between sources') + tail, tone: failed ? 'warn' : 'normal' }
    }
    case 'search_results':
      return { tag: 'HITS', text: `${(e.data.results ?? []).length} results · ${e.data.query ?? ''}`, tone: 'normal' }
    case 'section_draft':
      return { tag: 'DRAFT', text: `section ${n(e.data.index) + 1} being written`, tone: 'normal' }
    case 'claim_checked':
      return { tag: 'VERIFY', text: `claim ${n(e.data.done)}/${n(e.data.total)} checked`, tone: 'normal' }
    case 'paused':
      return { tag: 'PAUSE', text: 'paused by you after the current step', tone: 'warn' }
    case 'resumed':
      return { tag: 'RESUME', text: `resumed after ${n(e.data.paused_seconds)} s; the same run carries on`, tone: 'good' }
    case 'report_written':
      return { tag: 'WRITE', text: 'report text ready; verifying claims', tone: 'good' }
    case 'report_ready':
      return { tag: 'READY', text: 'report ready', tone: 'good' }
    case 'warning':
      return { tag: 'WARN', text: e.data.message ?? 'warning', tone: 'warn', details: e.data.details }
    case 'error':
      return { tag: 'WARN', text: `run stopped: ${e.data.message ?? 'unknown problem'}`, tone: 'warn' }
    case 'complete':
      return {
        tag: 'DONE',
        text: `${e.data.status ?? 'finished'}${e.data.stop_reason ? ` · ${STOP[e.data.stop_reason] ?? e.data.stop_reason}` : ''}`,
        tone: e.data.status === 'failed' ? 'warn' : 'good',
      }
  }
}

const TONE: Record<Tone, { tag: string; text: string }> = {
  normal: { tag: 'text-muted', text: 'text-ink' },
  good: { tag: 'text-ok-ink', text: 'text-ink' },
  warn: { tag: 'text-warn-ink', text: 'text-warn-ink' },
  gap: { tag: 'text-accent-ink', text: 'text-ink' },
}

const clock = (ms: number) => {
  const s = Math.max(0, Math.floor(ms / 1000))
  return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}

export default function ProgressFeed({ events }: { events: AnyEvent[] }) {
  const box = useRef<HTMLOListElement>(null)

  useEffect(() => {
    const el = box.current
    if (el) el.scrollTop = el.scrollHeight // newest at the bottom, always in view
  }, [events.length])

  if (events.length === 0) return null
  const t0 = Math.min(...events.map((e) => toMs(e.ts)))
  // drafts stream several times a second and claim checks once per claim: the summary lines cover them
  const shown = events.filter((e) => e.type !== 'section_draft' && e.type !== 'claim_checked')

  return (
    <section aria-label="Event log" className="rounded-sm border border-hair bg-surface">
      <h2 className="border-b border-hair px-4 py-2.5 font-display text-sm font-semibold tracking-wide">Event log</h2>
      <ol ref={box} className="max-h-[40vh] space-y-1 overflow-y-auto px-4 py-3 font-mono text-xs" aria-live="polite">
        {shown.map((e, i) => {
          const line = describe(e)
          const tone = TONE[line.tone]
          return (
            <li key={i} className="feed-in grid grid-cols-[3.25rem_4.5rem_minmax(0,1fr)] gap-x-2 leading-relaxed">
              <span className="tabular-nums text-muted">{clock(toMs(e.ts) - t0)}</span>
              <span className={`font-medium ${tone.tag}`}>{line.tag}</span>
              <span className={`min-w-0 break-words ${tone.text}`}>
                {line.text}
                {line.details && line.details.length > 0 && (
                  <details className="mt-0.5">
                    <summary className="cursor-pointer text-muted hover:text-ink">{line.details.length} detail{line.details.length === 1 ? '' : 's'}</summary>
                    <ul className="mt-1 space-y-0.5 text-muted">
                      {line.details.map((d, j) => (
                        <li key={j} className="break-words">· {d}</li>
                      ))}
                    </ul>
                  </details>
                )}
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
