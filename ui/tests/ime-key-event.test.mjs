// Which keydowns belong to an input method rather than to the composer.
//
// The bug this exists for: on macOS, with a Vietnamese Telex keyboard, pressing
// Enter while the last word was still underlined sent the message and then sent
// that word again as a message of its own — or, on a new chat, opened two
// conversations. Chromium reports that one key press as two keydowns, and the
// composer acted on both. The events below are the ones each browser sends.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const { isImeKeyEvent } = await load('src/utils/ime.ts')

test('a plain Enter is the composer\'s', () => {
  assert.equal(isImeKeyEvent({ key: 'Enter', keyCode: 13, isComposing: false }), false)
})

test('Chromium on macOS: the Enter that commits a word arrives twice, and only the second counts', () => {
  const committing = { key: 'Enter', keyCode: 229, isComposing: true }
  const real = { key: 'Enter', keyCode: 13, isComposing: false }
  assert.deepEqual([committing, real].map(isImeKeyEvent), [true, false])
})

test('Chromium on Windows and Firefox report the committing key as composing', () => {
  assert.equal(isImeKeyEvent({ key: 'Process', keyCode: 229, isComposing: true }), true)
  assert.equal(isImeKeyEvent({ key: 'Enter', keyCode: 13, isComposing: true }), true)
})

test('Safari ends the composition first, leaving only keyCode 229 to go by', () => {
  assert.equal(isImeKeyEvent({ key: 'Enter', keyCode: 229, isComposing: false }), true)
})

test('the keys that steer a candidate list are the input method\'s too', () => {
  for (const key of ['ArrowDown', 'ArrowUp', 'Tab', 'Escape']) {
    assert.equal(isImeKeyEvent({ key, keyCode: 229, isComposing: true }), true, key)
  }
})
