import { expect, test } from 'vitest'
import {
  MAX_LARGE_TEXT_CODEPOINTS,
  gray8ToGray4,
  shouldRenderLarge,
} from './glyph.js'

test('short words and short phrases use the large-text path', () => {
  expect(shouldRenderLarge('字')).toBe(true)
  expect(shouldRenderLarge('你好嗎')).toBe(true)
  expect(shouldRenderLarge('bonjour')).toBe(true)
  expect(shouldRenderLarge('thank you')).toBe(true)
})

test('long or multiline prompts remain native text', () => {
  expect(shouldRenderLarge('x'.repeat(MAX_LARGE_TEXT_CODEPOINTS + 1))).toBe(false)
  expect(shouldRenderLarge('line one\nline two')).toBe(false)
  expect(shouldRenderLarge('   ')).toBe(false)
})

test('gray conversion always produces G2 gray4 values', () => {
  expect(gray8ToGray4(-10)).toBe(0)
  expect(gray8ToGray4(0)).toBe(0)
  expect(gray8ToGray4(128)).toBe(8)
  expect(gray8ToGray4(255)).toBe(15)
  expect(gray8ToGray4(999)).toBe(15)
})
