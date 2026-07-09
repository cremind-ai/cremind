/**
 * Backup & Restore composable.
 *
 * Module-level singleton refs so state survives route remounts — a restore
 * outlives the settings page (it restarts the server), so the progress modal
 * must not reset when the component unmounts.
 *
 * Restore polling tolerates connection errors while the backend restarts
 * (the restore restarts the server to apply; the poll keeps retrying until
 * the new backend answers), mirroring useUpdate's startStatusPolling.
 */

import { computed, ref } from 'vue'

import { useSettingsStore } from '../stores/settings'
import {
  ackRestoreReport,
  createBackup,
  deleteBackup,
  downloadBackup,
  fetchBackupStatus,
  fetchRestoreReport,
  fetchRestoreStatus,
  listBackups,
  startRestore,
  uploadBackup,
  type BackupEntry,
  type RestoreReportResponse,
  type StatusFile,
} from '../services/backupApi'

const POLL_MS = 1500
const TERMINAL = new Set(['done', 'failed', 'idle'])

const backups = ref<BackupEntry[]>([])
const loading = ref(false)
const createPhase = ref<string>('idle')
const createError = ref<string | null>(null)
const restorePhase = ref<string>('idle')
const restoreError = ref<string | null>(null)
const restoreActive = ref(false)
const report = ref<RestoreReportResponse | null>(null)

let createTimer: ReturnType<typeof setInterval> | null = null
let restoreTimer: ReturnType<typeof setInterval> | null = null

function creds(): { url: string; token: string } | null {
  const s = useSettingsStore()
  if (!s.agentUrl) return null
  return { url: s.agentUrl, token: s.authToken }
}

async function refresh(): Promise<void> {
  const c = creds()
  if (!c) return
  loading.value = true
  try {
    backups.value = await listBackups(c.url, c.token)
  } finally {
    loading.value = false
  }
  await loadReport()
}

async function loadReport(): Promise<void> {
  const c = creds()
  if (!c) return
  try {
    report.value = await fetchRestoreReport(c.url, c.token)
  } catch {
    report.value = null
  }
}

function stopCreatePoll(): void {
  if (createTimer) { clearInterval(createTimer); createTimer = null }
}

function startCreatePoll(): void {
  if (createTimer) return
  const tick = async () => {
    const c = creds()
    if (!c) return
    let st: StatusFile
    try {
      st = await fetchBackupStatus(c.url, c.token)
    } catch {
      return
    }
    createPhase.value = st.phase
    if (TERMINAL.has(st.phase)) {
      stopCreatePoll()
      if (st.phase === 'failed') createError.value = st.error ?? 'Backup failed.'
      void refresh()
    }
  }
  createTimer = setInterval(() => { void tick() }, POLL_MS)
  void tick()
}

async function create(passphrase?: string): Promise<void> {
  const c = creds()
  if (!c) return
  createError.value = null
  createPhase.value = 'queued'
  try {
    await createBackup(c.url, c.token, passphrase)
  } catch (e) {
    createPhase.value = 'failed'
    createError.value = e instanceof Error ? e.message : String(e)
    return
  }
  startCreatePoll()
}

function stopRestorePoll(): void {
  if (restoreTimer) { clearInterval(restoreTimer); restoreTimer = null }
}

function startRestorePoll(): void {
  if (restoreTimer) return
  const tick = async () => {
    const c = creds()
    if (!c) return
    let st: StatusFile
    try {
      st = await fetchRestoreStatus(c.url, c.token)
    } catch {
      // Backend likely restarting mid-restore — keep polling.
      return
    }
    restorePhase.value = st.phase
    if (TERMINAL.has(st.phase) && st.phase !== 'idle') {
      stopRestorePoll()
      restoreActive.value = false
      if (st.phase === 'failed') {
        restoreError.value = st.error ?? 'Restore failed.'
      } else {
        void onRestoreDone()
      }
    }
  }
  restoreTimer = setInterval(() => { void tick() }, POLL_MS)
  void tick()
}

async function onRestoreDone(): Promise<void> {
  // The restored JWT secret invalidates the current session token; send the
  // user to sign in again. Load the report first so it survives the reload.
  await loadReport()
  const s = useSettingsStore()
  try {
    // Best-effort: clear the active profile token so the guard routes to login.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    ;(s as any).authToken = ''
  } catch { /* ignore */ }
  // Give the user a moment to see the success state, then reload.
  setTimeout(() => { window.location.reload() }, 1500)
}

async function restore(name: string, passphrase?: string): Promise<void> {
  const c = creds()
  if (!c) return
  restoreError.value = null
  restorePhase.value = 'queued'
  restoreActive.value = true
  try {
    await startRestore(c.url, c.token, name, passphrase)
  } catch (e) {
    restorePhase.value = 'failed'
    restoreActive.value = false
    restoreError.value = e instanceof Error ? e.message : String(e)
    return
  }
  startRestorePoll()
}

async function upload(file: File): Promise<void> {
  const c = creds()
  if (!c) return
  await uploadBackup(c.url, c.token, file)
  await refresh()
}

async function download(name: string): Promise<void> {
  const c = creds()
  if (!c) return
  await downloadBackup(c.url, c.token, name)
}

async function remove(name: string): Promise<void> {
  const c = creds()
  if (!c) return
  await deleteBackup(c.url, c.token, name)
  await refresh()
}

async function dismissReport(): Promise<void> {
  const c = creds()
  if (!c) return
  await ackRestoreReport(c.url, c.token)
  await loadReport()
}

export function useBackup() {
  const hasUnackedReport = computed(() => {
    const r = report.value?.report
    return !!r && !r.acknowledged
  })

  return {
    backups,
    loading,
    createPhase,
    createError,
    restorePhase,
    restoreError,
    restoreActive,
    report,
    hasUnackedReport,
    refresh,
    loadReport,
    create,
    restore,
    upload,
    download,
    remove,
    dismissReport,
  }
}
