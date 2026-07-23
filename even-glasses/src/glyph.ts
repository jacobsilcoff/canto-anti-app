// The REAL hardware limit for image containers is 200×100 (verified by the
// community; the SDK README's 288×144 is wrong). An oversized container is
// accepted by the bridge but the firmware silently renders nothing, while
// the simulator enforces no image limits at all.
export const LARGE_TEXT_WIDTH = 200
export const LARGE_TEXT_HEIGHT = 100
export const MAX_LARGE_TEXT_CODEPOINTS = 12
export const GLYPH_BINARY_THRESHOLD = 96
const LARGE_TEXT_FONT = '"PingFang HK", "PingFang TC", "Noto Sans CJK TC", "Microsoft JhengHei", sans-serif'

export interface LargeTextBitmap {
  width: number
  height: number
  /** Encoded PNG bytes as number[], the host bridge's preferred List<int> shape. */
  data: number[]
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

/**
 * Convert antialiased RGBA glyph pixels to an opaque white-on-black mask.
 * Every pixel stays fully opaque — the phone-side gray4 conversion composites
 * the decoded image, and transparent pixels convert unpredictably.
 */
export function binarizeRgba(
  data: Uint8ClampedArray,
  threshold = GLYPH_BINARY_THRESHOLD,
): number {
  let visiblePixels = 0
  for (let index = 0; index < data.length; index += 4) {
    const luminance =
      0.299 * data[index] +
      0.587 * data[index + 1] +
      0.114 * data[index + 2]
    const value = luminance >= threshold ? 255 : 0
    data[index] = value
    data[index + 1] = value
    data[index + 2] = value
    data[index + 3] = 255
    if (value) visiblePixels += 1
  }
  return visiblePixels
}

// --- Compact PNG encoding ---------------------------------------------------
// The bridge ships image bytes over BLE at roughly 200 bytes/second, so payload
// size IS the on-glasses render latency. The binarized glyph frame is pure
// black/white, which a 1-bit grayscale PNG (a baseline format every conformant
// PNG decoder must support) stores 3-5× smaller than the browser encoder's
// 32-bit RGBA output.

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]

let crcTable: Uint32Array | null = null

function crc32(bytes: Uint8Array): number {
  if (!crcTable) {
    crcTable = new Uint32Array(256)
    for (let n = 0; n < 256; n += 1) {
      let c = n
      for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
      crcTable[n] = c >>> 0
    }
  }
  let crc = 0xffffffff
  for (const byte of bytes) crc = crcTable[(crc ^ byte) & 0xff] ^ (crc >>> 8)
  return (crc ^ 0xffffffff) >>> 0
}

function pngChunk(type: string, body: Uint8Array): Uint8Array {
  const chunk = new Uint8Array(12 + body.length)
  const view = new DataView(chunk.buffer)
  view.setUint32(0, body.length)
  for (let index = 0; index < 4; index += 1) chunk[4 + index] = type.charCodeAt(index)
  chunk.set(body, 8)
  view.setUint32(8 + body.length, crc32(chunk.subarray(4, 8 + body.length)))
  return chunk
}

/** Pack binarized RGBA into 1-bit PNG scanlines: a 0 filter byte per row, then MSB-first bits. */
export function packMonoScanlines(
  rgba: ArrayLike<number>,
  width: number,
  height: number,
): Uint8Array<ArrayBuffer> {
  const rowBytes = Math.ceil(width / 8)
  const lines = new Uint8Array((rowBytes + 1) * height)
  for (let y = 0; y < height; y += 1) {
    const row = y * (rowBytes + 1) + 1
    for (let x = 0; x < width; x += 1) {
      if (rgba[(y * width + x) * 4] >= 128) {
        lines[row + (x >> 3)] |= 0x80 >> (x & 7)
      }
    }
  }
  return lines
}

/** 1-bit grayscale PNG from binarized RGBA pixels; null when the runtime can't deflate. */
export async function encodeMonoPng(
  rgba: ArrayLike<number>,
  width: number,
  height: number,
): Promise<number[] | null> {
  if (typeof CompressionStream === 'undefined') return null
  try {
    const scanlines = packMonoScanlines(rgba, width, height)
    const stream = new Blob([scanlines])
      .stream()
      .pipeThrough(new CompressionStream('deflate'))
    const idat = new Uint8Array(await new Response(stream).arrayBuffer())
    const ihdr = new Uint8Array(13)
    const view = new DataView(ihdr.buffer)
    view.setUint32(0, width)
    view.setUint32(4, height)
    ihdr[8] = 1 // bit depth: 1
    ihdr[9] = 0 // color type: grayscale
    return [
      ...PNG_SIGNATURE,
      ...pngChunk('IHDR', ihdr),
      ...pngChunk('IDAT', idat),
      ...pngChunk('IEND', new Uint8Array(0)),
    ]
  } catch {
    return null
  }
}

/**
 * Encode the canvas as PNG bytes — the transport format the phone app is
 * known to decode. (A hand-rolled 1-bit BMP was previously sent here; the
 * bridge reported success but the phone decoded it to a blank frame on real
 * hardware, while the simulator's browser decoder rendered it fine.)
 */
async function encodePng(canvas: HTMLCanvasElement): Promise<number[]> {
  const blob: Blob = await new Promise((resolve, reject) => {
    canvas.toBlob(
      (result) => (result ? resolve(result) : reject(new Error('PNG encoding failed.'))),
      'image/png',
    )
  })
  return Array.from(new Uint8Array(await blob.arrayBuffer()))
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

let blankBitmap: LargeTextBitmap | null = null

export async function renderBlankLargeText(
  diagnostic?: RenderDiagnostic,
): Promise<LargeTextBitmap | null> {
  if (blankBitmap) return blankBitmap
  const mono = await encodeMonoPng(
    new Uint8ClampedArray(LARGE_TEXT_WIDTH * LARGE_TEXT_HEIGHT * 4),
    LARGE_TEXT_WIDTH,
    LARGE_TEXT_HEIGHT,
  )
  if (mono) {
    blankBitmap = { width: LARGE_TEXT_WIDTH, height: LARGE_TEXT_HEIGHT, data: mono }
    return blankBitmap
  }
  const canvas = createCanvas(diagnostic)
  const context = canvas?.getContext('2d')
  if (!canvas || !context) {
    diagnostic?.('The 2D canvas context is unavailable for the blank image.')
    return null
  }
  context.fillStyle = '#000'
  context.fillRect(0, 0, canvas.width, canvas.height)
  const data = await encodePng(canvas)
  blankBitmap = { width: canvas.width, height: canvas.height, data }
  return blankBitmap
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

  // Solid black background: black pixels are "off" on the glasses, and an
  // opaque canvas avoids transparent pixels in the encoded PNG entirely.
  context.fillStyle = '#000'
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.fillStyle = '#fff'
  context.textAlign = 'center'
  context.textBaseline = 'middle'

  const value = text.trim()
  const maxWidth = canvas.width * 0.97
  const maxHeight = canvas.height * 0.94
  let fontSize = 120
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

  const visiblePixels = binarizeRgba(image.data)
  if (!visiblePixels) {
    diagnostic?.(`The selected font rendered no visible pixels for “${value}”.`)
    return null
  }
  context.putImageData(image, 0, 0)
  const data =
    (await encodeMonoPng(image.data, canvas.width, canvas.height)) ?? (await encodePng(canvas))
  return { width: canvas.width, height: canvas.height, data }
}
