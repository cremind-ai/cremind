// Where the UI finds its backend (getAgentUrl in src/services/runtimeConfig.ts)
// and the first-run gate that depends on it. A fresh desktop install has no
// backend until its setup wizard configures one, and the router sends it to
// the wizard only while both the bridge's agentUrl and the settings store's
// are empty. Two fallbacks used to fill the store anyway, so a fresh install
// showed the login page and called an address with no backend behind it:
//   - the page origin: a packaged app's window loads from file://, whose
//     origin is the non-empty "file://" in Electron ("null" in Node's URL);
//   - VITE_AGENT_URL: a developer's ui/.env.local, which builds read too.
import assert from 'node:assert/strict'
import test from 'node:test'

import { flush, installBrowser, load } from './harness.mjs'

const env = installBrowser()

const ASAR_PAGE = 'file:///C:/Program%20Files/Cremind%20App/resources/app.asar/dist/index.html#/'
const DEV_API = 'http://localhost:1112'

// The constants Vite bakes into a bundle: `vite build` or the dev server, with
// or without a VITE_AGENT_URL from the environment or a .env file.
function baked(dev, agentUrl) {
  return {
    define: {
      'import.meta.env.DEV': String(dev),
      'import.meta.env.VITE_AGENT_URL': agentUrl === undefined ? 'undefined' : JSON.stringify(agentUrl),
    },
  }
}

const ENTRY = 'tests/entries/agent-url.ts'
const build = await load(ENTRY, baked(false))
const buildWithUrl = await load(ENTRY, baked(false, DEV_API))
const devServer = await load(ENTRY, baked(true))
const devServerWithUrl = await load(ENTRY, baked(true, DEV_API))

function desktop(page, agentUrl = '') {
  env.setHref(page)
  window.cremind = { config: { agentUrl } }
}

function browser(page) {
  env.setHref(page)
  window.cremind = undefined
}

// The first-run gate in src/router/index.ts.
function gateSendsToSetup(M) {
  M.setActivePinia(M.createPinia())
  return !window.cremind.config.agentUrl && !M.useSettingsStore().agentUrl
}

test('a fresh desktop install goes to the setup wizard', () => {
  desktop(ASAR_PAGE)
  assert.ok(window.location.origin, 'a file:// page still has a non-empty origin')
  for (const M of [build, buildWithUrl]) {
    assert.equal(M.getAgentUrl(), '')
    assert.ok(gateSendsToSetup(M))
  }
  // The wizard seeds the backend it installs from its own default.
  assert.equal(build.defaultAgentOrigin(), 'http://localhost:1515')
})

test('before a backend is configured, App.vue opens no embedding stream', async () => {
  desktop(ASAR_PAGE)
  build.setActivePinia(build.createPinia())
  const store = build.useEmbeddingStatusStore()
  // An empty URL used to become the page origin: file:///api/config/embedding/stream,
  // retried for as long as the installer ran.
  store.connect(build.useSettingsStore().agentUrl)
  await flush()
  assert.equal(env.callsTo('/api/config/embedding/stream').length, 0)
  store.disconnect()
})

test("the desktop app's configured backend always wins", () => {
  desktop(ASAR_PAGE, 'http://127.0.0.1:1515')
  for (const M of [build, buildWithUrl, devServer, devServerWithUrl]) {
    assert.equal(M.getAgentUrl(), 'http://127.0.0.1:1515')
    assert.ok(!gateSendsToSetup(M))
  }
})

test('a desktop page the backend served uses that backend', () => {
  desktop('http://127.0.0.1:1515/electron-renderer/index.html#/')
  assert.equal(buildWithUrl.getAgentUrl(), 'http://127.0.0.1:1515')
  desktop('https://127.0.0.1:1515/electron-renderer/index.html#/')
  assert.equal(build.getAgentUrl(), 'https://127.0.0.1:1515')
})

test("under the desktop dev server only a developer's VITE_AGENT_URL applies", () => {
  desktop('http://localhost:5173/#/')
  assert.equal(devServerWithUrl.getAgentUrl(), DEV_API)
  // The page origin is Vite's, which serves no API: run the setup wizard.
  assert.equal(devServer.getAgentUrl(), '')
})

test('the web app uses VITE_AGENT_URL, else its own origin', () => {
  browser('https://cremind.example.com/#/')
  assert.equal(buildWithUrl.getAgentUrl(), DEV_API)
  assert.equal(build.getAgentUrl(), 'https://cremind.example.com')
  browser('http://localhost:1515/#/')
  assert.equal(devServerWithUrl.getAgentUrl(), DEV_API)
  assert.equal(devServer.getAgentUrl(), 'http://localhost:1515')
})
