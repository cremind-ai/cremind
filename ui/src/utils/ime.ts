/**
 * Whether a key event belongs to an input method that is still composing — the
 * underlined text a Vietnamese Telex, Japanese, Chinese or Korean keyboard is
 * building. Enter, Tab, Escape and the arrows are the input method's then
 * (commit the word, pick a candidate, cancel), not a shortcut of ours.
 *
 * Acting on one sends a message twice on macOS: Chromium reports the Enter that
 * commits a Telex word as TWO keydowns — this one first, while composing, then
 * the real Enter once the word is in. Sending on the first clears the box, the
 * committed word lands in the empty box, and the second sends that word alone.
 *
 * `isComposing` is the standard signal. Safari ends the composition before it
 * dispatches the keydown that confirmed it, so there the flag is already false
 * and the legacy `keyCode` 229 ("handled by the input method") is the only
 * thing that tells that event from a real Enter.
 */
export function isImeKeyEvent(event: Pick<KeyboardEvent, 'isComposing' | 'keyCode'>): boolean {
  return event.isComposing || event.keyCode === 229;
}
