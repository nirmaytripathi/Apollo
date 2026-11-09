// Hash router: #/claims, optionally with ?run=<id> so a link opens a specific run
// (the "shareable research link"). No dependency; back button and reloads keep the page.
// Existing URLs (#/dashboard ... #/reports) are unchanged.

import { useEffect, useState } from 'react'

export const PAGES = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'new', label: 'New Research' },
  { key: 'analytics', label: 'Analytics' },
  { key: 'claims', label: 'Claims & Evidence' },
  { key: 'evidence', label: 'Evidence Explorer' },
  { key: 'contradictions', label: 'Contradictions' },
  { key: 'sources', label: 'Source Intelligence' },
  { key: 'cost', label: 'Cost & Execution' },
  { key: 'verification', label: 'Verification' },
  { key: 'reports', label: 'Final Reports' },
] as const

// Pages reached from a flow rather than the sidebar (launching a run opens the live report).
export const FLOW_PAGES = [{ key: 'live', label: 'Live Report' }] as const

export type PageKey = (typeof PAGES)[number]['key'] | (typeof FLOW_PAGES)[number]['key']

export const pageLabel = (k: PageKey) => [...PAGES, ...FLOW_PAGES].find((p) => p.key === k)?.label ?? ''

const isPage = (k: string): k is PageKey => PAGES.some((p) => p.key === k) || FLOW_PAGES.some((p) => p.key === k)

function parse(): { page: PageKey; run: string | null } {
  const [path = '', query = ''] = window.location.hash.replace(/^#\/?/, '').split('?')
  return { page: isPage(path) ? path : 'dashboard', run: new URLSearchParams(query).get('run') }
}

export const runFromUrl = () => parse().run

export const go = (page: PageKey) => {
  window.location.hash = `/${page}`
}

/** Absolute link that opens `page` with `runId` selected. */
export const shareUrl = (page: PageKey, runId: string) =>
  `${window.location.origin}${window.location.pathname}#/${page}?run=${encodeURIComponent(runId)}`

/** The token of a public read-only report link (#/shared/<token>), or null. */
export const sharedToken = (): string | null => {
  const m = window.location.hash.match(/^#\/shared\/([A-Za-z0-9_-]+)/)
  return m?.[1] ?? null
}

/** Absolute public link to a shared report: opens without signing in. */
export const sharedUrl = (token: string) => `${window.location.origin}${window.location.pathname}#/shared/${token}`

export function useRoute(): PageKey {
  const [page, setPage] = useState<PageKey>(() => parse().page)
  useEffect(() => {
    const on = () => {
      setPage(parse().page)
      window.scrollTo(0, 0)
    }
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return page
}
