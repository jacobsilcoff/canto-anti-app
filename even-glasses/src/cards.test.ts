import { expect, test } from 'vitest'
import { buildView, playableCards } from './cards.js'
import type { DueCard } from './api.js'

function card(p: Partial<DueCard>): DueCard {
  return {
    card_id: 1,
    face: 'target',
    source_text: 'hello',
    target_text: '你好',
    romanization: 'nei5 hou2',
    target_lang: 'yue',
    notes: null,
    ...p,
  }
}

const ROMANIZABLE = new Set(['yue', 'cmn', 'ja', 'ko', 'hi', 'ru', 'th'])

test('Latin pronunciation faces (audio-only) are skipped', () => {
  const cards = [
    card({ face: 'source', target_lang: 'fr', romanization: null }),
    card({ face: 'target', target_lang: 'fr', romanization: null }),
    card({ face: 'pronunciation', target_lang: 'fr', romanization: null }), // audio-only -> drop
  ]
  const kept = playableCards(cards, ROMANIZABLE)
  expect(kept.map((c) => c.face)).toEqual(['source', 'target'])
})

test('non-Latin pronunciation faces are kept', () => {
  const cards = [card({ face: 'pronunciation', target_lang: 'yue' })]
  expect(playableCards(cards, ROMANIZABLE)).toHaveLength(1)
})

test('pronunciation face shows romanization as the prompt', () => {
  const v = buildView(card({ face: 'pronunciation', romanization: 'nei5 hou2' }))
  expect(v.prompt).toBe('nei5 hou2')
  expect(v.answer).toContain('你好')
  expect(v.answer).toContain('hello')
})

test('target face reveals meaning + romanization; romanization not in prompt', () => {
  const v = buildView(card({ face: 'target' }))
  expect(v.prompt).toBe('你好')
  expect(v.prompt).not.toContain('nei5')
  expect(v.answer).toContain('hello')
  expect(v.answer).toContain('nei5 hou2')
})

test('source face prompts in English, reveals target + romanization', () => {
  const v = buildView(card({ face: 'source' }))
  expect(v.prompt).toBe('hello')
  expect(v.answer).toContain('你好')
  expect(v.answer).toContain('nei5 hou2')
})

test('Latin target face has no romanization anywhere', () => {
  const v = buildView(
    card({ face: 'target', target_lang: 'fr', target_text: 'bonjour', romanization: null }),
  )
  expect(v.prompt).toBe('bonjour')
  expect(v.answer).toBe('hello')
})
