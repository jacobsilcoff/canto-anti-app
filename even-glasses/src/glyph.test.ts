import { expect, test } from 'vitest'
import { shouldRenderAsGlyph } from './glyph.js'

test('short CJK words render as a big glyph', () => {
  expect(shouldRenderAsGlyph('字')).toBe(true)
  expect(shouldRenderAsGlyph('你好')).toBe(true)
  expect(shouldRenderAsGlyph('你好嗎')).toBe(true) // 3 code points
  expect(shouldRenderAsGlyph('朋友仔們')).toBe(true) // 4 code points (the cap)
})

test('long words and phrases stay as text', () => {
  expect(shouldRenderAsGlyph('唔該晒你哋')).toBe(false) // 5 code points
  expect(shouldRenderAsGlyph('bonjour')).toBe(false)
  expect(shouldRenderAsGlyph('nei5 hou2')).toBe(false) // internal whitespace
})

test('empty / whitespace prompts are never glyphs', () => {
  expect(shouldRenderAsGlyph('')).toBe(false)
  expect(shouldRenderAsGlyph('   ')).toBe(false)
})

test('surrounding whitespace is trimmed before counting', () => {
  expect(shouldRenderAsGlyph('  字  ')).toBe(true)
})
