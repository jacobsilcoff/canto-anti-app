/**
 * Big-glyph rendering for the glasses HUD.
 *
 * The Even Hub text container has no font-size control, so a single-character
 * prompt (a lone CJK 字, say) renders tiny and hard to read. To draw it BIG we
 * fall back to the image path: rasterize the glyph to an offscreen canvas,
 * hand the pixels to `bridge.updateImageRawData`, and let the host convert to
 * the display's gray4.
 *
 * This is best-effort by design — the exact wire format for image data isn't
 * documented in the SDK types, so the caller checks the result and falls back
 * to plain text on any failure. Nothing here talks to the SDK; the pure
 * predicate `shouldRenderAsGlyph` is unit-tested, `renderGlyph` needs a DOM.
 */

// Image container bounds (SDK: width 20~288, height 20~144). Leave a margin.
export const GLYPH_W = 240
export const GLYPH_H = 136

// Longest prompt (in code points) still worth scaling up. renderGlyph shrinks
// the font to fit, so longer would render too — but past ~4 CJK chars each
// glyph is no bigger than the default text, so it stops being a win. Single
// vocab words (1–4 chars) are the sweet spot; multi-word phrases stay as text.
export const MAX_GLYPH_CHARS = 4

export interface GlyphBitmap {
  width: number
  height: number
  /** Row-major grayscale, one byte (0–255) per pixel; brighter = lit. */
  data: number[]
}

/** Count Unicode code points (so a CJK char counts as 1, not 2 UTF-16 units). */
function codePointLength(s: string): number {
  let n = 0
  for (const _ of s) n++
  return n
}

/**
 * Whether a prompt is short enough that scaling it up as an image is worth it.
 * True for a lone character or a very short token (no internal whitespace) —
 * i.e. exactly the single-CJK-character cards that render too small as text.
 */
export function shouldRenderAsGlyph(prompt: string): boolean {
  const t = (prompt || '').trim()
  if (!t) return false
  if (/\s/.test(t)) return false // multi-word phrases stay as text
  return codePointLength(t) <= MAX_GLYPH_CHARS
}

/**
 * Rasterize `text` centered on a GLYPH_W×GLYPH_H grayscale bitmap. Returns null
 * when no 2-D canvas is available (caller falls back to text).
 */
export function renderGlyph(text: string): GlyphBitmap | null {
  if (typeof document === 'undefined') return null
  const canvas = document.createElement('canvas')
  canvas.width = GLYPH_W
  canvas.height = GLYPH_H
  const ctx = canvas.getContext('2d')
  if (!ctx) return null

  // Black background, white glyph — the host maps brighter pixels to lit ones.
  ctx.fillStyle = '#000'
  ctx.fillRect(0, 0, GLYPH_W, GLYPH_H)
  ctx.fillStyle = '#fff'
  ctx.textAlign = 'center'
  ctx.textBaseline = 'middle'

  // Grow the font until the glyph nearly fills the box, then back off a touch.
  let size = GLYPH_H
  const maxW = GLYPH_W * 0.9
  const maxH = GLYPH_H * 0.9
  for (; size > 8; size -= 2) {
    ctx.font = `${size}px sans-serif`
    const m = ctx.measureText(text)
    const h =
      (m.actualBoundingBoxAscent || size * 0.8) +
      (m.actualBoundingBoxDescent || size * 0.2)
    if (m.width <= maxW && h <= maxH) break
  }
  ctx.font = `${size}px sans-serif`
  ctx.fillText(text, GLYPH_W / 2, GLYPH_H / 2)

  let img: ImageData
  try {
    img = ctx.getImageData(0, 0, GLYPH_W, GLYPH_H)
  } catch {
    return null
  }

  // RGBA → grayscale luminance.
  const px = img.data
  const data: number[] = new Array(GLYPH_W * GLYPH_H)
  for (let i = 0, p = 0; i < px.length; i += 4, p++) {
    data[p] = Math.round(0.299 * px[i] + 0.587 * px[i + 1] + 0.114 * px[i + 2])
  }
  return { width: GLYPH_W, height: GLYPH_H, data }
}
