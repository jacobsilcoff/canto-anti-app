import { expect, test } from 'vitest'
import {
  MAX_LARGE_TEXT_CODEPOINTS,
  binarizeRgba,
  encodeMonoPng,
  packMonoScanlines,
  shouldRenderLarge,
} from './glyph.js'

/** Bitwise (tableless) CRC-32 — an independent cross-check of the encoder's table. */
function referenceCrc32(bytes: Uint8Array): number {
  let crc = 0xffffffff
  for (const byte of bytes) {
    crc ^= byte
    for (let k = 0; k < 8; k += 1) crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1
  }
  return (crc ^ 0xffffffff) >>> 0
}

interface PngChunk {
  type: string
  body: Uint8Array
  crc: number
}

function parseChunks(png: number[]): PngChunk[] {
  const bytes = Uint8Array.from(png)
  const view = new DataView(bytes.buffer)
  const result: PngChunk[] = []
  let offset = 8
  while (offset < bytes.length) {
    const length = view.getUint32(offset)
    const type = String.fromCharCode(...bytes.subarray(offset + 4, offset + 8))
    result.push({
      type,
      body: bytes.subarray(offset + 8, offset + 8 + length),
      crc: view.getUint32(offset + 8 + length),
    })
    offset += 12 + length
  }
  return result
}

async function inflate(data: Uint8Array<ArrayBuffer>): Promise<Uint8Array> {
  const stream = new Blob([data]).stream().pipeThrough(new DecompressionStream('deflate'))
  return new Uint8Array(await new Response(stream).arrayBuffer())
}

/** width×height RGBA where the listed (x, y) pixels are white. */
function rgbaWith(width: number, height: number, lit: Array<[number, number]>): Uint8ClampedArray {
  const rgba = new Uint8ClampedArray(width * height * 4)
  for (const [x, y] of lit) {
    rgba.set([255, 255, 255, 255], (y * width + x) * 4)
  }
  return rgba
}

test('mono scanlines pack MSB-first with a leading filter byte per row', () => {
  const lines = packMonoScanlines(rgbaWith(10, 2, [[0, 0], [9, 1]]), 10, 2)
  expect([...lines]).toEqual([0, 0b10000000, 0, 0, 0, 0b01000000])
})

test('mono PNG is a valid 1-bit grayscale image that round-trips its pixels', async () => {
  const png = await encodeMonoPng(rgbaWith(10, 2, [[0, 0], [3, 0], [9, 1]]), 10, 2)
  expect(png).not.toBeNull()
  expect(png!.slice(0, 8)).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

  const chunks = parseChunks(png!)
  expect(chunks.map((chunk) => chunk.type)).toEqual(['IHDR', 'IDAT', 'IEND'])
  for (const chunk of chunks) {
    const typed = new Uint8Array([...chunk.type].map((c) => c.charCodeAt(0)))
    const checked = new Uint8Array([...typed, ...chunk.body])
    expect(chunk.crc).toBe(referenceCrc32(checked))
  }

  const ihdr = new DataView(Uint8Array.from(chunks[0].body).buffer)
  expect(ihdr.getUint32(0)).toBe(10) // width
  expect(ihdr.getUint32(4)).toBe(2) // height
  expect(chunks[0].body[8]).toBe(1) // bit depth
  expect(chunks[0].body[9]).toBe(0) // grayscale
  expect(chunks[0].body[12]).toBe(0) // no interlace

  const scanlines = await inflate(Uint8Array.from(chunks[1].body))
  expect([...scanlines]).toEqual([
    ...packMonoScanlines(rgbaWith(10, 2, [[0, 0], [3, 0], [9, 1]]), 10, 2),
  ])
})

test('mono PNG payloads stay small enough for a fast BLE transfer', async () => {
  // Glyph-ish 200×100 frame: a block of lit pixels in the middle.
  const lit: Array<[number, number]> = []
  for (let y = 20; y < 80; y += 1) {
    for (let x = 40; x < 160; x += 2) lit.push([x, y])
  }
  const glyphs = await encodeMonoPng(rgbaWith(200, 100, lit), 200, 100)
  const blank = await encodeMonoPng(new Uint8ClampedArray(200 * 100 * 4), 200, 100)
  expect(glyphs!.length).toBeLessThan(1000)
  expect(blank!.length).toBeLessThan(200)
})

test('short words and phrases, including CJK, use the large-text path', () => {
  expect(shouldRenderLarge('字')).toBe(true)
  expect(shouldRenderLarge('你好嗎')).toBe(true)
  expect(shouldRenderLarge('café')).toBe(true)
  expect(shouldRenderLarge('bonjour')).toBe(true)
  expect(shouldRenderLarge('thank you')).toBe(true)
})

test('long and multiline prompts remain native text', () => {
  expect(shouldRenderLarge('x'.repeat(MAX_LARGE_TEXT_CODEPOINTS + 1))).toBe(false)
  expect(shouldRenderLarge('line one\nline two')).toBe(false)
  expect(shouldRenderLarge('   ')).toBe(false)
})

test('antialiased glyph pixels become an opaque white-on-black mask', () => {
  const pixels = new Uint8ClampedArray([
    0, 0, 0, 255,
    95, 95, 95, 128,
    96, 96, 96, 128,
    255, 255, 255, 255,
  ])
  expect(binarizeRgba(pixels)).toBe(2)
  expect([...pixels]).toEqual([
    0, 0, 0, 255,
    0, 0, 0, 255,
    255, 255, 255, 255,
    255, 255, 255, 255,
  ])
})
