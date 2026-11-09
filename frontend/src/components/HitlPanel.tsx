// HitlPanel: human approval of the plan (approve / edit each sub-question / reject) or of
// Hermes' gap queries (approve all / edit as lines / skip). Keys: Enter approves, E toggles edit.

import { useEffect, useRef, useState } from 'react'
import type { ResumeAction } from '../api'
import { Confirm } from '../ui'

type Props =
  | { mode: 'plan'; items: string[]; onAnswer: (action: ResumeAction, payload?: { sub_questions?: string[] }) => void }
  | { mode: 'gaps'; items: string[]; onAnswer: (action: ResumeAction, payload?: { gaps?: string[] }) => void }

// Typing in a field must not trigger shortcuts; on a focused button or link, Enter already
// activates that control natively, so the shortcut stays out of the way there too.
const isTyping = (t: EventTarget | null) =>
  t instanceof HTMLElement && (['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].includes(t.tagName) || t.isContentEditable)

export default function HitlPanel(props: Props) {
  const { mode, items } = props
  const [editing, setEditing] = useState(false)
  const [drafts, setDrafts] = useState<string[]>(items) // plan: one input per sub-question
  const [text, setText] = useState(items.join('\n')) // gaps: one query per line

  const edited = (): string[] =>
    (mode === 'plan' ? drafts : text.split('\n')).map((s) => s.trim()).filter(Boolean)
  const changed = () => JSON.stringify(edited()) !== JSON.stringify(items)

  // Edits are sent whenever the list differs from the original, even after "Done editing".
  const approve = () => {
    const list = edited()
    if (list.length === 0) return // nothing left to approve
    if (props.mode === 'plan') props.onAnswer('approve', changed() ? { sub_questions: list } : undefined)
    else props.onAnswer('approve', changed() ? { gaps: list } : undefined)
  }
  const shown = changed() ? edited() : items
  // Rejecting the plan cancels the run, so it asks first; skipping gap queries does not.
  const [confirming, setConfirming] = useState(false)
  const dismiss = () => (mode === 'plan' ? setConfirming(true) : props.onAnswer('skip'))

  // Keyboard: Enter = approve, E = toggle edit. Ignored while typing in a field.
  const approveRef = useRef(approve)
  approveRef.current = approve
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.ctrlKey || ev.metaKey || ev.altKey || isTyping(ev.target)) return
      if (ev.key === 'Enter') {
        ev.preventDefault()
        approveRef.current()
      } else if (ev.key === 'e' || ev.key === 'E') {
        ev.preventDefault()
        setEditing((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const secondary = 'rounded-sm border border-line px-3 py-1.5 font-mono text-xs text-ink hover:border-line'

  return (
    <section
      aria-label={mode === 'plan' ? 'Approve the plan' : 'Approve the gap queries'}
      className="sticky bottom-3 z-10 rounded-sm border border-accent/50 bg-surface p-4 shadow-[0_6px_20px_-10px_rgb(17_19_24/0.35)]"
    >
      <h2 className="font-display text-sm font-semibold tracking-wide text-accent-ink">
        {mode === 'plan' ? 'APPROVE PLAN' : 'APPROVE GAP QUERIES'}
      </h2>
      <p className="mt-0.5 text-xs text-muted">
        {mode === 'plan' ? 'Athena searches these sub-questions next.' : 'Hermes found gaps. These queries run in the next round.'}
      </p>

      {mode === 'gaps' && editing ? (
        <textarea
          aria-label="Gap queries, one per line"
          value={text}
          rows={Math.max(3, items.length + 1)}
          onChange={(e) => setText(e.target.value)}
          className="mt-3 w-full resize-y rounded-sm border border-line bg-surface-2 px-2.5 py-2 font-mono text-xs text-ink focus:border-accent/70 focus:outline-none"
        />
      ) : (
        <ol className="mt-3 space-y-1.5 text-sm">
          {(mode === 'plan' && editing ? drafts : shown).map((item, i) => (
            <li key={i} className="flex gap-2">
              <span className="mt-0.5 w-5 shrink-0 text-right font-mono text-xs text-muted">{i + 1}</span>
              {mode === 'plan' && editing ? (
                <input
                  aria-label={`Sub-question ${i + 1}`}
                  value={item}
                  onChange={(e) => setDrafts(drafts.map((d, j) => (j === i ? e.target.value : d)))}
                  className="min-w-0 flex-1 rounded-sm border border-line bg-surface-2 px-2 py-1 text-sm text-ink focus:border-accent/70 focus:outline-none"
                />
              ) : (
                <span className="min-w-0 break-words text-ink">{item}</span>
              )}
            </li>
          ))}
        </ol>
      )}

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <button
          onClick={approve}
          className="rounded-sm bg-accent-ink px-3 py-1.5 font-display text-xs font-semibold tracking-[0.1em] text-white hover:bg-ink"
        >
          {mode === 'plan' ? 'APPROVE PLAN' : 'APPROVE ALL'}
        </button>
        <button onClick={() => setEditing((v) => !v)} className={secondary} aria-pressed={editing}>
          {editing ? 'Done editing' : 'Edit'}
        </button>
        <button onClick={dismiss} className={secondary}>
          {mode === 'plan' ? 'Reject' : 'Skip'}
        </button>
        <span className="ml-auto font-mono text-[11px] text-muted">
          <kbd>Enter</kbd> approve · <kbd>E</kbd> edit
        </span>
      </div>
      <Confirm
        open={confirming}
        title="Reject the plan and stop this run?"
        body="The run ends without searching or writing a report. This cannot be undone; start a new run to try again."
        confirmLabel="Reject and stop"
        onCancel={() => setConfirming(false)}
        onConfirm={() => {
          setConfirming(false)
          props.onAnswer('reject')
        }}
      />
    </section>
  )
}
