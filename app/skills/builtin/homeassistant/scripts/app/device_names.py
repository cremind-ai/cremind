"""Maintains the ``## Device list`` section at the END of the skill's ``SKILL.md`` — a
stable name↔entity_id index.

One line per (filtered) entity: ``entity_id | name``. Unlike ``references/devices.md`` (which
also carries type/state and is rewritten on every state tick), this list changes only when a
device is **added, removed, or renamed** — a low-churn, always-current map from ``entity_id``
to friendly name. The persistent listener rebuilds the whole list on each (re)connect
(:func:`full_sync`) and updates a **single line** in place when a name actually changes
(:func:`upsert` / :func:`remove`) — no full reload, no extra API calls.

The list lives at the tail of ``SKILL.md``, under a ``## Device list`` heading whose body ends
with the :data:`MARKER` sentinel; everything after the marker (to EOF) is device lines, sorted
by ``entity_id``. Everything up to and including the marker — the whole rest of ``SKILL.md`` —
is preserved byte-for-byte across every write, so unrelated documentation is never mangled. If
the marker is missing (someone stripped the section) the section is appended fresh; if
``SKILL.md`` itself is missing every write is a no-op (it is a shipped file, never conjured).
All writes are atomic (tmp file + ``os.replace``) with ``\\n`` newlines, matching
``devices._atomic_write`` / ``listener._save_state``.
"""
from __future__ import annotations

import os
import re

from . import config


MARKER = "<!-- BEGIN DEVICE LIST (entity_id | name) -->"

# Appended only when SKILL.md exists but the marker is absent (defensive self-heal). A leading
# blank line separates it from whatever preceded it; the marker is left as the final line so
# device lines append directly after it. Mirrors the section shipped in SKILL.md.
_SECTION = (
    "\n"
    "## Device list\n"
    "\n"
    "Auto-generated index of the entities this skill tracks (mirrors `HA_ENTITY_FILTER`),\n"
    "kept current by the event listener and the `sync-devices` verb. One device per line:\n"
    "`entity_id | name`. Do not hand-edit below the marker — rebuilt on each connect.\n"
    "\n"
    f"{MARKER}\n"
)

_SEP = " | "
_WS = re.compile(r"\s+")


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


def _compose(prefix: str, lines: list[str]) -> str:
    """``prefix`` ends with ``MARKER + "\\n"``; append the device lines (if any) after it."""
    return prefix + "\n".join(lines) + "\n" if lines else prefix


def _atomic_write(text: str) -> None:
    tmp = config.SKILL_FILE.with_suffix(".md.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, config.SKILL_FILE)


def _split_existing() -> tuple[str | None, list[str]]:
    """Return ``(prefix-through-marker, device lines)`` for SKILL.md.

    - Marker present (normal): ``prefix`` is everything up to and including the marker line,
      byte-for-byte; ``lines`` are the non-blank device lines after it.
    - Marker absent but file readable: append a fresh ``## Device list`` section to the current
      contents and return that as the prefix, with no device lines (never destructive).
    - File missing/unreadable: return ``(None, [])`` — the caller must no-op (SKILL.md is a
      shipped file, never created from scratch here).
    """
    try:
        text = config.SKILL_FILE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, []
    idx = text.find(MARKER)
    if idx == -1:
        # Self-heal: re-append the section. Avoid a doubled blank line if the file already
        # ends with a newline (it normally does).
        body = text if text.endswith("\n") else text + "\n"
        return body + _SECTION, []
    prefix = text[:idx] + MARKER + "\n"  # preserve everything before + including the marker
    after = text[idx + len(MARKER):].lstrip("\n")
    lines = [ln for ln in after.splitlines() if ln.strip()]
    return prefix, lines


def _write_if_changed(text: str) -> None:
    """Write *text* atomically, but skip the replace when SKILL.md already equals it.

    ``full_sync`` runs on every (re)connect with the same rows; skipping the no-op write keeps
    the skills watcher from re-scanning when nothing actually changed."""
    try:
        if config.SKILL_FILE.read_text(encoding="utf-8") == text:
            return
    except (OSError, UnicodeDecodeError):
        pass
    _atomic_write(text)


def full_sync(rows: list[dict]) -> None:
    """Replace ONLY the device lines (after the marker, to EOF) with ``rows``, sorted.

    Everything up to and including the marker is preserved byte-for-byte — full_sync must never
    clobber the rest of SKILL.md. No-op if SKILL.md is missing, or if the result is unchanged.
    Accepts the same rows ``devices.row_from_state`` produces; extra keys are ignored."""
    prefix, _ = _split_existing()
    if prefix is None:
        return
    ordered = sorted(rows, key=lambda r: r.get("entity_id", ""))
    lines = [_render_line(r.get("entity_id", ""), r.get("name", "")) for r in ordered]
    _write_if_changed(_compose(prefix, lines))


def upsert(entity_id: str, name) -> None:
    """Update (or insert, keeping sorted order) the single line for ``entity_id``.

    Other lines and the entire SKILL.md prefix pass through byte-for-byte; the file is never
    fully re-rendered. No-op if SKILL.md is missing."""
    prefix, lines = _split_existing()
    if prefix is None:
        return
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
    _atomic_write(_compose(prefix, lines))


def remove(entity_id: str) -> None:
    """Drop ``entity_id``'s line. No-op (no write) if it isn't present or SKILL.md is missing."""
    prefix, lines = _split_existing()
    if prefix is None:
        return
    kept = [ln for ln in lines if _key(ln) != entity_id]
    if len(kept) == len(lines):
        return
    _atomic_write(_compose(prefix, kept))
