import {
  CreateStartUpPageContainer,
  ImageContainerProperty,
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
  OsEventTypeList,
  RebuildPageContainer,
  StartUpPageCreateResult,
  TextContainerProperty,
  TextContainerUpgrade,
  waitForEvenAppBridge,
} from '@evenrealities/even_hub_sdk'
import type { EvenAppBridge, EvenHubEvent } from '@evenrealities/even_hub_sdk'

import { ApiClient, ApiError } from './api.js'
import type { DueCard, Label, Quality } from './api.js'
import { buildView, playableCards } from './cards.js'
import type { CardView } from './cards.js'
import {
  clearConfig,
  loadBaseUrl,
  loadConfig,
  loadDeckLabels,
  loadShowHints,
  saveConfig,
  saveDeckLabels,
  saveShowHints,
} from './config.js'
import {
  LARGE_TEXT_HEIGHT,
  LARGE_TEXT_WIDTH,
  renderBlankLargeText,
  renderLargeText,
  shouldRenderLarge,
} from './glyph.js'
import { safeContainerContent } from './display.js'
import { cardAction } from './interaction.js'
import type { CardAction, CardInput, CardPhase } from './interaction.js'

const HEADER_ID = 1
const HEADER_NAME = 'header'
const BODY_ID = 2
const BODY_NAME = 'flashcard'
const FOOTER_ID = 3
const FOOTER_NAME = 'controls'
const IMAGE_ID = 4
const IMAGE_NAME = 'largeText'
const IMAGE_X = Math.round((576 - LARGE_TEXT_WIDTH) / 2)
const IMAGE_Y = Math.round(38 + (208 - LARGE_TEXT_HEIGHT) / 2)
const BRIDGE_TIMEOUT_MS = 8_000
// Image frames transfer over BLE at ~1s/200 bytes in the wild; a 2KB PNG has
// been observed to take 10+ seconds, so image sends get a much longer leash.
const IMAGE_TIMEOUT_MS = 30_000
const GRADE_ACK_MS = 450

type Phase = CardPhase

const PROMPT_CONTROLS = '● reveal   ▲ undo   ▼ skip   ●● exit'
const REVEAL_CONTROLS = '● good   ●● again'
const RESTART_CONTROLS = '● restart   ●● exit'
const FINISH_CONTROLS = '▲ undo   ● restart   ●● exit'
const EXIT_CONTROLS = '●● exit'
const BACK_DIVIDER = '─'.repeat(14)

let bridge: EvenAppBridge
let api: ApiClient | null = null
let unsubscribeEvents: (() => void) | null = null
let startupReady = false
let connected = false
let queue: DueCard[] = []
let index = 0
let phase: Phase = 'prompt'
let view: CardView | null = null
let xp = 0
let graded = 0
let deckLabels: number[] = []
let largeTextActive = false
let reviewHistory: ReviewedCard[] = []
let displayTail: Promise<unknown> = Promise.resolve()
let actionTail: Promise<void> = Promise.resolve()
let displayLayout: DisplayLayout = 'front'

type DisplayLayout = 'front' | 'back'

interface ReviewedCard {
  card: DueCard
  reviewId: number
  xp: number
}

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

function bridgeCall<T>(operation: () => Promise<T>, timeoutMs = BRIDGE_TIMEOUT_MS): Promise<T> {
  // Every SDK call waits for the prior raw call, even if the caller times out.
  // That prevents a slow BLE hop from overlapping the next bridge operation.
  const raw = displayTail.then(operation, operation)
  displayTail = raw.then(
    () => undefined,
    () => undefined,
  )
  return new Promise<T>((resolve, reject) => {
    const timeout = window.setTimeout(
      () => reject(new Error('Glasses connection timed out.')),
      timeoutMs,
    )
    raw.then(
      (result) => {
        window.clearTimeout(timeout)
        resolve(result)
      },
      (error: unknown) => {
        window.clearTimeout(timeout)
        reject(error)
      },
    )
  })
}

function enqueueAction(action: () => Promise<void>): void {
  actionTail = actionTail.then(action).catch(async (error: unknown) => {
    await showError(error)
  })
}

function fitContent(content: string, max = 1_900): string {
  if (content.length <= max) return content
  return `${content.slice(0, max - 1)}…`
}

function textContainers(
  heading: string,
  body: string,
  footer: string,
  layout: DisplayLayout,
): TextContainerProperty[] {
  const back = layout === 'back'
  // SDK pinned to 0.0.11: 0.0.12 LZ4-compresses image payloads (compressMode 2),
  // which the current Even app (2.2.6) rejects with sendFailed. No zOrderIndex
  // either — that field is also 0.0.12+. Revisit both when the app catches up.
  const header = new TextContainerProperty({
    containerID: HEADER_ID,
    containerName: HEADER_NAME,
    content: safeContainerContent(heading),
    xPosition: 0,
    yPosition: 0,
    width: 576,
    height: 38,
    borderWidth: 0,
    borderColor: 0,
    borderRadius: 0,
    paddingLength: 4,
    isEventCapture: 0,
  })
  const content = [
    new TextContainerProperty({
      containerID: BODY_ID,
      containerName: BODY_NAME,
      content: fitContent(safeContainerContent(body), 950),
      xPosition: 0,
      yPosition: back ? 0 : 38,
      width: 576,
      height: back ? 246 : 208,
      borderWidth: 0,
      borderColor: 0,
      borderRadius: 0,
      paddingLength: 8,
      isEventCapture: 1,
    }),
    new TextContainerProperty({
      containerID: FOOTER_ID,
      containerName: FOOTER_NAME,
      content: safeContainerContent(footer),
      xPosition: 0,
      yPosition: 246,
      width: 576,
      height: 42,
      borderWidth: 0,
      borderColor: 0,
      borderRadius: 0,
      paddingLength: 4,
      isEventCapture: 0,
    }),
  ]
  return back ? content : [header, ...content]
}

function imageContainers(): ImageContainerProperty[] {
  return [
    new ImageContainerProperty({
      containerID: IMAGE_ID,
      containerName: IMAGE_NAME,
      xPosition: IMAGE_X,
      yPosition: IMAGE_Y,
      width: LARGE_TEXT_WIDTH,
      height: LARGE_TEXT_HEIGHT,
    }),
  ]
}

async function createStartupPage(content: string): Promise<void> {
  if (startupReady) throw new Error('The glasses page was already created.')
  const result = await bridgeCall(() =>
    bridge.createStartUpPageContainer(new CreateStartUpPageContainer({
      containerTotalNum: 4,
      textObject: textContainers('Canto Flashcards', content, EXIT_CONTROLS, 'front'),
      imageObject: imageContainers(),
    })),
  )
  if (result !== StartUpPageCreateResult.success && Number(result) !== 0) {
    throw new Error(`The glasses rejected the startup page (code ${String(result)}).`)
  }
  startupReady = true
  displayLayout = 'front'
}

async function rebuildLayout(
  heading: string,
  body: string,
  footer: string,
  layout: DisplayLayout,
): Promise<void> {
  const ok = await bridgeCall(() =>
    bridge.rebuildPageContainer(new RebuildPageContainer({
      containerTotalNum: layout === 'front' ? 4 : 2,
      textObject: textContainers(heading, body, footer, layout),
      ...(layout === 'front' ? { imageObject: imageContainers() } : {}),
    })),
  )
  if (!ok) throw new Error(`The glasses could not switch to the ${layout} layout.`)
  largeTextActive = false
  displayLayout = layout
}

async function updateTextContainer(
  containerID: number,
  containerName: string,
  content: string,
  max = 1_900,
): Promise<void> {
  const ok = await bridgeCall(() =>
    bridge.textContainerUpgrade(new TextContainerUpgrade({
      containerID,
      containerName,
      contentOffset: 0,
      contentLength: 0,
      content: fitContent(safeContainerContent(content), max),
    })),
  )
  if (!ok) throw new Error(`The glasses could not update ${containerName}.`)
}

async function updateLayout(
  heading: string,
  body: string,
  footer: string,
  clearImage = true,
  layout: DisplayLayout = 'front',
): Promise<void> {
  if (layout !== displayLayout) {
    await rebuildLayout(heading, body, footer, layout)
    return
  }
  if (clearImage) await clearLargeText()
  if (layout === 'front') {
    await updateTextContainer(HEADER_ID, HEADER_NAME, heading, 180)
  }
  await updateTextContainer(BODY_ID, BODY_NAME, body)
  await updateTextContainer(FOOTER_ID, FOOTER_NAME, footer, 180)
}

async function showMessage(heading: string, body: string, footer = EXIT_CONTROLS): Promise<void> {
  await updateLayout(heading, body, footer)
}

async function sendImage(data: number[]): Promise<ImageRawDataUpdateResult> {
  return bridgeCall(() =>
    bridge.updateImageRawData(new ImageRawDataUpdate({
      containerID: IMAGE_ID,
      containerName: IMAGE_NAME,
      imageData: data,
    })),
  IMAGE_TIMEOUT_MS)
}

async function updateImage(data: number[]): Promise<boolean> {
  let result = await sendImage(data)
  if (
    result === ImageRawDataUpdateResult.sendFailed ||
    String(result).toLowerCase() === 'sendfailed'
  ) {
    await sleep(300)
    result = await sendImage(data)
  }
  const success = (
    result === ImageRawDataUpdateResult.success ||
    String(result).toLowerCase() === 'success'
  )
  if (!success) console.warn(`Image update rejected: ${String(result)}.`)
  return success
}

async function clearLargeText(): Promise<void> {
  if (!largeTextActive) return
  largeTextActive = false
  const blank = await renderBlankLargeText((message) => console.warn(message))
  if (blank) await updateImage(blank.data)
}

function header(cardView: CardView): string {
  return `${cardView.hint}  |  ${index + 1}/${queue.length}`
}

async function showPrompt(cardView: CardView): Promise<void> {
  const footer = loadShowHints() ? PROMPT_CONTROLS : ''
  if (shouldRenderLarge(cardView.prompt)) {
    const bitmap = await renderLargeText(cardView.prompt, (message) => console.warn(message))
    if (bitmap) {
      // The image frame takes seconds over BLE, so show the prompt as regular
      // text at the top of the body right away (clear of the image area), and
      // remove it once the large-text frame has arrived.
      await updateLayout(header(cardView), cardView.prompt, footer)
      if (await updateImage(bitmap.data)) {
        largeTextActive = true
        await updateTextContainer(BODY_ID, BODY_NAME, '')
      }
      return
    }
  }
  await updateLayout(header(cardView), `\n\n${cardView.prompt}`, footer)
}

async function renderCard(): Promise<void> {
  const card = queue[index]
  if (!card) return
  if (phase === 'prompt' || !view) view = buildView(card)

  if (phase === 'prompt') {
    await showPrompt(view)
    return
  }

  const reading = view.reading ? `\n${view.reading}` : ''
  const notes = card.notes?.trim() ? `\n\n${card.notes.trim()}` : ''
  await updateLayout(
    '',
    `${view.prompt}${reading}\n${BACK_DIVIDER}\n${view.answer}${notes}`,
    REVEAL_CONTROLS,
    true,
    'back',
  )
}

async function begin(labelIds: number[] = deckLabels): Promise<void> {
  if (!api) return
  deckLabels = labelIds
  await showMessage('Canto Flashcards', 'Loading your flashcards…', EXIT_CONTROLS)

  let romanizable: Set<string>
  let cards: DueCard[]
  try {
    const [languages, session] = await Promise.all([
      api.romanizableLangs(),
      api.dueSession(labelIds),
    ])
    romanizable = languages
    cards = session.cards
  } catch (error) {
    await showError(error)
    return
  }

  queue = playableCards(cards, romanizable)
  index = 0
  phase = 'prompt'
  view = null
  xp = 0
  graded = 0
  reviewHistory = []

  if (queue.length === 0) {
    await showMessage('All caught up!', 'No cards are due right now.', RESTART_CONTROLS)
    return
  }
  await renderCard()
}

async function grade(quality: Quality): Promise<void> {
  const card = queue[index]
  if (!api || !card) return
  const result = await api.review(card.card_id, card.face, quality)
  const gained = result.xp || 0
  xp += gained
  graded += 1
  if (Number.isInteger(result.review_id)) {
    reviewHistory.push({ card, reviewId: result.review_id!, xp: gained })
  } else {
    console.warn('The connected server does not support review undo yet.')
  }

  const acknowledgment = quality === 'good' ? `Got it  +${gained} XP` : 'Again'
  await updateLayout('', `\n\n${acknowledgment}`, '', true, 'back')
  await sleep(GRADE_ACK_MS)

  queue.splice(index, 1)
  phase = 'prompt'
  view = null
  if (queue.length === 0) {
    await finish()
    return
  }
  if (index >= queue.length) index = 0
  await renderCard()
}

async function goBack(): Promise<void> {
  const previous = reviewHistory[reviewHistory.length - 1]
  if (previous && api) {
    await api.undoReview(previous.reviewId)
    reviewHistory.pop()
    xp = Math.max(0, xp - previous.xp)
    graded = Math.max(0, graded - 1)
    queue.splice(index, 0, previous.card)
    phase = 'prompt'
    view = null
    await renderCard()
    return
  }
  if (queue.length <= 1) return
  index = (index - 1 + queue.length) % queue.length
  phase = 'prompt'
  view = null
  await renderCard()
}

async function skip(): Promise<void> {
  if (queue.length <= 1) return
  index = (index + 1) % queue.length
  phase = 'prompt'
  view = null
  await renderCard()
}

async function finish(): Promise<void> {
  await clearLargeText()
  let streak = ''
  try {
    const summary = await api?.streak()
    if (summary) {
      streak = `\n\nStreak: ${summary.streak}   XP today: ${summary.points_today}/${summary.daily_goal}`
    }
  } catch {
    // The session result is still useful if the summary request fails.
  }
  await updateLayout(
    'Session done!',
    `${graded} cards   +${xp} XP${streak}`,
    FINISH_CONTROLS,
    false,
  )
}

async function showError(error: unknown): Promise<void> {
  const message = error instanceof ApiError || error instanceof Error
    ? error.message
    : 'Something went wrong.'
  console.error(message)
  if (startupReady) {
    try {
      await showMessage('Flashcards', message, EXIT_CONTROLS)
    } catch {
      // The phone UI remains available if the BLE connection has failed.
    }
  }
  const status = document.getElementById('phone-status')
  if (status) {
    status.textContent = message
    status.style.color = '#b42318'
  }
}

async function requestExit(): Promise<void> {
  await bridgeCall(() => bridge.shutDownPageContainer(1))
}

async function performCardAction(action: CardAction): Promise<void> {
  if (action === 'none') return
  if (action === 'reveal') {
    phase = 'reveal'
    await renderCard()
    return
  }
  if (action === 'good' || action === 'again') {
    await grade(action)
    return
  }
  if (action === 'back') {
    await goBack()
    return
  }
  if (action === 'skip') {
    await skip()
    return
  }
  await requestExit()
}

function cleanup(): void {
  unsubscribeEvents?.()
  unsubscribeEvents = null
}

async function handleEvent(event: EvenHubEvent): Promise<void> {
  if (event.sysEvent) {
    const type = event.sysEvent.eventType ?? OsEventTypeList.CLICK_EVENT
    if (type === OsEventTypeList.FOREGROUND_ENTER_EVENT) {
      if (connected && queue.length > 0 && index < queue.length) await renderCard()
      return
    }
    if (type === OsEventTypeList.FOREGROUND_EXIT_EVENT) return
    if (
      type === OsEventTypeList.ABNORMAL_EXIT_EVENT ||
      type === OsEventTypeList.SYSTEM_EXIT_EVENT
    ) {
      cleanup()
      return
    }
    if (!connected) {
      if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) await requestExit()
      return
    }
    if (index >= queue.length) {
      if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) await requestExit()
      else if (type === OsEventTypeList.CLICK_EVENT) await begin()
      return
    }
    if (type === OsEventTypeList.CLICK_EVENT) {
      await performCardAction(cardAction(phase, 'press'))
      return
    }
    if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      await performCardAction(cardAction(phase, 'double-press'))
    }
    return
  }

  if (!connected || !event.textEvent) return
  const type = event.textEvent.eventType
  let input: CardInput | null = null
  if (type === OsEventTypeList.SCROLL_TOP_EVENT) input = 'swipe-up'
  if (type === OsEventTypeList.SCROLL_BOTTOM_EVENT) input = 'swipe-down'
  if (index >= queue.length) {
    if (input === 'swipe-up' && reviewHistory.length > 0) await goBack()
    return
  }
  if (input) await performCardAction(cardAction(phase, input))
}

function onEvent(event: EvenHubEvent): void {
  enqueueAction(() => handleEvent(event))
}

function phoneShell(content: string): void {
  document.body.innerHTML = `
    <main style="max-width:420px;margin:32px auto;padding:20px;font-family:system-ui,sans-serif;color:#222">
      ${content}
      <p id="phone-status" style="margin-top:12px;font-size:.88rem;color:#666"></p>
    </main>
  `
}

function showPhoneConfig(): void {
  phoneShell(`
    <h2 style="margin:0 0 8px">Connect your glasses</h2>
    <ol style="color:#555;padding-left:1.2em;line-height:1.5">
      <li>Open the flashcard site’s <strong>Settings → Even G2 glasses</strong>.</li>
      <li>Generate and copy a token.</li>
      <li>Paste it here.</li>
    </ol>
    <label style="display:block;font-weight:600;font-size:.88rem">API token</label>
    <input id="config-token" type="text" autocomplete="off" placeholder="Paste token"
      style="width:100%;box-sizing:border-box;padding:12px;margin-top:4px;border:1px solid #bbb;border-radius:8px;font:1rem monospace">
    <label style="display:block;font-weight:600;font-size:.88rem;margin-top:14px">Site</label>
    <select id="config-url" style="width:100%;box-sizing:border-box;padding:12px;margin-top:4px;border:1px solid #bbb;border-radius:8px;background:#fff;font-size:1rem">
      <option value="https://canto-anki.silcoff-labs.ca">Production</option>
      <option value="https://dev.canto-anki.silcoff-labs.ca">Beta / development</option>
    </select>
    <button id="config-save" style="width:100%;margin-top:18px;padding:13px;background:#2d6a4f;color:#fff;border:0;border-radius:8px;font-size:1rem;font-weight:650">Connect</button>
  `)
  const url = document.getElementById('config-url') as HTMLSelectElement
  url.value = loadBaseUrl()
  document.getElementById('config-save')?.addEventListener('click', () => {
    enqueueAction(async () => {
      const token = (document.getElementById('config-token') as HTMLInputElement).value.trim()
      const status = document.getElementById('phone-status')!
      if (!token) {
        status.textContent = 'Paste your token first.'
        status.style.color = '#b42318'
        return
      }
      status.textContent = 'Testing connection…'
      const candidate = new ApiClient(url.value, token)
      await candidate.streak()
      saveConfig({ baseUrl: url.value, token })
      api = candidate
      connected = true
      showPhonePanel()
      await begin(deckLabels)
    })
  })
}

function showPhonePanel(): void {
  phoneShell(`
    <h2 style="margin:0 0 6px">Canto Flashcards</h2>
    <p style="color:#666;margin:0 0 18px">Review is running on your glasses.</p>
    <label style="display:block;font-weight:600;font-size:.88rem">Study</label>
    <select id="deck-select" style="width:100%;box-sizing:border-box;padding:12px;margin-top:4px;border:1px solid #bbb;border-radius:8px;background:#fff;font-size:1rem">
      <option value="">All due cards</option>
    </select>
    <label style="display:flex;align-items:center;gap:10px;margin-top:16px;font-size:.95rem">
      <input id="hints-toggle" type="checkbox" style="width:18px;height:18px;accent-color:#2d6a4f">
      Show button hints on the card front
    </label>
    <p style="margin:4px 0 0 28px;color:#888;font-size:.8rem">Turn off for a cleaner display once you know the controls.</p>
    <button id="deck-start" style="width:100%;margin-top:16px;padding:13px;background:#2d6a4f;color:#fff;border:0;border-radius:8px;font-size:1rem;font-weight:650">Start review</button>
    <button id="change-token" style="margin-top:22px;background:none;border:0;color:#777;text-decoration:underline">Change token / site</button>
  `)
  const hints = document.getElementById('hints-toggle') as HTMLInputElement
  hints.checked = loadShowHints()
  hints.addEventListener('change', () => {
    saveShowHints(hints.checked)
    // Repaint the current card so the change is visible immediately.
    if (connected && phase === 'prompt' && index < queue.length) {
      enqueueAction(() => renderCard())
    }
  })
  const select = document.getElementById('deck-select') as HTMLSelectElement
  void api?.listLabels().then((labels: Label[]) => {
    const withCards = labels.filter((label) => label.card_count > 0)
    withCards.sort((a, b) => a.name.localeCompare(b.name))
    for (const label of withCards) {
      const option = document.createElement('option')
      option.value = String(label.id)
      option.textContent = `${label.name} (${label.card_count})`
      select.appendChild(option)
    }
    if (deckLabels.length === 1) select.value = String(deckLabels[0])
  }).catch((error: unknown) => showError(error))

  document.getElementById('deck-start')?.addEventListener('click', () => {
    const selected = select.value ? [Number(select.value)] : []
    saveDeckLabels(selected)
    enqueueAction(() => begin(selected))
  })
  document.getElementById('change-token')?.addEventListener('click', () => {
    clearConfig()
    connected = false
    api = null
    showPhoneConfig()
  })
}

async function main(): Promise<void> {
  deckLabels = loadDeckLabels()
  bridge = await waitForEvenAppBridge()
  unsubscribeEvents = bridge.onEvenHubEvent(onEvent)

  const config = loadConfig()
  if (!config) {
    await createStartupPage(
      'Setup needed\n\nOpen this plugin on your phone and paste the token from Settings → Even G2 glasses.',
    )
    showPhoneConfig()
    return
  }

  api = new ApiClient(config.baseUrl, config.token)
  connected = true
  await createStartupPage('Connecting…')
  showPhonePanel()
  await begin(deckLabels)
}

void main().catch((error: unknown) => {
  phoneShell('<h2 style="margin:0 0 8px">Canto Flashcards could not start</h2>')
  void showError(error)
})
