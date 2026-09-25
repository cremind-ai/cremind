"""Sync storage for deep-research jobs over the user's documents.

Schema lives in :mod:`app.storage.models` (``userdoc_research_jobs``); the table
is created by Alembic. What the rows *mean* — the status machine, delivery, the
runner — lives in :mod:`app.userdocs.research.jobs`; this class only reads and
writes them.

Synchronous for the same reason as :mod:`app.storage.userdocs_storage`: every
caller runs it through ``asyncio.to_thread`` (the job runner's checkpoints, the
tool, the REST API) or from a User Document Search worker thread (purge).

Two things are load-bearing:

**Every read and write that a caller can reach names its profile.** A job id
from another profile must read as "not found", never as someone else's job.
The id-only methods (:meth:`get_by_id`, :meth:`claim_delivery`, the boot
sweep) are for the runner and the delivery path, which got the id from a row
they already hold.

**Delivery is claimed atomically.** :meth:`claim_delivery` is a
compare-and-set on ``(rev, delivered_rev)``, so the tool's own reply, the
turn-end hook, the runner and the boot sweep can race on one job and exactly
one of them delivers each ``rev``.
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from sqlalchemy import and_, delete, func, insert, not_, select, update

from app.databases import DatabaseProvider
from app.storage._sync_base import SyncStorageBase
from app.storage.models import UserDocResearchJobModel

_JOBS = UserDocResearchJobModel.__table__

# Kept in step with app.userdocs.research.types (ACTIVE / WAITING); spelled out
# here so the storage layer does not import the feature package.
_ACTIVE = ("queued", "planning", "running")
_WAITING = ("needs_clarification", "needs_confirmation", "interrupted")

# The list view: everything but the big JSON columns.
_LIST_COLUMNS = (
    _JOBS.c.id, _JOBS.c.profile, _JOBS.c.conversation_id, _JOBS.c.run_id, _JOBS.c.status,
    _JOBS.c.phase, _JOBS.c.mode, _JOBS.c.domain, _JOBS.c.question, _JOBS.c.model_group,
    _JOBS.c.provider, _JOBS.c.model, _JOBS.c.tokens_in, _JOBS.c.tokens_out, _JOBS.c.budget,
    _JOBS.c.elapsed_s, _JOBS.c.rev, _JOBS.c.delivered_rev, _JOBS.c.error, _JOBS.c.created_at,
    _JOBS.c.updated_at, _JOBS.c.finished_at,
)

# Columns a caller may write through :meth:`update` / :meth:`bump_rev`.
# ``profile``, ``id``, ``rev`` and ``delivered_rev`` are owned by this class.
_WRITABLE = frozenset({
    "conversation_id", "run_id", "status", "phase", "scope", "reference_scope", "answers",
    "state", "dossier", "model_group", "provider", "model", "tokens_in", "tokens_out",
    "budget", "elapsed_s", "error", "finished_at",
})
# …plus what a job is, fixed at creation.
_CREATABLE = _WRITABLE | {"mode", "domain", "question"}


def _now_ms() -> float:
    return time.time() * 1000


def _row(row: Any) -> dict[str, Any] | None:
    return dict(row._mapping) if row is not None else None


def _check(values: dict[str, Any], allowed: frozenset[str] = _WRITABLE) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ValueError(f"unknown userdoc_research_jobs fields: {sorted(unknown)}")


class UserDocResearchStorage(SyncStorageBase):
    """CRUD for ``userdoc_research_jobs``."""

    def __init__(self, provider: DatabaseProvider | None = None):
        super().__init__(provider)

    @property
    def provider(self) -> DatabaseProvider:
        # Follow the rest of User Document Search's main-database state (see
        # the same property on the citations storage): the purge that deletes
        # captions and settings must delete jobs from the same database.
        if self._provider_override is not None:
            return self._provider_override
        from app.storage.userdocs_storage import get_userdocs_storage

        return get_userdocs_storage().provider

    # ── create / read ─────────────────────────────────────────────────────

    def create(self, *, job_id: str, profile: str, **values: Any) -> dict[str, Any]:
        _check(values, _CREATABLE)
        now = _now_ms()
        with self._engine.begin() as conn:
            conn.execute(insert(_JOBS).values(
                id=job_id, profile=profile, rev=0, delivered_rev=0,
                created_at=now, updated_at=now, **values,
            ))
        return self.get_by_id(job_id) or {}

    def get(self, profile: str, job_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(_JOBS).where(and_(_JOBS.c.id == job_id, _JOBS.c.profile == profile))
            ).fetchone()
        return _row(row)

    def get_by_id(self, job_id: str) -> dict[str, Any] | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(_JOBS).where(_JOBS.c.id == job_id)).fetchone()
        return _row(row)

    def exists(self, job_id: str) -> bool:
        with self._engine.connect() as conn:
            return conn.execute(select(_JOBS.c.id).where(_JOBS.c.id == job_id)).fetchone() is not None

    def list(self, profile: str, *, limit: int = 20, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """The profile's jobs, newest first, without the JSON columns."""
        stmt = select(*_LIST_COLUMNS).where(_JOBS.c.profile == profile)
        if conversation_id is not None:
            stmt = stmt.where(_JOBS.c.conversation_id == conversation_id)
        stmt = stmt.order_by(_JOBS.c.created_at.desc()).limit(max(1, int(limit)))
        with self._engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt).fetchall()]

    def active_for_profile(self, profile: str) -> list[dict[str, Any]]:
        """The profile's queued/planning/running jobs (at most one, unless a
        crash left a stale row behind)."""
        with self._engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(*_LIST_COLUMNS)
                .where(and_(_JOBS.c.profile == profile, _JOBS.c.status.in_(_ACTIVE)))
                .order_by(_JOBS.c.created_at)
            ).fetchall()]

    def count(self, profile: str) -> int:
        with self._engine.connect() as conn:
            return int(conn.execute(
                select(func.count()).select_from(_JOBS).where(_JOBS.c.profile == profile)
            ).scalar() or 0)

    # ── writes ────────────────────────────────────────────────────────────

    def update(
        self, profile: str, job_id: str, *, expect: Iterable[str] | None = None, **values: Any,
    ) -> bool:
        """Apply ``values``; with ``expect``, only while the job's status is
        one of those (a late checkpoint must not overwrite a terminal write).
        Returns whether a row changed."""
        _check(values)
        cond = [_JOBS.c.id == job_id, _JOBS.c.profile == profile]
        if expect is not None:
            cond.append(_JOBS.c.status.in_(list(expect)))
        with self._engine.begin() as conn:
            return conn.execute(
                update(_JOBS).where(and_(*cond)).values(updated_at=_now_ms(), **values)
            ).rowcount > 0

    def bump_rev(
        self, profile: str, job_id: str, *, quiet: bool = False, expect: Iterable[str] | None = None,
        **values: Any,
    ) -> bool:
        """A transition into a state the requester must hear about: apply
        ``values`` and bump ``rev`` in the same statement. ``quiet`` marks the
        new state delivered at once (nobody is owed a message about it — a
        cancellation the requester asked for). Both SET expressions read the
        pre-update ``rev``, on SQLite and PostgreSQL alike."""
        _check(values)
        cond = [_JOBS.c.id == job_id, _JOBS.c.profile == profile]
        if expect is not None:
            cond.append(_JOBS.c.status.in_(list(expect)))
        extra: dict[str, Any] = {"rev": _JOBS.c.rev + 1}
        if quiet:
            extra["delivered_rev"] = _JOBS.c.rev + 1
        with self._engine.begin() as conn:
            return conn.execute(
                update(_JOBS).where(and_(*cond)).values(updated_at=_now_ms(), **extra, **values)
            ).rowcount > 0

    # ── delivery ──────────────────────────────────────────────────────────

    def claim_delivery(self, job_id: str) -> tuple[int, int] | None:
        """Claim the job's current state for delivery: ``delivered_rev = rev``
        where ``delivered_rev < rev``. A compare-and-set on the pair read in
        the same transaction, so of several racing claimants exactly one wins.
        Returns ``(claimed_rev, previous_delivered_rev)`` — what
        :meth:`release_delivery` needs — or None when there was nothing to
        claim or another claimant won."""
        with self._engine.begin() as conn:
            row = conn.execute(
                select(_JOBS.c.rev, _JOBS.c.delivered_rev).where(_JOBS.c.id == job_id)
            ).fetchone()
            if row is None:
                return None
            rev, prior = int(row[0] or 0), int(row[1] or 0)
            if prior >= rev:
                return None
            won = conn.execute(
                update(_JOBS)
                .where(and_(_JOBS.c.id == job_id, _JOBS.c.rev == rev, _JOBS.c.delivered_rev == prior))
                .values(delivered_rev=rev)
            ).rowcount
        return (rev, prior) if won else None

    def release_delivery(self, job_id: str, claimed_rev: int, prior: int) -> bool:
        """Undo a claim this caller took moments ago (its enqueue failed), so
        a later hook retries. Only while the claim is still the one taken —
        a newer state or a newer claim is left alone."""
        with self._engine.begin() as conn:
            return conn.execute(
                update(_JOBS)
                .where(and_(
                    _JOBS.c.id == job_id, _JOBS.c.rev == claimed_rev,
                    _JOBS.c.delivered_rev == claimed_rev,
                ))
                .values(delivered_rev=prior)
            ).rowcount > 0

    def mark_collected(self, profile: str, job_id: str) -> bool:
        """The requester was shown the job's current state: nothing is owed."""
        with self._engine.begin() as conn:
            return conn.execute(
                update(_JOBS)
                .where(and_(_JOBS.c.id == job_id, _JOBS.c.profile == profile))
                .values(delivered_rev=_JOBS.c.rev)
            ).rowcount > 0

    def list_undelivered(self, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """Jobs owed to a conversation: a state not yet shown (``delivered_rev
        < rev``) that is not a running one (a job continued since has moved
        on). REST/CLI jobs have no conversation and are never owed. Oldest
        first."""
        cond = [
            _JOBS.c.conversation_id.is_not(None),
            _JOBS.c.delivered_rev < _JOBS.c.rev,
            not_(_JOBS.c.status.in_(_ACTIVE)),
        ]
        if conversation_id is not None:
            cond.append(_JOBS.c.conversation_id == conversation_id)
        with self._engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(
                select(*_LIST_COLUMNS).where(and_(*cond)).order_by(_JOBS.c.updated_at)
            ).fetchall()]

    # ── lifecycle ─────────────────────────────────────────────────────────

    def mark_active_interrupted(self, exclude: Iterable[str] = ()) -> list[str]:
        """Boot: every running job whose process is gone becomes
        ``interrupted`` — a deliverable state, so its conversation hears it
        stopped and can resume it. ``exclude`` names jobs this process is
        running. Returns the ids changed."""
        skip = set(exclude)
        now = _now_ms()
        with self._engine.begin() as conn:
            ids = [
                r[0] for r in conn.execute(
                    select(_JOBS.c.id).where(_JOBS.c.status.in_(_ACTIVE))
                ).fetchall()
                if r[0] not in skip
            ]
            for job_id in ids:
                conn.execute(
                    update(_JOBS)
                    .where(and_(_JOBS.c.id == job_id, _JOBS.c.status.in_(_ACTIVE)))
                    .values(status="interrupted", rev=_JOBS.c.rev + 1, updated_at=now)
                )
        return ids

    def prune(self, profile: str, keep: int = 50) -> list[str]:
        """Retention: delete the profile's jobs past the newest ``keep``,
        never one that is running or waiting for an answer. Returns the ids
        deleted, so their artifacts can go too."""
        keep = max(0, int(keep))
        with self._engine.begin() as conn:
            ids = [r[0] for r in conn.execute(
                select(_JOBS.c.id, _JOBS.c.status)
                .where(_JOBS.c.profile == profile)
                .order_by(_JOBS.c.created_at.desc(), _JOBS.c.id.desc())
            ).fetchall()]
            if len(ids) <= keep:
                return []
            old = [
                r[0] for r in conn.execute(
                    select(_JOBS.c.id).where(and_(
                        _JOBS.c.profile == profile,
                        _JOBS.c.id.in_(ids[keep:]),
                        not_(_JOBS.c.status.in_(_ACTIVE + _WAITING)),
                    ))
                ).fetchall()
            ]
            if old:
                conn.execute(delete(_JOBS).where(and_(_JOBS.c.profile == profile, _JOBS.c.id.in_(old))))
        return old

    def delete_profile(self, profile: str) -> int:
        """Purge set P: every research job of the profile."""
        with self._engine.begin() as conn:
            return conn.execute(delete(_JOBS).where(_JOBS.c.profile == profile)).rowcount

    # The contract's name for the same purge.
    purge_profile = delete_profile


_instance: UserDocResearchStorage | None = None


def get_userdocs_research_storage(provider: DatabaseProvider | None = None) -> UserDocResearchStorage:
    global _instance
    if _instance is None:
        _instance = UserDocResearchStorage(provider)
    return _instance
