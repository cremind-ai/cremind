/**
 * Blueprint import wizard composable.
 *
 * Module-level singleton refs so the wizard state survives route remounts and a
 * browser refresh (rehydrate() reloads the server-side session). The import is
 * user-paced: each step is a synchronous POST, so there is no polling — the
 * server session IS the source of truth, and every response returns the fresh
 * session which we store here.
 */

import { computed, ref } from 'vue'

import { useSettingsStore } from '../stores/settings'
import {
  abortImport,
  applyStep,
  finalizeImport,
  getImportSession,
  skipStep,
  uploadBlueprint,
  type ImportReport,
  type ImportSession,
} from '../services/blueprintApi'

const session = ref<ImportSession | null>(null)
const report = ref<ImportReport | null>(null)
const busy = ref(false)
const error = ref<string | null>(null)

function creds(): { url: string; token: string } | null {
  const s = useSettingsStore()
  if (!s.agentUrl) return null
  return { url: s.agentUrl, token: s.authToken }
}

const plan = computed(() => session.value?.plan ?? [])
const steps = computed(() => session.value?.steps ?? [])

function statusOf(key: string): string {
  return steps.value.find(s => s.key === key)?.status ?? 'pending'
}

/** Index of the first step not yet applied/skipped (the "current" step). */
const currentStepIndex = computed(() => {
  const p = plan.value
  for (let i = 0; i < p.length; i++) {
    const st = statusOf(p[i].key)
    if (st !== 'applied' && st !== 'skipped') return i
  }
  return p.length
})

async function rehydrate(): Promise<void> {
  const c = creds()
  if (!c) return
  try {
    session.value = await getImportSession(c.url, c.token)
    report.value = session.value?.report ?? null
  } catch {
    session.value = null
  }
}

async function upload(file: File, replace = false): Promise<void> {
  const c = creds()
  if (!c) throw new Error('Not connected')
  busy.value = true
  error.value = null
  try {
    session.value = await uploadBlueprint(c.url, c.token, file, replace)
    report.value = null
  } finally {
    busy.value = false
  }
}

async function step(key: string, inputs: Record<string, any>): Promise<void> {
  const c = creds()
  if (!c) throw new Error('Not connected')
  busy.value = true
  error.value = null
  try {
    const res = await applyStep(c.url, c.token, key, inputs)
    session.value = res.session
  } catch (e: any) {
    error.value = e?.message ?? String(e)
    throw e
  } finally {
    busy.value = false
  }
}

async function skip(key: string): Promise<void> {
  const c = creds()
  if (!c) throw new Error('Not connected')
  busy.value = true
  error.value = null
  try {
    const res = await skipStep(c.url, c.token, key)
    session.value = res.session
  } catch (e: any) {
    error.value = e?.message ?? String(e)
    throw e
  } finally {
    busy.value = false
  }
}

async function finalize(): Promise<void> {
  const c = creds()
  if (!c) throw new Error('Not connected')
  busy.value = true
  error.value = null
  try {
    const res = await finalizeImport(c.url, c.token)
    report.value = res.report
    await rehydrate()
  } finally {
    busy.value = false
  }
}

async function abort(deleteProfile = true): Promise<void> {
  const c = creds()
  if (!c) return
  busy.value = true
  try {
    await abortImport(c.url, c.token, deleteProfile)
    session.value = null
    report.value = null
  } finally {
    busy.value = false
  }
}

function reset(): void {
  session.value = null
  report.value = null
  error.value = null
}

export function useBlueprintImport() {
  return {
    session,
    report,
    busy,
    error,
    plan,
    steps,
    currentStepIndex,
    statusOf,
    rehydrate,
    upload,
    step,
    skip,
    finalize,
    abort,
    reset,
  }
}
