// The Tags pages' pure helpers: the settings form's inherit/override model,
// the stage timeline, and the few constants copied from the server.
//
// The form holds every key, with `null` meaning "inherit the layer below"; a
// save must send only the keys this layer overrides (PUT options REPLACES the
// profile's own overrides), and an inherited value shown greyed must be the
// admin default when there is one, else the built-in. The built-ins and the
// low-battery threshold are copies of app/tags — pinned here against the
// Python source so a server change cannot leave the UI naming stale defaults.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { installBrowser, load } from './harness.mjs'

installBrowser()
const fmt = await load('src/utils/tagsFormat.ts')

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..')
const KINDS = Object.keys(fmt.TAG_BUILTIN_DEFAULTS.routes)

test('a draft round-trips to only the keys it overrides', () => {
  const own = { show_excerpts: true, timezone: '', routes: { usage: 'all', needs_input: ['dev-1'] } }
  const draft = fmt.draftFromOptions(own, KINDS)
  assert.equal(draft.language, null)
  assert.equal(draft.show_excerpts, true)
  assert.equal(draft.timezone, '', "'' (the profile's own timezone) is an override, not inherit")
  assert.equal(draft.routes.calendar, null)
  assert.deepEqual(draft.routes.needs_input, ['dev-1'])
  assert.deepEqual(fmt.optionsFromDraft(draft), own)

  const cleared = { ...draft, show_excerpts: null, routes: { ...draft.routes, usage: null, needs_input: null } }
  assert.deepEqual(fmt.optionsFromDraft(cleared), { timezone: '' })
  assert.deepEqual(fmt.optionsFromDraft(fmt.draftFromOptions({}, KINDS)), {})
})

test('the draft copies arrays, so editing it never edits the loaded settings', () => {
  const own = { routes: { needs_input: ['dev-1'] } }
  const draft = fmt.draftFromOptions(own, KINDS)
  draft.routes.needs_input.push('dev-2')
  assert.deepEqual(own.routes.needs_input, ['dev-1'])
})

test('inherited values: the admin default where set, else the built-in, routes per kind', () => {
  const inherited = fmt.inheritedOptions({ language: 'vi', routes: { usage: 'all' } })
  assert.equal(inherited.language, 'vi')
  assert.equal(inherited.progress_cadence_s, 300)
  assert.equal(inherited.routes.usage, 'all')
  assert.equal(inherited.routes.notification, 'all')
  assert.equal(fmt.inheritedOptions(null).routes.usage, 'none')
})

test('the form refuses what the server would', () => {
  const ok = fmt.draftFromOptions({}, KINDS)
  assert.equal(fmt.draftProblem(ok), '')
  assert.match(fmt.draftProblem({ ...ok, routes: { ...ok.routes, calendar: [] } }), /at least one tag/)
  assert.match(fmt.draftProblem({ ...ok, progress_cadence_s: 30 }), /60 and 3600/)
  assert.match(fmt.draftProblem({ ...ok, language: '  ' }), /language/)
})

test('the stage timeline walks every forward stage and appends a terminal outcome', () => {
  const rows = fmt.stageTimeline({
    stage: 'failed', created_at: 100,
    stage_times: { companion_accepted: 110, gateway_received: 120, failed: 130 },
  })
  assert.deepEqual(rows.map((r) => r.stage), [...fmt.DELIVERY_STAGES, 'failed'])
  assert.equal(rows[0].at, 100, 'queued falls back to created_at')
  assert.deepEqual(rows.filter((r) => r.reached).map((r) => r.stage),
    ['queued', 'companion_accepted', 'gateway_received', 'failed'])
  assert.equal(rows.at(-1).current, true)

  const shown = fmt.stageTimeline({ stage: 'displayed', created_at: 1, stage_times: { displayed: 9 } })
  assert.equal(shown.length, fmt.DELIVERY_STAGES.length, 'displayed is already on the line')
  assert.equal(shown.at(-1).current, true)
})

test('pill types never use a light-9 tag for an in-flight stage', () => {
  assert.equal(fmt.stagePillType('displayed'), 'success')
  assert.equal(fmt.stagePillType('failed'), 'danger')
  assert.equal(fmt.stagePillType('transferring'), 'primary')
  assert.equal(fmt.commandStatusPill('claimed').label, 'running')
  assert.equal(fmt.deviceStatusPill(null).label, 'unclaimed')
})

test('scan results accept the likely result shapes', () => {
  assert.deepEqual(fmt.scanResults({ devices: [{ uuid: 'u1', rssi: -50 }, { id: 'u2', name: 'B' }, {}] }), [
    { uuid: 'u1', rssi: -50, name: null },
    { uuid: 'u2', rssi: null, name: 'B' },
  ])
  assert.deepEqual(fmt.scanResults(['u3', ' ']).map((r) => r.uuid), ['u3'])
  assert.deepEqual(fmt.scanResults({ unprovisioned: ['u4'] }).map((r) => r.uuid), ['u4'])
  assert.deepEqual(fmt.scanResults(null), [])
})

test('the built-in defaults and battery threshold match app/tags', () => {
  const routing = readFileSync(path.join(repo, 'app/tags/routing.py'), 'utf8')
  const kinds = routing.match(/ROUTABLE_KINDS = \(([\s\S]*?)\)/)[1].match(/"([a-z_]+)"/g).map((k) => k.slice(1, -1))
  assert.deepEqual(KINDS, kinds)
  const builtin = routing.match(/BUILTIN_DEFAULTS[^=]*= \{([\s\S]*?)\n\}/)[1]
  assert.match(builtin, /"progress_cadence_s": 300/)
  assert.match(builtin, /"language": "en"/)
  assert.match(builtin, /"none" if kind == "usage" else "all"/)
  assert.equal(fmt.TAG_BUILTIN_DEFAULTS.progress_cadence_s, 300)
  assert.equal(fmt.TAG_BUILTIN_DEFAULTS.language, 'en')

  const projection = readFileSync(path.join(repo, 'app/tags/projection.py'), 'utf8')
  assert.equal(Number(projection.match(/BATTERY_LOW_MV = (\d+)/)[1]), fmt.BATTERY_LOW_MV)
})
