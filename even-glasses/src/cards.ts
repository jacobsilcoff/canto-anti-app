import type { DueCard, Face } from './api.js'

export interface CardView {
  hint: string
  prompt: string
  answer: string
}

export function playableCards(cards: DueCard[], romanizable: Set<string>): DueCard[] {
  return cards.filter(
    (card) => card.face !== 'pronunciation' || romanizable.has(card.target_lang),
  )
}

const HINTS: Record<Face, string> = {
  source: 'Recall the word',
  target: 'What does it mean?',
  pronunciation: 'Read it aloud',
}

export function buildView(card: DueCard): CardView {
  const romanization = (card.romanization || '').trim()
  const hint = HINTS[card.face]

  if (card.face === 'source') {
    return {
      hint,
      prompt: card.source_text,
      answer: romanization ? `${card.target_text}\n${romanization}` : card.target_text,
    }
  }
  if (card.face === 'target') {
    return {
      hint,
      prompt: card.target_text,
      answer: romanization ? `${card.source_text}\n(${romanization})` : card.source_text,
    }
  }
  return {
    hint,
    prompt: romanization || card.target_text,
    answer: `${card.target_text}\n${card.source_text}`,
  }
}
