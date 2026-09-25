/**
 * What User Document Search says to the user — every banner, label and number
 * format of Settings → My Documents and the NavRail chip, in one pure module.
 *
 * The banner copy follows the design's unified state table (§2.8): one
 * `state(reason)` pair from the snapshot decides the banner, the tone and the
 * actions offered, so the page, the chip and (through the same table on the
 * server) the CLI and the agent tool never disagree about what is going on.
 * Pure so node:test covers every row (tests/userdocs-view.test.mjs).
 */

import type {
  PlanEffect,
  UserDocsSettings,
  UserDocsSnapshot,
} from '../services/userdocsApi';
import { failedCount, isActive, syncProgress } from '../stores/userDocsReducer';

// ── numbers ────────────────────────────────────────────────────────────────

const COUNT = new Intl.NumberFormat('en-US');

/** 12840 → "12,840". */
export function formatCount(n: number | null | undefined): string {
  return COUNT.format(Math.max(0, Math.round(Number(n) || 0)));
}

/** Binary units, one decimal from KB up: 1288490188 → "1.2 GB". */
export function formatBytes(n: number | null | undefined): string {
  const v = Number(n);
  if (!Number.isFinite(v) || v <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  if (i === 0) return `${Math.round(x)} B`;
  // Drop the decimal once it stops carrying information ("512 MB", not "512.0 MB").
  return `${x >= 100 ? Math.round(x) : x.toFixed(1).replace(/\.0$/, '')} ${units[i]}`;
}

/** Remaining time for a progress line: "less than a minute left",
 *  "~12 min left", "~2 h 5 min left", "~3 days left". Empty when unknown. */
export function formatEta(seconds: number | null | undefined): string {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return '';
  if (s < 60) return 'less than a minute left';
  const min = Math.round(s / 60);
  if (min < 60) return `~${min} min left`;
  const hours = Math.floor(min / 60);
  if (hours < 48) {
    const rest = min % 60;
    return rest ? `~${hours} h ${rest} min left` : `~${hours} h left`;
  }
  return `~${Math.round(hours / 24)} days left`;
}

/** A rough duration without the "left": "about 40 minutes", "about 6 hours". */
export function formatDuration(seconds: number | null | undefined): string {
  const s = Number(seconds);
  if (!Number.isFinite(s) || s <= 0) return 'under a minute';
  if (s < 90) return 'about a minute';
  const min = Math.round(s / 60);
  if (min < 90) return `about ${min} minutes`;
  const hours = Math.round(min / 60);
  if (hours < 48) return `about ${hours} hours`;
  return `about ${Math.round(hours / 24)} days`;
}

function formatWhen(ts: number | null | undefined): string {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return '';
  }
}

function plural(n: number, one: string, many = `${one}s`): string {
  return `${formatCount(n)} ${n === 1 ? one : many}`;
}

// ── labels ─────────────────────────────────────────────────────────────────

const REASON_LABELS: Record<string, string> = {
  // Why a file failed, or was indexed by name and details only.
  timeout: 'Took too long to read',
  oom: 'Ran out of memory while reading it',
  extractor_crash: 'The document reader crashed on it',
  extractor_protocol: 'The document reader returned something unexpected',
  extractor_unavailable: 'The document reader is not available right now',
  pool_closed: 'Reading was interrupted by a restart',
  encrypted: 'Password-protected',
  corrupt: 'The file looks damaged',
  legacy_format: 'An old format that cannot be read — save it in a newer format',
  too_large: 'Larger than the size limit',
  permission_denied: 'Cremind is not allowed to read it',
  awaiting_extractor: 'Waiting for a document reader to be installed',
  placeholder: 'Cloud-only file — not downloaded to this computer',
  secret: 'Looks like it holds secrets (keys, passwords) — content left out on purpose',
  heic_unsupported: 'HEIC photos are not supported on this server yet',
  internal_error: 'Unexpected error while indexing',
  awaiting_vision: 'Waiting for a vision model to describe it',
  // A deferred file waits for the storage level it was deferred at to clear.
  budget: 'Waiting — the storage budget is full',
  disk_low: 'Waiting — disk space is low',
  disk_critical: 'Waiting — disk space is critically low',
  // Kinds indexed by name, date and folder only.
  audio: 'Audio — indexed by name and details only',
  video: 'Video — indexed by name and details only',
  archive: 'Archive — indexed by name and details only',
  executable: 'Program — indexed by name and details only',
  database: 'Database file — indexed by name and details only',
  font: 'Font — indexed by name and details only',
  bundle: 'App bundle — indexed by name and details only',
};

function humanize(code: string): string {
  const text = code.replace(/[_-]+/g, ' ').trim();
  return text ? text[0].toUpperCase() + text.slice(1) : '';
}

/** Human text for a file's `status_reason` / a failure `reason`. A
 *  `partial:<reason>` means the file was indexed, but not all of it. */
export function reasonLabel(code: string | null | undefined): string {
  if (!code) return '';
  if (code.startsWith('partial:')) {
    const inner = code.slice('partial:'.length);
    return inner ? `Partly read — ${reasonLabel(inner).toLowerCase()}` : 'Partly read';
  }
  return REASON_LABELS[code] ?? humanize(code);
}

const STATUS_LABELS: Record<string, string> = {
  dirty: 'Waiting',
  indexed: 'Indexed',
  metadata_only: 'Name & details only',
  awaiting_extractor: 'Needs a reader',
  deferred: 'Deferred',
  error: 'Failed',
  missing: 'Missing',
  tombstone: 'Deleted',
};

/** Label for a file status (a stage pill, a status tab). */
export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? humanize(status);
}

/** Display order of the per-status pills and tabs. */
export const STATUS_ORDER = [
  'indexed', 'dirty', 'metadata_only', 'awaiting_extractor', 'deferred', 'error', 'missing',
];

/** What a file currently being processed is doing. */
export function stageLabel(stage: string, progress?: { done: number; total: number } | null): string {
  switch (stage) {
    case 'read': return 'Opening';
    case 'hash': return 'Checking for changes';
    case 'extract': return 'Reading text';
    case 'chunk': return 'Splitting into passages';
    case 'index':
      return progress && progress.total > 0
        ? `Indexing ${formatCount(progress.done)}/${formatCount(progress.total)} passages`
        : 'Indexing';
    case 'caption': return 'Describing the image';
    case 'embed': return 'Embedding';
    default: return humanize(stage);
  }
}

/** One line per effect of a change plan (the confirm dialog). */
export function effectLabel(effect: PlanEffect): string {
  const files = plural(effect.files, 'file');
  const size = effect.bytes > 0 ? ` (${formatBytes(effect.bytes)})` : '';
  switch (effect.kind) {
    case 'purge_all':
      return `Delete this profile's whole document index — ${files}${size}.`;
    case 'purge_drive':
      return `Remove ${files} from Google Drive from the index${size}.`;
    case 'purge_out_of_scope':
      return effect.detail?.to
        ? `Remove ${files} that are not inside ${effect.detail.to} from the index.`
        : `Remove ${files} that the new rules leave out from the index.`;
    case 'reembed_all': {
      const chunks = Number(effect.detail?.chunks) || 0;
      return `Re-embed every indexed passage${chunks ? ` (${formatCount(chunks)})` : ''} across ${files}. `
        + 'Search keeps working while it runs.';
    }
    case 'reextract_all':
      return `Read all ${files} again and re-embed them. Search keeps working while it runs.`;
    default:
      return `${humanize(effect.kind)} — ${files}${size}.`;
  }
}

// ── the state banner ───────────────────────────────────────────────────────

export type BannerTone = 'info' | 'warning' | 'error' | 'success';

export type BannerActionId =
  | 'enable'
  | 'open_admin'
  | 'choose_folder'
  | 'rescan'
  | 'confirm_root_change'
  | 'review_first_sync'
  | 'review_deletions'
  | 'resume'
  | 'pause'
  | 'adjust_excludes'
  | 'retry_failed'
  | 'relink_google';

export interface BannerAction {
  id: BannerActionId;
  label: string;
  primary?: boolean;
}

export interface StateBanner {
  tone: BannerTone;
  title: string;
  body: string;
  actions: BannerAction[];
  /** A snippet to copy verbatim (the Docker `volumes:` line). */
  snippet: string | null;
  /** Work is under way — the panel shows a spinner rather than an icon. */
  busy: boolean;
}

export interface BannerContext {
  /** When the kept index was last brought up to date (epoch ms), for the
   *  "keyword search over the index as of <time>" line. */
  asOf?: number | null;
  /** Size of an index kept while the feature is off. */
  keptIndexBytes?: number | null;
}

function banner(
  tone: BannerTone,
  title: string,
  body: string,
  actions: BannerAction[] = [],
  extra: Partial<Pick<StateBanner, 'snippet' | 'busy'>> = {},
): StateBanner {
  return { tone, title, body, actions, snippet: extra.snippet ?? null, busy: extra.busy ?? false };
}

function rootOf(snap: UserDocsSnapshot, settings: UserDocsSettings | null | undefined): string {
  return snap.sources?.local?.root
    || settings?.local.root_path
    || settings?.policy_view.working_dir
    || 'your documents folder';
}

function holdBanner(
  snap: UserDocsSnapshot,
  settings: UserDocsSettings | null | undefined,
): StateBanner {
  const detail = snap.detail ?? {};
  const root = detail.root || rootOf(snap, settings);
  switch (snap.reason) {
    case 'root_invalid':
      return banner(
        'error',
        "This folder can't be indexed",
        `${detail.message || `${root} is not a folder Cremind can index.`} Nothing has been deleted — `
          + 'choose another folder to carry on.',
        [{ id: 'choose_folder', label: 'Choose a folder', primary: true }],
      );

    case 'pending_root_change':
    case 'root_change': {
      const from = detail.from ?? (snap.confirmation?.kind === 'root_change' ? snap.confirmation.from : null);
      const to = detail.to ?? (snap.confirmation?.kind === 'root_change' ? snap.confirmation.to : null);
      return banner(
        'warning',
        'Your documents folder moved',
        `The working directory changed${from ? ` from ${from}` : ''}${to ? ` to ${to}` : ''}. `
          + 'Syncing is on hold until you confirm: files that are still inside the new folder keep '
          + 'their index, and the rest are removed from it. Your files themselves are never touched.',
        [
          { id: 'confirm_root_change', label: 'Use the new folder', primary: true },
          { id: 'choose_folder', label: 'Choose a different folder' },
        ],
      );
    }

    case 'root_unavailable': {
      const why = detail.why as string | undefined;
      // The Docker snippet only helps inside a container, where the missing
      // bind mount is the usual cause; elsewhere it would be noise.
      const snippet = detail.in_container && typeof detail.snippet === 'string' ? detail.snippet : null;
      const nothingLost = 'Nothing has been deleted, and syncing resumes on its own when it is back.';
      let body: string;
      if (why === 'bind_missing') {
        body = `Cremind runs in Docker and no folder from your computer is mounted at ${root}. `
          + "Add this line under the cremind service's volumes: in docker-compose.yml, then run "
          + `docker compose up -d. ${nothingLost}`;
      } else if (why === 'root_empty') {
        const n = Number(detail.manifest_count) || 0;
        body = `${root} is empty, but the index holds ${plural(n, 'file')} from it — a disk, share or `
          + `mount that is not connected looks exactly like this. ${nothingLost}`;
      } else if (why === 'device_changed') {
        body = `${root} now sits on a different disk, and most of its files are missing. `
          + `If a drive was swapped or remounted, reconnect the right one. ${nothingLost}`;
      } else {
        body = `${root} is missing or can't be read — an unplugged drive, a share that did not mount, `
          + `or a renamed folder. ${nothingLost}`;
      }
      if (snippet && why !== 'bind_missing') {
        body += ' If Cremind runs in Docker, check that the folder is mounted — for example with this '
          + "line under the cremind service's volumes: in docker-compose.yml.";
      }
      return banner(
        'warning',
        "Your documents folder isn't available right now",
        body,
        [
          { id: 'rescan', label: 'Check again', primary: true },
          { id: 'choose_folder', label: 'Choose a different folder' },
        ],
        { snippet },
      );
    }

    // Google Drive holds (the Drive source reports these for itself).
    case 'auth_revoked': {
      const when = formatWhen(Number(detail.purge_at) || null);
      return banner(
        'warning',
        `Re-link Google — Drive index removed${when ? ` on ${when}` : ' soon'}`,
        'Google no longer accepts Cremind\'s access to your Drive, so Drive files are not synced. '
          + 'Link the account again to keep their index.',
        [{ id: 'relink_google', label: 'Re-link Google', primary: true }],
      );
    }
    case 'drive_unreachable':
      return banner(
        'warning',
        'Google Drive is unreachable — results may be outdated',
        'Drive files stay searchable as they were last synced; syncing resumes when Drive answers again.',
      );

    default:
      return banner(
        'warning',
        'Syncing is on hold',
        `${detail.message || 'Something needs your attention before syncing can continue.'} `
          + 'Nothing has been deleted.',
        [{ id: 'rescan', label: 'Check again' }],
      );
  }
}

/**
 * The banner for a snapshot — one per `state(reason)` of the design's state
 * table. `settings` supplies the folder names and whether the viewer is the
 * admin (who is offered the admin page where others are told to ask).
 */
export function stateBanner(
  snap: UserDocsSnapshot,
  settings?: UserDocsSettings | null,
  ctx: BannerContext = {},
): StateBanner {
  const isAdmin = !!settings?.policy_view.is_admin;
  const adminLink: BannerAction[] = isAdmin
    ? [{ id: 'open_admin', label: 'Open Vector Embedding settings' }]
    : [];
  const progress = syncProgress(snap);
  const failed = failedCount(snap);
  const failedNote = failed > 0 ? ` ${plural(failed, 'file')} could not be indexed.` : '';
  const retry: BannerAction[] = failed > 0 ? [{ id: 'retry_failed', label: 'Retry failed files' }] : [];

  switch (snap.state) {
    case 'disabled': {
      const kept = ctx.keptIndexBytes && ctx.keptIndexBytes > 0
        ? ` Your index is kept (${formatBytes(ctx.keptIndexBytes)}) — turning it back on only catches up with what changed.`
        : '';
      return banner(
        'info',
        'Document search is off',
        `Turn it on to let the agent search your own files and answer with citations.${kept}`,
        [{ id: 'enable', label: 'Turn on', primary: true }],
      );
    }

    case 'suspended':
      if (snap.reason === 'admin_gate' && !snap.enabled) {
        // The server reports the gate before the profile's own switch, so a
        // profile that never turned it on lands here too — with no index to keep.
        return banner(
          'info',
          'Not available on this server yet',
          isAdmin
            ? 'Allow User Document Search on the Vector Embedding page, then turn it on here.'
            : 'An administrator has to allow User Document Search before you can turn it on.',
          adminLink,
        );
      }
      if (snap.reason === 'admin_gate') {
        return banner(
          'warning',
          'Disabled by your administrator — your index is kept',
          'The agent cannot search your documents until an administrator allows User Document Search '
            + 'again. Nothing has been deleted, and syncing picks up where it left off.'
            + (isAdmin ? '' : ' Ask your administrator if you need it back.'),
          adminLink,
        );
      }
      if (snap.reason === 'embedding_off') {
        const when = formatWhen(ctx.asOf);
        return banner(
          'warning',
          `Vector Embedding is off — keyword search over the index as of ${when || 'when it was turned off'}`,
          'Syncing is paused and new changes are not picked up. The agent can still find your '
            + 'documents by keyword; search by meaning returns when Vector Embedding is back on.'
            + (isAdmin ? '' : ' Your administrator controls Vector Embedding.'),
          adminLink,
        );
      }
      if (snap.reason === 'allow_in') {
        return banner(
          'info',
          'Not available here',
          'You chose not to let the agent use your documents in this kind of conversation '
            + '(channels or group rooms). The web app and CLI are unaffected.',
        );
      }
      return banner('warning', 'Document search is suspended', 'Syncing is paused. Nothing has been deleted.');

    case 'blocked':
      // feature_missing: text files still index; PDF and Office wait.
      return banner(
        'warning',
        'PDF and Office files are waiting for a document reader',
        isAdmin
          ? 'Plain-text files are indexed normally. Install the document readers from the Vector '
            + 'Embedding page and the waiting files are picked up on their own.'
          : 'Plain-text files are indexed normally. Ask your administrator to install the document '
            + 'readers; the waiting files are picked up on their own.',
        adminLink,
      );

    case 'hold':
      return holdBanner(snap, settings);

    case 'awaiting_confirmation': {
      const conf = snap.confirmation;
      if (snap.reason === 'mass_delete' || conf?.kind === 'mass_delete') {
        const missing = conf?.kind === 'mass_delete' ? conf.missing : 0;
        const total = conf?.kind === 'mass_delete' ? conf.total : 0;
        return banner(
          'warning',
          `${formatCount(missing)} of ${formatCount(total)} files disappeared — remove them?`,
          'They vanished at once, so Cremind hides them from search and waits for you. If a drive or '
            + 'share is disconnected, keep them: they come back when it returns.',
          [{ id: 'review_deletions', label: 'Review', primary: true }],
        );
      }
      if (snap.reason === 'first_sync' || conf?.kind === 'first_sync') {
        const est = conf?.kind === 'first_sync' ? conf.estimate : null;
        const what = est?.files != null
          ? `Found ${plural(est.files, 'file')} (${formatBytes(est.bytes)}) in ${rootOf(snap, settings)}. `
          : '';
        return banner(
          'info',
          'Ready for the first sync',
          `${what}Review the estimate — size, time and images to describe — and start when you're ready.`,
          [{ id: 'review_first_sync', label: 'Review and start', primary: true }],
        );
      }
      if (snap.reason === 'root_change' || conf?.kind === 'root_change') {
        return holdBanner({ ...snap, reason: 'root_change' }, settings);
      }
      if (snap.reason === 'vision_consent') {
        return banner(
          'info',
          'Describing your photos needs your OK',
          'Images are already findable by name, date, camera and folder. To also find them by what '
            + 'they show, allow sending them to your vision model.',
        );
      }
      return banner('info', 'Waiting for your confirmation', 'Syncing continues once you confirm.');
    }

    case 'estimating':
      return banner(
        'info',
        'Estimating the first sync…',
        `${snap.phase ? `${humanize(snap.phase)}. ` : ''}Counting files and sizes — nothing is read or `
          + 'indexed yet.',
        [],
        { busy: true },
      );

    case 'paused': {
      const storage = snap.storage ?? {};
      const free = storage.free_bytes != null ? ` ${formatBytes(storage.free_bytes)} free.` : '';
      switch (snap.reason) {
        case 'user':
          return banner(
            'info',
            'Syncing is paused',
            'Changes to your files are not picked up until you resume. Search keeps working over '
              + 'what is already indexed.',
            [{ id: 'resume', label: 'Resume', primary: true }],
          );
        case 'budget':
          return banner(
            'warning',
            'The storage budget for document search is full',
            `${storage.message || 'New and growing files wait; deletes, moves and search keep working.'} `
              + 'Exclude folders you do not need searched'
              + (isAdmin ? ', or raise the budget on the Vector Embedding page.' : ', or ask your administrator for more space.'),
            [{ id: 'adjust_excludes', label: 'Adjust exclusions', primary: true }, ...adminLink],
          );
        case 'disk_low':
          return banner(
            'warning',
            'Disk space is low — indexing is paused',
            `${storage.message || 'Deletes and purges still run, and search keeps working.'}${free} `
              + 'Indexing resumes on its own once space is freed.',
            [{ id: 'adjust_excludes', label: 'Adjust exclusions' }],
          );
        case 'disk_critical':
          return banner(
            'error',
            'Disk space is critically low — the index is frozen',
            `${storage.message || 'Only changes that free space are applied. Search keeps working.'}${free} `
              + 'Free up disk space; indexing resumes on its own.',
            [{ id: 'adjust_excludes', label: 'Adjust exclusions' }],
          );
        default:
          return banner('info', 'Syncing is paused', 'Search keeps working over what is already indexed.',
            [{ id: 'resume', label: 'Resume', primary: true }]);
      }
    }

    case 'scanning':
      return banner(
        'info',
        'Checking your folder for changes…',
        `${snap.phase ? `${humanize(snap.phase)}. ` : ''}Only files that changed are read again.${failedNote}`,
        [{ id: 'pause', label: 'Pause' }],
        { busy: true },
      );

    case 'reembedding': {
      const pct = progress.pct != null ? ` ${Math.floor(progress.pct)}%` : '';
      return banner(
        'info',
        `Re-indexing for the new model${pct}`,
        'The embedding model changed, so the stored text is being embedded again — nothing is read '
          + 'from your files. Search keeps working and improves as this finishes.',
        [],
        { busy: true },
      );
    }

    case 'indexing': {
      const counts = progress.total > 0
        ? `${formatCount(progress.done)} of ${formatCount(progress.total)} files`
        : 'Reading your files';
      const eta = formatEta(progress.etaS);
      // The keyword index is written with each file, so search is never
      // waiting for the whole run to finish.
      return banner(
        'info',
        'Indexing documents',
        `${counts}${eta ? ` · ${eta}` : ''}. Search already covers what is indexed so far.${failedNote}`,
        [{ id: 'pause', label: 'Pause' }, ...retry],
        { busy: true },
      );
    }

    case 'idle': {
      const indexed = snap.stages
        ? (snap.stages.indexed ?? 0) + (snap.stages.metadata_only ?? 0)
        : null;
      const watching = snap.watch?.mode === 'poll'
        ? 'checked for changes regularly'
        : 'watched for changes';
      return banner(
        failed > 0 ? 'warning' : 'success',
        failed > 0 ? `Up to date — ${plural(failed, 'file')} could not be indexed` : 'Up to date',
        `${indexed != null ? `${plural(indexed, 'file')} indexed; ` : ''}your folder is ${watching}.`
          + (failed > 0 ? ' See the failed files below for why.' : ''),
        retry,
      );
    }

    case 'error':
      return banner(
        'error',
        'Syncing ran into a problem — retrying',
        `${snap.detail?.message || 'The last sync failed.'} Cremind retries on its own; search keeps `
          + 'working over what is already indexed.',
        [{ id: 'rescan', label: 'Retry now' }],
      );

    default:
      return banner(
        'info',
        'Status unavailable',
        "The sync status couldn't be read just now. It refreshes on its own.",
      );
  }
}

/** The NavRail chip's tooltip, e.g. "Indexing documents 3,120/12,840 · 12 failed". */
export function chipTooltip(snap: UserDocsSnapshot | null | undefined): string {
  if (!snap) return '';
  const failed = failedCount(snap);
  const failedTail = failed > 0 ? ` · ${formatCount(failed)} failed` : '';
  if (isActive(snap)) {
    const p = syncProgress(snap);
    switch (snap.state) {
      case 'estimating':
        return 'Estimating the first document sync…';
      case 'scanning':
        return `Checking your documents for changes…${failedTail}`;
      case 'reembedding':
        return `Re-indexing documents for the new model${p.pct != null ? ` ${Math.floor(p.pct)}%` : ''}`;
      default:
        return p.total > 0
          ? `Indexing documents ${formatCount(p.done)}/${formatCount(p.total)}${failedTail}`
          : `Indexing documents…${failedTail}`;
    }
  }
  if (snap.state === 'idle' && failed > 0) {
    return `${plural(failed, 'document')} could not be indexed`;
  }
  return stateBanner(snap).title;
}
