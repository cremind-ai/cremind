// What the Setup Wizard's "Secure this install" step actually shows, rendered
// from the real components through Vite's SSR loader.
//
// The bug this exists for: the installer trusts the local CA on the host
// automatically, and the wizard then showed a wall of manual per-OS trust
// commands and blocked Next on a checkbox anyway. It could not do better —
// a container's server can only read the container's trust store, and the
// step consulted nothing but the user's own tick.
//
// Two rules are pinned here, and they pull in opposite directions:
//   1. when something vouches for this device, the step asks for nothing;
//   2. when nothing does, it blocks exactly as it always did.
//
// Note where the decision lives. ``resolveTrustEvidence`` is called by the
// component that owns the gate (SetupWizard, SecuritySettings) and handed
// down. Having the panel work it out and report upward renders one frame too
// late — the step's own alert and checkbox are built before the child's
// answer arrives, so they flash on screen. That regression is invisible in a
// unit test of the function and obvious here, which is why this renders.
import assert from 'node:assert/strict'
import test, { after, before } from 'node:test'
import { createSSRApp, h } from 'vue'
import { renderToString } from '@vue/server-renderer'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'

// CaTrustPanel sniffs the platform at setup to re-order its command list.
// Node's own `navigator` has no `platform`, which is fine (the detection
// degrades to null) — older runtimes have no `navigator` at all, which is not.
if (typeof globalThis.navigator === 'undefined') {
  Object.defineProperty(globalThis, 'navigator', {
    value: { userAgent: 'node' }, configurable: true,
  })
}

let vite
let StepSecureInstall
let resolveTrustEvidence

before(async () => {
  vite = await createServer({
    root: new URL('..', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1'),
    // Never the repo's own vite.config.ts: that one builds the Electron app
    // and would drag vite-plugin-electron into a unit test. Everything these
    // components import is explicit (element-plus, @iconify/vue, relative
    // paths), so the vue plugin alone is the whole toolchain they need.
    configFile: false,
    plugins: [vue()],
    server: { middlewareMode: true },
    appType: 'custom',
    logLevel: 'error',
    // The step is reached in a browser far more often than in the desktop
    // app; individual cases pass isElectron explicitly.
    define: { __IS_ELECTRON__: 'false' },
  })
  ;({ default: StepSecureInstall } =
    await vite.ssrLoadModule('/src/components/setup/StepSecureInstall.vue'))
  ;({ resolveTrustEvidence } = await vite.ssrLoadModule('/src/services/caTrustHint.ts'))
})

after(async () => { await vite?.close() })

const CA = 'D2:2E:68:13:89:2A:25:EA:DE:2B:15:90:A5:5A:10:78:34:84:F0:4C:AF:D7:24:E6:96:0C:DA:4A:05:AB:03:25'

/** ``supported`` is false in every container — app/api/tls.py refuses to even
 *  try there — which also forces ``already_trusted`` to null. */
function tls({ supported = false, already = null, mode = 'after-setup' } = {}) {
  return {
    mode,
    serving_https: false,
    pending_https: true,
    ca_sha256: CA,
    https_url: 'https://localhost:1515',
    restart_supported: true,
    local_trust: {
      supported,
      store: supported ? "the current user's Trusted Root store" : null,
      os_prompt: supported ? 'windows' : null,
      already_trusted: already,
      reason: supported ? null : 'This server runs in a container.',
    },
  }
}

/** Mirrors SetupWizard: resolve the evidence, then hand it to the step. */
async function render({ tls: status, installMode, installerTrusted = false, isElectron = false, justTrusted = false }) {
  const evidence = resolveTrustEvidence({
    justTrusted,
    serverSaysTrusted: status?.local_trust?.already_trusted,
    installerTrusted,
    isElectron,
    installMode,
    tlsMode: status?.mode ?? null,
  })
  const html = await renderToString(createSSRApp({
    render: () => h(StepSecureInstall, {
      tls: status,
      installMode,
      evidence,
      agentUrl: 'http://localhost:1515',
      confirmed: false,
      justTrusted,
    }),
  }))
  return { html, evidence }
}

const CHECKBOX = 'I verified the fingerprint and trusted this exact CA'
const REQUIRED = 'Trust is required before activation'
const COMMANDS = 'certutil -addstore -user Root'

test('a fresh native install still asks, and offers the one click', async () => {
  const { html, evidence } = await render({
    tls: tls({ supported: true, already: false }), installMode: 'native',
  })
  assert.equal(evidence, null)
  assert.ok(html.includes(REQUIRED))
  assert.ok(html.includes(CHECKBOX))
  assert.ok(html.includes('Trust it on this device'))
})

test('a CA already in this machine\'s store asks for nothing', async () => {
  const { html, evidence } = await render({
    tls: tls({ supported: true, already: true }), installMode: 'native',
  })
  assert.equal(evidence, 'already')
  assert.ok(!html.includes(REQUIRED))
  assert.ok(!html.includes(CHECKBOX), 'the checkbox was the thing blocking Next')
  assert.ok(html.includes('Already trusted'))
})

// The reported bug, end to end.
test('a Kubernetes install the installer already trusted asks for nothing', async () => {
  const { html, evidence } = await render({
    tls: tls(), installMode: 'kubernetes', installerTrusted: true,
  })
  assert.equal(evidence, 'installer')
  assert.ok(!html.includes(REQUIRED))
  assert.ok(!html.includes(CHECKBOX))
  assert.ok(html.includes('Trusted during installation'))
  assert.ok(!html.includes(COMMANDS), 'the wall of manual commands is the complaint')
  assert.ok(html.includes('Trusting another device'), 'but they stay one click away')
})

test('a container install with no hint blocks exactly as before', async () => {
  for (const installMode of ['kubernetes', 'docker']) {
    const { html, evidence } = await render({ tls: tls(), installMode })
    assert.equal(evidence, null, installMode)
    assert.ok(html.includes(REQUIRED), installMode)
    assert.ok(html.includes(CHECKBOX), installMode)
    assert.ok(html.includes(COMMANDS), installMode)
    // Kubernetes never used to see this note — it was gated on Docker alone.
    assert.ok(html.includes('Installed with the Cremind installer'), installMode)
  }
})

test('the desktop app vouches for itself but still points at other browsers', async () => {
  const { html, evidence } = await render({
    tls: tls({ supported: true, already: false }), installMode: 'native', isElectron: true,
  })
  assert.equal(evidence, 'electron')
  assert.ok(!html.includes(CHECKBOX))
  assert.ok(html.includes('The desktop app trusts this certificate'))
  assert.ok(html.includes('Trust it on this device'), 'other browsers on the machine still need it')
})

// Mirrors the bail conditions of setCertificateVerifyProc in
// ui/electron/main.ts: it fills in the missing local CA and nothing else.
test('the desktop app claims nothing it cannot verify', async () => {
  const container = await render({ tls: tls(), installMode: 'kubernetes', isElectron: true })
  assert.equal(container.evidence, null, 'a container CA is not the one Chromium was handed')
  const custom = await render({
    tls: tls({ supported: true, mode: 'custom' }), installMode: 'native', isElectron: true,
  })
  assert.equal(custom.evidence, null, 'an operator-supplied certificate is not ours to vouch for')
})

test('a successful one-click trust satisfies the step by itself', async () => {
  const { html, evidence } = await render({
    tls: tls({ supported: true, already: false }), installMode: 'native', justTrusted: true,
  })
  assert.equal(evidence, 'just-trusted')
  assert.ok(!html.includes(CHECKBOX), 'clicking the button used to leave Next dead')
  assert.ok(html.includes('Certificate trusted'))
})
