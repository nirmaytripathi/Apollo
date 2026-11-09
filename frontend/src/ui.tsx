// APOLLO design-system primitives: panel, metric, status pill, bars, segmented control,
// confirm dialog, empty / needs-run / unavailable states, and formatters.
// Tokens live in index.css (@theme); nothing here uses raw colour values except the
// shared status palette below.

import { useEffect, useRef, type CSSProperties, type ReactNode } from 'react'
import type { ClaimStatus } from './api'
import { go } from './router'

// ------------------------------------------------------------------ layout

export function Panel({ title, label, aside, children, className = '', pad = true }: {
  title?: string; label?: string; aside?: ReactNode; children: ReactNode; className?: string; pad?: boolean
}) {
  return (
    <section className={`neo-panel min-w-0 border border-line bg-surface ${className}`}>
      {(title || aside || label) && (
        <header className="flex flex-wrap items-center justify-between gap-2 border-b border-hair px-4 py-2.5">
          <div className="min-w-0">
            {label && <p className="label">{label}</p>}
            {title && <h2 className="font-display text-[15px] font-semibold text-ink">{title}</h2>}
          </div>
          {aside && <div className="font-mono text-[11px] text-muted">{aside}</div>}
        </header>
      )}
      <div className={pad ? 'p-4' : ''}>{children}</div>
    </section>
  )
}

/** Backwards-compatible name used by older pages. */
export const Card = ({ title, aside, children, className = '' }: { title?: string; aside?: ReactNode; children: ReactNode; className?: string }) => (
  <Panel title={title} aside={aside} className={className}>{children}</Panel>
)

export function PageHeader({ kicker, title, children }: { kicker: string; title: string; children?: ReactNode }) {
  return (
    <div className="mb-5 flex flex-wrap items-end justify-between gap-3 border-b border-line pb-4">
      <div className="min-w-0">
        <p className="label">{kicker}</p>
        <h1 className="neo-page-title mt-2 font-display font-semibold leading-[0.95] tracking-[-0.045em] text-ink">{title}</h1>
      </div>
      {children && <div className="flex flex-wrap items-center gap-2">{children}</div>}
    </div>
  )
}

type Tone = 'normal' | 'good' | 'warn' | 'bad' | 'accent'
const TONE_TEXT: Record<Tone, string> = { normal: 'text-ink', good: 'text-ok-ink', warn: 'text-warn-ink', bad: 'text-err-ink', accent: 'text-accent-ink' }

export function Stat({ label, value, sub, tone = 'normal' }: { label: string; value: ReactNode; sub?: ReactNode; tone?: Tone }) {
  return (
    <div className="neo-stat min-w-0 border border-line bg-surface px-4 py-3">
      <p className="label">{label}</p>
      <p className={`mt-1.5 font-display text-[26px] font-semibold leading-none tabular-nums ${TONE_TEXT[tone]}`}>{value}</p>
      {sub && <p className="mt-1.5 truncate text-xs text-muted">{sub}</p>}
    </div>
  )
}

// ------------------------------------------------------------------ status

/** Claim statuses in display order (verify/claims.py). */
export const CLAIM_KEYS: ClaimStatus[] = ['verified', 'partial', 'contradicted', 'not_found', 'unchecked']

export const STATUS_COLOR: Record<ClaimStatus, string> = {
  verified: 'bg-ok',
  partial: 'bg-warn',
  contradicted: 'bg-err-ink',
  not_found: 'bg-err',
  unchecked: 'bg-muted/40',
}

const PILL: Record<string, string> = {
  verified: 'border-ok/50 bg-ok/10 text-ok-ink',
  complete: 'border-ok/50 bg-ok/10 text-ok-ink',
  done: 'border-ok/50 bg-ok/10 text-ok-ink',
  partial: 'border-warn/70 bg-warn/15 text-warn-ink',
  warn: 'border-warn/70 bg-warn/15 text-warn-ink',
  not_found: 'border-err/50 bg-err/10 text-err-ink',
  contradicted: 'border-err-ink bg-err-ink text-white',
  failed: 'border-err/50 bg-err/10 text-err-ink',
  running: 'border-accent bg-accent/10 text-accent-ink',
  working: 'border-accent bg-accent/10 text-accent-ink',
  awaiting_input: 'border-accent bg-accent/10 text-accent-ink',
  paused: 'border-warn/70 bg-warn/15 text-warn-ink',
  unchecked: 'border-line bg-surface-2 text-muted',
  idle: 'border-line bg-surface-2 text-muted',
  rejected: 'border-line bg-surface-2 text-muted',
  unavailable: 'border-dashed border-line bg-transparent text-muted',
}

/** Status is always written out, never colour alone. */
export function Pill({ status, label }: { status: string; label?: string }) {
  return (
    <span className={`state-fade inline-block whitespace-nowrap border px-1.5 py-[1px] font-mono text-[10.5px] uppercase tracking-wide ${PILL[status] ?? PILL.unchecked}`}>
      {label ?? status.replace(/_/g, ' ')}
    </span>
  )
}

export function StackedBar({ parts, height = 'h-2' }: { parts: { value: number; color: string; label: string }[]; height?: string }) {
  const total = parts.reduce((a, p) => a + p.value, 0)
  return (
    <div className={`flex w-full overflow-hidden bg-surface-2 ${height}`} role="img" aria-label={parts.map((p) => `${p.label} ${p.value}`).join(', ')}>
      {total > 0 && parts.map((p) => p.value > 0 && <div key={p.label} className={`${p.color} transition-[width] duration-500 ease-[var(--ease-out)] motion-reduce:transition-none`} style={{ width: `${(p.value / total) * 100}%` }} title={`${p.label}: ${p.value}`} />)}
    </div>
  )
}

export function Legend({ items }: { items: { color: string; label: string; value?: number }[] }) {
  return (
    <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted">
      {items.map((i) => (
        <span key={i.label} className="inline-flex items-center gap-1.5">
          <span className={`h-2 w-2 ${i.color}`} aria-hidden="true" />
          {i.label}
          {i.value !== undefined && <span className="font-mono text-ink">{i.value}</span>}
        </span>
      ))}
    </div>
  )
}

export function Meter({ value, max, color = 'bg-accent' }: { value: number; max: number; color?: string }) {
  return (
    <div className="h-1.5 w-full overflow-hidden bg-surface-2">
      <div className={`grow-x h-full w-full ${color}`} style={{ '--p': max > 0 ? Math.min(1, value / max) : 0 } as CSSProperties} />
    </div>
  )
}

// ------------------------------------------------------------------ controls

export const btn = {
  primary: 'neo-button neo-button-primary inline-flex items-center justify-center gap-2 border border-ink bg-accent px-3.5 py-2 font-display text-[13px] font-semibold tracking-wide text-ink disabled:cursor-not-allowed disabled:bg-surface-2 disabled:text-muted disabled:shadow-none',
  secondary: 'neo-button inline-flex items-center justify-center gap-2 border border-ink bg-surface px-3 py-1.5 text-[13px] text-ink hover:bg-workspace disabled:cursor-not-allowed disabled:text-muted disabled:hover:bg-surface',
  danger: 'neo-button inline-flex items-center justify-center gap-2 border border-err-ink bg-surface px-3 py-1.5 text-[13px] text-err-ink hover:bg-err/10 disabled:cursor-not-allowed disabled:opacity-50',
  ghost: 'inline-flex items-center gap-1.5 px-2 py-1 text-[13px] text-accent-ink underline decoration-transparent underline-offset-4 hover:decoration-accent-ink',
}

export const field = 'neo-field w-full border border-line bg-surface px-3 py-2 text-sm text-ink placeholder:text-muted focus:border-accent-strong focus:outline-none disabled:bg-surface-2 disabled:text-muted'

export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: { value: T; label: string }[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="inline-flex border border-line bg-surface">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={`px-2.5 py-1 font-mono text-[11px] uppercase tracking-wide ${value === o.value ? 'bg-ink text-white' : 'text-muted hover:bg-surface-2 hover:text-ink'}`}
        >
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** Modal confirmation for destructive actions. Escape or Cancel closes it. */
export function Confirm({ open, title, body, confirmLabel, onConfirm, onCancel }: {
  open: boolean; title: string; body: ReactNode; confirmLabel: string; onConfirm: () => void; onCancel: () => void
}) {
  const ref = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    if (!open) return
    ref.current?.focus()
    const on = (e: KeyboardEvent) => e.key === 'Escape' && onCancel()
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [open, onCancel])
  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="alertdialog" aria-modal="true" aria-labelledby="confirm-title">
      <button className="absolute inset-0 bg-ink/30" aria-label="Cancel" onClick={onCancel} />
      <div className="relative w-full max-w-md border border-line bg-surface p-5">
        <h2 id="confirm-title" className="font-display text-lg font-semibold">{title}</h2>
        <div className="mt-2 text-sm text-muted">{body}</div>
        <div className="mt-5 flex justify-end gap-2">
          <button ref={ref} className={btn.secondary} onClick={onCancel}>Cancel</button>
          <button className={btn.danger} onClick={onConfirm}>{confirmLabel}</button>
        </div>
      </div>
    </div>
  )
}

// ------------------------------------------------------------------ states

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-muted">{children}</p>
}

/** A feature the UI is ready for but the backend does not provide yet. Never shows made-up data. */
export function Unavailable({ what, need }: { what: string; need: string }) {
  return (
    <div className="border border-dashed border-line bg-workspace px-3 py-2.5">
      <p className="flex items-center gap-2 text-sm text-ink"><Pill status="unavailable" label="not available" />{what}</p>
      <p className="mt-1 text-xs text-muted">Needs: {need}</p>
    </div>
  )
}

export function NeedsRun({ what, busy }: { what: string; busy?: boolean }) {
  return (
    <Panel>
      <div className="py-10 text-center">
        <p className="text-sm text-ink">{busy ? `${what} appears when the run finishes writing its report.` : `No ${what.toLowerCase()} yet.`}</p>
        {busy ? (
          <p className="mt-2 font-mono text-xs text-muted" role="status">Run in progress…</p>
        ) : (
          <button onClick={() => go('new')} className={`${btn.primary} mt-4`}>Start a research run</button>
        )}
      </div>
    </Panel>
  )
}

// ------------------------------------------------------------------ formatters

export const fmtInt = (n: number) => n.toLocaleString('en-IN')
export const fmtSec = (s: number) => (s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(s < 10 ? 1 : 0)}s`)
export const fmtMs = (ms: number) => fmtSec(ms / 1000)
export const pct = (a: number, b: number) => (b > 0 ? `${Math.round((a / b) * 100)}%` : '–')
export const fmtTime = (ms: number) => new Date(ms).toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
export const fmtDate = (ms: number) => new Date(ms).toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' })

export function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '')
  } catch {
    return url
  }
}

export const STOP_REASON: Record<string, string> = {
  hermes_complete: 'Hermes judged the sources sufficient',
  max_rounds_reached: 'Round limit reached',
  timeout: 'Time limit reached',
  no_follow_up_queries: 'No new follow-up queries',
  gaps_skipped_by_user: 'Gap queries skipped by you',
  plan_rejected_by_user: 'Plan rejected by you',
  stopped_by_user: 'Stopped by you',
  error: 'Stopped by an error',
  interrupted: 'Interrupted by a server restart',
  video_unreadable: 'The video could not be read',
  llm_quota: 'AI model quota used up',
}

/** Download text as a file (client-side export of data already on screen). */
export function download(name: string, text: string, type = 'text/plain') {
  const url = URL.createObjectURL(new Blob([text], { type }))
  const a = Object.assign(document.createElement('a'), { href: url, download: name })
  a.click()
  URL.revokeObjectURL(url)
}

// ------------------------------------------------------------------ charts

/** Vertical bars with value labels; the same numbers are in a screen-reader table. */
export function BarChart({ data, height = 140, unit = '', label }: { data: { label: string; value: number; title?: string }[]; height?: number; unit?: string; label: string }) {
  const max = Math.max(1, ...data.map((d) => d.value))
  return (
    <figure>
      <div className="flex items-end gap-[3px] border-b border-line" style={{ height }} aria-hidden="true">
        {data.map((d, i) => (
          <div key={i} className="group relative flex h-full min-w-0 flex-1 flex-col justify-end" title={`${d.title ?? d.label}: ${d.value}${unit}`}>
            {d.value > 0 && <span className="mb-0.5 text-center font-mono text-[9.5px] text-muted">{d.value}</span>}
            <div className={`w-full ${d.value > 0 ? 'bg-accent-strong group-hover:bg-accent-ink' : 'bg-transparent'}`} style={{ height: `${(d.value / max) * (height - 18)}px` }} />
          </div>
        ))}
      </div>
      <div className="mt-1 flex gap-[3px]" aria-hidden="true">
        {data.map((d, i) => (
          <span key={i} className="min-w-0 flex-1 truncate text-center font-mono text-[9.5px] text-muted">{data.length > 16 && i % 2 ? '' : d.label}</span>
        ))}
      </div>
      <table className="sr-only">
        <caption>{label}</caption>
        <tbody>{data.map((d, i) => <tr key={i}><th>{d.title ?? d.label}</th><td>{d.value}{unit}</td></tr>)}</tbody>
      </table>
    </figure>
  )
}

/** Count items per day / ISO-week / month bucket, oldest first, filling empty buckets. */
export function bucketize(times: number[], unit: 'day' | 'week' | 'month', count: number): { label: string; title: string; value: number }[] {
  const start = (t: number) => {
    const d = new Date(t)
    d.setHours(0, 0, 0, 0)
    if (unit === 'week') d.setDate(d.getDate() - ((d.getDay() + 6) % 7))
    if (unit === 'month') d.setDate(1)
    return d
  }
  const out: { key: number; label: string; title: string; value: number }[] = []
  const cur = start(Date.now())
  for (let i = 0; i < count; i++) {
    const d = new Date(cur)
    if (unit === 'day') d.setDate(d.getDate() - i)
    if (unit === 'week') d.setDate(d.getDate() - 7 * i)
    if (unit === 'month') d.setMonth(d.getMonth() - i)
    const label = unit === 'month' ? d.toLocaleDateString('en-IN', { month: 'short' }) : d.toLocaleDateString('en-IN', { day: '2-digit', month: unit === 'day' ? undefined : 'short' })
    out.unshift({ key: d.getTime(), label, title: (unit === 'week' ? 'Week of ' : '') + d.toLocaleDateString('en-IN', { day: '2-digit', month: 'short', year: 'numeric' }), value: 0 })
  }
  for (const t of times) {
    const k = start(t).getTime()
    const b = out.find((o) => o.key === k)
    if (b) b.value += 1
  }
  return out.map(({ label, title, value }) => ({ label, title, value }))
}
