import { expect, test } from 'vitest'
import { cardAction } from './interaction.js'

test('the front uses press to reveal and double press to exit', () => {
  expect(cardAction('prompt', 'press')).toBe('reveal')
  expect(cardAction('prompt', 'double-press')).toBe('exit')
})

test('front swipes browse unreviewed cards', () => {
  expect(cardAction('prompt', 'swipe-up')).toBe('back')
  expect(cardAction('prompt', 'swipe-down')).toBe('skip')
})

test('the revealed card uses presses for grading and leaves swipes to scrolling', () => {
  expect(cardAction('reveal', 'press')).toBe('good')
  expect(cardAction('reveal', 'double-press')).toBe('again')
  expect(cardAction('reveal', 'swipe-up')).toBe('none')
  expect(cardAction('reveal', 'swipe-down')).toBe('none')
})
