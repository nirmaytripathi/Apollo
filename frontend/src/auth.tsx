// Accounts on the client: AuthGate asks the backend who is signed in (GET /api/auth/me) and shows
// either the sign-in / create-account screen or the app. Any API call that gets 401 fires
// SIGNED_OUT_EVENT, which returns the user to the sign-in screen. The JWT itself stays in an
// HttpOnly cookie that this code cannot read.

import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError, SIGNED_OUT_EVENT, authLogin, authLogout, authMe, authRegister, isTransient, type AuthUser } from './api'
import ThemeToggle from './components/ThemeToggle'
import { btn, field } from './ui'

interface AuthState {
  user: AuthUser
  signOut: () => Promise<void>
}

const Ctx = createContext<AuthState | null>(null)
const WAKE_WAIT_MS = 90_000 // how long to keep retrying while the server wakes up

export function useAuth(): AuthState {
  const s = useContext(Ctx)
  if (!s) throw new Error('useAuth outside AuthGate')
  return s
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null | undefined>(undefined) // undefined = still checking
  const [offline, setOffline] = useState<string | null>(null)

  const [waking, setWaking] = useState(false)

  useEffect(() => {
    // A free host may be asleep or restarting (502/503, ~1 minute): keep asking instead of
    // showing the sign-in screen to someone who is already signed in.
    let live = true
    let timer: number | undefined
    const started = Date.now()
    const check = () =>
      authMe()
        .then((u) => live && setUser(u))
        .catch((e: unknown) => {
          if (!live) return
          if (isTransient(e) && Date.now() - started < WAKE_WAIT_MS) {
            setWaking(true)
            timer = window.setTimeout(check, 3000)
            return
          }
          if (isTransient(e)) setOffline(e instanceof Error ? e.message : "Can't reach the server.")
          setUser(null)
        })
    void check()
    const out = () => setUser(null)
    window.addEventListener(SIGNED_OUT_EVENT, out)
    return () => {
      live = false
      window.clearTimeout(timer)
      window.removeEventListener(SIGNED_OUT_EVENT, out)
    }
  }, [])

  if (user === undefined) {
    return (
      <div className="flex min-h-screen flex-col items-center justify-center gap-1 bg-app font-mono text-xs text-muted" role="status">
        <span>{waking ? 'Waking up the server…' : 'Checking your session…'}</span>
        {waking && <span>The free server sleeps when idle; this can take up to a minute.</span>}
      </div>
    )
  }
  if (user === null) return <AuthScreen onSignedIn={(u) => { setOffline(null); setUser(u) }} notice={offline} />

  // Signed out only when the server confirms (the session cookie is cleared there). A failure throws, so the
  // caller can say why while the user stays signed in; a 401 means the session had already ended.
  const signOut = async () => {
    try {
      await authLogout()
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) throw e
    }
    setUser(null)
  }
  // key: a different account gets a fresh app state (no previous user's run in memory)
  return <Ctx.Provider value={{ user, signOut }}><div key={user.id}>{children}</div></Ctx.Provider>
}

type Mode = 'signin' | 'register'

function AuthScreen({ onSignedIn, notice }: { onSignedIn: (u: AuthUser) => void; notice: string | null }) {
  const [mode, setMode] = useState<Mode>('signin')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [show, setShow] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(notice)
  // Set at once (state only updates on the next render): a double click or Enter + click sends one request.
  const inFlight = useRef(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (inFlight.current) return
    setError(null)
    if (mode === 'register' && password.length < 8) return setError('Password must be at least 8 characters.')
    inFlight.current = true
    setBusy(true)
    try {
      onSignedIn(mode === 'signin' ? await authLogin(email, password) : await authRegister(name, email, password))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Something went wrong. Try again.')
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  const switchTo = (m: Mode) => {
    setMode(m)
    setError(null)
  }

  // Layout: two columns above 900px (brand left, card right, a divider between); one column below, brand on top.
  // The page wears the workspace's wave background (.neo-workspace), so both themes match the dashboard.
  return (
    <div className="auth-screen neo-workspace relative flex min-h-screen flex-col">
      <div className="absolute right-4 top-4 z-10 sm:right-6 sm:top-5">
        <ThemeToggle />
      </div>

      <div className="mx-auto grid w-full max-w-[1120px] flex-1 items-center gap-10 px-4 pb-10 pt-20 sm:px-6 min-[901px]:grid-cols-[minmax(0,1fr)_minmax(0,470px)] min-[901px]:gap-0 min-[901px]:py-16">
        <section aria-labelledby="auth-statement" className="min-w-0 min-[901px]:border-r min-[901px]:border-line min-[901px]:pr-14">
          <div className="flex items-center gap-4">
            <span className="neo-brand-mark auth-mark" aria-hidden="true">A</span>
            <div>
              <p className="font-display text-[26px] font-bold leading-none tracking-[0.2em] text-ink">APOLLO</p>
              <p className="label mt-2">Multi-Agent Research</p>
            </div>
          </div>
          <h1 id="auth-statement" className="auth-statement mt-10 font-display font-semibold leading-[0.98] tracking-[-0.04em] text-ink max-[900px]:mt-7">
            Evidence, not guesswork.
          </h1>
          <p className="mt-4 max-w-[46ch] text-[15px] leading-relaxed text-muted">
            Research that proves every line and shows the evidence behind every claim.
          </p>
          <ol className="auth-benefits mt-8 grid gap-px border border-line bg-line max-[900px]:mt-6">
            {['Multi-agent deep research', 'Claim-level verification', 'Evidence-linked reports'].map((b, i) => (
              <li key={b} className="flex items-center gap-3 bg-surface px-4 py-3">
                <span className="font-mono text-[11px] text-accent-ink">{String(i + 1).padStart(2, '0')}</span>
                <span className="text-sm font-medium text-ink">{b}</span>
              </li>
            ))}
          </ol>
        </section>

        <section aria-label="Account" className="min-w-0 min-[901px]:pl-14">
          <div className="auth-card w-full max-w-[470px] max-[900px]:mx-auto">
            <div className="flex items-center gap-2 border-b border-hair px-6 py-3">
              <span className="h-1.5 w-1.5 rounded-full bg-ok" aria-hidden="true" />
              <p className="label">Secure session</p>
            </div>
            <div className="px-6 pb-6 pt-5">
              <div role="tablist" aria-label="Account" className="grid grid-cols-2 border-2 border-ink">
                {(['signin', 'register'] as const).map((m) => (
                  <button
                    key={m}
                    type="button"
                    role="tab"
                    id={`tab-${m}`}
                    aria-selected={mode === m}
                    aria-controls="auth-form"
                    onClick={() => switchTo(m)}
                    className={`auth-tab min-h-11 font-mono text-[11.5px] uppercase tracking-wide ${mode === m ? 'auth-tab-selected' : 'text-muted hover:bg-surface-2 hover:text-ink'}`}
                  >
                    {m === 'signin' ? 'Sign in' : 'Create account'}
                  </button>
                ))}
              </div>

          <form id="auth-form" role="tabpanel" aria-labelledby={`tab-${mode}`} aria-busy={busy} onSubmit={submit} className="mt-5 space-y-4" noValidate>
            <div>
              <h2 className="font-display text-[22px] font-semibold text-ink">{mode === 'signin' ? 'Welcome back' : 'Create your account'}</h2>
              <p className="mt-1 text-sm text-muted">{mode === 'signin' ? 'Sign in to continue your research.' : 'Your research sessions stay private to your account.'}</p>
            </div>
            {mode === 'register' && (
              <div>
                <label htmlFor="name" className="label">Name</label>
                <input id="name" autoComplete="name" required value={name} onChange={(e) => setName(e.target.value)} className={`${field} auth-field mt-1.5`} />
              </div>
            )}
            <div>
              <label htmlFor="email" className="label">Email</label>
              <input id="email" type="email" autoComplete="email" required value={email} onChange={(e) => setEmail(e.target.value)} className={`${field} auth-field mt-1.5`} />
            </div>
            <div>
              <div className="flex items-baseline justify-between">
                <label htmlFor="password" className="label">Password</label>
                <button type="button" onClick={() => setShow((v) => !v)} className="font-mono text-[11px] text-accent-ink hover:underline" aria-controls="password">
                  {show ? 'Hide' : 'Show'}
                </button>
              </div>
              <input
                id="password"
                type={show ? 'text' : 'password'}
                autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
                required
                minLength={mode === 'register' ? 8 : undefined}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                aria-describedby={mode === 'register' ? 'pw-hint' : undefined}
                className={`${field} auth-field mt-1.5`}
              />
              {mode === 'register' && <p id="pw-hint" className="mt-1 text-[11px] text-muted">At least 8 characters.</p>}
            </div>
            {error && <p role="alert" className="border border-err/50 bg-err/10 px-3 py-2 text-sm text-err-ink">{error}</p>}
            <button type="submit" disabled={busy || !email || !password || (mode === 'register' && !name.trim())} className={`${btn.primary} auth-submit w-full`}>
              {busy ? 'Please wait…' : mode === 'signin' ? 'Sign in' : 'Create account'}
            </button>
            <p className="text-center text-sm text-muted">
              {mode === 'signin' ? 'New to APOLLO? ' : 'Already have an account? '}
              <button type="button" onClick={() => switchTo(mode === 'signin' ? 'register' : 'signin')} className="text-accent-ink hover:underline">
                {mode === 'signin' ? 'Create an account' : 'Sign in'}
              </button>
            </p>
          </form>
            </div>
          </div>
        </section>
      </div>
    </div>
  )
}
