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
  assert.deepEqual(fmt.deviceStatusPill('assign_failed'), { label: 'assign failed', type: 'danger' })
  assert.equal(fmt.isStuck('assign_failed'), true)
  assert.equal(fmt.isStuck('clear_failed'), true)
  assert.equal(fmt.isStuck('assigning'), false)
})

test('bridge capacity: known, full, unknown, and the tag already on the bridge', () => {
  const full = fmt.bridgeCapacity({ id: 'b1', max_tags: 10, assigned_count: 10 })
  assert.equal(full.label, '10 / 10 tags')
  assert.equal(full.full, true)
  assert.match(full.tooltip, /full/)
  assert.match(full.tooltip, /released tag still holds its slot/)

  const room = fmt.bridgeCapacity({ id: 'b1', max_tags: 10, assigned_count: 3 })
  assert.deepEqual([room.label, room.full], ['3 / 10 tags', false])

  const unknown = fmt.bridgeCapacity({ id: 'b2', max_tags: null, assigned_count: 1 })
  assert.deepEqual([unknown.label, unknown.full, unknown.max], ['1 tag', false, null])
  assert.match(unknown.tooltip, /has not reported/)
  assert.equal(fmt.bridgeCapacity({ id: 'b3' }).label, '0 tags')

  // Keeping a tag on its own full bridge needs no new slot (the server leaves it out too).
  assert.equal(fmt.bridgeCapacity({ id: 'b1', max_tags: 2, assigned_count: 2 }, { id: 't', bridge_device_id: 'b1' }).full, false)
  assert.equal(fmt.bridgeCapacity({ id: 'b1', max_tags: 2, assigned_count: 2 }, { id: 't', bridge_device_id: 'b9' }).full, true)
  assert.equal(fmt.bridgeCapacity({ id: 'b1', max_tags: 2, assigned_count: 2 }, { id: 't', bridge_device_id: null }).full, true)
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

test('a preview has the size the tag is read at: an odd quarter turn swaps the panel', () => {
  // The 2.13-inch Hema: native 128x250, rotation 3 — its PNG is 250x128.
  assert.deepEqual(fmt.logicalSize({ width: 128, height: 250, rotation: 3 }), { width: 250, height: 128 })
  assert.deepEqual(fmt.logicalSize({ width: 128, height: 250, rotation: 1 }), { width: 250, height: 128 })
  assert.deepEqual(fmt.logicalSize({ width: 400, height: 300, rotation: 2 }), { width: 400, height: 300 })
  assert.deepEqual(fmt.logicalSize({ width: 400, height: 300, rotation: 0 }), { width: 400, height: 300 })
  assert.deepEqual(fmt.logicalSize({ width: 400, height: 300 }), { width: 400, height: 300 }, 'no rotation reads as native')
  assert.equal(fmt.logicalSize({ width: null, height: 300, rotation: 1 }), null)
  assert.equal(fmt.logicalSize({ width: 400, height: 0 }), null)
})

test('a preview zooms by whole screen pixels, and smoothly only below 1x', () => {
  // 400x300 in a 460x360 box: 1x, never the 1.15x (or the old 0.8x) that drops 1 px stems.
  assert.deepEqual(fmt.previewZoom({ w: 400, h: 300 }, { w: 460, h: 360 }, 1),
    { cssWidth: 400, cssHeight: 300, zoom: 1, pixelated: true })
  // On a 225% screen the same box holds 2 screen pixels per tag pixel (the height allows 2.7).
  const hidpi = fmt.previewZoom({ w: 400, h: 300 }, { w: 460, h: 360 }, 2.25)
  assert.equal(hidpi.zoom, 2)
  assert.equal(hidpi.pixelated, true)
  assert.ok(Math.abs(hidpi.cssWidth * 2.25 - 800) < 1e-9, 'exactly 800 screen pixels wide')
  // The Hema at 125%: 2x is 500 screen pixels = 400 CSS pixels.
  assert.deepEqual(fmt.previewZoom({ w: 250, h: 128 }, { w: 460, h: 360 }, 1.25),
    { cssWidth: 400, cssHeight: 204.8, zoom: 2, pixelated: true })
  // The height bounds it too, and maxZoom caps it.
  assert.equal(fmt.previewZoom({ w: 250, h: 128 }, { w: 2000, h: 300 }, 1).zoom, 2)
  assert.equal(fmt.previewZoom({ w: 250, h: 128 }, { w: 2000, h: 2000 }, 1).zoom, 4)
  assert.equal(fmt.previewZoom({ w: 250, h: 128 }, { w: 2000, h: 2000 }, 1, 6).zoom, 6)
  assert.equal(fmt.previewZoom({ w: 250, h: 128 }, { w: 2000, h: 2000 }, 1, Infinity).zoom, 8)
  // An exact fit is that zoom, not one less.
  assert.equal(fmt.previewZoom({ w: 250, h: 128 }, { w: 750 / 1.1, h: 1000 }, 1.1).zoom, 3)

  // A thumbnail (96 px wide) cannot hold 1x: scaled down to fit, smoothly.
  const thumb = fmt.previewZoom({ w: 250, h: 128 }, { w: 94, h: 80 }, 1)
  assert.equal(thumb.pixelated, false)
  assert.ok(Math.abs(thumb.cssWidth - 94) < 1e-9)
  assert.ok(thumb.cssHeight < 80)
  assert.ok(thumb.zoom > 0 && thumb.zoom < 1)
  // A bad screen ratio counts as 1; an unmeasured box draws nothing.
  assert.equal(fmt.previewZoom({ w: 400, h: 300 }, { w: 460, h: 360 }, 0).cssWidth, 400)
  assert.deepEqual(fmt.previewZoom({ w: 400, h: 300 }, { w: 0, h: 360 }, 1),
    { cssWidth: 0, cssHeight: 0, zoom: 0, pixelated: false })
})

test('a preview zooms in only with room to spare, so a page scrollbar cannot flip it back', () => {
  const hema = { w: 250, h: 128 }
  // 505 px holds 2x (500), but a scrollbar that 2x brings in would take it back below 500.
  assert.equal(fmt.steadyZoom(1, hema, { w: 505, h: 360 }, 1).zoom, 1, 'held at 1x')
  assert.equal(fmt.steadyZoom(null, hema, { w: 505, h: 360 }, 1).zoom, 2, 'a first fit takes the largest')
  assert.equal(fmt.steadyZoom(1, hema, { w: 530, h: 360 }, 1).zoom, 2, 'enough to spare')
  // Out as soon as the held zoom stops fitting, and kept while it still fits.
  assert.equal(fmt.steadyZoom(2, hema, { w: 499, h: 360 }, 1).zoom, 1)
  assert.equal(fmt.steadyZoom(2, hema, { w: 501, h: 360 }, 1).zoom, 2)
  // A bigger jump (760 px holds 3x) stops at the largest zoom that fits with the slack.
  const step = fmt.steadyZoom(1, hema, { w: 760, h: 1000 }, 1)
  assert.deepEqual(step, { cssWidth: 500, cssHeight: 256, zoom: 2, pixelated: true })
  // Below 1x there is nothing to hold.
  assert.equal(fmt.steadyZoom(1, hema, { w: 94, h: 80 }, 1).pixelated, false)
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
