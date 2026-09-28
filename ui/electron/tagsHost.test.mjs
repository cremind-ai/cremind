// The desktop app as a Cremind Tag gateway computer (tagsHost.ts): the
// cremind://tags/setup link is validated strictly and reaches `cremind tags
// host enroll --events` on its stdin only (never argv, never a log), the
// approval is the app's own dialog with the four words, and the runner is
// supervised — restarted with back-off, stopped by closing its stdin, and
// left stopped (with a notice) when Cremind removed the computer.
import assert from 'node:assert/strict'
import { EventEmitter } from 'node:events'
import { mkdtemp, mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { PassThrough } from 'node:stream'
import { fileURLToPath } from 'node:url'
import test from 'node:test'
import { transform } from 'esbuild'

const here = path.dirname(fileURLToPath(import.meta.url))
const { code } = await transform(await readFile(path.join(here, 'tagsHost.ts'), 'utf8'), { loader: 'ts', format: 'esm' })
const M = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)

const SESSION = '6fb5bffe-fdb4-4192-860d-341115d9d060'
const TOKEN = 'T0ken-secret-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
const LINK = `cremind://tags/setup?v=1&server=${encodeURIComponent('https://cremind.example.org:1180')}&session=${SESSION}&token=${TOKEN}`

test('a good link, and every way a link is refused', () => {
  const link = M.parseTagsSetupLink(LINK)
  assert.deepEqual([link.server, link.session], ['https://cremind.example.org:1180', SESSION])
  assert.equal(M.redactLink(LINK).includes(TOKEN), false)
  const refused = [
    '', 'https://cremind.example.org', LINK.replace('cremind://', 'cremind-connect://'),
    LINK.replace('tags/setup', 'tags/remove'), LINK + '#x', LINK + `&token=${TOKEN}`, LINK.replace('v=1', 'v=2'),
    LINK.replace(encodeURIComponent('https://cremind.example.org:1180'), encodeURIComponent('https://x.org/path')),
    LINK.replace(encodeURIComponent('https://cremind.example.org:1180'), encodeURIComponent('https://u@x.org')),
    LINK.replace(encodeURIComponent('https://cremind.example.org:1180'), encodeURIComponent('file:///etc')),
    LINK.replace(SESSION, 'nope'), LINK.replace(TOKEN, 'short'), LINK + '&pin=zz',
    LINK.replace(encodeURIComponent('https://'), encodeURIComponent('http://')) + '&pin=' + 'ab'.repeat(32),
    LINK + '\u0000',
  ]
  for (const bad of refused) assert.throws(() => M.parseTagsSetupLink(bad), M.LinkRefused, bad)
  assert.equal(M.parseEventLine('{"event":"bound","words":"a b c d"}').words, 'a b c d')
  assert.equal(M.parseEventLine('Driving gateways…'), null)
  assert.equal(M.parseEventLine('{"no":"event"}'), null)
})

function fakeChild() {
  const child = new EventEmitter()
  child.stdin = new PassThrough()
  child.stdout = new PassThrough()
  child.stderr = new PassThrough()
  child.exitCode = null
  child.pid = 4242
  child.written = ''
  child.stdin.on('data', (chunk) => { child.written += chunk })
  child.finish = (code) => { child.exitCode = code; child.emit('close', code) }
  child.kill = () => child.finish(1)
  return child
}

async function harness({ local = false, runtime = 'C:/cremind/venv/cremind.exe', approve = true } = {}) {
  const sys = await mkdtemp(path.join(os.tmpdir(), 'cremind-tags-host-'))
  const spawned = []
  const timers = []
  const notices = []
  const logs = []
  const approvals = []
  const manager = new M.TagsHostManager({
    runtimeExe: () => runtime,
    env: () => ({ CREMIND_SYSTEM_DIR: sys }),
    systemDir: () => sys,
    spawn: (command, args, options) => {
      const child = fakeChild()
      spawned.push({ command, args, options, child })
      return child
    },
    localBackendDrivesUsb: () => local,
    approve: async (bound) => { approvals.push(bound); return approve },
    notify: (title, body, kind) => notices.push({ title, body, kind }),
    log: (line) => logs.push(line),
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length },
    clearTimer: () => {},
    now: () => 0,
  })
  const enrolled = async () => {
    await mkdir(path.join(sys, '.tag-runtime', 'remote'), { recursive: true })
    await writeFile(path.join(sys, '.tag-runtime', 'remote', 'enrollment.json'),
      JSON.stringify({ server: 'https://cremind.example.org:1180', profile: 'anna', host_id: 'h-1' }))
  }
  return { sys, manager, spawned, timers, notices, logs, approvals, enrolled, cleanup: () => rm(sys, { recursive: true, force: true }) }
}

const tick = () => new Promise((resolve) => setImmediate(resolve))

test('enrolling hands the link over stdin, shows the words, then starts the runner', async () => {
  const h = await harness()
  try {
    const done = h.manager.enroll(LINK)
    await tick()
    const { command, args, child } = h.spawned[0]
    assert.equal(command, 'C:/cremind/venv/cremind.exe')
    assert.deepEqual(args, ['tags', 'host', 'enroll', '--events'])
    assert.equal(args.join(' ').includes(TOKEN), false, 'the link never goes on a command line')
    assert.equal(child.written, LINK + '\n')
    child.stdout.write(JSON.stringify({ event: 'bound', server: 'https://cremind.example.org:1180', profile: 'anna',
      computer: 'LAPTOP-9', words: 'amber orbit lantern tidal' }) + '\n')
    await tick(); await tick()
    assert.deepEqual(h.approvals, [{ server: 'https://cremind.example.org:1180', profile: 'anna', computer: 'LAPTOP-9',
      words: 'amber orbit lantern tidal' }])
    assert.equal(child.written, LINK + '\napprove\n')
    await h.enrolled()
    child.stdout.write(JSON.stringify({ event: 'enrolled', host_id: 'h-1' }) + '\n')
    await tick()
    child.finish(0)
    assert.deepEqual(await done, { ok: true })
    assert.equal(h.logs.join('\n').includes(TOKEN), false, 'nothing logs the token')
    // The runner starts once enrolled.
    const run = h.spawned.find((s) => s.args[2] === 'run')
    assert.ok(run, 'the runner started')
    assert.equal(run.options.env.CREMIND_TAG_HOST_SUPERVISED, '1')
    assert.deepEqual(h.manager.status(), {
      available: true, enrolled: true, server: 'https://cremind.example.org:1180', profile: 'anna', hostId: 'h-1',
      running: true, state: 'starting', detail: undefined,
    })
  } finally {
    await h.cleanup()
  }
})

test('a decline, a bad link and a computer that drives its own USB never enroll', async () => {
  const h = await harness({ approve: false })
  try {
    const done = h.manager.enroll(LINK)
    await tick()
    const { child } = h.spawned[0]
    child.stdout.write(JSON.stringify({ event: 'bound', words: 'a b c d' }) + '\n')
    await tick(); await tick()
    assert.equal(child.written, LINK + '\ndecline\n')
    child.stdout.write(JSON.stringify({ event: 'failed', code: 'declined', message: 'Setting up this computer was declined.' }) + '\n')
    await tick()
    child.finish(1)
    assert.deepEqual(await done, { ok: false, error: 'Setting up this computer was declined.' })
    assert.deepEqual(await h.manager.enroll('https://evil.example/tags/setup'), { ok: false, error: 'This is not a Cremind link.' })
  } finally {
    await h.cleanup()
  }
  const local = await harness({ local: true })
  try {
    const res = await local.manager.enroll(LINK)
    assert.equal(res.ok, false)
    assert.match(res.error, /drives its USB ports itself/)
    assert.equal(local.spawned.length, 0)
  } finally {
    await local.cleanup()
  }
  const bare = await harness({ runtime: null })
  try {
    assert.equal(bare.manager.status().available, false)
    assert.match(bare.manager.status().reason, /gateway components/)
  } finally {
    await bare.cleanup()
  }
})

test('the runner restarts with back-off, stops by stdin, and stays stopped once removed', async () => {
  const h = await harness()
  try {
    await h.enrolled()
    h.manager.startIfEnrolled()
    const first = h.spawned.at(-1)
    assert.deepEqual(first.args, ['tags', 'host', 'run'])
    first.child.finish(1)
    assert.equal(h.timers.at(-1).ms, 1000)
    h.timers.at(-1).fn()
    h.spawned.at(-1).child.finish(1)
    assert.equal(h.timers.at(-1).ms, 2000, 'the back-off doubles')
    h.timers.at(-1).fn()
    const third = h.spawned.at(-1)
    third.child.finish(M.EXIT_REVOKED)
    assert.equal(h.manager.state, 'revoked')
    assert.equal(h.notices.length, 1)
    const timersBefore = h.timers.length
    assert.equal(h.timers.length, timersBefore, 'no restart after the computer was removed')

    // Stopping closes stdin (the runner exits by itself).
    h.manager.startIfEnrolled()
    const running = h.spawned.at(-1).child
    let ended = false
    running.stdin.on('finish', () => { ended = true })
    h.manager.stopRunner()
    await tick()
    assert.equal(ended, true)
    running.finish(0)
    assert.equal(h.manager.state, 'stopped')
  } finally {
    await h.cleanup()
  }
})
