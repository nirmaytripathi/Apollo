// Stop & Edit, while a run works (Live Report). One button, two ways out:
//   Stop & Edit: the run PAUSES after its current step (POST /pause; nothing is cut short) and an
//     editor opens with the topic. A run already waiting for approval is not paused: it is waiting anyway.
//   Resume: no edit after all. The SAME run carries on with its old topic (POST /continue): plan, sources
//     and progress kept, nothing starts over.
//   Update & restart research: the paused run ends (POST /stop) and a new run starts with the edited topic
//     and the old run's depth, approval setting and documents (POST /restart).
// A run that already ended as "stopped by you" (e.g. left paused 30 minutes) shows the editor without Resume.

import { useEffect, useState } from 'react'
import { useStore } from '../store'
import { btn, field } from '../ui'

const MIN = 3
const MAX = 300

export default function PauseEditPanel() {
  const { busy, stopping, pausing, openEdit, editing, closeEdit, restartEdited, selected, d } = useStore()
  const ended = !busy && d.stopReason === 'stopped_by_user'
  const original = selected?.topic ?? ''
  const [topic, setTopic] = useState(original)
  const [sending, setSending] = useState(false)

  // A different run selected (or the run list caught up): start the editor from that run's topic.
  useEffect(() => setTopic(original), [original, selected?.run_id])

  if (!busy && !ended) return null

  if (busy && !editing) {
    return (
      // The button comes first (left): the floating game panel sits on the right and could cover it there.
      <div className="mb-5 flex flex-wrap items-center gap-3 border border-line bg-surface px-4 py-3" role="region" aria-label="Stop and edit">
        <button type="button" onClick={openEdit} disabled={stopping} className={btn.secondary}>
          {stopping ? 'Stopping…' : 'Stop & Edit'}
        </button>
        <p className="min-w-0 flex-1 text-sm text-ink">
          Need to change the question? The research pauses while you edit; resume it if you change your mind.
        </p>
      </div>
    )
  }

  const approval = d.planPending || !!d.gapsPending
  const status = !busy
    ? 'Stopped by you'
    : d.paused
      ? 'Paused · nothing is lost'
      : pausing
        ? 'Pausing after the current step…'
        : approval
          ? 'Waiting for your approval'
          : 'Paused'
  const clean = topic.trim()
  const valid = clean.length >= MIN && clean.length <= MAX
  const same = clean === original.trim()

  return (
    <form
      className="neo-panel mb-5 border border-line bg-surface px-4 py-4"
      aria-label="Edit topic"
      onSubmit={async (e) => {
        e.preventDefault()
        if (!valid || sending) return
        setSending(true)
        await restartEdited(clean)
        setSending(false)
      }}
    >
      <p className="label" aria-live="polite">{status}</p>
      <label htmlFor="restart-topic" className="mt-2 block text-sm font-medium text-ink">Research topic</label>
      <textarea
        id="restart-topic"
        value={topic}
        onChange={(e) => setTopic(e.target.value)}
        rows={2}
        maxLength={MAX}
        className={`${field} mt-1.5 resize-y`}
      />
      <p className="mt-1.5 text-xs text-muted">
        {busy
          ? 'Resume carries this run on with its original topic, from where it paused. Update & restart starts a new run with the topic above'
          : 'This run has ended. Update & restart starts a new run with the topic above'}
        {' '}(same depth, approval setting and attached documents){same ? ', from the beginning' : ''}.
        {!valid && clean.length > 0 && ` The topic needs ${MIN}–${MAX} characters.`}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-3">
        {busy && (
          <button type="button" onClick={() => { setTopic(original); void closeEdit() }} disabled={sending || stopping} className={btn.primary}>
            Resume
          </button>
        )}
        <button type="submit" disabled={!valid || sending} className={busy ? btn.secondary : btn.primary}>
          {sending ? 'Restarting…' : 'Update & restart research'}
        </button>
      </div>
    </form>
  )
}
