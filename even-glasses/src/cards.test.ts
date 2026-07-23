import { expect, test } from 'vitest'
import type { DueCard } from './api.js'
import { buildView, playableCards } from './cards.js'

function card(overrides: Partial<DueCard> = {}): DueCard {
  return {
    card_id: 1,
    face: 'target',
    source_text: 'hello',
    target_text: '你好',
    romanization: 'nei5 hou2',
    target_lang: 'yue',
    notes: null,
    ...overrides,
  }
}

test('audio-only Latin pronunciation cards are skipped', () => {
  const cards = [
    card({ face: 'source', target_lang: 'fr', romanization: null }),
    card({ face: 'target', target_lang: 'fr', romanization: null }),
    card({ face: 'pronunciation', target_lang: 'fr', romanization: null }),
  ]
  expect(playableCards(cards, new Set(['yue']))).toHaveLength(2)
})

test('romanized pronunciation cards remain playable', () => {
  expect(playableCards([card({ face: 'pronunciation' })], new Set(['yue']))).toHaveLength(1)
})

test('target cards reveal meaning with the reading under the prompt', () => {
  const view = buildView(card())
  expect(view.prompt).toBe('你好')
  expect(view.reading).toBe('nei5 hou2')
  expect(view.answer).toBe('hello')
})

test('target cards without romanization omit the reading line', () => {
  const view = buildView(card({ romanization: null }))
  expect(view.reading).toBeUndefined()
  expect(view.answer).toBe('hello')
})

test('source cards reveal the target and romanization', () => {
  const view = buildView(card({ face: 'source' }))
  expect(view.prompt).toBe('hello')
  expect(view.answer).toBe('你好\nnei5 hou2')
})
