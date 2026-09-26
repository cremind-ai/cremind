// The file panel's root is the signed-in profile's own working directory.
// Each profile's is private to it, so the store must never carry one profile's
// folder (or its conversations' cwds) into the next profile's session: a
// profile switch or logout clears them, the next seed fetches the new
// profile's folder, and an answer still in flight for the previous profile is
// dropped. The files API client carries the server's "another profile's
// working directory" code so the panel can say so instead of a bare 403.
import assert from 'node:assert/strict'
import test from 'node:test'

import { deferred, flush, installBrowser, json, load } from './harness.mjs'

const env = installBrowser()
window.cremind = { config: { agentUrl: 'http://localhost:1515' } }
// The panel store imports the chat store, which imports Element Plus (for
// ElNotification) and with it Vue's DOM renderer.
function fakeElement() {
  return {
    style: {}, dataset: {}, childNodes: [], classList: { add() {}, remove() {} },
    setAttribute() {}, removeAttribute() {}, appendChild() {}, insertBefore() {},
    addEventListener() {}, removeEventListener() {},
  }
}
document.createElement = fakeElement
document.createElementNS = fakeElement
document.createTextNode = fakeElement
document.createComment = fakeElement
document.body = fakeElement()
document.head = fakeElement()
document.documentElement = fakeElement()
window.navigator = globalThis.navigator

const M = await load('tests/entries/terminal-panel.ts')

const BASE = 'http://localhost:1515'
const FOLDERS = {
  admin: '/srv/cremind/workspaces/admin',
  javis: '/srv/cremind/workspaces/javis',
  bob: '/srv/cremind/workspaces/bob',
}
// vue-router reads the global `history` (the harness only fakes window's).
globalThis.history = window.history
for (const profile of Object.keys(FOLDERS)) {
  localStorage.setItem(`agent_token_${profile}`, `jwt-${profile}`)
}

/** The profile a request was sent for, from its `Bearer jwt-<profile>`. */
function profileOf(init) {
  return init.headers.Authorization.replace('Bearer jwt-', '')
}

/** A fresh Pinia signed in as `profile`. */
function fresh(profile = 'admin') {
  M.setActivePinia(M.createPinia())
  const settings = M.useSettingsStore()
  settings.agentUrl = BASE
  settings.profileId = profile
  settings.authToken = `jwt-${profile}`
  return { settings, panel: M.useTerminalPanelStore() }
}

/** `/api/files/cwd` answers each token with its own profile's folder. */
function serveCwd(respond) {
  env.route('/api/files/cwd', respond ?? ((_url, init) => json({ cwd: FOLDERS[profileOf(init)] })))
}

/** `/api/me` answers each token with its profile and that profile's folder. */
function serveMe(folders = FOLDERS) {
  env.route('/api/me', (_url, init) => {
    const profile = profileOf(init)
    return json({ sub: profile, profile, exp: null, iat: null, user_working_dir: folders[profile] })
  })
}

function cwdCalls() {
  return env.callsTo('/api/files/cwd').filter(c => (c.init.method ?? 'GET') === 'GET')
}

/** The app's two routes that matter here: the profile picker ('/', no
 *  profile param) and a chat page. The guard activates the route's profile
 *  like router/index.ts does; `seen` records what App.vue's route watch
 *  would be handed. */
function appRouter(settings) {
  const router = M.createRouter({
    // An explicit base: without one the router looks for a <base> element.
    history: M.createMemoryHistory('/'),
    routes: [
      { path: '/', name: 'home', component: {} },
      { path: '/:profile/chat', name: 'chat', component: {} },
    ],
  })
  router.beforeEach((to) => {
    if (to.params.profile) settings.activateProfile(String(to.params.profile))
    return true
  })
  const seen = []
  M.watch(
    () => router.currentRoute.value.params.profile,
    (profile, previous) => seen.push([profile, previous]),
  )
  return { router, seen }
}

async function go(router, path) {
  await router.push(path)
  await flush()
}

test('the seed is the signed-in profile\'s own folder, fetched once', async () => {
  serveCwd()
  const { panel } = fresh('javis')
  const before = cwdCalls().length

  await Promise.all([panel.seedUserCwd(), panel.seedUserCwd()])
  await panel.seedUserCwd()

  assert.equal(panel.userDefaultCwd, FOLDERS.javis)
  assert.equal(panel.userWorkingRoot, FOLDERS.javis)
  assert.equal(panel.cwd, FOLDERS.javis)
  const calls = cwdCalls().slice(before)
  assert.equal(calls.length, 1, 'concurrent and repeated seeds share one request')
  assert.equal(calls[0].init.headers.Authorization, 'Bearer jwt-javis')
})

test('a profile switch forgets the previous profile\'s folders and re-seeds the new one', async () => {
  serveCwd()
  const { settings, panel } = fresh('admin')
  await panel.seedUserCwd()
  panel.setConversationCwd('c-admin', `${FOLDERS.admin}/project`)
  panel.setSelectedFile(`${FOLDERS.admin}/secret.txt`)
  panel.pushFileEvent({ type: 'created', path: `${FOLDERS.admin}/new.txt` })
  assert.equal(panel.userWorkingRoot, FOLDERS.admin)

  settings.authToken = 'jwt-javis'
  settings.profileId = 'javis'
  panel.resetForProfileSwitch()

  assert.equal(panel.userDefaultCwd, '')
  assert.equal(panel.userWorkingRoot, '')
  assert.deepEqual(panel.cwdByConversation, {})
  assert.equal(panel.selectedFilePath, null)
  assert.equal(panel.lastFileEvent, null)
  assert.equal(panel.cwd, '', 'nothing of admin\'s tree roots the next profile\'s panel')

  await panel.seedUserCwd()
  assert.equal(panel.userDefaultCwd, FOLDERS.javis)
  assert.equal(panel.userWorkingRoot, FOLDERS.javis)
})

test('logout clears the folder too, so the next sign-in fetches its own', async () => {
  serveCwd()
  const { settings, panel } = fresh('admin')
  await panel.seedUserCwd()

  panel.resetForProfileSwitch()
  settings.authToken = ''
  await panel.seedUserCwd()
  assert.equal(panel.userDefaultCwd, '', 'no token, no seed')

  settings.authToken = 'jwt-javis'
  await panel.seedUserCwd()
  assert.equal(panel.userWorkingRoot, FOLDERS.javis)
})

test('an answer still in flight for the previous profile is dropped', async () => {
  for (const order of ['before the new seed', 'after the new seed']) {
    const pending = deferred()
    serveCwd((_url, init) => {
      if (init.headers.Authorization === 'Bearer jwt-admin') return pending.promise
      return json({ cwd: FOLDERS.javis })
    })
    const { settings, panel } = fresh('admin')
    const stale = panel.seedUserCwd()

    settings.authToken = 'jwt-javis'
    panel.resetForProfileSwitch()
    if (order === 'before the new seed') {
      pending.resolve(json({ cwd: FOLDERS.admin }))
      await stale
      await flush()
      assert.equal(panel.userDefaultCwd, '', `${order}: admin's late answer never lands`)
      await panel.seedUserCwd()
    } else {
      await panel.seedUserCwd()
      pending.resolve(json({ cwd: FOLDERS.admin }))
      await stale
      await flush()
    }
    assert.equal(panel.userDefaultCwd, FOLDERS.javis, order)
    assert.equal(panel.userWorkingRoot, FOLDERS.javis, order)
  }
})

test('switching profile through the picker (\'/\' → \'/bob/chat\') drops admin\'s folder', async () => {
  serveCwd()
  serveMe()
  env.route('/api/terminals', (_url, init) => json({
    terminal_id: 't-1', shell: 'bash', title: 'bash', working_dir: JSON.parse(init.body).cwd,
  }))
  const { settings, panel } = fresh('admin')
  const stop = M.followSignedInProfile()
  const { router, seen } = appRouter(settings)

  await go(router, '/admin/chat')
  await panel.seedUserCwd()
  panel.setConversationCwd('c-admin', `${FOLDERS.admin}/project`)
  assert.equal(panel.userWorkingRoot, FOLDERS.admin)

  await go(router, '/')           // NavRail → "Switch profile"
  await go(router, '/bob/chat')   // the picker opens bob
  assert.deepEqual(seen.at(-1), ['bob', undefined],
    'the route watch never sees admin → bob, so it cannot be what resets the panel')
  assert.equal(settings.profileId, 'bob')
  assert.equal(panel.userDefaultCwd, '')
  assert.equal(panel.userWorkingRoot, '')
  assert.deepEqual(panel.cwdByConversation, {})

  await panel.seedUserCwd()
  assert.equal(panel.userWorkingRoot, FOLDERS.bob, "bob's breadcrumb floor is bob's folder")
  assert.equal(cwdCalls().at(-1).init.headers.Authorization, 'Bearer jwt-bob')
  await panel.newTerminal()
  const spawn = env.callsTo('/api/terminals').at(-1)
  assert.equal(JSON.parse(spawn.init.body).cwd, FOLDERS.bob, "a new terminal opens in bob's folder")
  stop()
})

test('logout drops the folder through the same identity watch', async () => {
  serveCwd()
  const { settings, panel } = fresh('admin')
  const stop = M.followSignedInProfile()
  await panel.seedUserCwd()

  // App.vue's handleLogout clears the session and nothing else.
  settings.authToken = ''
  settings.profileId = ''
  await flush()
  assert.equal(panel.userWorkingRoot, '')
  assert.equal(panel.cwd, '')

  settings.authToken = 'jwt-bob'
  settings.profileId = 'bob'
  await flush()
  await panel.seedUserCwd()
  assert.equal(panel.userWorkingRoot, FOLDERS.bob)
  stop()
})

test('the admin moving its own folder re-seeds this session; moving another profile\'s does not', async () => {
  const folders = { ...FOLDERS }
  serveCwd((_url, init) => json({ cwd: folders[profileOf(init)] }))
  serveMe(folders)
  const { settings, panel } = fresh('admin')
  settings.workingDir = FOLDERS.admin
  await panel.seedUserCwd()
  panel.setConversationCwd('c-admin', FOLDERS.admin)
  const before = { cwd: cwdCalls().length, me: env.callsTo('/api/me').length }

  folders.bob = '/data/bob-elsewhere'
  await panel.workingDirChanged('bob')
  assert.equal(panel.userWorkingRoot, FOLDERS.admin, "bob's move leaves admin's panel alone")
  assert.deepEqual(panel.cwdByConversation, { 'c-admin': FOLDERS.admin })
  assert.equal(settings.workingDir, FOLDERS.admin)
  assert.equal(cwdCalls().length, before.cwd)
  assert.equal(env.callsTo('/api/me').length, before.me)

  folders.admin = '/data/admin-new'
  await panel.workingDirChanged('admin')
  assert.equal(panel.userDefaultCwd, '/data/admin-new')
  assert.equal(panel.userWorkingRoot, '/data/admin-new', 'the breadcrumb floor follows the move')
  assert.deepEqual(panel.cwdByConversation, {}, 'no conversation stays rooted in the old folder')
  assert.equal(panel.cwd, '/data/admin-new')
  assert.equal(settings.workingDir, '/data/admin-new')
})

test('a seed still in flight for the old folder is dropped when the admin moves it', async () => {
  const before = deferred()
  const after = deferred()
  let moved = false
  serveCwd(() => (moved ? after.promise : before.promise))
  serveMe({ ...FOLDERS, admin: '/data/admin-new' })
  const { panel } = fresh('admin')
  const stale = panel.seedUserCwd()

  moved = true
  const reseed = panel.workingDirChanged('admin')
  // Same token, same profile: only the reset tells the old answer apart.
  before.resolve(json({ cwd: FOLDERS.admin }))
  await stale
  await flush()
  assert.equal(panel.userDefaultCwd, '', "the old folder's late answer never lands")
  after.resolve(json({ cwd: '/data/admin-new' }))
  await reseed
  await flush()
  assert.equal(panel.userDefaultCwd, '/data/admin-new')
  assert.equal(panel.userWorkingRoot, '/data/admin-new')
})

test('a 403 for another profile\'s working directory is told apart from other refusals', async () => {
  env.route('/api/files/list', (url) => {
    if (url.includes(encodeURIComponent(FOLDERS.admin))) {
      return json({
        error: "That location belongs to another profile's working directory",
        code: M.FOREIGN_WORKSPACE_CODE,
      }, 403)
    }
    return json({ error: 'Access denied' }, 403)
  })
  env.route('/api/files/cwd', (_url, init) => {
    if (init.method === 'POST') {
      return json({
        error: "That location belongs to another profile's working directory",
        code: M.FOREIGN_WORKSPACE_CODE,
      }, 403)
    }
    return json({ cwd: FOLDERS.javis })
  })

  const foreign = await M.listDirectory(BASE, 'jwt-javis', FOLDERS.admin, false).catch(e => e)
  assert.ok(foreign instanceof M.DirectoryAccessError)
  assert.equal(foreign.status, 403)
  assert.equal(foreign.code, M.FOREIGN_WORKSPACE_CODE)
  assert.match(foreign.message, /belongs to another profile/)
  assert.equal(M.isForeignWorkspaceError(foreign), true)

  const other = await M.listDirectory(BASE, 'jwt-javis', '/etc', false).catch(e => e)
  assert.equal(other.status, 403)
  assert.equal(other.code, undefined)
  assert.equal(M.isForeignWorkspaceError(other), false)
  assert.equal(M.isForeignWorkspaceError(new Error('x')), false)

  const cwd = await M.setConversationCwd(BASE, 'jwt-javis', 'c1', FOLDERS.admin).catch(e => e)
  assert.equal(M.isForeignWorkspaceError(cwd), true)
})
