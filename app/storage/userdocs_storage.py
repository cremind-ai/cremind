"""Sync storage for the main-database half of User Document Search.

Schema lives in :mod:`app.storage.models` (``userdoc_sources``,
``userdoc_captions``, ``userdoc_vision_usage``); the tables are created by
Alembic. This class only reads and writes them. The index itself (files,
chunks, the full-text index) is a per-profile SQLite file owned by
:mod:`app.userdocs.index` and never goes through here.

Synchronous on purpose: every caller is either a sync worker thread of the
indexer or an API handler that wraps the call in ``asyncio.to_thread``. The
statements are one-row lookups and upserts, so a sync engine is the simpler
fit — the same reasoning as :mod:`app.storage.dynamic_config_storage`.

Two things are load-bearing:

**Every read and write names its profile.** There is no default, unlike
``DynamicConfigStorage.get(..., profile="admin")``: a forgotten argument there
silently reads the admin's row, and here that would hand one profile another's
folder.

**The caption quota is reserved atomically.** :meth:`reserve_vision` is one
conditional ``UPDATE … WHERE captions < :cap``, so two workers racing for the
last slot of the day cannot both win it.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.exc import IntegrityError

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase
from app.storage.models import (
    UserDocCaptionModel,
    UserDocSourceModel,
    UserDocVisionUsageModel,
)

_SOURCES = UserDocSourceModel.__table__
_CAPTIONS = UserDocCaptionModel.__table__
_USAGE = UserDocVisionUsageModel.__table__

# Columns a caller may set through :meth:`upsert_source`. Everything else
# (id, profile, kind, timestamps) is owned by this class.
_SOURCE_FIELDS = frozenset({
    "enabled", "root_mode", "root_path", "excludes", "options",
    "first_sync_confirmed_at",
})


def _now_ms() -> float:
    return time.time() * 1000


def _row(row: Any) -> dict[str, Any] | None:
    return dict(row._mapping) if row is not None else None


class UserDocsStorage(SyncStorageBase):
    """CRUD for the three User Document Search tables in the main database."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    # ── sources ───────────────────────────────────────────────────────────

    def get_source(self, profile: str, kind: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(_SOURCES).where(
                    and_(_SOURCES.c.profile == profile, _SOURCES.c.kind == kind)
                )
            ).fetchone()
        return _row(row)

    def list_sources(self, profile: str | None = None) -> list[dict[str, Any]]:
        """Every source row, or one profile's. ``None`` means all profiles —
        only the indexer's boot sweep and the admin overview ask for that."""
        stmt = select(_SOURCES)
        if profile is not None:
            stmt = stmt.where(_SOURCES.c.profile == profile)
        with self._engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt).fetchall()]

    def upsert_source(self, profile: str, kind: str, **fields: Any) -> dict[str, Any]:
        """Create the (profile, kind) row if it is missing, then apply ``fields``.

        Never raises on a concurrent create: two requests enabling the feature
        at once both insert, one loses on ``UNIQUE(profile, kind)``, and the
        loser applies its fields to the winner's row.
        """
        unknown = set(fields) - _SOURCE_FIELDS
        if unknown:
            raise ValueError(f"unknown userdoc_sources fields: {sorted(unknown)}")
        now = _now_ms()
        if self.get_source(profile, kind) is None:
            try:
                with self._engine.begin() as conn:
                    conn.execute(insert(_SOURCES).values(
                        id=str(uuid.uuid4()),
                        profile=profile,
                        kind=kind,
                        created_at=now,
                        updated_at=now,
                        **fields,
                    ))
                return self.get_source(profile, kind) or {}
            except IntegrityError:
                pass  # created concurrently — fall through and update it
        if fields:
            with self._engine.begin() as conn:
                conn.execute(
                    update(_SOURCES)
                    .where(and_(_SOURCES.c.profile == profile, _SOURCES.c.kind == kind))
                    .values(updated_at=now, **fields)
                )
        return self.get_source(profile, kind) or {}

    def delete_source(self, profile: str, kind: str) -> bool:
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(_SOURCES).where(
                    and_(_SOURCES.c.profile == profile, _SOURCES.c.kind == kind)
                )
            )
        return result.rowcount > 0

    def profile_uid(self, profile: str) -> str | None:
        """The profile's immutable uuid (``profiles.id``).

        The index file and vector collections are keyed by this, not by the
        name, so a profile deleted and re-created under the same name starts
        with an empty index instead of inheriting the old one.
        """
        from app.storage.models import ProfileModel

        t = ProfileModel.__table__
        with self._engine.connect() as conn:
            row = conn.execute(select(t.c.id).where(t.c.name == profile)).fetchone()
        return str(row[0]) if row and row[0] else None

    def profile_uids(self) -> dict[str, str]:
        """``{uid: name}`` for every live profile — the garbage collector's
        list of index directories and collections that must be kept."""
        from app.storage.models import ProfileModel

        t = ProfileModel.__table__
        with self._engine.connect() as conn:
            return {str(r[0]): str(r[1]) for r in conn.execute(select(t.c.id, t.c.name)).fetchall() if r[0]}

    # ── caption cache ─────────────────────────────────────────────────────

    def get_caption(self, profile: str, sha256: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(_CAPTIONS).where(
                    and_(_CAPTIONS.c.profile == profile, _CAPTIONS.c.sha256 == sha256)
                )
            ).fetchone()
        return _row(row)

    def put_caption(
        self,
        profile: str,
        sha256: str,
        *,
        variant: str,
        caption_text: str,
        caption_json: dict[str, Any] | None = None,
        provider: str | None = None,
        model: str | None = None,
        prompt_version: int = 1,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Store a caption. A second write for the same image replaces the
        first — only a re-caption (new prompt version) ever does that."""
        values = {
            "variant": variant,
            "caption_text": caption_text,
            "caption_json": caption_json,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "created_at": _now_ms(),
        }
        with self._engine.begin() as conn:
            done = conn.execute(
                update(_CAPTIONS)
                .where(and_(_CAPTIONS.c.profile == profile, _CAPTIONS.c.sha256 == sha256))
                .values(**values)
            ).rowcount
            if not done:
                conn.execute(insert(_CAPTIONS).values(profile=profile, sha256=sha256, **values))

    def delete_captions(self, profile: str) -> int:
        with self._engine.begin() as conn:
            return conn.execute(
                delete(_CAPTIONS).where(_CAPTIONS.c.profile == profile)
            ).rowcount

    # ── vision quota ──────────────────────────────────────────────────────

    def _ensure_usage_row(self, profile: str, day: str) -> None:
        try:
            with self._engine.begin() as conn:
                exists = conn.execute(
                    select(_USAGE.c.day).where(
                        and_(_USAGE.c.profile == profile, _USAGE.c.day == day)
                    )
                ).fetchone()
                if exists is None:
                    conn.execute(insert(_USAGE).values(
                        profile=profile, day=day, captions=0, ocr_pages=0, tokens=0,
                    ))
        except IntegrityError:
            pass  # another worker created today's row first — that's fine

    def reserve_vision(self, profile: str, day: str, cap: int, *, ocr: bool = False) -> bool:
        """Take one captioning slot for ``day`` if ``cap`` allows it.

        Returns False when the day is used up. Scanned-page OCR counts against
        the same cap as photo captions — they cost the same.
        """
        if cap <= 0:
            return False
        self._ensure_usage_row(profile, day)
        extra = {"ocr_pages": _USAGE.c.ocr_pages + 1} if ocr else {}
        with self._engine.begin() as conn:
            taken = conn.execute(
                update(_USAGE)
                .where(and_(
                    _USAGE.c.profile == profile,
                    _USAGE.c.day == day,
                    _USAGE.c.captions < cap,
                ))
                .values(captions=_USAGE.c.captions + 1, **extra)
            ).rowcount
        return taken > 0

    def refund_vision(self, profile: str, day: str, *, ocr: bool = False) -> None:
        """Give back a slot whose vision call failed, so a flaky provider does
        not quietly eat the day's quota."""
        extra = {"ocr_pages": _USAGE.c.ocr_pages - 1} if ocr else {}
        with self._engine.begin() as conn:
            conn.execute(
                update(_USAGE)
                .where(and_(
                    _USAGE.c.profile == profile,
                    _USAGE.c.day == day,
                    _USAGE.c.captions > 0,
                ))
                .values(captions=_USAGE.c.captions - 1, **extra)
            )

    def add_vision_tokens(self, profile: str, day: str, tokens: int) -> None:
        if tokens <= 0:
            return
        self._ensure_usage_row(profile, day)
        with self._engine.begin() as conn:
            conn.execute(
                update(_USAGE)
                .where(and_(_USAGE.c.profile == profile, _USAGE.c.day == day))
                .values(tokens=_USAGE.c.tokens + int(tokens))
            )

    def vision_usage(self, profile: str, day: str) -> dict[str, int]:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(_USAGE).where(and_(_USAGE.c.profile == profile, _USAGE.c.day == day))
            ).fetchone()
        if row is None:
            return {"captions": 0, "ocr_pages": 0, "tokens": 0}
        m = row._mapping
        return {"captions": int(m["captions"]), "ocr_pages": int(m["ocr_pages"]), "tokens": int(m["tokens"])}

    def delete_vision_usage(self, profile: str) -> int:
        with self._engine.begin() as conn:
            return conn.execute(delete(_USAGE).where(_USAGE.c.profile == profile)).rowcount


_instance: UserDocsStorage | None = None


def get_userdocs_storage(provider: DatabaseProvider | None = None) -> UserDocsStorage:
    global _instance
    if _instance is None:
        _instance = UserDocsStorage(provider)
    return _instance
