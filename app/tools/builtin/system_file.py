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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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

def _safe_resolve(data_dir: str, relative_path: str) -> str:
    """Resolve *relative_path* inside data_dir.  Raises ValueError on traversal."""
    base = os.path.realpath(data_dir)
    os.makedirs(base, exist_ok=True)
    # Strip leading slashes so "/" or "/foo" are treated as relative to data_dir
    relative_path = relative_path.lstrip("/\\")
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"Path traversal detected: {relative_path}")
    return target


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
        "file name. E.g. 'find the file open claw' will match 'OpenClaw.pdf'."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search query. Split into keywords; a file matches if ALL "
                    "keywords appear in its name (case-insensitive). "
                    "E.g. 'open claw' matches 'OpenClaw.pdf'."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Optional sub-path **relative to the current working "
                    "directory** to start searching from. Defaults to '.' "
                    "(the current working directory itself). Do not repeat "
                    "the working directory's own name here — e.g. if the "
                    "cwd is '.../Lee', use '.' or omit, not 'Lee'."
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
            search_root = _safe_resolve(data_dir, rel_path)
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
                entry_rel = os.path.relpath(full_path, base)

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
                    "Optional sub-path **relative to the current working "
                    "directory** to search. May be a directory (searched "
                    "recursively) or a single file. Defaults to '.' (the "
                    "current working directory). Do not repeat the working "
                    "directory's own name here."
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
            target = _safe_resolve(data_dir, rel_path)
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
            return os.path.relpath(p, base).replace(os.sep, "/")

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
                    "Optional sub-path **relative to the current working "
                    "directory** to list. Defaults to '.' (the current "
                    "working directory itself). Do not repeat the working "
                    "directory's own name here — e.g. if the cwd is "
                    "'.../Lee', use '.' or omit, not 'Lee'."
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
            target = _safe_resolve(data_dir, rel_path)
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
        "modification date, whether it is binary, and its extension. "
        "E.g. 'get info about ./report.pdf'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Relative path to the file within the data directory.",
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
            target = _safe_resolve(data_dir, rel_path)
        except ValueError as e:
            return BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})

        if not os.path.exists(target):
            return BuiltInToolResult(structured_content={"error": "Not found", "message": f"'{rel_path}' does not exist."})

        try:
            stat = os.stat(target)
        except OSError as e:
            return BuiltInToolResult(structured_content={"error": "OS error", "message": str(e)})

        mime = _guess_mime(target)
        binary = _is_binary(target) if os.path.isfile(target) else False
        ext = os.path.splitext(target)[1]

        return BuiltInToolResult(structured_content={
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
        })


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
                "description": "Relative path to the file within the data directory.",
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
            target = _safe_resolve(data_dir, rel_path)
        except ValueError as e:
            return BuiltInToolResult(structured_content={"error": "Access denied", "message": str(e)})

        if not os.path.isfile(target):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": f"'{rel_path}' is not a file or does not exist.",
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
                    "Relative path within the data directory for the output file. "
                    "Must have a text file extension (e.g. .txt, .md, .json)."
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
            target = _safe_resolve(data_dir, rel_path)
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
        rel_output = os.path.relpath(target, base).replace(os.sep, "/")

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


class WriteFileFromReferenceTool(BuiltInTool):
    name: str = "write_file_from_reference"
    description: str = (
        "Extract a region from an existing human-readable text file and write "
        "it to a new file. The region is specified with 1-based line and column "
        "coordinates. Both the reference file and the output file must be "
        "human-readable text types.\n"
        "E.g. 'extract lines 10-20 from <absolute_path>/data.csv into <absolute_path>/snippet.csv'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "reference_path": {
                "type": "string",
                "description": (
                    "Relative path to the source text file to extract from. "
                    "Must be a human-readable text file."
                ),
            },
            "start_line": {
                "type": "integer",
                "description": "1-based start line number (inclusive).",
            },
            "end_line": {
                "type": "integer",
                "description": "1-based end line number (inclusive).",
            },
            "path": {
                "type": "string",
                "description": (
                    "Relative path within the data directory for the output file. "
                    "Must have a text file extension."
                ),
            },
        },
        "required": [
            "reference_path", "start_line", "end_line", "path",
        ],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        ref_path = arguments.get("reference_path", "")
        start_line = arguments.get("start_line", 0)
        start_col = arguments.get("start_column", 0)
        end_line = arguments.get("end_line", 0)
        end_col = arguments.get("end_column", 0)
        out_path = arguments.get("path", "")

        # --- Validate required params ---
        if not ref_path or not out_path:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "reference_path and path are both required.",
            })

        if start_line < 1 or start_col < 1 or end_line < 1 or end_col < 1:
            return BuiltInToolResult(structured_content={
                "error": "Invalid coordinates",
                "message": "All line/column values must be >= 1 (1-based).",
            })

        if (end_line, end_col) < (start_line, start_col):
            return BuiltInToolResult(structured_content={
                "error": "Invalid range",
                "message": "End position must be at or after start position.",
            })

        # --- Resolve paths ---
        try:
            ref_target = _safe_resolve(data_dir, ref_path)
            out_target = _safe_resolve(data_dir, out_path)
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        # --- Validate reference file ---
        if not os.path.isfile(ref_target):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": f"Reference file '{ref_path}' does not exist or is not a file.",
            })

        ref_mime = _guess_mime(ref_target)
        if not _is_text_mime(ref_mime) or _is_binary(ref_target):
            return BuiltInToolResult(structured_content={
                "error": "Unsupported file type",
                "message": (
                    f"Reference file '{os.path.basename(ref_target)}' is not a "
                    "human-readable text file."
                ),
            })

        # --- Validate output MIME ---
        out_mime = _guess_mime(out_target)
        if not _is_text_mime(out_mime):
            return BuiltInToolResult(structured_content={
                "error": "Unsupported file type",
                "message": (
                    f"Output file '{os.path.basename(out_target)}' has MIME type "
                    f"'{out_mime}' which is not a human-readable text format."
                ),
            })

        # --- Read reference file ---
        try:
            with open(ref_target, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError as e:
            return BuiltInToolResult(structured_content={
                "error": "Read error",
                "message": f"Failed to read reference file: {e}",
            })

        if start_line > len(lines):
            return BuiltInToolResult(structured_content={
                "error": "Out of range",
                "message": (
                    f"start_line ({start_line}) exceeds file length "
                    f"({len(lines)} lines)."
                ),
            })

        # Clamp end_line to file length
        end_line = min(end_line, len(lines))

        # --- Extract region (1-based coords) ---
        selected = lines[start_line - 1 : end_line]

        if not selected:
            extracted = ""
        elif len(selected) == 1:
            extracted = selected[0][start_col - 1 : end_col - 1]
        else:
            selected[0] = selected[0][start_col - 1 :]
            selected[-1] = selected[-1][: end_col - 1]
            extracted = "".join(selected)

        # --- Write output ---
        os.makedirs(os.path.dirname(out_target), exist_ok=True)

        try:
            with open(out_target, "w", encoding="utf-8") as f:
                f.write(extracted)
        except OSError as e:
            logger.error(f"[WriteFileFromReferenceTool] write failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Write error",
                "message": f"Failed to write output file: {e}",
            })

        out_name = os.path.basename(out_target)
        file_uri = out_target.replace(os.sep, "/")
        base = os.path.realpath(data_dir)
        rel_output = os.path.relpath(out_target, base).replace(os.sep, "/")

        file_entry: ToolResultFile = {
            "uri": file_uri,
            "name": out_name,
            "mime_type": out_mime,
        }

        payload: ToolResultWithFiles = {
            "text": (
                f"Extracted lines {start_line}:{start_col} to {end_line}:{end_col} "
                f"from '{os.path.basename(ref_target)}' ({len(extracted)} characters).\n"
                f"Output: {rel_output}"
            ),
            "_files": [file_entry],
        }

        logger.info(
            f"[WriteFileFromReferenceTool] extracted "
            f"{start_line}:{start_col}-{end_line}:{end_col} from "
            f"{ref_path} -> {rel_output} ({len(extracted)} chars)"
        )

        return BuiltInToolResult(structured_content=payload)


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
        WriteFileFromReferenceTool(data_dir=data_dir),
        RegisterFileWatcherTool(),
        ListFileWatchersTool(),
        DeleteFileWatcherTool(),
    ]


SERVER_NAME = "System File"

TOOL_CONFIG: ToolConfig = {
    "name": "system_file",
    "display_name": "System File",
    "default_model_group": "low",
    # Visible in Settings (so its token/size limits can be configured) but
    # locked on — a core capability the user must not disable.
    "locked": True,
    "llm_parameters": {
        "tool_instructions": (
            "A file management assistant. Operates inside the conversation's "
            "*current* working directory — which may have just been changed by "
            "the `change_working_directory` tool. All `path` arguments are "
            "interpreted relative to that current directory, never to a fixed "
            "root. To act on the current directory itself, omit `path` (or "
            "pass '.'). Never repeat the working directory's own name as a "
            "`path` value: if the user says 'list files in the Lee folder' "
            "and the cwd is already '.../Lee', call list_files with no path. "
            "Use search_files to find files by NAME and grep_files to search "
            "file CONTENTS by regular expression. "
            "Also registers file/folder watchers that notify you or re-run an "
            "action whenever files are created, modified, deleted, or moved on "
            "disk — use this when the user asks to be notified or to act on "
            "changes to a file or folder."
        ),
    },
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
        "A file management assistant. Operates inside the conversation's "
        "current working directory; all `path` arguments are relative to "
        "that directory and default to '.' when omitted. Find files by name "
        "with search_files and search file contents by regex with grep_files. "
        "Also registers "
        "file/folder watchers that notify you or re-run an action whenever "
        "files are created, modified, deleted, or moved on disk."
    )


def get_prepare_tools() -> Optional[Callable]:
    """Module hook auto-detected by ``register_builtin_tools``.

    Returns a ``prepare_tools`` callback that suppresses the
    ``register_file_watcher`` subtool on event-triggered runs. Registering a
    new watcher while *reacting* to a watcher/skill event risks recursive
    event storms (event → reasoning → register → event → …), so the reasoning
    agent injects ``_triggered_by_event=True`` into the System File dispatch
    arguments for those runs (see ``reasoning_agent._dispatch``). ``list`` and
    ``delete`` stay available — they pose no storm risk.
    """

    def prepare_tools(
        query: str,  # noqa: ARG001
        tools: List[Dict[str, Any]],
        *,
        arguments: Optional[Dict[str, Any]] = None,
        context_id: Optional[str] = None,  # noqa: ARG001
        profile: Optional[str] = None,  # noqa: ARG001
        **_: Any,
    ) -> List[Dict[str, Any]]:
        if not (arguments or {}).get("_triggered_by_event"):
            return tools
        return [
            t for t in tools
            if (t.get("function") or {}).get("name") != "register_file_watcher"
        ]

    return prepare_tools
