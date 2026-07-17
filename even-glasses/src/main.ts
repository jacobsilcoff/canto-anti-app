import {
  CreateStartUpPageContainer,
  ImageContainerProperty,
  ImageRawDataUpdate,
  ImageRawDataUpdateResult,
  OsEventTypeList,
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
  saveConfig,
  saveDeckLabels,
} from './config.js'
import {
  LARGE_TEXT_HEIGHT,
  LARGE_TEXT_WIDTH,
  renderLargeText,
  shouldRenderLarge,
} from './glyph.js'

const TEXT_ID = 1
const TEXT_NAME = 'flashcard'
const IMAGE_ID = 2
const IMAGE_NAME = 'largeText'
const IMAGE_X = Math.round((576 - LARGE_TEXT_WIDTH) / 2)
const IMAGE_Y = 58
const BRIDGE_TIMEOUT_MS = 8_000
const GRADE_ACK_MS = 450

type Phase = 'prompt' | 'reveal'

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
let displayTail: Promise<unknown> = Promise.resolve()
let actionTail: Promise<void> = Promise.resolve()

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

function bridgeCall<T>(operation: () => Promise<T>): Promise<T> {
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
      BRIDGE_TIMEOUT_MS,
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

async function createStartupPage(content: string): Promise<void> {
  if (startupReady) throw new Error('The glasses page was already created.')
  const result = await bridgeCall(() =>
    bridge.createStartUpPageContainer(new CreateStartUpPageContainer({
      containerTotalNum: 2,
      textObject: [
        new TextContainerProperty({
          containerID: TEXT_ID,
          containerName: TEXT_NAME,
          content: fitContent(content, 950),
          xPosition: 0,
          yPosition: 0,
          width: 576,
          height: 288,
          borderWidth: 0,
          borderColor: 0,
          borderRadius: 0,
          paddingLength: 8,
          isEventCapture: 1,
          zOrderIndex: 1,
        }),
      ],
      imageObject: [
        new ImageContainerProperty({
          containerID: IMAGE_ID,
          containerName: IMAGE_NAME,
          xPosition: IMAGE_X,
          yPosition: IMAGE_Y,
          width: LARGE_TEXT_WIDTH,
          height: LARGE_TEXT_HEIGHT,
          zOrderIndex: 2,
        }),
      ],
    })),
  )
  if (result !== StartUpPageCreateResult.success && Number(result) !== 0) {
    throw new Error(`The glasses rejected the startup page (code ${String(result)}).`)
  }
  startupReady = true
}

async function updateText(content: string): Promise<void> {
  const ok = await bridgeCall(() =>
    bridge.textContainerUpgrade(new TextContainerUpgrade({
      containerID: TEXT_ID,
      containerName: TEXT_NAME,
      contentOffset: 0,
      contentLength: 0,
      content: fitContent(content),
    })),
  )
  if (!ok) throw new Error('The glasses could not update the flashcard text.')
}

async function updateImage(data: number[]): Promise<boolean> {
  const result = await bridgeCall(() =>
    bridge.updateImageRawData(new ImageRawDataUpdate({
      containerID: IMAGE_ID,
      containerName: IMAGE_NAME,
      imageData: data,
    })),
  )
  return (
    result === ImageRawDataUpdateResult.success ||
    String(result).toLowerCase() === 'success'
  )
}

async function clearLargeText(): Promise<void> {
  if (!largeTextActive) return
  largeTextActive = false
  await updateImage(new Array<number>(LARGE_TEXT_WIDTH * LARGE_TEXT_HEIGHT).fill(0))
}

async function showText(content: string): Promise<void> {
  await clearLargeText()
  await updateText(content)
}

function header(cardView: CardView): string {
  return `${cardView.hint}  |  ${index + 1}/${queue.length}`
}

async function showPrompt(cardView: CardView): Promise<void> {
  const backHint = index > 0 ? '  ·  double tap = back' : '  ·  double tap = exit'
  if (shouldRenderLarge(cardView.prompt)) {
    const bitmap = renderLargeText(cardView.prompt)
    if (bitmap && (await updateImage(bitmap.data))) {
      largeTextActive = true
      await updateText(
        `${header(cardView)}\n\n\n\n\n\n\n\n\nTap to reveal${backHint}`,
      )
      return
    }
  }
  await showText(
    `${header(cardView)}\n\n\n${cardView.prompt}\n\n\nTap to reveal${backHint}`,
  )
}

async function renderCard(): Promise<void> {
  const card = queue[index]
  if (!card) return
  if (phase === 'prompt' || !view) view = buildView(card)

  if (phase === 'prompt') {
    await showPrompt(view)
    return
  }

  const notes = card.notes?.trim() ? `\n\n${card.notes.trim()}` : ''
  await showText(
    `${header(view)}\n\n${view.prompt}\n---\n${view.answer}${notes}\n\n` +
      'Tap / swipe up = got it\nSwipe down = again',
  )
}

async function begin(labelIds: number[] = deckLabels): Promise<void> {
  if (!api) return
  deckLabels = labelIds
  await showText('Loading your flashcards…')

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

  if (queue.length === 0) {
    await showText('All caught up!\n\nNo cards are due right now.\n\nTap to check again\nDouble tap to exit')
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

  const acknowledgment = quality === 'good' ? `Got it  +${gained} XP` : 'Again'
  await showText(`\n\n${acknowledgment}`)
  await sleep(GRADE_ACK_MS)

  index += 1
  phase = 'prompt'
  view = null
  if (index >= queue.length) {
    await finish()
    return
  }
  await renderCard()
}

async function goBack(): Promise<void> {
  if (index <= 0) return
  index -= 1
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
  await updateText(
    `Session done!\n\n${graded} cards   +${xp} XP${streak}\n\n` +
      'Tap to check again\nDouble tap to exit',
  )
}

async function showError(error: unknown): Promise<void> {
  const message = error instanceof ApiError || error instanceof Error
    ? error.message
    : 'Something went wrong.'
  if (startupReady) {
    try {
      await showText(`Flashcards\n\n${message}\n\nDouble tap to exit`)
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
      if (phase === 'prompt') {
        phase = 'reveal'
        await renderCard()
      } else {
        await grade('good')
      }
      return
    }
    if (type === OsEventTypeList.DOUBLE_CLICK_EVENT) {
      if (phase === 'reveal') await grade('again')
      else if (index > 0) await goBack()
      else await requestExit()
    }
    return
  }

  if (!connected || !event.textEvent || phase !== 'reveal' || index >= queue.length) return
  const type = event.textEvent.eventType
  if (type === OsEventTypeList.SCROLL_TOP_EVENT) await grade('good')
  if (type === OsEventTypeList.SCROLL_BOTTOM_EVENT) await grade('again')
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
    <button id="deck-start" style="width:100%;margin-top:16px;padding:13px;background:#2d6a4f;color:#fff;border:0;border-radius:8px;font-size:1rem;font-weight:650">Start review</button>
    <button id="change-token" style="margin-top:22px;background:none;border:0;color:#777;text-decoration:underline">Change token / site</button>
  `)
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
      'Setup needed\n\nOpen this plugin on your phone and paste the token from Settings → Even G2 glasses.\n\nDouble tap to exit',
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
