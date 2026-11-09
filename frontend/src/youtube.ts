// YouTube links in the research form: the video id, and the video's length read in the user's own browser
// through YouTube's IFrame player API (the server cannot ask: YouTube blocks cloud servers). The player is
// hidden, never plays, and uses the no-cookie host. Any failure gives null and the backend decides instead.

// Same link forms the backend reads (tools/video.py): watch, shorts, live, embed and youtu.be.
const ID = /(?:youtube\.com\/(?:watch\?(?:[^\s#]*&)?v=|shorts\/|live\/|embed\/)|youtu\.be\/)([\w-]{11})/i

export const youtubeId = (text: string): string | null => text.match(ID)?.[1] ?? null

type Player = { getDuration: () => number; destroy: () => void }
type YT = { Player: new (el: HTMLElement, opts: object) => Player }
declare global {
  interface Window {
    YT?: YT
    onYouTubeIframeAPIReady?: () => void
  }
}

let api: Promise<YT> | null = null

function loadApi(): Promise<YT> {
  if (window.YT?.Player) return Promise.resolve(window.YT)
  api ??= new Promise<YT>((resolve, reject) => {
    const before = window.onYouTubeIframeAPIReady
    window.onYouTubeIframeAPIReady = () => {
      before?.()
      if (window.YT) resolve(window.YT)
    }
    const s = document.createElement('script')
    s.src = 'https://www.youtube.com/iframe_api'
    s.async = true
    s.onerror = () => {
      api = null // a later check may try again
      reject(new Error('YouTube player API could not load'))
    }
    document.head.appendChild(s)
  })
  return api
}

const known = new Map<string, number>()

/** The video's length in seconds, or null when it cannot be read within `waitMs` (blocked, offline, private video). */
export async function videoSeconds(id: string, waitMs = 8000): Promise<number | null> {
  const cached = known.get(id)
  if (cached) return cached
  let timer = 0
  const timeout = new Promise<null>((resolve) => { timer = window.setTimeout(() => resolve(null), waitMs) })
  const read = loadApi().then((yt) => new Promise<number | null>((resolve) => {
    const host = document.createElement('div')
    host.setAttribute('aria-hidden', 'true')
    host.style.cssText = 'position:fixed;width:1px;height:1px;left:-9999px;top:0;opacity:0;pointer-events:none'
    const slot = document.createElement('div')
    host.appendChild(slot)
    document.body.appendChild(host)
    let player: Player | null = null
    const done = (v: number | null) => {
      try { player?.destroy() } catch { /* already gone */ }
      host.remove()
      resolve(v)
    }
    player = new yt.Player(slot, {
      videoId: id,
      host: 'https://www.youtube-nocookie.com',
      playerVars: { autoplay: 0, controls: 0 },
      events: {
        onReady: () => {
          const d = player?.getDuration() ?? 0
          done(d > 0 ? d : null)
        },
        onError: () => done(null),
      },
    })
    window.setTimeout(() => done(null), waitMs) // the player never answered: clean it up
  })).catch(() => null)
  const seconds = await Promise.race([read, timeout])
  window.clearTimeout(timer)
  if (seconds) known.set(id, seconds)
  return seconds
}

/** "2 h 4 min", "41 min". */
export const minutesText = (seconds: number): string => {
  const m = Math.round(seconds / 60)
  return m >= 60 ? `${Math.floor(m / 60)} h ${m % 60} min` : `${m} min`
}
