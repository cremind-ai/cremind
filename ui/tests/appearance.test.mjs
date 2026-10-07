// Appearance: the color theme, font and text size a profile picks, and how a
// reply's Thinking Process opens.
//
// People asked for more than a light/dark switch — some found both too harsh —
// so a theme is now one of several presets or four custom colors, from which
// every CSS variable the app and Element Plus read is computed. These tests
// pin what that must not break: Light and Dark stay exactly today's look, every
// preset keeps its text readable, a custom theme decides light or dark from its
// own colors, and the store paints before the server answers, saves after a
// pause, never lets a stale read undo an edit, and carries over the old
// per-browser dark switch once.
//
// The Thinking Process used to open itself for the whole turn; now it stays
// closed until clicked, unless the profile turns on Auto-open — a switch on the
// row itself (chat.thinking_process), read and saved with the appearance.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

import { deferred, flush, installBrowser, json, load, until } from './harness.mjs'

const here = path.dirname(fileURLToPath(import.meta.url))
const AGENT = 'http://localhost:1515'

// The pure helpers ride the same bundle as the store, whose settings store
// reads localStorage as it loads.
installBrowser()
const M = await load('tests/entries/appearance.ts')

// ── Light and Dark are today's themes, value for value ──────────────────────

function cssBlock(selector) {
  const css = readFileSync(path.join(here, '..', 'src', 'style.css'), 'utf8')
  const start = css.indexOf(`${selector} {`)
  assert.ok(start >= 0, `style.css has no ${selector} block`)
  const body = css.slice(start, css.indexOf('}', start))
  return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map(m => [m[1], m[2].trim()]))
}

// Layout and type sizes, not colors: no theme sets them.
const NOT_COLORS = /^--(rail-width|conversations-panel-width|el-font-size-)/

for (const [id, selector] of [['light', ':root'], ['dark', '[data-theme="dark"]']]) {
  test(`the ${id} preset paints every color style.css gives ${id}, unchanged`, () => {
    const css = cssBlock(selector)
    const vars = M.computeAppearance({ ...M.DEFAULT_APPEARANCE, theme: id }, false).vars
    for (const [name, value] of Object.entries(css)) {
      if (NOT_COLORS.test(name)) continue
      assert.equal(vars[name], value, `${id}: ${name}`)
    }
  })
}

// ── Every preset ─────────────────────────────────────────────────────────────

test('every preset sets the same variables, so switching never leaves one behind', () => {
  const keys = M.THEME_PRESETS.map(p => Object.keys(M.themeTokens(p.palette)).sort().join(','))
  assert.equal(new Set(keys).size, 1)
  const custom = M.computeAppearance({ ...M.DEFAULT_APPEARANCE, theme: 'custom' }, false).vars
  assert.equal(Object.keys(custom).filter(k => !k.startsWith('--font-') && k !== '--root-font-size').sort().join(','), keys[0])
})

test('every preset keeps its text readable', () => {
  for (const { id, palette } of M.THEME_PRESETS) {
    const t = M.themeTokens(palette)
    assert.ok(M.contrast(t['--text-primary'], t['--surface-color']) >= 4.5, `${id}: text on panels`)
    assert.ok(M.contrast(t['--text-primary'], t['--bg-color']) >= 4.5, `${id}: text on the background`)
    assert.ok(M.contrast(t['--text-secondary'], t['--surface-color']) >= 4.5, `${id}: secondary text`)
    assert.ok(M.contrast(t['--on-primary'], t['--primary-color']) >= 3, `${id}: text on the accent`)
  }
})

test('dark text goes on an accent too light for white', () => {
  const nord = M.computeAppearance({ ...M.DEFAULT_APPEARANCE, theme: 'nord' }, false)
  assert.equal(nord.onPrimary, 'dark')
  assert.equal(nord.vars['--on-primary'], '#2e3440', 'the theme\'s own background')
  const light = M.computeAppearance(M.DEFAULT_APPEARANCE, false)
  assert.equal(light.onPrimary, 'light')
  assert.equal(M.readableOn('#facc15', '#334155'), '#334155')
  assert.equal(M.readableOn('#2563eb', '#334155'), '#ffffff')
})

test('Match system is Light or Dark as the device says', () => {
  const system = { ...M.DEFAULT_APPEARANCE, theme: 'system' }
  assert.equal(M.computeAppearance(system, false).vars['--bg-color'], '#f8fafc')
  assert.equal(M.computeAppearance(system, true).vars['--bg-color'], '#0f172a')
  assert.deepEqual(M.themeChoices(system).map(c => c.id), [
    'light', 'dark', 'system', 'paper', 'mint', 'rose', 'dim', 'nord', 'midnight', 'high_contrast', 'custom',
  ])
})

// ── Custom colors ────────────────────────────────────────────────────────────

test('a custom theme is dark when its background is darker than its text', () => {
  const colors = { customAccent: '#ea580c', customBackground: '#1c1917', customSurface: '#292524', customText: '#f5f5f4' }
  assert.equal(M.customPalette(colors).mode, 'dark')
  assert.equal(M.customPalette({ ...colors, customBackground: '#fafaf9', customSurface: '#ffffff', customText: '#1c1917' }).mode, 'light')
  const applied = M.computeAppearance({ ...M.DEFAULT_APPEARANCE, theme: 'custom', ...colors }, false)
  assert.equal(applied.mode, 'dark')
  assert.equal(applied.vars['--bg-color'], '#1c1917')
  assert.equal(applied.vars['--surface-color'], '#292524')
  assert.equal(applied.vars['--el-color-primary'], '#ea580c')
  // Hover shades and borders are mixed from the four colors.
  assert.equal(applied.vars['--surface-hover'], M.mix('#292524', '#f5f5f4', 0.1))
  // Element Plus's tints fade toward the background in a dark theme.
  assert.equal(applied.vars['--el-color-primary-light-9'], M.mix('#ea580c', '#1c1917', 0.9))
})

// ── Fonts and size ───────────────────────────────────────────────────────────

test('a custom font falls back to the system font, never to the browser default', () => {
  assert.equal(M.fontStack({ font: 'custom', customFont: 'Roboto Slab' }), 'Roboto Slab, system-ui, sans-serif')
  assert.equal(M.fontStack({ font: 'custom', customFont: 'x; } body {' }), M.FONT_PRESETS[0].stack)
  assert.equal(M.fontStack({ font: 'serif', customFont: '' }), M.FONT_PRESETS.find(f => f.id === 'serif').stack)
  const vars = M.computeAppearance({ ...M.DEFAULT_APPEARANCE, fontSize: 115 }, false).vars
  assert.equal(vars['--root-font-size'], '115%')
})

// ── Settings in and out ──────────────────────────────────────────────────────

test('what this build cannot paint falls back one setting at a time', () => {
  const s = M.normalizeAppearance({
    theme: 'solarized', font: 'mono', fontSize: 400, customAccent: '#ABCDEF',
    customText: 'red', thinkingProcess: 'expanded',
  })
  assert.equal(s.theme, 'light')
  assert.equal(s.font, 'mono')
  assert.equal(s.fontSize, 140)
  assert.equal(s.customAccent, '#abcdef')
  assert.equal(s.customText, M.DEFAULT_APPEARANCE.customText)
  assert.equal(s.thinkingProcess, 'collapsed')
})

test('a key the profile never set reads as the server default', () => {
  const s = M.appearanceFromConfig(
    { 'appearance.theme': 'dim', 'appearance.font_size': null },
    { 'appearance.font_size': 100, 'chat.thinking_process': 'collapsed' },
  )
  assert.equal(s.theme, 'dim')
  assert.equal(s.fontSize, 100)
  assert.deepEqual(M.appearanceToConfig({ theme: 'nord', fontSize: 110 }), {
    'appearance.theme': 'nord', 'appearance.font_size': 110,
  })
})

// ── Thinking Process ─────────────────────────────────────────────────────────

test('collapsed: the Thinking Process never opens or closes by itself', () => {
  const working = { isStreaming: true, stepCount: 3, userToggled: false }
  assert.equal(M.shouldAutoOpen('collapsed', working), false)
  assert.equal(M.shouldAutoClose('collapsed', { userToggled: false }), false)
})

test('live: open while the agent works, closed when done, until the user takes over', () => {
  assert.equal(M.shouldAutoOpen('live', { isStreaming: true, stepCount: 1, userToggled: false }), true)
  assert.equal(M.shouldAutoOpen('live', { isStreaming: false, stepCount: 4, userToggled: false }), false, 'a finished reply')
  assert.equal(M.shouldAutoOpen('live', { isStreaming: true, stepCount: 0, userToggled: false }), false)
  assert.equal(M.shouldAutoClose('live', { userToggled: false }), true)
  // Closed (or opened) by hand mid-turn: it stays the way the user left it.
  assert.equal(M.shouldAutoOpen('live', { isStreaming: true, stepCount: 5, userToggled: true }), false)
  assert.equal(M.shouldAutoClose('live', { userToggled: true }), false)
})

test('Auto-open switched on a row reaches the reply still being written, at once', () => {
  const writing = { isStreaming: true, stepCount: 2, userToggled: false }
  assert.equal(M.openAfterModeChange('live', { ...writing, open: false }), true)
  assert.equal(M.openAfterModeChange('collapsed', { ...writing, open: true }), false)
  assert.equal(M.openAfterModeChange('live', { ...writing, open: true }), null, 'already open')
  // A finished reply keeps its state; one the user opened or closed is theirs.
  assert.equal(M.openAfterModeChange('live', { ...writing, isStreaming: false, open: false }), null)
  assert.equal(M.openAfterModeChange('collapsed', { ...writing, userToggled: true, open: true }), null)
  assert.equal(M.openAfterModeChange('live', { ...writing, stepCount: 0, open: false }), null)
})

// ── The store ────────────────────────────────────────────────────────────────

const DEFAULTS = {
  'appearance.theme': 'light',
  'appearance.font': 'default',
  'appearance.font_size': 100,
  'appearance.custom_font': 'system-ui',
  'appearance.custom_accent': '#2563eb',
  'appearance.custom_background': '#f8fafc',
  'appearance.custom_surface': '#ffffff',
  'appearance.custom_text': '#334155',
  'chat.thinking_process': 'collapsed',
}

function fakeRoot() {
  const attrs = new Map()
  const props = new Map()
  return {
    getAttribute: name => attrs.get(name) ?? null,
    setAttribute: (name, value) => { attrs.set(name, String(value)) },
    style: {
      get length() { return props.size },
      item: i => [...props.keys()][i],
      setProperty: (name, value) => { props.set(name, String(value)) },
      removeProperty: (name) => { props.delete(name) },
      getPropertyValue: name => props.get(name) ?? '',
    },
  }
}

/** A signed-in tab on alice's chat. `server` holds alice's stored overrides. */
async function setup({ server = {}, storage = {}, matchMedia } = {}) {
  const env = installBrowser({ href: 'http://localhost:1515/#/alice/c/42' })
  globalThis.window.cremind = { config: { agentUrl: AGENT } }
  if (matchMedia) globalThis.window.matchMedia = matchMedia
  globalThis.document.documentElement = fakeRoot()
  for (const [key, value] of Object.entries(storage)) localStorage.setItem(key, value)
  const stored = { ...server }
  const puts = []
  const putAuth = []
  let gate = null
  env.route('/api/config/user', async (_url, init) => {
    if (init.method === 'PUT') {
      const { values } = JSON.parse(init.body)
      puts.push(values)
      putAuth.push(init.headers?.Authorization)
      Object.assign(stored, values)
      return json({ success: true, updated: Object.keys(values) })
    }
    if (gate) await gate.promise
    const values = Object.fromEntries(Object.keys(DEFAULTS).map(k => [k, stored[k] ?? null]))
    return json({ profile: 'alice', values, defaults: DEFAULTS })
  })
  const S = await load('tests/entries/appearance.ts')
  S.setActivePinia(S.createPinia())
  const settings = S.useSettingsStore()
  const store = S.useAppearanceStore()
  const root = globalThis.document.documentElement
  const signIn = () => {
    settings.authToken = 'tok-alice'
    settings.profileId = 'alice'
    store.start()
  }
  const hold = () => { gate = deferred(); return gate }
  return { env, store, settings, root, stored, puts, putAuth, signIn, hold }
}

const bg = root => root.style.getPropertyValue('--bg-color')
const cachedSettings = key => JSON.parse(localStorage.getItem(key)).settings

test('the cached look is painted before the server answers', async () => {
  const cache = JSON.stringify({ v: 1, settings: { theme: 'nord', fontSize: 120 }, applied: {} })
  const { root } = await setup({ storage: { appearance_alice: cache } })
  assert.equal(root.getAttribute('data-theme'), 'dark')
  assert.equal(root.getAttribute('data-on-primary'), 'dark')
  assert.equal(root.style.getPropertyValue('--primary-color'), '#88c0d0')
  assert.equal(root.style.getPropertyValue('--root-font-size'), '120%')
})

test('the server\'s values replace it, and are kept for this profile', async () => {
  const { store, root, signIn } = await setup({ server: { 'appearance.theme': 'paper' } })
  assert.equal(bg(root), '#f8fafc', 'nothing cached yet: Light')
  signIn()
  await until(() => bg(root) === '#f4eee2', 'the Paper theme')
  assert.equal(store.loadedProfile, 'alice')
  assert.equal(cachedSettings('appearance_alice').theme, 'paper')
  assert.equal(cachedSettings('appearance_last').theme, 'paper')
  assert.equal(localStorage.getItem('theme'), 'light', 'an older build still finds light or dark')
})

test('edits paint at once and are saved together after a pause', async () => {
  const { store, root, puts, signIn } = await setup()
  signIn()
  await until(() => store.loadedProfile === 'alice', 'the first read')
  const saved = store.update({ theme: 'dim' })
  store.update({ fontSize: 110 })
  await flush(1) // Vue's update tick, ahead of the next frame
  assert.equal(bg(root), '#22272e')
  assert.equal(root.style.getPropertyValue('--root-font-size'), '110%')
  assert.equal(puts.length, 0, 'not on every keystroke')
  await saved
  assert.deepEqual(puts, [{ 'appearance.theme': 'dim', 'appearance.font_size': 110 }])
})

test('an edit made just before a profile switch is saved for the profile it was made in', async () => {
  const { store, settings, puts, putAuth, signIn } = await setup()
  signIn()
  await until(() => store.loadedProfile === 'alice', 'the first read')
  const saved = store.update({ theme: 'rose' })
  settings.authToken = 'tok-bob'
  settings.profileId = 'bob'
  await saved
  assert.deepEqual(puts, [{ 'appearance.theme': 'rose' }])
  assert.deepEqual(putAuth, ['Bearer tok-alice'], 'never on bob')
})

test('a read that started before an edit does not undo it', async () => {
  const { store, root, signIn, hold, stored } = await setup({ server: { 'appearance.theme': 'paper' } })
  signIn()
  await until(() => bg(root) === '#f4eee2', 'the first read')
  const gate = hold()
  globalThis.__settingsSubscribers.forEach(ping => ping({ ts: Date.now() }))
  await new Promise(resolve => setTimeout(resolve, 200))
  store.update({ theme: 'mint' })
  gate.resolve()
  await flush(5)
  assert.equal(store.settings.theme, 'mint')
  assert.equal(bg(root), '#f1f6f3')
  await until(() => stored['appearance.theme'] === 'mint', 'the save')
})

test('a change made elsewhere arrives with the settings ping', async () => {
  const { root, signIn, stored } = await setup()
  signIn()
  await until(() => globalThis.__settingsSubscribers.length === 1, 'the subscription')
  stored['appearance.theme'] = 'midnight'
  globalThis.__settingsSubscribers[0]({ ts: Date.now() })
  await until(() => bg(root) === '#000000', 'the Midnight theme')
})

test('the old per-browser dark switch carries over once, to a profile that never chose', async () => {
  const { store, root, puts, signIn } = await setup({ storage: { theme: 'dark' } })
  assert.equal(root.getAttribute('data-theme'), 'dark', 'dark from the first paint')
  signIn()
  await until(() => puts.length === 1, 'the migration save')
  assert.deepEqual(puts[0], { 'appearance.theme': 'dark' })
  assert.equal(store.settings.theme, 'dark')
  assert.equal(localStorage.getItem('appearance_migrated_alice'), '1')
  assert.equal(localStorage.getItem('appearance_legacy_theme'), 'dark')
})

test('a profile that already chose a theme keeps it', async () => {
  const { store, puts, signIn } = await setup({
    storage: { theme: 'dark' }, server: { 'appearance.theme': 'mint' },
  })
  signIn()
  await until(() => store.loadedProfile === 'alice', 'the first read')
  await new Promise(resolve => setTimeout(resolve, 450))
  assert.equal(store.settings.theme, 'mint')
  assert.deepEqual(puts, [])
})

test('the first switch to Custom starts from the theme on screen', async () => {
  const { store, puts, signIn } = await setup({ server: { 'appearance.theme': 'nord' } })
  signIn()
  await until(() => store.settings.theme === 'nord', 'the first read')
  await store.chooseTheme('custom')
  assert.equal(store.settings.customAccent, '#88c0d0')
  assert.equal(store.settings.customBackground, '#2e3440')
  assert.deepEqual(puts.at(-1), {
    'appearance.theme': 'custom',
    'appearance.custom_accent': '#88c0d0',
    'appearance.custom_background': '#2e3440',
    'appearance.custom_surface': '#3b4252',
    'appearance.custom_text': '#eceff4',
  })
})

test('Reset puts the appearance back and leaves Auto-open as it is', async () => {
  const { store, stored, signIn, env } = await setup({
    server: { 'appearance.theme': 'nord', 'appearance.font_size': 120, 'chat.thinking_process': 'live' },
  })
  env.route('/api/config/user/', (url, init) => {
    const key = decodeURIComponent(url.split('/api/config/user/')[1])
    const deleted = key in stored && stored[key] !== null
    delete stored[key]
    return json({ success: init.method === 'DELETE', deleted })
  })
  signIn()
  await until(() => store.settings.theme === 'nord', 'the first read')
  await store.resetAll()
  assert.equal(store.settings.theme, 'light')
  assert.equal(store.settings.fontSize, 100)
  assert.equal(store.settings.thinkingProcess, 'live')
  assert.equal(stored['chat.thinking_process'], 'live', 'not reset on the server either')
  assert.equal(stored['appearance.theme'], undefined)
})

test('Match system follows the device as it changes', async () => {
  const listeners = []
  let dark = true
  const matchMedia = () => ({ get matches() { return dark }, addEventListener: (_type, fn) => listeners.push(fn) })
  const cache = JSON.stringify({ v: 1, settings: { theme: 'system' }, applied: {} })
  const { store, root } = await setup({ storage: { appearance_alice: cache }, matchMedia })
  store.start()
  assert.equal(root.getAttribute('data-theme'), 'dark')
  dark = false
  listeners.forEach(fn => fn({ matches: false }))
  await flush()
  assert.equal(root.getAttribute('data-theme'), 'light')
  const kept = JSON.parse(localStorage.getItem('appearance_last'))
  assert.equal(kept.system.dark.mode, 'dark', 'the first paint can pick either')
  assert.equal(kept.system.light.mode, 'light')
})
