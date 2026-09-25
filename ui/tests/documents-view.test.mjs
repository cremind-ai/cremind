// What Documentation search says to the user: the state banner for every row
// of the design's unified state table (§2.8), the failure-reason labels, the
// number formats, and the NavRail chip's tooltip.
//
// The banner is the one place a state becomes words, so each `state(reason)`
// the engine can report has a case here — a new reason that falls through to a
// generic banner, or a destructive-sounding line on a harmless state, shows up
// as a failing assertion rather than as a confused user.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const {
  stateBanner, reasonLabel, formatBytes, formatEta, formatCount, formatDuration, effectLabel,
  chipTooltip, stageLabel, statusLabel, visionStatus, visionQuota, captionStateLabel,
  dockerWarning, HELM_WORK_VOLUME_FLAG,
} = await load('src/utils/documentsView.ts')

function snap(overrides = {}) {
  return {
    v: 1, boot: 'b', seq: 1, enabled: true, state: 'idle', reason: null,
    sources: { local: { enabled: true, root_mode: 'custom', root: '/home/ann/Documents', first_sync_confirmed: true }, drive: null },
    ...overrides,
  }
}

function settings({ admin = false } = {}) {
  return {
    local: { root_path: '/home/ann/Documents' },
    policy_view: { is_admin: admin, working_dir: '/home/ann', allowed: true, effective: true, reason: null },
  }
}

const ACTIONS = new Set([
  'enable', 'open_admin', 'choose_folder', 'rescan', 'confirm_root_change', 'review_first_sync',
  'review_deletions', 'resume', 'pause', 'adjust_excludes', 'retry_failed', 'relink_google',
])

/** Every banner is complete, whatever the state. */
function check(b) {
  assert.ok(['info', 'warning', 'error', 'success'].includes(b.tone), `tone ${b.tone}`)
  assert.ok(b.title.length > 3, 'title')
  assert.ok(b.body.length > 10, 'body')
  for (const a of b.actions) assert.ok(ACTIONS.has(a.id), `unknown action ${a.id}`)
  return b
}

const ids = b => b.actions.map(a => a.id)

// ── disabled / suspended / blocked ──────────────────────────────────────────

test('disabled: off, with the kept index size when there is one', () => {
  const plain = check(stateBanner(snap({ state: 'disabled', enabled: false })))
  assert.equal(plain.tone, 'info')
  assert.match(plain.title, /off/i)
  assert.deepEqual(ids(plain), ['enable'])
  assert.doesNotMatch(plain.body, /kept/)

  const kept = check(stateBanner(snap({ state: 'disabled', enabled: false }), null, { keptIndexBytes: 1.2 * 1024 ** 3 }))
  assert.match(kept.body, /index is kept \(1\.2 GB\)/)
})

test('suspended(admin_gate): disabled by the administrator, index kept', () => {
  const b = check(stateBanner(snap({ state: 'suspended', reason: 'admin_gate' }), settings()))
  assert.equal(b.tone, 'warning')
  assert.equal(b.title, 'Disabled by your administrator — your index is kept')
  assert.deepEqual(ids(b), [])
  assert.match(b.body, /Ask your administrator/)
  // The admin is offered the page where the gate lives instead.
  const admin = check(stateBanner(snap({ state: 'suspended', reason: 'admin_gate' }), settings({ admin: true })))
  assert.deepEqual(ids(admin), ['open_admin'])
})

test('suspended(admin_gate) for a profile that never turned it on promises no kept index', () => {
  const b = check(stateBanner(snap({ state: 'suspended', reason: 'admin_gate', enabled: false }), settings()))
  assert.doesNotMatch(b.title + b.body, /kept/)
  assert.match(b.body, /administrator has to allow/)
})

test('suspended(embedding_off): keyword search over the index as of a time', () => {
  const asOf = new Date(2026, 8, 20, 14, 30).getTime()
  const b = check(stateBanner(snap({ state: 'suspended', reason: 'embedding_off' }), settings(), { asOf }))
  assert.equal(b.tone, 'warning')
  assert.match(b.title, /^Vector Embedding is off — keyword search over the index as of /)
  assert.doesNotMatch(b.title, /when it was turned off/)
  // Without a time it still says what it means.
  const noTime = check(stateBanner(snap({ state: 'suspended', reason: 'embedding_off' }), settings()))
  assert.match(noTime.title, /as of when it was turned off$/)
})

test('suspended(allow_in): explains the origin limit', () => {
  const b = check(stateBanner(snap({ state: 'suspended', reason: 'allow_in' })))
  assert.equal(b.tone, 'info')
  assert.match(b.body, /channels or group rooms/)
})

test('blocked(feature_missing): admins install, others ask', () => {
  const admin = check(stateBanner(snap({ state: 'blocked', reason: 'feature_missing' }), settings({ admin: true })))
  assert.deepEqual(ids(admin), ['open_admin'])
  assert.match(admin.body, /Install/)
  const other = check(stateBanner(snap({ state: 'blocked', reason: 'feature_missing' }), settings()))
  assert.deepEqual(ids(other), [])
  assert.match(other.body, /Ask your administrator/)
})

// ── hold ────────────────────────────────────────────────────────────────────

test('hold(root_invalid): the server message, and a way to pick another folder', () => {
  const b = check(stateBanner(snap({
    state: 'hold', reason: 'root_invalid',
    detail: { code: 'not_found', message: 'That folder does not exist.', root: '/gone' },
  })))
  assert.equal(b.tone, 'error')
  assert.match(b.body, /That folder does not exist\./)
  assert.match(b.body, /Nothing has been deleted/)
  assert.deepEqual(ids(b), ['choose_folder'])
})

test('hold(root_unavailable): missing folder, nothing deleted, resumes on its own', () => {
  const b = check(stateBanner(snap({ state: 'hold', reason: 'root_unavailable', detail: { why: 'root_missing', exists: false } })))
  assert.equal(b.tone, 'warning')
  assert.match(b.body, /\/home\/ann\/Documents is missing/)
  assert.match(b.body, /Nothing has been deleted/)
  assert.deepEqual(ids(b), ['rescan', 'choose_folder'])
  assert.equal(b.snippet, null)
})

test('hold(root_unavailable) in Docker with no bind mount carries the compose line', () => {
  const b = check(stateBanner(snap({
    state: 'hold', reason: 'root_unavailable',
    sources: { local: { root: '/root/Documents' } },
    detail: { why: 'bind_missing', in_container: true, root_mounted: false, snippet: '- "~/Documents:/root/Documents"' },
  })))
  assert.match(b.body, /Docker/)
  assert.match(b.body, /docker compose up -d/)
  assert.equal(b.snippet, '- "~/Documents:/root/Documents"')
})

test('hold(root_unavailable): an empty root that used to hold files', () => {
  const b = check(stateBanner(snap({ state: 'hold', reason: 'root_unavailable', detail: { why: 'root_empty', manifest_count: 1300 } })))
  assert.match(b.body, /empty, but the index holds 1,300 files/)
})

test('hold(root_unavailable): a changed device', () => {
  const b = check(stateBanner(snap({ state: 'hold', reason: 'root_unavailable', detail: { why: 'device_changed' } })))
  assert.match(b.body, /different disk/)
})

test('hold(root_unavailable) inside a container adds the Docker hint to other causes too', () => {
  const b = check(stateBanner(snap({
    state: 'hold', reason: 'root_unavailable',
    detail: { why: 'root_missing', in_container: true, snippet: '- "~/Documents:/root/Documents"' },
  })))
  assert.match(b.body, /If Cremind runs in Docker/)
  assert.ok(b.snippet)
})

test('hold(pending_root_change): confirm the new folder, or choose one', () => {
  const b = check(stateBanner(snap({
    state: 'hold', reason: 'pending_root_change',
    detail: { from: '/old/docs', to: '/new/docs' },
    confirmation: { kind: 'root_change', from: '/old/docs', to: '/new/docs', files: 420 },
  })))
  assert.equal(b.tone, 'warning')
  assert.match(b.body, /from \/old\/docs to \/new\/docs/)
  assert.deepEqual(ids(b), ['confirm_root_change', 'choose_folder'])
})

test('Drive holds: re-link Google, or wait for it to answer', () => {
  const revoked = check(stateBanner(snap({ state: 'hold', reason: 'auth_revoked', detail: { purge_at: Date.now() + 7 * 86400e3 } })))
  assert.match(revoked.title, /^Re-link Google — Drive index removed on /)
  assert.deepEqual(ids(revoked), ['relink_google'])
  const unreachable = check(stateBanner(snap({ state: 'hold', reason: 'drive_unreachable' })))
  assert.equal(unreachable.title, 'Google Drive is unreachable — results may be outdated')
})

test('an unknown hold reason still reassures and offers a re-check', () => {
  const b = check(stateBanner(snap({ state: 'hold', reason: 'something_new', detail: {} })))
  assert.match(b.body, /Nothing has been deleted/)
  assert.deepEqual(ids(b), ['rescan'])
})

// ── the container underneath (an install from before the bind mount) ────────

const LEGACY_DOCKER = {
  in_container: true, kubernetes: false, root_mounted: false, persistent: false, fstype: 'overlay',
  bind_expected: false, snippet: '- "~/Documents:/root/Documents"',
}

test('dockerWarning: a folder in the container layer gets the compose line and the installer', () => {
  const b = check(dockerWarning(snap({ docker: LEGACY_DOCKER })))
  assert.equal(b.tone, 'warning')
  assert.match(b.body, /inside the container and is lost when it is recreated/)
  assert.match(b.body, /re-run the installer \(your data is kept\)/)
  assert.match(b.body, /docker-compose\.yml/)
  assert.match(b.body, /copy out any you need first/)
  assert.doesNotMatch(b.body, /persistence\.work/)
  assert.equal(b.snippet, '- "~/Documents:/root/Documents"')
  assert.deepEqual(ids(b), [])
  assert.equal(b.busy, false)
})

test('dockerWarning is separate: the state banner is the same with or without it', () => {
  for (const state of [{ state: 'idle' }, { state: 'indexing' }, { state: 'paused', reason: 'user' }]) {
    assert.deepEqual(stateBanner(snap({ ...state, docker: LEGACY_DOCKER })), stateBanner(snap(state)))
  }
  assert.equal(stateBanner(snap({ docker: LEGACY_DOCKER })).title, 'Up to date')
})

test('dockerWarning in a pod suggests the chart\'s work volume, not compose', () => {
  const b = check(dockerWarning(snap({ docker: { ...LEGACY_DOCKER, kubernetes: true } })))
  assert.match(b.body, /inside the pod and is lost when the pod restarts/)
  assert.match(b.body, /work volume/)
  assert.doesNotMatch(b.body, /docker-compose|installer/)
  assert.equal(b.snippet, HELM_WORK_VOLUME_FLAG)
  assert.equal(HELM_WORK_VOLUME_FLAG, '--set persistence.work.enabled=true')
})

test('dockerWarning stays quiet unless the folder is known to be in the container layer', () => {
  assert.equal(dockerWarning(null), null)
  assert.equal(dockerWarning(snap()), null) // no docker block (older server, folder off)
  assert.equal(dockerWarning(snap({ docker: null })), null)
  assert.equal(dockerWarning(snap({ docker: { ...LEGACY_DOCKER, in_container: false } })), null)
  assert.equal(dockerWarning(snap({ docker: { ...LEGACY_DOCKER, root_mounted: true } })), null)
  // Unknown (no mountinfo) is not a warning.
  assert.equal(dockerWarning(snap({ docker: { ...LEGACY_DOCKER, root_mounted: null } })), null)
  // A compose file that mounts the folder: a missing mount is the bind_missing hold instead.
  assert.equal(dockerWarning(snap({ docker: { ...LEGACY_DOCKER, bind_expected: true } })), null)
})

test('dockerWarning without a snippet still says what to do', () => {
  const b = check(dockerWarning(snap({ docker: { ...LEGACY_DOCKER, snippet: null } })))
  assert.equal(b.snippet, null)
  assert.match(b.body, /re-run the installer/)
})

// ── confirmations ───────────────────────────────────────────────────────────

test('awaiting_confirmation(mass_delete): "N of M files disappeared — remove them?"', () => {
  const b = check(stateBanner(snap({
    state: 'awaiting_confirmation', reason: 'mass_delete',
    confirmation: { kind: 'mass_delete', missing: 1234, total: 1300 },
  })))
  assert.equal(b.title, '1,234 of 1,300 files disappeared — remove them?')
  assert.equal(b.tone, 'warning')
  assert.deepEqual(ids(b), ['review_deletions'])
})

test('awaiting_confirmation(first_sync): what was found, then review', () => {
  const b = check(stateBanner(snap({
    state: 'awaiting_confirmation', reason: 'first_sync',
    confirmation: { kind: 'first_sync', estimate: { state: 'done', files: 48120, bytes: 2 * 1024 ** 3 } },
  }), settings()))
  assert.equal(b.tone, 'info')
  assert.match(b.body, /Found 48,120 files \(2 GB\)/)
  assert.deepEqual(ids(b), ['review_first_sync'])
})

test('awaiting_confirmation(root_change) reads like the root-change hold', () => {
  const b = check(stateBanner(snap({
    state: 'awaiting_confirmation', reason: 'root_change',
    confirmation: { kind: 'root_change', from: '/a', to: '/b', files: 3 },
  })))
  assert.deepEqual(ids(b), ['confirm_root_change', 'choose_folder'])
  assert.match(b.body, /from \/a to \/b/)
})

test('awaiting_confirmation(vision_consent)', () => {
  const b = check(stateBanner(snap({ state: 'awaiting_confirmation', reason: 'vision_consent' })))
  assert.match(b.title, /photos/)
})

// ── paused ──────────────────────────────────────────────────────────────────

test('paused(user): resume', () => {
  const b = check(stateBanner(snap({ state: 'paused', reason: 'user' })))
  assert.equal(b.tone, 'info')
  assert.deepEqual(ids(b), ['resume'])
})

test('paused(budget): exclusions, and the admin can raise the budget', () => {
  const other = check(stateBanner(snap({ state: 'paused', reason: 'budget', storage: { level: 'budget' } }), settings()))
  assert.equal(other.tone, 'warning')
  assert.deepEqual(ids(other), ['adjust_excludes'])
  assert.match(other.body, /ask your administrator/)
  const admin = check(stateBanner(snap({ state: 'paused', reason: 'budget' }), settings({ admin: true })))
  assert.deepEqual(ids(admin), ['adjust_excludes', 'open_admin'])
})

test('paused(disk_low) and paused(disk_critical) name the free space', () => {
  const low = check(stateBanner(snap({ state: 'paused', reason: 'disk_low', storage: { free_bytes: 2 * 1024 ** 3 } })))
  assert.equal(low.tone, 'warning')
  assert.match(low.title, /Disk space is low/)
  assert.match(low.body, /2 GB free/)
  const critical = check(stateBanner(snap({ state: 'paused', reason: 'disk_critical', storage: { free_bytes: 512 * 1024 ** 2 } })))
  assert.equal(critical.tone, 'error')
  assert.match(critical.body, /512 MB free/)
})

// ── work under way ──────────────────────────────────────────────────────────

test('estimating, scanning, reembedding and indexing are busy banners', () => {
  const est = check(stateBanner(snap({ state: 'estimating', phase: 'estimating (3120 files)' })))
  assert.equal(est.busy, true)
  assert.match(est.body, /nothing is read/)

  const scan = check(stateBanner(snap({ state: 'scanning' })))
  assert.equal(scan.busy, true)
  assert.deepEqual(ids(scan), ['pause'])

  const re = check(stateBanner(snap({ state: 'reembedding', reembed: { done: 45, total: 100 } })))
  assert.equal(re.title, 'Re-indexing for the new model 45%')

  const idx = check(stateBanner(snap({
    state: 'indexing',
    batch: { label: 'sync', total: 12840, done: 3120, failed: 0, skipped: 0, eta_s: 720 },
    stages: { error: 12 },
  })))
  assert.equal(idx.title, 'Indexing documents')
  assert.match(idx.body, /^3,120 of 12,840 files · ~12 min left\./)
  assert.match(idx.body, /12 files could not be indexed/)
  assert.deepEqual(ids(idx), ['pause', 'retry_failed'])
})

test('idle: up to date, or up to date with failures to look at', () => {
  const ok = check(stateBanner(snap({ state: 'idle', stages: { indexed: 120, metadata_only: 3 } })))
  assert.equal(ok.tone, 'success')
  assert.equal(ok.title, 'Up to date')
  assert.match(ok.body, /123 files indexed/)
  assert.deepEqual(ids(ok), [])

  const failed = check(stateBanner(snap({ state: 'idle', stages: { indexed: 120, error: 1 } })))
  assert.equal(failed.tone, 'warning')
  assert.equal(failed.title, 'Up to date — 1 file could not be indexed')
  assert.deepEqual(ids(failed), ['retry_failed'])

  const polled = check(stateBanner(snap({ state: 'idle', stages: { indexed: 1 }, watch: { mode: 'poll' } })))
  assert.match(polled.body, /checked for changes regularly/)
})

test('error and unknown states', () => {
  const err = check(stateBanner(snap({ state: 'error', detail: { message: 'Store unreachable.' } })))
  assert.equal(err.tone, 'error')
  assert.match(err.body, /Store unreachable\./)
  const unknown = check(stateBanner({ v: 1, enabled: false, state: 'unknown', reason: null }))
  assert.equal(unknown.title, 'Status unavailable')
})

// ── labels ──────────────────────────────────────────────────────────────────

test('reasonLabel: every failure reason has human words', () => {
  const codes = [
    'timeout', 'oom', 'extractor_crash', 'encrypted', 'corrupt', 'legacy_format', 'too_large',
    'permission_denied', 'awaiting_extractor', 'placeholder', 'secret', 'heic_unsupported',
    'internal_error',
  ]
  for (const code of codes) {
    const label = reasonLabel(code)
    assert.ok(label && !label.includes('_'), `${code} → ${label}`)
  }
  assert.equal(reasonLabel('encrypted'), 'Password-protected')
  assert.equal(reasonLabel('partial:too_large'), 'Partly read — larger than the size limit')
  assert.equal(reasonLabel('some_new_reason'), 'Some new reason')
  assert.equal(reasonLabel(null), '')
  assert.equal(reasonLabel(''), '')
})

test('status and stage labels', () => {
  assert.equal(statusLabel('dirty'), 'Waiting')
  assert.equal(statusLabel('error'), 'Failed')
  assert.equal(stageLabel('index', { done: 3, total: 128 }), 'Indexing 3/128 passages')
  assert.equal(stageLabel('extract'), 'Reading text')
})

test('change-plan effects read as sentences', () => {
  assert.equal(
    effectLabel({ kind: 'purge_all', files: 1300, bytes: 1.2 * 1024 ** 3, detail: {} }),
    "Delete this profile's whole document index — 1,300 files (1.2 GB).",
  )
  assert.match(effectLabel({ kind: 'purge_out_of_scope', files: 42, bytes: 0, detail: { to: '/new' } }), /42 files that are not inside \/new/)
  assert.match(effectLabel({ kind: 'purge_out_of_scope', files: 1, bytes: 0, detail: {} }), /1 file that the new rules leave out/)
  assert.match(effectLabel({ kind: 'reembed_all', files: 10, bytes: 0, detail: { chunks: 5000 } }), /5,000/)
  assert.match(effectLabel({ kind: 'reextract_all', files: 10, bytes: 0, detail: {} }), /Read all 10 files again/)
})

// ── formats ─────────────────────────────────────────────────────────────────

test('formatBytes', () => {
  assert.equal(formatBytes(0), '0 B')
  assert.equal(formatBytes(null), '0 B')
  assert.equal(formatBytes(512), '512 B')
  assert.equal(formatBytes(1536), '1.5 KB')
  assert.equal(formatBytes(512 * 1024 ** 2), '512 MB')
  assert.equal(formatBytes(1.2 * 1024 ** 3), '1.2 GB')
  assert.equal(formatBytes(2 * 1024 ** 3), '2 GB')
})

test('formatEta', () => {
  assert.equal(formatEta(null), '')
  assert.equal(formatEta(0), '')
  assert.equal(formatEta(30), 'less than a minute left')
  assert.equal(formatEta(720), '~12 min left')
  assert.equal(formatEta(2 * 3600 + 300), '~2 h 5 min left')
  assert.equal(formatEta(2 * 3600), '~2 h left')
  assert.equal(formatEta(3 * 86400), '~3 days left')
})

test('formatCount and formatDuration', () => {
  assert.equal(formatCount(12840), '12,840')
  assert.equal(formatCount(undefined), '0')
  assert.equal(formatDuration(40 * 60), 'about 40 minutes')
  assert.equal(formatDuration(6 * 3600), 'about 6 hours')
})

// ── the chip ────────────────────────────────────────────────────────────────

test('chip tooltip: progress with failures', () => {
  assert.equal(
    chipTooltip(snap({
      state: 'indexing',
      batch: { label: 'sync', total: 12840, done: 3120, failed: 0, skipped: 0 },
      stages: { error: 12 },
    })),
    'Indexing documents 3,120/12,840 · 12 failed',
  )
  assert.equal(chipTooltip(snap({ state: 'estimating' })), 'Estimating the first document sync…')
})

test('chip tooltip: what needs attention', () => {
  assert.equal(
    chipTooltip(snap({ state: 'awaiting_confirmation', reason: 'mass_delete', confirmation: { kind: 'mass_delete', missing: 5, total: 9 } })),
    '5 of 9 files disappeared — remove them?',
  )
  assert.equal(chipTooltip(snap({ state: 'idle', stages: { error: 3 } })), '3 documents could not be indexed')
  assert.equal(chipTooltip(null), '')
})

// ── image descriptions ──────────────────────────────────────────────────────
// Descriptions come only from the Specialized Vision Model: a missing model is
// a setup step (never "using your main model"), and permission names one model.

const VISION_OK = {
  ready: true, provider: 'openai', model: 'gpt-4o', reason: null, consent: true,
  consent_for: { at: 1, provider: 'openai', model: 'gpt-4o' },
  quota: { day: '2026-09-25', used: 37, ocr_pages: 2, cap: 1000 },
}

test('vision status: every reason a model is unusable sends the user to LLM Providers', () => {
  for (const reason of ['vision_disabled', 'vision_model_unset', 'vision_model_auth_incompatible',
    'vision_model_not_capable', 'vision_model_error']) {
    const s = visionStatus({ ready: false, reason })
    assert.equal(s.action, 'open_llm', reason)
    assert.equal(s.tone, 'warning', reason)
    assert.match(s.detail, /found by name, folder, date and camera/, reason)
    assert.doesNotMatch(`${s.title} ${s.detail}`, /main model/i, reason)
  }
  assert.equal(visionStatus(null).action, 'open_llm')
})

test('vision status: consent names the model, and asks again when it changed', () => {
  const ask = visionStatus({ ...VISION_OK, ready: false, reason: 'no_consent', consent: false, consent_for: null })
  assert.equal(ask.action, 'consent')
  assert.match(ask.detail, /openai\/gpt-4o/)
  assert.match(ask.detail, /Nothing is sent until you allow it/)

  const changed = visionStatus({
    ...VISION_OK, ready: false, reason: 'no_consent', consent: false,
    consent_for: { at: 1, provider: 'anthropic', model: 'claude-sonnet-5' },
  })
  assert.equal(changed.action, 'consent')
  assert.match(changed.title, /changed/)
  assert.match(changed.detail, /You had allowed anthropic\/claude-sonnet-5/)
})

test('vision status: ready, and turned off', () => {
  const ok = visionStatus(VISION_OK)
  assert.equal(ok.tone, 'success')
  assert.equal(ok.action, null)
  assert.match(ok.detail, /openai\/gpt-4o/)
  const off = visionStatus({ ...VISION_OK, ready: false, reason: 'vision_model_unset' }, false)
  assert.equal(off.action, null)
  assert.match(off.title, /off/)
})

test('vision quota: used of cap, full at the cap, and a zero cap sends nothing', () => {
  assert.deepEqual(visionQuota({ used: 37, cap: 1000 }), { label: '37 of 1,000 today', fraction: 0.037 })
  assert.equal(visionQuota({ used: 1200, cap: 1000 }).fraction, 1)
  assert.match(visionQuota({ used: 0, cap: 0 }).label, /no images are sent/)
  assert.equal(visionQuota(null), null)
})

test('caption state labels', () => {
  assert.match(captionStateLabel('awaiting_consent'), /your OK/)
  assert.match(captionStateLabel('over_cap'), /limit/)
  assert.match(captionStateLabel('awaiting_vision'), /no vision model/)
  assert.equal(captionStateLabel('done'), null)
  assert.equal(captionStateLabel(null), null)
  assert.match(reasonLabel('awaiting_consent'), /your OK/)
})
