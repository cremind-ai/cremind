// Google Drive as the second User Document Search source: what the Drive
// section says, how its folder picker moves, and what the client sends.
//
// Drive reports its own state in `snap.drive`, beside the local folder's
// top-level state. The tests pin that the two never mask each other (a Drive
// hold under an idle folder still shows; a Drive-only profile is not "off"),
// that only revoked/unlinked holds speak of removing the index, and that a
// server-supplied link only ever becomes an https href.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, json, load } from './harness.mjs'

const view = await load('src/utils/userdocsView.ts')
const {
  stateBanner, driveBanner, driveStatus, driveCountsText, driveAccessText, driveEnableBlocker,
  toEpochMs, formatAgo, effectLabel, chipTooltip,
  driveRootCrumb, enterFolder, crumbsTo, coveringFolder, toggleFolder, sameFolderSet, droppedFolders,
  namedFolders, folderFallbackName, parseDriveFolderRef, safeWebLink, sourceLabel,
} = view

const ACTIONS = new Set([
  'enable', 'open_admin', 'choose_folder', 'rescan', 'confirm_root_change', 'review_first_sync',
  'review_deletions', 'resume', 'pause', 'adjust_excludes', 'retry_failed', 'relink_google',
])

function check(b) {
  assert.ok(b, 'a banner')
  assert.ok(['info', 'warning', 'error', 'success'].includes(b.tone), `tone ${b.tone}`)
  assert.ok(b.title.length > 3, 'title')
  assert.ok(b.body.length > 10, 'body')
  for (const a of b.actions) assert.ok(ACTIONS.has(a.id), `unknown action ${a.id}`)
  return b
}

const ids = b => b.actions.map(a => a.id)

function drive(overrides = {}) {
  return {
    enabled: true, state: 'live', reason: null, detail: null,
    account_email: 'ann@example.com', identity_set: true, whole_drive: false, include_folders: [],
    last_sync_at: 1_790_000_000, last_full_at: 1_789_990_000,
    counts: { indexed: 120, pending: 0, error: 0, metadata_only: 0 },
    confirmation: null,
    ...overrides,
  }
}

function snap(driveOverrides = {}, overrides = {}) {
  return {
    v: 1, boot: 'b', seq: 1, enabled: true, state: 'idle', reason: null,
    sources: { local: { enabled: true, root_mode: 'custom', root: '/home/ann/Documents', first_sync_confirmed: true }, drive: { enabled: true } },
    stages: { indexed: 10 },
    drive: drive(driveOverrides),
    ...overrides,
  }
}

// ── the Drive banner ────────────────────────────────────────────────────────

test('no Drive banner while Drive is off, missing, or simply live', () => {
  assert.equal(driveBanner(null), null)
  assert.equal(driveBanner({ v: 1, enabled: true, state: 'idle', reason: null }), null)
  assert.equal(driveBanner(snap({ enabled: false, state: 'hold', reason: 'auth_revoked' })), null)
  assert.equal(driveBanner(snap()), null)
})

test('auth_revoked: the same re-link wording as the top-level table, with the removal date', () => {
  const purgeAt = new Date(2026, 9, 2, 9, 0).getTime()
  const b = check(driveBanner(snap({ state: 'hold', reason: 'auth_revoked', detail: { purge_at: purgeAt } })))
  assert.match(b.title, /^Re-link Google — Drive index removed on /)
  assert.doesNotMatch(b.title, /soon/)
  assert.deepEqual(ids(b), ['relink_google'])
  // Reused, not re-worded: identical to the top-level hold for the same reason.
  const top = stateBanner({ ...snap(), state: 'hold', reason: 'auth_revoked', detail: { purge_at: purgeAt } })
  assert.equal(b.title, top.title)
  assert.equal(b.body, top.body)
  // No date known: "soon", never a blank.
  assert.match(check(driveBanner(snap({ state: 'hold', reason: 'auth_revoked' }))).title, /removed soon$/)
})

test('drive_unlinked: hidden, removed on a date, and a way to link again', () => {
  const b = check(driveBanner(snap({ state: 'hold', reason: 'drive_unlinked', detail: { purge_at: Date.now() + 7 * 86400e3 } })))
  assert.match(b.title, /^Google Drive was unlinked — Drive index removed on /)
  assert.match(b.body, /hidden from search/)
  assert.deepEqual(ids(b), ['relink_google'])
  assert.equal(b.actions[0].primary, true)
})

test('drive_unreachable and drive_misconfigured keep the index and never speak of removing it', () => {
  const unreachable = check(driveBanner(snap({ state: 'hold', reason: 'drive_unreachable' })))
  assert.equal(unreachable.title, 'Google Drive is unreachable — results may be outdated')
  assert.deepEqual(ids(unreachable), [])
  assert.doesNotMatch(unreachable.title + unreachable.body, /removed on|deleted/i)

  // A reason from the source (the many-files-unavailable guard) leads the body.
  const guarded = check(driveBanner(snap({
    state: 'hold', reason: 'drive_unreachable', detail: { message: 'many files became unavailable' },
  })))
  assert.match(guarded.body, /^Many files became unavailable\. Drive files stay searchable/)

  const misconfigured = check(driveBanner(snap({ state: 'hold', reason: 'drive_misconfigured' })))
  assert.match(misconfigured.title, /results may be outdated/)
  assert.match(misconfigured.body, /nothing is removed/)
  assert.deepEqual(ids(misconfigured), ['relink_google'])
  assert.equal(misconfigured.actions[0].primary, undefined)
})

test('an unknown Drive hold reason still reassures', () => {
  const b = check(driveBanner(snap({ state: 'hold', reason: 'something_new', detail: {} })))
  assert.equal(b.title, 'Google Drive syncing is on hold')
  assert.match(b.body, /Nothing has been removed/)
})

test('a held Drive mass removal: N of M Drive files, review', () => {
  const b = check(driveBanner(snap({ confirmation: { kind: 'mass_delete', missing: 1234, total: 1300 } })))
  assert.equal(b.title, '1,234 of 1,300 Google Drive files disappeared — remove them?')
  assert.deepEqual(ids(b), ['review_deletions'])
  assert.match(b.body, /Nothing is removed from the index until you decide/)
})

test('a Drive hold does not touch the local banner, and a local hold does not hide Drive', () => {
  const s = snap({ state: 'hold', reason: 'drive_unreachable' })
  assert.equal(stateBanner(s).title, 'Up to date')
  assert.match(driveBanner(s).title, /unreachable/)

  const both = snap({ state: 'hold', reason: 'auth_revoked', detail: { purge_at: Date.now() } }, {
    state: 'hold', reason: 'root_unavailable', detail: { why: 'root_missing' },
  })
  assert.match(stateBanner(both).title, /documents folder isn't available/)
  assert.match(driveBanner(both).title, /^Re-link Google/)
})

test('the new hold reasons also word a top-level hold', () => {
  const b = check(stateBanner({ ...snap(), state: 'hold', reason: 'drive_unlinked', detail: {} }))
  assert.match(b.title, /unlinked/)
  const m = check(stateBanner({ ...snap(), state: 'hold', reason: 'drive_misconfigured', detail: {} }))
  assert.match(m.title, /refused/)
})

// ── Drive-only profiles ─────────────────────────────────────────────────────

test('a Drive-only profile is not "document search is off"', () => {
  const driveOnly = snap({}, {
    state: 'disabled',
    sources: { local: { enabled: false, root_mode: 'inherit', root: null, first_sync_confirmed: false }, drive: { enabled: true } },
  })
  const b = check(stateBanner(driveOnly))
  assert.equal(b.title, 'Searching Google Drive only')
  assert.deepEqual(ids(b), ['enable'])
  assert.equal(chipTooltip(driveOnly), 'Searching Google Drive only')
  // The page's saved setting wins over a snapshot without a Drive view.
  const noView = { v: 1, enabled: true, state: 'disabled', reason: null }
  assert.equal(stateBanner(noView, null, { driveEnabled: true }).title, 'Searching Google Drive only')
  assert.equal(stateBanner(noView, null, { driveEnabled: false }).title, 'Document search is off')
  assert.equal(stateBanner({ ...noView, enabled: false }).title, 'Document search is off')
})

test('idle with Drive only says Drive is checked, not that a folder is watched', () => {
  const driveOnly = snap({}, {
    state: 'idle', stages: { indexed: 40 },
    sources: { local: null, drive: { enabled: true } },
  })
  const b = check(stateBanner(driveOnly))
  assert.match(b.body, /40 files indexed; Google Drive is checked for changes/)
  assert.doesNotMatch(b.body, /folder/)
  // Both sources: the folder wording, as before.
  assert.match(stateBanner(snap()).body, /your folder is watched for changes/)
})

// ── status line, counts, access ─────────────────────────────────────────────

test('Drive status: off, listing, indexing, up to date, failures, holds, confirmation', () => {
  assert.equal(driveStatus(null), null)
  assert.equal(driveStatus(drive({ enabled: false })), null)
  const listing = driveStatus(drive({ last_sync_at: null, last_full_at: null, counts: {} }))
  assert.equal(listing.label, 'Listing your Drive…')
  assert.equal(listing.busy, true)
  const indexing = driveStatus(drive({ counts: { indexed: 3, pending: 1200 } }))
  assert.equal(indexing.label, 'Indexing — 1,200 waiting')
  assert.equal(indexing.busy, true)
  // Rows already in the index mean the first listing is done, times or not.
  assert.equal(driveStatus(drive({ last_sync_at: null, last_full_at: null })).label, 'Up to date')
  assert.deepEqual(driveStatus(drive()), { tone: 'success', label: 'Up to date', busy: false })
  assert.equal(driveStatus(drive({ counts: { indexed: 5, error: 2 } })).label, 'Up to date — 2 files failed')
  assert.match(driveStatus(drive({ state: 'hold', reason: 'auth_revoked' })).label, /revoked/)
  assert.match(driveStatus(drive({ state: 'hold', reason: 'drive_unreachable' })).label, /Unreachable/)
  assert.equal(driveStatus(drive({ confirmation: { kind: 'mass_delete', missing: 1, total: 2 } })).label,
    'Waiting for your confirmation')
})

test('Drive counts read as one line, zeros left out', () => {
  assert.equal(driveCountsText(drive({ counts: { indexed: 1204, pending: 3, error: 2, metadata_only: 7 } })),
    '1,204 files indexed · 3 waiting · 2 failed · 7 by name only')
  assert.equal(driveCountsText(drive({ counts: { indexed: 1 } })), '1 file indexed')
  assert.equal(driveCountsText(null), '0 files indexed')
})

test('sync times: seconds or milliseconds, relative while recent', () => {
  assert.equal(toEpochMs(1_790_000_000), 1_790_000_000_000)
  assert.equal(toEpochMs(1_790_000_000_000), 1_790_000_000_000)
  assert.equal(toEpochMs(null), null)
  assert.equal(toEpochMs(0), null)
  const now = 1_790_000_000_000
  assert.equal(formatAgo(now / 1000 - 20, now), 'just now')
  assert.equal(formatAgo(now - 12 * 60e3, now), '12 min ago')
  assert.equal(formatAgo(now / 1000 - 3 * 3600, now), '3 h ago')
  assert.ok(formatAgo(now - 3 * 86400e3, now).length > 3)
  assert.equal(formatAgo(null, now), '')
})

test('access model and what blocks turning Drive on', () => {
  assert.equal(driveAccessText({ linked: false }), 'Not linked')
  assert.match(driveAccessText({ linked: true, whole_drive: true }), /Whole Drive — choose the folders/)
  assert.match(driveAccessText({ linked: true, whole_drive: false }), /granted Cremind/)
  assert.equal(driveEnableBlocker(null, []), 'not_linked')
  assert.equal(driveEnableBlocker({ linked: false }, ['a']), 'not_linked')
  assert.equal(driveEnableBlocker({ linked: true, whole_drive: true }, []), 'folders_required')
  assert.equal(driveEnableBlocker({ linked: true, whole_drive: true }, ['f1']), null)
  // Per-file accounts may index everything granted.
  assert.equal(driveEnableBlocker({ linked: true, whole_drive: false }, []), null)
})

test('narrowing the Drive folders reads as a Drive removal', () => {
  assert.equal(
    effectLabel({ kind: 'purge_out_of_scope', files: 42, bytes: 0, detail: { kind: 'drive' } }),
    'Remove 42 Google Drive files that are not inside the chosen folders from the index.',
  )
  assert.match(effectLabel({ kind: 'purge_drive', files: 3, bytes: 0, detail: {} }), /3 files from Google Drive/)
  // The local wording is unchanged.
  assert.match(effectLabel({ kind: 'purge_out_of_scope', files: 2, bytes: 0, detail: { to: '/new' } }), /not inside \/new/)
})

// ── the folder picker model ─────────────────────────────────────────────────

test('picker path: in and back out, the top always stays', () => {
  const top = driveRootCrumb(true)
  assert.deepEqual(top, { id: null, name: 'My Drive' })
  assert.equal(driveRootCrumb(false).name, 'Shared with Cremind')
  const a = enterFolder([top], { id: 'A', name: 'Work' })
  const b = enterFolder(a, { id: 'B', name: 'Reports' })
  assert.deepEqual(b.map(c => c.id), [null, 'A', 'B'])
  assert.deepEqual(crumbsTo(b, 1).map(c => c.id), [null, 'A'])
  assert.deepEqual(crumbsTo(b, 0).map(c => c.id), [null])
  assert.deepEqual(crumbsTo(b, -5).map(c => c.id), [null])
  // The input path is not mutated.
  assert.equal(a.length, 2)
})

test('picker selection: toggle, and a chosen ancestor covers what is below it', () => {
  let chosen = toggleFolder([], { id: 'A', name: 'Work' })
  chosen = toggleFolder(chosen, { id: 'C', name: 'Photos' })
  assert.deepEqual(chosen.map(f => f.id), ['A', 'C'])
  chosen = toggleFolder(chosen, { id: 'A', name: 'Work' })
  assert.deepEqual(chosen.map(f => f.id), ['C'])

  const path = [driveRootCrumb(true), { id: 'A', name: 'Work' }, { id: 'B', name: 'Reports' }]
  assert.equal(coveringFolder(path, [{ id: 'C', name: 'Photos' }]), null)
  assert.deepEqual(coveringFolder(path, [{ id: 'A', name: 'Work' }]), { id: 'A', name: 'Work' })
})

test('picker changes: order does not matter, and dropping a folder narrows', () => {
  assert.equal(sameFolderSet(['a', 'b'], ['b', 'a']), true)
  assert.equal(sameFolderSet(['a'], ['a', 'b']), false)
  assert.equal(sameFolderSet([], []), true)
  assert.deepEqual(droppedFolders(['a', 'b', 'c'], ['c', 'd']), ['a', 'b'])
  assert.deepEqual(droppedFolders([], ['a']), [])
})

test('chosen folders are named from the view, then from the picker, then a fallback', () => {
  const named = namedFolders(
    ['id-from-view', 'id-learned', 'id-unknown-123', 'id-from-view'],
    ['id-bare', { id: 'id-from-view', name: 'Reports' }, { id: 'id-not-saved', name: 'Elsewhere' }],
    { 'id-learned': 'Photos' },
  )
  assert.deepEqual(named, [
    { id: 'id-from-view', name: 'Reports' },
    { id: 'id-learned', name: 'Photos' },
    { id: 'id-unknown-123', name: folderFallbackName('id-unknown-123') },
  ])
  assert.equal(folderFallbackName('1AbCdEfGhIjK'), 'Folder 1AbCdEfG…')
  assert.deepEqual(namedFolders([], null), [])
})

test('a pasted Drive folder link or id becomes a folder id; anything else does not', () => {
  const id = '1AbC_dEf-GhIjKlMnOpQ'
  assert.equal(parseDriveFolderRef(`https://drive.google.com/drive/folders/${id}`), id)
  assert.equal(parseDriveFolderRef(`https://drive.google.com/drive/u/0/folders/${id}?usp=sharing`), id)
  assert.equal(parseDriveFolderRef(`https://drive.google.com/open?id=${id}`), id)
  assert.equal(parseDriveFolderRef(`  ${id}  `), id)
  assert.equal(parseDriveFolderRef('Reports'), null)
  assert.equal(parseDriveFolderRef(''), null)
  assert.equal(parseDriveFolderRef(null), null)
  assert.equal(parseDriveFolderRef(`http://drive.google.com/drive/folders/${id}`), null)
  assert.equal(parseDriveFolderRef(`https://evil.example/drive/folders/${id}`), null)
  assert.equal(parseDriveFolderRef("x' or 'a' in parents"), null)
})

// ── files from both sources ─────────────────────────────────────────────────

test('only an https link ever becomes an href', () => {
  const link = 'https://docs.google.com/document/d/abc/edit'
  assert.equal(safeWebLink(link), link)
  assert.equal(safeWebLink(' HTTPS://drive.google.com/file/d/x/view '), 'HTTPS://drive.google.com/file/d/x/view')
  for (const bad of ['javascript:alert(1)', 'http://drive.google.com/x', 'data:text/html,hi', '//drive.google.com/x', '', null, undefined, 42]) {
    assert.equal(safeWebLink(bad), null, String(bad))
  }
})

test('source labels', () => {
  assert.equal(sourceLabel('drive'), 'Google Drive')
  assert.equal(sourceLabel('local'), 'This computer')
  assert.equal(sourceLabel(null), 'This computer')
})

// ── the client ──────────────────────────────────────────────────────────────

const URL_ = 'http://localhost:1515'
const TOKEN = 'jwt-ann'
let env = installBrowser()
const api = await load('src/services/userdocsApi.ts')

test('listDriveFolders: the top without a parent, a folder by encoded id', async () => {
  env = installBrowser()
  const listing = { folders: [{ id: 'A', name: 'Work' }], parent: null, whole_drive: true }
  env.route('/api/userdocs/drive/folders', () => json(listing))
  assert.deepEqual(await api.listDriveFolders(URL_, TOKEN, null), listing)
  await api.listDriveFolders(URL_, TOKEN, 'a/b c')
  const urls = env.callsTo('/api/userdocs/drive/folders').map(c => new URL(c.url))
  assert.equal(urls[0].search, '')
  assert.equal(urls[1].searchParams.get('parent'), 'a/b c')
  assert.equal(env.callsTo('/api/userdocs/drive/folders')[0].init.headers.Authorization, `Bearer ${TOKEN}`)
})

test('listDriveFolders errors keep their code', async () => {
  env = installBrowser()
  env.route('/api/userdocs/drive/folders', () => json({ error: 'DriveNotLinked', message: 'Link Google Drive first.' }, 409))
  const err = await api.listDriveFolders(URL_, TOKEN, null).then(() => null, e => e)
  assert.ok(err instanceof api.UserDocsApiError)
  assert.equal(err.code, 'DriveNotLinked')
  assert.equal(err.message, 'Link Google Drive first.')
})

test('a Drive settings save sends kind drive with include_folders, and DriveFoldersRequired is thrown', async () => {
  env = installBrowser()
  env.route('/api/userdocs/settings', (_url, init) => {
    const body = JSON.parse(init.body)
    if (!body.options?.include_folders?.length) {
      return json({ error: 'DriveFoldersRequired', message: 'Choose the Drive folders to index first.' }, 409)
    }
    return json({ settings: {}, snapshot: { v: 1, boot: 'b', seq: 2, enabled: true, state: 'idle', reason: null } })
  })
  const err = await api.saveUserDocsSettings(URL_, TOKEN, { kind: 'drive', enabled: true }).then(() => null, e => e)
  assert.equal(err.code, 'DriveFoldersRequired')
  const ok = await api.saveUserDocsSettings(URL_, TOKEN, { kind: 'drive', enabled: true, options: { include_folders: ['A'] } })
  assert.equal(ok.kind, 'done')
  assert.deepEqual(JSON.parse(env.callsTo('/api/userdocs/settings')[1].init.body),
    { kind: 'drive', enabled: true, options: { include_folders: ['A'] } })
})

test('turning Drive off comes back as a purge_drive plan to confirm', async () => {
  env = installBrowser()
  env.route('/api/userdocs/settings', (_url, init) => {
    const body = JSON.parse(init.body)
    if (body.confirm === 'd1') return json({ settings: {}, snapshot: { v: 1, boot: 'b', seq: 3, enabled: false, state: 'disabled', reason: null } })
    return json({
      error: 'ConfirmationRequired', message: 'Turning Drive off removes its index.',
      plan: { effects: [{ kind: 'purge_drive', files: 812, bytes: 0, detail: {} }], destructive: true },
      confirm: 'd1',
    }, 409)
  })
  const first = await api.saveUserDocsSettings(URL_, TOKEN, { kind: 'drive', enabled: false })
  assert.equal(first.kind, 'confirm')
  assert.equal(first.plan.effects[0].kind, 'purge_drive')
  assert.equal((await first.confirm()).kind, 'done')
  assert.deepEqual(env.callsTo('/api/userdocs/settings').map(c => JSON.parse(c.init.body)),
    [{ kind: 'drive', enabled: false }, { kind: 'drive', enabled: false, confirm: 'd1' }])
})

test('Drive control actions carry source=drive; local ones stay as they were', async () => {
  env = installBrowser()
  env.route('/api/userdocs/control', () => json({ accepted: true, snapshot: { v: 1, boot: 'b', seq: 4, enabled: true, state: 'idle', reason: null } }, 202))
  await api.controlUserDocs(URL_, TOKEN, { action: 'sync_now', source: 'drive' })
  await api.controlUserDocs(URL_, TOKEN, { action: 'confirm_deletions', source: 'drive' })
  await api.controlUserDocs(URL_, TOKEN, { action: 'rescan' })
  assert.deepEqual(env.callsTo('/api/userdocs/control').map(c => JSON.parse(c.init.body)), [
    { action: 'sync_now', source: 'drive' },
    { action: 'confirm_deletions', source: 'drive' },
    { action: 'rescan' },
  ])
})

test('the file listing filters by source', async () => {
  env = installBrowser()
  env.route('/api/userdocs/files', () => json({
    files: [{ fid: 'abcd1234', id: 1, rel_path: 'Drive/Work/plan.docx', name: 'plan.docx', source: 'drive', web_link: 'https://docs.google.com/document/d/x/edit' }],
    next: null, counts: { indexed: 1 },
  }))
  const page = await api.listUserDocsFiles(URL_, TOKEN, { source: 'drive' })
  assert.equal(page.files[0].source, 'drive')
  assert.equal(new URL(env.callsTo('/api/userdocs/files')[0].url).searchParams.get('source'), 'drive')
})
