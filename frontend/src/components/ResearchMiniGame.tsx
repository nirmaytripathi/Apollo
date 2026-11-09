import { useEffect, useRef, useState } from 'react'
import { useStore } from '../store'

type Pipe = { x: number; gapY: number; passed: boolean }

const BEST_KEY = 'apollo-flappy-best'

// Browser storage can be unavailable (private windows, blocked site data): the best score is then not kept.
function readBest(): number {
  try { return Number(localStorage.getItem(BEST_KEY) ?? 0) || 0 } catch { return 0 }
}

function saveBest(value: number) {
  try { localStorage.setItem(BEST_KEY, String(value)) } catch { /* not kept this time */ }
}

// Space flaps only when the page itself has focus: in a text box (the HITL plan editor, the topic form) a space
// is typed, and on a focused button or link Space is that control's own key.
function typingOrOnControl(target: EventTarget | null): boolean {
  const el = target as HTMLElement | null
  return !!el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT', 'BUTTON', 'A'].includes(el.tagName))
}

export default function ResearchMiniGame({ onClose }: { onClose: () => void }) {
  const { busy, d } = useStore()
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const gameRef = useRef({
    birdY: 150,
    velocity: 0,
    pipes: [] as Pipe[],
    frame: 0,
    score: 0,
    running: false,
    started: false,
  })
  const [score, setScore] = useState(0)
  const [best, setBest] = useState(readBest)
  const [status, setStatus] = useState<'ready' | 'playing' | 'ended'>('ready')
  const [minimized, setMinimized] = useState(false)

  // Human approval on: the run is paused until the user answers, so the panel says so instead of "in progress".
  const waiting = d.planPending || !!d.gapsPending?.length
  const activeAgent = d.apollo === 'active' ? 'Apollo' : d.athena === 'active' ? 'Athena' : d.hermes === 'active' ? 'Hermes' : 'Apollo'
  const stage = activeAgent === 'Apollo' ? d.labels.apollo : activeAgent === 'Athena' ? d.labels.athena : d.labels.hermes
  // Real progress from the backend's events (derive.ts): searches, sections and claims done so far.
  const progress = d.progress

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const context = canvas.getContext('2d')
    if (!context) return
    const game = gameRef.current
    let animation = 0

    const draw = () => {
      const width = 320
      const height = 360
      context.clearRect(0, 0, width, height)

      const sky = context.createLinearGradient(0, 0, 0, height)
      sky.addColorStop(0, '#dff8f5')
      sky.addColorStop(1, '#eef5fb')
      context.fillStyle = sky
      context.fillRect(0, 0, width, height)

      context.globalAlpha = 0.34
      context.fillStyle = '#82dfcf'
      context.beginPath()
      context.moveTo(0, 70)
      context.bezierCurveTo(75, 25, 145, 110, 320, 42)
      context.lineTo(320, 0)
      context.lineTo(0, 0)
      context.closePath()
      context.fill()
      context.globalAlpha = 1

      context.strokeStyle = 'rgba(16,32,59,.12)'
      context.lineWidth = 1
      for (let y = 40; y < height; y += 40) {
        context.beginPath()
        context.moveTo(0, y)
        context.lineTo(width, y)
        context.stroke()
      }

      for (const pipe of game.pipes) {
        const gap = 104
        const topHeight = pipe.gapY - gap / 2
        const bottomY = pipe.gapY + gap / 2
        context.fillStyle = '#d5f8f1'
        context.strokeStyle = '#10203b'
        context.lineWidth = 3
        context.fillRect(pipe.x, 0, 42, topHeight)
        context.strokeRect(pipe.x, -2, 42, topHeight + 2)
        context.fillRect(pipe.x, bottomY, 42, height - bottomY)
        context.strokeRect(pipe.x, bottomY, 42, height - bottomY + 2)
        context.fillStyle = '#82dfcf'
        context.fillRect(pipe.x - 5, topHeight - 17, 52, 17)
        context.strokeRect(pipe.x - 5, topHeight - 17, 52, 17)
        context.fillRect(pipe.x - 5, bottomY, 52, 17)
        context.strokeRect(pipe.x - 5, bottomY, 52, 17)
      }

      context.save()
      context.translate(76, game.birdY)
      context.rotate(Math.min(0.45, Math.max(-0.35, game.velocity * 0.055)))
      context.fillStyle = '#d5f8f1'
      context.strokeStyle = '#10203b'
      context.lineWidth = 3
      context.fillRect(-15, -12, 30, 24)
      context.strokeRect(-15, -12, 30, 24)
      context.fillStyle = '#10203b'
      context.fillRect(7, -5, 4, 4)
      context.fillStyle = '#82dfcf'
      context.fillRect(-20, 1, 15, 10)
      context.strokeRect(-20, 1, 15, 10)
      context.fillStyle = '#f3b92e'
      context.fillRect(15, -1, 9, 6)
      context.strokeRect(15, -1, 9, 6)
      context.restore()

      context.fillStyle = '#10203b'
      context.font = '600 20px ui-monospace, monospace'
      context.fillText(String(game.score).padStart(2, '0'), 16, 30)

      if (!game.running) {
        context.fillStyle = 'rgba(251,253,255,.9)'
        context.fillRect(52, 135, 216, 88)
        context.strokeStyle = '#10203b'
        context.lineWidth = 2
        context.strokeRect(52, 135, 216, 88)
        context.fillStyle = '#10203b'
        context.textAlign = 'center'
        context.font = '600 14px ui-sans-serif, sans-serif'
        context.fillText(game.started ? 'RUN ENDED' : 'READY TO FLY', 160, 169)
        context.font = '11px ui-monospace, monospace'
        context.fillText(game.started ? 'tap to restart' : 'space / tap to start', 160, 193)
        context.textAlign = 'left'
      }
    }

    const endGame = () => {
      if (!game.running) return
      game.running = false
      setStatus('ended')
      setBest((current) => {
        const next = Math.max(current, game.score)
        saveBest(next)
        return next
      })
    }

    const tick = () => {
      if (game.running) {
        game.frame += 1
        game.velocity += 0.29
        game.birdY += game.velocity

        if (game.frame % 115 === 0) {
          game.pipes.push({ x: 340, gapY: 105 + Math.random() * 150, passed: false })
        }

        for (const pipe of game.pipes) {
          pipe.x -= 2.25
          if (!pipe.passed && pipe.x + 42 < 61) {
            pipe.passed = true
            game.score += 1
            setScore(game.score)
          }
          const overlapsX = 91 > pipe.x && 61 < pipe.x + 42
          const gap = 104
          const outsideGap =
            game.birdY - 12 < pipe.gapY - gap / 2 ||
            game.birdY + 12 > pipe.gapY + gap / 2
          if (overlapsX && outsideGap) endGame()
        }
        game.pipes = game.pipes.filter((pipe) => pipe.x > -60)
        if (game.birdY < 12 || game.birdY > 348) endGame()
      }
      draw()
      animation = requestAnimationFrame(tick)
    }

    tick()
    return () => cancelAnimationFrame(animation)
    // Re-run when the panel is restored: minimizing unmounts the canvas, and the restored one is a new element.
    // The game itself lives in gameRef, so the score and position carry over.
  }, [minimized])

  const flap = () => {
    const game = gameRef.current
    if (!game.running) {
      Object.assign(game, {
        birdY: 150,
        velocity: -4.9,
        pipes: [],
        frame: 0,
        score: 0,
        running: true,
        started: true,
      })
      setScore(0)
      setStatus('playing')
      return
    }
    game.velocity = -5.2
  }

  useEffect(() => {
    const handleKey = (event: KeyboardEvent) => {
      if (event.code === 'Space' && !minimized && !typingOrOnControl(event.target)) {
        event.preventDefault()
        flap()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  })

  if (!busy) return null

  if (minimized) {
    return (
      <button className="research-game-minimized" onClick={() => setMinimized(false)} type="button">
        <span className="research-game-live-dot" />
        Research running
        <b>{score} pts</b>
      </button>
    )
  }

  return (
    <aside aria-label="Research waiting game" className="research-game">
      <header className="research-game-header">
        <div>
          <span className="label">While you wait / Mini game</span>
          <h2>APOLLO FLIGHT</h2>
          <p>Play until your research is ready.</p>
        </div>
        <div className="research-game-window-actions">
          <button aria-label="Minimize game" onClick={() => setMinimized(true)} type="button">—</button>
          <button aria-label="Close game" onClick={onClose} type="button">×</button>
        </div>
      </header>

      <div className="research-game-status">
        <div>
          <span><i className="research-game-live-dot" /> {d.paused ? 'Paused' : waiting ? 'Waiting for your approval' : 'Research in progress'}</span>
          <b>{progress}%</b>
        </div>
        <div className="research-game-progress"><i style={{ width: `${progress}%` }} /></div>
        <p>{waiting ? `Approve the ${d.planPending ? 'plan' : 'follow-up searches'} to continue · Round ${Math.max(1, d.round)}`
          : `${activeAgent} · ${stage || 'working'} · Round ${Math.max(1, d.round)}`}</p>
      </div>

      <button
        aria-label="Play Apollo Flight. Tap to make the bird fly."
        className="research-game-canvas"
        onClick={flap}
        type="button"
      >
        <canvas height="360" ref={canvasRef} width="320" />
      </button>

      <div className="research-game-footer">
        <div><span>Score</span><strong>{String(score).padStart(2, '0')}</strong></div>
        <div><span>Best</span><strong>{String(best).padStart(2, '0')}</strong></div>
        <button className="neo-button border border-ink bg-surface px-3 py-2 text-sm" onClick={flap} type="button">
          {status === 'ended' ? 'Restart' : status === 'ready' ? 'Start game' : 'Flap'}
        </button>
      </div>
    </aside>
  )
}
