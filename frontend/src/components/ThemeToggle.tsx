// Theme toggle, used in the dashboard top bar and on the sign-in screen (one component, one theme state:
// theme.ts, localStorage "apollo-theme"). Shows the theme it switches TO: a moon + "Dark" in the light theme,
// a sun + "Light" in the dark theme. The word hides on phones. Only the theme changes, so no page or form
// state resets.

import { useTheme } from '../theme'
import { btn } from '../ui'

export default function ThemeToggle({ className = '' }: { className?: string }) {
  const { theme, toggle } = useTheme()
  const dark = theme === 'dark'
  const label = dark ? 'Switch to light theme' : 'Switch to dark theme'
  return (
    <button type="button" onClick={toggle} aria-pressed={dark} aria-label={label} title={label} className={`${btn.secondary} theme-toggle ${className}`}>
      <svg viewBox="0 0 16 16" className="h-4 w-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        {dark ? (
          <>
            <circle cx="8" cy="8" r="3" fill="currentColor" fillOpacity="0.25" />
            <path d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1" />
          </>
        ) : (
          <path d="M13.5 9.6A5.75 5.75 0 0 1 6.4 2.5a5.75 5.75 0 1 0 7.1 7.1z" />
        )}
      </svg>
      <span className="hidden sm:inline" aria-hidden="true">{dark ? 'Light' : 'Dark'}</span>
    </button>
  )
}
