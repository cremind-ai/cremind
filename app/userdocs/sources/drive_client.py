"""A small Google Drive REST client for indexing — read-only, in memory.

It borrows the access token of the profile's gdrive skill link
(:func:`app.drive.skill_token.access_token`) and never writes the skill's
files: the skill owns its token and its own change cursor. Indexing keeps a
*separate* cursor in the profile's index file, so the two never fight over one.

What it needs that the skill-token helpers do not offer: listing without the
1,000-id cap (and saying when a listing is *not* complete), the change feed a
page at a time, content checksums and revisions for change detection, and
downloads/exports into memory with a size cap.

**Errors are classified, because the right reaction differs completely.** A
wrong kind is how an index gets deleted: "this file can't be downloaded" read
as "this file is gone" purges files the user can still open, and an account-wide
403 read per file queues every file for removal. So the kinds are narrow:

- ``auth_revoked``       — a real refresh got ``invalid_grant`` (or there is no
  refresh token). Drive results are hidden and, after a grace period, purged.
- ``auth_failed``        — Google said 401 again right after a successful forced
  refresh. Held visibly; never purged.
- ``auth_misconfigured`` — the token endpoint refused the OAuth client itself.
- ``unlinked``           — no token file. May be a read landing mid-swap, so the
  caller re-confirms before acting on it.
- ``unreachable``        — network, DNS, 5xx after retries, a non-JSON answer, or
  the client being stopped. Nothing is purged: an offline laptop keeps its index.
- ``account_forbidden``  — a 403 that is about the account or the OAuth project
  (daily limit, domain policy, API not enabled, missing scope), never one file.
- ``rate_limited``       — still throttled after backing off.
- ``bad_cursor``         — the change feed no longer accepts the page token.
- ``not_found`` / ``not_authorized`` — this one file is gone, or the app lost
  its ``drive.file`` grant to it. The only kinds that may lead to a purge.
- ``not_downloadable``   — the file exists but its bytes are off limits
  (``canDownload`` false, download restricted). Indexed metadata-only.
- ``file_unavailable``   — this file's content endpoint keeps failing while the
  API itself answers. Retried per file; the source is not held for it.
- ``too_large`` / ``export_too_large`` — over our cap, or Google's export cap.
- ``http``               — anything else (logged).
"""

from __future__ import annotations

import base64
import hashlib
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Iterator

import httpx

from app.google.registry import DRIVE_API_BASE as API
from app.utils.logger import logger

KINDS = frozenset({
    "auth_revoked", "auth_failed", "auth_misconfigured", "unlinked", "unreachable",
    "account_forbidden", "rate_limited", "bad_cursor", "not_found", "not_authorized",
    "not_downloadable", "file_unavailable", "too_large", "export_too_large", "http",
})
# The kinds a DriveTokenError can carry (app.drive.skill_token).
_TOKEN_KINDS = frozenset({"unlinked", "auth_revoked", "auth_misconfigured", "unreachable"})

FIELDS = (
    "id,name,mimeType,md5Checksum,sha256Checksum,size,modifiedTime,createdTime,modifiedByMeTime,"
    "version,headRevisionId,parents,trashed,webViewLink,ownedByMe,driveId,fileExtension,"
    "fullFileExtension,shortcutDetails(targetId,targetMimeType),capabilities(canDownload),"
    "imageMediaMetadata(time,cameraMake,cameraModel,location,width,height,rotation)"
)
_FOLDER_FIELDS = "id,name,parents,driveId,mimeType"

MIME_FOLDER = "application/vnd.google-apps.folder"
MIME_SHORTCUT = "application/vnd.google-apps.shortcut"
GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Google-native files have no bytes of their own; they are exported. Ordered
# preferences, first that works wins: (export mime, ext, KIND_*). Docs go as
# Markdown so headings survive; Slides through pptx so slide numbers survive as
# locators; Sheets fall back to CSV (first sheet only) when the workbook is over
# Google's export cap.
EXPORTS: dict[str, list[tuple[str, str, str]]] = {
    "application/vnd.google-apps.document": [
        ("text/markdown", ".md", "markdown"), ("text/plain", ".txt", "text"),
    ],
    "application/vnd.google-apps.spreadsheet": [
        (_XLSX, ".xlsx", "xlsx"), ("text/csv", ".csv", "csv"),
    ],
    "application/vnd.google-apps.presentation": [
        (_PPTX, ".pptx", "pptx"), ("text/plain", ".txt", "text"),
    ],
    "application/vnd.google-apps.drawing": [("image/png", ".png", "image")],
}
# Google-native types with nothing useful to export. Any other native type not
# in EXPORTS (shortcuts, videos in Photos, unknown future types) is treated the
# same way: its "download" is a 403 that must never read as lost access.
METADATA_ONLY_NATIVE = frozenset({
    "application/vnd.google-apps.form", "application/vnd.google-apps.site",
    "application/vnd.google-apps.map", "application/vnd.google-apps.jam",
    "application/vnd.google-apps.script", "application/vnd.google-apps.fusiontable",
    MIME_SHORTCUT, MIME_FOLDER,
})

WHOLE_DRIVE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
})

# Bytes held in memory for one file: the extractor gets them over a pipe, so a
# bigger file is indexed metadata-only rather than risk the worker's memory cap.
IN_MEMORY_CAP = 256 * 1024 * 1024

RETRY_AFTER_CAP_S = 120.0
_META_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
# Exports of big Docs and Sheets are rendered on Google's side before the first
# byte arrives; 30 s cut those off as "unreachable".
_CONTENT_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_PROBE_TIMEOUT = httpx.Timeout(15.0, connect=10.0)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_FOLDER_BATCH = 40  # "'a' in parents or 'b' in parents ..." per files.list query

_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"})
# 403s that describe the account or the OAuth project. Every file would fail
# the same way, so they hold the source instead of touching any file.
_ACCOUNT_REASONS = frozenset({
    "dailyLimitExceeded", "dailyLimitExceededUnreg", "domainPolicy", "accessNotConfigured",
    "SERVICE_DISABLED", "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
})


class DriveError(Exception):
    """A classified Drive failure. ``kind`` is one of :data:`KINDS`; ``reason``
    is Google's own reason string when it gave one (``stopped`` when the
    client's stop event ended a wait)."""

    def __init__(self, kind: str, message: str = "", *, status: int | None = None,
                 reason: str | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.status = status
        self.reason = reason


@dataclass
class Listing:
    """What a listing saw. ``complete`` is False when it was truncated at
    ``max_files``, Google reported ``incompleteSearch``, or an include folder
    could not be read; a reconcile must never remove anything on such a
    listing, because "not listed" then does not mean "gone"."""

    files: list[dict[str, Any]]
    complete: bool = True
    reason: str | None = None
    # Include-folder ids that files.get could not find (reason folder_missing).
    missing: list[str] = field(default_factory=list)


@dataclass
class ChangePage:
    """One page of the change feed. Commit ``resume_token`` as the cursor only
    after the page is applied: a failure on the next page then resumes here
    instead of replaying everything."""

    changes: list[dict[str, Any]]      # {"fileId", "removed": bool, "file": {...} | None}
    drive_removals: list[str]          # shared-drive ids removed or no longer reachable
    resume_token: str                  # nextPageToken, or newStartPageToken on the last page
    last: bool


@dataclass
class Content:
    data: bytes
    ext: str                 # the export's extension, or "" for a binary download
    export_mime: str | None  # None for binary downloads
    kind: str | None         # KIND_* for exports; None: the caller runs detect_bytes


def valid_id(value: str | None) -> bool:
    """Drive ids are ``[A-Za-z0-9_-]+``. Anything else is refused before it can
    reach a query string or a URL path."""
    return bool(value) and bool(_ID_RE.match(str(value)))


def is_whole_drive(scopes: list[str] | None) -> bool:
    """True when the scopes let a listing enumerate the entire Drive."""
    return bool(WHOLE_DRIVE_SCOPES & set(scopes or []))


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or an
    HTTP-date), or None when absent or unreadable. Not capped here."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    current = now if now is not None else time.time()
    return max(0.0, when.timestamp() - current)


def classify_token_error(exc: BaseException) -> str:
    """Map a token failure onto a :data:`KINDS` entry. ``DriveTokenError``
    carries its ``kind``; the message match is for anything older."""
    kind = getattr(exc, "kind", None)
    if kind in _TOKEN_KINDS:
        return str(kind)
    msg = str(exc).lower()
    if "not linked" in msg:
        return "unlinked"
    if "rejected the stored refresh token" in msg or "invalid_grant" in msg or "no refresh token" in msg:
        return "auth_revoked"
    if "invalid_client" in msg or "unauthorized_client" in msg:
        return "auth_misconfigured"
    return "unreachable"


def account_key_for_email(email: str) -> str:
    """The skill's account key (``base32(sha256("google:" + email)[:16])``,
    lowercase, unpadded), for token files written before it stored one."""
    digest = hashlib.sha256(f"google:{email.strip().lower()}".encode("utf-8")).digest()
    return base64.b32encode(digest[:16]).decode("ascii").rstrip("=").lower()


def _default_token(profile: str, force_refresh: bool = False) -> str:
    from app.drive import skill_token

    return skill_token.access_token(profile, force_refresh=force_refresh)


class _ErrorInfo:
    """Google's error body, reduced to what classification needs."""

    __slots__ = ("reasons", "location", "message")

    def __init__(self, resp: httpx.Response) -> None:
        self.reasons: list[str] = []
        self.location: str | None = None
        self.message = ""
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - an HTML error page is still an error
            self.message = (resp.text or "")[:200]
            return
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, str):
            self.reasons.append(err)
            return
        if not isinstance(err, dict):
            return
        self.message = str(err.get("message") or "")[:300]
        errors = [e for e in err.get("errors") or [] if isinstance(e, dict)]
        self.reasons.extend(str(e["reason"]) for e in errors if e.get("reason"))
        if errors and errors[0].get("location"):
            self.location = str(errors[0]["location"])
        for detail in err.get("details") or []:
            if isinstance(detail, dict) and detail.get("reason"):
                self.reasons.append(str(detail["reason"]))
        if not self.reasons and err.get("status"):
            self.reasons.append(str(err["status"]))

    @property
    def reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None


class DriveClient:
    """One profile's Drive, read-only. Blocking: call it from a worker thread,
    never the event loop. Its only state is the ``httpx.Client``, which is
    thread-safe, so one client may serve several threads.

    ``token_fn(profile, force_refresh)`` returns an access token; the default
    is :func:`app.drive.skill_token.access_token`. ``stop_event``, once set,
    ends any backoff wait (and any further request) with
    ``DriveError("unreachable", reason="stopped")``, so a long ``Retry-After``
    never holds up shutdown. ``sleep`` replaces the wait itself (tests).
    """

    MAX_ATTEMPTS = 6      # metadata endpoints: ~31 s of backoff before giving up
    CONTENT_ATTEMPTS = 3  # content endpoints: one bad file must not stall a worker for long

    def __init__(
        self,
        profile: str,
        *,
        http: httpx.Client | None = None,
        token_fn: Callable[[str, bool], str] | None = None,
        stop_event: threading.Event | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        self.profile = profile
        self._http = http or httpx.Client(timeout=_META_TIMEOUT, follow_redirects=True)
        self._own_http = http is None
        self._token_fn = token_fn or _default_token
        self._stop = stop_event
        self._sleep = sleep

    def close(self) -> None:
        if self._own_http:
            self._http.close()

    def __enter__(self) -> "DriveClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── identity (the token file, no network) ──────────────────────────────

    def _token_data(self) -> dict[str, Any] | None:
        from app.drive import skill_token

        data = skill_token.read_token(self.profile)
        return data if isinstance(data, dict) else None

    def identity(self) -> str | None:
        """``"<client_id>|<account_key>"`` of the linked account, or None when
        there is no readable token file. A grant belongs to the (OAuth client,
        account) pair, so switching either changes what Drive can reach."""
        data = self._token_data()
        if data is None:
            return None
        client_id = str(data.get("client_id") or "")
        key = str(data.get("account_key") or "")
        if not key and data.get("email"):
            key = account_key_for_email(str(data["email"]))
        return f"{client_id}|{key}"

    def whole_drive(self) -> bool:
        return is_whole_drive((self._token_data() or {}).get("scopes"))

    # ── plumbing ───────────────────────────────────────────────────────────

    def _stopped(self) -> DriveError:
        return DriveError("unreachable", "Drive client stopped", reason="stopped")

    def _check_stop(self) -> None:
        if self._stop is not None and self._stop.is_set():
            raise self._stopped()

    def _pause(self, seconds: float) -> None:
        self._check_stop()
        if self._sleep is not None:
            self._sleep(seconds)
        elif self._stop is not None:
            if self._stop.wait(seconds):
                raise self._stopped()
        else:
            time.sleep(seconds)
        self._check_stop()

    @staticmethod
    def _delay(attempt: int, retry_after: str | None) -> float:
        delay = min(64.0, 2.0 ** attempt) + random.uniform(0, 1)
        asked = parse_retry_after(retry_after)
        if asked is not None:
            delay = max(delay, asked)
        return min(delay, RETRY_AFTER_CAP_S)

    def _token(self, force: bool) -> str:
        try:
            return self._token_fn(self.profile, force_refresh=force)
        except DriveError:
            raise
        except Exception as exc:  # noqa: BLE001 - DriveTokenError or anything below it
            raise DriveError(classify_token_error(exc), str(exc)) from exc

    def _api_answers(self) -> bool:
        """One cheap metadata call, no retries: does the API itself answer?
        Separates "this file's content keeps failing" from "Drive is down"."""
        headers = {"Authorization": f"Bearer {self._token(False)}"}
        try:
            resp = self._http.get(f"{API}/changes/startPageToken", params={"supportsAllDrives": "true"},
                                  headers=headers, timeout=_PROBE_TIMEOUT)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

    def _content_failure(self, message: str, status: int | None, reason: str | None) -> DriveError:
        if self._api_answers():
            return DriveError("file_unavailable", message, status=status, reason=reason)
        return DriveError("unreachable", message, status=status, reason=reason)

    @staticmethod
    def _classify(status: int, info: _ErrorInfo, *, scope: str, cursor: bool) -> str:
        """``retry`` or a :data:`KINDS` entry. ``scope`` is ``account`` (lists,
        changes, about), ``file`` (files.get) or ``content`` (download/export)."""
        reasons = set(info.reasons)
        if cursor and (status in (404, 410) or (status == 400 and info.location == "pageToken")):
            return "bad_cursor"
        if status == 429 or (status == 403 and reasons & _RATE_REASONS):
            return "retry"
        if status == 403:
            if reasons & _ACCOUNT_REASONS:
                return "account_forbidden"
            if "appNotAuthorizedToFile" in reasons:
                return "not_authorized"
            if scope == "content":
                if "exportSizeLimitExceeded" in reasons:
                    return "export_too_large"
                if "downloadQuotaExceeded" in reasons:
                    return "file_unavailable"
                # fileNotDownloadable, cannotDownloadAbusiveFile, restricted
                # downloads, insufficientFilePermissions: readable metadata,
                # unreadable bytes. Never "gone".
                return "not_downloadable"
            return "account_forbidden" if scope == "account" else "http"
        if status == 404:
            return "not_found" if scope in ("file", "content") else "http"
        if status == 408 or 500 <= status < 600:
            return "retry"
        return "http"

    def _request(self, url: str, *, params: dict[str, Any] | None = None, scope: str = "account",
                 cursor: bool = False, stream_limit: int | None = None) -> httpx.Response | bytes:
        content = stream_limit is not None
        attempts = self.CONTENT_ATTEMPTS if content else self.MAX_ATTEMPTS
        failures = 0
        force = False
        refreshed = False
        while True:
            self._check_stop()
            headers = {"Authorization": f"Bearer {self._token(force)}"}
            force = False
            try:
                if content:
                    with self._http.stream("GET", url, params=params, headers=headers,
                                           timeout=_CONTENT_TIMEOUT) as resp:
                        if resp.status_code == 200:
                            return _read_capped(resp, stream_limit)
                        resp.read()
                        error = resp
                else:
                    error = self._http.get(url, params=params, headers=headers)
                    if error.status_code == 200:
                        return error
            except httpx.HTTPError as exc:
                failures += 1
                if failures >= attempts:
                    message = f"Google Drive unreachable: {type(exc).__name__}: {exc}"
                    if content:
                        raise self._content_failure(message, None, None) from exc
                    raise DriveError("unreachable", message) from exc
                self._pause(self._delay(failures - 1, None))
                continue

            status = error.status_code
            info = _ErrorInfo(error)
            if status == 401:
                if refreshed:
                    # A token Google just minted is refused: not a revocation
                    # (the refresh itself succeeded), so hold without a purge.
                    raise DriveError("auth_failed", f"Google Drive refused a fresh token: {info.reason}",
                                     status=status, reason=info.reason)
                # The stored or cached token may be revoked early; only a real
                # refresh can tell, and only its invalid_grant means revoked.
                refreshed = True
                force = True
                continue
            kind = self._classify(status, info, scope=scope, cursor=cursor)
            message = f"Google Drive error {status}: {info.reason or info.message or '-'}"
            if kind == "retry":
                failures += 1
                if failures >= attempts:
                    if status in (403, 429):
                        raise DriveError("rate_limited", message, status=status, reason=info.reason)
                    if content:
                        raise self._content_failure(message, status, info.reason)
                    raise DriveError("unreachable", message, status=status, reason=info.reason)
                self._pause(self._delay(failures - 1, error.headers.get("Retry-After")))
                continue
            if kind == "http":
                path = url[len(API):] if url.startswith(API) else url
                logger.warning(f"[userdocs:drive] profile={self.profile} {path}: {message} "
                               f"({info.message[:120]})")
            raise DriveError(kind, message, status=status, reason=info.reason)

    def _json(self, url: str, params: dict[str, Any], *, scope: str = "account",
              cursor: bool = False) -> dict[str, Any]:
        resp = self._request(url, params=params, scope=scope, cursor=cursor)
        assert isinstance(resp, httpx.Response)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            # A captive portal or proxy page answering 200: not Drive at all.
            raise DriveError("unreachable", "Google Drive answered with something other than JSON",
                             status=resp.status_code, reason="not_json")
        return data

    @staticmethod
    def _require_id(value: str, what: str = "file") -> str:
        if not valid_id(value):
            raise DriveError("http", f"not a Drive {what} id: {str(value)[:80]!r}", reason="invalid_id")
        return value

    # ── account ────────────────────────────────────────────────────────────

    def about(self) -> dict[str, Any]:
        """``{"user": {"permissionId", "emailAddress", "displayName"}}``."""
        return self._json(f"{API}/about", {"fields": "user(permissionId,emailAddress,displayName)"})

    def start_page_token(self) -> str:
        data = self._json(f"{API}/changes/startPageToken", {"supportsAllDrives": "true"})
        token = data.get("startPageToken")
        if not token:
            raise DriveError("unreachable", "Google Drive returned no start page token", reason="not_json")
        return str(token)

    def get(self, file_id: str) -> dict[str, Any]:
        """One file's metadata (``FIELDS``). Raises ``not_found`` when Google
        says 404 and ``not_authorized`` when the app lost its grant: the only
        confirmations a purge may rest on."""
        self._require_id(file_id)
        return self._json(f"{API}/files/{file_id}", {"fields": FIELDS, "supportsAllDrives": "true"},
                          scope="file")

    # ── listing ────────────────────────────────────────────────────────────

    def _pages(self, q: str, fields: str) -> Iterator[tuple[list[dict[str, Any]], bool]]:
        """(files, incompleteSearch) per files.list page."""
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "q": q, "pageSize": 1000, "fields": f"nextPageToken,incompleteSearch,files({fields})",
                "corpora": "allDrives", "includeItemsFromAllDrives": "true", "supportsAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            data = self._json(f"{API}/files", params)
            files = [f for f in data.get("files") or [] if isinstance(f, dict)]
            yield files, bool(data.get("incompleteSearch"))
            token = data.get("nextPageToken")
            if not token:
                return

    def list_all(self, *, q: str = "trashed = false", max_files: int = 50_000) -> Listing:
        """Every file (and folder) this link can reach. Under the per-file
        ``drive.file`` scope Google itself limits this to what was granted
        plus what Cremind created — which is exactly the set to index."""
        out = Listing(files=[])
        for files, incomplete in self._pages(q, FIELDS):
            if incomplete:
                _mark(out, "incomplete_search")
            for f in files:
                if len(out.files) >= max_files:
                    _mark(out, "truncated")
                    return out
                out.files.append(f)
        return out

    def list_folders_tree(self, folder_ids: list[str], *, max_files: int = 50_000) -> Listing:
        """Everything under ``folder_ids``, recursively, the folders themselves
        included. Each root is fetched first; a root that is gone or no longer
        granted makes the listing incomplete (``folder_missing``) rather than
        letting its whole subtree read as deleted."""
        for fid in folder_ids:
            self._require_id(fid, "folder")
        out = Listing(files=[])
        seen: set[str] = set()
        frontier: list[str] = []
        for fid in dict.fromkeys(folder_ids):
            try:
                meta = self.get(fid)
            except DriveError as exc:
                if exc.kind in ("not_found", "not_authorized"):
                    out.missing.append(fid)
                    _mark(out, "folder_missing")
                    continue
                raise
            if meta.get("trashed") or meta.get("id") in seen:
                continue
            if len(out.files) >= max_files:
                _mark(out, "truncated")
                return out
            seen.add(str(meta["id"]))
            out.files.append(meta)
            if meta.get("mimeType") == MIME_FOLDER:
                frontier.append(str(meta["id"]))
        while frontier:
            batch, frontier = frontier[:_FOLDER_BATCH], frontier[_FOLDER_BATCH:]
            parents = " or ".join(f"'{fid}' in parents" for fid in batch)
            try:
                for files, incomplete in self._pages(f"({parents}) and trashed = false", FIELDS):
                    if incomplete:
                        _mark(out, "incomplete_search")
                    for f in files:
                        fid = str(f.get("id") or "")
                        if not fid or fid in seen:
                            continue  # a multi-parent file under two include folders
                        if len(out.files) >= max_files:
                            _mark(out, "truncated")
                            return out
                        seen.add(fid)
                        out.files.append(f)
                        if f.get("mimeType") == MIME_FOLDER and valid_id(fid):
                            frontier.append(fid)
            except DriveError as exc:
                if exc.status != 404:
                    raise
                # A folder deleted between being listed and being queried.
                _mark(out, "folder_missing")
        return out

    def list_child_folders(self, parent_id: str | None) -> list[dict[str, Any]]:
        """Folders for the include-folder picker, sorted by name. ``parent_id``
        None means the top: "My Drive" on a whole-Drive account, or the
        top-most folders the per-file grant can see (a granted folder whose
        own parent was never granted is a top)."""
        folder_q = f"mimeType = '{MIME_FOLDER}' and trashed = false"
        if parent_id is None and not self.whole_drive():
            found: list[dict[str, Any]] = []
            for files, _incomplete in self._pages(folder_q, _FOLDER_FIELDS):
                found.extend(files)
                if len(found) >= 5000:
                    break
            ids = {f.get("id") for f in found}
            tops = [f for f in found if not any(p in ids for p in f.get("parents") or [])]
            return _by_name(tops)
        parent = "root" if parent_id is None else self._require_id(parent_id, "folder")
        found = []
        for files, _incomplete in self._pages(f"'{parent}' in parents and {folder_q}", _FOLDER_FIELDS):
            found.extend(files)
            if len(found) >= 5000:
                break
        return _by_name(found)

    # ── changes ────────────────────────────────────────────────────────────

    def changes(self, page_token: str) -> Iterator[ChangePage]:
        """The change feed since ``page_token``, one page at a time. Raises
        ``DriveError('bad_cursor')`` only when Google rejects the token itself
        (404/410, or a 400 naming ``pageToken``) — the caller then re-lists."""
        token = str(page_token)
        while True:
            data = self._json(f"{API}/changes", {
                "pageToken": token, "pageSize": 1000, "includeRemoved": "true",
                "supportsAllDrives": "true", "includeItemsFromAllDrives": "true",
                "fields": ("nextPageToken,newStartPageToken,"
                           f"changes(changeType,removed,fileId,driveId,file({FIELDS}))"),
            }, cursor=True)
            changes: list[dict[str, Any]] = []
            drive_removals: list[str] = []
            for c in data.get("changes") or []:
                if not isinstance(c, dict):
                    continue
                if (c.get("changeType") or "file") == "drive":
                    # Losing a shared drive arrives as one drive-level change,
                    # not one per file.
                    if c.get("removed") and c.get("driveId"):
                        drive_removals.append(str(c["driveId"]))
                    continue
                if not c.get("fileId"):
                    continue
                removed = bool(c.get("removed"))
                changes.append({"fileId": str(c["fileId"]), "removed": removed,
                                "file": None if removed else c.get("file")})
            new_start, nxt = data.get("newStartPageToken"), data.get("nextPageToken")
            if new_start:
                yield ChangePage(changes, drive_removals, str(new_start), True)
                return
            if nxt:
                yield ChangePage(changes, drive_removals, str(nxt), False)
                token = str(nxt)
                continue
            # Neither token: re-reading this page next time is harmless.
            yield ChangePage(changes, drive_removals, token, True)
            return

    # ── content ────────────────────────────────────────────────────────────

    def download(self, file_id: str, *, max_bytes: int) -> bytes:
        self._require_id(file_id)
        data = self._request(f"{API}/files/{file_id}", params={"alt": "media", "supportsAllDrives": "true"},
                             scope="content", stream_limit=max_bytes)
        assert isinstance(data, bytes)
        return data

    def export(self, file_id: str, mime: str, *, max_bytes: int) -> bytes:
        self._require_id(file_id)
        data = self._request(f"{API}/files/{file_id}/export", params={"mimeType": mime},
                             scope="content", stream_limit=max_bytes)
        assert isinstance(data, bytes)
        return data

    def fetch_content(self, meta: dict[str, Any], *, max_bytes: int) -> Content | None:
        """The bytes to index, or None when the type has no indexable content
        (folders, shortcuts, forms, sites, unknown Google-native types).

        ``canDownload`` false is checked before anything is fetched, exports
        included. Exports are tried in order; a format that fails for this file
        only (Google's size cap, a 5xx or timeout, an unsupported conversion)
        moves on to the next. Account-level failures stop at once."""
        mime = str(meta.get("mimeType") or "")
        if mime in METADATA_ONLY_NATIVE or (mime.startswith(GOOGLE_NATIVE_PREFIX) and mime not in EXPORTS):
            return None
        file_id = self._require_id(str(meta.get("id") or ""))
        caps = meta.get("capabilities") or {}
        if caps.get("canDownload") is False:
            raise DriveError("not_downloadable", "the owner disabled downloads for this file",
                             reason="canDownload")
        if mime in EXPORTS:
            failures: list[DriveError] = []
            for export_mime, ext, kind in EXPORTS[mime]:
                try:
                    data = self.export(file_id, export_mime, max_bytes=max_bytes)
                except DriveError as exc:
                    if exc.kind not in ("export_too_large", "too_large", "file_unavailable",
                                        "not_downloadable", "http"):
                        raise
                    failures.append(exc)
                    continue
                return Content(data=data, ext=ext, export_mime=export_mime, kind=kind)
            raise _pick_export_failure(failures)
        size = meta.get("size")
        try:
            known = int(size) if size is not None else None
        except (TypeError, ValueError):
            known = None
        if known is not None and known > max_bytes:
            raise DriveError("too_large", f"{known} bytes is over the {max_bytes}-byte cap")
        return Content(data=self.download(file_id, max_bytes=max_bytes), ext="", export_mime=None, kind=None)


def _mark(listing: Listing, reason: str) -> None:
    listing.complete = False
    if listing.reason is None:
        listing.reason = reason


def _by_name(folders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(folders, key=lambda f: (str(f.get("name") or "").casefold(), str(f.get("id") or "")))


def _pick_export_failure(failures: list[DriveError]) -> DriveError:
    """The one error that describes every export format failing."""
    if not failures:
        return DriveError("http", "no export format for this file")
    kinds = {e.kind for e in failures}
    if "file_unavailable" in kinds:
        return next(e for e in failures if e.kind == "file_unavailable")  # retry later may work
    if kinds <= {"export_too_large", "too_large"}:
        kind = "export_too_large" if "export_too_large" in kinds else "too_large"
        return next(e for e in failures if e.kind == kind)
    if "not_downloadable" in kinds:
        return next(e for e in failures if e.kind == "not_downloadable")
    return failures[0]


def _read_capped(resp: httpx.Response, limit: int) -> bytes:
    buf = bytearray()
    for chunk in resp.iter_bytes():
        buf.extend(chunk)
        if len(buf) > limit:
            raise DriveError("too_large", f"larger than {limit} bytes")
    return bytes(buf)
