// `/settings/tags` has no profile in it (Cremind Connect and the docs link to
// it). The router sends it to the current profile's Settings → Tags — the
// profile this tab uses, else the first signed-in one — and with nobody
// signed in to the profile selector, which lands on that page after sign-in.
// Only the exact known path maps: the existing redirect rules stay as strict.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, load } from './harness.mjs'

installBrowser()
const r = await load('src/utils/loginRedirect.ts')

test('the current profile wins, then the first signed-in one, else the selector', () => {
  assert.deepEqual(r.currentProfilePath('/settings/tags', { profileId: 'bob', loggedIn: ['ann', 'bob'] }),
    { path: '/bob/settings/tags' })
  assert.deepEqual(r.currentProfilePath('/settings/tags', { profileId: '', loggedIn: ['ann', 'bob'] }),
    { path: '/ann/settings/tags' })
  assert.deepEqual(r.currentProfilePath('/settings/tags', { profileId: '', loggedIn: [] }),
    { path: '/', query: { redirect: '/settings/tags' } })
  assert.deepEqual(r.currentProfilePath('/settings/tags', { profileId: 'a b', loggedIn: [] }),
    { path: '/a%20b/settings/tags' })
})

test('after sign-in the selector maps only the profile-less path', () => {
  assert.equal(r.profileNeutralTarget('/settings/tags', 'ann'), '/ann/settings/tags')
  assert.equal(r.profileNeutralTarget('/settings/tags?x=1', 'ann'), null)
  assert.equal(r.profileNeutralTarget('/settings', 'ann'), null)
  assert.equal(r.profileNeutralTarget('//evil.example/settings/tags', 'ann'), null)
  assert.equal(r.profileNeutralTarget('/settings/tags', ''), null)
  assert.equal(r.profileNeutralTarget(undefined, 'ann'), null)
  // The strict rule for profile-scoped paths is unchanged.
  assert.equal(r.safeRedirectTarget('/settings/tags', 'ann'), null)
  assert.equal(r.safeRedirectTarget('/ann/settings/tags', 'ann'), '/ann/settings/tags')
  assert.equal(r.safeRedirectTarget('/bob/settings/tags', 'ann'), null)
})
