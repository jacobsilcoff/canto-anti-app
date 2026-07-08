/**
 * Pure helpers for turning a face card into what shows on the glasses HUD.
 * No SDK / network here so it stays unit-testable.
 *
 * Face semantics (mirrors the web study page):
 *   source        - prompt is the native word (English); recall the target.
 *   target        - prompt is the target word; recall the meaning.
 *   pronunciation - normally an audio "listen and identify" challenge. The
 *                   glasses have no speaker, so:
 *                     - non-Latin (has romanization): prompt IS the romanization.
 *                     - Latin (no romanization): audio-only -> skip entirely.
 *
 * Romanization is shown wherever it exists (as the prompt on the pronunciation
 * face, in the reveal on the source/target faces), since without audio it is
 * the learner's only pronunciation cue.
 */
import type { DueCard, Face } from './api.js'

export interface CardView {
  hint: string   // short label for what's being tested
  prompt: string // shown before reveal
  answer: string // shown after reveal (below the prompt)
}

/**
 * Drop pronunciation faces for Latin-script languages (audio-only, and the
 * glasses can't play audio). `romanizable` = languages that have romanization.
 */
export function playableCards(cards: DueCard[], romanizable: Set<string>): DueCard[] {
  return cards.filter(
    (c) => !(c.face === 'pronunciation' && !romanizable.has(c.target_lang)),
  )
}

const HINTS: Record<Face, string> = {
  source: 'Recall the word',
  target: 'What does it mean?',
  pronunciation: 'Read it aloud',
}

export function buildView(card: DueCard): CardView {
  const rom = (card.romanization || '').trim()
  const hint = HINTS[card.face]

  if (card.face === 'source') {
    return {
      hint,
      prompt: card.source_text,
      answer: rom ? `${card.target_text}\n${rom}` : card.target_text,
    }
  }
  if (card.face === 'target') {
    return {
      hint,
      prompt: card.target_text,
      answer: rom ? `${card.source_text}\n(${rom})` : card.source_text,
    }
  }
  // pronunciation face (only reached for romanizable languages)
  return {
    hint,
    prompt: rom || card.target_text,
    answer: `${card.target_text}\n${card.source_text}`,
  }
}
