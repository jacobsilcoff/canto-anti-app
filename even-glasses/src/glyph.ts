// Image containers max out at 288×144. The SDK expects encoded image bytes,
// then decodes and converts the image to gray4 for the BLE transfer.
export const LARGE_TEXT_WIDTH = 288
export const LARGE_TEXT_HEIGHT = 144
export const MAX_LARGE_TEXT_CODEPOINTS = 12
const LARGE_TEXT_FONT = '"PingFang HK", "PingFang TC", "Noto Sans CJK TC", "Microsoft JhengHei", sans-serif'

export interface LargeTextBitmap {
  width: number
  height: number
  /** Encoded PNG bytes, matching the official Even Hub image template. */
  data: Uint8Array
}

export type RenderDiagnostic = (message: string) => void

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

export function hasPngSignature(data: Uint8Array): boolean {
  const signature = [137, 80, 78, 71, 13, 10, 26, 10]
  return signature.every((value, index) => data[index] === value)
}

async function encodePng(
  canvas: HTMLCanvasElement,
  diagnostic?: RenderDiagnostic,
): Promise<Uint8Array | null> {
  try {
    const blob = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob(resolve, 'image/png')
    })
    if (!blob) {
      diagnostic?.('Canvas returned no PNG blob.')
      return null
    }
    const data = new Uint8Array(await blob.arrayBuffer())
    if (!hasPngSignature(data)) {
      diagnostic?.(`Canvas returned ${data.length} bytes without a PNG signature.`)
      return null
    }
    return data
  } catch (error) {
    diagnostic?.(`PNG encoding threw: ${error instanceof Error ? error.message : String(error)}`)
    return null
  }
}

function createCanvas(diagnostic?: RenderDiagnostic): HTMLCanvasElement | null {
  if (typeof document === 'undefined') {
    diagnostic?.('No document is available to create the large-text canvas.')
    return null
  }
  const canvas = document.createElement('canvas')
  canvas.width = LARGE_TEXT_WIDTH
  canvas.height = LARGE_TEXT_HEIGHT
  return canvas
}

export async function renderBlankLargeText(
  diagnostic?: RenderDiagnostic,
): Promise<LargeTextBitmap | null> {
  const canvas = createCanvas(diagnostic)
  const context = canvas?.getContext('2d')
  if (!canvas || !context) {
    diagnostic?.('The 2D canvas context is unavailable for the blank image.')
    return null
  }
  context.fillStyle = '#000'
  context.fillRect(0, 0, canvas.width, canvas.height)
  const data = await encodePng(canvas, diagnostic)
  return data ? { width: canvas.width, height: canvas.height, data } : null
}

export async function renderLargeText(
  text: string,
  diagnostic?: RenderDiagnostic,
): Promise<LargeTextBitmap | null> {
  const canvas = createCanvas(diagnostic)
  const context = canvas?.getContext('2d')
  if (!canvas || !context) {
    diagnostic?.('The 2D canvas context is unavailable for large text.')
    return null
  }

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
  } catch (error) {
    diagnostic?.(`Reading canvas pixels threw: ${error instanceof Error ? error.message : String(error)}`)
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
  if (!hasVisiblePixels(data)) {
    diagnostic?.(`The selected font rendered no visible pixels for “${value}”.`)
    return null
  }
  diagnostic?.(`Rendered “${value}” at ${fontSize}px on ${canvas.width}×${canvas.height}.`)
  const png = await encodePng(canvas, diagnostic)
  return png ? { width: canvas.width, height: canvas.height, data: png } : null
}
