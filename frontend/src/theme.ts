// Light / dark theme: <html data-theme="light|dark">, saved in localStorage "apollo-theme".
// index.html applies the saved value before the first paint (no flash); this hook keeps React in step.
// Storage can be unavailable (private windows, blocked site data): the theme then lasts only this visit.

import { useCallback, useEffect, useState } from 'react'

export const THEME_KEY = 'apollo-theme'
export type Theme = 'light' | 'dark'

function initial(): Theme {
  if (document.documentElement.dataset.theme === 'dark') return 'dark'
  try {
    return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

export function useTheme(): { theme: Theme; toggle: () => void } {
  const [theme, setTheme] = useState<Theme>(initial)

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* not kept after a reload */
    }
  }, [theme])

  const toggle = useCallback(() => setTheme((t) => (t === 'dark' ? 'light' : 'dark')), [])
  return { theme, toggle }
}
