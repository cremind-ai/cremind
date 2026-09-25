"""Route one :class:`ExtractRequest` to its format extractor, in process.

The worker calls :func:`extract_any` for every file, and tests call it
directly. It never raises: every outcome is an :class:`ExtractResult` whose
``reason`` the engine can store and act on.

- ``partial(too_large)``: a limit was hit; what was read before it is kept.
- ``metadata_only(<reason>)``: the content is deliberately not read (``archive``,
  ``executable``, ``audio``...) or cannot be (``encrypted``, ``legacy_format``,
  ``heic_unsupported``).
- ``error(<reason>)``: nothing usable was read. ``corrupt`` for a parser
  failure, ``awaiting_extractor`` when an optional extra is missing (the file
  is retried once it is installed), ``not_found`` / ``permission_denied`` /
  ``locked`` for file access, ``oom`` for a MemoryError.

A parser that fails after producing blocks yields ``partial`` with the
failure's reason instead of ``error``: a PDF truncated at page 80 still has 79
readable pages.

The final pass cleans every string (NUL bytes, lone surrogates) because the
result crosses a JSON pipe and ends in SQLite/Postgres, and either would
reject them far from here.
"""

from __future__ import annotations

import importlib
import os

from loguru import logger

from app.userdocs.types import (
    EXTRACT_ERROR,
    EXTRACT_METADATA_ONLY,
    EXTRACT_PARTIAL,
    KIND_ARCHIVE,
    KIND_CODE,
    KIND_CSV,
    KIND_DOC,
    KIND_DOCX,
    KIND_EML,
    KIND_EPUB,
    KIND_EXECUTABLE,
    KIND_HTML,
    KIND_IMAGE,
    KIND_JSON,
    KIND_MARKDOWN,
    KIND_MSG,
    KIND_ODP,
    KIND_ODS,
    KIND_ODT,
    KIND_OTHER,
    KIND_PDF,
    KIND_PPT,
    KIND_PPTX,
    KIND_RTF,
    KIND_TEXT,
    KIND_XLS,
    KIND_XLSX,
    KIND_XML,
    ExtractRequest,
    ExtractResult,
)

from .detect import HEAD_BYTES, TAIL_BYTES, detect_file, sniff
from .formats._base import (
    DEFAULT_LIMITS,
    Ctx,
    EncryptedDocument,
    LegacyFormatError,
    LimitReached,
    clean_str,
    json_safe,
    tidy,
)

__all__ = ["DEFAULT_LIMITS", "extract_any"]

REASON_CORRUPT = "corrupt"
REASON_ENCRYPTED = "encrypted"
REASON_LEGACY = "legacy_format"
REASON_MISSING_EXTRA = "awaiting_extractor"
REASON_OOM = "oom"

# kind → (module under .formats, function). Modules are imported on first use
# so a worker that only ever sees text files never loads pdfplumber.
_ROUTES: dict[str, tuple[str, str]] = {
    KIND_TEXT: ("text", "extract_text"),
    KIND_XML: ("text", "extract_text"),
    KIND_JSON: ("text", "extract_json"),
    KIND_MARKDOWN: ("markdown", "extract_markdown"),
    KIND_CODE: ("code", "extract_code"),
    KIND_CSV: ("sheets", "extract_csv"),
    KIND_XLSX: ("sheets", "extract_xlsx"),
    KIND_XLS: ("sheets", "extract_xls"),
    KIND_PDF: ("pdf", "extract_pdf"),
    KIND_DOCX: ("office", "extract_docx"),
    KIND_HTML: ("office", "extract_html"),
    KIND_EPUB: ("office", "extract_epub"),
    KIND_MSG: ("office", "extract_msg"),
    KIND_PPTX: ("slides", "extract_pptx"),
    KIND_ODT: ("odf", "extract_odt"),
    KIND_ODS: ("odf", "extract_ods"),
    KIND_ODP: ("odf", "extract_odp"),
    KIND_DOC: ("legacy", "extract_doc"),
    KIND_PPT: ("legacy", "extract_ppt"),
    KIND_RTF: ("rtf", "extract_rtf"),
    KIND_EML: ("mail", "extract_eml"),
    KIND_IMAGE: ("image", "extract_image"),
    KIND_ARCHIVE: ("archive", "extract_archive"),
    KIND_EXECUTABLE: ("binary", "extract_executable"),
}

# Once the container opened, any failure of these readers means "an old
# format variant we do not understand", which the user can fix by re-saving.
_LEGACY_KINDS = frozenset({KIND_DOC, KIND_PPT, KIND_RTF})


def _head_tail(req: ExtractRequest) -> tuple[bytes, bytes] | None:
    if req.data is not None:
        return req.data[:HEAD_BYTES], req.data[-TAIL_BYTES:]
    if not req.path or not os.path.isfile(req.path):
        return None
    try:
        with open(req.path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
            size = os.fstat(fh.fileno()).st_size
            if size <= len(head):
                return head, head[-TAIL_BYTES:]
            fh.seek(size - TAIL_BYTES)
            return head, fh.read(TAIL_BYTES)
    except OSError:
        return None  # the extractor will hit the same error and name it


def _detect(req: ExtractRequest) -> str:
    if req.data is None and req.path and os.path.exists(req.path):
        try:
            return detect_file(req.path)[0]
        except OSError:
            return KIND_OTHER
    ends = _head_tail(req)
    ext = os.path.splitext(req.name or req.path or "")[1]
    return sniff(ends[0], ends[1], ext)[0] if ends else KIND_OTHER


def _fail(ctx: Ctx, reason: str, exc: BaseException | None = None) -> None:
    """Record a failure, keeping any blocks already produced as ``partial``."""
    result = ctx.result
    if exc is not None:
        result.doc_meta["error"] = f"{type(exc).__name__}: {exc}"[:300]
    if result.blocks:
        result.status = EXTRACT_PARTIAL
    else:
        result.status = EXTRACT_ERROR
    result.reason = reason


def _access_reason(exc: OSError) -> str | None:
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if getattr(exc, "winerror", None) in (32, 33):
        return "locked"  # sharing or lock violation: another program holds it
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, IsADirectoryError):
        return "not_a_file"
    return None


def _finish(ctx: Ctx) -> ExtractResult:
    result = ctx.result
    blocks = []
    for block in result.blocks:
        text = tidy(clean_str(block.text))
        if text:
            block.text = text
            block.locator = json_safe(block.locator)
            blocks.append(block)
    result.blocks = blocks
    result.doc_meta = {k: v for k, v in json_safe(result.doc_meta).items() if v not in (None, "")}
    if result.exif is not None:
        result.exif = json_safe(result.exif)
    if result.image is not None:
        result.image = json_safe(result.image)
    result.listing = [clean_str(name) for name in result.listing]
    return result


def extract_any(req: ExtractRequest) -> ExtractResult:
    """Extract one file in this process. Never raises."""
    try:
        kind = req.kind or _detect(req)
    except Exception:  # noqa: BLE001 - detection is a convenience; never fatal
        kind = KIND_OTHER
    ctx = Ctx(req, kind)
    name = ctx.name
    try:
        if req.data is None and not req.path:
            _fail(ctx, "no_input")
            return _finish(ctx)
        ends = _head_tail(req)
        if ends is not None:
            ctx.result.mime = sniff(ends[0], ends[1], ctx.ext)[1]
        route = _ROUTES.get(kind)
        if route is None:
            # audio, video, database, font, bundle, encrypted, other: the
            # reason is the kind itself.
            ctx.metadata_only(kind)
            return _finish(ctx)
        module = importlib.import_module(f"{__package__}.formats.{route[0]}")
        getattr(module, route[1])(ctx)
    except LimitReached:
        pass  # Ctx.add already marked the result partial(too_large)
    except EncryptedDocument:
        ctx.result.blocks = []
        ctx.result.status = EXTRACT_METADATA_ONLY
        ctx.result.reason = REASON_ENCRYPTED
    except LegacyFormatError as exc:
        ctx.result.blocks = []
        ctx.result.status = EXTRACT_METADATA_ONLY
        ctx.result.reason = REASON_LEGACY
        ctx.result.doc_meta["error"] = str(exc)[:300]
    except MemoryError as exc:
        _fail(ctx, REASON_OOM, exc)
    except ImportError as exc:
        _fail(ctx, REASON_MISSING_EXTRA, exc)
        if exc.name:
            ctx.result.doc_meta["missing"] = exc.name
    except Exception as exc:  # noqa: BLE001 - every parser failure becomes a reason
        access = _access_reason(exc) if isinstance(exc, OSError) else None
        if type(exc).__name__ == "MissingDependencyException":  # markitdown's own
            _fail(ctx, REASON_MISSING_EXTRA, exc)
        elif access:
            _fail(ctx, access, exc)
        elif kind in _LEGACY_KINDS and not isinstance(exc, OSError):
            ctx.result.blocks = []
            ctx.result.status = EXTRACT_METADATA_ONLY
            ctx.result.reason = REASON_LEGACY
            ctx.result.doc_meta["error"] = f"{type(exc).__name__}: {exc}"[:300]
        else:
            _fail(ctx, REASON_CORRUPT, exc)
            logger.opt(exception=exc).debug(f"[userdocs:extract] {kind} '{name}' failed: {type(exc).__name__}")
    return _finish(ctx)
