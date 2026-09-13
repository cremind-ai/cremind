// Which install modes a front-end may offer.
//
// The catalog's per-mode `requires` is a visibility gate shared by install.sh,
// install.ps1, the terminal TUI and the Electron installer stage. Only the
// last one lives here, and it is the odd one out: it probes Docker and Python
// and nothing else, so it must not list a mode whose requirements it cannot
// answer — Kubernetes, whose install runs from the scripts.
//
// The distinction between "probed" and "available" is the whole test. Using
// availability for visibility would hide Docker whenever the daemon is down,
// deleting the shown-but-disabled row that explains why.
import assert from 'node:assert/strict'
import test from 'node:test'

import { load } from './harness.mjs'

const api = await load('src/services/installCatalogApi.ts')
const catalog = api.getBundledInstallCatalog()

test('the bundled catalog carries the kubernetes mode and its requirements', () => {
  assert.ok(catalog.modes.kubernetes, 'kubernetes missing from the bundled catalog')
  assert.deepEqual(catalog.modes.kubernetes.requires, ['kubectl', 'helm'])
  // Order is the recommendation, and kubernetes must never outrank Docker.
  assert.ok(catalog.modes.kubernetes.order > catalog.modes.docker.order)
  // native declares nothing, so some mode is always available.
  assert.deepEqual(catalog.modes.native.requires ?? [], [])
})

test('the installer stage hides modes it cannot probe', () => {
  const caps = { hasDocker: true } // what detectInstallEnvironment reports
  assert.equal(api.isInstallModeProbed(catalog.modes.kubernetes, caps), false)
  assert.equal(api.isInstallModeProbed(catalog.modes.docker, caps), true)
  assert.equal(api.isInstallModeProbed(catalog.modes.native, caps), true)
})

test('a probed-but-absent capability keeps its row visible', () => {
  // Docker stays on the list, disabled, with an explanation — hiding it would
  // leave the user wondering where it went.
  const caps = { hasDocker: false }
  assert.equal(api.isInstallModeProbed(catalog.modes.docker, caps), true)
  assert.equal(api.isInstallModeAvailable(catalog.modes.docker, caps), false)
})

test('availability needs every requirement probed and true', () => {
  const all = { hasDocker: true, hasKubectl: true, hasHelm: true }
  assert.equal(api.isInstallModeAvailable(catalog.modes.kubernetes, all), true)
  assert.equal(
    api.isInstallModeAvailable(catalog.modes.kubernetes, { hasDocker: true, hasKubectl: true }),
    false,
    'helm unprobed must not count as present',
  )
  // An unknown requirement is conservatively unmet, so a typo in catalog.toml
  // hides a mode instead of advertising a broken one.
  assert.equal(api.isInstallModeAvailable({ requires: ['nonsense'] }, all), false)
})

test('the recommendation never lands on kubernetes in the desktop app', () => {
  assert.equal(api.recommendInstallMode(catalog, { hasDocker: true }), 'docker')
  assert.equal(api.recommendInstallMode(catalog, { hasDocker: false }), 'native')
  // Even with everything present, catalog order keeps Docker first.
  assert.equal(
    api.recommendInstallMode(catalog, { hasDocker: true, hasKubectl: true, hasHelm: true }),
    'docker',
  )
  assert.equal(
    api.recommendInstallMode(catalog, { hasDocker: false, hasKubectl: true, hasHelm: true }),
    'native',
  )
})
