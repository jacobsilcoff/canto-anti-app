// Image containers max out at 288×144. SDK 0.0.12 compresses the sparse
// 4-bit buffer over BLE, so this can use the full bounds without the sendFailed
// behaviour of the old 0.0.10 / 8-bit implementation.
export const LARGE_TEXT_WIDTH = 288
export const LARGE_TEXT_HEIGHT = 144
export const MAX_LARGE_TEXT_CODEPOINTS = 12

export interface LargeTextBitmap {
  width: number
  height: number
  /** Row-major gray4 pixels (0–15), matching the G2 display wire format. */
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

export function renderLargeText(text: string): LargeTextBitmap | null {
  if (typeof document === 'undefined') return null
  const canvas = document.createElement('canvas')
  canvas.width = LARGE_TEXT_WIDTH
  canvas.height = LARGE_TEXT_HEIGHT
  const context = canvas.getContext('2d')
  if (!context) return null

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
    context.font = `${fontSize}px sans-serif`
    const metrics = context.measureText(value)
    const height =
      (metrics.actualBoundingBoxAscent || fontSize * 0.8) +
      (metrics.actualBoundingBoxDescent || fontSize * 0.2)
    if (metrics.width <= maxWidth && height <= maxHeight) break
    fontSize -= 2
  }
  context.font = `${fontSize}px sans-serif`
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
  return { width: canvas.width, height: canvas.height, data }
}
