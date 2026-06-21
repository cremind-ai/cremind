"""Maintains ``references/device_names.md`` — a stable name↔entity_id index.

One line per (filtered) entity: ``entity_id | name``. Unlike ``references/devices.md`` (which
also carries type/state and is rewritten on every state tick), this file changes only when a
device is **added, removed, or renamed** — a low-churn, always-current map from ``entity_id``
to friendly name. The persistent listener rebuilds the whole file on each (re)connect
(:func:`full_sync`) and updates a **single line** in place when a name actually changes
(:func:`upsert` / :func:`remove`) — no full reload, no extra API calls.

A fixed header block ends with the :data:`MARKER` sentinel; everything after it is device
lines, sorted by ``entity_id``. Header text before the marker is preserved verbatim across
single-line updates. All writes are atomic (tmp file + ``os.replace``) with ``\\n`` newlines,
matching ``devices._atomic_write`` / ``listener._save_state``.
"""
from __future__ import annotations

import os
import re

from . import config


MARKER = "<!-- BEGIN DEVICE NAMES (entity_id | name) -->"

_SEP = " | "
_WS = re.compile(r"\s+")


def _header() -> str:
    return (
        "# Home Assistant — device name index\n"
        "\n"
        "Auto-generated; kept current by the event listener (full snapshot on each connect,\n"
        "single-line in-place updates only when a device is added, removed, or renamed). Do\n"
        "not hand-edit — changes are overwritten. Mirrors HA_ENTITY_FILTER. One device per\n"
        "line: `entity_id | name`.\n"
        "\n"
        f"{MARKER}\n"
    )


def _sanitize_cell(value) -> str:
    """Collapse a value to a single safe cell: no pipes, no newlines, no run-on whitespace."""
    s = str(value if value is not None else "")
    s = s.replace("|", "/")
    s = _WS.sub(" ", s)  # \s+ also folds \r and \n
    return s.strip()


def _render_line(entity_id: str, name) -> str:
    return _SEP.join([entity_id, _sanitize_cell(name)])


def _key(line: str) -> str:
    return line.split(_SEP, 1)[0]


def _compose(header: str, lines: list[str]) -> str:
    return header + "\n".join(lines) + "\n" if lines else header


def _atomic_write(text: str) -> None:
    config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.DEVICE_NAMES_FILE.with_suffix(".md.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, config.DEVICE_NAMES_FILE)


def _split_existing() -> tuple[str, list[str]]:
    """Return (header-through-marker, device lines). Falls back to a fresh header if the
    file is missing or the marker is absent (never raises on a malformed file)."""
    try:
        text = config.DEVICE_NAMES_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return _header(), []
    idx = text.find(MARKER)
    if idx == -1:
        return _header(), []
    header = text[:idx] + MARKER + "\n"  # preserve any header text before the marker
    after = text[idx + len(MARKER):].lstrip("\n")
    lines = [ln for ln in after.splitlines() if ln.strip()]
    return header, lines


def full_sync(rows: list[dict]) -> None:
    """Rebuild the whole file from ``rows`` (each needs ``entity_id`` + ``name``), sorted.

    Accepts the same rows ``devices.row_from_state`` produces; extra keys are ignored."""
    ordered = sorted(rows, key=lambda r: r.get("entity_id", ""))
    lines = [_render_line(r.get("entity_id", ""), r.get("name", "")) for r in ordered]
    _atomic_write(_compose(_header(), lines))


def upsert(entity_id: str, name) -> None:
    """Update (or insert, keeping sorted order) the single line for ``entity_id``.

    Other lines pass through byte-for-byte; the file is never fully re-rendered."""
    header, lines = _split_existing()
    new_line = _render_line(entity_id, name)
    for i, ln in enumerate(lines):
        if _key(ln) == entity_id:
            lines[i] = new_line
            break
    else:
        pos = len(lines)
        for i, ln in enumerate(lines):
            if _key(ln) > entity_id:
                pos = i
                break
        lines.insert(pos, new_line)
    _atomic_write(_compose(header, lines))


def remove(entity_id: str) -> None:
    """Drop ``entity_id``'s line. No-op (no write) if it isn't present."""
    header, lines = _split_existing()
    kept = [ln for ln in lines if _key(ln) != entity_id]
    if len(kept) == len(lines):
        return
    _atomic_write(_compose(header, kept))
