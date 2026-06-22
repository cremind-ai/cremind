"""Maintains ``references/devices.md`` — a concise, always-current device inventory.

One line per (filtered) entity: ``entity_id | name | type | state``. The persistent
listener rebuilds the whole file on each (re)connect (:func:`full_sync`) and updates a
**single line** in place as individual states change (:func:`upsert` / :func:`remove`) —
no full reload, no extra API calls. The file purposely carries only the four short fields
so the agent can load the current picture of every device cheaply.

A fixed header block ends with the :data:`MARKER` sentinel; everything after it is device
lines, sorted by ``entity_id``. Header text before the marker is preserved verbatim across
single-line updates. All writes are atomic (tmp file + ``os.replace``) with ``\\n`` newlines,
matching ``listener._save_state``.
"""
from __future__ import annotations

import os
import re

from . import config


MARKER = "<!-- BEGIN DEVICES (entity_id | name | type | state) -->"

_SEP = " | "
_WS = re.compile(r"\s+")


def _header() -> str:
    return (
        "# Home Assistant — current device inventory\n"
        "\n"
        "Auto-generated; kept current by the event listener (full snapshot on each connect,\n"
        "single-line in-place updates as states change). Do not hand-edit — changes are\n"
        "overwritten. Mirrors HA_ENTITY_FILTER. One device per line:\n"
        "`entity_id | name | type | state`.\n"
        "\n"
        f"{MARKER}\n"
    )


def device_type(entity_id: str, attrs: dict | None) -> str:
    """The entity's type: its domain, or ``domain/device_class`` when one is set."""
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    device_class = (attrs or {}).get("device_class")
    return f"{domain}/{device_class}" if device_class else domain


def _sanitize_cell(value) -> str:
    """Collapse a value to a single safe cell: no pipes, no newlines, no run-on whitespace."""
    s = str(value if value is not None else "")
    s = s.replace("|", "/")
    s = _WS.sub(" ", s)  # \s+ also folds \r and \n
    return s.strip()


def _render_line(entity_id: str, name, dtype, state) -> str:
    return _SEP.join(
        [entity_id, _sanitize_cell(name), _sanitize_cell(dtype), _sanitize_cell(state)]
    )


def row_from_state(state: dict) -> dict:
    """Build a devices row ``{entity_id, name, dtype, state}`` from a raw HA state dict."""
    entity_id = state.get("entity_id", "")
    attrs = state.get("attributes") or {}
    return {
        "entity_id": entity_id,
        "name": attrs.get("friendly_name") or entity_id,
        "dtype": device_type(entity_id, attrs),
        "state": state.get("state", ""),
    }


def _key(line: str) -> str:
    return line.split(_SEP, 1)[0]


def _compose(header: str, lines: list[str]) -> str:
    return header + "\n".join(lines) + "\n" if lines else header


def _atomic_write(text: str) -> None:
    config.REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.DEVICES_FILE.with_suffix(".md.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, config.DEVICES_FILE)


def _split_existing() -> tuple[str, list[str]]:
    """Return (header-through-marker, device lines). Falls back to a fresh header if the
    file is missing or the marker is absent (never raises on a malformed file)."""
    try:
        text = config.DEVICES_FILE.read_text(encoding="utf-8")
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
    """Rebuild the whole file from ``rows`` (``{entity_id, name, dtype, state}``), sorted."""
    ordered = sorted(rows, key=lambda r: r.get("entity_id", ""))
    lines = [
        _render_line(r.get("entity_id", ""), r.get("name", ""), r.get("dtype", ""), r.get("state", ""))
        for r in ordered
    ]
    _atomic_write(_compose(_header(), lines))


def upsert(entity_id: str, name, dtype, state) -> None:
    """Update (or insert, keeping sorted order) the single line for ``entity_id``.

    Other lines pass through byte-for-byte; the file is never fully re-rendered."""
    header, lines = _split_existing()
    new_line = _render_line(entity_id, name, dtype, state)
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
