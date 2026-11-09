// Research command form: objective (a question, or a YouTube link to read and fact-check), research depth (= max research rounds, which the backend
// caps at 2) and human approval. It starts empty: no sample topics, so nothing looks like an
// earlier user's search. Attached documents (PDF, .txt, .md) are uploaded and read as soon as they are picked,
// so a file the server cannot read says why before the run starts. A YouTube video longer than the backend's
// limit (35 min) is refused with a pop-up before the run starts; its length comes from the browser's YouTube player.

import { useEffect, useRef, useState } from 'react'
import { uploadDocument, type UploadedDocument } from '../api'
import { useStore } from '../store'
import { btn, field } from '../ui'
import { minutesText, videoSeconds, youtubeId } from '../youtube'

const VIDEO_MAX_SECONDS = 2100 // used until /api/system answers (its limits.video_max_seconds)

const MAX_DOCUMENTS = 3 // the backend's DOCUMENT_MAX_FILES
const ACCEPT = '.pdf,.txt,.md'
type Attachment = { key: string; name: string } & (
  | { state: 'reading' }
  | { state: 'ready'; doc: UploadedDocument }
  | { state: 'failed'; error: string }
)

// The backend enforces the project's hard limit of 2 research rounds (it rejects 3 with a 422).
export const MAX_ROUNDS_OPTIONS = [1, 2] as const
// Same links the backend reads (tools/video.py): watch, shorts, live, embed and youtu.be.
const YOUTUBE = /(?:youtube\.com\/(?:watch\?\S*v=|shorts\/|live\/|embed\/)|youtu\.be\/)[\w-]{11}/i

const DEPTH = [
  { rounds: 1, label: 'Focused', hint: '1 search round' },
  { rounds: 2, label: 'Deep', hint: '2 rounds: Hermes can order follow-up searches' },
] as const

type Props = {
  busy: boolean
  onStart: (topic: string, maxRounds: number, hitl: boolean, documents: string[]) => void
  topic?: string
  onTopic?: (t: string) => void
}

export default function TopicForm({ busy, onStart, topic: outer, onTopic }: Props) {
  const [own, setOwn] = useState('')
  const topic = outer ?? own
  const setTopic = onTopic ?? setOwn
  const [maxRounds, setMaxRounds] = useState<number>(2)
  const [hitl, setHitl] = useState(false)
  const [files, setFiles] = useState<Attachment[]>([])
  const picker = useRef<HTMLInputElement>(null)
  const reading = files.some((f) => f.state === 'reading')
  const ready = topic.trim().length >= 3 && !reading
  const video = YOUTUBE.test(topic)
  const room = MAX_DOCUMENTS - files.filter((f) => f.state !== 'failed').length

  const attach = (picked: FileList | null) => {
    for (const file of Array.from(picked ?? []).slice(0, Math.max(0, room))) {
      const key = `${file.name}-${file.size}-${Date.now()}`
      setFiles((fs) => [...fs, { key, name: file.name, state: 'reading' }])
      uploadDocument(file).then(
        (doc) => setFiles((fs) => fs.map((f) => (f.key === key ? { key, name: doc.name, state: 'ready', doc } : f))),
        (e: unknown) => setFiles((fs) => fs.map((f) => (f.key === key
          ? { key, name: file.name, state: 'failed', error: e instanceof Error ? e.message : 'Could not read this file.' } : f))),
      )
    }
    if (picker.current) picker.current.value = '' // picking the same file again still triggers onChange
  }
  const documentIds = files.flatMap((f) => (f.state === 'ready' ? [f.doc.id] : []))

  const { system } = useStore()
  const limit = system?.limits?.video_max_seconds || VIDEO_MAX_SECONDS
  const [checking, setChecking] = useState(false)
  const [tooLong, setTooLong] = useState<number | null>(null) // the refused video's length, while the pop-up is open

  const submit = async () => {
    if (!ready || busy || checking) return
    const id = youtubeId(topic)
    if (id) {
      setChecking(true)
      const seconds = await videoSeconds(id)
      setChecking(false)
      // Length unknown (player blocked or offline): let the run start; the backend still stops a long video.
      if (seconds !== null && seconds > limit + 30) {
        setTooLong(seconds)
        return
      }
    }
    onStart(topic.trim(), maxRounds, hitl, documentIds)
  }

  return (
    <form
      className="neo-panel @container border border-line bg-surface"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      {tooLong !== null && <TooLongDialog seconds={tooLong} limit={limit} onClose={() => setTooLong(null)} />}
      <div className="p-4">
        <label htmlFor="topic" className="label">Research objective</label>
        <textarea
          id="topic"
          rows={3}
          value={topic}
          disabled={busy}
          onChange={(e) => setTopic(e.target.value)}
          onKeyDown={(e) => {
            // Enter starts the research (the form's own submit: same checks as the button); Shift+Enter is
            // a new line, and Enter while an input method is composing a character stays with it.
            if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing) return
            e.preventDefault()
            e.currentTarget.form?.requestSubmit()
          }}
          aria-describedby="topic-hint"
          placeholder="Type your research question, or paste a YouTube link (add a question after it if you like)"
          className={`${field} mt-1.5 resize-none font-display text-[15px]`}
        />
        {video ? (
          <p className="feed-in mt-1.5 border-l-2 border-accent-strong bg-accent/[0.06] px-2.5 py-1.5 text-xs text-ink">
            <span className="font-medium">YouTube video detected.</span> Videos must be under {Math.round(limit / 60)} minutes, and a YouTube link may take a longer time than expected.
          </p>
        ) : (
          <p id="topic-hint" className="mt-1 text-[11px] text-muted">3–300 characters. Press Enter to start (Shift+Enter for a new line). The report uses retrieved sources only; unsupported points say "not found in sources".</p>
        )}

      </div>

      <div className="grid gap-px border-t border-line bg-hair @xl:grid-cols-2">
        <div className="bg-surface p-4">
          <p id="depth-label" className="label mb-1.5">Research depth</p>
          <div role="radiogroup" aria-labelledby="depth-label" className="flex gap-2">
            {DEPTH.map((o) => (
              <label key={o.rounds} className={`depth-card flex flex-1 cursor-pointer flex-col px-2.5 py-1.5 ${maxRounds === o.rounds ? 'depth-card-selected' : ''} ${busy ? 'opacity-60' : ''}`}>
                <span className="flex items-center gap-2 text-sm text-ink">
                  <input type="radio" name="depth" value={o.rounds} checked={maxRounds === o.rounds} disabled={busy} onChange={() => setMaxRounds(o.rounds)} className="accent-[#10203b]" />
                  {o.label}
                </span>
                <span className="text-[11px] text-muted">{o.hint}</span>
              </label>
            ))}
          </div>
        </div>
        <div className="bg-surface p-4">
          <p className="label mb-1.5">Human approval</p>
          <label className="flex items-start gap-2 text-sm text-ink">
            <input type="checkbox" checked={hitl} disabled={busy} onChange={(e) => setHitl(e.target.checked)} className="mt-0.5 h-4 w-4 accent-[#3b82f6]" />
            <span>Approve the plan and follow-up queries before they run<span className="block text-[11px] text-muted">Off: the run proceeds automatically.</span></span>
          </label>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3 border-t border-line p-4">
        <input ref={picker} type="file" accept={ACCEPT} multiple hidden onChange={(e) => attach(e.target.files)} />
        <button type="button" disabled={busy || room <= 0} className={btn.secondary} aria-describedby="attach-note"
          onClick={() => picker.current?.click()}>
          Attach document
        </button>
        <span id="attach-note" className="text-[11px] text-muted">
          {room > 0 ? `PDF, .txt or .md · up to ${MAX_DOCUMENTS} files, 10 MB each. Cited and verified like web sources.` : `${MAX_DOCUMENTS} documents attached (the most per run).`}
        </span>
        {files.length > 0 && (
          <ul className="flex w-full flex-wrap gap-2" aria-label="Attached documents">
            {files.map((f) => (
              <li key={f.key} className={`feed-in state-fade flex max-w-full items-center gap-2 border px-2.5 py-1.5 text-xs ${
                f.state === 'failed' ? 'border-err/50 bg-err/[0.06]' : f.state === 'ready' ? 'border-ok/40 bg-ok/[0.05]' : 'border-line'}`}>
                <span className="min-w-0">
                  <span className="block truncate font-medium text-ink" title={f.name}>{f.name}</span>
                  <span className={`block ${f.state === 'failed' ? 'text-err-ink' : 'text-muted'}`}>
                    {f.state === 'reading' ? 'Reading…'
                      : f.state === 'failed' ? f.error
                      : `${f.doc.pages ? `${f.doc.pages} pages` : 'text file'} · ${Math.round(f.doc.chars / 1000)}k characters read${
                          f.doc.truncated ? ` · cut${f.doc.read_to_page ? ` after page ${f.doc.read_to_page}` : ''} (length limit)` : ''}`}
                  </span>
                </span>
                {f.state !== 'reading' && (
                  <button type="button" disabled={busy} aria-label={`Remove ${f.name}`} className="ml-1 text-muted hover:text-ink"
                    onClick={() => setFiles((fs) => fs.filter((x) => x.key !== f.key))}>✕</button>
                )}
              </li>
            ))}
          </ul>
        )}
        <button type="submit" disabled={busy || !ready || checking} className={`${btn.primary} ml-auto min-w-[180px]`}>
          {busy ? 'Research in progress' : checking ? 'Checking video length…' : <>Initiate research <span className="nudge" aria-hidden="true">→</span></>}
        </button>
      </div>
    </form>
  )
}

function TooLongDialog({ seconds, limit, onClose }: { seconds: number; limit: number; onClose: () => void }) {
  const ok = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    ok.current?.focus()
    const on = (e: KeyboardEvent) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', on)
    return () => window.removeEventListener('keydown', on)
  }, [onClose])
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button type="button" className="absolute inset-0 bg-ink/30" aria-label="Close" tabIndex={-1} onClick={onClose} />
      <div role="alertdialog" aria-modal="true" aria-labelledby="too-long-title" aria-describedby="too-long-text"
        className="neo-panel relative w-full max-w-[420px] border border-line bg-surface p-5">
        <p className="label">YouTube video</p>
        <h2 id="too-long-title" className="mt-1.5 font-display text-lg font-semibold text-ink">This video is too long</h2>
        <p id="too-long-text" className="mt-2 text-sm text-ink">
          It is {minutesText(seconds)} long. The video must be under {Math.round(limit / 60)} minutes for Apollo to read all of it.
          Please use a shorter video, or a shorter part of this one.
        </p>
        <div className="mt-4 flex justify-end">
          <button ref={ok} type="button" onClick={onClose} className={btn.primary}>OK</button>
        </div>
      </div>
    </div>
  )
}
