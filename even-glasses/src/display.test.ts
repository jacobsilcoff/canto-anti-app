import { expect, test } from 'vitest'

import { safeContainerContent } from './display.js'

test('empty text updates use a space so hardware clears the container', () => {
  expect(safeContainerContent('')).toBe(' ')
})

test('nonempty text updates are unchanged', () => {
  expect(safeContainerContent('Got it  +5 XP')).toBe('Got it  +5 XP')
})
