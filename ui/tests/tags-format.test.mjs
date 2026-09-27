// The Tags pages' pure helpers: the settings form's inherit/override model,
// the save PATCH, the stage timeline, pills and cancel wording.
//
// The form holds every key, with `null` meaning "inherit the layer below". A
// save PATCHes only what changed (`null` = inherit again), so an edit made
// meanwhile from the CLI survives. An inherited value shown greyed is the
// admin default when there is one, else the server's `builtin` layer — the UI
// keeps no copy of the built-ins; it renders whatever the server sends.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, load } from './harness.mjs'

installBrowser()
const fmt = await load('src/utils/tagsFormat.ts')

// Shaped like GET /api/tags/settings `builtin` (values deliberately not the
// real built-ins: whatever the server sends is what the form must show).
const BUILTIN = {
  layout: 'status', show_excerpts: false, qr_links: true, progress_cadence_s: 600,
  language: 'fr', timezone: '',
  routes: { notification: 'all', needs_input: 'all', calendar: 'all', usage: 'none' },
}
const KINDS = Object.keys(BUILTIN.routes)

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

test('inherited values: the admin default where set, else the server-sent builtin', () => {
  const inherited = fmt.inheritedOptions({ language: 'vi', routes: { usage: 'all' } }, BUILTIN)
  assert.equal(inherited.language, 'vi')
  assert.equal(inherited.progress_cadence_s, 600, 'the builtin the server sent, not a UI copy')
  assert.equal(inherited.qr_links, true)
  assert.equal(inherited.routes.usage, 'all')
  assert.equal(inherited.routes.notification, 'all')
  const bare = fmt.inheritedOptions(null, BUILTIN)
  assert.equal(bare.routes.usage, 'none')
  assert.equal(bare.language, 'fr')
  assert.equal(fmt.TAG_BUILTIN_DEFAULTS, undefined, 'no client-side copy of the built-ins')
})

test('a save PATCHes only what changed; back to inherit is null', () => {
  const saved = fmt.draftFromOptions({ language: 'vi', show_excerpts: true, routes: { usage: 'all', calendar: ['a'] } }, KINDS)
  const current = structuredClone(saved)
  assert.deepEqual(fmt.draftPatch(saved, current), {}, 'nothing touched, nothing sent')

  current.language = null // back to "Use admin default"
  current.qr_links = true // a new override
  current.routes.usage = null
  current.routes.calendar = ['a', 'b']
  current.routes.needs_input = 'none'
  assert.deepEqual(fmt.draftPatch(saved, current), {
    language: null, qr_links: true,
    routes: { usage: null, calendar: ['a', 'b'], needs_input: 'none' },
  })

  const typed = structuredClone(saved)
  typed.language = '  en '
  assert.deepEqual(fmt.draftPatch(saved, typed), { language: 'en' }, 'trimmed like the server')
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

test('pill types: stages, commands, and clear_failed as danger', () => {
  assert.equal(fmt.stagePillType('displayed'), 'success')
  assert.equal(fmt.stagePillType('failed'), 'danger')
  assert.equal(fmt.stagePillType('transferring'), 'primary')
  assert.equal(fmt.commandStatusPill('claimed').label, 'running')
  assert.equal(fmt.deviceStatusPill(null).label, 'unclaimed')
  assert.deepEqual(fmt.deviceStatusPill('clear_failed'), { label: 'clear failed', type: 'danger' })
})

test('cancel wording: a card the companion holds can still be cancelled', () => {
  const queued = fmt.cancelPrompt({ stage: 'queued', kind: 'pinned_note', card: { title: 'Lunch' } })
  assert.match(queued, /"Lunch"/)
  assert.match(queued, /has not left Cremind/)
  const held = fmt.cancelPrompt({ stage: 'transferring', kind: 'notification', card: null })
  assert.match(held, /"Notifications"/)
  assert.match(held, /companion already has it; Cremind tells it to drop the card/)
  assert.match(fmt.cancelledMessage({ resolved: { kind: 'resolved' } }), /told to drop the card/)
  assert.equal(fmt.cancelledMessage({ resolved: null }), 'Delivery cancelled')
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
