// The file tree's index status and the "Indexed content" preview.
//
// A scanned PDF used to look "indexed" in every listing while its only stored
// chunk was its file card — name, path, size — and research then found nothing
// to read in it. The tree now shows, per file, what the index actually holds
// (a content badge from the server's summary), and a preview shows the exact
// stored passages. These tests pin the pure half of that:
//
// - every badge and every lookup state has its own icon, label and tooltip;
// - lookups cover the files on screen only, in batches of at most 500, and a
//   burst of changes (a first sync sends many snapshots a second) costs one
//   lookup, not one per change;
// - an answer that lands after the profile or the directory changed is
//   dropped — one profile's statuses never show in another's tree;
// - the preview pages in source order, joins a passage split across two pages,
//   and starts over (never stitching two versions) when the file was
//   re-indexed in between.
import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, installBrowser, json, load } from './harness.mjs'

installBrowser()
const S = await load('src/utils/indexStatus.ts')
const api = await load('src/services/documentsApi.ts')

const AGENT = 'http://localhost:1515'

function summary(overrides = {}) {
  return {
    state: 'complete',
    phase: 'indexed',
    badge: 'indexed',
    headline: 'Indexed: 3 passages of text from 2 pages.',
    readable: { passages: 3, chars: 120 },
    segments: { text: 3, ocr: 0, image_description: 0, metadata: 1 },
    chars: { text: 120, ocr: 0, image_description: 0, metadata: 30 },
    indexed_at: 1_780_000_000_000,
    pages: null,
    reasons: [],
    embedding: { state: 'ready', ready: 4, total: 4 },
    refresh_queued: false,
    previewable: true,
    ...overrides,
  }
}

function found(overrides = {}, summaryOverrides = {}) {
  return {
    state: 'indexed', fid: 'k7m2xq9a', name: 'a.pdf', rel_path: 'a.pdf', kind: 'pdf', status: 'indexed',
    summary: summary(summaryOverrides), ...overrides,
  }
}

// ── what each status looks like ─────────────────────────────────────────────

const BADGES = ['waiting', 'indexing', 'indexed', 'partial', 'metadata_only', 'blocked', 'failed', 'unknown', 'unavailable']

const EXPECTED_LABELS = {
  waiting: 'Waiting to be indexed',
  indexing: 'Indexing…',
  indexed: 'Indexed content',
  partial: 'Partly indexed',
  metadata_only: 'Metadata only',
  blocked: 'Blocked',
  failed: 'Failed',
  unknown: 'Index status unknown',
  unavailable: 'Not available',
  outside: 'Not in the indexed folder',
  excluded: 'Excluded from indexing',
  unmatched: 'Not indexed yet',
  gone: 'Removed from the index',
  status_unavailable: 'Index status unavailable',
}

test('every badge and lookup state has its own label, icon and tone', () => {
  const kinds = Object.keys(S.INDEX_STATUS_PRESENTATION)
  assert.deepEqual(kinds.sort(), Object.keys(EXPECTED_LABELS).sort())
  for (const [kind, label] of Object.entries(EXPECTED_LABELS)) {
    const look = S.INDEX_STATUS_PRESENTATION[kind]
    assert.equal(look.label, label, kind)
    assert.match(look.icon, /^mdi:/, kind)
    assert.ok(['ok', 'info', 'warn', 'danger', 'muted'].includes(look.tone), kind)
  }
  const icons = kinds.map(k => S.INDEX_STATUS_PRESENTATION[k].icon)
  assert.equal(new Set(icons).size, icons.length, 'no two statuses share an icon')
})

test('an indexed file shows its summary badge, with the headline in the tooltip', () => {
  for (const badge of BADGES) {
    const headline = `Headline for ${badge}.`
    const view = S.describeIndexStatus(found({}, { badge, headline }))
    assert.equal(view.kind, badge)
    assert.equal(view.label, EXPECTED_LABELS[badge])
    assert.equal(view.fid, 'k7m2xq9a')
    assert.equal(view.detail, headline)
    assert.ok(view.tooltip.includes(EXPECTED_LABELS[badge]) && view.tooltip.includes(headline), view.tooltip)
    assert.equal(view.summary.badge, badge)
  }
  // A badge this client does not know, or no summary at all, is "unknown".
  assert.equal(S.describeIndexStatus(found({}, { badge: 'shiny' })).kind, 'unknown')
  assert.equal(S.describeIndexStatus(found({ summary: null })).kind, 'unknown')
})

test('a path that is not in the index says why, and carries no file id', () => {
  const cases = [
    [{ state: 'outside', reason: 'outside_root' }, 'outside', /outside it/],
    [{ state: 'outside', reason: 'system' }, 'outside', /system folder/],
    [{ state: 'excluded', reason: 'excluded' }, 'excluded', /exclusion rule/],
    [{ state: 'unmatched', reason: 'not_indexed' }, 'unmatched', /not in the index yet/],
    [{ state: 'unmatched', reason: 'root_unavailable' }, 'status_unavailable', /not available right now/],
    [{ state: 'unmatched', reason: 'no_index' }, 'status_unavailable', /not been built/],
    [{ state: 'gone', reason: 'tombstone' }, 'gone', /removed from the index/],
    [{ state: 'gone', reason: 'missing' }, 'gone', /disappeared/],
    [{ state: 'error', reason: null }, 'status_unavailable', /could not be checked/],
  ]
  for (const [entry, kind, detail] of cases) {
    const view = S.describeIndexStatus(entry)
    assert.equal(view.kind, kind, JSON.stringify(entry))
    assert.equal(view.label, EXPECTED_LABELS[kind])
    assert.match(view.detail, detail)
    assert.equal(view.fid, null)
    assert.equal(view.summary, null)
  }
  assert.equal(S.describeIndexStatus(null), null)
  assert.equal(S.describeIndexStatus(undefined), null)
  assert.equal(S.describeIndexStatus({}), null)
  // Another profile's folder (a group seat working there): no button at all.
  assert.equal(S.describeIndexStatus({ state: 'outside', reason: 'foreign' }), null)
})

test('the reasons under the headline never repeat it', () => {
  const summary = {
    headline: 'Only file details were indexed. 8 pages are waiting for OCR: consent is needed.',
    reasons: [
      { code: 'awaiting_consent', message: '8 pages are waiting for OCR: consent is needed.' },
      { code: 'ocr_failed', message: '1 page could not be transcribed; it is retried.' },
    ],
  }
  assert.deepEqual(S.reasonsBeyondHeadline(summary).map(r => r.code), ['ocr_failed'])
  assert.deepEqual(S.reasonsBeyondHeadline(null), [])
})

test('the status button names the file, the state and what it does', () => {
  const view = S.describeIndexStatus(found({}, { badge: 'partial' }))
  const label = S.indexStatusAriaLabel(view, 'Nghị định 13.pdf')
  assert.ok(label.includes('Nghị định 13.pdf'))
  assert.ok(label.includes('Partly indexed'))
  assert.match(label, /Show what is indexed/)
})

test('the settings table reads the same badge from a row summary', () => {
  assert.equal(S.describeSummary(null), null)
  const view = S.describeSummary(summary({ badge: 'metadata_only' }), 'k7m2xq9a')
  assert.equal(view.kind, 'metadata_only')
  assert.equal(view.fid, 'k7m2xq9a')
})

test('the local folder switch comes from the snapshot, unknown until one says', () => {
  assert.equal(S.localSourceEnabled(null), null)
  assert.equal(S.localSourceEnabled({ v: 1, enabled: true, state: 'idle', reason: null }), null)
  const snap = (local) => ({ v: 1, enabled: true, state: 'idle', reason: null, sources: { local, drive: null } })
  assert.equal(S.localSourceEnabled(snap({ enabled: true, root: '/w', first_sync_confirmed: true })), true)
  assert.equal(S.localSourceEnabled(snap({ enabled: false, root: '/w', first_sync_confirmed: true })), false)
  // Drive on, local folder never set up: the tree shows nothing.
  assert.equal(S.localSourceEnabled(snap(null)), false)
})

// ── batching ────────────────────────────────────────────────────────────────

test('paths are looked up in batches of at most 500, once each, in order', () => {
  const paths = []
  for (let i = 0; i < 1203; i += 1) paths.push(`/w/f${i}.txt`)
  const chunks = S.chunkPaths([...paths, '/w/f0.txt', '', '/w/f7.txt'])
  assert.deepEqual(chunks.map(c => c.length), [500, 500, 203])
  assert.deepEqual(chunks.flat(), paths)
  // A larger batch than the server takes is capped.
  assert.deepEqual(S.chunkPaths(paths, 5000).map(c => c.length), [500, 500, 203])
  const five = paths.slice(0, 5)
  assert.deepEqual(S.chunkPaths(five, 2), [five.slice(0, 2), five.slice(2, 4), five.slice(4)])
  assert.deepEqual(S.chunkPaths([]), [])
})

test('only files are looked up — a folder would come back unmatched anyway', () => {
  const entries = [
    { path: '/w/docs', is_dir: true },
    { path: '/w/a.pdf', is_dir: false },
    { path: '/w/b.txt', is_dir: false },
    { path: '', is_dir: false },
  ]
  assert.deepEqual(S.lookupCandidates(entries), ['/w/a.pdf', '/w/b.txt'])
})

// ── the lookup controller ───────────────────────────────────────────────────

function lookupStub() {
  const calls = []
  return {
    calls,
    fn(paths) {
      const d = deferred()
      calls.push({ paths, ...d })
      return d.promise
    },
  }
}

function answer(paths, fid = 'k7m2xq9a', extra = {}) {
  return {
    enabled: true,
    root: '/w',
    available: true,
    items: Object.fromEntries(paths.map(p => [p, found({ fid })])),
    ...extra,
  }
}

function controller(t, opts = {}) {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_800_000_000_000 })
  const stub = lookupStub()
  const c = new S.IndexLookupController({ lookup: stub.fn, ...opts })
  c.setSession('tok-ann\u0000/w')
  return { c, stub, timers: t.mock.timers }
}

test('rows that appear together are looked up together, after a short debounce', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/a.pdf')
  c.register('/w/b.pdf')
  timers.tick(40)
  c.register('/w/c.pdf')
  timers.tick(S.LOOKUP_DEBOUNCE_MS - 41)
  assert.equal(stub.calls.length, 0)
  timers.tick(1)
  assert.equal(stub.calls.length, 1)
  assert.deepEqual(stub.calls[0].paths, ['/w/a.pdf', '/w/b.pdf', '/w/c.pdf'])

  stub.calls[0].resolve(answer(stub.calls[0].paths))
  await flush()
  assert.equal(c.state.enabled, true)
  assert.equal(c.cache.get('/w/b.pdf').fid, 'k7m2xq9a')
  // Already looked up this session: showing it again costs nothing.
  c.unregister('/w/b.pdf')
  c.register('/w/b.pdf')
  timers.tick(1000)
  assert.equal(stub.calls.length, 1)
  c.dispose()
})

test('a big folder is looked up 500 paths at a time, one request after another', async (t) => {
  const { c, stub, timers } = controller(t)
  for (let i = 0; i < 1200; i += 1) c.register(`/w/f${i}.txt`)
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  assert.equal(stub.calls.length, 1)
  assert.equal(stub.calls[0].paths.length, 500)
  stub.calls[0].resolve(answer(stub.calls[0].paths))
  await flush()
  assert.equal(stub.calls.length, 2)
  assert.equal(stub.calls[1].paths.length, 500)
  stub.calls[1].resolve(answer(stub.calls[1].paths))
  await flush()
  assert.equal(stub.calls.length, 3)
  assert.equal(stub.calls[2].paths.length, 200)
  stub.calls[2].resolve(answer(stub.calls[2].paths))
  await flush()
  assert.equal(c.cache.size, 1200)
  for (const call of stub.calls) assert.ok(call.paths.length <= 500)
  c.dispose()
})

test('a burst of index changes costs one lookup, at most one per refresh interval', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/a.pdf')
  c.register('/w/b.pdf')
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  stub.calls[0].resolve(answer(stub.calls[0].paths))
  await flush()

  for (let i = 0; i < 20; i += 1) c.requestRefresh()
  timers.tick(S.LOOKUP_REFRESH_MS - 1)
  assert.equal(stub.calls.length, 1, 'throttled')
  timers.tick(1)
  assert.equal(stub.calls.length, 2)
  assert.deepEqual(stub.calls[1].paths, ['/w/a.pdf', '/w/b.pdf'], 'every visible path again')

  // A change while that lookup is in flight: one more lookup after it, never
  // two at once — its answer may already be out of date.
  c.requestRefresh()
  c.requestRefresh()
  timers.tick(5000)
  assert.equal(stub.calls.length, 2, 'nothing starts while one is in flight')
  stub.calls[1].resolve(answer(stub.calls[1].paths))
  await flush()
  timers.tick(S.LOOKUP_REFRESH_MS)
  assert.equal(stub.calls.length, 3)
  stub.calls[2].resolve(answer(stub.calls[2].paths))
  await flush()
  timers.tick(10_000)
  assert.equal(stub.calls.length, 3, 'settled')
  c.dispose()
})

test('a refresh looks up only what is still on screen', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/a.pdf')
  c.register('/w/b.pdf')
  c.register('/w/b.pdf') // shown twice (two panels): counted once
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  stub.calls[0].resolve(answer(stub.calls[0].paths))
  await flush()

  c.unregister('/w/a.pdf')
  c.unregister('/w/b.pdf')
  assert.deepEqual(c.visiblePaths(), ['/w/b.pdf'])
  c.requestRefresh()
  timers.tick(S.LOOKUP_REFRESH_MS)
  assert.deepEqual(stub.calls[1].paths, ['/w/b.pdf'])
  stub.calls[1].resolve(answer(['/w/b.pdf']))
  await flush()
  c.dispose()
})

test('an answer that lands after the profile switched is dropped, and the new profile starts empty', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/ann.pdf')
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  assert.equal(stub.calls.length, 1)

  // Bob signs in; the tree's rows re-register under his session.
  assert.equal(c.setSession('tok-bob\u0000/w'), true)
  assert.equal(c.setSession('tok-bob\u0000/w'), false, 'the same session again changes nothing')
  assert.equal(c.state.enabled, null)
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  assert.equal(stub.calls.length, 2, "the new session does not wait for the old one's answer")

  stub.calls[1].resolve(answer(['/w/ann.pdf'], 'bobbbbbb'))
  await flush()
  assert.equal(c.cache.get('/w/ann.pdf').fid, 'bobbbbbb')
  // Ann's answer arrives last: it must not overwrite Bob's.
  stub.calls[0].resolve(answer(['/w/ann.pdf'], 'annnnnnn'))
  await flush()
  assert.equal(c.cache.get('/w/ann.pdf').fid, 'bobbbbbb')
  c.dispose()
})

test('an answer for the previous directory is dropped too', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/old/a.pdf')
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  c.setSession('tok-ann\u0000/w/new')
  c.unregister('/w/old/a.pdf')
  stub.calls[0].resolve(answer(['/w/old/a.pdf']))
  await flush()
  assert.equal(c.cache.size, 0)
  assert.equal(c.state.enabled, null)
  c.dispose()
})

test('with the local folder off nothing is shown or looked up, until it may be on again', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/a.pdf')
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  stub.calls[0].resolve({ enabled: false, root: null, items: {} })
  await flush()
  assert.equal(c.state.enabled, false)
  assert.equal(c.cache.size, 0)

  // Expanding a folder, a file changing: no request while it is off.
  c.register('/w/b.pdf')
  c.requestRefresh()
  timers.tick(10_000)
  assert.equal(stub.calls.length, 1)

  // A new snapshot that does not say "off": look again.
  c.sourceMaybeEnabled()
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  assert.equal(stub.calls.length, 2)
  assert.deepEqual(stub.calls[1].paths, ['/w/a.pdf', '/w/b.pdf'])
  stub.calls[1].resolve(answer(stub.calls[1].paths))
  await flush()
  assert.equal(c.state.enabled, true)
  assert.equal(c.cache.size, 2)
  c.dispose()
})

test('turning the local folder off drops what is shown and what is in flight', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/a.pdf')
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  stub.calls[0].resolve(answer(['/w/a.pdf']))
  await flush()
  c.requestRefresh()
  timers.tick(S.LOOKUP_REFRESH_MS)
  assert.equal(stub.calls.length, 2)

  c.sourceDisabled()
  assert.equal(c.state.enabled, false)
  assert.equal(c.cache.size, 0)
  stub.calls[1].resolve(answer(['/w/a.pdf']))
  await flush()
  assert.equal(c.cache.size, 0, 'the answer in flight is not shown')
  c.dispose()
})

test('a failed lookup shows "status unavailable" and is tried again later, not in a loop', async (t) => {
  const { c, stub, timers } = controller(t)
  c.register('/w/a.pdf')
  timers.tick(S.LOOKUP_DEBOUNCE_MS)
  stub.calls[0].reject(new TypeError('Failed to fetch'))
  await flush()
  assert.equal(S.describeIndexStatus(c.cache.get('/w/a.pdf')).kind, 'status_unavailable')
  timers.tick(S.LOOKUP_RETRY_MS - 1)
  assert.equal(stub.calls.length, 1, 'no retry loop')
  timers.tick(1) // the retry asks for a refresh…
  timers.tick(S.LOOKUP_DEBOUNCE_MS) // …which runs after the debounce
  assert.equal(stub.calls.length, 2)
  stub.calls[1].resolve(answer(['/w/a.pdf']))
  await flush()
  assert.equal(S.describeIndexStatus(c.cache.get('/w/a.pdf')).kind, 'indexed')
  c.dispose()
})

// ── the store: the session follows the signed-in profile ────────────────────

async function storeSetup() {
  const env = installBrowser()
  globalThis.window.cremind = { config: { agentUrl: AGENT } }
  const M = await load('tests/entries/index-status.ts')
  M.setActivePinia(M.createPinia())
  const settings = M.useSettingsStore()
  settings.authToken = 'tok-ann'
  const docs = M.useDocumentsStore()
  // As App.vue does: binds the documents store to the token (it drops any
  // snapshot it holds for another one). No route, so no transport opens.
  docs.connect(AGENT)
  const store = M.useIndexStatusStore()
  const pending = []
  env.route('/api/documentation-search/files/lookup', (_url, init) => {
    const d = deferred()
    pending.push({ body: JSON.parse(init.body), auth: init.headers.Authorization, ...d })
    return d.promise
  })
  return { env, M, settings, docs, store, pending }
}

function lookupAnswer(paths, fid) {
  return json(answer(paths, fid))
}

function snapshot(seq, localEnabled, overrides = {}) {
  return {
    v: 1, boot: 'b', seq, ts: 1_800_000_000_000 + seq, enabled: localEnabled,
    state: localEnabled ? 'idle' : 'disabled', reason: null,
    sources: { local: { enabled: localEnabled, root: '/w', first_sync_confirmed: true }, drive: null },
    ...overrides,
  }
}

test('store: a profile switch never shows the previous profile\'s statuses', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_800_000_000_000 })
  const { settings, store, pending } = await storeSetup()
  store.setDirectory('/w/ann')
  store.register('/w/ann/a.pdf')
  t.mock.timers.tick(S.LOOKUP_DEBOUNCE_MS)
  await flush()
  assert.equal(pending.length, 1)
  assert.equal(pending[0].auth, 'Bearer tok-ann')
  assert.deepEqual(pending[0].body, { paths: ['/w/ann/a.pdf'] })
  pending[0].resolve(lookupAnswer(['/w/ann/a.pdf'], 'annnnnnn'))
  await flush()
  assert.equal(store.statusFor('/w/ann/a.pdf').fid, 'annnnnnn')

  // Another request for Ann is on its way when Bob signs in.
  store.noteListingChanged()
  t.mock.timers.tick(S.LOOKUP_REFRESH_MS)
  await flush()
  assert.equal(pending.length, 2)
  settings.authToken = 'tok-bob'
  assert.equal(store.statusFor('/w/ann/a.pdf'), null, 'cleared at once')
  store.setDirectory('/w/bob')
  store.unregister('/w/ann/a.pdf')
  store.register('/w/bob/b.pdf')
  pending[1].resolve(lookupAnswer(['/w/ann/a.pdf'], 'annnnnnn'))
  await flush()
  assert.equal(store.statusFor('/w/ann/a.pdf'), null, "Ann's late answer is dropped")

  t.mock.timers.tick(S.LOOKUP_DEBOUNCE_MS)
  await flush()
  assert.equal(pending.length, 3)
  assert.equal(pending[2].auth, 'Bearer tok-bob')
  assert.deepEqual(pending[2].body, { paths: ['/w/bob/b.pdf'] })
  pending[2].resolve(lookupAnswer(['/w/bob/b.pdf'], 'bobbbbbb'))
  await flush()
  assert.equal(store.statusFor('/w/bob/b.pdf').fid, 'bobbbbbb')
})

test('store: the "Search my documents" switch shows and hides every status', async (t) => {
  t.mock.timers.enable({ apis: ['setTimeout', 'Date'], now: 1_800_000_000_000 })
  const { docs, store, pending } = await storeSetup()
  store.setDirectory('/w')
  store.register('/w/a.pdf')
  t.mock.timers.tick(S.LOOKUP_DEBOUNCE_MS)
  await flush()
  pending[0].resolve(lookupAnswer(['/w/a.pdf']))
  await flush()
  assert.equal(store.enabled, true)
  assert.equal(store.statusFor('/w/a.pdf').kind, 'indexed')

  docs.applySnapshot(snapshot(2, false))
  await flush()
  assert.equal(store.enabled, false)
  assert.equal(store.statusFor('/w/a.pdf'), null)
  store.register('/w/b.pdf')
  t.mock.timers.tick(10_000)
  await flush()
  assert.equal(pending.length, 1, 'nothing is looked up while it is off')

  docs.applySnapshot(snapshot(3, true))
  await flush()
  t.mock.timers.tick(S.LOOKUP_DEBOUNCE_MS)
  await flush()
  assert.equal(pending.length, 2)
  assert.deepEqual(pending[1].body.paths, ['/w/a.pdf', '/w/b.pdf'])
  pending[1].resolve(lookupAnswer(['/w/a.pdf', '/w/b.pdf']))
  await flush()
  assert.equal(store.statusFor('/w/b.pdf').kind, 'indexed')

  // A poll that finds the index as it was (new stamp, same content) looks
  // nothing up again.
  for (let seq = 4; seq < 10; seq += 1) docs.applySnapshot(snapshot(seq, true))
  await flush()
  t.mock.timers.tick(10_000)
  await flush()
  assert.equal(pending.length, 2)

  // A sync under way: every snapshot moves the index, and the statuses are
  // refreshed — throttled, however many arrive.
  for (let seq = 10; seq < 40; seq += 1) {
    docs.applySnapshot(snapshot(seq, true, { state: 'indexing', stages: { indexed: seq, dirty: 50 - seq } }))
  }
  await flush()
  t.mock.timers.tick(S.LOOKUP_DEBOUNCE_MS)
  await flush()
  assert.equal(pending.length, 3)
  assert.deepEqual(pending[2].body.paths, ['/w/a.pdf', '/w/b.pdf'])
  pending[2].resolve(lookupAnswer(['/w/a.pdf', '/w/b.pdf']))
  await flush()
  docs.applySnapshot(snapshot(40, true, { state: 'indexing', stages: { indexed: 40, dirty: 10 } }))
  await flush()
  t.mock.timers.tick(S.LOOKUP_REFRESH_MS - 1)
  await flush()
  assert.equal(pending.length, 3, 'at most one refresh per interval')
  t.mock.timers.tick(1)
  await flush()
  assert.equal(pending.length, 4)
})

test('a snapshot\'s content key ignores its stamp, nothing else', () => {
  const a = snapshot(1, true)
  assert.equal(S.snapshotContentKey(a), S.snapshotContentKey({ ...a, seq: 99, ts: 5, boot: 'other' }))
  assert.notEqual(S.snapshotContentKey(a), S.snapshotContentKey({ ...a, state: 'indexing' }))
  assert.notEqual(S.snapshotContentKey(a), S.snapshotContentKey(snapshot(1, false)))
  assert.equal(S.snapshotContentKey(null), '')
})

// ── the preview ─────────────────────────────────────────────────────────────

function seg(index, text, part) {
  const s = {
    token: `[doc:k7m2xq9a#${String(index).padStart(8, '0')}]`, index, ordinal: index, type: 'text',
    heading: '', locator: { page: index + 1 }, locator_label: `p. ${index + 1}`, text,
  }
  if (part) s.part = part
  return s
}

function page(overrides = {}) {
  return {
    fid: 'k7m2xq9a', name: 'Nghị định.pdf', rel_path: 'Luật/Nghị định.pdf', kind: 'pdf', source: 'local',
    status: 'indexed', summary: summary({ revision: 'r1' }),
    metadata: { card: 'File: Nghị định.pdf', document: { title: 'Nghị định 13' } },
    revision: 'r1', total_segments: 3, start_index: 0, segments: [], next_cursor: null,
    ...overrides,
  }
}

test('pages append in source order, and a passage split across two pages reads as one', () => {
  let s = S.previewBegin(S.EMPTY_PREVIEW, 'k7m2xq9a', true)
  assert.equal(s.loading, true)
  s = S.previewReceive(s, page({
    segments: [seg(0, 'Điều 1. Phạm vi'), seg(1, 'Khoản 2 bắt đầu ', { start: 0, end: 16, length: 31 })],
    next_cursor: 'c1',
  }), false)
  assert.equal(s.loading, false)
  assert.equal(s.nextCursor, 'c1')
  assert.equal(s.header.metadata.card, 'File: Nghị định.pdf')
  assert.equal(s.header.total_segments, 3)
  assert.equal('segments' in s.header, false, 'the passages are not part of the header')
  assert.equal(S.loadedPassages(s), 2)

  s = S.previewBegin(s, 'k7m2xq9a', false)
  assert.equal(s.segments.length, 2, 'load more keeps what is shown')
  s = S.previewReceive(s, page({
    start_index: 1,
    segments: [seg(1, 'và kết thúc ở đây', { start: 16, end: 31, length: 31 }), seg(2, 'Điều 3.')],
    next_cursor: null,
  }), true)
  assert.equal(s.segments.length, 3)
  assert.equal(s.segments[1].text, 'Khoản 2 bắt đầu và kết thúc ở đây', 'joined, Unicode kept')
  assert.equal(s.segments[1].part, undefined, 'whole again')
  assert.equal(s.nextCursor, null)
  assert.equal(S.loadedPassages(s), 3)
})

test('a passage split over three pages keeps its partial range until the last part', () => {
  const held = [seg(4, 'aaaa', { start: 0, end: 4, length: 10 })]
  const two = S.mergeSegments(held, [seg(4, 'bbb', { start: 4, end: 7, length: 10 })])
  assert.equal(two.length, 1)
  assert.deepEqual(two[0].part, { start: 0, end: 7, length: 10 })
  const three = S.mergeSegments(two, [seg(4, 'ccc', { start: 7, end: 10, length: 10 }), seg(5, 'next')])
  assert.equal(three.length, 2)
  assert.equal(three[0].text, 'aaaabbbccc')
  assert.equal(three[0].part, undefined)
  // Two identical passages are two passages: only a continuation joins.
  const dupes = S.mergeSegments([seg(6, 'same')], [seg(7, 'same')])
  assert.equal(dupes.length, 2)
})

test('a stale cursor starts over from the first page — never two versions stitched', () => {
  let s = S.previewReceive(S.previewBegin(S.EMPTY_PREVIEW, 'k7m2xq9a', true), page({
    segments: [seg(0, 'old one'), seg(1, 'old two')], next_cursor: 'c1',
  }), false)
  s = S.previewBegin(s, 'k7m2xq9a', false)
  s = S.previewStale(s)
  assert.deepEqual(s.segments, [])
  assert.equal(s.nextCursor, null)
  assert.equal(s.revision, null)
  assert.equal(s.loading, true)
  assert.equal(s.restarted, true)
  assert.equal(s.header.name, 'Nghị định.pdf', 'the header stays while the first page reloads')

  s = S.previewReceive(s, page({ revision: 'r2', segments: [seg(0, 'new one')], next_cursor: null }), false)
  assert.deepEqual(s.segments.map(x => x.text), ['new one'])
  assert.equal(s.revision, 'r2')
  assert.equal(s.restarted, true, 'the notice stays up')

  // A manual reload clears the notice.
  s = S.previewBegin(s, 'k7m2xq9a', true)
  assert.equal(s.restarted, false)
})

test('a page from another revision or file replaces what is shown instead of appending', () => {
  let s = S.previewReceive(S.EMPTY_PREVIEW, page({ segments: [seg(0, 'r1 text')], next_cursor: 'c1' }), false)
  s = S.previewReceive(s, page({ revision: 'r2', segments: [seg(0, 'r2 text')] }), true)
  assert.deepEqual(s.segments.map(x => x.text), ['r2 text'])
  s = S.previewBegin(s, 'zzzzzzzz', false)
  assert.equal(s.header, null, 'another file: nothing of the previous one stays')
  assert.deepEqual(s.segments, [])
})

test('a failed page keeps what is shown and says why', () => {
  let s = S.previewReceive(S.EMPTY_PREVIEW, page({ segments: [seg(0, 'kept')], next_cursor: 'c1' }), false)
  s = S.previewFailed(S.previewBegin(s, 'k7m2xq9a', false), 'Could not load', false)
  assert.equal(s.loading, false)
  assert.equal(s.error, 'Could not load')
  assert.equal(s.segments.length, 1)
  assert.equal(S.previewFailed(S.EMPTY_PREVIEW, 'gone', true).notFound, true)
})

// ── the preview header ──────────────────────────────────────────────────────

test('page coverage lists what extraction did with each page, zero counts left out', () => {
  assert.deepEqual(S.coverageParts(null), [])
  assert.deepEqual(S.coverageParts({
    total: 12, read: 12, text: 4, scanned: 8, ocr_done: 6, blank: 1, truncated: 0, pending: 1, failed: 0, unreadable: 0,
  }), ['12 pages', '4 with text', '6 OCR done', '1 blank', '1 pending OCR'])
  assert.deepEqual(S.coverageParts({
    total: 1, read: null, text: 0, scanned: 1, ocr_done: 0, blank: 0, truncated: 1, pending: 0, failed: 1, unreadable: 0,
  }), ['1 page', '1 OCR failed', '1 cut off'])
  assert.deepEqual(S.coverageParts({
    total: 500, read: 200, text: 200, scanned: 0, ocr_done: 0, blank: 0, truncated: 0, pending: 0, failed: 0, unreadable: 2,
  }), ['500 pages', '200 read', '200 with text', '2 unreadable'])
})

test('readable text and vector readiness read plainly', () => {
  assert.equal(S.readableText(summary()), '3 passages (120 characters)')
  assert.equal(S.readableText(summary({ readable: { passages: 1, chars: 1 } })), '1 passage (1 character)')
  assert.equal(S.readableText(summary({ readable: { passages: 0, chars: 0 } })), 'No readable text')
  assert.equal(S.embeddingText(summary()), null)
  assert.match(S.embeddingText(summary({ embedding: { state: 'partial', ready: 2, total: 4 } })), /2 of 4/)
  assert.match(S.embeddingText(summary({ embedding: { state: 'pending', ready: 0, total: 4 } })), /keyword search works/)
})

test('metadata rows keep the server order and spell out the legal fields', () => {
  const rows = S.metadataRows({
    title: 'Nghị định 13/2023/NĐ-CP', author: 'Chính phủ', keywords: ['dữ liệu', 'cá nhân'], pages: 32,
    legal: { number: '13/2023/NĐ-CP', type: 'Nghị định', issued: '2023-04-17', empty: '' },
    last_modified_by: 'An',
  })
  assert.deepEqual(rows, [
    { label: 'Title', value: 'Nghị định 13/2023/NĐ-CP' },
    { label: 'Author', value: 'Chính phủ' },
    { label: 'Keywords', value: 'dữ liệu, cá nhân' },
    { label: 'Pages', value: '32' },
    { label: 'Legal number', value: '13/2023/NĐ-CP' },
    { label: 'Legal type', value: 'Nghị định' },
    { label: 'Legal issued', value: '2023-04-17' },
    { label: 'Last modified by', value: 'An' },
  ])
  assert.deepEqual(S.metadataRows(null), [])
})

test('each reason links to where it is fixed', () => {
  assert.deepEqual(S.reasonLink('vision_model'), { kind: 'route', route: 'llm-settings', label: 'Choose a vision model' })
  assert.equal(S.reasonLink('vision_consent').route, 'documents-settings')
  assert.equal(S.reasonLink('install').route, 'documents-settings')
  assert.equal(S.reasonLink('retry').kind, 'retry')
  assert.equal(S.reasonLink('wait'), null)
  assert.equal(S.reasonLink(undefined), null)
})

test('OCR and image descriptions are tagged apart from the file\'s own text', () => {
  assert.equal(S.segmentTypeLabel('text'), null)
  assert.equal(S.segmentTypeLabel('ocr'), 'OCR')
  assert.equal(S.segmentTypeLabel('image_description'), 'Image description')
})

test('the summary signature moves when the stored content does, not on a mere phase change', () => {
  const base = summary()
  assert.equal(S.summarySignature(null), '')
  assert.equal(S.summarySignature(base), S.summarySignature({ ...base, phase: 'queued', refresh_queued: true }))
  assert.notEqual(S.summarySignature(base), S.summarySignature({ ...base, indexed_at: base.indexed_at + 1 }))
  assert.notEqual(S.summarySignature(base), S.summarySignature({ ...base, readable: { passages: 9, chars: 1 } }))
  assert.notEqual(S.summarySignature(base), S.summarySignature({ ...base, badge: 'partial', state: 'partial' }))
})

// ── the API client ──────────────────────────────────────────────────────────

test('a lookup posts the paths as sent and refuses more than 500 before any request', async () => {
  const env = installBrowser()
  env.route('/api/documentation-search/files/lookup', () => json({ enabled: false, root: null, items: {} }))
  const res = await api.lookupIndexedPaths(AGENT, 'tok', ['C:\\w\\a.pdf', '/w/b.txt'])
  assert.deepEqual(res, { enabled: false, root: null, items: {} })
  const [call] = env.callsTo('/files/lookup')
  assert.equal(call.init.method, 'POST')
  assert.equal(call.init.headers.Authorization, 'Bearer tok')
  assert.deepEqual(JSON.parse(call.init.body), { paths: ['C:\\w\\a.pdf', '/w/b.txt'] })

  const many = Array.from({ length: 501 }, (_, i) => `/w/${i}`)
  await assert.rejects(api.lookupIndexedPaths(AGENT, 'tok', many), RangeError)
  assert.equal(env.callsTo('/files/lookup').length, 1)
})

test('a preview page is asked for by cursor, with the limit capped at 60', async () => {
  const env = installBrowser()
  env.route('/preview', () => json(page()))
  await api.getFilePreview(AGENT, 'tok', 'k7m2xq9a')
  await api.getFilePreview(AGENT, 'tok', 'k7m2xq9a', 'eyJyIjoiYSJ9', 999)
  const calls = env.callsTo('/api/documentation-search/files/k7m2xq9a/preview')
  assert.equal(calls.length, 2)
  assert.ok(calls[0].url.endsWith('/api/documentation-search/files/k7m2xq9a/preview'))
  const q = new URL(calls[1].url).searchParams
  assert.equal(q.get('cursor'), 'eyJyIjoiYSJ9')
  assert.equal(q.get('limit'), '60')
})

test('409 StalePreview surfaces as its own error type, carrying the new revision', async () => {
  const env = installBrowser()
  env.route('/preview', () => json({ error: 'StalePreview', message: 'The file was re-indexed.', revision: 'r9' }, 409))
  await assert.rejects(api.getFilePreview(AGENT, 'tok', 'k7m2xq9a', 'c1'), (e) => {
    assert.ok(e instanceof api.StalePreviewError)
    assert.ok(e instanceof api.DocumentsApiError)
    assert.equal(e.code, 'StalePreview')
    assert.equal(e.status, 409)
    assert.equal(e.revision, 'r9')
    return true
  })

  env.route('/preview', () => json({ error: 'NotFound', message: 'No such file.' }, 404))
  await assert.rejects(api.getFilePreview(AGENT, 'tok', 'k7m2xq9a'), (e) => {
    assert.ok(!(e instanceof api.StalePreviewError))
    assert.equal(e.code, 'NotFound')
    return true
  })
})
