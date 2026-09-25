"""Sync storage for the User Document Search citation registry.

Schema lives in :mod:`app.storage.models` (``userdoc_citations``); the table is
created by Alembic. The meaning of the rows — what counts as "issued", how an
answer's tokens are verified — lives in :mod:`app.userdocs.citations`; this
class only reads and writes them.

Synchronous for the same reason as :mod:`app.storage.userdocs_storage`: the
writers are User Documents tools running in ``asyncio.to_thread`` and the
readers are the finalizer and the file API, which call it from worker threads.

**Every read and write names its profile.** A token is meaningless outside the
profile that issued it (cite ids are allocated per profile index), so a lookup
without the profile would let one profile's answer "verify" against another's
registry.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Iterable

from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase
from app.storage.models import ConversationModel, UserDocCitationModel

_CIT = UserDocCitationModel.__table__
_CONV = ConversationModel.__table__

# Bound parameters per IN (...) list, portable to old SQLite builds.
_BATCH = 500

# Columns a caller supplies for one issued token. Everything else (id,
# profile, conversation, issued_at) is owned by this class.
_ROW_FIELDS = (
    "token", "cite_id", "target", "ref_id", "text_hash", "source_kind", "locator",
    "label", "rel_path", "snippet", "leaf", "web_link",
)


def _row(row: Any) -> dict[str, Any]:
    return dict(row._mapping)


class UserDocCitationsStorage(SyncStorageBase):
    """CRUD for ``userdoc_citations``."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    @property
    def provider(self) -> DatabaseProvider:
        # Without an explicit provider, follow the rest of User Document
        # Search's main-database state rather than resolving on our own: the
        # purge that deletes captions and settings must delete citations from
        # the same database, including when that storage was pointed at
        # another one (the setup wizard's hot swap, tests).
        if self._provider_override is not None:
            return self._provider_override
        from app.storage.userdocs_storage import get_userdocs_storage

        return get_userdocs_storage().provider

    # ── conversations ─────────────────────────────────────────────────────

    def resolve_conversation(self, profile: str, context_id: str) -> str | None:
        """The conversation a tool's ``_context_id`` belongs to, or None.

        A web conversation's context id is back-filled to its own id; a
        channel/A2A conversation stores the platform's context id. Either way
        the row must be this profile's — a context id is only unique within a
        profile.
        """
        if not context_id:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(
                select(_CONV.c.id).where(and_(
                    _CONV.c.profile == profile, _CONV.c.context_id == context_id,
                )).limit(1)
            ).fetchone()
            if row is None:
                row = conn.execute(
                    select(_CONV.c.id).where(and_(
                        _CONV.c.profile == profile, _CONV.c.id == context_id,
                    ))
                ).fetchone()
        return str(row[0]) if row is not None else None

    def owns_conversation(self, profile: str, conversation_id: str) -> bool:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(_CONV.c.id).where(and_(
                    _CONV.c.profile == profile, _CONV.c.id == conversation_id,
                ))
            ).fetchone()
        return row is not None

    # ── writes ────────────────────────────────────────────────────────────

    def issue(
        self, profile: str, conversation_id: str | None, rows: Iterable[dict[str, Any]],
    ) -> list[str]:
        """Record issued tokens; a token already recorded for the conversation
        is left as it is (the first snapshot wins). Returns the ids of the rows
        actually written.

        One ``INSERT … ON CONFLICT DO NOTHING`` on the conversation's unique key.
        Rows without a conversation (NULL never conflicts under SQL's NULL
        semantics) are de-duplicated by a lookup first instead.
        """
        now = time.time()
        values: dict[str, dict[str, Any]] = {}
        for r in rows:
            token = str(r.get("token") or "")
            if not token or token in values:
                continue
            v = {k: r.get(k) for k in _ROW_FIELDS}
            v.update(id=str(uuid.uuid4()), profile=profile, conversation_id=conversation_id, issued_at=now)
            values[token] = v
        if not values:
            return []
        if conversation_id is None:
            with self._engine.begin() as conn:
                have = {
                    r[0] for r in conn.execute(
                        select(_CIT.c.token).where(and_(
                            _CIT.c.profile == profile,
                            _CIT.c.conversation_id.is_(None),
                            _CIT.c.token.in_(list(values)),
                        ))
                    ).fetchall()
                }
                fresh = [v for t, v in values.items() if t not in have]
                if fresh:
                    conn.execute(insert(_CIT), fresh)
            return [v["id"] for v in fresh]

        engine = self._engine
        if engine.dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        elif engine.dialect.name in ("postgresql", "postgres"):
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        else:  # pragma: no cover — Cremind ships SQLite and PostgreSQL only
            raise NotImplementedError(f"no upsert for dialect {engine.dialect.name!r}")
        written: list[str] = []
        batch = list(values.values())
        with engine.begin() as conn:
            for i in range(0, len(batch), _BATCH):
                part = batch[i:i + _BATCH]
                stmt = dialect_insert(_CIT).values(part).on_conflict_do_nothing(
                    index_elements=["conversation_id", "token"],
                ).returning(_CIT.c.id)
                written += [r[0] for r in conn.execute(stmt).fetchall()]
        return written

    def bind_conversation(self, profile: str, row_ids: Iterable[str], conversation_id: str) -> int:
        """Attach rows issued before their conversation existed (the A2A path)
        to it. A token the conversation already has keeps its row; the
        duplicate is dropped. Returns the rows bound."""
        ids = [str(i) for i in row_ids]
        bound = 0
        for rid in ids:
            try:
                with self._engine.begin() as conn:
                    bound += conn.execute(
                        update(_CIT)
                        .where(and_(
                            _CIT.c.id == rid,
                            _CIT.c.profile == profile,
                            _CIT.c.conversation_id.is_(None),
                        ))
                        .values(conversation_id=conversation_id)
                    ).rowcount
            except IntegrityError:
                with self._engine.begin() as conn:
                    conn.execute(delete(_CIT).where(and_(_CIT.c.id == rid, _CIT.c.profile == profile)))
        return bound

    def delete_profile(self, profile: str) -> int:
        """Purge set P: every citation of the profile. Messages keep their own
        ``metadata.citations`` snapshots."""
        with self._engine.begin() as conn:
            return conn.execute(delete(_CIT).where(_CIT.c.profile == profile)).rowcount

    def delete_source_kind(self, profile: str, kind: str) -> int:
        """Purge set D's share of the registry: every citation of the
        profile's ``kind`` source (``"drive"``), the rest untouched. A Drive
        index removed after an unlink or a revocation must leave no token
        that still "verifies" against rows that are gone. Messages keep their
        own ``metadata.citations`` snapshots."""
        with self._engine.begin() as conn:
            return conn.execute(
                delete(_CIT).where(and_(_CIT.c.profile == profile, _CIT.c.source_kind == kind))
            ).rowcount

    # ── reads ─────────────────────────────────────────────────────────────

    def rows_for_cite_ids(self, profile: str, cite_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Every row of this profile whose cite id is in ``cite_ids``, oldest
        first — file tokens and chunk tokens of those files alike."""
        ids = list(dict.fromkeys(str(c) for c in cite_ids if c))
        out: list[dict[str, Any]] = []
        with self._engine.connect() as conn:
            for i in range(0, len(ids), _BATCH):
                part = ids[i:i + _BATCH]
                out += [
                    _row(r) for r in conn.execute(
                        select(_CIT)
                        .where(and_(_CIT.c.profile == profile, _CIT.c.cite_id.in_(part)))
                        .order_by(_CIT.c.issued_at)
                    ).fetchall()
                ]
        return out

    def count(self, profile: str, conversation_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(_CIT).where(_CIT.c.profile == profile)
        if conversation_id is not None:
            stmt = stmt.where(_CIT.c.conversation_id == conversation_id)
        with self._engine.connect() as conn:
            return int(conn.execute(stmt).scalar() or 0)


_instance: UserDocCitationsStorage | None = None


def get_userdocs_citations_storage(provider: DatabaseProvider | None = None) -> UserDocCitationsStorage:
    global _instance
    if _instance is None:
        _instance = UserDocCitationsStorage(provider)
    return _instance
