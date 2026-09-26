// Creating a backup: the profiles' working directories are included unless
// the user unticks them. Only the opt-out travels (`include_workspaces:
// false`), so an older server — which knows nothing of the flag — still gets
// the body it expects for the default.
import assert from 'node:assert/strict'
import test from 'node:test'

import { installBrowser, json, load } from './harness.mjs'

let env = installBrowser()
const api = await load('src/services/backupApi.ts')

const URL_ = 'http://localhost:1515'
const TOKEN = 'jwt-admin'

function bodyOf(call) {
  return JSON.parse(call.init.body)
}

test('the default sends no workspaces flag at all', async () => {
  env = installBrowser()
  env.route('/api/backup/create', () => json({ ok: true }, 202))
  await api.createBackup(URL_, TOKEN)
  const [call] = env.callsTo('/api/backup/create')
  assert.equal(call.init.method, 'POST')
  assert.deepEqual(bodyOf(call), {})
})

test('unticking the working directories sends the opt-out, beside a passphrase', async () => {
  env = installBrowser()
  env.route('/api/backup/create', () => json({ ok: true }, 202))
  await api.createBackup(URL_, TOKEN, 's3cret', false)
  const [call] = env.callsTo('/api/backup/create')
  assert.deepEqual(bodyOf(call), { passphrase: 's3cret', include_workspaces: false })
})

test('ticked with a passphrase sends only the passphrase', async () => {
  env = installBrowser()
  env.route('/api/backup/create', () => json({ ok: true }, 202))
  await api.createBackup(URL_, TOKEN, 's3cret', true)
  const [call] = env.callsTo('/api/backup/create')
  assert.deepEqual(bodyOf(call), { passphrase: 's3cret' })
})
