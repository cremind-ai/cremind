"""Documentation search — the ``userdoc_*`` tables and keys take their new names.

Revision ID: 20260928b_document_tables
Revises: 20260928_search_tool_ids
Create Date: 2026-09-28

The personal-document search ("User Document Search") is now Documentation
search, and its durable state follows the new name. Nothing changes shape;
every row keeps its identity (profile names, source ids, caption hashes, quota
days, citation ids, research job ids, usage totals).

**Tables.** ``userdoc_sources``, ``userdoc_captions``, ``userdoc_vision_usage``,
``userdoc_citations`` and ``userdoc_research_jobs`` become ``document_*``, and
every name derived from them follows so the schema matches
:mod:`app.storage.models` exactly:

- PostgreSQL renames in place: ``ALTER TABLE … RENAME TO``, then every index
  (``ALTER INDEX … RENAME``) and every constraint (``ALTER TABLE … RENAME
  CONSTRAINT`` — the two named UNIQUEs and PostgreSQL's own ``*_pkey`` /
  ``*_fkey`` names, which embed the table name). Discovered with the
  inspector, never hardcoded, so it renames exactly what is there.
- SQLite renames ``captions`` / ``vision_usage`` / ``research_jobs`` with
  ``ALTER TABLE … RENAME TO`` and re-creates their indexes under the new names
  (SQLite has no ``ALTER INDEX``). ``sources`` and ``citations`` carry a named
  UNIQUE constraint, and on SQLite that name lives only in the table's DDL,
  so those two are rebuilt: the new table is created from its explicit
  definition, the rows are copied, the old table is dropped. That rebuild is
  safe for these tables ONLY because nothing references them — it must never
  be copied onto ``conversations``, ``profiles`` or ``group_chats``, whose
  rebuild cascade-deletes their children. Rows a CASCADE would already have
  removed (a profile or conversation that no longer exists) are purged first,
  because foreign keys are enforced during the copy.

**Settings.** ``server_config`` keys ``userdocs.*`` become
``documentation_search.*`` (the admin gate). A target key that already exists
— written by the renamed code before this ran — wins, and the legacy duplicate
is dropped.

**Usage.** ``usage_records.source_kind`` ``userdocs`` → ``documents``.

**Citations.** Stored tokens ``[ud:…]`` become ``[doc:…]`` — the same
citation, now in its canonical form. ``UNIQUE(conversation_id, token)`` cannot
collide on a 1:1 prefix swap, but a row whose ``[doc:…]`` twin already exists
in the same conversation is left as it is rather than risk the constraint
(readers normalise both forms to one citation). Message text and metadata are
NOT rewritten: an old answer keeps its ``[ud:…]`` tokens, and every reader
accepts them.

Guarded and re-runnable: SQLite commits DDL as it goes, so a crash can leave
a partial state; every step checks what is there first. ``downgrade`` is the
exact mirror. ``MIN_SUPPORTED_UPGRADE_FROM`` is not bumped.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260928b_document_tables"
down_revision: Union[str, Sequence[str], None] = "20260928_search_tool_ids"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEGACY = "userdoc"
_CURRENT = "document"

# Table suffixes (``<prefix>_<suffix>``), in creation order.
_SUFFIXES = ("sources", "captions", "vision_usage", "citations", "research_jobs")

# The two tables whose UNIQUE constraint carries the prefix in its name, and
# so must be rebuilt on SQLite to rename it.
_REBUILT_ON_SQLITE = ("sources", "citations")

_LEGACY_CONFIG_PREFIX = "userdocs."
_CURRENT_CONFIG_PREFIX = "documentation_search."
_LEGACY_USAGE_KIND = "userdocs"
_CURRENT_USAGE_KIND = "documents"
_LEGACY_TOKEN_PREFIX = "[ud:"
_CURRENT_TOKEN_PREFIX = "[doc:"


def _log(message: str) -> None:
    try:
        from app.utils.logger import logger

        logger.info(f"[migrations] {message}")
    except Exception:  # noqa: BLE001 — logging never fails a migration
        pass


# ── explicit definitions (identical to the historical revisions and to the
#    ORM; only the prefix in the names differs) ──────────────────────────────


def _indexes(prefix: str, suffix: str) -> tuple[tuple[str, list[str]], ...]:
    return {
        "sources": ((f"ix_{prefix}_sources_profile", ["profile"]),),
        "captions": (),
        "vision_usage": (),
        "citations": ((f"ix_{prefix}_citations_profile_cite", ["profile", "cite_id"]),),
        "research_jobs": (
            (f"ix_{prefix}_research_jobs_profile_created", ["profile", "created_at"]),
            (f"ix_{prefix}_research_jobs_conversation", ["conversation_id"]),
        ),
    }[suffix]


def _definition(prefix: str, suffix: str) -> list:
    """Columns + constraints of ``<prefix>_<suffix>`` for ``op.create_table``.
    Only the two tables rebuilt on SQLite are ever created from here."""
    if suffix == "sources":
        return [
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("kind", sa.String(length=16), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("root_mode", sa.String(length=16), nullable=False, server_default="inherit"),
            sa.Column("root_path", sa.Text(), nullable=True),
            sa.Column("excludes", sa.JSON(), nullable=True),
            sa.Column("options", sa.JSON(), nullable=True),
            sa.Column("first_sync_confirmed_at", sa.Float(), nullable=True),
            sa.Column("created_at", sa.Float(), nullable=False),
            sa.Column("updated_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("profile", "kind", name=f"uq_{prefix}_sources_profile_kind"),
        ]
    if suffix == "citations":
        return [
            sa.Column("id", sa.String(length=36), nullable=False),
            sa.Column("profile", sa.String(length=128), nullable=False),
            sa.Column("conversation_id", sa.String(length=128), nullable=True),
            sa.Column("token", sa.String(length=32), nullable=False),
            sa.Column("cite_id", sa.String(length=8), nullable=False),
            sa.Column("target", sa.String(length=8), nullable=False),
            sa.Column("ref_id", sa.Integer(), nullable=True),
            sa.Column("text_hash", sa.String(length=32), nullable=True),
            sa.Column("source_kind", sa.String(length=8), nullable=False),
            sa.Column("locator", sa.JSON(), nullable=True),
            sa.Column("label", sa.String(length=256), nullable=False),
            sa.Column("rel_path", sa.Text(), nullable=False),
            sa.Column("snippet", sa.Text(), nullable=False),
            sa.Column("leaf", sa.String(length=16), nullable=False),
            sa.Column("web_link", sa.Text(), nullable=True),
            sa.Column("issued_at", sa.Float(), nullable=False),
            sa.ForeignKeyConstraint(["profile"], ["profiles.name"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("conversation_id", "token", name=f"uq_{prefix}_citations_conv_token"),
        ]
    raise KeyError(suffix)


# ── helpers ──────────────────────────────────────────────────────────────────


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def _cols(bind, table: str) -> list[str]:
    return [c["name"] for c in sa.inspect(bind).get_columns(table)]


def _count(bind, table: str) -> int:
    return int(bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)


def _q(bind, name: str) -> str:
    return bind.dialect.identifier_preparer.quote(name)


def _clear_target(bind, src: str, dst: str) -> bool:
    """Make room for ``dst``. ``False`` when it holds rows of its own — then the
    source table is left in place (nothing is ever dropped with data in it)."""
    if dst not in _tables(bind):
        return True
    if _count(bind, dst):
        _log(f"both {src} and {dst} hold rows; leaving {src} in place")
        return False
    op.drop_table(dst)
    return True


def _purge_orphans(bind, table: str) -> None:
    """Delete the rows a CASCADE would already have removed, so the FK-enforced
    copy of a SQLite rebuild cannot fail on them.

    Counted first and deleted only when there are some: with foreign keys on,
    SQLite refuses ANY write to a table whose FK names a table that does not
    exist — which a minimal schema (no ``conversations``) can present."""
    present = _tables(bind)
    cols = _cols(bind, table)
    checks = []
    if "profile" in cols and "profiles" in present:
        checks.append("profile NOT IN (SELECT name FROM profiles)")
    if "conversation_id" in cols and "conversations" in present:
        checks.append(
            "conversation_id IS NOT NULL AND conversation_id NOT IN (SELECT id FROM conversations)"
        )
    for where in checks:
        orphans = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table} WHERE {where}")).scalar()
        if orphans:
            bind.execute(sa.text(f"DELETE FROM {table} WHERE {where}"))
            _log(f"{table}: purged {orphans} row(s) whose owner no longer exists")


def _sqlite_rebuild(bind, src: str, dst_prefix: str, suffix: str) -> None:
    """Create ``<dst_prefix>_<suffix>`` from its definition, copy ``src`` into
    it, drop ``src``. Only for the document tables (nothing references them)."""
    dst = f"{dst_prefix}_{suffix}"
    _purge_orphans(bind, src)
    op.create_table(dst, *_definition(dst_prefix, suffix))
    if _count(bind, src):
        src_cols = set(_cols(bind, src))
        common = [c for c in _cols(bind, dst) if c in src_cols]
        col_list = ", ".join(_q(bind, c) for c in common)
        bind.execute(sa.text(f"INSERT INTO {dst} ({col_list}) SELECT {col_list} FROM {src}"))
    op.drop_table(src)


def _rename_objects_postgres(bind, table: str, old_prefix: str, new_prefix: str) -> None:
    """Rename every constraint and index of ``table`` whose name carries the
    old prefix (PostgreSQL). Constraints first: renaming a PK/UNIQUE
    constraint renames the index behind it too."""
    old_tag, new_tag = f"{old_prefix}_", f"{new_prefix}_"
    insp = sa.inspect(bind)
    names: list[str] = []
    pk = insp.get_pk_constraint(table) or {}
    if pk.get("name"):
        names.append(pk["name"])
    names += [fk["name"] for fk in insp.get_foreign_keys(table) if fk.get("name")]
    names += [uq["name"] for uq in insp.get_unique_constraints(table) if uq.get("name")]
    for name in names:
        if old_tag in name:
            bind.execute(sa.text(
                f"ALTER TABLE {_q(bind, table)} RENAME CONSTRAINT {_q(bind, name)} "
                f"TO {_q(bind, name.replace(old_tag, new_tag))}"
            ))
    for idx in sa.inspect(bind).get_indexes(table):
        name = idx.get("name") or ""
        if old_tag in name:
            bind.execute(sa.text(
                f"ALTER INDEX {_q(bind, name)} RENAME TO {_q(bind, name.replace(old_tag, new_tag))}"
            ))


def _rename_indexes_sqlite(bind, table: str, old_prefix: str, new_prefix: str) -> None:
    """SQLite has no ALTER INDEX: drop each prefixed index, create it anew."""
    old_tag, new_tag = f"{old_prefix}_", f"{new_prefix}_"
    for idx in sa.inspect(bind).get_indexes(table):
        name = idx.get("name") or ""
        if old_tag not in name:
            continue
        op.drop_index(name, table_name=table)
        new_name = name.replace(old_tag, new_tag)
        if new_name not in {i["name"] for i in sa.inspect(bind).get_indexes(table)}:
            op.create_index(new_name, table, idx["column_names"], unique=bool(idx.get("unique")))


def _ensure_indexes(bind, prefix: str, suffix: str) -> None:
    table = f"{prefix}_{suffix}"
    if table not in _tables(bind):
        return
    existing = {i["name"] for i in sa.inspect(bind).get_indexes(table)}
    for name, columns in _indexes(prefix, suffix):
        if name not in existing:
            op.create_index(name, table, columns)


def _move_tables(bind, old_prefix: str, new_prefix: str) -> None:
    is_sqlite = bind.dialect.name == "sqlite"
    for suffix in _SUFFIXES:
        src, dst = f"{old_prefix}_{suffix}", f"{new_prefix}_{suffix}"
        if src in _tables(bind):
            if not _clear_target(bind, src, dst):
                continue
            if is_sqlite and suffix in _REBUILT_ON_SQLITE:
                _sqlite_rebuild(bind, src, new_prefix, suffix)
            else:
                op.rename_table(src, dst)
        if dst not in _tables(bind):
            continue
        if is_sqlite:
            _rename_indexes_sqlite(bind, dst, old_prefix, new_prefix)
        else:
            _rename_objects_postgres(bind, dst, old_prefix, new_prefix)
        _ensure_indexes(bind, new_prefix, suffix)


def _move_config_keys(bind, old: str, new: str) -> None:
    if "server_config" not in _tables(bind):
        return
    rows = bind.execute(sa.text(
        "SELECT key FROM server_config WHERE key LIKE :pat"
    ), {"pat": f"{old}%"}).fetchall()
    for (key,) in rows:
        if not key.startswith(old):
            continue  # LIKE is case-insensitive on SQLite
        target = new + key[len(old):]
        taken = bind.execute(
            sa.text("SELECT 1 FROM server_config WHERE key = :k"), {"k": target},
        ).first()
        if taken:
            bind.execute(sa.text("DELETE FROM server_config WHERE key = :k"), {"k": key})
            _log(f"server_config {key!r} dropped: {target!r} is already set")
        else:
            bind.execute(
                sa.text("UPDATE server_config SET key = :new WHERE key = :old"),
                {"new": target, "old": key},
            )


def _move_usage_kind(bind, old: str, new: str) -> None:
    if "usage_records" not in _tables(bind) or "source_kind" not in _cols(bind, "usage_records"):
        return
    bind.execute(
        sa.text("UPDATE usage_records SET source_kind = :new WHERE source_kind = :old"),
        {"new": new, "old": old},
    )


def _move_citation_tokens(bind, table: str, old: str, new: str) -> None:
    if table not in _tables(bind) or "token" not in _cols(bind, table):
        return
    if not bind.execute(
        sa.text(f"SELECT COUNT(*) FROM {table} WHERE token LIKE :pat"), {"pat": f"{old}%"},
    ).scalar():
        return  # nothing to rewrite (and no write to trip over a minimal schema)
    # 1:1 prefix swap; the NOT EXISTS keeps UNIQUE(conversation_id, token)
    # safe should a twin in the new form already exist. NULL conversations
    # never collide (and never match the ``=`` below).
    bind.execute(sa.text(
        f"UPDATE {table} SET token = :new || substr(token, :cut) "
        f"WHERE token LIKE :pat AND NOT EXISTS ("
        f"SELECT 1 FROM {table} twin "
        f"WHERE twin.conversation_id = {table}.conversation_id "
        f"AND twin.token = :new || substr({table}.token, :cut))"
    ), {"new": new, "cut": len(old) + 1, "pat": f"{old}%"})


# ── upgrade / downgrade ─────────────────────────────────────────────────────


def upgrade() -> None:
    bind = op.get_bind()
    _move_tables(bind, _LEGACY, _CURRENT)
    _move_config_keys(bind, _LEGACY_CONFIG_PREFIX, _CURRENT_CONFIG_PREFIX)
    _move_usage_kind(bind, _LEGACY_USAGE_KIND, _CURRENT_USAGE_KIND)
    _move_citation_tokens(bind, f"{_CURRENT}_citations", _LEGACY_TOKEN_PREFIX, _CURRENT_TOKEN_PREFIX)


def downgrade() -> None:
    bind = op.get_bind()
    _move_citation_tokens(bind, f"{_CURRENT}_citations", _CURRENT_TOKEN_PREFIX, _LEGACY_TOKEN_PREFIX)
    _move_usage_kind(bind, _CURRENT_USAGE_KIND, _LEGACY_USAGE_KIND)
    _move_config_keys(bind, _CURRENT_CONFIG_PREFIX, _LEGACY_CONFIG_PREFIX)
    _move_tables(bind, _CURRENT, _LEGACY)
