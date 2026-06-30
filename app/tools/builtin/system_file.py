"""System File built-in tool.

Provides file browsing, reading, and writing capabilities within the
CREMIND_SYSTEM_DIR directory. Supports text and binary files with intelligent
content handling via markitdown conversion and token-based limits. Write
operations are restricted to human-readable text files.
"""

import asyncio
import fnmatch
import mimetypes
import os
import platform
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.tools.builtin.base import (
    BuiltInTool,
    BuiltInToolResult,
    missing_dependency_result,
)
from app.types import ToolConfig, ToolResultFile, ToolResultWithFiles
from app.utils.logger import logger

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

MAX_READABLE_TOKENS = 10000
MAX_MARKITDOWN_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_LIST_ENTRIES = 100
MAX_SEARCH_RESULTS = 100

# grep_files defaults (the first three are user-configurable; the rest are
# internal safety rails that bound a single grep call regardless of profile).
MAX_GREP_RESULTS = 100                  # hard cap on returned entries
MAX_GREP_FILE_SIZE = 5 * 1024 * 1024    # 5 MB; larger files are skipped
MAX_GREP_MATCH_LINE_LENGTH = 1000       # clip a single matched/context line
DEFAULT_GREP_RESULTS = 50               # per-call max_results default
MAX_GREP_FILES_SCANNED = 5000           # traversal budget (files actually read)
MAX_GREP_CONTEXT_LINES = 50             # clamp for context / -A / -B / -C


class Var:
    """Variable keys for the System File tool's per-profile overrides."""
    MAX_READABLE_TOKENS = "MAX_READABLE_TOKENS"
    MAX_MARKITDOWN_FILE_SIZE = "MAX_MARKITDOWN_FILE_SIZE"
    MAX_LIST_ENTRIES = "MAX_LIST_ENTRIES"
    MAX_SEARCH_RESULTS = "MAX_SEARCH_RESULTS"
    MAX_GREP_RESULTS = "MAX_GREP_RESULTS"
    MAX_GREP_FILE_SIZE = "MAX_GREP_FILE_SIZE"
    MAX_GREP_MATCH_LINE_LENGTH = "MAX_GREP_MATCH_LINE_LENGTH"


def _resolve_limits(arguments: Dict[str, Any]) -> Dict[str, int]:
    """Read the four file-tool limits from ``arguments['_variables']``.

    Falls back to the module-level constants when no per-profile override
    is present or when the override fails to parse as an integer.
    """
    variables = arguments.get("_variables") or {}

    def _as_int(key: str, fallback: int) -> int:
        try:
            raw = variables.get(key)
            return int(raw) if raw not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    return {
        "max_readable_tokens": _as_int(Var.MAX_READABLE_TOKENS, MAX_READABLE_TOKENS),
        "max_markitdown_file_size": _as_int(Var.MAX_MARKITDOWN_FILE_SIZE, MAX_MARKITDOWN_FILE_SIZE),
        "max_list_entries": _as_int(Var.MAX_LIST_ENTRIES, MAX_LIST_ENTRIES),
        "max_search_results": _as_int(Var.MAX_SEARCH_RESULTS, MAX_SEARCH_RESULTS),
        "max_grep_results": _as_int(Var.MAX_GREP_RESULTS, MAX_GREP_RESULTS),
        "max_grep_file_size": _as_int(Var.MAX_GREP_FILE_SIZE, MAX_GREP_FILE_SIZE),
        "max_grep_match_line_length": _as_int(
            Var.MAX_GREP_MATCH_LINE_LENGTH, MAX_GREP_MATCH_LINE_LENGTH),
    }

# Lazy-loaded encoder (created once on first use)
_encoder = None


def _get_encoder():
    """Lazy-build the tiktoken encoder.

    Imported here rather than at module top so the system_file tool can
    still load when the ``tokenization`` extras group isn't installed —
    callers that need a token count get a clear ImportError instead of
    failing at server boot.
    """
    global _encoder
    if _encoder is None:
        from tiktoken import encoding_for_model
        _encoder = encoding_for_model("gpt-4o")
    return _encoder


# ---------------------------------------------------------------------------
# Lazy markitdown converter
# ---------------------------------------------------------------------------

_markitdown = None


def _get_markitdown():
    """Return a MarkItDown instance, importing lazily to keep startup fast.

    Raises ``ImportError`` if the ``documents`` feature isn't installed.
    Callers decide whether the absence is fatal (binary file that only
    markitdown can read) or merely a degraded path (text file that falls
    back to a raw read).
    """
    global _markitdown
    if _markitdown is None:
        from markitdown import MarkItDown  # ImportError → caller handles
        _markitdown = MarkItDown()
    return _markitdown


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _allowed_roots(arguments: Dict[str, Any], data_dir: str) -> List[str]:
    """Roots an *absolute* path argument may fall under, beyond ``data_dir``.

    Mirrors the trust boundary already enforced by ``app/api/files.py``: the
    per-profile skills tree (``CREMIND_SKILL_DIR`` — its parent, so every skill
    is covered by one entry), the user working dir, and the per-profile slice of
    ``CREMIND_SYSTEM_DIR`` (NOT the bare root, so another profile's
    ``tokens/*.token`` stays unreachable). ``data_dir`` itself is always allowed
    by ``_safe_resolve`` so it is not repeated here.
    """
    roots: List[str] = []
    profile = arguments.get("_profile")
    try:
        from app.config.system_vars import build_system_env
        env = build_system_env(profile)
    except Exception:  # noqa: BLE001
        logger.debug("system_file: build_system_env failed for allowed roots", exc_info=True)
        return roots
    if env.get("CREMIND_SKILL_DIR"):
        roots.append(env["CREMIND_SKILL_DIR"])
    if env.get("CREMIND_USER_WORKING_DIR"):
        roots.append(env["CREMIND_USER_WORKING_DIR"])
    sys_dir = env.get("CREMIND_SYSTEM_DIR")
    if sys_dir and profile:
        roots.append(os.path.join(sys_dir, profile))
    return roots


def _report_path(full_path: str, base: str) -> str:
    """Display path for a result: relative to ``base`` when inside it, else the
    absolute path. Avoids ``../..`` strings for hits that live outside the
    active working directory (e.g. results under an absolute skill path)."""
    if full_path == base or full_path.startswith(base + os.sep):
        return os.path.relpath(full_path, base).replace(os.sep, "/")
    return full_path.replace(os.sep, "/")


def _safe_resolve(
    data_dir: str,
    relative_path: str,
    allowed_roots: Optional[List[str]] = None,
) -> str:
    """Resolve *relative_path* and confirm it stays inside an allowed root.

    Relative paths resolve under ``data_dir`` (the active working directory).
    Absolute paths (and ``~``-prefixed paths) are accepted only when they fall
    within ``data_dir`` or one of ``allowed_roots`` (e.g. a loaded skill's own
    directory) — matching the paths ``exec_shell`` already accepts. Raises
    ValueError with actionable guidance on a true escape.
    """
    base = os.path.realpath(data_dir)
    os.makedirs(base, exist_ok=True)

    roots = [base]
    for r in (allowed_roots or []):
        if r:
            roots.append(os.path.realpath(r))

    raw = os.path.expanduser(relative_path)
    # Treat a Windows drive-qualified path ("C:..." / "C:\\...") as absolute too.
    is_abs = os.path.isabs(raw) or (len(raw) >= 2 and raw[1] == ":")
    if is_abs:
        target = os.path.realpath(raw)
    else:
        target = os.path.realpath(os.path.join(base, raw.lstrip("/\\")))

    for root in roots:
        if target == root or target.startswith(root + os.sep):
            return target

    raise ValueError(
        f"Access denied: '{relative_path}' resolves outside the allowed "
        f"directories. Allowed roots: {roots}. Absolute paths are accepted only "
        f"under one of these (e.g. a loaded skill's own directory). Use a path "
        f"inside the current working directory, or call change_working_directory first."
    )


# Shared guidance appended to every path-style parameter description. The
# reasoning model used to relativize absolute paths — stripping the home/drive
# prefix off an attached-file path — which then resolved under the wrong base
# (the working directory) and 404'd. ``_safe_resolve`` already accepts an
# absolute path verbatim when it lands inside an allowed root, so the fix is to
# tell the model absolute paths are first-class and must be passed unchanged.
_ABS_PATH_NOTE = (
    "May be given relative to the current working directory, OR as an absolute "
    "path (e.g. 'C:\\Users\\...' on Windows, '/home/...' on POSIX). When an "
    "absolute path is provided — such as an attached/uploaded file listed in the "
    "prompt — pass it EXACTLY as given: never shorten it, strip the home/drive "
    "prefix, or convert it to a relative path. Relative paths resolve under the "
    "current working directory; do not repeat that directory's own name."
)


def _relative_abs_hint(path: str, data_dir: str, arguments: Dict[str, Any]) -> str:
    """Return a one-line correction hint to append to a 'Not found' message.

    Fires only when *path* is relative yet the same trailing path exists under
    the user's home or an allowed root — the signature of the model having
    relativized an absolute/attached path (e.g. stripping the ``C:\\Users\\you``
    prefix off an upload path). Purely read-only: it never resolves or mutates
    anything, and changes no path-resolution semantics. Empty string otherwise.
    """
    raw = os.path.expanduser(path or "")
    is_abs = os.path.isabs(raw) or (len(raw) >= 2 and raw[1] == ":")
    if is_abs:
        return ""
    tail = raw.lstrip("/\\")
    if not tail:
        return ""
    bases = [os.path.expanduser("~"), *(_allowed_roots(arguments, data_dir) or [])]
    for base in bases:
        try:
            if base and os.path.exists(os.path.join(base, tail)):
                return (
                    " If you meant an attached or absolute path, pass it EXACTLY "
                    "as given (do not strip the home/drive prefix or relativize it)."
                )
        except OSError:
            continue
    return ""


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def _is_binary(file_path: str) -> bool:
    """Heuristic: file is binary if the first 8 KB contain a null byte."""
    try:
        with open(file_path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except OSError:
        return True


def _guess_mime(file_path: str) -> str:
    mime, _ = mimetypes.guess_type(file_path)
    return mime or "application/octet-stream"


def _image_dimensions(file_path: str, mime: str) -> Tuple[Optional[int], Optional[int]]:
    """Return ``(width, height)`` in pixels for an image, or ``(None, None)``.

    Lazily imports Pillow; if it is not installed (an optional dependency) the
    dimensions are simply omitted rather than failing — ``get_file_info`` still
    returns the rest of the metadata.
    """
    if not mime.startswith("image/"):
        return None, None
    try:
        from PIL import Image  # lazy; optional
    except ImportError:
        return None, None
    try:
        with Image.open(file_path) as im:
            return int(im.width), int(im.height)
    except Exception:  # noqa: BLE001
        return None, None


def _format_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def _format_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# MIME prefixes that markitdown can handle beyond plain text
_MARKITDOWN_SUPPORTED_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "text/html",
    "text/csv",
    "application/json",
    "application/xml",
    "text/xml",
}


def _is_markitdown_supported(mime: str) -> bool:
    """Return True if markitdown can convert this mime type."""
    if mime.startswith("text/"):
        return True
    return mime in _MARKITDOWN_SUPPORTED_MIMES


# MIME types that represent human-readable text beyond the text/* family
_TEXT_APP_MIMES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/typescript",
    "application/x-yaml",
    "application/x-sh",
    "application/x-httpd-php",
    "application/sql",
    "application/graphql",
    "application/x-perl",
    "application/x-python",
    "application/x-ruby",
    "application/toml",
}


def _is_text_mime(mime: str) -> bool:
    """Return True if *mime* represents a human-readable text file."""
    if mime.startswith("text/"):
        return True
    return mime in _TEXT_APP_MIMES


# ---------------------------------------------------------------------------
# Unified-diff helpers (used by OverwriteFileTool)
# ---------------------------------------------------------------------------

# '@@ -a[,b] +c[,d] @@' with optional counts and optional trailing section text.
_HUNK_HEADER_RE = re.compile(r"^@@+\s*-(\d+)(?:,\d+)?\s+\+\d+(?:,\d+)?\s*@@+")


class _Hunk:
    """One parsed hunk: 1-based old-start hint + old/new line blocks.

    ``has_header`` records whether an ``@@`` header was present, so a
    pure-insertion hunk (empty ``old_block``) can be positioned from the header
    but rejected when no header gives us anywhere to anchor it.
    """
    __slots__ = ("old_start", "old_block", "new_block", "has_header")

    def __init__(self, old_start: int, old_block: List[str],
                 new_block: List[str], has_header: bool):
        self.old_start = old_start
        self.old_block = old_block   # context + removed lines, marker stripped
        self.new_block = new_block   # context + added lines, marker stripped
        self.has_header = has_header


def _parse_unified_diff(diff: str) -> List["_Hunk"]:
    """Parse *diff* into hunks (terminator-free lines). Raises ValueError('Invalid diff').

    Tolerant of LLM-authored diffs: optional hunk-header counts and trailing
    section text, an optional leading ``---``/``+++`` file header, ``\\ No
    newline`` markers, and a bare body with no ``@@`` header (treated as a
    single content-matched hunk).
    """
    raw = diff.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if raw and raw[-1] == "":
        raw.pop()  # drop the phantom element from a trailing newline on the diff string

    hunks: List[_Hunk] = []
    old_start = 0
    has_header = False
    old_block: Optional[List[str]] = None
    new_block: Optional[List[str]] = None

    def _flush() -> None:
        nonlocal old_block, new_block, old_start, has_header
        if old_block is not None and (old_block or new_block):
            hunks.append(_Hunk(old_start, old_block, new_block or [], has_header))
        old_block, new_block, old_start, has_header = None, None, 0, False

    for line in raw:
        m = _HUNK_HEADER_RE.match(line)
        if m:
            _flush()
            old_start = int(m.group(1))
            has_header = True
            old_block, new_block = [], []
            continue
        if line.startswith("--- ") or line.startswith("+++ ") or line.startswith("\\"):
            continue  # file headers a model may prepend / "\ No newline at end of file"
        if old_block is None:  # headerless diff: open an implicit hunk on first body line
            if line[:1] not in (" ", "+", "-"):
                continue       # skip leading prose/blank lines before any hunk content
            old_block, new_block = [], []
        marker = line[:1]
        if marker == "-":
            old_block.append(line[1:])
        elif marker == "+":
            new_block.append(line[1:])
        elif marker == " ":
            old_block.append(line[1:])   # space-marked context: drop the one marker space
            new_block.append(line[1:])
        else:
            # No diff marker: the model dropped the leading space on a context
            # line (or it's a bare blank line). Treat the WHOLE line as unchanged
            # context -- do NOT strip the first character.
            old_block.append(line)
            new_block.append(line)

    _flush()
    if not hunks:
        raise ValueError("Invalid diff")
    return hunks


def _find_block(lines: List[str], block: List[str], hint_start: int) -> int:
    """0-based index where *block* matches contiguously; -1 none, -2 ambiguous.

    With several matches, the header's 1-based ``hint_start`` breaks the tie by
    nearest position; a perfect tie (or no usable hint) stays ambiguous.
    """
    n, b = len(lines), len(block)
    if b == 0:
        return -1
    matches = [i for i in range(0, n - b + 1) if lines[i:i + b] == block]
    if not matches:
        return -1
    if len(matches) == 1:
        return matches[0]
    if hint_start <= 0:
        return -2
    target = hint_start - 1
    nearest = sorted(matches, key=lambda i: (abs(i - target), i))
    if abs(nearest[0] - target) == abs(nearest[1] - target):
        return -2  # equally near on both sides -> still ambiguous
    return nearest[0]


def _deescape_quotes(line: str) -> str:
    r"""Undo backslash-escaped quotes (\' and \") in a diff line.

    A model that passes the diff as a quoted JSON string often escapes the
    quotes inside it (e.g. ``don\'t``), and those backslashes leak into the
    diff argument. Used only as a fallback when the verbatim match fails, so a
    file that genuinely contains ``\'`` keeps matching exactly.
    """
    return line.replace("\\'", "'").replace('\\"', '"')


def _apply_hunks(file_lines: List[str], hunks: List["_Hunk"]) -> Tuple[List[str], int]:
    """Apply hunks sequentially, re-matching by content. Returns (new_lines, lines_changed).

    Re-matching against the running result (rather than trusting header line
    numbers) keeps later hunks correct even when an earlier hunk changed the
    line count. Raises ValueError('Diff did not apply:<n>') or
    ValueError('Ambiguous diff:<n>') naming the 1-based hunk that failed.
    """
    lines = list(file_lines)
    changed = 0
    for n, h in enumerate(hunks, start=1):
        if not h.old_block:  # pure insertion: position from the header (after old_start lines)
            if not h.has_header:
                raise ValueError(f"Diff did not apply:{n}")
            pos = min(max(h.old_start, 0), len(lines))
            lines[pos:pos] = h.new_block
            changed += len(h.new_block)
            continue
        new_block = h.new_block
        at = _find_block(lines, h.old_block, h.old_start)
        if at == -1:
            # Fallback for the common quoting artifact: the model backslash-
            # escaped quotes inside the diff (don\'t). Retry with those escapes
            # stripped from BOTH sides so the match lands and clean text is
            # written. Only runs after the verbatim match failed, so a file that
            # really contains \' is still matched exactly above.
            deesc_old = [_deescape_quotes(line) for line in h.old_block]
            if deesc_old != h.old_block:
                at = _find_block(lines, deesc_old, h.old_start)
                if at >= 0:
                    new_block = [_deescape_quotes(line) for line in h.new_block]
        if at == -1:
            raise ValueError(f"Diff did not apply:{n}")
        if at == -2:
            raise ValueError(f"Ambiguous diff:{n}")
        lines[at:at + len(h.old_block)] = new_block
        changed += max(len(h.old_block), len(new_block))
    return lines, changed


def _file_description(name: str, mime: str, uri: str) -> str:
    """Return a Markdown description of a file for the LLM observation."""
    if mime.startswith("image/"):
        return f'![{name}]({uri} "{name}")'
    return f'[{name}]({uri} "{name}")'


# ---------------------------------------------------------------------------
# Grep helpers
# ---------------------------------------------------------------------------

# Curated file-type aliases (ripgrep-style ``--type``). Maps a short language
# name the LLM reliably knows to the file extensions it covers, so the agent
# can write ``type='py'`` instead of an error-prone glob. Extensions are
# compared case-insensitively against ``os.path.splitext(name)[1].lower()``.
_TYPE_EXTENSIONS: Dict[str, Tuple[str, ...]] = {
    "py":     (".py", ".pyi"),
    "js":     (".js", ".jsx", ".mjs", ".cjs"),
    "ts":     (".ts", ".tsx"),
    "web":    (".html", ".htm", ".css", ".scss", ".js", ".jsx", ".ts", ".tsx",
               ".vue", ".svelte"),
    "c":      (".c", ".h"),
    "cpp":    (".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"),
    "rust":   (".rs",),
    "go":     (".go",),
    "java":   (".java",),
    "json":   (".json", ".jsonl", ".ndjson"),
    "yaml":   (".yaml", ".yml"),
    "toml":   (".toml",),
    "xml":    (".xml",),
    "html":   (".html", ".htm"),
    "css":    (".css", ".scss", ".sass", ".less"),
    "md":     (".md", ".markdown", ".mdx"),
    "txt":    (".txt", ".text", ".log"),
    "sh":     (".sh", ".bash", ".zsh"),
    "sql":    (".sql",),
    "csv":    (".csv", ".tsv"),
    "config": (".ini", ".cfg", ".conf", ".env", ".properties", ".toml",
               ".yaml", ".yml"),
}


def _compile_grep_pattern(
    pattern: str,
    *,
    fixed_strings: bool,
    whole_word: bool,
    case_insensitive: bool,
    multiline: bool,
) -> "re.Pattern[str]":
    """Build the compiled regex for a grep call. Raises ``re.error`` on bad input.

    ``fixed_strings`` escapes the pattern (grep -F); ``whole_word`` brackets it
    in word boundaries (grep -w) -- grouped with ``(?:...)`` so alternations
    bind correctly. ``multiline`` sets ``re.DOTALL`` so ``.`` spans newlines;
    we drive line boundaries ourselves (see ``_grep_search_multiline``) so
    ``re.MULTILINE`` is intentionally *not* set.
    """
    body = re.escape(pattern) if fixed_strings else pattern
    if whole_word:
        body = r"\b(?:" + body + r")\b"
    flags = 0
    if case_insensitive:
        flags |= re.IGNORECASE
    if multiline:
        flags |= re.DOTALL
    return re.compile(body, flags)


def _expand_braces(glob_pat: str) -> List[str]:
    """Expand a single ``{a,b,c}`` group into multiple fnmatch patterns.

    stdlib ``fnmatch`` does not understand brace sets, so ``*.{ts,tsx}`` is
    pre-expanded to ``['*.ts', '*.tsx']``. Only the first brace group is
    expanded (enough for the common ``*.{ext1,ext2}`` case); patterns without
    a brace group are returned unchanged.
    """
    start = glob_pat.find("{")
    end = glob_pat.find("}", start + 1)
    if start == -1 or end == -1:
        return [glob_pat]
    prefix, inner, suffix = glob_pat[:start], glob_pat[start + 1:end], glob_pat[end + 1:]
    return [f"{prefix}{opt}{suffix}" for opt in inner.split(",")]


def _grep_name_matches(
    name: str,
    glob_variants: Optional[List[str]],
    type_exts: Optional[Tuple[str, ...]],
) -> bool:
    """Return True if a file base name passes the glob AND type filters.

    ``glob`` matching uses ``fnmatch.fnmatch`` (same call as search_files /
    list_files -- case-insensitive on Windows, case-sensitive on POSIX); the
    type filter compares the lower-cased extension against the alias set.
    """
    if glob_variants is not None:
        if not any(fnmatch.fnmatch(name, g) for g in glob_variants):
            return False
    if type_exts is not None:
        if os.path.splitext(name)[1].lower() not in type_exts:
            return False
    return True


def _truncate_line(text: str, max_len: int) -> Tuple[str, bool]:
    """Clip *text* to *max_len* chars; return (clipped, was_truncated)."""
    if len(text) > max_len:
        return text[:max_len], True
    return text, False


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class SearchFilesTool(BuiltInTool):
    name: str = "search_files"
    description: str = (
        "Recursively search for files or directories by name within the user's "
        "data directory. Supports deep searching through nested folders using "
        "case-insensitive keyword matching. All query words must appear in the "
        "file name. E.g. 'find the file cremind' will match 'cremind.pdf'."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query. Split into keywords; a file matches if ALL "
                    "keywords appear in its name (case-insensitive). "
                    "E.g. 'cremind' matches 'cremind.pdf'."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional sub-path to start searching from. "
                    + _ABS_PATH_NOTE + " Defaults to '.' (the current working "
                    "directory itself)."
                ),
            },
            "pattern": {
                "type": "string",
                "description": (
                    "Optional glob pattern to filter results (e.g. '*.pdf'). "
                    "Applied on top of the keyword query."
                ),
            },
            "type": {
                "type": "string",
                "enum": ["file", "directory"],
                "description": (
                    "Filter results by type: 'file' or 'directory'. "
                    "If omitted, both files and directories are returned."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum number of results to return. "
                    "Defaults to 20, capped at 100."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        limits = _resolve_limits(arguments)
        query = arguments.get("query", "").strip()
        rel_path = arguments.get("path", ".")
        pattern = arguments.get("pattern")
        type_filter = arguments.get("type")
        max_results = min(arguments.get("max_results", 20), limits["max_search_results"])

        if not query:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "query is required.",
            })

        try:
            search_root = _safe_resolve(data_dir, rel_path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        if not os.path.isdir(search_root):
            return BuiltInToolResult(structured_content={
                "error": "Not a directory",
                "message": f"'{rel_path}' is not a directory.",
            })

        keywords = query.lower().split()
        base = os.path.realpath(data_dir)
        results = []

        for dirpath, dirnames, filenames in os.walk(search_root):
            entries = []
            if type_filter != "file":
                entries.extend((d, True) for d in dirnames)
            if type_filter != "directory":
                entries.extend((f, False) for f in filenames)

            for name, is_dir in entries:
                name_lower = name.lower()

                if not all(kw in name_lower for kw in keywords):
                    continue

                if pattern and not fnmatch.fnmatch(name, pattern):
                    continue

                full_path = os.path.join(dirpath, name)
                entry_rel = _report_path(full_path, base)

                entry: Dict[str, Any] = {
                    "name": name,
                    "path": entry_rel.replace(os.sep, "/"),
                    "type": "directory" if is_dir else "file",
                }

                if not is_dir:
                    try:
                        stat = os.stat(full_path)
                        entry["size"] = stat.st_size
                        entry["size_human"] = _format_size(stat.st_size)
                        entry["mime_type"] = _guess_mime(full_path)
                        entry["modified"] = _format_timestamp(stat.st_mtime)
                    except OSError:
                        entry["error"] = "cannot stat"
                else:
                    try:
                        stat = os.stat(full_path)
                        entry["modified"] = _format_timestamp(stat.st_mtime)
                    except OSError:
                        pass

                results.append(entry)
                if len(results) >= max_results:
                    break

            if len(results) >= max_results:
                break

        return BuiltInToolResult(structured_content={
            "query": query,
            "search_root": rel_path,
            "total_matches": len(results),
            "max_results": max_results,
            "results": results,
        })


class GrepFilesTool(BuiltInTool):
    name: str = "grep_files"
    description: str = (
        "Search the CONTENTS of text files for a regular-expression (or "
        "fixed-string) pattern, recursively within the current working "
        "directory. This is the content counterpart to search_files, which "
        "matches file NAMES — use grep_files to find WHICH files contain a "
        "string/regex and on WHAT line. Supports case-insensitivity, glob and "
        "file-type filters, before/after context lines, whole-word, "
        "fixed-string, invert and multiline matching, plus count and "
        "files-with-matches summary modes. Returns relative paths you can pass "
        "straight to read_file. Binary and oversized files are skipped "
        "automatically. E.g. 'find TODO in the python files' -> pattern='TODO', "
        "type='py'."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": (
                    "The search pattern. Interpreted as a regular expression "
                    "by default (e.g. 'def\\s+\\w+', 'TODO|FIXME'). Set "
                    "fixed_strings=true to match it literally instead. Required."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional sub-path to search — a directory (searched "
                    "recursively) or a single file. " + _ABS_PATH_NOTE
                    + " Defaults to '.' (the current working directory)."
                ),
            },
            "glob": {
                "type": "string",
                "description": (
                    "Optional glob to limit which files are searched, matched "
                    "against the file name only, e.g. '*.py' or '*.{ts,tsx}' "
                    "(one brace set is supported). Combine with `type` for "
                    "broader categories; a file must satisfy both when both "
                    "are given."
                ),
            },
            "type": {
                "type": "string",
                "enum": list(_TYPE_EXTENSIONS.keys()),
                "description": (
                    "Optional file-type alias that expands to a set of "
                    "extensions, e.g. type='py' searches *.py/*.pyi, "
                    "type='web' searches html/css/js/ts/etc. More reliable "
                    "than hand-writing a multi-extension glob."
                ),
            },
            "output_mode": {
                "type": "string",
                "enum": ["files_with_matches", "content", "count"],
                "description": (
                    "What to return. 'files_with_matches' (default): just the "
                    "list of files containing a match — cheapest; follow up "
                    "with read_file. 'content': matching lines with line "
                    "numbers and optional context (grep -n). 'count': number "
                    "of matching lines per file (grep -c)."
                ),
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Ignore case when matching (grep -i). Default false.",
            },
            "fixed_strings": {
                "type": "boolean",
                "description": (
                    "Treat `pattern` as a literal string instead of a regex "
                    "(grep -F). Use when the pattern contains regex "
                    "metacharacters to match literally. Default false."
                ),
            },
            "whole_word": {
                "type": "boolean",
                "description": (
                    "Match only whole words — the pattern must be bounded by "
                    "word boundaries (grep -w). 'log' will not match 'login'. "
                    "Default false."
                ),
            },
            "invert_match": {
                "type": "boolean",
                "description": (
                    "Select lines that do NOT match the pattern (grep -v). "
                    "Not supported together with multiline. Default false."
                ),
            },
            "multiline": {
                "type": "boolean",
                "description": (
                    "Let the pattern span multiple lines: '.' matches newlines "
                    "and each file is searched as one string. Use for patterns "
                    "like 'class\\s+\\w+.*?:'. Disables invert_match and "
                    "context. Default false."
                ),
            },
            "only_matching": {
                "type": "boolean",
                "description": (
                    "Return only the matched substring(s) rather than the whole "
                    "line (grep -o); emits one entry per occurrence. "
                    "output_mode='content' only. Default false."
                ),
            },
            "show_line_numbers": {
                "type": "boolean",
                "description": (
                    "Include 1-based line numbers in 'content' results "
                    "(grep -n). Default true."
                ),
            },
            "before_context": {
                "type": "integer",
                "description": (
                    "Lines of context to show BEFORE each match (grep -B). "
                    "output_mode='content' only. Default 0, capped at 50."
                ),
            },
            "after_context": {
                "type": "integer",
                "description": (
                    "Lines of context to show AFTER each match (grep -A). "
                    "output_mode='content' only. Default 0, capped at 50."
                ),
            },
            "context": {
                "type": "integer",
                "description": (
                    "Lines of context to show both before AND after each match "
                    "(grep -C); overrides before_context/after_context when "
                    "set. Default 0, capped at 50."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": (
                    "Maximum entries to return: matching lines for "
                    "output_mode='content', files for 'files_with_matches', or "
                    "per-file counts for 'count'. Defaults to 50, capped at the "
                    "server limit. When the cap is hit the response sets "
                    "truncated=true so you know more matches exist."
                ),
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        limits = _resolve_limits(arguments)

        pattern = arguments.get("pattern", "")
        if not isinstance(pattern, str) or pattern == "":
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "pattern is required.",
            })

        rel_path = arguments.get("path") or "."
        glob_pat = arguments.get("glob")
        type_filter = arguments.get("type")
        output_mode = arguments.get("output_mode", "files_with_matches")
        if output_mode not in ("files_with_matches", "content", "count"):
            output_mode = "files_with_matches"
        case_insensitive = bool(arguments.get("case_insensitive", False))
        fixed_strings = bool(arguments.get("fixed_strings", False))
        whole_word = bool(arguments.get("whole_word", False))
        invert_match = bool(arguments.get("invert_match", False))
        multiline = bool(arguments.get("multiline", False))
        only_matching = bool(arguments.get("only_matching", False))
        show_line_numbers = bool(arguments.get("show_line_numbers", True))

        if multiline and invert_match:
            return BuiltInToolResult(structured_content={
                "error": "Invalid combination",
                "message": "invert_match is not supported in multiline mode.",
            })

        def _as_nonneg_int(value: Any, fallback: int = 0) -> int:
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                return fallback

        if arguments.get("context") is not None:
            before = after = min(_as_nonneg_int(arguments.get("context")),
                                 MAX_GREP_CONTEXT_LINES)
        else:
            before = min(_as_nonneg_int(arguments.get("before_context")),
                         MAX_GREP_CONTEXT_LINES)
            after = min(_as_nonneg_int(arguments.get("after_context")),
                        MAX_GREP_CONTEXT_LINES)

        max_results = _as_nonneg_int(arguments.get("max_results"), DEFAULT_GREP_RESULTS)
        if max_results <= 0:
            max_results = DEFAULT_GREP_RESULTS
        max_results = min(max_results, limits["max_grep_results"])

        # Compile up front so a bad pattern is reported regardless of path.
        try:
            regex = _compile_grep_pattern(
                pattern,
                fixed_strings=fixed_strings,
                whole_word=whole_word,
                case_insensitive=case_insensitive,
                multiline=multiline,
            )
        except re.error as e:
            return BuiltInToolResult(structured_content={
                "error": "Invalid regex",
                "message": str(e),
                "pattern": pattern,
            })

        try:
            target = _safe_resolve(data_dir, rel_path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        if not os.path.exists(target):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": f"'{rel_path}' does not exist.",
            })

        glob_variants = _expand_braces(glob_pat) if glob_pat else None
        type_exts = _TYPE_EXTENSIONS.get(type_filter) if type_filter else None

        # The scan is CPU/IO-bound and synchronous; offload it so it doesn't
        # block the event loop serving other concurrent agent requests.
        payload = await asyncio.to_thread(
            self._run_search,
            data_dir=data_dir,
            target=target,
            rel_path=rel_path,
            regex=regex,
            pattern=pattern,
            glob_variants=glob_variants,
            type_exts=type_exts,
            output_mode=output_mode,
            invert_match=invert_match,
            multiline=multiline,
            only_matching=only_matching,
            before=before,
            after=after,
            show_line_numbers=show_line_numbers,
            max_results=max_results,
            limits=limits,
        )
        return BuiltInToolResult(structured_content=payload)

    def _walk_files(self, root, glob_variants, type_exts):
        """Yield candidate file paths under *root*, applying name filters.

        ``followlinks=False`` (the os.walk default) prevents directory-symlink
        loops from causing infinite recursion on any OS.
        """
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
            for name in filenames:
                if _grep_name_matches(name, glob_variants, type_exts):
                    yield os.path.join(dirpath, name)

    def _search_one_file(
        self,
        path: str,
        regex: "re.Pattern[str]",
        *,
        output_mode: str,
        invert_match: bool,
        multiline: bool,
        only_matching: bool,
        before: int,
        after: int,
        show_line_numbers: bool,
        max_line_len: int,
        entry_limit: int,
    ) -> Tuple[int, List[Dict[str, Any]], bool]:
        """Search one file. Returns ``(match_count, content_entries, more_in_file)``.

        For 'files_with_matches' the read short-circuits on the first hit; for
        'count' it streams and counts matching lines; for 'content' it reads the
        file (already size-capped) and builds up to ``entry_limit`` entries.
        ``more_in_file`` is True when 'content' stopped early with more to show.
        Raises OSError/UnicodeError to the caller, which counts it as unreadable.
        """
        if multiline:
            with open(path, "r", encoding="utf-8", errors="replace", newline=None) as f:
                text = f.read()
            if output_mode == "files_with_matches":
                return (1 if regex.search(text) else 0), [], False
            if output_mode == "count":
                return sum(1 for _ in regex.finditer(text)), [], False
            entries: List[Dict[str, Any]] = []
            more = False
            for m in regex.finditer(text):
                if len(entries) >= entry_limit:
                    more = True
                    break
                line_no = text.count("\n", 0, m.start()) + 1
                snippet, was_trunc = _truncate_line(m.group(0), max_line_len)
                entry: Dict[str, Any] = {}
                if show_line_numbers:
                    entry["line_number"] = line_no
                entry["line"] = snippet
                if was_trunc:
                    entry["line_truncated"] = True
                entries.append(entry)
            return len(entries), entries, more

        # --- Line-by-line modes (universal newlines unify CRLF/LF) ---
        if output_mode == "files_with_matches":
            with open(path, "r", encoding="utf-8", errors="replace", newline=None) as f:
                for line in f:
                    hit = bool(regex.search(line.rstrip("\n")))
                    if hit != invert_match:  # hit XOR invert
                        return 1, [], False
            return 0, [], False

        if output_mode == "count":
            count = 0
            with open(path, "r", encoding="utf-8", errors="replace", newline=None) as f:
                for line in f:
                    hit = bool(regex.search(line.rstrip("\n")))
                    if hit != invert_match:
                        count += 1
            return count, [], False

        # --- content mode ---
        with open(path, "r", encoding="utf-8", errors="replace", newline=None) as f:
            text = f.read()
        file_lines = text.split("\n")
        if file_lines and file_lines[-1] == "":
            file_lines.pop()  # drop the phantom trailing element from a final \n
        n = len(file_lines)
        content_entries: List[Dict[str, Any]] = []
        more = False

        # grep -o: one entry per matched substring, no context (only when not
        # inverted — there are no substrings to show for non-matching lines).
        if only_matching and not invert_match:
            for i, line in enumerate(file_lines):
                stop = False
                for m in regex.finditer(line):
                    if len(content_entries) >= entry_limit:
                        more = True
                        stop = True
                        break
                    seg, was_trunc = _truncate_line(m.group(0), max_line_len)
                    e: Dict[str, Any] = {}
                    if show_line_numbers:
                        e["line_number"] = i + 1
                    e["line"] = seg
                    if was_trunc:
                        e["line_truncated"] = True
                    content_entries.append(e)
                if stop:
                    break
            return len(content_entries), content_entries, more

        for i, line in enumerate(file_lines):
            hit = bool(regex.search(line))
            if hit == invert_match:  # not a match for this mode
                continue
            if len(content_entries) >= entry_limit:
                more = True
                break
            line_text, was_trunc = _truncate_line(line, max_line_len)
            e = {}
            if show_line_numbers:
                e["line_number"] = i + 1
            e["line"] = line_text
            if was_trunc:
                e["line_truncated"] = True
            if before:
                e["before"] = [
                    {"line_number": j + 1, "line": _truncate_line(file_lines[j], max_line_len)[0]}
                    for j in range(max(0, i - before), i)
                ]
            if after:
                e["after"] = [
                    {"line_number": j + 1, "line": _truncate_line(file_lines[j], max_line_len)[0]}
                    for j in range(i + 1, min(n, i + 1 + after))
                ]
            content_entries.append(e)
        return len(content_entries), content_entries, more

    def _run_search(
        self,
        *,
        data_dir: str,
        target: str,
        rel_path: str,
        regex: "re.Pattern[str]",
        pattern: str,
        glob_variants: Optional[List[str]],
        type_exts: Optional[Tuple[str, ...]],
        output_mode: str,
        invert_match: bool,
        multiline: bool,
        only_matching: bool,
        before: int,
        after: int,
        show_line_numbers: bool,
        max_results: int,
        limits: Dict[str, int],
    ) -> Dict[str, Any]:
        """Synchronous traversal + matching; builds the full structured payload."""
        base = os.path.realpath(data_dir)
        max_file_size = limits["max_grep_file_size"]
        max_line_len = limits["max_grep_match_line_length"]

        files_searched = 0
        files_skipped_binary = 0
        files_skipped_too_large = 0
        files_unreadable = 0
        truncated = False

        matches: List[Dict[str, Any]] = []
        files_list: List[str] = []
        counts: List[Dict[str, Any]] = []
        total_matches = 0

        # A single named file is searched directly (name filters don't apply
        # when the user points at one file); a directory is walked recursively.
        if os.path.isfile(target):
            candidates: Any = [target]
        else:
            candidates = self._walk_files(target, glob_variants, type_exts)

        def _rel(p: str) -> str:
            return _report_path(p, base)

        for file_path in candidates:
            if files_searched >= MAX_GREP_FILES_SCANNED:
                truncated = True
                break

            try:
                size = os.path.getsize(file_path)
            except OSError:
                files_unreadable += 1
                continue
            if size > max_file_size:
                files_skipped_too_large += 1
                continue
            try:
                if _is_binary(file_path):
                    files_skipped_binary += 1
                    continue
            except OSError:
                files_unreadable += 1
                continue

            entry_limit = (max_results - len(matches)) if output_mode == "content" else 0
            try:
                count, entries, more = self._search_one_file(
                    file_path, regex,
                    output_mode=output_mode,
                    invert_match=invert_match,
                    multiline=multiline,
                    only_matching=only_matching,
                    before=before,
                    after=after,
                    show_line_numbers=show_line_numbers,
                    max_line_len=max_line_len,
                    entry_limit=entry_limit,
                )
            except (OSError, UnicodeError):
                files_unreadable += 1
                continue

            files_searched += 1
            if count == 0:
                continue

            rel = _rel(file_path)
            if output_mode == "files_with_matches":
                files_list.append(rel)
                total_matches += 1
                if len(files_list) >= max_results:
                    truncated = True
                    break
            elif output_mode == "count":
                counts.append({"path": rel, "count": count})
                total_matches += count
                if len(counts) >= max_results:
                    truncated = True
                    break
            else:  # content
                for entry in entries:
                    matches.append({"path": rel, **entry})
                total_matches += len(entries)
                if more or len(matches) >= max_results:
                    truncated = True
                    break

        payload: Dict[str, Any] = {
            "output_mode": output_mode,
            "pattern": pattern,
            "search_root": rel_path,
            "truncated": truncated,
            "total_matches": total_matches,
            "max_results": max_results,
            "files_searched": files_searched,
            "files_skipped_binary": files_skipped_binary,
            "files_skipped_too_large": files_skipped_too_large,
            "files_unreadable": files_unreadable,
        }
        if output_mode == "files_with_matches":
            payload["total_files"] = len(files_list)
            payload["files"] = files_list
        elif output_mode == "count":
            payload["total_files"] = len(counts)
            payload["counts"] = counts
        else:
            payload["matches"] = matches
        payload["truncation_note"] = (
            f"Result limit ({max_results}) reached; more matches exist. Narrow "
            "the pattern, add a glob/type filter, or raise max_results."
            if truncated else None
        )
        return payload


class ListFilesTool(BuiltInTool):
    name: str = "list_files"
    description: str = (
        "List files and directories inside the user's data directory. "
        "Returns names, sizes, types, and modification dates. "
        "E.g. 'list all PDFs in the root'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Optional sub-path to list. " + _ABS_PATH_NOTE + " Defaults "
                    "to '.' (the current working directory itself)."
                ),
            },
            "pattern": {
                "type": "string",
                "description": "Optional glob pattern to filter results (e.g. '*.pdf').",
            },
        },
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        limits = _resolve_limits(arguments)
        rel_path = arguments.get("path", ".")
        pattern = arguments.get("pattern")

        try:
            target = _safe_resolve(data_dir, rel_path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})

        if not os.path.isdir(target):
            return BuiltInToolResult(structured_content={
                "error": "Not a directory",
                "message": f"'{rel_path}' is not a directory.",
            })

        entries = []
        try:
            with os.scandir(target) as it:
                for entry in it:
                    if pattern and not fnmatch.fnmatch(entry.name, pattern):
                        continue
                    try:
                        stat = entry.stat()
                        entries.append({
                            "name": entry.name,
                            "type": "directory" if entry.is_dir() else "file",
                            "size": stat.st_size if entry.is_file() else None,
                            "size_human": _format_size(stat.st_size) if entry.is_file() else None,
                            "mime_type": _guess_mime(entry.path) if entry.is_file() else None,
                            "modified": _format_timestamp(stat.st_mtime),
                        })
                    except OSError:
                        entries.append({"name": entry.name, "type": "unknown", "error": "cannot stat"})
                    if len(entries) >= limits["max_list_entries"]:
                        break
        except PermissionError:
            return BuiltInToolResult(structured_content={
                "error": "Permission denied",
                "message": f"Cannot read directory '{rel_path}'.",
            })

        return BuiltInToolResult(structured_content={
            "path": rel_path,
            "os": platform.system(),
            "total_entries": len(entries),
            "entries": entries,
        })


class GetFileInfoTool(BuiltInTool):
    name: str = "get_file_info"
    description: str = (
        "Get detailed metadata about a single file: name, size, MIME type, "
        "modification date, whether it is binary, its extension, and — for "
        "image files — its pixel width and height (when available). "
        "E.g. 'get info about ./report.pdf' or 'what are the dimensions of ./photo.png'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file. " + _ABS_PATH_NOTE,
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        rel_path = arguments.get("path", "")
        if not rel_path:
            return BuiltInToolResult(structured_content={"error": "Missing parameter", "message": "path is required."})

        try:
            target = _safe_resolve(data_dir, rel_path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})

        if not os.path.exists(target):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": f"'{rel_path}' does not exist."
                           + _relative_abs_hint(rel_path, data_dir, arguments),
            })

        try:
            stat = os.stat(target)
        except OSError as e:
            return BuiltInToolResult(structured_content={"error": "OS error", "message": str(e)})

        mime = _guess_mime(target)
        binary = _is_binary(target) if os.path.isfile(target) else False
        ext = os.path.splitext(target)[1]

        info: Dict[str, Any] = {
            "name": os.path.basename(target),
            "path": rel_path,
            "size": stat.st_size,
            "size_human": _format_size(stat.st_size),
            "mime_type": mime,
            "is_binary": binary,
            "is_directory": os.path.isdir(target),
            "extension": ext,
            "modified": _format_timestamp(stat.st_mtime),
            "os": platform.system(),
        }

        if os.path.isfile(target):
            width, height = _image_dimensions(target, mime)
            if width is not None and height is not None:
                info["width"] = width
                info["height"] = height
                info["dimensions"] = f"{width}x{height}"

        return BuiltInToolResult(structured_content=info)


class ReadFileTool(BuiltInTool):
    name: str = "read_file"
    description: str = (
        "Read the contents of a file. For small text/document files the "
        "content is returned as markdown text. For binary files, large files, "
        "or when readable=false, only file metadata is returned and the file "
        "is sent to the frontend for display. E.g. 'read ./report.pdf'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the file. " + _ABS_PATH_NOTE,
            },
            "readable": {
                "type": "boolean",
                "description": (
                    "If true (default), attempt to convert the file to readable "
                    "markdown text. If false, treat as non-readable regardless of type."
                ),
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        limits = _resolve_limits(arguments)
        rel_path = arguments.get("path", "")
        readable = arguments.get("readable", True)

        if not rel_path:
            return BuiltInToolResult(structured_content={"error": "Missing parameter", "message": "path is required."})

        try:
            target = _safe_resolve(data_dir, rel_path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})

        if not os.path.isfile(target):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": f"'{rel_path}' is not a file or does not exist."
                           + _relative_abs_hint(rel_path, data_dir, arguments),
            })

        mime = _guess_mime(target)
        file_size = os.path.getsize(target)
        file_name = os.path.basename(target)
        file_uri = target.replace(os.sep, "/")
        file_entry: ToolResultFile = {
            "uri": file_uri,
            "name": file_name,
            "mime_type": mime,
        }

        def _result(text: str) -> BuiltInToolResult:
            """Build a ToolResultWithFiles response."""
            payload: ToolResultWithFiles = {"text": text, "_files": [file_entry]}
            return BuiltInToolResult(structured_content=payload)

        # Non-readable fast path
        if not readable:
            return _result(_file_description(file_name, mime, file_uri))

        binary = _is_binary(target)

        # Binary file that markitdown cannot handle -> non-readable
        if binary and not _is_markitdown_supported(mime):
            return _result(_file_description(file_name, mime, file_uri))

        # Too large for markitdown processing
        if file_size > limits["max_markitdown_file_size"]:
            return _result(_file_description(file_name, mime, file_uri))

        # Attempt markitdown conversion
        md_content = None
        converter = None
        markitdown_import_error: str | None = None
        try:
            converter = _get_markitdown()
        except ImportError as e:
            # Binary files that only markitdown can read must surface a
            # structured error; text files can fall through to the raw-read
            # branch below, so we record the failure and continue.
            markitdown_import_error = str(e)

        if converter and (_is_markitdown_supported(mime) or not binary):
            try:
                result = converter.convert(target)
                md_content = result.text_content if result and result.text_content else None
            except Exception as e:
                logger.warning(f"[ReadFileTool] markitdown conversion failed for {file_name}: {e}")

        # Binary file that only markitdown could parse but markitdown is
        # missing — give the agent a real error instead of a useless
        # "binary file description" placeholder.
        if markitdown_import_error and binary and _is_markitdown_supported(mime):
            return missing_dependency_result(
                tool="read_file",
                feature_key="documents",
                extras=("documents",),
                detail=(
                    f"Cannot extract text from '{file_name}' ({mime}) -- "
                    f"markitdown is required for this file type ({markitdown_import_error})."
                ),
            )

        logger.debug(f"[ReadFileTool] file={file_name}, mime={mime}, binary={binary}, "
                     f"size={file_size}, md_content_length={len(md_content) if md_content else 0}")
        # Fallback: read raw text for plain text files
        if md_content is None and not binary:
            try:
                with open(target, "r", encoding="utf-8", errors="replace") as f:
                    md_content = f.read()
            except OSError as e:
                logger.warning(f"[ReadFileTool] Failed to read {file_name}: {e}")

        # If we still have no content, treat as non-readable
        if not md_content:
            return _result(_file_description(file_name, mime, file_uri))

        # Token check
        encoder = _get_encoder()
        token_count = len(encoder.encode(md_content))

        logger.debug(f"[ReadFileTool] file={file_name}, token_count={token_count}, "
                     f"max_readable={limits['max_readable_tokens']}")

        if token_count > limits["max_readable_tokens"]:
            return _result(_file_description(file_name, mime, file_uri))

        # Readable content -- include text for the LLM and file meta for the frontend
        return _result(md_content)


class WriteFileTool(BuiltInTool):
    name: str = "write_file"
    description: str = (
        "Write plain text content to a human-readable text file inside the "
        "user's data directory. Creates parent directories as needed. "
        "Only text file types are allowed (e.g. .txt, .md, .json, .csv). "
        "E.g. 'write a summary to ./notes/summary.txt'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Output file path. " + _ABS_PATH_NOTE + " Must have a text "
                    "file extension (e.g. .txt, .md, .json). To move or copy a "
                    "binary file such as an image or PDF, use move_file / "
                    "copy_file instead."
                ),
            },
            "content": {
                "type": "string",
                "description": "Plain text content to write to the file.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        limits = _resolve_limits(arguments)
        rel_path = arguments.get("path", "")
        content = arguments.get("content", "")

        if not rel_path:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "path is required.",
            })

        try:
            target = _safe_resolve(data_dir, rel_path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        mime = _guess_mime(target)
        if not _is_text_mime(mime):
            return BuiltInToolResult(structured_content={
                "error": "Unsupported file type",
                "message": (
                    f"'{os.path.basename(target)}' has MIME type '{mime}' which is "
                    "not a human-readable text format. Only text files are allowed."
                ),
            })

        encoder = _get_encoder()
        token_count = len(encoder.encode(content))
        if token_count > limits["max_readable_tokens"]:
            return BuiltInToolResult(structured_content={
                "error": "Content too large",
                "message": (
                    f"Content has {token_count} tokens, which exceeds the "
                    f"maximum of {limits['max_readable_tokens']} tokens."
                ),
            })

        os.makedirs(os.path.dirname(target), exist_ok=True)

        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            logger.error(f"[WriteFileTool] write failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Write error",
                "message": f"Failed to write file: {e}",
            })

        file_name = os.path.basename(target)
        file_uri = target.replace(os.sep, "/")
        base = os.path.realpath(data_dir)
        rel_output = _report_path(target, base)

        file_entry: ToolResultFile = {
            "uri": file_uri,
            "name": file_name,
            "mime_type": mime,
        }

        payload: ToolResultWithFiles = {
            "text": (
                f"Wrote '{file_name}' ({len(content)} characters).\n"
                f"Output: {rel_output}"
            ),
            "_files": [file_entry],
        }

        logger.info(
            f"[WriteFileTool] wrote {rel_output} ({len(content)} chars)"
        )

        return BuiltInToolResult(structured_content=payload)


class OverwriteFileTool(BuiltInTool):
    name: str = "overwrite_file"
    description: str = (
        "Edit part of an EXISTING human-readable text file in place by applying a "
        "unified diff. Use this to change a few lines without rewriting the whole "
        "file; to create a new file or replace it entirely use write_file. First "
        "read the file with read_file (or locate lines with grep_files) so the "
        "diff's context and removed ('-') lines EXACTLY match the current text, "
        "whitespace included. The '@@ -a,b +c,d @@' header line numbers are only a "
        "hint - lines are matched by content. Provide both `path` and `diff`.\n"
        "E.g. path='conversation.txt', diff='@@ -2,1 +2,1 @@\\n"
        "-Steve: see you at 3pm\\n+James: see you at 3pm'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path to the existing text file to edit in place. "
                    + _ABS_PATH_NOTE + " Must be a human-readable text file that "
                    "already exists. To create or fully replace a file use "
                    "write_file; to move or copy a file use move_file / copy_file."
                ),
            },
            "diff": {
                "type": "string",
                "description": (
                    "A unified diff to apply. Each hunk may start with an "
                    "'@@ -a,b +c,d @@' header; body lines are prefixed with ' ' "
                    "(unchanged context), '-' (removed) or '+' (added). The "
                    "context and '-' lines must match the current file exactly; "
                    "the header line numbers are only a hint since lines are "
                    "matched by content. Copy the file's text verbatim - do not "
                    "add backslash escapes to quotes or other characters. A bare "
                    "body with no '@@' header is accepted as a single "
                    "content-matched hunk. E.g. "
                    "'@@ -2,1 +2,1 @@\\n-old line\\n+new line'."
                ),
            },
        },
        "required": ["path", "diff"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        rel_path = arguments.get("path", "")
        diff = arguments.get("diff", "")

        # --- Validate required params (before touching the filesystem) ---
        if not rel_path:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": (
                    "path is required. Call as: "
                    'overwrite_file path="<file>" diff="<unified diff>".'
                ),
            })

        if not diff:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "diff is required.",
            })

        # --- Resolve path ---
        try:
            target = _safe_resolve(data_dir, rel_path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        # --- File must already exist (edit-in-place, never create) ---
        if not os.path.isfile(target):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": (
                    f"File '{rel_path}' does not exist or is not a file. "
                    "To create a new file, use write_file instead."
                ),
            })

        mime = _guess_mime(target)
        if not _is_text_mime(mime) or _is_binary(target):
            return BuiltInToolResult(structured_content={
                "error": "Unsupported file type",
                "message": (
                    f"'{os.path.basename(target)}' is not a human-readable text "
                    "file. Only text files can be edited with a diff."
                ),
            })

        # --- Parse the diff up front so a malformed diff fails clearly ---
        try:
            hunks = _parse_unified_diff(diff)
        except ValueError:
            return BuiltInToolResult(structured_content={
                "error": "Invalid diff",
                "message": (
                    "No applicable hunks found. Provide a unified diff: an "
                    "'@@ -a,b +c,d @@' header followed by ' ' context, '-' removed "
                    "and '+' added lines (a bare '-'/'+' body is also accepted)."
                ),
            })

        # --- Read existing file (universal newlines unify CRLF/LF) ---
        try:
            with open(target, "r", encoding="utf-8", errors="replace", newline=None) as f:
                text = f.read()
        except OSError as e:
            return BuiltInToolResult(structured_content={
                "error": "Read error",
                "message": f"Failed to read file: {e}",
            })

        # Split to terminator-free lines, remembering the trailing-newline state
        # so it can be restored on write (mirrors GrepFilesTool's content read).
        file_lines = text.split("\n")
        had_trailing_newline = bool(file_lines) and file_lines[-1] == ""
        if had_trailing_newline:
            file_lines.pop()

        # --- Apply the hunks (matched by content; header numbers are a hint) ---
        try:
            new_lines, lines_changed = _apply_hunks(file_lines, hunks)
        except ValueError as e:
            code, _, which = str(e).partition(":")
            if code == "Ambiguous diff":
                message = (
                    f"Hunk {which} matches more than one place in the file. Add "
                    "more surrounding context lines to the diff so the target is "
                    "unique."
                )
            else:
                message = (
                    f"Hunk {which} did not apply: its context/removed lines do not "
                    "match the current file. Re-read the file with read_file and "
                    "copy the exact lines (including whitespace) into the diff."
                )
            return BuiltInToolResult(structured_content={
                "error": code,
                "message": message,
            })

        new_text = "\n".join(new_lines)
        if had_trailing_newline:
            new_text += "\n"

        # --- Write back ---
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(new_text)
        except OSError as e:
            logger.error(f"[OverwriteFileTool] write failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Write error",
                "message": f"Failed to write file: {e}",
            })

        file_name = os.path.basename(target)
        file_uri = target.replace(os.sep, "/")
        base = os.path.realpath(data_dir)
        rel_output = _report_path(target, base)

        file_entry: ToolResultFile = {
            "uri": file_uri,
            "name": file_name,
            "mime_type": mime,
        }

        payload: ToolResultWithFiles = {
            "text": (
                f"Applied {len(hunks)} hunk(s) to '{file_name}' "
                f"({lines_changed} line(s) changed, file now "
                f"{len(new_lines)} lines).\n"
                f"Output: {rel_output}"
            ),
            "_files": [file_entry],
        }

        logger.info(
            f"[OverwriteFileTool] {rel_output}: applied {len(hunks)} hunk(s), "
            f"{lines_changed} line(s) changed"
        )

        return BuiltInToolResult(structured_content=payload)


def _resolve_relocation(
    data_dir: str,
    source_path: str,
    destination_path: str,
    overwrite: bool,
    arguments: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[BuiltInToolResult]]:
    """Resolve + validate a move/copy. Returns ``(src, final_target, error)``.

    Both endpoints are passed through ``_safe_resolve`` (so neither can escape
    the working dir / allowed roots), the source must exist, and the
    destination is interpreted as a target directory (keep the source's name)
    when it is an existing directory, otherwise as the full target path. On any
    failure ``src``/``final_target`` are ``None`` and ``error`` is a ready
    ``BuiltInToolResult``; on success ``error`` is ``None``.
    """
    roots = _allowed_roots(arguments, data_dir)

    try:
        src = _safe_resolve(data_dir, source_path, roots)
    except ValueError as e:
        return None, None, BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})

    if not os.path.exists(src):
        return None, None, BuiltInToolResult(structured_content={
            "error": "Not found",
            "message": f"Source '{source_path}' does not exist."
                       + _relative_abs_hint(source_path, data_dir, arguments),
        })

    try:
        dst = _safe_resolve(data_dir, destination_path, roots)
    except ValueError as e:
        return None, None, BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})

    if os.path.isdir(dst):
        # Destination is an existing folder → move/copy the source inside it,
        # keeping its name. Re-validate the joined path stays inside a root.
        try:
            final_target = _safe_resolve(
                data_dir, os.path.join(dst, os.path.basename(src)), roots)
        except ValueError as e:
            return None, None, BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})
    else:
        final_target = dst

    if os.path.realpath(src) == os.path.realpath(final_target):
        return None, None, BuiltInToolResult(structured_content={
            "error": "Same file",
            "message": "Source and destination are the same file.",
        })

    if os.path.exists(final_target) and not overwrite:
        return None, None, BuiltInToolResult(structured_content={
            "error": "Destination exists",
            "message": (
                f"'{_report_path(final_target, os.path.realpath(data_dir))}' already "
                "exists. Pass overwrite=true to replace it."
            ),
        })

    return src, final_target, None


def _relocation_result(
    src: str, final_target: str, data_dir: str, verb: str,
) -> BuiltInToolResult:
    """Build the success result for a move/copy, mirroring ``write_file``'s shape."""
    base = os.path.realpath(data_dir)
    rel_output = _report_path(final_target, base)
    text = f"{verb} '{os.path.basename(src)}' to {rel_output}."
    if os.path.isdir(final_target):
        return BuiltInToolResult(structured_content={"text": text})
    file_entry: ToolResultFile = {
        "uri": final_target.replace(os.sep, "/"),
        "name": os.path.basename(final_target),
        "mime_type": _guess_mime(final_target),
    }
    payload: ToolResultWithFiles = {"text": text, "_files": [file_entry]}
    return BuiltInToolResult(structured_content=payload)


class MoveFileTool(BuiltInTool):
    name: str = "move_file"
    description: str = (
        "Move or rename a file or directory. Works for ANY file type, including "
        "binary files such as images, PDFs and archives (unlike write_file, which "
        "is text-only). Use this to relocate an uploaded/attached file into the "
        "user's working directory, or to rename a file. If the destination is an "
        "existing directory the item is moved inside it keeping its name; "
        "otherwise the destination is treated as the full target path (a rename). "
        "E.g. source_path='C:/Users/you/.cremind/admin/uploads_tmp/<id>/photo.png', "
        "destination_path='C:/Users/you/Documents'."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "source_path": {
                "type": "string",
                "description": "The file or directory to move. " + _ABS_PATH_NOTE,
            },
            "destination_path": {
                "type": "string",
                "description": (
                    "Where to move it: an existing directory (the source keeps "
                    "its name) or a full target path (a rename). " + _ABS_PATH_NOTE
                ),
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "If true, replace an existing destination file. Default false "
                    "— the move fails if the destination already exists."
                ),
            },
        },
        "required": ["source_path", "destination_path"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        source_path = (arguments.get("source_path") or "").strip()
        destination_path = (arguments.get("destination_path") or "").strip()
        overwrite = bool(arguments.get("overwrite", False))

        if not source_path or not destination_path:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "Both source_path and destination_path are required.",
            })

        src, final_target, error = _resolve_relocation(
            data_dir, source_path, destination_path, overwrite, arguments)
        if error is not None:
            return error

        # Replace an existing FILE target first: os.rename (used by shutil.move
        # on a same-filesystem move) refuses to clobber on Windows. Never remove
        # a directory here.
        if overwrite and os.path.isfile(final_target):
            try:
                os.remove(final_target)
            except OSError as e:
                return BuiltInToolResult(structured_content={
                    "error": "Move error",
                    "message": f"Failed to replace destination: {e}",
                })

        parent = os.path.dirname(final_target)
        if parent:
            os.makedirs(parent, exist_ok=True)

        try:
            shutil.move(src, final_target)
        except (OSError, shutil.Error) as e:
            logger.error(f"[MoveFileTool] move failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Move error",
                "message": f"Failed to move file: {e}",
            })

        logger.info(f"[MoveFileTool] moved {source_path} -> {final_target}")
        return _relocation_result(src, final_target, data_dir, verb="Moved")


class CopyFileTool(BuiltInTool):
    name: str = "copy_file"
    description: str = (
        "Duplicate a file, leaving the original in place. Works for ANY file "
        "type, including binary files such as images and PDFs. If the destination "
        "is an existing directory the copy is placed inside it keeping the "
        "source's name; otherwise the destination is the full target path. "
        "Copies a single file only — to relocate a whole directory use move_file."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "source_path": {
                "type": "string",
                "description": "The file to copy. " + _ABS_PATH_NOTE,
            },
            "destination_path": {
                "type": "string",
                "description": (
                    "Where to put the copy: an existing directory (the copy keeps "
                    "the source's name) or a full target path. " + _ABS_PATH_NOTE
                ),
            },
            "overwrite": {
                "type": "boolean",
                "description": (
                    "If true, replace an existing destination file. Default false "
                    "— the copy fails if the destination already exists."
                ),
            },
        },
        "required": ["source_path", "destination_path"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        source_path = (arguments.get("source_path") or "").strip()
        destination_path = (arguments.get("destination_path") or "").strip()
        overwrite = bool(arguments.get("overwrite", False))

        if not source_path or not destination_path:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "Both source_path and destination_path are required.",
            })

        src, final_target, error = _resolve_relocation(
            data_dir, source_path, destination_path, overwrite, arguments)
        if error is not None:
            return error

        if os.path.isdir(src):
            return BuiltInToolResult(structured_content={
                "error": "Unsupported",
                "message": (
                    "copy_file copies a single file, not a directory. Use "
                    "move_file to relocate a directory, or copy its files individually."
                ),
            })

        if overwrite and os.path.isfile(final_target):
            try:
                os.remove(final_target)
            except OSError as e:
                return BuiltInToolResult(structured_content={
                    "error": "Copy error",
                    "message": f"Failed to replace destination: {e}",
                })

        parent = os.path.dirname(final_target)
        if parent:
            os.makedirs(parent, exist_ok=True)

        try:
            shutil.copy2(src, final_target)
        except (OSError, shutil.Error) as e:
            logger.error(f"[CopyFileTool] copy failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Copy error",
                "message": f"Failed to copy file: {e}",
            })

        logger.info(f"[CopyFileTool] copied {source_path} -> {final_target}")
        return _relocation_result(src, final_target, data_dir, verb="Copied")


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server.

    The three file-watcher subtools (register/list/delete) used to be a
    standalone "Register File Watcher" tool; they are folded in here so file
    watching lives under the same always-on System File tool. The watcher
    classes resolve their own context (``_profile``/``_context_id`` injected by
    the adapter) and need no ``data_dir``.
    """
    from app.tools.builtin.register_file_watcher import (
        DeleteFileWatcherTool,
        ListFileWatchersTool,
        RegisterFileWatcherTool,
    )

    data_dir = config.get("CREMIND_SYSTEM_DIR", os.path.join(os.path.expanduser("~"), ".cremind"))
    return [
        SearchFilesTool(data_dir=data_dir),
        GrepFilesTool(data_dir=data_dir),
        ListFilesTool(data_dir=data_dir),
        GetFileInfoTool(data_dir=data_dir),
        ReadFileTool(data_dir=data_dir),
        WriteFileTool(data_dir=data_dir),
        OverwriteFileTool(data_dir=data_dir),
        MoveFileTool(data_dir=data_dir),
        CopyFileTool(data_dir=data_dir),
        RegisterFileWatcherTool(),
        ListFileWatchersTool(),
        DeleteFileWatcherTool(),
    ]


SERVER_NAME = "System File"

TOOL_CONFIG: ToolConfig = {
    "name": "system_file",
    "display_name": "System File",
    # Visible in Settings (so its token/size limits can be configured) but
    # locked on — a core capability the user must not disable.
    "locked": True,
    "required_config": {
        Var.MAX_READABLE_TOKENS: {
            "description": (
                "Maximum tokens of file content returned inline to the agent "
                "before falling back to a metadata-only response. Default: 1000."
            ),
            "type": "number",
            "default": MAX_READABLE_TOKENS,
        },
        Var.MAX_MARKITDOWN_FILE_SIZE: {
            "description": (
                "Maximum file size in bytes that markitdown will attempt to "
                "convert. Larger files return metadata only. Default: 10485760 (10 MB)."
            ),
            "type": "number",
            "default": MAX_MARKITDOWN_FILE_SIZE,
        },
        Var.MAX_LIST_ENTRIES: {
            "description": (
                "Maximum number of entries returned by list_files in one call. "
                "Default: 100."
            ),
            "type": "number",
            "default": MAX_LIST_ENTRIES,
        },
        Var.MAX_SEARCH_RESULTS: {
            "description": (
                "Hard cap on results returned by search_files (the per-call "
                "max_results argument is clamped to this). Default: 100."
            ),
            "type": "number",
            "default": MAX_SEARCH_RESULTS,
        },
        Var.MAX_GREP_RESULTS: {
            "description": (
                "Hard cap on the number of match entries returned by grep_files "
                "(the per-call max_results argument is clamped to this). "
                "Default: 100."
            ),
            "type": "number",
            "default": MAX_GREP_RESULTS,
        },
        Var.MAX_GREP_FILE_SIZE: {
            "description": (
                "Maximum file size in bytes that grep_files will read; larger "
                "files are skipped. Default: 5242880 (5 MB)."
            ),
            "type": "number",
            "default": MAX_GREP_FILE_SIZE,
        },
        Var.MAX_GREP_MATCH_LINE_LENGTH: {
            "description": (
                "Maximum characters of a single matched or context line "
                "returned by grep_files; longer lines are truncated. "
                "Default: 1000."
            ),
            "type": "number",
            "default": MAX_GREP_MATCH_LINE_LENGTH,
        },
    },
}


def _make_server_instructions(_data_dir: str) -> str:
    return (
        "File management inside the conversation's "
        "current working directory; a relative `path` is relative to that "
        "directory and defaults to '.' when omitted, while an absolute path is "
        "used as-is — pass an absolute path (e.g. an attached/uploaded file) "
        "EXACTLY as given, never relativized. Find files by name "
        "with search_files and search file contents by regex with grep_files. "
        "Edit part of an existing file with overwrite_file (a unified diff); "
        "create or replace a whole file with write_file. Move or rename a file "
        "(any type, including binary) with move_file and duplicate one with "
        "copy_file. "
        "Also registers "
        "file/folder watchers that notify you or re-run an action whenever "
        "files are created, modified, deleted, or moved on disk."
    )
