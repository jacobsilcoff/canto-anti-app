// Image containers max out at 288×144. The host decodes this module's PNG
// frames, converts them to gray4, and compresses them for the BLE transfer.
export const LARGE_TEXT_WIDTH = 288
export const LARGE_TEXT_HEIGHT = 144
export const MAX_LARGE_TEXT_CODEPOINTS = 12
const LARGE_TEXT_FONT = '"PingFang HK", "PingFang TC", "Noto Sans CJK TC", "Microsoft JhengHei", sans-serif'

export interface LargeTextBitmap {
  width: number
  height: number
  /** Encoded PNG bytes; the Even Hub host decodes and converts them to gray4. */
  data: number[]
}

export function codePointLength(value: string): number {
  return [...value].length
}

export function shouldRenderLarge(prompt: string): boolean {
  const value = (prompt || '').trim()
  if (!value || value.includes('\n')) return false
  return codePointLength(value) <= MAX_LARGE_TEXT_CODEPOINTS
}

export function gray8ToGray4(value: number): number {
  return Math.max(0, Math.min(15, Math.round(value / 17)))
}

export function hasVisiblePixels(data: number[]): boolean {
  return data.some((value) => value > 0)
}

function encodePng(canvas: HTMLCanvasElement): number[] | null {
  try {
    const encoded = canvas.toDataURL('image/png')
    const comma = encoded.indexOf(',')
    if (comma < 0) return null
    const binary = window.atob(encoded.slice(comma + 1))
    return Array.from(binary, (character) => character.charCodeAt(0))
  } catch {
    return null
  }
}

function createCanvas(): HTMLCanvasElement | null {
  if (typeof document === 'undefined') return null
  const canvas = document.createElement('canvas')
  canvas.width = LARGE_TEXT_WIDTH
  canvas.height = LARGE_TEXT_HEIGHT
  return canvas
}

export function renderBlankLargeText(): LargeTextBitmap | null {
  const canvas = createCanvas()
  const context = canvas?.getContext('2d')
  if (!canvas || !context) return null
  context.fillStyle = '#000'
  context.fillRect(0, 0, canvas.width, canvas.height)
  const data = encodePng(canvas)
  return data ? { width: canvas.width, height: canvas.height, data } : null
}

export function renderLargeText(text: string): LargeTextBitmap | null {
  const canvas = createCanvas()
  const context = canvas?.getContext('2d')
  if (!canvas || !context) return null

  context.fillStyle = '#000'
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.fillStyle = '#fff'
  context.textAlign = 'center'
  context.textBaseline = 'middle'

  const value = text.trim()
  const maxWidth = canvas.width * 0.92
  const maxHeight = canvas.height * 0.82
  let fontSize = 124
  while (fontSize > 22) {
    context.font = `${fontSize}px ${LARGE_TEXT_FONT}`
    const metrics = context.measureText(value)
    const height =
      (metrics.actualBoundingBoxAscent || fontSize * 0.8) +
      (metrics.actualBoundingBoxDescent || fontSize * 0.2)
    if (metrics.width <= maxWidth && height <= maxHeight) break
    fontSize -= 2
  }
  context.font = `${fontSize}px ${LARGE_TEXT_FONT}`
  context.fillText(value, canvas.width / 2, canvas.height / 2)

  let image: ImageData
  try {
    image = context.getImageData(0, 0, canvas.width, canvas.height)
  } catch {
    return null
  }

  const data = new Array<number>(canvas.width * canvas.height)
  for (let src = 0, dest = 0; src < image.data.length; src += 4, dest += 1) {
    const luminance =
      0.299 * image.data[src] +
      0.587 * image.data[src + 1] +
      0.114 * image.data[src + 2]
    data[dest] = gray8ToGray4(luminance)
  }
  if (!hasVisiblePixels(data)) return null
  const png = encodePng(canvas)
  return png ? { width: canvas.width, height: canvas.height, data: png } : null
}
