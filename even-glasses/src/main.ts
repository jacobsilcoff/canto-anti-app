/**
 * Even glasses flashcard plugin (Even Hub).
 *
 * Drives a hands-free review of your due flashcards on Even Realities G2
 * glasses. Every grade is sent to your account, so you earn XP and keep your
 * streak / daily quests in sync — the same as reviewing on the web.
 *
 * Controls (G2 TouchBar / R1 Ring):
 *   - before reveal:  tap             -> show the answer
 *                     double-tap      -> go back to the previous card
 *   - after reveal:   tap             -> "got it"    (SRS "good")
 *                     double-tap      -> "missed it" (SRS "again")
 *                     swipe up        -> "got it"
 *                     swipe down      -> "missed it"
 * Every grade shows a brief 👍/👎 confirmation so a finicky ring tap is
 * visibly acknowledged.
 *
 * Because the glasses have no speaker, audio is never used:
 *   - Non-Latin languages show romanization as the prompt on the pronunciation
 *     face and in the reveal on other faces.
 *   - Latin pronunciation faces (audio-only) are skipped automatically.
 *
 * Short prompts (a lone CJK character) are drawn BIG via the image path for
 * legibility, falling back to text when the image can't be sent.
 */
import {
  waitForEvenAppBridge,
  CreateStartUpPageContainer,
  TextContainerProperty,
  TextContainerUpgrade,
  ImageContainerProperty,
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
  OsEventTypeList,
} from '@evenrealities/even_hub_sdk'
import type { EvenAppBridge, EvenHubEvent } from '@evenrealities/even_hub_sdk'

import { ApiClient, ApiError } from './api.js'
import type { DueCard, Quality, Label } from './api.js'
import { buildView, playableCards } from './cards.js'
import type { CardView } from './cards.js'
import {
  loadConfig,
  loadBaseUrl,
  loadDeckLabels,
  saveDeckLabels,
} from './config.js'
import { shouldRenderAsGlyph, renderGlyph, GLYPH_W, GLYPH_H } from './glyph.js'

// ── Display constants ────────────────────────────────────────────────────────
const CONTAINER_ID = 1
const CONTAINER_NAME = 'flashcard'
const IMAGE_ID = 2
const IMAGE_NAME = 'glyph'

// Full usable text area with a small margin.
const X = 20
const Y = 10
const W = 536
const H = 268

// Glyph image centered inside the text area.
const IX = X + Math.round((W - GLYPH_W) / 2)
const IY = Y + Math.round((H - GLYPH_H) / 2)

// How long to hold the 👍/👎 grade confirmation before advancing.
const GRADE_ACK_MS = 450

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
let deckLabels: number[] = []

/** XP awarded per graded card, so a "go back" can subtract it cleanly. */
let awarded: number[] = []
/** Whether a glyph image is currently drawn (so we know to clear it). */
let glyphActive = false

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

// ── Display helpers ──────────────────────────────────────────────────────────

/** Show the initial page container (text + image), called once. */
async function showStartup(text: string): Promise<void> {
  await bridge.createStartUpPageContainer(
    new CreateStartUpPageContainer({
      containerTotalNum: 2,
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
          borderRadius: 4,
          paddingLength: 10,
          isEventCapture: 1,
        }),
      ],
      imageObject: [
        new ImageContainerProperty({
          containerID: IMAGE_ID,
          containerName: IMAGE_NAME,
          xPosition: IX,
          yPosition: IY,
          width: GLYPH_W,
          height: GLYPH_H,
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

/** Push a grayscale bitmap to the image container. Returns true on success. */
async function drawImage(data: number[]): Promise<boolean> {
  try {
    const res = await bridge.updateImageRawData(
      new ImageRawDataUpdate({
        containerID: IMAGE_ID,
        containerName: IMAGE_NAME,
        imageData: data,
      }),
    )
    return (
      res === ImageRawDataUpdateResult.success ||
      (res as unknown as string) === 'success'
    )
  } catch {
    return false
  }
}

/** Blank the glyph image (all-black) so text mode isn't overlapped by it. */
async function clearGlyph(): Promise<void> {
  if (!glyphActive) return
  glyphActive = false
  await drawImage(new Array(GLYPH_W * GLYPH_H).fill(0))
}

/** Show plain text, clearing any glyph first. */
async function showText(text: string): Promise<void> {
  await clearGlyph()
  await updateText(text)
}

// ── Review loop ──────────────────────────────────────────────────────────────

async function begin(labelIds: number[] = deckLabels): Promise<void> {
  deckLabels = labelIds
  await showText('Loading your flashcards...')

  let romanizable: Set<string>
  let cards: DueCard[]
  try {
    ;[romanizable, { cards }] = await Promise.all([
      api.romanizableLangs(),
      api.dueSession(labelIds).then((s) => ({ cards: s.cards })),
    ])
  } catch (e) {
    return showError(e)
  }

  queue = playableCards(cards, romanizable)
  idx = 0
  xp = 0
  graded = 0
  awarded = []
  phase = 'prompt'

  if (queue.length === 0) {
    await showText('All caught up!\n\nNo cards due right now.\n\n( tap to check again )')
    return
  }

  await render()
}

/** Header line: what's being tested + progress. */
function header(v: CardView): string {
  return `${v.hint}  |  ${idx + 1}/${queue.length}`
}

async function render(): Promise<void> {
  const card = queue[idx]
  if (phase === 'prompt') view = buildView(card)
  const v = view!

  if (phase === 'prompt') {
    const backHint = idx > 0 ? '  ·  2× tap = back' : ''
    // Draw a lone character BIG via the image path; keep the instructions as
    // two compact lines up top, with the glyph centered below.
    if (shouldRenderAsGlyph(v.prompt)) {
      const bmp = renderGlyph(v.prompt)
      if (bmp && (await drawImage(bmp.data))) {
        glyphActive = true
        await updateText(`${header(v)}\n( tap to reveal${backHint} )`)
        return
      }
    }
    await showText(`${header(v)}\n\n\n${v.prompt}\n\n\n( tap to reveal${backHint} )`)
  } else {
    const notes = card.notes && card.notes.trim() ? `\n\n${card.notes.trim()}` : ''
    await showText(
      `${header(v)}\n\n` +
        `${v.prompt}\n---\n${v.answer}${notes}\n\n` +
        `tap = got it  ·  swipe ↓ = again`,
    )
  }
}

async function grade(quality: Quality): Promise<void> {
  const card = queue[idx]
  busy = true

  let gained = 0
  try {
    const res = await api.review(card.card_id, card.face, quality)
    gained = res.xp || 0
  } catch (e) {
    busy = false
    return showError(e)
  }

  // Acknowledge the input explicitly — the ring can be finicky, so show that
  // the grade registered before moving on.
  xp += gained
  awarded[idx] = gained
  graded += 1
  const ack = quality === 'good' ? `👍 Got it   +${gained} XP` : '👎 Again'
  await showText(`\n\n${ack}`)
  await sleep(GRADE_ACK_MS)

  idx += 1
  phase = 'prompt'
  busy = false
  if (idx >= queue.length) return finish()
  await render()
}

/** Prompt-phase "go back": re-show the previous card so it can be re-read. */
async function goBack(): Promise<void> {
  if (idx <= 0) return
  busy = true
  idx -= 1
  // The previous card was already graded; walk its award back so the running
  // totals stay honest. (There's no server endpoint to reverse an SRS review,
  // so re-grading it simply schedules a fresh review.)
  const prev = awarded[idx] || 0
  xp = Math.max(0, xp - prev)
  graded = Math.max(0, graded - 1)
  awarded.length = idx
  phase = 'prompt'
  busy = false
  await render()
}

async function finish(): Promise<void> {
  await clearGlyph()
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
  void showText(`Flashcards\n\n${msg}`)
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

  // Finished / empty screen -> any press restarts with the current deck scope.
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
        void render()
      } else {
        void grade('good')
      }
      break

    // Double-tap -> go back (prompt) / "missed it" (reveal)
    case OsEventTypeList.DOUBLE_CLICK_EVENT:
      if (phase === 'reveal') {
        void grade('again')
      } else {
        void goBack()
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
  deckLabels = loadDeckLabels()

  bridge = await waitForEvenAppBridge()
  bridge.onEvenHubEvent(onEvent)

  if (!cfg) {
    await showStartup(
      'Setup needed\n\n' +
        'Open this plugin on your phone\n' +
        'and paste your API token.\n\n' +
        '(Generate one in the site\'s\n' +
        'Settings > Even glasses)',
    )
    showPhoneConfigUI()
    return
  }

  api = new ApiClient(cfg.baseUrl, cfg.token)
  await showStartup('Connecting...')
  // Auto-start on the glasses with the saved deck scope, and also show the
  // phone panel so the deck / token can be changed and the review restarted.
  showPhonePanel()
  await begin(deckLabels)
}

/**
 * First-run setup form in the phone's WebView. Token-first (all a deployed
 * user needs); the site URL defaults to production behind an "Advanced" line.
 */
function showPhoneConfigUI(): void {
  document.body.innerHTML = `
    <div style="max-width:420px;margin:32px auto;padding:20px;font-family:system-ui,sans-serif;color:#222">
      <h2 style="margin:0 0 6px">Connect your glasses</h2>
      <ol style="font-size:0.92em;color:#555;padding-left:1.1em;margin:0 0 16px;line-height:1.5">
        <li>In the flashcard site, open <strong>Settings &rarr; Even glasses</strong>.</li>
        <li>Tap <strong>Generate token</strong> and copy it.</li>
        <li>Paste it below and tap Connect.</li>
      </ol>
      <label style="display:block;font-weight:600;font-size:0.85em">API Token</label>
      <input id="cfg-token" type="text" placeholder="Paste your token here"
        style="width:100%;box-sizing:border-box;padding:12px;margin-top:4px;border:1px solid #ccc;border-radius:8px;font-size:1em;font-family:monospace">
      <details style="margin-top:14px">
        <summary style="cursor:pointer;font-size:0.85em;color:#2d6a4f">Advanced: site URL</summary>
        <input id="cfg-url" type="url"
          style="width:100%;box-sizing:border-box;padding:10px;margin-top:8px;border:1px solid #ccc;border-radius:6px;font-size:0.95em"
          value="${loadBaseUrl()}">
        <p style="font-size:0.78em;color:#888;margin:6px 0 0">Only change this if you self-host at a different address.</p>
      </details>
      <button id="cfg-save" style="display:block;width:100%;margin-top:18px;padding:13px;
        background:#2d6a4f;color:#fff;border:none;border-radius:8px;font-size:1em;font-weight:600;cursor:pointer">
        Connect
      </button>
      <p id="cfg-status" style="margin-top:10px;font-size:0.85em;color:#666"></p>
    </div>
  `
  document.getElementById('cfg-save')!.addEventListener('click', async () => {
    const token = (document.getElementById('cfg-token') as HTMLInputElement).value.trim()
    const urlInput = (document.getElementById('cfg-url') as HTMLInputElement | null)
    const url = (urlInput?.value || loadBaseUrl()).trim()
    const status = document.getElementById('cfg-status')!

    if (!token) {
      status.textContent = 'Please paste your API token.'
      status.style.color = '#c0392b'
      return
    }

    status.textContent = 'Testing connection...'
    status.style.color = '#666'

    const testApi = new ApiClient(url, token)
    try {
      await testApi.streak()
    } catch (e) {
      status.textContent =
        e instanceof ApiError ? e.message : 'Could not connect. Check the URL.'
      status.style.color = '#c0392b'
      return
    }

    localStorage.setItem('canto_base_url', url)
    localStorage.setItem('canto_api_token', token)
    status.textContent = 'Connected!'
    status.style.color = '#27ae60'

    api = new ApiClient(url, token)
    showPhonePanel()
    await begin(deckLabels)
  })
}

/**
 * The persistent phone control panel (shown once connected): pick a deck/label
 * to study and (re)start the glasses review. Reachable even when the plugin was
 * launched from the glasses, so the deck can always be changed.
 */
function showPhonePanel(): void {
  document.body.innerHTML = `
    <div style="max-width:420px;margin:32px auto;padding:20px;font-family:system-ui,sans-serif;color:#222">
      <h2 style="margin:0 0 4px">Flashcards on your glasses</h2>
      <p style="font-size:0.9em;color:#666;margin:0 0 18px">Review is running on your glasses. Pick what to study, then Start.</p>
      <label style="display:block;font-weight:600;font-size:0.85em">Study</label>
      <select id="deck-select"
        style="width:100%;box-sizing:border-box;padding:12px;margin-top:4px;border:1px solid #ccc;border-radius:8px;font-size:1em;background:#fff">
        <option value="">All due cards</option>
      </select>
      <button id="deck-start" style="display:block;width:100%;margin-top:16px;padding:13px;
        background:#2d6a4f;color:#fff;border:none;border-radius:8px;font-size:1em;font-weight:600;cursor:pointer">
        Start review
      </button>
      <p id="deck-status" style="margin-top:10px;font-size:0.85em;color:#666"></p>
      <button id="deck-reset" style="display:block;margin-top:22px;background:none;border:none;
        color:#999;font-size:0.8em;text-decoration:underline;cursor:pointer">Change token / site</button>
    </div>
  `

  const select = document.getElementById('deck-select') as HTMLSelectElement
  const status = document.getElementById('deck-status')!

  // Populate deck / label options; 📦 deck labels first, then categories.
  void api
    .listLabels()
    .then((labels: Label[]) => {
      const withCards = labels.filter((l) => (l.card_count || 0) > 0)
      const isDeck = (l: Label) => l.name.startsWith('📦')
      withCards.sort((a, b) => {
        if (isDeck(a) !== isDeck(b)) return isDeck(a) ? -1 : 1
        return a.name.localeCompare(b.name)
      })
      for (const l of withCards) {
        const opt = document.createElement('option')
        opt.value = String(l.id)
        opt.textContent = `${l.name} (${l.card_count})`
        select.appendChild(opt)
      }
      // Restore the saved scope (single label; empty = all).
      if (deckLabels.length === 1) select.value = String(deckLabels[0])
    })
    .catch(() => {
      status.textContent = 'Could not load your decks (still reviewing all due cards).'
    })

  document.getElementById('deck-start')!.addEventListener('click', async () => {
    const val = select.value
    const labels = val ? [Number(val)] : []
    saveDeckLabels(labels)
    deckLabels = labels
    status.textContent = 'Starting on your glasses...'
    status.style.color = '#27ae60'
    await begin(labels)
  })

  document.getElementById('deck-reset')!.addEventListener('click', () => {
    localStorage.removeItem('canto_api_token')
    showPhoneConfigUI()
  })
}

main()
