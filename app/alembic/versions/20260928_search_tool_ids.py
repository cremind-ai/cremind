"""Search tool ids — the two document searches swap names.

Revision ID: 20260928_search_tool_ids
Revises: 20260927_userdocs_research
Create Date: 2026-09-28

Cremind's own manual search was the built-in ``documentation_search``; it is
now ``cremind_documentation_search``. The personal-document search was
``user_documents``; it takes the freed name ``documentation_search``. Every
per-profile setting follows its tool: ``profile_tools`` (membership / enabled
flag) and ``tool_configs`` (arguments, variables, meta, and the per-function
``leaf`` disables), plus the ``usage_records.tool_id`` attribution.

**The order is the whole migration.** The Cremind-manual row moves out of
``documentation_search`` FIRST, and only then does ``user_documents`` move in.
The other order would land every profile's manual-search settings (its
``DEFAULT_TOP_K``, its disabled ``read_documentation_section``) on the
personal-document search and silently discard the personal ones.

A move is copy → repoint → delete, never ``UPDATE tools SET tool_id``:

1. insert the row under the new id (and the new ``source`` — the built-in's
   module name, which ``UNIQUE(tool_type, source)`` ties to the id);
2. repoint the children (``profile_tools``, ``tool_configs``) at it;
3. delete the old row, which by then has no children left to cascade.

That is correct whether or not the live DDL carries ``ON UPDATE CASCADE``
(pre-Alembic installs may not) and whether or not SQLite enforces foreign keys
on this connection (the sync engine turns them on, a test harness may not) —
the new parent always exists before a child points at it.

A development database that booted the renamed code before this migration
existed already holds a fresh ``cremind_documentation_search`` row with
defaults (and possibly a few rows of its own). The migrated row's settings
win: the fresh duplicate and its children are removed before the move. Usage
rows written in that window under ``documentation_search`` with
``source_kind='documents'`` were already the personal search's, so they are
left where they are.

Only ``tool_type='builtin'`` rows are touched. Skills, A2A and MCP rows keep
their ids; one that happens to hold a target id (an MCP server named
"Cremind Documentation Search") is moved aside to ``<id>_2`` with its
per-profile state, exactly as the registry displaces it at boot.

Guarded throughout: minimal test schemas lack ``tools`` / ``usage_records``,
and a fresh install has no rows at all. The revision runs in one transaction
with its own version stamp (per-migration on SQLite, whole-upgrade on
PostgreSQL), so it either lands completely or not at all; Alembic never runs it
twice. ``downgrade`` is the mirror swap in the mirrored order.
``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

import time
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260928_search_tool_ids"
down_revision: Union[str, Sequence[str], None] = "20260927_userdocs_research"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (old tool_id, new tool_id), in the only safe order — see the module docstring.
_SWAPS: tuple[tuple[str, str], ...] = (
    ("documentation_search", "cremind_documentation_search"),
    ("user_documents", "documentation_search"),
)

# Usage rows that already belong to the NEW owner of an old id and must not be
# carried along with the swap: ``documentation_search`` rows the renamed code
# wrote as the personal search (source_kind ``documents``) before this ran.
_USAGE_KEEP_SOURCE_KINDS: dict[str, tuple[str, ...]] = {
    "documentation_search": ("documents",),
}

_BUILTIN = "builtin"
_CHILD_TABLES = ("profile_tools", "tool_configs")


def _log(message: str) -> None:
    try:
        from app.utils.logger import logger

        logger.info(f"[migrations] {message}")
    except Exception:  # noqa: BLE001 — logging never fails a migration
        pass


def _columns(inspector, table: str) -> list[str]:
    return [c["name"] for c in inspector.get_columns(table)]


def _tool_row(bind, tool_id: str):
    return bind.execute(
        sa.text("SELECT tool_id, tool_type, source FROM tools WHERE tool_id = :id"),
        {"id": tool_id},
    ).mappings().first()


def _delete_tool(bind, children: tuple[str, ...], tool_id: str) -> None:
    """Remove a tool row and its per-profile rows (explicitly — the cascade
    may be absent from older DDL or unenforced on this connection)."""
    for child in children:
        bind.execute(sa.text(f"DELETE FROM {child} WHERE tool_id = :id"), {"id": tool_id})
    bind.execute(sa.text("DELETE FROM tools WHERE tool_id = :id"), {"id": tool_id})


def _copy_move(
    bind, tool_cols: list[str], children: tuple[str, ...],
    old: str, new: str, *, new_source: str | None,
) -> None:
    """Move ``old`` to ``new``: insert the copy, repoint children, drop ``old``.

    ``new_source`` replaces ``source`` when given; ``None`` keeps it — and
    since the copy may not share ``(tool_type, source)`` with the row it is
    copied from, the original is first parked under a placeholder source.
    """
    if new_source is None and "source" in tool_cols:
        kept = bind.execute(
            sa.text("SELECT source FROM tools WHERE tool_id = :old"), {"old": old},
        ).scalar()
        if kept is not None:
            bind.execute(
                sa.text("UPDATE tools SET source = :tmp WHERE tool_id = :old"),
                {"tmp": f"{kept}#moving-to:{new}", "old": old},
            )
            new_source = kept
    quote = bind.dialect.identifier_preparer.quote
    select_list = []
    params: dict[str, object] = {"old": old, "new": new, "now": time.time() * 1000}
    for col in tool_cols:
        if col == "tool_id":
            select_list.append(":new")
        elif col == "source" and new_source is not None:
            select_list.append(":new_source")
            params["new_source"] = new_source
        elif col == "updated_at":
            select_list.append(":now")
        else:
            select_list.append(quote(col))
    bind.execute(
        sa.text(
            f"INSERT INTO tools ({', '.join(quote(c) for c in tool_cols)}) "
            f"SELECT {', '.join(select_list)} FROM tools WHERE tool_id = :old"
        ),
        params,
    )
    for child in children:
        bind.execute(
            sa.text(f"UPDATE {child} SET tool_id = :new WHERE tool_id = :old"),
            {"new": new, "old": old},
        )
    bind.execute(sa.text("DELETE FROM tools WHERE tool_id = :old"), {"old": old})


def _free_id(bind, base: str) -> str:
    taken = {r[0] for r in bind.execute(sa.text("SELECT tool_id FROM tools")).fetchall()}
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def _swap_builtin(bind, tool_cols: list[str], children: tuple[str, ...], old: str, new: str) -> None:
    row = _tool_row(bind, old)
    if row is None or row["tool_type"] != _BUILTIN:
        return  # fresh install, or nothing of ours under this id

    # Clear the target id. A built-in there is a fresh duplicate a renamed
    # boot registered with defaults — the migrated row's settings win. Anything
    # else is a user's tool that merely shares the slug: move it aside.
    target = _tool_row(bind, new)
    if target is not None:
        if target["tool_type"] == _BUILTIN:
            _delete_tool(bind, children, new)
            _log(f"removed the fresh built-in row '{new}' before moving '{old}' into it")
        else:
            aside = _free_id(bind, new)
            _copy_move(bind, tool_cols, children, new, aside, new_source=None)
            _log(f"moved {target['tool_type']} tool '{new}' aside to '{aside}'")

    # Any other built-in row already claiming the new module name would trip
    # UNIQUE(tool_type, source) on the insert below.
    if "source" in tool_cols:
        dupes = bind.execute(
            sa.text(
                "SELECT tool_id FROM tools WHERE tool_type = :t AND source = :s "
                "AND tool_id <> :old"
            ),
            {"t": _BUILTIN, "s": new, "old": old},
        ).fetchall()
        for (dupe_id,) in dupes:
            _delete_tool(bind, children, dupe_id)
            _log(f"removed stale built-in row '{dupe_id}' holding source '{new}'")

    _copy_move(
        bind, tool_cols, children, old, new,
        new_source=new if "source" in tool_cols else None,
    )
    _log(f"moved built-in tool '{old}' -> '{new}' with its per-profile settings")


# The display name each built-in carried as a usage ``label`` (its SERVER_NAME
# at the time the row was written), per tool id, before and after the rename.
# Usage & Cost shows ``label`` first, so a moved row keeping its old label would
# read as the OTHER tool: the manual's old judge rows said "Documentation
# Search", which is now the personal search's name. Only these exact default
# labels are rewritten, and only on rows this swap moves.
_TOOL_LABELS: dict[str, str] = {
    "documentation_search": "Documentation Search",
    "cremind_documentation_search": "Cremind Documentation Search",
    "user_documents": "User Documents",
}


def _swap_usage(bind, old: str, new: str, *, keep: tuple[str, ...] = (),
                old_label: str | None = None, new_label: str | None = None) -> None:
    where = "tool_id = :old"
    params: dict[str, object] = {"new": new, "old": old}
    if keep:
        names = []
        for i, kind in enumerate(keep):
            params[f"k{i}"] = kind
            names.append(f":k{i}")
        where += f" AND (source_kind IS NULL OR source_kind NOT IN ({', '.join(names)}))"
    if old_label is not None and new_label is not None and old_label != new_label:
        # Relabel first (the WHERE still sees the old id), then move.
        bind.execute(sa.text(
            f"UPDATE usage_records SET label = :new_label WHERE {where} AND label = :old_label"
        ), {**params, "old_label": old_label, "new_label": new_label})
    bind.execute(sa.text(f"UPDATE usage_records SET tool_id = :new WHERE {where}"), params)


def _run(swaps: tuple[tuple[str, str], ...], *, keep_kinds: dict[str, tuple[str, ...]]) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    tool_cols: list[str] = []
    children: tuple[str, ...] = ()
    if "tools" in tables:
        tool_cols = _columns(inspector, "tools")
        children = tuple(
            t for t in _CHILD_TABLES
            if t in tables and "tool_id" in _columns(inspector, t)
        )
    tools_ok = "tool_id" in tool_cols and "tool_type" in tool_cols

    usage_cols = _columns(inspector, "usage_records") if "usage_records" in tables else []
    usage_ok = "tool_id" in usage_cols
    usage_has_kind = "source_kind" in usage_cols
    usage_has_label = "label" in usage_cols

    # Strictly in order: each swap's tools AND usage rows move before the next
    # swap reuses the id the previous one freed.
    for old, new in swaps:
        if tools_ok:
            _swap_builtin(bind, tool_cols, children, old, new)
        if usage_ok:
            _swap_usage(
                bind, old, new,
                keep=keep_kinds.get(old, ()) if usage_has_kind else (),
                old_label=_TOOL_LABELS.get(old) if usage_has_label else None,
                new_label=_TOOL_LABELS.get(new) if usage_has_label else None,
            )


def upgrade() -> None:
    _run(_SWAPS, keep_kinds=_USAGE_KEEP_SOURCE_KINDS)


def downgrade() -> None:
    # The exact mirror, in the mirrored order: the personal search leaves
    # ``documentation_search`` first, then the manual search moves back in.
    # Every ``documentation_search`` usage row is the personal search's by now.
    _run(tuple((new, old) for old, new in reversed(_SWAPS)), keep_kinds={})
