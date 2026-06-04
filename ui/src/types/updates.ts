/**
 * Shared types for the unified update surface (UpdateBanner +
 * UpdatesSettings) driven by the ``useUpdate`` composable.
 *
 * ``BackendStatus`` is the response shape returned by
 * ``GET /api/upgrade/check`` — see ``app/api/upgrade.py``. The Python
 * endpoint emits one of a small set of ``status`` values; this discriminated
 * union captures the payload fields that come with each.
 *
 * ``UpdaterStatus`` is the desktop-app side, sourced from electron-updater
 * via the ``window.cremind.updater.onStatus`` bridge.
 *
 * ``UpgradeStatusFile`` mirrors ``GET /api/upgrade/status`` — the shape
 * the detached runner writes to ``~/.cremind/.upgrade.status.json``.
 *
 * ``UnifiedUpdateState`` is what the composable exposes to consumers.
 * It hides which component (shell/backend) has the change so the UI
 * can render one card instead of two.
 */

export type BackendStatus =
  | { status: 'unknown' }
  | { status: 'up_to_date'; current: string; latest?: string; channel?: string; release_url?: string }
  | {
      status: 'available'
      current: string
      latest: string
      channel?: string
      release_url: string
      release_notes: string
    }
  | {
      status: 'too_old'
      current: string
      latest: string
      channel?: string
      release_url: string
      min_supported_upgrade_from: string
    }
  | { status: 'unreachable'; current?: string; channel?: string; reason: string }
  | { status: 'unavailable'; current?: string; reason: string }
  | { status: 'error'; reason: string }

export type UpdaterStatus = CremindUpdaterStatus

// Re-exports for the in-app Electron backend-upgrade flow.
export type BackendUpgradePhase = CremindBackendUpgradePhase
export type BackendUpgradeStatus = CremindBackendUpgradeStatus
export type BackendUpgradeLog = CremindBackendUpgradeLog
export type BackendUpgradeDone = CremindBackendUpgradeDone

// ── Web-UI POST /api/upgrade/apply contract ───────────────────────────────

export type UpgradeStatusPhase =
  | 'idle'
  | 'queued'
  | 'check'
  | 'backup'
  | 'install'
  | 'migrate'
  | 'health'
  | 'restart'
  | 'rollback'
  | 'done'
  | 'failed'

export type UpgradeStatusFile = {
  upgrade_id: string | null
  phase: UpgradeStatusPhase
  ok: boolean
  current_version: string | null
  target_version: string | null
  started_at: number | null
  finished_at: number | null
  exit_code: number | null
  error: string | null
  log_tail: string[]
}

export type UpgradeApplyResponse = {
  ok: true
  pid: number
  status_url: string
  stream_url: string
}

// ── Test-channel version picker (GET /api/upgrade/versions) ───────────────

/** One selectable release in the test-channel version picker. */
export type ReleaseChoice = {
  version: string
  tag_name: string
  name: string
  published_at: string
  html_url: string
}

export type VersionsResponse = {
  channel: string
  current: string
  versions: ReleaseChoice[]
}

// ── Unified state exposed by useUpdate() ──────────────────────────────────

/** Top-level state the renderer reacts to. */
export type UnifiedUpdatePhase =
  | 'idle'              // up to date or never checked — card hidden
  | 'available'         // at least one component has a newer version
  | 'applying'          // an upgrade is in flight; modal open
  | 'restart_required'  // shell update downloaded; needs `Restart now`
  | 'done'              // upgrade complete; modal can close
  | 'failed'            // upgrade rolled back
  | 'blocked'           // backend too old to upgrade in place

export type UnifiedUpdateState = {
  phase: UnifiedUpdatePhase
  /** Version string to show in headers, e.g. "0.1.10". */
  latestVersion: string | null
  currentVersion: string | null
  /** Truthy when the user can press the Update Now button. */
  canApply: boolean
  /** Live log lines from the in-flight upgrade, capped at LOG_TAIL_MAX. */
  log: string[]
  /** Human-readable phase label, e.g. "Installing wheel..." */
  phaseLabel: string
  /** Error string when phase === 'failed' or blocked. */
  error: string | null
  /** Release-notes URL (when available). */
  releaseUrl: string | null
  /** Active release channel from the backend ('test' | 'production' | 'dev' | …). */
  channel: string | null
  /** Test-channel only: installable releases for the version picker. */
  availableVersions: ReleaseChoice[]
}
