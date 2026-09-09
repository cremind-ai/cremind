import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import test from 'node:test'
import { build, transform } from 'esbuild'

const here = path.dirname(fileURLToPath(import.meta.url))
// Main-process capability refreshes are intentionally fire-and-forget. Keep a
// stable asset root available if one settles just after a harness restores env.
process.env.VITE_PUBLIC ||= here
async function loadHelper(name) {
  const { code } = await transform(await readFile(path.join(here, name), 'utf8'), { loader: 'ts', format: 'esm' })
  return import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
}
const transport = await loadHelper('backendTransport.ts')
const stateHelpers = await loadHelper('transitionState.ts')
const vnc = await loadHelper('vncDesktop.ts')

test('installer dotenv values are parsed without executing or expanding them', () => {
  assert.deepEqual(transport.parseInstallEnv(`﻿# comment
export CREMIND_SSL = "after-setup" # selected
APP_URL='https://localhost:9443'
LITERAL=$(do-not-execute)
PASSWORD=abc#123
EMPTY= # omitted
INVALID="unfinished
`), {
    CREMIND_SSL: 'after-setup', APP_URL: 'https://localhost:9443',
    LITERAL: '$(do-not-execute)', PASSWORD: 'abc#123', EMPTY: '',
  })
})

test('origin resolution follows setup state, explicit TLS and custom ports', () => {
  const resolve = transport.resolveBackendOrigin
  assert.equal(resolve('', {}, false), 'http://127.0.0.1:1515')
  assert.equal(resolve('http://localhost:1515', { CREMIND_SSL: 'after-setup' }, false), 'http://localhost:1515')
  assert.equal(resolve('http://localhost:1515', { CREMIND_SSL: 'after-setup' }, true), 'https://localhost:1515')
  assert.equal(resolve('', { APP_URL: 'https://localhost:9443', CREMIND_SSL: 'after-setup' }, false), 'http://localhost:9443')
  for (const mode of ['auto', 'true', '1', 'yes']) {
    assert.equal(resolve('http://localhost:1515', { CREMIND_SSL: mode }, false), 'https://localhost:1515')
  }
  assert.equal(resolve('http://127.0.0.1:1515', { CREMIND_UI_PORT: '9443' }, true, true), 'https://127.0.0.1:9443')
  assert.equal(resolve('', { CREMIND_SSL_CERTFILE: 'cert.pem', CREMIND_SSL_KEYFILE: 'key.pem' }, false), 'https://127.0.0.1:1515')
  assert.equal(transport.httpsOrigin('http://localhost'), 'https://localhost:80')
  assert.equal(transport.httpOrigin('https://user:secret@example.com'), null)
})

test('native trust accepts the installed valid leaf and rejects unrelated, wrong-name and expired certificates', async () => {
  const [ca, leaf, other] = await Promise.all(['ca.pem', 'server.pem', 'other-server.pem'].map(name => readFile(path.join(here, 'fixtures', name), 'utf8')))
  const verify = (presented, expected, host, now = Date.parse('2026-09-06T00:00:00Z')) =>
    transport.isExpectedLocalCertificate(presented, expected, ca, host, now)
  assert.equal(verify(leaf, leaf, 'localhost'), true)
  assert.equal(verify(leaf, leaf, '127.0.0.1'), true)
  assert.equal(verify(leaf, leaf, '[::1]'), true)
  assert.equal(verify(leaf, leaf, 'unrelated.example'), false)
  assert.equal(verify(other, leaf, 'localhost'), false)
  assert.equal(verify(leaf, leaf, 'localhost', Date.parse('2031-01-01')), false)
  assert.equal(verify(leaf, leaf, 'localhost', Date.parse('2024-01-01')), false)
  assert.equal(verify('invalid PEM', leaf, 'localhost'), false)
  assert.equal(verify(ca, ca, 'localhost'), false)
})

test('handoffs retain only this window profile, preferences and its drafts', () => {
  const state = stateHelpers.filterTransitionState('http://localhost:1515/electron-renderer/#/alice/c/42', {
    local: {
      profile_id: 'bob', agent_token_alice: 'alice-token', agent_token_bob: 'bob-token',
      theme: 'dark', terminalPanelWidth: '520', chat_mode_alice: 'thinking',
      chat_mode_bob: 'instant', unknown: 'drop',
    },
    session: { 'cremind:draft:alice:42': 'draft', 'cremind:draft:bob:99': 'other draft', unrelated: 'drop' },
  })
  assert.deepEqual(state.local, {
    agent_token_alice: 'alice-token', theme: 'dark', terminalPanelWidth: '520',
    chat_mode_alice: 'thinking', profile_id: 'alice', logged_in_profiles: '["alice"]',
  })
  assert.deepEqual(state.session, { 'cremind:draft:alice:42': 'draft' })
  assert.equal(stateHelpers.transitionProfile('http://localhost/#/setup', 'alice'), 'alice')
  assert.equal(stateHelpers.transitionProfile('http://localhost/#/setup', 123), null)
  assert.throws(() => stateHelpers.filterTransitionState('http://localhost/#/alice', {
    session: { 'cremind:draft:alice:new': 'a'.repeat(4 * 1024 * 1024 + 1) },
  }), /too large/)
})

test('restart waits for actual child exit and forces a stuck owned process before respawning', async () => {
  const normal = Object.assign(new EventEmitter(), { pid: 123, exitCode: null, kill() {} })
  let stopped = false
  const graceful = transport.stopOwnedBackend(normal, () => assert.fail('unneeded forced stop'), 100, 100)
    .then(result => { stopped = true; return result })
  await new Promise(resolve => setTimeout(resolve, 10))
  assert.equal(stopped, false)
  normal.emit('exit', null, 'SIGTERM')
  assert.equal(await graceful, true)
  assert.equal(normal.listenerCount('exit'), 0)
  const stuck = Object.assign(new EventEmitter(), { pid: 456, exitCode: null, kill() {} })
  let killedPid
  assert.equal(await transport.stopOwnedBackend(stuck, pid => {
    killedPid = pid
    stuck.emit('exit', null, 'SIGKILL')
  }, 5, 5), true)
  assert.equal(killedPid, 456)
  assert.equal(await transport.stopOwnedBackend(stuck, () => {}, 5, 5), false)
})

test('owned HTTPS activation leaves enough time for the committed 202 response', async () => {
  assert.ok(transport.ACTIVATION_RESPONSE_GRACE_MS >= 1500)
  let released = false
  const waiting = transport.waitForActivationResponseGrace(20).then(() => { released = true })
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(released, false)
  await waiting
  assert.equal(released, true)
})

test('default-port HTTPS discovery requires the exact cached installation and transition', () => {
  const instanceId = 'a'.repeat(48)
  const status = {
    serving_https: true,
    instance_id: instanceId,
    transition: {
      phase: 'active', same_public_port: false,
      source_origin: 'http://cremind.example:80',
      target_origin: 'https://cremind.example:443',
    },
  }
  assert.equal(transport.defaultPortHttpsOrigin('http://cremind.example:80'), 'https://cremind.example')
  assert.equal(transport.defaultPortHttpsOrigin('http://cremind.example:1515'), null)
  assert.equal(transport.isExpectedDefaultPortTlsStatus(
    status, 'http://cremind.example', 'https://cremind.example', instanceId,
  ), true)
  assert.equal(transport.isExpectedDefaultPortTlsStatus(
    { ...status, instance_id: 'b'.repeat(48) },
    'http://cremind.example', 'https://cremind.example', instanceId,
  ), false)
  assert.equal(transport.isExpectedDefaultPortTlsStatus(
    { ...status, transition: { ...status.transition, source_origin: 'http://other.example' } },
    'http://cremind.example', 'https://cremind.example', instanceId,
  ), false)
  assert.equal(transport.isExpectedDefaultPortTlsStatus(
    { ...status, transition: { ...status.transition, target_origin: 'https://cremind.example:444' } },
    'http://cremind.example', 'https://cremind.example', instanceId,
  ), false)
  assert.equal(transport.isExpectedDefaultPortTlsStatus(
    { ...status, ready: false },
    'http://cremind.example', 'https://cremind.example', instanceId,
  ), false)
})

test('the noVNC window URL follows the backend descriptor, with the legacy Docker rule behind it', () => {
  const url = (descriptor, agentUrl = 'http://cremind.example:1515', mode = 'docker', flavor = 'desktop') =>
    vnc.vncUrlFor(agentUrl, descriptor, mode, flavor)
  assert.equal(vnc.VNC_WINDOW_QUERY, 'autoconnect=1&resize=remote')
  const query = `?${vnc.VNC_WINDOW_QUERY}`
  assert.equal(url({ enabled: true, access: 'direct', novnc_path: '/vnc.html', novnc_port: 6080 }),
    `http://cremind.example:6080/vnc.html${query}`)
  // Cremind's own HTTPS never covers noVNC's separate listener.
  assert.equal(url({ enabled: true, access: 'direct', novnc_port: 7000 }, 'https://cremind.example'),
    `http://cremind.example:7000/vnc.html${query}`)
  assert.equal(url({ enabled: true, access: 'direct' }, 'http://[::1]:1515'), `http://[::1]:6080/vnc.html${query}`)
  assert.equal(url({ enabled: true, access: 'same_origin', novnc_path: '/vnc/vnc.html' },
    'https://cremind.example', 'kubernetes', null), `https://cremind.example/vnc/vnc.html${query}`)
  assert.equal(url({ enabled: true, access: 'same_origin' }, 'http://cremind.example:1515', 'kubernetes', null),
    `http://cremind.example:1515/vnc/vnc.html${query}`)
  // A descriptor path is resolved against the origin, so it must never be
  // able to move the host.
  assert.equal(url({ enabled: true, access: 'same_origin', novnc_path: '//evil.example/vnc.html' },
    'https://cremind.example', 'kubernetes', null), `https://cremind.example/vnc/vnc.html${query}`)
  assert.equal(url({ enabled: true, access: 'port_forward', novnc_port: 6080, novnc_path: '/vnc.html' },
    'https://cremind.example', 'kubernetes', null), `http://localhost:6080/vnc.html${query}`)
  assert.equal(url({ enabled: false, access: null, novnc_path: null, novnc_port: null }), null)
  assert.equal(url({ enabled: true, access: null }), null)
  assert.equal(url({ enabled: true, access: 'direct' }, ''), null)
  // Backends older than the descriptor: Docker on a non-basic image meant 6080.
  const legacy = (mode, flavor) => vnc.vncUrlFor('https://cremind.example:1515', null, mode, flavor)
  assert.equal(legacy('docker', null), `http://cremind.example:6080/vnc.html${query}`)
  assert.equal(legacy('docker', 'desktop'), `http://cremind.example:6080/vnc.html${query}`)
  assert.equal(legacy('docker', 'basic'), null)
  assert.equal(legacy('native', null), null)
  assert.equal(legacy('kubernetes', null), null)
})

test('a renderer-supplied desktop URL is refused unless it is a plain http(s) target', () => {
  assert.equal(vnc.validVncUrl('http://127.0.0.1:6080/vnc.html'),
    'http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=remote')
  // An advertised URL that already carries connect parameters keeps them.
  assert.equal(vnc.validVncUrl('https://cremind.example/vnc/vnc.html?path=websockify'),
    'https://cremind.example/vnc/vnc.html?path=websockify')
  for (const refused of [
    'file:///etc/passwd', 'javascript:alert(1)', 'about:blank', 'ftp://cremind.example/vnc.html',
    'http://user:secret@127.0.0.1:6080/vnc.html', 'https://:secret@cremind.example/vnc.html',
    'not a url', '', 42, null, undefined, {}, ['http://127.0.0.1:6080/vnc.html'],
  ]) assert.equal(vnc.validVncUrl(refused), null, String(refused))
})

async function mainHarness(run) {
  const tmp = await mkdtemp(path.join(os.tmpdir(), 'cremind-electron-https-'))
  const savedEnv = { ...process.env }
  const handlers = new Map()
  const events = new Map()
  const windows = []
  const loaded = []
  const created = []
  const recovered = new Map()
  const eventFor = contents => ({ sender: contents, senderFrame: { parent: null, url: contents.getURL() } })
  // main.ts really constructs windows (tray entries, the VNC bridge), so the
  // stub has to be a constructor and not only the static lookup that the
  // first-party sender check uses.
  class StubBrowserWindow {
    constructor(options) {
      this.options = options
      this.url = ''
      this.webContents = Object.assign(new EventEmitter(), {
        id: 500 + created.length, getURL: () => this.url, send() {},
      })
      created.push(this)
      windows.push(this)
    }
    loadURL(next) { this.url = next; return Promise.resolve() }
    loadFile() { return Promise.resolve() }
    on() { return this }
    isDestroyed() { return false }
    isMinimized() { return false }
    isVisible() { return true }
    show() {}
    focus() {}
    restore() {}
    static getAllWindows() { return windows }
  }
  globalThis.__cremindElectronTest = {
    app: {
      isPackaged: true, getPath: () => tmp, setPath() {}, setAppUserModelId() {},
      requestSingleInstanceLock: () => false, quit() {}, setJumpList() {},
    },
    ipcMain: { handle: (name, fn) => handlers.set(name, fn), on: (name, fn) => events.set(name, fn) },
    BrowserWindow: StubBrowserWindow,
    session: { defaultSession: { fetch: async (url) => new Response(JSON.stringify(
      url.endsWith('/health') ? { status: 'ok' }
        : url.endsWith('/api/tls/status') ? {
          serving_https: true, ready: true, instance_id: 'instance',
          transition: {
            id: 'transition', phase: 'active',
            target_origin: 'https://127.0.0.1:1515', same_public_port: true,
          },
        }
        : { install_mode: 'native', ui_features: [] },
    )) } },
    Tray: class {}, Menu: {}, nativeImage: {}, shell: {},
  }
  function makeWindow(id, url, state = {}, ready = Promise.resolve()) {
    const contents = Object.assign(new EventEmitter(), {
      id, getURL: () => url,
      send(name) {
        if (name !== 'cremind:https:capture') return
        events.get('cremind:https:capture-ack')(eventFor(contents))
        void ready.then(() => events.get('cremind:https:captured')(eventFor(contents), state))
      },
    })
    const win = {
      webContents: contents, isDestroyed: () => false,
      loadURL(next) {
        url = next
        loaded.push([id, next])
        const event = eventFor(contents)
        events.get('cremind:https:consume-sync')(event)
        recovered.set(id, event.returnValue)
        return Promise.resolve()
      },
    }
    windows.push(win)
    return win
  }
  let helperWindowId = -1
  async function invokeFromApp(name, ...args) {
    const fileUrl = pathToFileURL(path.join(path.dirname(tmp), 'dist', 'index.html')).href
    const win = makeWindow(helperWindowId--, fileUrl)
    try {
      return await handlers.get(name)(eventFor(win.webContents), ...args)
    } finally {
      const index = windows.indexOf(win)
      if (index >= 0) windows.splice(index, 1)
    }
  }
  try {
    process.env.CREMIND_SYSTEM_DIR = path.join(tmp, 'system')
    process.env.CREMIND_INSTALL_DIR = path.join(tmp, 'install')
    process.env.CREMIND_SSL = ''
    delete process.env.CREMIND_SSL_CERTFILE
    delete process.env.CREMIND_SSL_KEYFILE
    delete process.env.VITE_DEV_SERVER_URL
    process.env.VITE_PUBLIC = tmp
    process.env.CREMIND_UI_PORT = '1515'
    const outfile = path.join(tmp, 'main.mjs')
    const result = await build({
      entryPoints: [path.join(here, 'main.ts')], bundle: true, platform: 'node', format: 'esm', write: false,
      external: ['electron-updater'], define: { __CREMIND_INSTALL_CHANNEL__: '"production"' },
      plugins: [{ name: 'electron-test', setup(builder) {
        builder.onResolve({ filter: /^electron$/ }, () => ({ path: 'electron', namespace: 'mock' }))
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({ contents: 'export const {app,ipcMain,BrowserWindow,session,Tray,Menu,nativeImage,shell} = globalThis.__cremindElectronTest' }))
      } }],
    })
    await writeFile(outfile, result.outputFiles[0].text)
    await import(pathToFileURL(outfile).href)
    await invokeFromApp('cremind:set-config', { agentUrl: 'http://127.0.0.1:1515', deploymentType: 'local' })
    await run({ handlers, events, makeWindow, loaded, created, recovered, eventFor, invokeFromApp, tmp })
    // Capabilities refresh runs in the background after a successful pivot.
    // Let its Electron menu rebuild finish before restoring process.env.
    await new Promise(resolve => setTimeout(resolve, 50))
  } finally {
    for (const key of Object.keys(process.env)) if (!(key in savedEnv)) delete process.env[key]
    Object.assign(process.env, savedEnv)
    delete globalThis.__cremindElectronTest
    assert.ok(tmp.startsWith(path.join(os.tmpdir(), 'cremind-electron-https-')))
    await rm(tmp, { recursive: true, force: true })
  }
}

test('preload IPC is limited to known top-level Cremind windows on the exact configured origin', async () => {
  await mainHarness(async ({ handlers, events, makeWindow, eventFor }) => {
    const appWindow = makeWindow(10, 'http://127.0.0.1:1515/electron-renderer/#/alice')
    assert.equal(
      (await handlers.get('cremind:get-config')(eventFor(appWindow.webContents))).agentUrl,
      'http://127.0.0.1:1515',
    )

    const external = makeWindow(11, 'https://provider.example/oauth')
    const oauthCallback = makeWindow(12, 'http://127.0.0.1:1515/api/oauth/callback')
    const recovery = makeWindow(13, 'data:text/html,HTTPS%20recovery')
    const wrongScheme = makeWindow(14, 'https://127.0.0.1:1515/electron-renderer/#/alice')
    const subframe = {
      ...eventFor(appWindow.webContents),
      senderFrame: { parent: {}, url: appWindow.webContents.getURL() },
    }
    const unknownContents = Object.assign(new EventEmitter(), {
      id: 99,
      getURL: () => appWindow.webContents.getURL(),
    })
    const unknownWindow = eventFor(unknownContents)

    for (const deniedEvent of [
      eventFor(external.webContents),
      eventFor(oauthCallback.webContents),
      eventFor(recovery.webContents),
      eventFor(wrongScheme.webContents),
      subframe,
      unknownWindow,
    ]) {
      await assert.rejects(
        async () => handlers.get('cremind:get-config')(deniedEvent),
        /top-level Cremind app window/,
      )
      const syncConfig = { ...deniedEvent }
      events.get('cremind:get-config-sync')(syncConfig)
      assert.deepEqual(syncConfig.returnValue, {})
      const syncHandoff = { ...deniedEvent }
      events.get('cremind:https:consume-sync')(syncHandoff)
      assert.equal(syncHandoff.returnValue, null)
    }

    const rejectedOperations = [
      ['cremind:set-config', { autoUpdate: false }],
      ['cremind:open-external', 'https://example.com'],
      ['cremind:open-vnc', 'http://127.0.0.1:6080/vnc.html'],
      ['cremind:backend-upgrade:apply', {}],
      ['cremind:installer:detect'],
      ['cremind:installer:list-versions'],
      ['cremind:installer:run', { deployment: 'local', mode: 'native' }],
      ['cremind:installer:cancel'],
      ['cremind:installer:uninstall', 'keep'],
      ['cremind:updater:check'],
      ['cremind:updater:download'],
      ['cremind:updater:install'],
    ]
    const externalEvent = eventFor(external.webContents)
    for (const [channel, payload] of rejectedOperations) {
      await assert.rejects(
        async () => handlers.get(channel)(externalEvent, payload),
        /top-level Cremind app window/,
        channel,
      )
    }

    const serverOperations = [
      ['cremind:server:prepare-https', { nextOrigin: 'https://127.0.0.1:1515' }],
      ['cremind:server:release-https'],
      ['cremind:server:start'],
      ['cremind:server:restart'],
      ['cremind:server:migrate-https', { nextOrigin: 'https://127.0.0.1:1515' }],
    ]
    for (const [channel, payload] of serverOperations) {
      assert.equal((await handlers.get(channel)(externalEvent, payload)).ok, false, channel)
    }
  })
})

test('the VNC bridge validates the renderer URL before loading it in the sandboxed desktop window', async () => {
  await mainHarness(async ({ created, invokeFromApp }) => {
    for (const refused of [
      'file:///etc/passwd', 'javascript:alert(1)', 'about:blank',
      'http://user:secret@127.0.0.1:6080/vnc.html', 'not a url', 42, null,
    ]) {
      const result = await invokeFromApp('cremind:open-vnc', refused)
      assert.equal(result.ok, false, String(refused))
      assert.ok(result.error)
    }
    assert.equal(created.length, 0)

    assert.deepEqual(await invokeFromApp('cremind:open-vnc', 'http://127.0.0.1:6080/vnc.html'), { ok: true })
    assert.equal(created.length, 1)
    const [desktop] = created
    assert.equal(desktop.url, 'http://127.0.0.1:6080/vnc.html?autoconnect=1&resize=remote')
    assert.equal(desktop.options.title, 'Cremind VNC Desktop')
    // Third-party content: no preload bridge, isolated and sandboxed.
    assert.equal(desktop.options.webPreferences.preload, undefined)
    assert.equal(desktop.options.webPreferences.contextIsolation, true)
    assert.equal(desktop.options.webPreferences.sandbox, true)
  })
})

test('main waits for all windows, preserves each route and profile, and excludes external/OAuth/VNC pages', async () => {
  await mainHarness(async ({ handlers, events, makeWindow, loaded, recovered, eventFor }) => {
    let phase = 'prepared'
    const handoffs = new Map()
    globalThis.__cremindElectronTest.session.defaultSession.fetch = async (url, init = {}) => {
      if (url.endsWith('/health')) return new Response(JSON.stringify({ status: 'ok' }))
      if (url.endsWith('/api/tls/status')) return new Response(JSON.stringify({
        serving_https: phase === 'active', ready: true, instance_id: 'instance',
        transition: {
          id: 'transition', phase,
          target_origin: 'https://127.0.0.1:1515', same_public_port: true,
        },
      }))
      if (url.endsWith('/api/tls/handoff/redeem')) {
        const payload = JSON.parse(init.body)
        return new Response(JSON.stringify(handoffs.get(payload.ticket) || {}))
      }
      if (url.endsWith('/api/tls/handoff')) {
        const request = JSON.parse(init.body)
        const authorization = init.headers.Authorization
        const profile = authorization.endsWith('alice-token') ? 'alice' : 'bob'
        const ticket = (profile === 'alice' ? 'a' : 'b').repeat(43)
        handoffs.set(ticket, { profile, token: `${profile}-token-new`, route: request.route })
        return new Response(JSON.stringify({ ticket }))
      }
      return new Response(JSON.stringify({ install_mode: 'native', ui_features: [] }))
    }
    let finishUpload
    const upload = new Promise(resolve => { finishUpload = resolve })
    const alice = makeWindow(1, 'http://127.0.0.1:1515/electron-renderer/#/alice/settings/security', {
      local: { agent_token_alice: 'alice-token', agent_token_bob: 'other-token' },
    })
    makeWindow(2, 'http://127.0.0.1:1515/electron-renderer/?popout=1#/bob/processes/42', {
      local: { agent_token_bob: 'bob-token' }, session: { 'cremind:draft:bob:42': 'unsent text' },
    }, upload)
    makeWindow(3, 'http://127.0.0.1:6080/vnc.html')
    makeWindow(4, 'https://provider.example/oauth')
    makeWindow(5, 'http://127.0.0.1:1515/api/oauth/callback')
    const migrate = handlers.get('cremind:server:migrate-https')
    assert.equal((await migrate(eventFor(alice.webContents), { nextOrigin: 'https://evil.example:1515' })).ok, false)
    assert.equal((await migrate(eventFor(alice.webContents), { nextOrigin: 'https://127.0.0.1:1515' })).ok, false)
    const subframe = { ...eventFor(alice.webContents), senderFrame: { parent: {}, url: alice.webContents.getURL() } }
    assert.equal((await migrate(subframe, { nextOrigin: 'https://127.0.0.1:1515' })).ok, false)
    const options = {
      nextOrigin: 'https://127.0.0.1:1515', transitionId: 'transition', instanceId: 'instance',
    }
    const preparing = handlers.get('cremind:server:prepare-https')(eventFor(alice.webContents), options)
    await new Promise(resolve => setTimeout(resolve, 25))
    assert.deepEqual(loaded, [])
    finishUpload()
    assert.equal((await preparing).ok, true)
    phase = 'active'
    const moving = migrate(eventFor(alice.webContents), options)
    assert.equal((await moving).ok, true)
    assert.deepEqual(loaded, [
      [1, 'https://127.0.0.1:1515/electron-renderer/#/alice/settings/security'],
      [2, 'https://127.0.0.1:1515/electron-renderer/?popout=1#/bob/processes/42'],
    ])
    assert.equal(recovered.get(1).local.agent_token_alice, 'alice-token-new')
    assert.equal(recovered.get(1).local.agent_token_bob, undefined)
    assert.equal(recovered.get(2).local.agent_token_bob, 'bob-token-new')
    assert.equal(recovered.get(2).session['cremind:draft:bob:42'], 'unsent text')
    const secondRead = eventFor(alice.webContents)
    events.get('cremind:https:consume-sync')(secondRead)
    assert.equal(secondRead.returnValue, null)
    assert.equal((await handlers.get('cremind:get-config')(eventFor(alice.webContents))).agentUrl, 'https://127.0.0.1:1515')
  })
})

test('external transition can move Electron from HTTP port 80 to verified HTTPS port 443', async () => {
  await mainHarness(async ({ handlers, makeWindow, loaded, eventFor, invokeFromApp }) => {
    await invokeFromApp('cremind:set-config', { agentUrl: 'http://127.0.0.1', deploymentType: 'remote' })
    let phase = 'prepared'
    const ticket = 'z'.repeat(43)
    globalThis.__cremindElectronTest.session.defaultSession.fetch = async (url, init = {}) => {
      if (url.endsWith('/health')) return new Response(JSON.stringify({ status: 'ok' }))
      if (url.endsWith('/api/tls/status')) return new Response(JSON.stringify({
        serving_https: true, ready: true, instance_id: 'edge-instance',
        transition: {
          id: 'edge-transition', phase, target_origin: 'https://127.0.0.1',
          same_public_port: false,
        },
      }))
      if (url.endsWith('/api/tls/handoff/redeem')) return new Response(JSON.stringify({
        profile: 'alice', token: 'alice-token-new', route: '/alice/settings/security',
      }))
      if (url.endsWith('/api/tls/handoff')) return new Response(JSON.stringify({ ticket }))
      return new Response(JSON.stringify({ install_mode: 'native', ui_features: [] }))
    }
    const main = makeWindow(1, 'http://127.0.0.1/electron-renderer/#/alice/settings/security', {
      local: { agent_token_alice: 'alice-token' },
    })
    const migrate = handlers.get('cremind:server:migrate-https')
    assert.equal((await migrate(eventFor(main.webContents), {
      nextOrigin: 'https://127.0.0.1',
    })).ok, false)
    const options = {
      nextOrigin: 'https://127.0.0.1', transitionId: 'edge-transition', instanceId: 'edge-instance',
    }
    assert.equal((await handlers.get('cremind:server:prepare-https')(eventFor(main.webContents), options)).ok, true)
    phase = 'active'
    const moved = await migrate(eventFor(main.webContents), options)
    assert.equal(moved.ok, true)
    assert.deepEqual(loaded, [[1, 'https://127.0.0.1/electron-renderer/#/alice/settings/security']])
  })
})

test('preflight waits for another window’s upload before activation and coalesces duplicate requests', async () => {
  await mainHarness(async ({ handlers, makeWindow, loaded, eventFor }) => {
    let finishUpload
    const upload = new Promise(resolve => { finishUpload = resolve })
    const main = makeWindow(1, 'http://127.0.0.1:1515/electron-renderer/#/alice/settings/security')
    makeWindow(2, 'http://127.0.0.1:1515/electron-renderer/#/bob', {}, upload)
    let phase = 'prepared'
    globalThis.__cremindElectronTest.session.defaultSession.fetch = async url => new Response(JSON.stringify(
      url.endsWith('/api/tls/status') ? {
        serving_https: false, instance_id: 'e'.repeat(48),
        transition: { id: 'preflight-transition', phase },
      } : url.endsWith('/health') ? { status: 'ok' } : { install_mode: 'native', ui_features: [] },
    ))
    let completed = false
    const preflight = handlers.get('cremind:server:prepare-https')
    const options = {
      nextOrigin: 'https://127.0.0.1:1515',
      transitionId: 'preflight-transition', instanceId: 'e'.repeat(48),
    }
    const first = preflight(eventFor(main.webContents), options).then(result => { completed = true; return result })
    const duplicate = preflight(eventFor(main.webContents), options)
    await new Promise(resolve => setTimeout(resolve, 25))
    assert.equal(completed, false)
    assert.deepEqual(loaded, [])
    finishUpload()
    assert.equal((await first).ok, true)
    assert.equal((await duplicate).ok, true)
    assert.deepEqual(loaded, [])
    phase = 'cancelled'
    await new Promise(resolve => setTimeout(resolve, 300))
  })
})

test('startup follows a verified HTTPS listener, retaining the legacy local renderer hostname', async () => {
  await mainHarness(async ({ handlers, invokeFromApp }) => {
    await invokeFromApp('cremind:set-config', { agentUrl: 'http://localhost:1515', deploymentType: 'local' })
    const probed = []
    globalThis.__cremindElectronTest.session.defaultSession.fetch = async url => {
      probed.push(url)
      return url.startsWith('http:')
        ? new Response(null, { status: 308, headers: { location: url.replace('http:', 'https:') } })
        : new Response(JSON.stringify({ status: 'ok' }))
    }
    const started = await invokeFromApp('cremind:server:start')
    assert.equal(started.ok, true)
    assert.equal(started.agentUrl, 'https://127.0.0.1:1515')
    assert.ok(probed.includes('http://127.0.0.1:1515/health'))
    assert.ok(probed.includes('https://127.0.0.1:1515/health'))
  })
})

test('startup adopts external HTTPS 443 only after caching and matching the instance on HTTP 80', async () => {
  await mainHarness(async ({ handlers, invokeFromApp, tmp }) => {
    const instanceId = 'c'.repeat(48)
    await invokeFromApp('cremind:set-config', {
      agentUrl: 'http://cremind.example', deploymentType: 'custom',
    })

    let phase = 'http'
    let statusInstanceId = instanceId
    const probed = []
    globalThis.__cremindElectronTest.session.defaultSession.fetch = async url => {
      probed.push(url)
      if (phase === 'http') {
        if (url === 'http://cremind.example/health') {
          return new Response(JSON.stringify({ status: 'ok' }))
        }
        if (url === 'http://cremind.example/api/tls/status') {
          return new Response(JSON.stringify({
            serving_https: false, ready: true, instance_id: instanceId,
          }))
        }
      } else {
        if (url === 'http://cremind.example/health') return new Response(null, { status: 426 })
        if (url === 'https://cremind.example:80/health') return new Response(null, { status: 503 })
        if (url === 'https://cremind.example/health') {
          return new Response(JSON.stringify({ status: 'ok' }))
        }
        if (url === 'https://cremind.example/api/tls/status') {
          return new Response(JSON.stringify({
            serving_https: true, ready: true, instance_id: statusInstanceId,
            transition: {
              id: 'edge-transition', phase: 'active', same_public_port: false,
              source_origin: 'http://cremind.example',
              target_origin: 'https://cremind.example',
            },
          }))
        }
      }
      return new Response(null, { status: 404 })
    }

    assert.equal((await invokeFromApp('cremind:server:start')).ok, true)
    assert.equal((await invokeFromApp('cremind:get-config')).backendInstanceId, instanceId)
    assert.equal(JSON.parse(await readFile(path.join(tmp, 'cremind-config.json'), 'utf8')).backendInstanceId, instanceId)

    phase = 'https'
    statusInstanceId = 'd'.repeat(48)
    assert.equal((await invokeFromApp('cremind:server:start')).ok, false)
    assert.equal((await invokeFromApp('cremind:get-config')).agentUrl, 'http://cremind.example')

    statusInstanceId = instanceId
    const started = await invokeFromApp('cremind:server:start')
    assert.equal(started.ok, true)
    assert.equal(started.agentUrl, 'https://cremind.example')
    assert.equal((await invokeFromApp('cremind:get-config')).agentUrl, 'https://cremind.example')
    assert.ok(probed.includes('https://cremind.example:80/health'))
    assert.ok(probed.includes('https://cremind.example/api/tls/status'))
  })
})
