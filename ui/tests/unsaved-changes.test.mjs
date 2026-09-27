// What counts as an unsaved change on a settings page.
//
// Every settings page saves from one bar (SettingsSaveBar), lit while the form
// differs from what was last saved. "Differs" is a comparison of values, not a
// record of touched fields: a field typed back to its saved value is no change,
// and two equal forms whose keys were set in another order are equal — or the
// bar would light, and leaving the page would ask, over nothing.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const { stableStringify, useSavedSnapshot, ref, nextTick } = await load('tests/unsaved-changes-entry.ts')

test('equal states serialize the same whatever order their keys were set in', () => {
  assert.equal(
    stableStringify({ b: 1, a: { d: [3, { y: 1, x: 2 }], c: null } }),
    stableStringify({ a: { c: null, d: [3, { x: 2, y: 1 }] }, b: 1 }),
  )
  // Arrays keep their order: a reordered list is a change.
  assert.notEqual(stableStringify([1, 2]), stableStringify([2, 1]))
  // An absent key and an undefined one are the same form.
  assert.equal(stableStringify({ a: 1, b: undefined }), stableStringify({ a: 1 }))
  assert.equal(stableStringify(undefined), '')
})

test('a form is dirty while it differs from what was saved, and not after it is typed back', async () => {
  const form = ref({ name: 'Ops', hops: 6, members: ['admin'] })
  const snap = useSavedSnapshot(() => form.value)
  assert.equal(snap.dirty.value, false)

  form.value.hops = 7
  await nextTick()
  assert.equal(snap.dirty.value, true)

  form.value.hops = 6
  await nextTick()
  assert.equal(snap.dirty.value, false, 'typed back to the saved value')

  // A whole new object with the same content, keys in another order.
  form.value = { members: ['admin'], hops: 6, name: 'Ops' }
  await nextTick()
  assert.equal(snap.dirty.value, false)
})

test('commit takes the form as saved; saved() hands back a copy for Discard', async () => {
  const form = ref({ name: 'Ops', members: ['admin'] })
  const snap = useSavedSnapshot(() => form.value)
  form.value.members.push('bob')
  await nextTick()
  assert.equal(snap.dirty.value, true)

  const before = snap.saved()
  assert.deepEqual(before, { members: ['admin'], name: 'Ops' })
  before.members.push('carol')
  assert.deepEqual(snap.saved().members, ['admin'], 'saved() is a copy, not the baseline itself')

  snap.commit()
  await nextTick()
  assert.equal(snap.dirty.value, false)
  assert.deepEqual(snap.saved().members, ['admin', 'bob'])

  // Discard: the form back to what was saved.
  form.value.name = 'Changed'
  await nextTick()
  assert.equal(snap.dirty.value, true)
  form.value = snap.saved()
  await nextTick()
  assert.equal(snap.dirty.value, false)
})
