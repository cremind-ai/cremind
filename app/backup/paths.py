"""Path relocation — the piece that makes a backup environment-independent.

Absolute paths stored in DB rows (a conversation's working directory, an
autostart process's cwd/command, a skill tool's source dir, a file-watcher
root, the configured user working dir) are meaningful only on the machine that
wrote them. When restoring onto a different machine — Windows→Linux, a new
home directory, a Docker ``/root`` — those prefixes must be rewritten to the
target's equivalents:

    C:\\Users\\alice\\.cremind\\admin\\skills\\x   →   /root/.cremind/admin/skills/x
    C:\\Users\\alice\\Documents\\notes             →   /home/bob/Documents/notes

The rule is prefix substitution: the source ``system_dir`` and ``home_dir``
(from the manifest) map to the target's, longest-prefix first. Anything not
under a known prefix (e.g. ``D:\\projects\\x``) is left untouched and recorded
so the restore can warn the user that a process may fail in the new
environment (matching the user's "warn, don't silently break" requirement).

Pure functions — no ``app.*`` imports.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from app.backup.manifest import Manifest

# (column, mode) per table. Mode "path" is a full-value relocation; "command"
# is a best-effort token-by-token substitution inside a command string.
_PATH_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "conversations": [("working_directory", "path")],
    "autostart_processes": [("working_dir", "path"), ("command", "command")],
    "tools": [("source", "path")],
    "file_watcher_subscriptions": [("root_path", "path")],
}

# server_config is a key/value table; only this key holds a filesystem path.
_SERVER_CONFIG_PATH_KEYS = frozenset({"user_working_dir"})


@dataclass
class RelocationReport:
    relocated: list[dict[str, Any]] = field(default_factory=list)
    unmapped: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"relocated": self.relocated, "unmapped": self.unmapped}


@dataclass
class PathMap:
    # Ordered (source-prefix-parts, target-prefix-string), longest first.
    rules: list[tuple[tuple[str, ...], str]]
    windows_source: bool
    case_insensitive: bool
    target_sep: str

    def _flavor(self):
        return PureWindowsPath if self.windows_source else PurePosixPath


def _target_sep(sample_abs_path: str) -> str:
    r"""Infer the target OS separator from a sample absolute path.

    A Windows path has a drive (``C:``) or a leading backslash; everything else
    is treated as POSIX. Deriving it from the target (rather than ``os.sep``)
    keeps relocation deterministic and unit-testable in both directions on a
    single host.
    """
    if re.match(r"^[A-Za-z]:", sample_abs_path) or sample_abs_path.startswith("\\"):
        return "\\"
    return "/"


def _fold(parts: tuple[str, ...], case_insensitive: bool) -> tuple[str, ...]:
    return tuple(p.casefold() for p in parts) if case_insensitive else parts


def build_path_map(
    manifest: Manifest, target_system_dir: str, target_home: str
) -> PathMap:
    sp = manifest.source_paths
    windows_source = sp.sep == "\\"
    flavor = PureWindowsPath if windows_source else PurePosixPath

    raw_rules: list[tuple[tuple[str, ...], str]] = []
    if sp.system_dir:
        raw_rules.append((flavor(sp.system_dir).parts, target_system_dir))
    if sp.home_dir:
        raw_rules.append((flavor(sp.home_dir).parts, target_home))

    # Longest source prefix first so ``~/.cremind`` (system dir) wins over
    # ``~`` (home) for a path that sits under both.
    raw_rules.sort(key=lambda r: len(r[0]), reverse=True)

    return PathMap(
        rules=raw_rules,
        windows_source=windows_source,
        case_insensitive=sp.case_insensitive,
        target_sep=_target_sep(target_system_dir or target_home or "/"),
    )


def _join_target(pm: PathMap, target_prefix: str, remainder: tuple[str, ...]) -> str:
    base = target_prefix.rstrip("/\\")
    if not remainder:
        return base
    return base + pm.target_sep + pm.target_sep.join(remainder)


def relocate_path(pm: PathMap, value: str | None) -> tuple[str | None, bool, bool]:
    """Relocate one absolute path value.

    Returns ``(new_value, changed, was_absolute)``. Relative values and empty
    strings pass through unchanged (``was_absolute=False``). An absolute value
    that matches no known prefix passes through unchanged with
    ``was_absolute=True`` (the caller records it as unmapped).
    """
    if not value or not isinstance(value, str):
        return value, False, False

    flavor = pm._flavor()
    try:
        p = flavor(value)
    except Exception:  # noqa: BLE001
        return value, False, False
    if not p.is_absolute():
        return value, False, False

    val_parts = p.parts
    val_folded = _fold(val_parts, pm.case_insensitive)
    for src_parts, target_prefix in pm.rules:
        n = len(src_parts)
        if n == 0 or len(val_parts) < n:
            continue
        if val_folded[:n] == _fold(src_parts, pm.case_insensitive):
            remainder = val_parts[n:]
            return _join_target(pm, target_prefix, remainder), True, True
    return value, False, True


# Token splitter that preserves double/single-quoted spans as single tokens.
_TOKEN_RE = re.compile(r'"[^"]*"|\'[^\']*\'|[^\s]+')


def relocate_command(pm: PathMap, command: str | None) -> tuple[str | None, bool]:
    """Best-effort relocation of absolute-path tokens inside a command string.

    Each whitespace/quote-delimited token that parses as an absolute path under
    a known prefix is rewritten; everything else (flags, relative paths, URLs)
    is left alone. Runtime separator normalization for *relative* tokens is
    handled elsewhere (``exec_shell_autostart.normalize_command_paths``); this
    only touches absolute old-prefix occurrences.
    """
    if not command or not isinstance(command, str):
        return command, False

    changed = False

    def _sub(m: "re.Match[str]") -> str:
        nonlocal changed
        tok = m.group(0)
        quote = ""
        inner = tok
        if len(tok) >= 2 and tok[0] in "\"'" and tok[-1] == tok[0]:
            quote = tok[0]
            inner = tok[1:-1]
        new_inner, did, _abs = relocate_path(pm, inner)
        if did and new_inner is not None:
            changed = True
            return f"{quote}{new_inner}{quote}"
        return tok

    result = _TOKEN_RE.sub(_sub, command)
    return result, changed


def transform_row(
    pm: PathMap, table: str, row: dict[str, Any], report: RelocationReport
) -> dict[str, Any]:
    """Rewrite path-bearing columns of ``row`` in place-ish (returns the row).

    Used as ``load_logical``'s ``row_transform`` hook so rows are relocated
    before insert, inside the restore transaction.
    """
    if table == "server_config":
        if row.get("key") in _SERVER_CONFIG_PATH_KEYS:
            _apply_path(pm, table, "value", row, report)
        return row

    for column, mode in _PATH_COLUMNS.get(table, ()):  # type: ignore[arg-type]
        if mode == "command":
            old = row.get(column)
            new, did = relocate_command(pm, old)
            if did:
                row[column] = new
                report.relocated.append(
                    {"table": table, "column": column, "old": old, "new": new}
                )
        else:
            _apply_path(pm, table, column, row, report)
    return row


def _apply_path(
    pm: PathMap, table: str, column: str, row: dict[str, Any], report: RelocationReport
) -> None:
    old = row.get(column)
    new, did, was_abs = relocate_path(pm, old)
    if did:
        row[column] = new
        report.relocated.append(
            {"table": table, "column": column, "old": old, "new": new}
        )
    elif was_abs and old:
        report.unmapped.append({"table": table, "column": column, "value": old})


__all__ = [
    "PathMap",
    "RelocationReport",
    "build_path_map",
    "relocate_command",
    "relocate_path",
    "transform_row",
]
