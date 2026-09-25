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
  DriveFolder,
  PlanEffect,
  UserDocsDriveFolderRef,
  UserDocsDriveLink,
  UserDocsDriveView,
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
  awaiting_consent: 'Waiting for your OK to send it to the vision model',
  over_cap: "Waiting — today's image limit is reached",
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
      // Narrowing Drive's folders: the counter is asked with kind="drive".
      if (effect.detail?.kind === 'drive' || effect.detail?.source === 'drive') {
        return `Remove ${plural(effect.files, 'Google Drive file')} that are not inside the chosen folders from the index.`;
      }
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
  /** Google Drive is on for the profile (the saved setting). Defaults to the
   *  snapshot's `drive.enabled`; with the local folder off it turns "search
   *  is off" into "searching Drive only". */
  driveEnabled?: boolean;
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

/** A server message as the lead sentence of a body: capitalised, closed
 *  with a full stop, followed by a space. Empty when there is none. */
function sentence(message: unknown): string {
  if (typeof message !== 'string' || !message.trim()) return '';
  const text = message.trim();
  return `${text[0].toUpperCase()}${text.slice(1)}${/[.!?]$/.test(text) ? '' : '.'} `;
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

    // Google Drive holds. The Drive source reports these in `snap.drive`,
    // beside the local folder's state; `driveBanner` words them through here.
    // Revoked and unlinked hide Drive from search and remove its index when
    // the timer runs out; unreachable and misconfigured never remove anything.
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
    case 'drive_unlinked': {
      const when = formatWhen(Number(detail.purge_at) || null);
      return banner(
        'warning',
        `Google Drive was unlinked — Drive index removed${when ? ` on ${when}` : ' soon'}`,
        'This profile no longer has a Google Drive link (it was unlinked, or the Drive skill was removed), '
          + 'so Drive files are hidden from search and not synced. Link Google Drive again to keep their index.',
        [{ id: 'relink_google', label: 'Link Google Drive', primary: true }],
      );
    }
    case 'drive_unreachable':
      return banner(
        'warning',
        'Google Drive is unreachable — results may be outdated',
        `${sentence(detail.message)}Drive files stay searchable as they were last synced; syncing resumes `
          + 'when Drive answers again.',
      );
    case 'drive_misconfigured':
      return banner(
        'warning',
        "Google refused Cremind's sign-in — results may be outdated",
        `${sentence(detail.message)}Drive files stay searchable as they were last synced, and nothing is `
          + 'removed. Re-linking Google usually fixes it; if it keeps happening, the Google client settings '
          + 'need checking.',
        [{ id: 'relink_google', label: 'Re-link Google' }],
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
  // A profile may search Google Drive with no local folder at all; the
  // top-level state is the local folder's, so "off" must not read as
  // "document search is off" then.
  const driveOn = ctx.driveEnabled ?? !!snap.drive?.enabled;
  const localOn = !!snap.sources?.local?.enabled;

  switch (snap.state) {
    case 'disabled': {
      if (driveOn) {
        return banner(
          'info',
          'Searching Google Drive only',
          'The agent searches your Google Drive files. No folder on this computer is indexed — turn on '
            + 'Search my documents to add one.',
          [{ id: 'enable', label: 'Index a folder too' }],
        );
      }
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
      const where = !localOn && driveOn
        ? 'Google Drive is checked for changes every few minutes'
        : `your folder is ${watching}`;
      return banner(
        failed > 0 ? 'warning' : 'success',
        failed > 0 ? `Up to date — ${plural(failed, 'file')} could not be indexed` : 'Up to date',
        `${indexed != null ? `${plural(indexed, 'file')} indexed; ` : ''}${where}.`
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

// ── image descriptions (the Specialized Vision Model) ─────────────────────

/** A file row's image-description state, for the file table; null when
 *  there is nothing to say (described, not an image, or an icon-sized image
 *  that is never sent). */
export function captionStateLabel(state: string | null | undefined): string | null {
  switch (state) {
    case 'awaiting_vision': return 'No description yet — no vision model is set up';
    case 'awaiting_consent': return 'No description yet — waiting for your OK';
    case 'over_cap': return "No description yet — today's image limit is reached";
    case 'failed': return 'The vision model could not describe it — Retry all tries again';
    case 'captions_off': return 'No description — image descriptions are off';
    default: return null;
  }
}

export type VisionAction = 'open_llm' | 'consent' | null;

export interface VisionStatus {
  tone: BannerTone;
  title: string;
  detail: string;
  action: VisionAction;
}

const VISION_SETUP: Record<string, string> = {
  vision_disabled: 'Image understanding is turned off in LLM Providers.',
  vision_model_unset: 'No Specialized Vision Model is chosen in LLM Providers.',
  vision_model_auth_incompatible:
    "The chosen vision model can't be used with how its provider is signed in — pick another in LLM Providers.",
  vision_model_not_capable: "The chosen vision model can't read images — pick another in LLM Providers.",
  vision_model_error: "The vision model settings couldn't be read.",
};

/**
 * Whether photos and scanned pages can be described right now, and what to
 * do if not. Descriptions come only from the Specialized Vision Model — the
 * main model is never used — so a missing model is a setup step, not a
 * fallback.
 */
export function visionStatus(
  vision: UserDocsSettings['vision'] | null | undefined,
  captionsEnabled = true,
): VisionStatus {
  if (!captionsEnabled) {
    return {
      tone: 'info',
      title: 'Image descriptions are off',
      detail: 'Photos are found by name, folder, date and camera only.',
      action: null,
    };
  }
  const model = vision?.provider && vision?.model ? `${vision.provider}/${vision.model}` : null;
  if (!vision || (vision.reason && vision.reason !== 'no_consent')) {
    return {
      tone: 'warning',
      title: 'No vision model to describe images',
      detail: `${VISION_SETUP[vision?.reason ?? ''] ?? VISION_SETUP.vision_model_unset} `
        + 'Until then photos are found by name, folder, date and camera.',
      action: 'open_llm',
    };
  }
  if (!vision.consent) {
    const old = vision.consent_for;
    const changed = old && model && `${old.provider}/${old.model}` !== model;
    return {
      tone: 'info',
      title: changed ? 'The vision model changed — allow it again?' : 'Allow describing your images?',
      detail: `Photos and scanned pages will be sent to ${model ?? 'your vision model'} to describe what they `
        + 'show. Nothing is sent until you allow it.'
        + (changed ? ` You had allowed ${old!.provider}/${old!.model}.` : ''),
      action: 'consent',
    };
  }
  return {
    tone: 'success',
    title: 'Describing images',
    detail: `Photos and scanned pages are described by ${model}.`,
    action: null,
  };
}

/** "37 of 1,000 today" with a fill fraction (0–1), or null without a quota. */
export function visionQuota(
  quota: { used: number; ocr_pages?: number; cap: number } | null | undefined,
): { label: string; fraction: number } | null {
  if (!quota) return null;
  const used = (quota.used ?? 0);
  const cap = Math.max(0, quota.cap ?? 0);
  // A cap of 0 sends nothing (the server reserves `used < cap`).
  return cap > 0
    ? { label: `${formatCount(used)} of ${formatCount(cap)} today`, fraction: Math.min(1, used / cap) }
    : { label: 'Daily limit is 0 — no images are sent', fraction: 1 };
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

// ── Google Drive ───────────────────────────────────────────────────────────
// Drive is a second source beside the local folder, with its own state in
// `snap.drive`. Everything here reads that view (or the saved link) and never
// the top-level state, which belongs to the local folder.

const DRIVE_HOLD_REASONS = new Set(['auth_revoked', 'drive_unlinked', 'drive_unreachable', 'drive_misconfigured']);

/**
 * The Drive banner: a hold (revoked, unlinked, unreachable, misconfigured —
 * worded by the same table as the top-level banner) or a held mass removal.
 * Null while Drive is off or simply live; the Drive section's status line
 * covers those.
 */
export function driveBanner(snap: UserDocsSnapshot | null | undefined): StateBanner | null {
  const drive = snap?.drive;
  if (!snap || !drive?.enabled) return null;
  if (drive.state === 'hold') {
    const detail = drive.detail ?? {};
    if (drive.reason && DRIVE_HOLD_REASONS.has(drive.reason)) {
      return holdBanner({ ...snap, state: 'hold', reason: drive.reason, detail, confirmation: null }, null);
    }
    return banner(
      'warning',
      'Google Drive syncing is on hold',
      `${sentence(detail.message) || 'Something needs attention before Drive can sync again. '}`
        + 'Nothing has been removed from the index.',
    );
  }
  const conf = drive.confirmation;
  if (conf?.kind === 'mass_delete') {
    return banner(
      'warning',
      `${formatCount(conf.missing)} of ${formatCount(conf.total)} Google Drive files disappeared — remove them?`,
      'They vanished from what Cremind can see in your Drive all at once — moved to the trash, unshared, '
        + 'or moved out of the chosen folders. Nothing is removed from the index until you decide; if access '
        + 'changed by mistake, keep them.',
      [{ id: 'review_deletions', label: 'Review', primary: true }],
    );
  }
  return null;
}

/** Epoch seconds or milliseconds → milliseconds. The Drive view's sync
 *  times come from the index, which keeps seconds; `purge_at` is already ms. */
export function toEpochMs(ts: number | null | undefined): number | null {
  const n = Number(ts);
  if (ts == null || !Number.isFinite(n) || n <= 0) return null;
  return n < 1e11 ? n * 1000 : n;
}

/** "just now", "12 min ago", "3 h ago", then the date and time. */
export function formatAgo(ts: number | null | undefined, now = Date.now()): string {
  const ms = toEpochMs(ts);
  if (ms == null) return '';
  const s = Math.max(0, (now - ms) / 1000);
  if (s < 60) return 'just now';
  const min = Math.floor(s / 60);
  if (min < 60) return `${min} min ago`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours} h ago`;
  return formatWhen(ms);
}

/** "1,204 files indexed · 3 waiting · 2 failed · 7 by name only". */
export function driveCountsText(view: UserDocsDriveView | null | undefined): string {
  const c = view?.counts ?? {};
  const parts = [`${plural(Number(c.indexed) || 0, 'file')} indexed`];
  if (Number(c.pending) > 0) parts.push(`${formatCount(c.pending)} waiting`);
  if (Number(c.error) > 0) parts.push(`${formatCount(c.error)} failed`);
  if (Number(c.metadata_only) > 0) parts.push(`${formatCount(c.metadata_only)} by name only`);
  return parts.join(' · ');
}

export interface DriveStatus {
  tone: BannerTone;
  label: string;
  busy: boolean;
}

/** The short status beside the Drive switch; null while Drive is off. */
export function driveStatus(view: UserDocsDriveView | null | undefined): DriveStatus | null {
  if (!view?.enabled) return null;
  if (view.state === 'hold') {
    switch (view.reason) {
      case 'auth_revoked': return { tone: 'warning', label: 'On hold — Google access was revoked', busy: false };
      case 'drive_unlinked': return { tone: 'warning', label: 'On hold — Google Drive was unlinked', busy: false };
      case 'drive_unreachable': return { tone: 'warning', label: 'Unreachable — retrying', busy: false };
      case 'drive_misconfigured': return { tone: 'warning', label: 'Sign-in refused — retrying', busy: false };
      default: return { tone: 'warning', label: 'On hold', busy: false };
    }
  }
  if (view.confirmation) return { tone: 'warning', label: 'Waiting for your confirmation', busy: false };
  const counts = view.counts ?? {};
  const pending = Number(counts.pending) || 0;
  if (pending > 0) return { tone: 'info', label: `Indexing — ${formatCount(pending)} waiting`, busy: true };
  const known = (Number(counts.indexed) || 0) + (Number(counts.error) || 0) + (Number(counts.metadata_only) || 0);
  if (!view.last_sync_at && !view.last_full_at && known === 0) {
    // The first listing has not finished (or found nothing yet).
    return { tone: 'info', label: 'Listing your Drive…', busy: true };
  }
  const failed = Number(counts.error) || 0;
  if (failed > 0) return { tone: 'warning', label: `Up to date — ${plural(failed, 'file')} failed`, busy: false };
  return { tone: 'success', label: 'Up to date', busy: false };
}

/** What the linked account lets Cremind index. */
export function driveAccessText(link: UserDocsDriveLink | null | undefined): string {
  if (!link?.linked) return 'Not linked';
  return link.whole_drive
    ? 'Whole Drive — choose the folders to index'
    : 'The files you granted Cremind, and the files it created';
}

export type DriveEnableBlocker = 'not_linked' | 'folders_required' | null;

/** Why Drive can't be turned on as it stands. A whole-Drive account must
 *  name folders first — indexing someone's entire Drive is never implied. */
export function driveEnableBlocker(
  link: UserDocsDriveLink | null | undefined,
  folders: readonly string[],
): DriveEnableBlocker {
  if (!link?.linked) return 'not_linked';
  if (link.whole_drive && folders.length === 0) return 'folders_required';
  return null;
}

// ── the Drive folder picker ───────────────────────────────────────────────

/** One step of the picker's path; the first (`id: null`) is the top. */
export interface DriveCrumb {
  id: string | null;
  name: string;
}

export function driveRootCrumb(wholeDrive: boolean): DriveCrumb {
  return { id: null, name: wholeDrive ? 'My Drive' : 'Shared with Cremind' };
}

/** The path after opening `folder`. */
export function enterFolder(crumbs: readonly DriveCrumb[], folder: DriveFolder): DriveCrumb[] {
  return [...crumbs, { id: folder.id, name: folder.name }];
}

/** The path after going back to the crumb at `index` (the top stays). */
export function crumbsTo(crumbs: readonly DriveCrumb[], index: number): DriveCrumb[] {
  return crumbs.slice(0, Math.max(1, index + 1));
}

/** The chosen folder the current one sits in, if any: a folder's subfolders
 *  are indexed with it, so choosing them as well would add nothing. */
export function coveringFolder(crumbs: readonly DriveCrumb[], selected: readonly DriveFolder[]): DriveFolder | null {
  const chosen = new Set(selected.map(f => f.id));
  for (const c of crumbs) {
    if (c.id && chosen.has(c.id)) return { id: c.id, name: c.name };
  }
  return null;
}

/** Choose `folder`, or un-choose it if it already is. */
export function toggleFolder(selected: readonly DriveFolder[], folder: DriveFolder): DriveFolder[] {
  return selected.some(f => f.id === folder.id)
    ? selected.filter(f => f.id !== folder.id)
    : [...selected, { id: folder.id, name: folder.name }];
}

/** Same folders, whatever the order. */
export function sameFolderSet(a: readonly string[], b: readonly string[]): boolean {
  const left = new Set(a);
  const right = new Set(b);
  return left.size === right.size && [...left].every(id => right.has(id));
}

/** Folders `next` drops from `saved`. Dropping one (or adding one to an
 *  empty per-file set) narrows what is indexed, so the server confirms it. */
export function droppedFolders(saved: readonly string[], next: readonly string[]): string[] {
  const keep = new Set(next);
  return saved.filter(id => !keep.has(id));
}

/** How an id with no known name is shown. */
export function folderFallbackName(id: string): string {
  return `Folder ${id.length > 8 ? `${id.slice(0, 8)}…` : id}`;
}

/**
 * The chosen folders with names: `ids` (the saved setting) in order, named
 * from the Drive view's resolved entries, then from names the picker learned,
 * then a fallback. Ids only in `refs` (a view newer than the settings) are
 * left out — the saved setting is what a save would keep.
 */
export function namedFolders(
  ids: readonly string[],
  refs: readonly UserDocsDriveFolderRef[] | null | undefined = [],
  known: Readonly<Record<string, string>> = {},
): DriveFolder[] {
  const fromView: Record<string, string> = {};
  for (const ref of refs ?? []) {
    if (ref && typeof ref === 'object' && ref.id && ref.name) fromView[ref.id] = ref.name;
  }
  const seen = new Set<string>();
  const out: DriveFolder[] = [];
  for (const id of ids) {
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push({ id, name: fromView[id] || known[id] || folderFallbackName(id) });
  }
  return out;
}

const DRIVE_ID = /^[A-Za-z0-9_-]+$/;

/** A folder id from a pasted Google Drive link, or a bare id; null when the
 *  text is neither (the server validates ids against the same pattern). */
export function parseDriveFolderRef(text: string | null | undefined): string | null {
  const t = (text ?? '').trim();
  if (!t) return null;
  if (DRIVE_ID.test(t)) return t.length >= 10 ? t : null;
  let url: URL;
  try {
    url = new URL(t);
  } catch {
    return null;
  }
  if (url.protocol !== 'https:' || !/(^|\.)google\.com$/i.test(url.hostname)) return null;
  const m = url.pathname.match(/\/folders\/([A-Za-z0-9_-]+)/);
  if (m) return m[1];
  const id = url.searchParams.get('id');
  return id && DRIVE_ID.test(id) ? id : null;
}

// ── files from both sources ───────────────────────────────────────────────

/** Only an https link is ever put in an href — a server-supplied string must
 *  not become a `javascript:` (or any other scheme's) URL. */
export function safeWebLink(link: string | null | undefined): string | null {
  if (typeof link !== 'string') return null;
  const t = link.trim();
  if (!/^https:\/\//i.test(t)) return null;
  try {
    return new URL(t).protocol === 'https:' ? t : null;
  } catch {
    return null;
  }
}

/** "Google Drive" / "This computer". */
export function sourceLabel(source: string | null | undefined): string {
  return source === 'drive' ? 'Google Drive' : 'This computer';
}
