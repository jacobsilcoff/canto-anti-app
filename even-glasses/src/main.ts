/**
 * Even glasses flashcard plugin (Even Hub).
 *
 * Drives a hands-free review of your due flashcards on Even Realities G2
 * glasses. Every grade is sent to your account, so you earn XP and keep your
 * streak / daily quests in sync — the same as reviewing on the web.
 *
 * Controls (G2 TouchBar / R1 Ring):
 *   - before reveal:  tap             -> show the answer
 *   - after reveal:   tap             -> "got it"   (SRS "good")
 *                     double-tap      -> "missed it" (SRS "again")
 *   - swipe up = "got it", swipe down = "missed it" (alternative)
 *
 * Because the glasses have no speaker, audio is never used:
 *   - Non-Latin languages show romanization as the prompt on the pronunciation
 *     face and in the reveal on other faces.
 *   - Latin pronunciation faces (audio-only) are skipped automatically.
 */
import {
  waitForEvenAppBridge,
  CreateStartUpPageContainer,
  TextContainerProperty,
  TextContainerUpgrade,
  OsEventTypeList,
} from '@evenrealities/even_hub_sdk'
import type { EvenAppBridge, EvenHubEvent } from '@evenrealities/even_hub_sdk'

import { ApiClient, ApiError } from './api.js'
import type { DueCard, Quality } from './api.js'
import { buildView, playableCards } from './cards.js'
import type { CardView } from './cards.js'
import { loadConfig } from './config.js'

// ── Display constants ────────────────────────────────────────────────────────
const CONTAINER_ID = 1
const CONTAINER_NAME = 'flashcard'

// Full usable area with a small margin.
const X = 20
const Y = 10
const W = 536
const H = 268

// ── State ────────────────────────────────────────────────────────────────────
type Phase = 'prompt' | 'reveal'

let bridge: EvenAppBridge
let api: ApiClient

let queue: DueCard[] = []
let idx = 0
let phase: Phase = 'prompt'
let view: CardView | null = null
let xp = 0
let graded = 0
let busy = false

// ── Display helpers ──────────────────────────────────────────────────────────

/** Show the initial page container (must be called once). */
async function showStartup(text: string): Promise<void> {
  await bridge.createStartUpPageContainer(
    new CreateStartUpPageContainer({
      containerTotalNum: 1,
      textObject: [
        new TextContainerProperty({
          containerID: CONTAINER_ID,
          containerName: CONTAINER_NAME,
          content: text,
          xPosition: X,
          yPosition: Y,
          width: W,
          height: H,
          borderWidth: 1,
          borderColor: 8,
          borderRdaius: '4', // SDK typo is intentional
          paddingLength: 10,
          isEventCapture: 1,
        }),
      ],
    }),
  )
}

/** Update the text in the existing container (no flicker). */
async function updateText(text: string): Promise<void> {
  await bridge.textContainerUpgrade(
    new TextContainerUpgrade({
      containerID: CONTAINER_ID,
      containerName: CONTAINER_NAME,
      content: text,
    }),
  )
}

// ── Review loop ──────────────────────────────────────────────────────────────

async function begin(): Promise<void> {
  await updateText('Loading your flashcards...')

  let romanizable: Set<string>
  let cards: DueCard[]
  try {
    ;[romanizable, { cards }] = await Promise.all([
      api.romanizableLangs(),
      api.dueSession().then((s) => ({ cards: s.cards })),
    ])
  } catch (e) {
    return showError(e)
  }

  queue = playableCards(cards, romanizable)
  idx = 0
  xp = 0
  graded = 0

  if (queue.length === 0) {
    await updateText('All caught up!\n\nNo cards due right now.\n\n( tap to check again )')
    return
  }

  render()
}

function render(): void {
  const card = queue[idx]
  if (phase === 'prompt') view = buildView(card)
  const v = view!
  const progress = `${idx + 1}/${queue.length}`

  if (phase === 'prompt') {
    updateText(`${v.hint}  |  ${progress}\n\n${v.prompt}\n\n( tap to reveal )`)
  } else {
    const notes =
      card.notes && card.notes.trim() ? `\n\n${card.notes.trim()}` : ''
    updateText(
      `${v.hint}  |  ${progress}\n\n` +
        `${v.prompt}\n---\n${v.answer}${notes}\n\n` +
        `tap = got it  |  2x tap = missed`,
    )
  }
}

async function grade(quality: Quality): Promise<void> {
  const card = queue[idx]
  busy = true
  try {
    const res = await api.review(card.card_id, card.face, quality)
    xp += res.xp || 0
  } catch (e) {
    busy = false
    return showError(e)
  }
  busy = false
  graded += 1
  idx += 1
  phase = 'prompt'
  if (idx >= queue.length) return finish()
  render()
}

async function finish(): Promise<void> {
  let tail = ''
  try {
    const s = await api.streak()
    tail = `\n\nStreak: ${s.streak}   XP today: ${s.points_today}/${s.daily_goal}`
  } catch {
    /* summary is best-effort */
  }
  await updateText(
    `Session done!\n\n${graded} cards   +${xp} XP${tail}\n\n( tap to check for more )`,
  )
}

function showError(e: unknown): void {
  const msg = e instanceof ApiError ? e.message : 'Something went wrong.'
  updateText(`Flashcards\n\n${msg}`)
}

// ── Event handling ───────────────────────────────────────────────────────────

function resolveEventType(event: EvenHubEvent): number | undefined {
  if (event.textEvent) return event.textEvent.eventType
  if (event.sysEvent) return event.sysEvent.eventType
  if (event.listEvent) return event.listEvent.eventType
  return undefined
}

function onEvent(event: EvenHubEvent): void {
  if (busy) return

  const eventType = resolveEventType(event)

  // Finished screen -> any press restarts.
  if (idx >= queue.length) {
    void begin()
    return
  }

  switch (eventType) {
    // Tap -> reveal or "got it"
    case OsEventTypeList.CLICK_EVENT:
    case undefined: // SDK normalizes 0 (CLICK) to undefined
      if (phase === 'prompt') {
        phase = 'reveal'
        render()
      } else {
        void grade('good')
      }
      break

    // Double-tap -> "missed it"
    case OsEventTypeList.DOUBLE_CLICK_EVENT:
      if (phase === 'reveal') {
        void grade('again')
      } else {
        // Double-tap on prompt also reveals (forgiving)
        phase = 'reveal'
        render()
      }
      break

    // Swipe up -> "got it"
    case OsEventTypeList.SCROLL_TOP_EVENT:
      if (phase === 'reveal') {
        void grade('good')
      }
      break

    // Swipe down -> "missed it"
    case OsEventTypeList.SCROLL_BOTTOM_EVENT:
      if (phase === 'reveal') {
        void grade('again')
      }
      break
  }
}

// ── Bootstrap ────────────────────────────────────────────────────────────────

async function main(): Promise<void> {
  const cfg = loadConfig()

  bridge = await waitForEvenAppBridge()
  bridge.onEvenHubEvent(onEvent)

  if (!cfg) {
    await showStartup(
      'Setup needed\n\n' +
        'Open this plugin on your phone\n' +
        'and enter your site URL + API token.\n\n' +
        '(Generate a token in the site\'s\n' +
        'Settings > Even glasses)',
    )
    // Also show a config form in the phone WebView.
    showPhoneConfigUI()
    return
  }

  api = new ApiClient(cfg.baseUrl, cfg.token)
  await showStartup('Connecting...')
  await begin()
}

/**
 * Show a simple config form in the phone's WebView (the HTML body). This is
 * only visible on the phone, not on the glasses — the glasses get their
 * display via the SDK bridge.
 */
function showPhoneConfigUI(): void {
  document.body.innerHTML = `
    <div style="max-width:400px;margin:40px auto;padding:20px;font-family:system-ui,sans-serif;color:#333">
      <h2 style="margin-top:0">Flashcards Setup</h2>
      <p style="font-size:0.9em;color:#666">
        Enter your flashcard site URL and API token to connect your glasses.
        Generate a token in the site's <strong>Settings &gt; Even glasses</strong>.
      </p>
      <label style="display:block;margin-top:16px;font-weight:600;font-size:0.85em">Site URL</label>
      <input id="cfg-url" type="url" placeholder="https://canto-ank.silcoff-labs.ca"
        style="width:100%;box-sizing:border-box;padding:10px;margin-top:4px;border:1px solid #ccc;border-radius:6px;font-size:1em"
        value="${localStorage.getItem('canto_base_url') || ''}">
      <label style="display:block;margin-top:14px;font-weight:600;font-size:0.85em">API Token</label>
      <input id="cfg-token" type="text" placeholder="Paste your token here"
        style="width:100%;box-sizing:border-box;padding:10px;margin-top:4px;border:1px solid #ccc;border-radius:6px;font-size:1em;font-family:monospace"
        value="${localStorage.getItem('canto_api_token') || ''}">
      <button id="cfg-save" style="display:block;width:100%;margin-top:18px;padding:12px;
        background:#2d6a4f;color:#fff;border:none;border-radius:8px;font-size:1em;font-weight:600;cursor:pointer">
        Connect
      </button>
      <p id="cfg-status" style="margin-top:10px;font-size:0.85em;color:#666"></p>
    </div>
  `
  document.getElementById('cfg-save')!.addEventListener('click', async () => {
    const url = (document.getElementById('cfg-url') as HTMLInputElement).value.trim()
    const token = (document.getElementById('cfg-token') as HTMLInputElement).value.trim()
    const status = document.getElementById('cfg-status')!

    if (!url || !token) {
      status.textContent = 'Both fields are required.'
      status.style.color = '#c0392b'
      return
    }

    status.textContent = 'Testing connection...'
    status.style.color = '#666'

    // Validate the credentials by hitting the streak endpoint.
    const testApi = new ApiClient(url, token)
    try {
      await testApi.streak()
    } catch (e) {
      status.textContent =
        e instanceof ApiError ? e.message : 'Could not connect. Check the URL.'
      status.style.color = '#c0392b'
      return
    }

    // Save config and restart.
    localStorage.setItem('canto_base_url', url)
    localStorage.setItem('canto_api_token', token)
    status.textContent = 'Connected! Starting review...'
    status.style.color = '#27ae60'

    api = new ApiClient(url, token)
    await begin()
  })
}

main()
