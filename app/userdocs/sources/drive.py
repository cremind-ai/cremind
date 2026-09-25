"""Google Drive as a User Document Search source: one profile's Drive half.

A :class:`DriveSource` lives next to the local folder inside a profile's
:class:`~app.userdocs.runtime.ProfileRuntime` (as ``rt.drive``) and shares its
index file. It indexes everything the profile's linked Google account lets
Cremind see — the files granted file-by-file (``drive.file``) or, for an
account holding whole-Drive access, the folders the user chose
(``include_folders``, required there so a whole Drive is never indexed by
accident).

**Keys.** A Drive row is keyed by its file id, never by its path:
``path_hash = drive_key(file_id)``. Drive allows two files of the same name in
one folder and a legacy file can have several parents, so a path is only a
display string (``rel_path = "Drive/<visible parents>/<name>"``). Folder rows
are keyed by ``drive_key("folder:" + id)``.

**Sync.** The first sync (and a rejected cursor, a lost shared drive, a
changed ``include_folders`` and every ``reconcile_interval_min``) is a full
reconcile: take the change-feed token *before* listing, so nothing that
changes during the listing is lost, then compare the listing with the index
by file id. In between, the change feed is polled every 5 minutes and its
cursor committed after every page. A rename or move changes the row and its
file card only; content is re-read only when Drive says it changed (binary:
``md5Checksum``/``size``; Google-native: ``modifiedTime``/``version``, and an
export whose sha256 matches the indexed one still counts as unchanged).

**Nothing is removed on doubt.** An incomplete listing removes nothing; a
reconcile that would remove half the index (and at least 200 files) asks
first, as the local folder does; a file is purged only when ``files.get``
confirms it is gone, and 50 such purges within 10 minutes hold the source
instead. An unreachable Drive is a hold that clears itself; a revoked or
unlinked account hides Drive results and deletes the Drive index only 7 days
after the first confirmation, re-confirmed at that moment.

Threading: :meth:`configure`, :meth:`sync_due`, :meth:`housekeeping` run on
the service's maintenance thread and never touch the network;
:meth:`run_sync` runs on its scan executor; :meth:`process` on the pipeline
workers. The index serialises its own writes; everything else shared is
guarded by ``self._lock``.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Iterable

from app.userdocs import settings as uds
from app.userdocs import types as t
from app.userdocs.textnorm import fold
from app.utils.logger import logger

SOURCE = uds.SOURCE_DRIVE

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_SHORTCUT = "application/vnd.google-apps.shortcut"
NATIVE_PREFIX = "application/vnd.google-apps."

WHOLE_DRIVE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
})

POLL_INTERVAL_S = 300.0
# Held sources are probed after 1 min, 5 min, then every 15 min.
HOLD_RETRY_S = (60.0, 300.0, 900.0)
# A confirmed revocation or unlink deletes the Drive index this long after
# the first confirmation.
PURGE_GRACE_S = 7 * 86400.0
# Content is held in memory for extraction; nothing bigger is downloaded.
IN_MEMORY_CAP = 256 * 1024 * 1024
LIST_MAX_FILES = 50_000
# Two reads this far apart must both find no token file before "unlinked"
# counts: the skill replaces the file atomically, and a read can land mid-swap.
UNLINK_RECHECK_S = 1.0
# More row purges than this inside the window mean something account-wide.
UNAVAILABLE_LIMIT = 50
UNAVAILABLE_WINDOW_S = 600.0
# Rows the user kept after a mass removal stay hidden and re-checked this long.
MISSING_KEEP_S = 14 * 86400.0
MAX_DEPTH = 64
ANCESTOR_CACHE_MAX = 20_000

# Hold reasons whose data is deleted after PURGE_GRACE_S.
TIMER_REASONS = frozenset({"auth_revoked", "drive_unlinked"})
# DriveError kinds that stop the whole source rather than one file.
SOURCE_KINDS = frozenset({
    "auth_revoked", "unlinked", "auth_failed", "auth_misconfigured", "unreachable", "account_forbidden",
})
KINDS = SOURCE_KINDS | frozenset({
    "rate_limited", "bad_cursor", "not_found", "not_authorized", "not_downloadable",
    "file_unavailable", "too_large", "export_too_large", "http",
})

CARD_SOURCE = "Google Drive"
METADATA_ONLY_NOTE = "metadata only (content not read)"

# Mime types that are metadata-only whatever the name says (the name may
# have no extension at all).
_ARCHIVE_MIMES = frozenset({
    "application/zip", "application/x-zip-compressed", "application/x-7z-compressed",
    "application/x-rar-compressed", "application/vnd.rar", "application/x-tar", "application/gzip",
    "application/x-gzip", "application/x-bzip2", "application/x-xz", "application/zstd",
    "application/x-iso9660-image",
})
_EXECUTABLE_MIMES = frozenset({
    "application/x-msdownload", "application/x-msdos-program", "application/x-executable",
    "application/x-mach-binary", "application/x-apple-diskimage", "application/x-sharedlib",
    "application/vnd.microsoft.portable-executable", "application/vnd.android.package-archive",
})
# What a Google-native file is when it cannot be exported (the kind its
# export would have had, so filters treat both alike).
_NATIVE_KINDS = {
    "application/vnd.google-apps.document": t.KIND_MARKDOWN,
    "application/vnd.google-apps.spreadsheet": t.KIND_XLSX,
    "application/vnd.google-apps.presentation": t.KIND_PPTX,
    "application/vnd.google-apps.drawing": t.KIND_IMAGE,
}

_ROW_MESSAGES = {
    "rate_limited": "Google Drive is rate-limiting requests; retrying later",
    "file_unavailable": "Google Drive could not serve the file right now; retrying later",
    "http": "Google Drive returned an unexpected error",
    "bad_cursor": "Google Drive returned an unexpected error",
}

_HOLD_MESSAGES = {
    "auth_revoked": "Google rejected Cremind's access to Drive. Drive results are hidden; re-link Google "
                    "within 7 days or the Drive index is removed.",
    "drive_unlinked": "Google Drive is no longer linked. Drive results are hidden; re-link Google within "
                      "7 days or the Drive index is removed.",
    "drive_unreachable": "Google Drive can't be reached right now; Drive results may be out of date. "
                         "Nothing was removed.",
    "drive_misconfigured": "Google Drive refused Cremind's credentials or settings; check the Google link.",
}


class _Aborted(Exception):
    """A sync was told to stop (suspend, purge, shutdown)."""


# ── keys and paths ──────────────────────────────────────────────────────────


def drive_key(ident: str) -> str:
    """``path_hash`` of a Drive row: blake2b-128 of ``"drive:" + ident``.
    No case folding — Drive ids are case-sensitive."""
    return hashlib.blake2b(("drive:" + str(ident)).encode("utf-8"), digest_size=16).hexdigest()


def _segment(name: Any) -> str:
    """One display-path segment. A "/" inside a Drive name would invent a
    folder, so it becomes a look-alike."""
    s = str(name or "").strip().replace("/", "∕")
    return s or "(untitled)"


def drive_rel_path(names: list[str]) -> str:
    """``"Drive/a/b/name"`` — the display path of a Drive item from its
    visible parents' names down to its own."""
    return "/".join(["Drive", *(_segment(n) for n in names)])


def is_whole_drive(scopes: Iterable[str]) -> bool:
    """Whether a token's scopes reach the whole Drive (``drive`` or
    ``drive.readonly``) rather than granted files only."""
    return bool(WHOLE_DRIVE_SCOPES & {str(s) for s in scopes or ()})


# ── small helpers ───────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def _ms(ts: Any) -> float | None:
    return float(ts) * 1000 if ts else None


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _ts(value: Any) -> float | None:
    """Epoch seconds of an RFC 3339 timestamp from the Drive API."""
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _version(meta: dict[str, Any]) -> str | None:
    v = meta.get("version")
    return None if v in (None, "") else str(v)


def _first_parent(meta: dict[str, Any]) -> str | None:
    parents = meta.get("parents") or []
    return str(parents[0]) if parents else None


def _clean_name(value: Any) -> str:
    return str(value or "").strip() or "(untitled)"


def _drive_kind(exc: BaseException) -> str | None:
    """The DriveError kind of ``exc`` (also a token error carrying one), or
    None for anything else. Duck-typed, so the client can be swapped."""
    kind = getattr(exc, "kind", None)
    return kind if isinstance(kind, str) and kind in KINDS else None


def _is_native(mime: str) -> bool:
    return (mime or "").startswith(NATIVE_PREFIX)


def _meta_exif(meta: dict[str, Any]) -> dict[str, Any]:
    """Photo details Drive already parsed (``imageMediaMetadata``), so an
    image whose caption is off or cached costs no download to date it."""
    imm = meta.get("imageMediaMetadata") or {}
    if not isinstance(imm, dict) or not imm:
        return {}
    out: dict[str, Any] = {}
    taken = _exif_time(imm.get("time"))
    if taken:
        out["taken_at"] = taken
    for key, src in (("make", "cameraMake"), ("model", "cameraModel")):
        val = str(imm.get(src) or "").strip()
        if val:
            out[key] = val
    loc = imm.get("location") or {}
    try:
        lat, lon = float(loc.get("latitude")), float(loc.get("longitude"))
    except (TypeError, ValueError):
        lat = lon = None  # type: ignore[assignment]
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180 and (lat, lon) != (0.0, 0.0):
        out["gps"] = {"lat": round(lat, 6), "lon": round(lon, 6)}
    for key in ("width", "height"):
        n = _int(imm.get(key))
        if n:
            out[key] = n
    return out


def _exif_time(value: Any) -> str | None:
    try:
        from app.userdocs.extract.formats.image import exif_datetime
    except Exception:  # noqa: BLE001 — the date is a nicety, never a failure
        return None
    return exif_datetime(value)


def _merge_exif(extracted: dict[str, Any] | None, from_drive: dict[str, Any]) -> dict[str, Any]:
    """The file's own EXIF wins; Drive's parse fills what it lacks."""
    out = dict(from_drive or {})
    out.update({k: v for k, v in (extracted or {}).items() if v is not None})
    return out


def _type_only_kind(name: str, mime: str) -> str | None:
    """A kind that is metadata-only by its name or mime alone (video, audio,
    archives, executables…), decided before anything is downloaded."""
    from app.userdocs.kinds import guess_kind

    by_name = guess_kind(name)
    if by_name in t.METADATA_ONLY_KINDS and by_name != t.KIND_OTHER:
        return by_name
    if by_name in (t.KIND_OTHER, t.KIND_TEXT):
        m = (mime or "").lower()
        if m.startswith("video/"):
            return t.KIND_VIDEO
        if m.startswith("audio/"):
            return t.KIND_AUDIO
        if m in _ARCHIVE_MIMES:
            return t.KIND_ARCHIVE
        if m in _EXECUTABLE_MIMES:
            return t.KIND_EXECUTABLE
    return None


def _detect(data: bytes, name: str) -> tuple[str, str | None]:
    try:
        from app.userdocs.extract.detect import detect_bytes
    except ImportError:
        detect_bytes = None  # type: ignore[assignment]
    if detect_bytes is not None:
        kind, mime = detect_bytes(data, name)
        return kind, mime
    from app.userdocs.extract.detect import HEAD_BYTES, TAIL_BYTES, sniff

    return sniff(data[:HEAD_BYTES], data[-TAIL_BYTES:] if data else b"", os.path.splitext(name)[1])


# ── seams (module level so tests and the engine can replace them) ───────────


def _feature_gate() -> tuple[bool, str | None]:
    try:
        return uds.feature_effective(uds.read_admin_policy())
    except Exception:  # noqa: BLE001
        logger.exception("[userdocs] could not read the admin gate for Drive")
        return False, "admin_gate_off"


def _token_present(profile: str) -> bool:
    from app.drive import skill_token

    return skill_token.token_path(profile) is not None


def _token_info(profile: str) -> dict[str, Any] | None:
    from app.drive import skill_token

    return skill_token.read_token(profile)


def _forget_access_token(profile: str) -> None:
    from app.drive import skill_token

    skill_token.forget_access_token(profile)


def _purge_citations(profile: str) -> int:
    from app.storage.userdocs_citations_storage import get_userdocs_citations_storage

    return get_userdocs_citations_storage().delete_source_kind(profile, SOURCE)


def _default_client_factory(profile: str, stop_event: threading.Event) -> Any:
    from app.userdocs.sources.drive_client import DriveClient

    return DriveClient(profile, stop_event=stop_event)


# ── the source ──────────────────────────────────────────────────────────────


class DriveSource:
    """The Drive half of one profile's runtime. Owned by ProfileRuntime as
    ``rt.drive``; all methods run on engine threads (never the event loop)."""

    def __init__(self, rt: Any, *, client_factory: Callable[[str, threading.Event], Any] | None = None):
        self.rt = rt
        self.profile = rt.profile
        self._factory = client_factory or _default_client_factory
        self._lock = threading.RLock()
        self._sync_lock = threading.RLock()
        self._sync_owner: int | None = None
        self._closed = threading.Event()
        self._client: Any = None
        self._client_stop = threading.Event()
        self._sync_client: Any = None

        self._row: dict[str, Any] = {}
        self._enabled = False
        self._gate: str | None = None
        self._include: list[str] = []
        self._reconcile_s = 360 * 60.0
        self._loaded = False

        self._hold: dict[str, Any] | None = None     # {reason, detail, since, purge_after, entered}
        self._hold_attempts = 0
        self._hold_retry_at = 0.0
        self._confirmation: dict[str, Any] | None = None
        self._suspended = False
        self._token_check = False     # a worker saw "unlinked": re-check before more work
        self._purging = False

        self._syncing = False
        self._sync_requested: str | None = None
        self._full_requested = False
        self._next_poll_at = 0.0
        self._next_full_at = 0.0
        self._last_sync_at: float | None = None
        self._last_full_at: float | None = None

        self._account_key: str | None = None
        self._account_email: str | None = None
        self._whole_drive = False
        self._counts: dict[str, int] = {}

        # Sync-thread state (guarded by _sync_lock).
        self._folders: dict[str, dict[str, Any]] = {}   # folder id -> {id, rel, parent_row, name}
        self._folders_loaded = False
        self._scope_ids: set[str] = set()
        self._listed: dict[str, dict[str, Any]] = {}     # folder id -> {name, parent} of the listing at hand
        self._ancestors: dict[str, dict[str, Any] | None] = {}
        self._unavailable: deque[float] = deque()

    # ── lifecycle / settings ───────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    def configure(self, row: dict[str, Any] | None) -> None:
        """Apply the drive ``userdoc_sources`` row (or None): turn Drive on or
        off in memory, read ``include_folders``, and schedule a sync — which
        also re-checks the token, so a suspension ends here."""
        row = dict(row or {})
        ok, gate = _feature_gate()
        opts = uds.normalize_options(row.get("options"))
        include = [str(f) for f in opts.get("include_folders") or []]
        with self._lock:
            was = self._enabled
            before = list(self._include)
            self._row = row
            self._enabled = bool(row.get("enabled")) and ok
            self._gate = None if ok else gate
            self._include = include
            self._reconcile_s = float(opts.get("reconcile_interval_min") or 360) * 60.0
            self._suspended = False
            if was and self._enabled and include != before:
                # Narrowed or widened: only a full listing of the new
                # folders can say what is in scope now.
                self._full_requested = True
        if not self._enabled:
            with self._lock:
                # Off (and usually purged by now): re-read the index when it
                # comes back rather than trust what memory says.
                self._loaded = False
            self._abort_inflight()
            self._publish()
            return
        if not self._loaded:
            self._load_state()
        with self._lock:
            self._next_full_at = (self._last_full_at or 0.0) + self._reconcile_s
        self.request_sync("configure")

    def _load_state(self) -> None:
        """Pick up what the index remembers: a hold (with its purge timer),
        the account, the sync times, and a mass-removal still unanswered."""
        db = self._db()
        st = db.get_source_state(SOURCE)
        pending = db.read_sql(
            "SELECT COUNT(*) AS n FROM files WHERE source = ? AND status = 'missing' "
            "AND COALESCE(status_reason, '') != 'kept'",
            (SOURCE,),
        )
        missing = int((pending[0] if pending else {}).get("n") or 0)
        total = sum(db.count_by_status(SOURCE).values()) if missing else 0
        with self._lock:
            self._account_key = st.get("drive_account_key")
            self._last_sync_at = st.get("drive_cursor_at")
            self._last_full_at = st.get("last_scan_finished_at")
            self._hold = None
            if st.get("state") == "hold" and st.get("reason"):
                self._hold = {
                    "reason": st["reason"], "detail": dict(st.get("detail") or {}),
                    "since": st.get("hold_since"), "purge_after": st.get("purge_after"), "entered": 0.0,
                }
            self._hold_attempts, self._hold_retry_at = 0, 0.0
            self._confirmation = (
                {"kind": "mass_delete", "source": SOURCE, "missing": missing, "total": total} if missing else None
            )
            self._loaded = True
            self._folders_loaded = False
        self.refresh_counts()

    def work_allowed(self) -> bool:
        """Whether the pipeline may process Drive rows right now."""
        with self._lock:
            return bool(
                self._enabled and not self._hold and not self._suspended and not self._token_check
                and not self._purging and not self._confirmation and not self._paused()
                and not self._closed.is_set()
            )

    def suspend(self) -> None:
        """Stop in memory until the next configure()/sync re-checks the token.
        The Google unlink hook calls this for every Google skill, so it must
        not decide anything by itself: the sync it schedules resumes Drive
        when the token is still there."""
        with self._lock:
            self._suspended = True
            self._sync_requested = self._sync_requested or "token_check"
        self._abort_inflight()
        self._publish()
        self._wake()

    def close(self) -> None:
        self._closed.set()
        with self._lock:
            client = self._client
        self._abort_inflight()
        _close_client(client)

    def request_sync(self, reason: str = "user", *, full: bool = False) -> None:
        with self._lock:
            self._sync_requested = self._sync_requested or reason
            if full:
                self._full_requested = True
        self._wake()

    # ── scheduling ─────────────────────────────────────────────────────────

    def sync_due(self, now: float) -> bool:
        with self._lock:
            if (not self._enabled or self._syncing or self._purging or self._confirmation
                    or self._closed.is_set() or self._paused()):
                return False
            if self._sync_requested:
                return True
            if self._suspended or self._token_check:
                return False
            if self._hold:
                return now >= self._hold_retry_at
            return now >= self._next_poll_at

    def housekeeping(self, now: float) -> None:
        """Maintenance thread, no network: make sure a held source is probed
        on its backoff and that a due purge gets its re-confirmation (which
        :meth:`run_sync` does, off this thread)."""
        if not self._enabled or self._closed.is_set():
            return
        with self._lock:
            if self._hold and not self._syncing:
                if self._purge_due(now) or now >= self._hold_retry_at:
                    self._sync_requested = self._sync_requested or "hold_retry"
            while self._unavailable and now - self._unavailable[0] > UNAVAILABLE_WINDOW_S:
                self._unavailable.popleft()
        self.refresh_counts()

    def run_sync(self, *, full: bool = False) -> None:
        """Scan executor: identity check, then a full reconcile or the change
        feed. Never raises — errors become holds or a later retry."""
        if not self._sync_lock.acquire(blocking=False):
            self.request_sync("queued", full=full)
            return
        stop: threading.Event | None = None
        try:
            self._sync_owner = threading.get_ident()
            with self._lock:
                if not self._enabled or self._closed.is_set() or self._paused() or self._confirmation:
                    return
                why = self._sync_requested or "poll"
                self._sync_requested = None
                full = full or self._full_requested
                self._syncing = True
            self._publish()
            client, stop = self._get_client()
            self._sync_client = client
            try:
                self._sync(client, stop, full=full, why=why)
            except _Aborted:
                logger.info(f"[userdocs] {self.profile}: Drive sync stopped")
            except Exception as exc:  # noqa: BLE001 — a sync never raises into the executor
                if stop.is_set() or self._closed.is_set():
                    logger.info(f"[userdocs] {self.profile}: Drive sync stopped")
                elif _drive_kind(exc):
                    self._source_error(exc, worker=False)
                else:
                    logger.exception(f"[userdocs] {self.profile}: Drive sync failed")
                    self._retry_later(HOLD_RETRY_S[1])
                    self._activity("error", f"Google Drive sync failed: {exc}", level="error")
        finally:
            self._sync_client = None
            with self._lock:
                self._syncing = False
            self._sync_owner = None
            self._sync_lock.release()
            self.refresh_counts()
            self._publish()
            self._wake()

    def _sync(self, client: Any, stop: threading.Event, *, full: bool, why: str) -> None:
        db = self._db()
        started = time.monotonic()
        ident = self._identity(client)
        if ident is None:
            return
        self._check_account(db, ident)
        with self._lock:
            self._suspended = False
            self._token_check = False
            full = full or self._full_requested
        self._read_token_details()
        if self._whole_drive and not self._include:
            # Whole-Drive access with no folders chosen: indexing would read
            # the entire Drive, which the user never asked for.
            self._enter_hold("drive_misconfigured", {
                "why": "folders_required",
                "message": "Choose the Drive folders to index; this account can reach the whole Drive.",
            })
            return
        with self._lock:
            hold = dict(self._hold) if self._hold else None
        if hold and hold["reason"] == "auth_revoked" and self._purge_due(_now()):
            # The purge is only ever done on a fresh confirmation.
            try:
                client.about()
            except Exception as exc:  # noqa: BLE001
                if _drive_kind(exc) == "auth_revoked":
                    self._purge_for_hold()
                    return
                raise
        if hold and (hold.get("detail") or {}).get("why") == "many_unavailable":
            full = True
        self._check_abort(stop)

        cursor = db.get_source_state(SOURCE).get("drive_cursor")
        now = _now()
        if full or not cursor or (self._next_full_at and now >= self._next_full_at):
            self._full_sync(client, stop, db, first=not cursor, why=why)
        else:
            try:
                self._changes(client, stop, db, cursor)
            except _Aborted:
                raise
            except Exception as exc:  # noqa: BLE001
                if _drive_kind(exc) != "bad_cursor" or stop.is_set():
                    raise
                self._activity("resync", "Google Drive no longer accepts the saved change position; "
                               "re-listing Drive.", level="warning")
                self._full_sync(client, stop, db, first=False, why="bad_cursor")
            with self._lock:
                again = self._full_requested
            if again:
                self._full_sync(client, stop, db, first=False, why="drive_removed")
        self._clear_hold(since=started)
        with self._lock:
            self._next_poll_at = _now() + POLL_INTERVAL_S

    # ── identity and account ───────────────────────────────────────────────

    def _identity(self, client: Any) -> str | None:
        """``client_id|account_key`` of the linked token, or None after
        entering the right hold. "Unlinked" needs two reads at least
        :data:`UNLINK_RECHECK_S` apart that both find no token file."""
        present = False
        for attempt in range(2):
            if attempt:
                self._wait(UNLINK_RECHECK_S)
            ident = client.identity()
            if ident:
                return str(ident)
            present = present or _token_present(self.profile)
        if present:
            self._enter_hold("drive_misconfigured", {
                "why": "token_unreadable", "message": "The Google Drive token file can't be read.",
            })
            return None
        self._enter_hold("drive_unlinked", {"why": "token_missing"})
        if self._purge_due(_now()):
            self._purge_for_hold()
        return None

    def _check_account(self, db: Any, ident: str) -> None:
        stored = db.get_source_state(SOURCE).get("drive_account_key")
        if stored and stored != ident:
            # Another account (or another OAuth client) now: the in-memory
            # access token is not tied to an account, so drop it before any
            # call, and nothing indexed from the old account may stay.
            logger.warning(f"[userdocs] {self.profile}: Drive account changed; re-indexing")
            _forget_access_token(self.profile)
            self.purge_index(keep_hold=False)
            db.update_source_state(SOURCE, drive_account_key=ident)
            self._activity("account_changed", "Drive account changed — re-indexing", level="warning")
            with self._lock:
                self._account_key = ident
                self._full_requested = True
        elif not stored:
            db.update_source_state(SOURCE, drive_account_key=ident)
            with self._lock:
                self._account_key = ident

    def _read_token_details(self) -> None:
        try:
            info = _token_info(self.profile)
        except Exception:  # noqa: BLE001 — a torn read keeps the last known values
            info = None
        if isinstance(info, dict):
            with self._lock:
                self._account_email = info.get("email") or self._account_email
                self._whole_drive = is_whole_drive(info.get("scopes") or [])

    # ── full reconcile ─────────────────────────────────────────────────────

    def _full_sync(self, client: Any, stop: threading.Event, db: Any, *, first: bool, why: str) -> None:
        from app.userdocs.discovery.guard import RootGuard
        from app.userdocs.runtime import P_BULK

        mono = time.monotonic()
        db.update_source_state(SOURCE, last_scan_started_at=_now())
        self._phase("listing Google Drive")
        # Before the listing: whatever changes while it runs is in the feed.
        token = client.start_page_token()
        self._check_abort(stop)
        include = list(self._include)
        if include:
            listing = client.list_folders_tree(include, max_files=LIST_MAX_FILES)
        else:
            listing = client.list_all(max_files=LIST_MAX_FILES)
        self._check_abort(stop)
        items = [f for f in (listing.files or []) if isinstance(f, dict) and f.get("id") and not f.get("trashed")]
        folders = {str(f["id"]): f for f in items if f.get("mimeType") == MIME_FOLDER}
        files = [f for f in items if f.get("mimeType") != MIME_FOLDER]

        self._ancestors.clear()
        self._load_folders(db, force=True)
        stale = {fid: row for fid, row in self._folders.items() if fid not in folders}
        for fid in stale:
            self._folders.pop(fid, None)
        self._apply_folders(db, folders, use_rows=False)
        self._scope_ids = set(include) | set(folders)

        manifest = db.drive_manifest()
        seen: set[str] = set()
        queued = 0
        files.sort(key=lambda f: str(f.get("modifiedTime") or ""), reverse=True)
        for i, meta in enumerate(files):
            if i % 200 == 0:
                self._check_abort(stop)
                self._phase(f"listing Google Drive ({i} of {len(files)} files compared)")
            seen.add(str(meta["id"]))
            if self._apply_file(db, meta, manifest.get(str(meta["id"])), priority=P_BULK):
                queued += 1

        now = _now()
        held = False
        if not listing.complete:
            self._activity(
                "limit_reached",
                f"Google Drive returned an incomplete listing ({getattr(listing, 'reason', None) or 'truncated'}); "
                "nothing was removed.",
                level="warning",
            )
            held = True  # keep folder rows too
        else:
            unseen = [m for fid, m in manifest.items() if fid not in seen]
            outside = set(db.drive_file_ids_outside(include)) if include else set()
            excluded = [int(m["id"]) for m in unseen if int(m["id"]) in outside]
            if excluded:
                self._purge_rows(db, excluded)
                self._activity("removed", f"Removed {len(excluded)} Drive files outside the chosen folders.",
                               detail={"files": len(excluded)})
            missing = [m for m in unseen if int(m["id"]) not in outside]
            fresh = [m for m in missing if m.get("status") != "missing"]
            if fresh:
                total = max(1, len(manifest))
                if RootGuard().check_bulk(len(fresh), total):
                    for m in fresh:
                        db.update_file(int(m["id"]), status="missing", status_reason="pending_removal",
                                       missing_since=now)
                    self._set_confirmation({"kind": "mass_delete", "source": SOURCE,
                                            "missing": len(fresh), "total": len(manifest)})
                    self._activity(
                        "held", f"{len(fresh)} of {len(manifest)} Drive files disappeared at once. They are "
                        "hidden from search until you confirm or reject removing them.",
                        level="warning", detail={"missing": len(fresh), "total": len(manifest)},
                    )
                    held = True
                else:
                    self._purge_rows(db, [int(m["id"]) for m in fresh])
                    self._activity("removed", f"Removed {len(fresh)} files that are gone from Google Drive.",
                                   detail={"files": len(fresh)})
            expired = [
                int(m["id"]) for m in missing
                if m.get("status") == "missing" and m.get("status_reason") == "kept"
                and m.get("missing_since") and now - float(m["missing_since"]) > MISSING_KEEP_S
            ]
            self._purge_rows(db, expired)
        if stale:
            if held:
                self._folders.update(stale)
            else:
                self.rt.queue_vector_deletes(db.delete_folders([int(r["id"]) for r in stale.values()]))
        db.refresh_folder_stats(SOURCE)
        end = _now()
        db.update_source_state(SOURCE, drive_cursor=str(token), drive_cursor_at=end,
                               last_scan_finished_at=end, last_scan_s=time.monotonic() - mono)
        with self._lock:
            self._full_requested = False
            self._last_full_at = self._last_sync_at = end
            self._next_full_at = end + self._reconcile_s
            self._unavailable.clear()
        self._phase(None)
        if queued:
            self._note_queued(queued, "initial_sync" if first else "sync")
        logger.info(
            f"[userdocs] {self.profile}: Drive reconcile ({why}) listed={len(files)} folders={len(folders)} "
            f"queued={queued} complete={bool(listing.complete)} in {time.monotonic() - mono:.1f}s"
        )

    # ── change feed ────────────────────────────────────────────────────────

    def _changes(self, client: Any, stop: threading.Event, db: Any, cursor: str) -> None:
        from app.userdocs.runtime import P_LIVE

        self._load_folders(db)
        queued = removed = 0
        applied = False
        for page in client.changes(cursor):
            self._check_abort(stop)
            latest: dict[str, dict[str, Any]] = {}
            for ch in page.changes or []:
                fid = ch.get("fileId") or (ch.get("file") or {}).get("id")
                if fid:
                    # The last change of a file in the page is its state now.
                    latest.pop(str(fid), None)
                    latest[str(fid)] = ch
            folder_changes = {fid: ch for fid, ch in latest.items() if self._is_folder_change(fid, ch)}
            new_roots: list[str] = []
            for fid in _parents_first(folder_changes):
                removed += self._apply_folder_change(db, fid, folder_changes[fid], new_roots)
            for fid, ch in latest.items():
                if fid in folder_changes:
                    continue
                result = self._apply_file_change(db, fid, ch, priority=P_LIVE)
                if result == "queued":
                    queued += 1
                elif result == "removed":
                    removed += 1
            if new_roots and self._include:
                # A folder moved into scope brings files the feed never
                # mentions (they did not change): list its tree.
                queued += self._list_subtrees(client, stop, db, new_roots, priority=P_LIVE)
            applied = applied or bool(latest)
            now = _now()
            db.update_source_state(SOURCE, drive_cursor=str(page.resume_token), drive_cursor_at=now)
            with self._lock:
                self._last_sync_at = now
            if page.drive_removals:
                with self._lock:
                    self._full_requested = True
                self._activity("resync", "A shared drive is no longer available; re-listing Drive.",
                               level="warning", detail={"drives": list(page.drive_removals)})
            if page.last:
                break
        if applied:
            db.refresh_folder_stats(SOURCE)
        if removed:
            self._activity("removed", f"Removed {removed} files deleted from Google Drive or moved out of "
                           "the indexed folders.", detail={"files": removed})
        if queued:
            self._note_queued(queued, "changes")

    def _is_folder_change(self, fid: str, ch: dict[str, Any]) -> bool:
        f = ch.get("file")
        if f:
            return f.get("mimeType") == MIME_FOLDER
        return fid in self._folders

    def _apply_folder_change(self, db: Any, fid: str, ch: dict[str, Any], new_roots: list[str]) -> int:
        """A folder changed: new rows for folders entering scope, a rewritten
        subtree for a renamed or moved one, removal for one that left.
        Returns the files removed."""
        f = ch.get("file")
        known = self._folders.get(fid)
        gone = bool(ch.get("removed")) or not f or bool(f.get("trashed"))
        if gone or not self._in_scope(f):
            return self._drop_subtree(db, fid) if known else 0
        name = _clean_name(f.get("name"))
        parent = _first_parent(f)
        parent_row = (self._folders.get(parent) or {}).get("id") if parent else None
        rel = (self._prefix_for(parent) if parent else "Drive") + "/" + _segment(name)
        if known and (known["rel"], known["parent_row"], known["name"]) == (rel, parent_row, name):
            return 0
        row_id = db.upsert_folder(
            SOURCE, rel, drive_key("folder:" + fid), name=name, name_folded=fold(name), parent_id=parent_row,
            depth=rel.count("/"), drive_id=fid, status="live", updated_at=_now(),
        )
        self._folders[fid] = {"id": row_id, "rel": rel, "parent_row": parent_row, "name": name}
        self._scope_ids.add(fid)
        if known is None:
            new_roots.append(fid)
        elif known["rel"] != rel:
            self._rewrite_subtree(db, row_id, rel)
        return 0

    def _apply_file_change(self, db: Any, fid: str, ch: dict[str, Any], *, priority: int) -> str | None:
        f = ch.get("file")
        row = db.file_by_drive_id(fid)
        if ch.get("removed") or not f or f.get("trashed") or not self._in_scope(f):
            if row is not None:
                self._purge_rows(db, [int(row["id"])])
                return "removed"
            return None
        if f.get("mimeType") == MIME_FOLDER:
            return None
        return "queued" if self._apply_file(db, f, row, priority=priority) else None

    def _list_subtrees(self, client: Any, stop: threading.Event, db: Any, roots: list[str], *,
                       priority: int) -> int:
        listing = client.list_folders_tree(roots, max_files=LIST_MAX_FILES)
        self._check_abort(stop)
        items = [f for f in (listing.files or []) if isinstance(f, dict) and f.get("id") and not f.get("trashed")]
        folders = {str(f["id"]): f for f in items if f.get("mimeType") == MIME_FOLDER}
        self._apply_folders(db, folders, use_rows=True)
        self._scope_ids |= set(folders)
        queued = 0
        metas = [f for f in items if f.get("mimeType") != MIME_FOLDER]
        rows = db.files_by_drive_ids([str(f["id"]) for f in metas])
        for meta in metas:
            if self._apply_file(db, meta, rows.get(str(meta["id"])), priority=priority):
                queued += 1
        return queued

    # ── rows and folders ───────────────────────────────────────────────────

    def _in_scope(self, meta: dict[str, Any]) -> bool:
        """Every change the feed reports is reachable; with include_folders
        only what sits under a known in-scope folder counts."""
        if not self._include:
            return True
        if str(meta.get("id") or "") in self._include:
            return True
        return any(str(p) in self._scope_ids for p in meta.get("parents") or [])

    def _listed_fields(self, meta: dict[str, Any]) -> dict[str, Any]:
        name = _clean_name(meta.get("name"))
        ext = os.path.splitext(name)[1].lower()
        if not ext:
            fe = str(meta.get("fullFileExtension") or meta.get("fileExtension") or "").strip(".").lower()
            ext = f".{fe}" if fe else ""
        fields: dict[str, Any] = {
            "name": name, "name_folded": fold(name), "ext": ext,
            "mtime": _ts(meta.get("modifiedTime")), "birthtime": _ts(meta.get("createdTime")),
            **self._drive_fields(meta),
        }
        size = _int(meta.get("size"))
        if size is not None:
            fields["size"] = size
        return fields

    @staticmethod
    def _drive_fields(meta: dict[str, Any]) -> dict[str, Any]:
        """The Drive columns (and the dates they give) of a file resource."""
        return {
            "drive_file_id": str(meta.get("id") or ""),
            "drive_md5": meta.get("md5Checksum") or None,
            "drive_version": _version(meta),
            "drive_modified": meta.get("modifiedTime") or None,
            "drive_created": meta.get("createdTime") or None,
            "drive_modified_by_me_at": _ts(meta.get("modifiedByMeTime")),
            "drive_mime": meta.get("mimeType") or None,
            "drive_web_link": meta.get("webViewLink") or None,
            "mtime": _ts(meta.get("modifiedTime")),
            "birthtime": _ts(meta.get("createdTime")),
        }

    @staticmethod
    def _content_changed(row: dict[str, Any], meta: dict[str, Any]) -> bool:
        """Binary: md5 or size moved. Google-native: modifiedTime or version
        moved (the export's sha256 decides later whether it really did)."""
        mime = meta.get("mimeType") or ""
        if row.get("drive_mime") and row.get("drive_mime") != mime:
            return True
        if _is_native(mime):
            return (row.get("drive_modified") != (meta.get("modifiedTime") or None)
                    or (row.get("drive_version") or None) != _version(meta))
        md5 = meta.get("md5Checksum") or None
        if (row.get("drive_md5") or None) != md5:
            return True
        size = _int(meta.get("size"))
        if size is not None and _int(row.get("size")) != size:
            return True
        # No checksum at all (rare): the modification time is all there is.
        return md5 is None and row.get("drive_modified") != (meta.get("modifiedTime") or None)

    def _placement(self, meta: dict[str, Any]) -> tuple[str, int | None]:
        """(display path, folder row id) of a file from its first parent."""
        name = _segment(_clean_name(meta.get("name")))
        parent = _first_parent(meta)
        if not parent:
            return "Drive/" + name, None
        row = self._folders.get(parent)
        if row is not None:
            return f"{row['rel']}/{name}", int(row["id"])
        return f"{self._prefix_for(parent)}/{name}", None

    def _apply_file(self, db: Any, meta: dict[str, Any], row: dict[str, Any] | None, *, priority: int) -> bool:
        """Bring one listed file's row in line. True when it was queued for
        (re)indexing; a rename or touch only rewrites the row and its card."""
        from app.userdocs.runtime import P_UPGRADE

        fid = str(meta["id"])
        fields = self._listed_fields(meta)
        rel, folder_row = self._placement(meta)
        fields["folder_id"] = folder_row
        if row is None:
            try:
                db.insert_file(SOURCE, rel, drive_key(fid), status="dirty", priority=priority, **fields)
                return True
            except Exception as exc:  # noqa: BLE001 — a racing sync inserted it first
                logger.debug(f"[userdocs] Drive insert {fid} skipped: {exc}")
                row = db.file_by_drive_id(fid)
                if row is None:
                    return False
        rid = int(row["id"])
        content = self._content_changed(row, meta)
        moved = (row.get("rel_path"), row.get("name"), row.get("folder_id")) != (rel, fields["name"], folder_row)
        touched = moved or content or any(
            row.get(k) != v for k, v in fields.items() if k in row and k not in ("name_folded", "ext")
        )
        if touched:
            db.update_file(rid, rel_path=rel, **fields)
        status = row.get("status")
        if status == "missing":
            db.update_file(rid, missing_since=None, status_reason=None)
            db.mark_dirty([rid], priority=P_UPGRADE)
            return True
        if content:
            db.mark_dirty([rid], priority=priority)
            return True
        if moved or row.get("mtime") != fields.get("mtime"):
            if status == "indexed":
                self._recard(db, rid)
            elif status == "metadata_only":
                # No content to keep, and its card depends on details the
                # pipeline reads anyway (a shortcut's target): cheap to redo.
                db.mark_dirty([rid], priority=P_UPGRADE)
                return True
        return False

    def _load_folders(self, db: Any, *, force: bool = False) -> None:
        if self._folders_loaded and not force:
            return
        folders: dict[str, dict[str, Any]] = {}
        after = 0
        while True:
            rows = db.list_folders(source=SOURCE, after_id=after, limit=2000)
            for r in rows:
                if r.get("drive_id"):
                    folders[str(r["drive_id"])] = {
                        "id": int(r["id"]), "rel": r["rel_path"], "parent_row": r.get("parent_id"),
                        "name": r.get("name") or "",
                    }
            if len(rows) < 2000:
                break
            after = int(rows[-1]["id"])
        self._folders = folders
        self._folders_loaded = True
        self._scope_ids = set(self._include) | set(folders)

    def _apply_folders(self, db: Any, folders: dict[str, dict[str, Any]], *, use_rows: bool) -> None:
        """Rows for the listed folders, parents first. Paths come from the
        listing; a parent outside it is named as far as Drive shows it."""
        self._listed = {fid: {"name": _clean_name(m.get("name")), "parent": _first_parent(m)}
                        for fid, m in folders.items()}
        rels: dict[str, str] = {}
        for fid in folders:
            chain: list[str] = []
            cur: str | None = fid
            while cur in self._listed and cur not in rels and cur not in chain:
                chain.append(cur)
                cur = self._listed[cur]["parent"]
            if cur in rels:
                base = rels[cur]
            elif cur is None or cur in chain:
                base = "Drive"
            else:
                base = self._prefix_for(cur, use_rows=use_rows)
            for c in reversed(chain):
                base = f"{base}/{_segment(self._listed[c]['name'])}"
                rels[c] = base
        now = _now()
        for fid in sorted(folders, key=lambda f: rels[f].count("/")):
            info = self._listed[fid]
            rel, name, parent = rels[fid], info["name"], info["parent"]
            parent_row = (self._folders.get(parent) or {}).get("id") if parent else None
            known = self._folders.get(fid)
            if known and (known["rel"], known["parent_row"], known["name"]) == (rel, parent_row, name):
                continue
            row_id = db.upsert_folder(
                SOURCE, rel, drive_key("folder:" + fid), name=name, name_folded=fold(name),
                parent_id=parent_row, depth=rel.count("/"), drive_id=fid, status="live", updated_at=now,
            )
            self._folders[fid] = {"id": row_id, "rel": rel, "parent_row": parent_row, "name": name}
        self._listed = {}

    def _prefix_for(self, parent: str | None, *, use_rows: bool = True) -> str:
        """The display path of folder ``parent``: an in-scope folder row's
        path, or the names Drive shows going up. An ancestor Cremind cannot
        see (common under per-file access) ends the chain."""
        names: list[str] = []
        seen: set[str] = set()
        cur = parent
        while cur and cur not in seen and len(names) < MAX_DEPTH:
            seen.add(cur)
            row = self._folders.get(cur) if use_rows else None
            if row is not None:
                return row["rel"] + "".join("/" + n for n in reversed(names))
            info = self._folder_info(cur)
            if info is None:
                break
            names.append(_segment(info["name"]))
            cur = info["parent"]
        return drive_rel_path(list(reversed(names)))

    def _folder_info(self, fid: str) -> dict[str, Any] | None:
        if fid in self._listed:
            return self._listed[fid]
        if fid in self._ancestors:
            return self._ancestors[fid]
        client = self._sync_client
        info: dict[str, Any] | None = None
        if client is not None:
            try:
                meta = client.get(fid)
            except Exception as exc:  # noqa: BLE001
                if _drive_kind(exc) not in ("not_found", "not_authorized", "http"):
                    raise
            else:
                if meta and not meta.get("trashed"):
                    info = {"name": _clean_name(meta.get("name")), "parent": _first_parent(meta)}
        if len(self._ancestors) >= ANCESTOR_CACHE_MAX:
            self._ancestors.clear()
        self._ancestors[fid] = info
        return info

    def _rewrite_subtree(self, db: Any, folder_row: int, rel: str) -> None:
        """A folder was renamed or moved: every folder and file under it gets
        its new display path, and indexed files a new card — no re-read."""
        from app.userdocs.runtime import P_UPGRADE

        by_row = {int(v["id"]): k for k, v in self._folders.items()}
        queue: list[tuple[int, str]] = [(folder_row, rel)]
        seen = {folder_row}
        while queue:
            cur, crel = queue.pop()
            for r in db.read_sql(
                "SELECT id, name, drive_id, rel_path FROM folders WHERE source = ? AND parent_id = ?", (SOURCE, cur),
            ):
                if int(r["id"]) in seen or not r.get("drive_id"):
                    continue
                seen.add(int(r["id"]))
                new = f"{crel}/{_segment(r.get('name'))}"
                if new != r.get("rel_path"):
                    db.upsert_folder(SOURCE, new, drive_key("folder:" + str(r["drive_id"])), depth=new.count("/"))
                    key = by_row.get(int(r["id"]))
                    if key in self._folders:
                        self._folders[key]["rel"] = new
                queue.append((int(r["id"]), new))
            for f in db.read_sql("SELECT * FROM files WHERE source = ? AND folder_id = ?", (SOURCE, cur),
                                 table="files"):
                new = f"{crel}/{_segment(f.get('name'))}"
                if new == f.get("rel_path"):
                    continue
                db.update_file(int(f["id"]), rel_path=new)
                if f.get("status") == "indexed":
                    self._recard(db, int(f["id"]))
                elif f.get("status") == "metadata_only":
                    db.mark_dirty([int(f["id"])], priority=P_UPGRADE)

    def _drop_subtree(self, db: Any, fid: str) -> int:
        """A folder left scope (or the Drive): its folder rows and the files
        in them leave the index. Returns the files removed."""
        root = self._folders.get(fid)
        if root is None:
            return 0
        ids = [int(root["id"])]
        queue = list(ids)
        while queue:
            cur = queue.pop()
            for r in db.read_sql("SELECT id FROM folders WHERE source = ? AND parent_id = ?", (SOURCE, cur)):
                if int(r["id"]) not in ids:
                    ids.append(int(r["id"]))
                    queue.append(int(r["id"]))
        files: list[int] = []
        for i in range(0, len(ids), 400):
            part = ids[i:i + 400]
            files += [int(r["id"]) for r in db.read_sql(
                f"SELECT id FROM files WHERE source = ? AND folder_id IN ({', '.join('?' * len(part))})",
                (SOURCE, *part),
            )]
        self._purge_rows(db, files)
        self.rt.queue_vector_deletes(db.delete_folders(ids))
        gone = set(ids)
        for key in [k for k, v in self._folders.items() if int(v["id"]) in gone]:
            self._folders.pop(key, None)
            self._scope_ids.discard(key)
        return len(files)

    def _recard(self, db: Any, fid: int) -> bool:
        """Rewrite only the file card of an indexed row (a rename, a move, a
        touched date): body chunks and their vectors stay."""
        from app.userdocs.chunking import diff_chunks, make_file_card
        from app.userdocs.runtime import chunks_from_rows, iso_local

        row = db.get_file(fid)
        if row is None:
            return False
        existing = db.get_chunks(fid)
        rows = db.chunk_rows([c.id for c in existing])
        body = chunks_from_rows(r for r in rows if r.get("ctype") != t.CTYPE_FILE_CARD)
        doc_meta = row.get("doc_meta") or {}
        exif = row.get("exif") or {}
        camera = " ".join(x for x in (exif.get("make"), exif.get("model")) if x) or None
        card = make_file_card(
            name=row.get("name") or "", rel_path=row.get("rel_path") or "", kind=row.get("kind") or t.KIND_OTHER,
            size=int(row.get("size") or 0), mtime_iso=iso_local(row.get("mtime")) or "",
            title=doc_meta.get("title"), author=doc_meta.get("author"),
            created_iso=iso_local(row.get("doc_created_at")), taken_iso=iso_local(row.get("taken_at")),
            camera=camera, summary_text=body[0].text if body else None,
            extra={"source": CARD_SOURCE},
        )
        diff = diff_chunks(existing, [card] + body)
        if not diff.add and not diff.remove:
            return False
        try:
            db.apply_chunks(file_id=fid, folder_id=row.get("folder_id"), source=SOURCE, diff=diff)
        except LookupError:
            return False
        self.rt.queue_vector_deletes(diff.remove)
        self._wake_embedder()
        return True

    def _purge_rows(self, db: Any, ids: Iterable[int]) -> int:
        n = 0
        for fid in list(ids):
            try:
                self.rt.queue_vector_deletes(db.delete_file(int(fid)))
                n += 1
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[userdocs] Drive purge {fid} failed: {exc}")
        return n

    # ── the per-file pipeline ──────────────────────────────────────────────

    def process(self, row: dict[str, Any]) -> tuple[str, str, str | None]:
        """Index one dirty Drive row (pipeline worker thread). Same return
        contract as the local pipeline: (event, activity text, error reason)."""
        fid = int(row["id"])
        rel = row.get("rel_path") or ""
        name = row.get("name") or rel.rsplit("/", 1)[-1]
        if not self.work_allowed():
            return "skipped", f"{name}: Google Drive sync is paused or on hold", None
        db = self._db()
        file_id = row.get("drive_file_id")
        if not file_id:
            self._purge_rows(db, [fid])
            return "removed", f"{name} had no Drive id", None
        client, stop = self._get_client()
        progress = getattr(self.rt, "progress", None)

        try:
            meta = client.get(str(file_id))
        except Exception as exc:  # noqa: BLE001
            if _drive_kind(exc) in ("not_found", "not_authorized") and not stop.is_set():
                return self._gone(db, row, name)
            return self._fail(db, row, name, exc, stop)
        if meta.get("trashed") or meta.get("mimeType") == MIME_FOLDER:
            self._purge_rows(db, [fid])
            return "removed", f"{name} was removed from Google Drive", None

        mime = meta.get("mimeType") or ""
        cap = min(int(self.rt.service.max_file_bytes()), IN_MEMORY_CAP)
        size = _int(meta.get("size"))
        if mime == MIME_SHORTCUT:
            # A card that names its target; the target is indexed only when
            # it is in scope itself.
            target = (meta.get("shortcutDetails") or {}).get("targetId")
            try:
                target_name = self._target_name(client, target)
            except Exception as exc:  # noqa: BLE001
                return self._fail(db, row, name, exc, stop)
            return self._metadata_only(row, meta, kind=t.KIND_OTHER, reason="shortcut",
                                       extra={"target": target_name or target})
        by_type = None if _is_native(mime) else _type_only_kind(meta.get("name") or name, mime)
        if by_type:
            return self._metadata_only(row, meta, kind=by_type, reason=by_type)
        guess = self._guess_kind(meta, name)
        if size is not None and size > cap:
            return self._metadata_only(row, meta, kind=guess, reason="too_large")
        if (meta.get("capabilities") or {}).get("canDownload") is False:
            return self._metadata_only(row, meta, kind=guess, reason="not_downloadable")
        deferred = self._governor_defers(db, row, size or 0)
        if deferred:
            return "skipped", f"{name}: waiting for storage space", "deferred"

        if progress is not None:
            progress.file_stage(fid, "download")
        try:
            content = client.fetch_content(meta, max_bytes=cap)
        except Exception as exc:  # noqa: BLE001
            kind = _drive_kind(exc)
            if kind == "not_downloadable":
                return self._metadata_only(row, meta, kind=guess, reason="not_downloadable")
            if kind in ("too_large", "export_too_large"):
                return self._metadata_only(row, meta, kind=guess, reason="too_large")
            if kind in ("not_found", "not_authorized") and not stop.is_set():
                return self._confirm_gone(db, row, name, client, stop, exc)
            return self._fail(db, row, name, exc, stop)
        if content is None:
            return self._metadata_only(row, meta, kind=_NATIVE_KINDS.get(mime, t.KIND_OTHER) if _is_native(mime)
                                       else guess, reason="google_type" if _is_native(mime) else "no_content")

        data = content.data if isinstance(content.data, bytes) else bytes(content.data or b"")
        export = getattr(content, "export_mime", None)
        req_name = self._request_name(meta, name, content)
        kind = getattr(content, "kind", None)
        found_mime = export
        if not kind:
            kind, found_mime = _detect(data, req_name)
        sha = hashlib.sha256(data).hexdigest()
        length = len(data) if (export or size is None) else size
        fields = self._drive_fields(meta)
        if self._same_content(row, sha, kind):
            return self._unchanged(db, row, name, fields, length)

        common = dict(
            row=row, name=name, rel=rel, kind=kind, mime=found_mime or mime or None, sha=sha, size=length,
            extra={"source": CARD_SOURCE}, source=SOURCE, drive_fields=fields,
        )
        drive_exif = _meta_exif(meta)
        if kind in t.METADATA_ONLY_KINDS:
            return self.rt.index_content(
                **common, result=None, status="metadata_only", reason=kind, error=None, image=None,
                exif=drive_exif or None, image_bytes=None, taken_ts=_iso_ts(drive_exif.get("taken_at")),
                created_ts=None,
            )
        limits = dict(self.rt.service.extract_limits() or {})
        if export and kind == t.KIND_MARKDOWN:
            # Citations point into a converted document the user cannot open
            # as Markdown: locate by heading, and drop the export's escapes and
            # inlined images.
            limits["md_locators"] = "structure"
            limits["md_export"] = True
        if progress is not None:
            progress.file_stage(fid, "extract")
        result = self.rt.service.extract(
            t.ExtractRequest(name=req_name, kind=kind, data=data, limits=limits), size=len(data),
        )
        status, reason, error = _outcome(result)
        exif = _merge_exif(result.exif, drive_exif)
        doc_meta = result.doc_meta or {}
        return self.rt.index_content(
            **common, result=result, status=status, reason=reason, error=error, image=result.image,
            exif=exif or None, image_bytes=data if kind == t.KIND_IMAGE else None,
            taken_ts=_iso_ts(exif.get("taken_at")), created_ts=_iso_ts(doc_meta.get("created")),
        )

    def _metadata_only(self, row: dict[str, Any], meta: dict[str, Any], *, kind: str, reason: str,
                       extra: dict[str, Any] | None = None) -> tuple[str, str, str | None]:
        """Index a file by its name and details only — decided before any
        download (by type, size, a shortcut) or because Drive refuses one."""
        exif = _meta_exif(meta)
        name = row.get("name") or (row.get("rel_path") or "").rsplit("/", 1)[-1]
        size = _int(meta.get("size"))
        return self.rt.index_content(
            row=row, name=name, rel=row.get("rel_path") or "", kind=kind, mime=meta.get("mimeType") or None,
            sha=None, size=size if size is not None else int(row.get("size") or 0), result=None,
            status="metadata_only", reason=reason, error=None, image=None, exif=exif or None,
            image_bytes=None, taken_ts=_iso_ts(exif.get("taken_at")), created_ts=None,
            extra={"source": CARD_SOURCE, "status": METADATA_ONLY_NOTE, **(extra or {})},
            source=SOURCE, drive_fields=self._drive_fields(meta),
        )

    def _same_content(self, row: dict[str, Any], sha: str, kind: str) -> bool:
        """The bytes are the ones indexed last time, that indexing succeeded
        with today's extractor and chunker, and nothing (a caption) waits on
        them: only the card may need refreshing."""
        from app.userdocs.chunking import CHUNKER_VERSION
        from app.userdocs.extract import EXTRACTOR_VERSION

        reason = row.get("status_reason")
        return bool(
            sha and row.get("sha256") == sha
            and row.get("extractor_version") == EXTRACTOR_VERSION and row.get("chunker_version") == CHUNKER_VERSION
            and not row.get("error") and row.get("indexed_at") and int(row.get("chunk_count") or 0) > 0
            and (reason is None or str(reason).startswith("partial:"))
            and (kind != t.KIND_IMAGE or row.get("caption_state") == "done")
        )

    def _unchanged(self, db: Any, row: dict[str, Any], name: str, fields: dict[str, Any],
                   size: int) -> tuple[str, str, str | None]:
        fid = int(row["id"])
        db.update_file(fid, size=size, **fields)
        recarded = self._recard(db, fid)
        ok = db.update_file(fid, if_queued_at=row.get("queued_at"), status="indexed", error=None, attempts=0,
                            next_attempt_at=None, indexed_at=_now())
        if not ok:
            return "skipped", f"{name} changed while it was being indexed", None
        if recarded:
            return "moved", f"{name}: location or details updated", None
        return "unchanged", f"{name} is unchanged", None

    def _request_name(self, meta: dict[str, Any], name: str, content: Any) -> str:
        """The name the extractor sees: an export carries its format's
        extension ("Report.docx" exported as Markdown is "Report.docx.md");
        a binary without one gets Drive's extension or the mime's."""
        if getattr(content, "export_mime", None):
            return (name or "file") + (getattr(content, "ext", "") or "")
        if os.path.splitext(name)[1]:
            return name
        fe = str(meta.get("fullFileExtension") or meta.get("fileExtension") or "").strip(".")
        if fe:
            return f"{name}.{fe}"
        guess = mimetypes.guess_extension(meta.get("mimeType") or "") if meta.get("mimeType") else None
        return name + guess if guess else name

    @staticmethod
    def _guess_kind(meta: dict[str, Any], name: str) -> str:
        from app.userdocs.kinds import guess_kind

        mime = meta.get("mimeType") or ""
        if _is_native(mime):
            return _NATIVE_KINDS.get(mime, t.KIND_OTHER)
        return guess_kind(meta.get("name") or name)

    def _target_name(self, client: Any, target: Any) -> str | None:
        if not target:
            return None
        try:
            meta = client.get(str(target))
        except Exception as exc:  # noqa: BLE001 — an unreachable target leaves the card without it
            if _drive_kind(exc) in SOURCE_KINDS:
                raise
            return None
        return _clean_name(meta.get("name")) if meta and not meta.get("trashed") else None

    def _governor_defers(self, db: Any, row: dict[str, Any], size: int) -> bool:
        from app.userdocs import governor as gov

        op = "add_content" if not row.get("sha256") else gov.classify_edit(int(size) - int(row.get("size") or 0))
        try:
            level = gov.Level(getattr(self.rt, "level", "ok") or "ok")
        except ValueError:
            level = gov.Level.OK
        if gov.Governor.allows(level, op):
            return False
        db.update_file(int(row["id"]), if_queued_at=row.get("queued_at"), status="deferred",
                       status_reason=str(level.value))
        return True

    # ── per-row errors ─────────────────────────────────────────────────────

    def _fail(self, db: Any, row: dict[str, Any], name: str, exc: BaseException,
              stop: threading.Event) -> tuple[str, str, str | None]:
        kind = _drive_kind(exc)
        if kind is None:
            raise exc
        if stop.is_set() or self._closed.is_set():
            return "skipped", f"{name}: Drive sync stopped", None
        if kind in SOURCE_KINDS:
            self._source_error(exc, worker=True)
            with self._lock:
                reason = (self._hold or {}).get("reason") or kind
            return "skipped", f"{name}: waiting for Google Drive", reason
        return self._row_failed(db, row, name, kind, str(exc), transient=kind in ("rate_limited", "file_unavailable"))

    def _row_failed(self, db: Any, row: dict[str, Any], name: str, reason: str, error: str, *,
                    transient: bool) -> tuple[str, str, str | None]:
        from app.userdocs.runtime import BACKOFF_S, MAX_ATTEMPTS

        attempts = int(row.get("attempts") or 0) + 1
        delay = BACKOFF_S[min(attempts, len(BACKOFF_S)) - 1]
        # A throttled or flaky Drive is retried for as long as it takes; a
        # file that keeps failing otherwise gives up like a local one.
        nxt = _now() + delay if (transient or attempts < MAX_ATTEMPTS) else None
        db.update_file(int(row["id"]), if_queued_at=row.get("queued_at"), status="error", status_reason=reason,
                       error=(error or "")[:2000], attempts=attempts, next_attempt_at=nxt)
        return "failed", f"{name}: {_ROW_MESSAGES.get(reason, reason)}", reason

    def _confirm_gone(self, db: Any, row: dict[str, Any], name: str, client: Any, stop: threading.Event,
                      first: BaseException) -> tuple[str, str, str | None]:
        """The content endpoint said "not found": only ``files.get`` saying
        the same makes it gone."""
        try:
            meta = client.get(str(row.get("drive_file_id")))
        except Exception as exc:  # noqa: BLE001
            if _drive_kind(exc) in ("not_found", "not_authorized"):
                return self._gone(db, row, name)
            return self._fail(db, row, name, exc, stop)
        if meta.get("trashed"):
            self._purge_rows(db, [int(row["id"])])
            return "removed", f"{name} was removed from Google Drive", None
        return self._row_failed(db, row, name, "file_unavailable", str(first), transient=True)

    def _gone(self, db: Any, row: dict[str, Any], name: str) -> tuple[str, str, str | None]:
        """Drive confirmed the file is gone (or no longer shared with
        Cremind). Many at once is an account-wide problem, not a deletion."""
        now = _now()
        with self._lock:
            while self._unavailable and now - self._unavailable[0] > UNAVAILABLE_WINDOW_S:
                self._unavailable.popleft()
            too_many = len(self._unavailable) >= UNAVAILABLE_LIMIT
            if not too_many:
                self._unavailable.append(now)
        if too_many:
            self._enter_hold("drive_unreachable", {
                "why": "many_unavailable", "message": "many files became unavailable",
            })
            return "skipped", f"{name}: held — many Drive files became unavailable at once", "many_unavailable"
        self._purge_rows(db, [int(row["id"])])
        return "removed", f"{name} is no longer available in Google Drive", None

    # ── holds ──────────────────────────────────────────────────────────────

    def _source_error(self, exc: BaseException, *, worker: bool) -> None:
        """Turn a client error that concerns the whole source into a hold (or
        a later retry)."""
        kind = _drive_kind(exc)
        message = str(exc)[:300]
        if kind == "auth_revoked":
            self._enter_hold("auth_revoked", {"message": message})
        elif kind == "unlinked":
            # Possibly a read mid-swap: never acted on until re-confirmed.
            if worker:
                with self._lock:
                    self._token_check = True
                self.request_sync("token_check")
            else:
                client = self._sync_client
                if client is not None and self._identity(client) is not None:
                    self._retry_later(HOLD_RETRY_S[0])
        elif kind in ("auth_failed", "auth_misconfigured"):
            self._enter_hold("drive_misconfigured", {"kind": kind, "message": message})
        elif kind in ("unreachable", "account_forbidden"):
            self._enter_hold("drive_unreachable", {"kind": kind, "message": message})
        elif kind in ("not_found", "not_authorized") or getattr(exc, "reason", None) == "invalid_id":
            # At source level: a chosen folder is gone, no longer shared, or
            # not a Drive id at all — only new settings can fix that.
            self._enter_hold("drive_misconfigured", {"why": "folder_unavailable", "message": message})
        elif kind == "rate_limited":
            logger.info(f"[userdocs] {self.profile}: Drive is rate-limiting; retrying later")
            self._retry_later(HOLD_RETRY_S[1])
        else:
            logger.warning(f"[userdocs] {self.profile}: Drive sync error ({kind}): {message}")
            self._activity("error", f"Google Drive sync failed: {message}", level="error")
            self._retry_later(HOLD_RETRY_S[1])

    def _enter_hold(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        """Hold the Drive source. A purge timer starts on the *first*
        confirmation of a revoked or unlinked account and never restarts
        while the hold lasts; an outage seen during such a hold cannot
        prove anything and leaves it as it is."""
        now = _now()
        with self._lock:
            cur = dict(self._hold) if self._hold else None
        if cur and cur["reason"] in TIMER_REASONS and reason not in TIMER_REASONS:
            self._bump_retry()
            return
        db = self._db()
        st = db.get_source_state(SOURCE)
        held = st.get("state") == "hold"
        prev_detail = dict(st.get("detail") or {}) if held else {}
        purge_after: float | None = None
        purged_at = prev_detail.get("purged_at") if held and st.get("reason") in TIMER_REASONS else None
        if reason in TIMER_REASONS and not purged_at:
            prev = st.get("purge_after") if held and st.get("reason") in TIMER_REASONS else None
            if prev:
                purge_after = float(prev)
            elif sum(db.count_by_status(SOURCE).values()):
                purge_after = now + PURGE_GRACE_S
        since = float(st["hold_since"]) if held and st.get("hold_since") else now
        det = dict(detail or {})
        det["purge_at"] = purge_after * 1000 if purge_after else None
        if purged_at:
            det["purged_at"] = purged_at
        db.update_source_state(SOURCE, state="hold", reason=reason, detail=det, hold_since=since,
                               purge_after=purge_after)
        with self._lock:
            self._hold = {"reason": reason, "detail": det, "since": since, "purge_after": purge_after,
                          "entered": time.monotonic()}
        self._bump_retry()
        if not cur or cur["reason"] != reason:
            logger.warning(f"[userdocs] {self.profile}: Drive held ({reason})")
            self._activity("held", _HOLD_MESSAGES.get(reason, f"Google Drive is on hold ({reason})."),
                           level="warning", detail={"reason": reason, **det})
        self._publish()

    def _bump_retry(self) -> None:
        with self._lock:
            self._hold_attempts += 1
            delay = HOLD_RETRY_S[min(self._hold_attempts, len(HOLD_RETRY_S)) - 1]
            self._hold_retry_at = _now() + delay

    def _clear_hold(self, *, since: float) -> None:
        """Drive answered: the source is live again — unless a worker held it
        after this sync began (``since``, monotonic); that newer evidence
        stands."""
        with self._lock:
            cur = self._hold
            if cur and cur.get("entered", 0.0) > since:
                return
            self._hold = None
            self._hold_attempts, self._hold_retry_at = 0, 0.0
        db = self._db()
        if cur is None and db.get_source_state(SOURCE).get("state") == "live":
            return
        db.update_source_state(SOURCE, state="live", reason=None, detail=None, hold_since=None, purge_after=None)
        if cur is not None:
            logger.info(f"[userdocs] {self.profile}: Drive hold ({cur['reason']}) cleared")
            self._activity("resumed", "Google Drive is answering again; syncing resumed.")

    def _retry_later(self, delay: float) -> None:
        with self._lock:
            self._next_poll_at = _now() + delay

    def _purge_due(self, now: float) -> bool:
        with self._lock:
            h = self._hold
            return bool(h and h["reason"] in TIMER_REASONS and h.get("purge_after") and now >= float(h["purge_after"]))

    def _purge_for_hold(self) -> None:
        with self._lock:
            reason = (self._hold or {}).get("reason")
        n = self.purge_index(keep_hold=True)
        what = "access was revoked" if reason == "auth_revoked" else "the account was unlinked"
        logger.warning(f"[userdocs] {self.profile}: Drive index deleted ({n} files); {what} 7 days ago")
        self._activity("purged", f"Removed the Drive index ({n} files): Google {what} and was not restored "
                       "within 7 days.", level="warning", detail={"files": n, "reason": reason})

    # ── purge set D ────────────────────────────────────────────────────────

    def purge_index(self, *, keep_hold: bool = True) -> int:
        """Delete set D: every Drive file, chunk (vectors queued for
        deletion), folder, the Drive source state and the Drive citations —
        and reset what this object remembers. A hold being purged for stays,
        marked as purged, so the user still sees why. Returns files removed.

        Stops an in-flight sync first; safe from the sync itself."""
        inside = self._sync_owner == threading.get_ident()
        with self._lock:
            self._purging = True
        try:
            if not inside:
                self._abort_inflight()
            got = self._sync_lock.acquire(timeout=120.0)
            if not got:
                logger.warning(f"[userdocs] {self.profile}: Drive sync did not stop; purging anyway")
            try:
                db = self._db()
                n = sum(db.count_by_status(SOURCE).values())
                for ids in db.purge_source(SOURCE):
                    self.rt.queue_vector_deletes(ids)
                try:
                    _purge_citations(self.profile)
                except Exception:  # noqa: BLE001 — a stale token only reads as issued-but-removed
                    logger.exception(f"[userdocs] {self.profile}: could not delete the Drive citations")
                with self._lock:
                    hold = dict(self._hold) if (keep_hold and self._hold) else None
                self.reset()
                if hold:
                    det = dict(hold.get("detail") or {})
                    det.update(purge_at=None, purged_at=_now() * 1000)
                    db.update_source_state(SOURCE, state="hold", reason=hold["reason"], detail=det,
                                           hold_since=hold.get("since") or _now(), purge_after=None)
                    with self._lock:
                        self._hold = {**hold, "detail": det, "purge_after": None}
                return n
            finally:
                if got:
                    self._sync_lock.release()
        finally:
            with self._lock:
                self._purging = False
            self.refresh_counts()
            self._publish()

    def reset(self) -> None:
        """Forget everything remembered about the Drive index (after its rows
        are gone): the next sync starts from a full listing."""
        with self._lock:
            self._hold = None
            self._hold_attempts, self._hold_retry_at = 0, 0.0
            self._confirmation = None
            self._account_key = None
            self._last_sync_at = self._last_full_at = None
            self._next_poll_at = self._next_full_at = 0.0
            self._full_requested = True
            self._counts = {}
            self._unavailable.clear()
        self._folders = {}
        self._folders_loaded = False
        self._scope_ids = set()
        self._ancestors = {}

    # ── confirmations ──────────────────────────────────────────────────────

    def confirm_deletions(self) -> int:
        db = self._db()
        ids = [int(r["id"]) for r in db.read_sql(
            "SELECT id FROM files WHERE source = ? AND status = 'missing'", (SOURCE,))]
        n = self._purge_rows(db, ids)
        self._set_confirmation(None)
        self._activity("removed", f"Removed {n} vanished Drive files from the index (confirmed).",
                       detail={"files": n})
        self.refresh_counts()
        self.request_sync("confirmed")
        return n

    def reject_deletions(self) -> int:
        db = self._db()
        ids = [int(r["id"]) for r in db.read_sql(
            "SELECT id FROM files WHERE source = ? AND status = 'missing' "
            "AND COALESCE(status_reason, '') != 'kept'", (SOURCE,))]
        for fid in ids:
            db.update_file(fid, status_reason="kept")
        self._set_confirmation(None)
        self._activity("kept", "Kept the vanished Drive files; they stay hidden and are re-checked on every "
                       "full Drive sync for 14 days.", detail={"files": len(ids)})
        self.request_sync("rejected")
        return len(ids)

    def _set_confirmation(self, confirmation: dict[str, Any] | None) -> None:
        with self._lock:
            self._confirmation = confirmation
        self._publish()

    # ── state for snapshot / API ───────────────────────────────────────────

    def refresh_counts(self) -> None:
        """Re-read the per-status Drive counts :meth:`view` reports (the
        snapshot itself never touches the index)."""
        db = getattr(self.rt, "db", None)
        if db is None or getattr(db, "closed", False):
            return
        try:
            counts = db.count_by_status(SOURCE)
        except Exception:  # noqa: BLE001
            return
        with self._lock:
            self._counts = dict(counts)

    def _effective(self) -> tuple[str, str | None]:
        if not self._enabled:
            if self._gate and self._row.get("enabled"):
                return "suspended", self._gate
            return "disabled", None
        if self._hold:
            return "hold", self._hold.get("reason")
        if self._confirmation:
            return "awaiting_confirmation", self._confirmation.get("kind")
        if self._suspended or self._token_check:
            return "suspended", "token_check"
        if self._paused():
            return "paused", "user"
        if self._syncing:
            return "syncing", None
        if int(self._counts.get("dirty", 0)) + int(self._counts.get("deferred", 0)):
            return "indexing", None
        return "idle", None

    def view(self) -> dict[str, Any]:
        with self._lock:
            state, reason = self._effective()
            c = self._counts
            return {
                "enabled": self._enabled,
                "state": state,
                "reason": reason,
                "detail": dict(self._hold.get("detail") or {}) if self._hold else None,
                "account_email": self._account_email,
                "identity_set": bool(self._account_key),
                "whole_drive": self._whole_drive,
                "include_folders": list(self._include),
                "last_sync_at": _ms(self._last_sync_at),
                "last_full_at": _ms(self._last_full_at),
                "counts": {
                    "indexed": int(c.get("indexed", 0)),
                    "pending": int(c.get("dirty", 0)) + int(c.get("deferred", 0)),
                    "error": int(c.get("error", 0)),
                    "metadata_only": int(c.get("metadata_only", 0)),
                },
                "confirmation": dict(self._confirmation) if self._confirmation else None,
            }

    # ── plumbing ───────────────────────────────────────────────────────────

    def _db(self) -> Any:
        return self.rt.ensure_db()

    def _paused(self) -> bool:
        return bool(getattr(self.rt, "paused_user", False))

    def _get_client(self) -> tuple[Any, threading.Event]:
        with self._lock:
            if self._client is None:
                self._client_stop = threading.Event()
                self._client = self._factory(self.profile, self._client_stop)
            return self._client, self._client_stop

    def _abort_inflight(self) -> None:
        """Stop whatever runs on the current client; the next call gets a new
        one. The old client is not closed here — a worker may still be
        inside it, and it fails its next wait instead."""
        with self._lock:
            self._client_stop.set()
            self._client = None

    def _check_abort(self, stop: threading.Event) -> None:
        if stop.is_set() or self._closed.is_set():
            raise _Aborted()

    def _wait(self, seconds: float) -> None:
        if seconds > 0 and self._closed.wait(seconds):
            raise _Aborted()

    def _activity(self, kind: str, message: str, *, level: str = "info", detail: Any = None) -> None:
        try:
            self._db().add_activity(kind, message, source=SOURCE, level=level, detail=detail)
        except Exception:  # noqa: BLE001 — the log is best-effort
            logger.debug(f"[userdocs] {self.profile}: Drive activity not recorded: {message}")

    def _phase(self, phase: str | None) -> None:
        try:
            self.rt.progress.set_source(SOURCE, phase=phase)
        except Exception:  # noqa: BLE001
            pass

    def _publish(self) -> None:
        try:
            with self._lock:
                state, reason = self._effective()
            self.rt.progress.set_source(SOURCE, state=state, reason=reason)
        except Exception:  # noqa: BLE001 — progress is best-effort
            pass

    def _note_queued(self, n: int, label: str) -> None:
        note = getattr(self.rt, "note_queued", None)
        if callable(note):
            try:
                note(n, label)
            except Exception:  # noqa: BLE001
                pass
        self._wake()

    def _wake(self) -> None:
        try:
            self.rt.service.wake()
        except Exception:  # noqa: BLE001
            pass

    def _wake_embedder(self) -> None:
        try:
            self.rt.service.wake_embedder()
        except Exception:  # noqa: BLE001
            pass


def _parents_first(changes: dict[str, dict[str, Any]]) -> list[str]:
    """Folder changes ordered so a folder comes after its parent when both
    changed in the same page (the child's scope depends on the parent's)."""
    order: list[str] = []
    placed: set[str] = set()
    for start in changes:
        chain: list[str] = []
        cur: str | None = start
        while cur in changes and cur not in placed and cur not in chain:
            chain.append(cur)
            cur = _first_parent((changes[cur].get("file") or {}))
        for fid in reversed(chain):
            placed.add(fid)
            order.append(fid)
    return order


def _outcome(result: Any) -> tuple[str, str | None, str | None]:
    """(status, reason, error) of an extraction, as the local pipeline reads it."""
    doc_meta = result.doc_meta or {}
    if result.status in (t.EXTRACT_OK, t.EXTRACT_PARTIAL):
        return "indexed", (f"partial:{result.reason}" if result.status == t.EXTRACT_PARTIAL else None), None
    if result.status == t.EXTRACT_METADATA_ONLY:
        return "metadata_only", result.reason, None
    if result.reason == "awaiting_extractor":
        return "awaiting_extractor", (doc_meta.get("missing") or "extractor"), None
    return "error", (result.reason or "corrupt"), doc_meta.get("error")


def _iso_ts(value: Any) -> float | None:
    from app.userdocs.runtime import ts_from_iso

    return ts_from_iso(value)


def _close_client(client: Any) -> None:
    if client is None:
        return
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass


__all__ = [
    "DriveSource",
    "drive_key",
    "drive_rel_path",
    "is_whole_drive",
]
