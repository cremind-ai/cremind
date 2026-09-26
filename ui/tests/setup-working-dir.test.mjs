// Which working directory the first-setup wizard sends for the admin.
//
// The server suggests `<System Directory>/workspaces/admin` before the admin
// has had a chance to change the System Directory on the same Server step. The
// wizard used to copy that suggestion into the field and always send it, so an
// admin who moved the System Directory still got a folder under the old one.
// The suggestion is now a placeholder only, and the payload carries a folder
// only when the admin typed a different one; blank leaves it to the server,
// which settles the default after relocating. (Every other profile's setup
// never picks its folder here — the admin chose it on Settings → Profiles.)
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, load } from './harness.mjs'

installBrowser()
const { chosenWorkingDir } = await load('src/utils/setupWorkingDir.ts')

const SUGGESTED = '/home/lee/.cremind/workspaces/admin'

test('an untouched field sends nothing: the server settles the default after any System Directory move', () => {
  assert.equal(chosenWorkingDir('', SUGGESTED), null)
  assert.equal(chosenWorkingDir('   ', SUGGESTED), null)
})

test('the suggestion typed back is still the server\'s call, not a pinned path', () => {
  assert.equal(chosenWorkingDir(SUGGESTED, SUGGESTED), null)
  assert.equal(chosenWorkingDir(`  ${SUGGESTED} `, SUGGESTED), null)
})

test('a folder the admin typed is sent, trimmed', () => {
  assert.equal(chosenWorkingDir(' /data/admin ', SUGGESTED), '/data/admin')
  assert.equal(chosenWorkingDir('/data/admin', null), '/data/admin')
  assert.equal(chosenWorkingDir('/data/admin', undefined), '/data/admin')
})

test('with no suggestion (an older server) a blank field still sends nothing', () => {
  assert.equal(chosenWorkingDir('', null), null)
})
