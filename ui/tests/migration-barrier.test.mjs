// The pre-activation barrier (ui/src/services/migrationReadiness.ts).
//
// Before a tab activates HTTPS it asks the other tabs of this browser to finish
// their uploads and save a handoff. That wait makes no network request at all,
// so anything it blocks on is invisible: the Settings page sat on an unchanged
// card, its button dimmed, for as long as the wait lasted — which is exactly
// how a user reported it ("I click Show deployment commands and nothing happens,
// I tried many times"). The regressions pinned here:
//   - a sibling that never answers costs seconds, not the five-minute upload
//     deadline: the switch goes ahead and the SERVER's own readiness round —
//     which is the actual guarantee, and which the page can see — keeps waiting;
//   - a sibling that reports failure still stops the switch, with its reason;
//   - an abandoned round cannot cancel the barrier of the round that replaced
//     it (both register under the same transition id).
import assert from 'node:assert/strict'
import test from 'node:test'

import { advance, flush, installBrowser, load } from './harness.mjs'

// Delivers between channels of the same name, never back to the sender —
// enough to stand two tabs' listeners up against each other in one process.
class Channels {
  static open = []
  static reset() { Channels.open = [] }
  constructor(name) {
    this.name = name
    this.closed = false
    this.onmessage = null
    this.listeners = []
    Channels.open.push(this)
  }
  addEventListener(type, fn) { if (type === 'message') this.listeners.push(fn) }
  removeEventListener(type, fn) {
    if (type !== 'message') return
    const i = this.listeners.indexOf(fn)
    if (i >= 0) this.listeners.splice(i, 1)
  }
  postMessage(data) {
    for (const other of Channels.open) {
      if (other === this || other.closed || other.name !== this.name) continue
      other.onmessage?.({ data })
      for (const fn of [...other.listeners]) fn({ data })
    }
  }
  close() { this.closed = true }
}

/** A second tab that hears the barrier, says "seen", and answers as told. */
function sibling(answer) {
  const channel = new Channels('cremind:https-migration-readiness')
  const seen = []
  channel.onmessage = ({ data }) => {
    if (data.kind !== 'request') return
    seen.push(data)
    channel.postMessage({ ...data, kind: 'seen', tabId: 'sibling' })
    if (answer) channel.postMessage({ ...data, kind: answer, tabId: 'sibling' })
  }
  return { channel, seen }
}

function setup(t) {
  installBrowser()
  globalThis.BroadcastChannel = Channels
  Channels.reset()
  t.mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'], now: 1_800_000_000_000 })
  return t.mock.timers
}

test('a sibling that never answers delays the switch by seconds, not minutes', async (t) => {
  const timers = setup(t)
  const mod = await load('src/services/migrationReadiness.ts')
  const other = sibling(null) // answers "seen", then goes quiet (mid-upload, frozen…)

  let settled = false
  const barrier = mod.waitForBrowserMigrationReady('tx-1').then((nonce) => {
    settled = true
    return nonce
  })

  await advance(timers, 1_000)
  assert.equal(other.seen.length > 0, true, 'the sibling must have heard the barrier')
  assert.equal(settled, false, 'it still gives the sibling a moment')

  await advance(timers, 20_000)
  assert.equal(settled, true, 'the wait has to end well inside the five-minute upload deadline')
  assert.match(await barrier, /^[a-z0-9-]+/)
})

test('a sibling that answers is not waited out at all', async (t) => {
  const timers = setup(t)
  const mod = await load('src/services/migrationReadiness.ts')
  sibling('ready')

  let settled = false
  void mod.waitForBrowserMigrationReady('tx-1').then(() => { settled = true })
  await advance(timers, 2_000)
  assert.equal(settled, true)
})

test('a sibling that reports failure stops the switch and says why', async (t) => {
  const timers = setup(t)
  const mod = await load('src/services/migrationReadiness.ts')
  sibling('failed')

  let error = null
  void mod.waitForBrowserMigrationReady('tx-1').catch((e) => { error = e })
  await advance(timers, 2_000)
  assert.match(String(error), /could not save a fresh HTTPS handoff/)
})

test('an abandoned round cannot release the round that replaced it', async (t) => {
  const timers = setup(t)
  const mod = await load('src/services/migrationReadiness.ts')
  const other = sibling(null)
  const releases = []
  const watcher = new Channels('cremind:https-migration-readiness')
  watcher.onmessage = ({ data }) => { if (data.kind === 'release') releases.push(data.nonce) }

  const first = await (async () => {
    const promise = mod.waitForBrowserMigrationReady('tx-1')
    await advance(timers, 20_000)
    return promise
  })()
  const second = await (async () => {
    const promise = mod.waitForBrowserMigrationReady('tx-1')
    await advance(timers, 20_000)
    return promise
  })()
  assert.notEqual(first, second)
  assert.equal(other.seen.length >= 2, true)

  // The first round, long superseded, tidies up: it must not broadcast the
  // second round's nonce, or every sibling stops answering the live barrier.
  mod.releaseBrowserMigration('tx-1', first)
  assert.deepEqual(releases, [], 'a superseded round releases nothing')

  mod.releaseBrowserMigration('tx-1', second)
  assert.deepEqual(releases, [second], 'the current round releases its own')
})

test.afterEach(async () => {
  Channels.reset()
  await flush(1)
})
