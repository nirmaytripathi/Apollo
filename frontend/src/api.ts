// Typed fetch wrappers for the Apollo backend contract, and the types for
// events, sources, claims and the report. Fields the UI reads are optional where
// the backend might omit them; derive.ts and the components supply fallbacks.

// ------------------------------------------------------------------ events

export type EventType =
  | 'video' // the YouTube video in the topic was read (its transcript is source S1)
  | 'documents' // the attached documents were read (each is a source for every section)
  | 'plan'
  | 'search_started'
  | 'search_done'
  | 'search_results' // live: raw hits for one query, before pages are read and numbered
  | 'search_failed'
  | 'review'
  | 'gaps_pending'
  | 'section_draft' // live: a section's text while the LLM writes it (raw, before citation checks)
  | 'section_written'
  | 'report_written' // the full report text, before claim verification
  | 'claim_checked' // live: one more claim verified (progress)
  | 'paused' // the user paused the run; it waits after its current step
  | 'resumed' // the user resumed it: the same run carries on
  | 'verification'
  | 'quality' // research-quality checks run before completion
  | 'contradictions'
  | 'report_ready'
  | 'warning' // non-terminal problem the run survived (backend extension, see README)
  | 'error'
  | 'complete'

export interface EventDataMap {
  video: VideoInfo
  documents: { documents?: { id: string; name: string; pages?: number; truncated?: boolean }[] }
  plan: { sub_questions?: string[]; awaiting_approval?: boolean }
  search_started: { round?: number; index?: number; total?: number; query?: string }
  search_done: { round?: number; query?: string; result_count?: number; sources?: { id: string; title?: string; url: string }[] }
  search_results: { query?: string; results?: { title?: string; url: string }[] }
  search_failed: { round?: number; query?: string; reason?: string }
  review: { round?: number; complete?: boolean; gaps?: string[] }
  gaps_pending: { round?: number; gaps?: string[] }
  section_draft: { index?: number; text?: string }
  section_written: { section?: string; index?: number; citations?: number; not_found?: boolean; partly_not_found?: boolean; write_failed?: boolean; text?: string }
  report_written: { markdown?: string }
  claim_checked: { done?: number; total?: number; status?: string }
  paused: Record<string, never>
  resumed: { paused_seconds?: number }
  verification: { verified?: number; partial?: number; contradicted?: number; not_found?: number; unchecked?: number }
  quality: { overall?: string; checks?: QualityCheck[] }
  contradictions: { count?: number; reviews?: number; reviews_failed?: number }
  report_ready: Record<string, never>
  warning: { message?: string; details?: string[] }
  error: { message?: string }
  complete: { status?: 'complete' | 'failed'; stop_reason?: string }
}

/** A known event; `data` is narrowed by `type`. */
export type RunEvent = { [K in EventType]: { type: K; data: EventDataMap[K]; ts: number } }[EventType]

/** Anything else the stream might send; shown with a neutral fallback, never a crash. */
export type UnknownEvent = { type: string; data: Record<string, unknown>; ts: number }

export type AnyEvent = RunEvent | UnknownEvent

const KNOWN = new Set<string>([
  'video', 'documents', 'plan', 'search_started', 'search_results', 'search_done', 'search_failed', 'review', 'gaps_pending', 'section_draft', 'section_written', 'report_written', 'claim_checked', 'paused', 'resumed',
  'verification', 'quality', 'contradictions', 'report_ready', 'warning', 'error', 'complete',
])

export const isKnown = (e: AnyEvent): e is RunEvent => KNOWN.has(e.type)

/** Parse one SSE message defensively: bad JSON or odd shapes become an UnknownEvent. */
export function parseEvent(raw: string): AnyEvent {
  try {
    const v: unknown = JSON.parse(raw)
    if (v && typeof v === 'object') {
      const o = v as { type?: unknown; data?: unknown; ts?: unknown }
      return {
        type: typeof o.type === 'string' ? o.type : 'unknown',
        data: o.data && typeof o.data === 'object' ? (o.data as Record<string, unknown>) : {},
        ts: typeof o.ts === 'number' ? o.ts : Date.now(),
      } as AnyEvent
    }
  } catch {
    /* fall through */
  }
  return { type: 'unknown', data: {}, ts: Date.now() }
}

/** ts may be epoch seconds or milliseconds. */
export const toMs = (ts: number) => (ts < 1e12 ? ts * 1000 : ts)

// ------------------------------------------------------------------ REST types

/** A YouTube video given in the topic. Its transcript is source S1, shared by every section. */
export interface VideoInfo {
  id?: string
  url?: string
  title?: string
  channel?: string
  extracted_by?: 'youtube_captions' | 'gemini_transcript'
  language?: string
  seconds?: number // transcript covers the video up to here
  truncated?: boolean // the transcript was cut at the backend's length limit
}

/** A document read by POST /api/documents, ready to attach to a run. */
export interface UploadedDocument {
  id: string
  name: string
  kind: 'pdf' | 'text'
  pages: number // 0 for a text file
  chars: number
  truncated: boolean // text beyond the backend's limit was cut
  read_to_page: number | null // with truncated: the last page read
}

/** An attached document has no web address; the backend gives it a document:// stand-in that is never a link. */
export const isAttached = (url?: string) => (url ?? '').startsWith('document://')

/** Link to a moment in a YouTube video. */
export const videoAt = (url: string, seconds: number) => `${url}${url.includes('?') ? '&' : '?'}t=${Math.max(0, Math.floor(seconds))}s`

export type ClaimStatus = 'verified' | 'partial' | 'contradicted' | 'not_found' | 'unchecked'

export interface Source {
  id: string
  title?: string
  url: string
  snippet?: string
  section?: number // 0: the shared video, used by every section
  status?: string
  question?: string
  extracted_by?: string // "trafilatura" | "tavily_raw" | "youtube_captions" | "gemini_transcript" | "snippet only"
  kind?: string // "primary" (original research / official) | "secondary" (rule-based from the URL) | "video" | "document"; "" in older runs
  pages?: number | null // attached PDFs only
  published?: string // the page's own publication date (YYYY[-MM[-DD]]); "" = the page states none
  retrieved_at?: number | null // research date: when Apollo read the page (epoch seconds); null in older runs
  cited?: boolean
  claims?: Partial<Record<ClaimStatus, number>>
}

export interface Claim {
  claim: string
  status: ClaimStatus
  evidence?: string
  source_ids?: string[]
  similarity?: number // cosine similarity of the claim to its best-matching source sentence (0-1)
}

// ---- Evidence Explorer (/api/runs/{id}/evidence): each claim split into parts, judged on numbered passages

export type PartVerdict = 'supported' | 'partial' | 'contradicted' | 'unsupported'

export interface ClaimPart {
  text: string
  verdict: PartVerdict
  passage: string // deciding passage label, e.g. "S3.2" ("" when unsupported)
  source_id: string
  quote: string // exact words from that passage, checked by the backend to be in the source text
  note: string // why the backend lowered the verifier's verdict, if it did
}

export interface Passage {
  label: string // "S3.2": second passage taken from source S3
  source_id: string
  text: string
  before: string // neighbouring sentences in the source text
  after: string
  similarity: number // embedding similarity to the claim, 0-1 (not a probability)
  position: number | null // where it starts in the extracted text, 0 = start
  heading: string // nearest heading above it in the extracted text
  start?: number | null // video transcripts only: the second this passage is spoken
  page?: number | null // attached PDFs only: the page this passage is on
}

export interface EvidenceClaim {
  n: number
  claim: string // as written in the report, with [S#] citations
  text: string // without citations
  status: ClaimStatus
  source_ids: string[]
  similarity: number
  best_match: string
  evidence: string
  parts: ClaimPart[]
  passages: Passage[]
}

export interface Evidence {
  legacy: boolean // verified before claims had parts and passages: only best_match is known
  claims: EvidenceClaim[]
  sources: Source[]
  contradictions: Contradiction[]
}

export interface Contradiction {
  source_a: string
  source_b: string
  claim: string
}

export interface SectionInfo {
  index: number
  question: string
  citations: number
  not_found: boolean // no cited answer at all
  partly_not_found?: boolean // answered, but some point was not in the sources
  write_failed?: boolean // every LLM failed: the section was not written (not a "not found")
  source_ids: string[]
}

export interface Report {
  topic?: string
  status?: string
  stop_reason?: string
  markdown: string
  references?: { id: string; title: string; url: string }[] // sources actually cited
  sources: Source[]
  verification: Claim[]
  contradictions?: Contradiction[]
  contradictions_html_path: string | null
  plan?: string[]
  rounds?: number
  errors?: string[]
  sections?: SectionInfo[]
  review_health?: ReviewHealth
  citation_check?: { citations: number; unique_cited: number; dangling: string[]; uncited_sources: number; sources: number }
  quality?: QualityCheck[] // research-quality checks run before completion (empty in older runs)
  quality_overall?: '' | 'passed' | 'passed_with_warnings' | 'failed'
  duplicates?: { url: string; duplicate_of: string }[]
  irrelevant?: { url: string; relevance: number }[] // pages dropped as not about their sub-question
  video?: VideoInfo // {} unless the topic was a YouTube link
}

export interface QualityCheck {
  id: string
  label: string
  status: 'pass' | 'warn' | 'fail'
  detail: string
}

/** Hermes reviews run / failed. Contradictions are only searched for in reviews that succeeded. */
export interface ReviewHealth {
  reviews: number
  reviews_failed: number
}

export interface Usage {
  llm_calls: number
  llm_failed: number
  prompt_tokens: number
  completion_tokens: number
  llm_seconds: number
  searches: number
  searches_failed: number
  search_seconds: number
}

export interface RunSummary {
  run_id: string
  topic: string
  status: 'running' | 'awaiting_input' | 'complete' | 'failed' | 'rejected'
  hitl: boolean
  max_rounds: number
  created_at: number
  finished_at: number | null
  rounds: number
  stop_reason: string | null
  sources: number
  claims: Record<ClaimStatus, number>
  contradictions: number
  reviews?: number
  reviews_failed?: number
  has_report: boolean
  usage: Usage
}

export interface UsageRow {
  name: string
  calls: number
  failed: number
  seconds: number
  prompt_tokens: number
  completion_tokens: number
  cost_usd?: number
}

export interface MeterCall {
  kind: 'llm' | 'search'
  ok: boolean
  seconds: number
  node: string
  ts: number
  provider?: string
  model?: string
  prompt_tokens?: number
  completion_tokens?: number
  query?: string
  results?: number
  error?: string
}

export interface Metrics {
  summary: Usage
  by_node: UsageRow[]
  by_model: UsageRow[]
  calls: MeterCall[]
  stages: { stage: string; start: number; ms: number }[]
  cost_usd: number | null
  wall_ms: number
}

export interface SystemInfo {
  keys: Record<string, boolean>
  llm: { provider: string; groq_model: string; gemini_model: string; verify_chain: string[] }
  limits: Record<string, number>
  checks: {
    env_example_committed: boolean
    env_gitignored: boolean
    env_tracked_by_git: boolean | null
    cors_origins: string[]
    demo_switch: number
    database?: 'postgres' | 'sqlite'
  }
  secret_scan: { ran: boolean; files: number; findings: { file: string; line: number; kind: string; match: string }[] }
}

export interface DemoTopic {
  id: string
  title: string
  topic: string
  note?: string | null
}

export type ResumeAction = 'approve' | 'edit' | 'reject' | 'skip'

// ------------------------------------------------------------------ fetch wrappers

/** Fired on window when any API call gets 401 (session missing or expired). */
export const SIGNED_OUT_EVENT = 'apollo:signed-out'

/** An API failure; `status` is the HTTP status, or 0 when the server could not be reached. */
export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message)
  }
}

/** 0 = unreachable, 502/503/504 = the server is restarting or overloaded: worth retrying quietly. */
export const isTransient = (e: unknown) => e instanceof ApiError && [0, 502, 503, 504].includes(e.status)

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  let resp: Response
  try {
    resp = await fetch(url, init)
  } catch {
    throw new ApiError('Cannot reach the server. Check your connection; retrying.', 0)
  }
  if (resp.status === 401 && !url.startsWith('/api/auth/')) {
    // The session expired or was revoked: tell the app to show the sign-in screen.
    window.dispatchEvent(new Event(SIGNED_OUT_EVENT))
  }
  if (!resp.ok) {
    const body: unknown = await resp.json().catch(() => null)
    const detail = (body as { detail?: unknown } | null)?.detail
    const msg =
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail) // FastAPI validation errors: [{loc, msg}]
          ? detail.map((d: { msg?: string; loc?: unknown[] }) => `${(d.loc ?? []).slice(1).join('.')} ${d.msg ?? ''}`.trim()).join('; ')
          : [502, 503, 504].includes(resp.status)
            ? `The server is restarting or busy (${resp.status}); retrying.`
            : `Request failed (${resp.status})`
    throw new ApiError(msg, resp.status)
  }
  return resp.json() as Promise<T>
}

const post = (body: unknown): RequestInit => ({
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export const startRun = (topic: string, maxRounds: number, hitl: boolean, documents: string[] = []) =>
  request<{ run_id: string }>('/api/runs', post({ topic, max_rounds: maxRounds, hitl, documents }))

/** Upload one file; the server reads it now, so a problem (scanned PDF, wrong type) shows before the run starts. */
export const uploadDocument = (file: File) => {
  const body = new FormData()
  body.append('file', file)
  return request<UploadedDocument>('/api/documents', { method: 'POST', body })
}

export const resumeRun = (runId: string, action: ResumeAction, payload?: { sub_questions?: string[]; gaps?: string[] }) =>
  request<{ ok: boolean }>(`/api/runs/${runId}/resume`, post(payload ? { action, payload } : { action }))

/** Stop: the run ends at its next safe point, with stop_reason "stopped_by_user" (it can then be restarted). */
export const stopRun = (runId: string) => request<{ ok: boolean }>(`/api/runs/${runId}/stop`, post({}))

/** Pause: the run waits after its current step; continueRun carries the same run on from there. */
export const pauseRun = (runId: string) => request<{ ok: boolean }>(`/api/runs/${runId}/pause`, post({}))
export const continueRun = (runId: string) => request<{ ok: boolean }>(`/api/runs/${runId}/continue`, post({}))

/** A new run with an edited topic and the stopped run's depth, approval setting and attached documents. */
export const restartRun = (runId: string, topic: string) =>
  request<{ run_id: string; documents_kept: number; documents_lost: boolean }>(`/api/runs/${runId}/restart`, post({ topic }))

export const getReport = (runId: string) => request<Report>(`/api/runs/${runId}/report`)
export const getEvidence = (runId: string) => request<Evidence>(`/api/runs/${runId}/evidence`)

export const listRuns = () => request<RunSummary[]>('/api/runs')
export const getRunStatus = (runId: string) => request<{ status: string; events: number }>(`/api/runs/${runId}`)
export const getMetrics = (runId: string) => request<Metrics>(`/api/runs/${runId}/metrics`)
export const getSystem = () => request<SystemInfo>('/api/system')

export const getDemoTopics = () => request<DemoTopic[]>('/api/demo-topics')

export const eventsUrl = (runId: string) => `/api/runs/${runId}/events`
export const reportDownloadUrl = (runId: string) => `/api/runs/${runId}/report.md`
export const reportDocxUrl = (runId: string) => `/api/runs/${runId}/report.docx`

// ------------------------------------------------------------------ public share links
// The owner turns a read-only link on or off; anyone with the link can read that one report without signing in.

export const getShare = (runId: string) => request<{ token: string | null }>(`/api/runs/${runId}/share`)
export const createShare = (runId: string) => request<{ token: string }>(`/api/runs/${runId}/share`, post({}))
export const deleteShare = (runId: string) => request<{ ok: boolean }>(`/api/runs/${runId}/share`, { method: 'DELETE' })

/** A shared report: only what the report shows (no errors, logs or usage). */
export interface SharedReport {
  topic: string
  created_at: number
  status?: string
  stop_reason?: string
  markdown: string
  references?: { id: string; title: string; url: string }[]
  sources: Source[]
  verification: Claim[]
  plan?: string[]
}

export const getShared = (token: string) => request<SharedReport>(`/api/shared/${encodeURIComponent(token)}`)
export const sharedMdUrl = (token: string) => `/api/shared/${encodeURIComponent(token)}/report.md`
export const sharedDocxUrl = (token: string) => `/api/shared/${encodeURIComponent(token)}/report.docx`

// ------------------------------------------------------------------ accounts
// The session is a JWT in an HttpOnly cookie set by the backend; this code never sees the token.

export interface AuthUser {
  id: string
  email: string
  name: string
  created_at: number
}

export const authMe = () => request<{ user: AuthUser }>('/api/auth/me').then((r) => r.user)
export const authLogin = (email: string, password: string) =>
  request<{ user: AuthUser }>('/api/auth/login', post({ email, password })).then((r) => r.user)
export const authRegister = (name: string, email: string, password: string) =>
  request<{ user: AuthUser }>('/api/auth/register', post({ name, email, password })).then((r) => r.user)
export const authLogout = () => request<{ ok: boolean }>('/api/auth/logout', post({}))
