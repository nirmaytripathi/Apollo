// Pure helpers that turn data the backend already returns into labelled facts:
// - classifyErrors: the run's non-fatal error lines (state.errors) grouped by what they mean
// - sourceCategory: a DOMAIN HEURISTIC for source type (never presented as a credibility score)
// Nothing here invents values; anything unknown stays "unclassified".

import { hostOf } from './ui'

export type ErrorKind =
  | 'blocked'        // HTTP 401/402/403/451: paywall, login or bot block
  | 'fetch_failed'   // other download failures (timeouts, 404, TLS)
  | 'non_html'       // PDF or other non-HTML page: not extracted (no PDF/OCR support)
  | 'no_main_text'   // page fetched but no article text (often JavaScript-rendered)
  | 'no_text'        // neither extraction nor the search provider returned text
  | 'search'         // a search provider failed or returned nothing
  | 'uncited_dropped'// a written sentence had no valid citation and was removed
  | 'bad_citation'   // a citation to a source the writer was not given was removed
  | 'duplicate'      // a duplicate sentence was removed from the report
  | 'quota'          // an LLM quota ran out; the verifier switched model
  | 'llm'            // an LLM call failed or returned invalid JSON
  | 'other'

export interface ClassifiedError { kind: ErrorKind; text: string; url?: string }

const URL_RE = /(https?:\/\/[^\s'")]+)/

export function classifyError(text: string): ClassifiedError {
  const url = text.match(URL_RE)?.[1]
  const k = (kind: ErrorKind): ClassifiedError => ({ kind, text, url })
  if (/^extract failed/.test(text)) return k(/\b(401|402|403|451)\b/.test(text) ? 'blocked' : 'fetch_failed')
  if (/^extract skipped non-HTML/.test(text)) return k('non_html')
  if (/^extract found no main text/.test(text)) return k('no_main_text')
  if (/^no page text for/.test(text)) return k('no_text')
  if (/^search\[/.test(text)) return k('search')
  if (/dropped uncited sentence/.test(text)) return k('uncited_dropped')
  if (/removed citation\(s\) to ids not in its sources/.test(text)) return k('bad_citation')
  if (/removed duplicate sentence/.test(text)) return k('duplicate')
  if (/quota exhausted/.test(text)) return k('quota')
  if (/LLM call failed|invalid (plan )?JSON/.test(text)) return k('llm')
  return k('other')
}

export function classifyErrors(errors: string[] = []): Record<ErrorKind, ClassifiedError[]> {
  const out = {} as Record<ErrorKind, ClassifiedError[]>
  for (const e of errors) {
    const c = classifyError(e)
    ;(out[c.kind] ??= []).push(c)
  }
  return out
}

export const ERROR_LABEL: Record<ErrorKind, string> = {
  blocked: 'Blocked or paywalled (HTTP 401/402/403/451)',
  fetch_failed: 'Download failed',
  non_html: 'PDF / non-HTML page not extracted',
  no_main_text: 'No main text (likely JavaScript-rendered)',
  no_text: 'No text from page or search provider',
  search: 'Search provider failure or empty result',
  uncited_dropped: 'Uncited sentence removed',
  bad_citation: 'Invalid citation removed',
  duplicate: 'Duplicate sentence removed',
  quota: 'LLM quota exhausted, verifier switched model',
  llm: 'LLM call failed or invalid output',
  other: 'Other',
}

// ------------------------------------------------------------------ source categories

export type SourceCategory = 'Academic' | 'Government' | 'News' | 'Industry & company' | 'Blogs & forums' | 'Unclassified'
export const CATEGORIES: SourceCategory[] = ['Academic', 'Government', 'News', 'Industry & company', 'Blogs & forums', 'Unclassified']

const ACADEMIC = /(\.edu$|\.ac\.[a-z]{2}$|\.edu\.[a-z]{2}$|arxiv\.org|doi\.org|springer|sciencedirect|nature\.com|wiley|researchgate|semanticscholar|ncbi\.nlm\.nih\.gov|frontiersin\.org|mdpi\.com|tandfonline|jstor|ssrn\.com|cgiar\.org|icrisat|ifpri)/
const GOVERNMENT = /(\.gov$|\.gov\.[a-z]{2}$|\.nic\.in$|\.gouv\.|europa\.eu$|\.int$|worldbank\.org|fao\.org|un\.org)/
const NEWS = /(thehindu|indianexpress|timesofindia|hindustantimes|ndtv|livemint|economictimes|business-standard|reuters|bbc\.|theguardian|nytimes|bloomberg|cnbc|news\.|finance\.yahoo|investing\.com|financialexpress|downtoearth|thewire|scroll\.in|theprint)/
const BLOGS = /(medium\.com|substack|blogspot|wordpress|reddit\.com|quora\.com|linkedin\.com|facebook\.com|x\.com|twitter\.com|youtube\.com|tumblr)/

/** Heuristic from the domain name only. It says what KIND of site this is, not whether it is right. */
export function sourceCategory(url: string): SourceCategory {
  const h = hostOf(url).toLowerCase()
  if (ACADEMIC.test(h)) return 'Academic'
  if (GOVERNMENT.test(h)) return 'Government'
  if (NEWS.test(h)) return 'News'
  if (BLOGS.test(h)) return 'Blogs & forums'
  if (/\.(com|co|io|ai|biz)$/.test(h)) return 'Industry & company'
  return 'Unclassified'
}

// ------------------------------------------------------------------ contradiction check status

export type ConflictCheck = 'checked' | 'partial' | 'not_checked' | 'unknown'

/** Was the contradiction search actually done? "0 conflicts" only means "none found" when 'checked'. */
export function conflictCheck(h?: { reviews?: number; reviews_failed?: number } | null): ConflictCheck {
  if (!h || h.reviews === undefined || h.reviews_failed === undefined) return 'unknown'
  if (h.reviews === 0 || h.reviews_failed >= h.reviews) return 'not_checked'
  return h.reviews_failed > 0 ? 'partial' : 'checked'
}

export const CONFLICT_CHECK_TEXT: Record<ConflictCheck, string> = {
  checked: 'Hermes compared the sources in every review round.',
  partial: 'Some Hermes reviews failed, so conflicts may be missing for those rounds.',
  not_checked: 'Not checked: every Hermes review failed (LLM error), so contradictions were never looked for. 0 does not mean the sources agree.',
  unknown: 'Review status unknown for this run.',
}
