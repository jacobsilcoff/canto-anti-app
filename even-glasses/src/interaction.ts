export type CardPhase = 'prompt' | 'reveal'
export type CardInput = 'press' | 'double-press' | 'swipe-up' | 'swipe-down'
export type CardAction = 'reveal' | 'good' | 'again' | 'back' | 'skip' | 'exit' | 'none'

export function cardAction(phase: CardPhase, input: CardInput): CardAction {
  if (phase === 'reveal') {
    if (input === 'press') return 'good'
    if (input === 'double-press') return 'again'
    return 'none'
  }

  if (input === 'press') return 'reveal'
  if (input === 'double-press') return 'exit'
  if (input === 'swipe-up') return 'back'
  return 'skip'
}
