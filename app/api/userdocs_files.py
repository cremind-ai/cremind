"""User Document Search — citations and file viewing.

What a citation chip opens:

- ``POST /api/userdocs/citations/resolve`` — ``{conversation_id?, tokens: [...]}``
  (at most 100) → ``{items: {<token>: item}}``, each item shaped like an entry
  of a message's ``metadata.citations.items`` (status, file, locator, snippet).
- ``GET /api/userdocs/files/{fid}/text`` — the indexed text around a citation:
  ``?chunk=<c8>&context=2`` (the cited chunk and its neighbours, highlighted),
  ``?pages=3-5`` or ``?lines=40-58``; without a selector, the start of the file.
  The text comes from the index, never from re-reading the file.
- ``GET /api/userdocs/files/{fid}/raw`` — the original bytes, for "open at page
  N". Local files only, and only when the file's real path is still inside the
  profile's indexed folder: the index names a path, and the path is re-checked
  now (a symlink swapped in since indexing must not reach outside). Drive files
  answer 409 ``DriveFile`` with their ``web_link``.
- ``GET /api/userdocs/files/{fid}/thumbnail?size=256`` — a JPEG made in memory
  and cached in a small in-process LRU; never written to disk, so viewing the
  user's photos leaves no copies of them behind.

The profile is always the caller's own (``request.user.username``). A ``fid``
is looked up in the caller's own index only, so another profile's ids are
simply unknown here — 404, indistinguishable from an id that never existed.

Everything that touches SQLite or the filesystem runs in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import io
import mimetypes
import os
import re
import threading
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from app.api._auth import require_auth
from app.userdocs.cite import CITE_ALPHABET_RE, locator_label, make_token
from app.utils.logger import logger

MAX_RESOLVE_TOKENS = 100
# The text view is a reader pane, not an export: a page or two either side of
# the citation. The client asks again for more.
MAX_SEGMENTS = 60
MAX_TEXT_CHARS = 120_000
MAX_CONTEXT = 10
# The original bytes of formats a browser would *execute* are never served
# inline from the app's origin: a user's own .html or .svg must not run script
# next to their session.
_ACTIVE_TYPES = frozenset({
    "text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml",
    "text/javascript", "application/javascript", "application/x-javascript",
})
THUMB_MIN, THUMB_MAX, THUMB_DEFAULT = 32, 1024, 256
# Decoding a huge image for a 256-pixel preview is wasted work at best.
THUMB_MAX_FILE_BYTES = 64 * 1024 * 1024
_THUMB_CACHE_ENTRIES = 256
_THUMB_CACHE_BYTES = 16 * 1024 * 1024

_FID_RE = re.compile(f"^{CITE_ALPHABET_RE}{{8}}$")
_C8_RE = re.compile(r"^[0-9a-f]{8}$")
_RANGE_RE = re.compile(r"^\s*(\d{1,7})\s*(?:[-–]\s*(\d{1,7})\s*)?$")

_GONE_FILE_STATUSES = frozenset({"tombstone", "missing"})


class _Fail(Exception):
    """An error response from a worker-thread helper."""

    def __init__(self, status: int, error: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.payload = {"error": error, "message": message, **extra}


def _profile(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _fid(request: Request) -> str:
    fid = str(request.path_params.get("fid") or "").lower()
    if not _FID_RE.match(fid):
        raise _Fail(404, "NotFound", "No such file.")
    return fid


def _range(raw: Optional[str], name: str) -> Optional[Tuple[int, int]]:
    if raw is None or raw == "":
        return None
    m = _RANGE_RE.match(raw)
    if not m:
        raise _Fail(400, "ValidationFailed", f"{name} must look like 3 or 3-5.", details={name: raw})
    a = int(m.group(1))
    b = int(m.group(2) or a)
    return (a, b) if a <= b else (b, a)


async def _run(fn, *args) -> Any:
    try:
        return await asyncio.to_thread(fn, *args)
    except _Fail as exc:
        return JSONResponse(exc.payload, status_code=exc.status)


# ── lookups (worker threads) ───────────────────────────────────────────────


def _record(db: Any, fid: str) -> Tuple[str, Dict[str, Any]]:
    if db is None:
        raise _Fail(404, "NotFound", "No such file.")
    rec = db.file_by_cite(fid)
    if rec is not None:
        if rec.get("status") in _GONE_FILE_STATUSES:
            raise _Fail(404, "NotFound", "That file is no longer in your documents.")
        return "file", rec
    rec = db.folder_by_cite(fid)
    if rec is not None and rec.get("status") in (None, "live"):
        return "folder", rec
    raise _Fail(404, "NotFound", "No such file.")


def _local_root(profile: str) -> Optional[str]:
    """The folder this profile's local index is relative to, as it stands now.

    The running engine's value when it has one (it validated it on configure).
    Otherwise the saved setting, re-validated for this profile — and only if
    it still resolves to the folder that was saved: an inherited working
    directory that has moved since is a pending change, not a new root.
    """
    from app.storage.userdocs_storage import get_userdocs_storage
    from app.userdocs import settings as uds
    from app.userdocs.service import get_service

    svc = get_service()
    rt = svc.runtime(profile) if svc is not None else None
    if rt is not None and rt.root:
        return rt.root
    row = get_userdocs_storage().get_source(profile, uds.SOURCE_LOCAL) or {}
    stored = row.get("root_path")
    if not stored:
        return None
    custom = row.get("root_mode") == uds.ROOT_CUSTOM
    check = uds.validate_root(stored if custom else None, is_admin=profile == "admin")
    if not check.ok or not check.path:
        return None
    if os.path.normcase(check.path) != os.path.normcase(uds.real_path(stored)):
        return None
    return check.path


def _local_file(profile: str, rec: Dict[str, Any]) -> str:
    """The real path of an indexed local file, re-checked against the root."""
    from app.userdocs import settings as uds

    if rec.get("source") != uds.SOURCE_LOCAL:
        raise _Fail(
            409, "DriveFile", "This file lives in Google Drive; open it there.",
            web_link=rec.get("drive_web_link"),
        )
    root = _local_root(profile)
    if not root:
        raise _Fail(409, "RootUnavailable", "Your documents folder is not available right now.")
    root_real = uds.real_path(root)
    rel = str(rec.get("rel_path") or "")
    real = os.path.realpath(os.path.join(root_real, *[p for p in rel.split("/") if p]))
    # realpath follows symlinks, so a link planted inside the folder cannot
    # hand out a file outside it — or Cremind's own system folder, which may
    # sit inside an admin's root.
    if not rel or not uds.is_inside(real, root_real) or uds.is_inside(real, uds.system_dir()):
        logger.warning(f"[userdocs] {profile}: refused to serve {rel!r}: outside the indexed folder")
        raise _Fail(403, "OutsideRoot", "That file is outside your documents folder.")
    if not os.path.isfile(real):
        raise _Fail(404, "FileMissing", "The file is no longer on disk.")
    return real


def _resolve(profile: str, conversation_id: Optional[str], tokens: List[str]) -> Dict[str, Any]:
    from app.storage.userdocs_citations_storage import get_userdocs_citations_storage
    from app.userdocs.citations import resolve_tokens

    if conversation_id and not get_userdocs_citations_storage().owns_conversation(profile, conversation_id):
        raise _Fail(404, "NotFound", "No such conversation.")
    return {"items": resolve_tokens(profile, conversation_id, tokens)}


def _overlaps(loc: Dict[str, Any], lo_key: str, hi_key: str, want: Tuple[int, int]) -> bool:
    try:
        lo = int(loc.get(lo_key))
    except (TypeError, ValueError):
        return False
    try:
        hi = int(loc.get(hi_key) or lo)
    except (TypeError, ValueError):
        hi = lo
    return lo <= want[1] and hi >= want[0]


def _file_text(
    profile: str, fid: str, c8: Optional[str], pages: Optional[Tuple[int, int]],
    lines: Optional[Tuple[int, int]], context: int,
) -> Dict[str, Any]:
    from app.userdocs.citations import profile_index

    with profile_index(profile) as db:
        target, rec = _record(db, fid)
        chunks = db.get_chunks(int(rec["id"])) if target == "file" else db.folder_chunks(int(rec["id"]))
        body = [c for c in chunks if c.ordinal >= 0]
        cards = [c for c in chunks if c.ordinal < 0]
        highlight: Optional[int] = None
        if c8:
            idx = next((i for i, c in enumerate(body) if (c.text_hash or "").startswith(c8)), None)
            if idx is not None:
                selected = body[max(0, idx - context): idx + context + 1]
                highlight = body[idx].id
            else:
                card = next((c for c in cards if (c.text_hash or "").startswith(c8)), None)
                if card is None:
                    raise _Fail(404, "ChunkNotFound", "That passage is no longer in the file.")
                selected, highlight = [card], card.id
        elif pages:
            selected = [c for c in body if _overlaps(c.locator or {}, "page", "page_end", pages)]
        elif lines:
            selected = [c for c in body if _overlaps(c.locator or {}, "line_start", "line_end", lines)]
        else:
            # A file with no body text (a photo, an archive) still has its card.
            selected = body or cards
        truncated = len(selected) > MAX_SEGMENTS
        selected = selected[:MAX_SEGMENTS]
        rows = {r["id"]: r for r in db.chunk_rows([c.id for c in selected])}

    segments: List[Dict[str, Any]] = []
    used = 0
    for c in selected:
        r = rows.get(c.id)
        if r is None:
            continue
        text = r.get("text") or ""
        if used + len(text) > MAX_TEXT_CHARS and segments:
            truncated = True
            break
        used += len(text)
        locator = r.get("locator") if isinstance(r.get("locator"), dict) else {}
        segments.append({
            "token": make_token(fid, r.get("text_hash")),
            "locator": locator,
            "locator_label": locator_label(locator),
            "text": text,
            "highlight": c.id == highlight,
        })
    return {
        "fid": fid,
        "name": rec.get("name") or str(rec.get("rel_path") or "").rsplit("/", 1)[-1],
        "rel_path": rec.get("rel_path"),
        "kind": "folder" if target == "folder" else rec.get("kind"),
        "source": rec.get("source"),
        "segments": segments,
        "truncated": truncated,
    }


def _raw_target(profile: str, fid: str) -> Tuple[str, str, str]:
    from app.userdocs.citations import profile_index

    with profile_index(profile) as db:
        target, rec = _record(db, fid)
    if target != "file":
        raise _Fail(404, "NotFound", "A folder has no file to open.")
    path = _local_file(profile, rec)
    name = rec.get("name") or os.path.basename(path)
    mime = rec.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return path, name, mime


# ── thumbnails ─────────────────────────────────────────────────────────────

_thumbs: "OrderedDict[tuple, bytes]" = OrderedDict()
_thumbs_bytes = 0
_thumbs_lock = threading.Lock()


def _thumb_get(key: tuple) -> Optional[bytes]:
    with _thumbs_lock:
        data = _thumbs.get(key)
        if data is not None:
            _thumbs.move_to_end(key)
        return data


def _thumb_put(key: tuple, data: bytes) -> None:
    global _thumbs_bytes
    with _thumbs_lock:
        old = _thumbs.pop(key, None)
        if old is not None:
            _thumbs_bytes -= len(old)
        _thumbs[key] = data
        _thumbs_bytes += len(data)
        while _thumbs and (len(_thumbs) > _THUMB_CACHE_ENTRIES or _thumbs_bytes > _THUMB_CACHE_BYTES):
            _, dropped = _thumbs.popitem(last=False)
            _thumbs_bytes -= len(dropped)


def clear_thumbnail_cache() -> None:
    global _thumbs_bytes
    with _thumbs_lock:
        _thumbs.clear()
        _thumbs_bytes = 0


def _render_thumbnail(path: str, size: int) -> bytes:
    """A JPEG no larger than ``size`` on its long side, built in memory."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise _Fail(409, "ThumbnailUnavailable", "Image previews need the User Document Search extras.") from None
    try:
        from app.userdocs.extract.formats.image import _register_heif

        _register_heif()  # HEIC/HEIF when pillow-heif is installed; a no-op otherwise
    except Exception:  # noqa: BLE001
        pass
    try:
        with Image.open(path) as src:
            src.draft("RGB", (size, size))  # JPEG decodes at a reduced scale
            im = ImageOps.exif_transpose(src)
            im.thumbnail((size, size))
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                rgba = im.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82, optimize=True)
            return buf.getvalue()
    except (Image.DecompressionBombError, OSError, ValueError, SyntaxError) as exc:
        logger.info(f"[userdocs] no thumbnail for {os.path.basename(path)}: {exc}")
        raise _Fail(422, "ThumbnailFailed", "Cannot make a preview of this image.") from None


def _thumbnail(profile: str, fid: str, size: int) -> bytes:
    from app.userdocs.citations import profile_index

    with profile_index(profile) as db:
        target, rec = _record(db, fid)
    if target != "file" or rec.get("kind") != "image":
        raise _Fail(415, "NoThumbnail", "Only images have previews.")
    version = rec.get("sha256") or f"{rec.get('size')}:{rec.get('mtime_ns')}"
    # The profile is part of the key: two profiles indexing the same folder
    # share file ids by coincidence only.
    key = (profile, fid, version, size)
    cached = _thumb_get(key)
    if cached is not None:
        return cached
    path = _local_file(profile, rec)
    if os.path.getsize(path) > THUMB_MAX_FILE_BYTES:
        raise _Fail(413, "TooLarge", "That image is too large to preview.")
    data = _render_thumbnail(path, size)
    _thumb_put(key, data)
    return data


# ── routes ─────────────────────────────────────────────────────────────────


def get_userdocs_files_routes() -> List[Route]:
    async def handle_resolve(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        body = await _json_body(request)
        tokens = body.get("tokens")
        conv = body.get("conversation_id") or None
        if not isinstance(tokens, list) or not all(isinstance(t, str) for t in tokens):
            return JSONResponse(
                {"error": "ValidationFailed", "details": {"tokens": "must be a list of strings"}},
                status_code=400,
            )
        if len(tokens) > MAX_RESOLVE_TOKENS:
            return JSONResponse(
                {"error": "ValidationFailed", "details": {"tokens": f"at most {MAX_RESOLVE_TOKENS}"}},
                status_code=400,
            )
        if conv is not None and not isinstance(conv, str):
            return JSONResponse(
                {"error": "ValidationFailed", "details": {"conversation_id": "must be a string"}},
                status_code=400,
            )
        result = await _run(_resolve, _profile(request), conv, [t[:64] for t in tokens])
        return result if isinstance(result, Response) else JSONResponse(result)

    async def handle_text(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        q = request.query_params
        try:
            fid = _fid(request)
            c8 = (q.get("chunk") or "").lower() or None
            if c8 is not None and not _C8_RE.match(c8):
                raise _Fail(400, "ValidationFailed", "chunk must be 8 hex characters.")
            pages = _range(q.get("pages"), "pages")
            lines = _range(q.get("lines"), "lines")
            try:
                context = max(0, min(MAX_CONTEXT, int(q.get("context") or 2)))
            except ValueError:
                raise _Fail(400, "ValidationFailed", "context must be a number.")
        except _Fail as exc:
            return JSONResponse(exc.payload, status_code=exc.status)
        result = await _run(_file_text, _profile(request), fid, c8, pages, lines, context)
        return result if isinstance(result, Response) else JSONResponse(result)

    async def handle_raw(request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            fid = _fid(request)
        except _Fail as exc:
            return JSONResponse(exc.payload, status_code=exc.status)
        result = await _run(_raw_target, _profile(request), fid)
        if isinstance(result, Response):
            return result
        path, name, mime = result
        active = mime in _ACTIVE_TYPES or mime.endswith("+xml")
        headers = {"X-Content-Type-Options": "nosniff", "Cache-Control": "private, no-cache"}
        if active:
            # Belt and braces for a download that gets opened anyway: no
            # script, no forms, no same-origin access. (Not on everything —
            # a sandboxed PDF will not open in the browser's viewer.)
            headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
        return FileResponse(
            path,
            media_type="application/octet-stream" if active else mime,
            filename=name,
            content_disposition_type="attachment" if active else "inline",
            headers=headers,
        )

    async def handle_thumbnail(request: Request) -> Response:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            fid = _fid(request)
            try:
                size = int(request.query_params.get("size") or THUMB_DEFAULT)
            except ValueError:
                raise _Fail(400, "ValidationFailed", "size must be a number.")
        except _Fail as exc:
            return JSONResponse(exc.payload, status_code=exc.status)
        size = max(THUMB_MIN, min(THUMB_MAX, size))
        result = await _run(_thumbnail, _profile(request), fid, size)
        if isinstance(result, Response):
            return result
        return Response(result, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    return [
        Route("/api/userdocs/citations/resolve", handle_resolve, methods=["POST"]),
        Route("/api/userdocs/files/{fid}/text", handle_text, methods=["GET"]),
        Route("/api/userdocs/files/{fid}/raw", handle_raw, methods=["GET"]),
        Route("/api/userdocs/files/{fid}/thumbnail", handle_thumbnail, methods=["GET"]),
    ]
