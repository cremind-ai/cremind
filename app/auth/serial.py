"""Per-profile session-token generation counter — the revocation primitive.

Cremind's session tokens are stateless HS256 JWTs: there is no ``jti``, no
server-side session table, and no denylist. Signature + ``exp`` were the only
things ever checked, so a leaked token stayed usable for its whole 30-day life
and re-minting the token file just created a *second* valid token.

``profiles.token_serial`` closes that. Every minted token carries the profile's
serial as the ``tsr`` claim; :func:`serial_matches` compares it against the
stored value on every decode. Incrementing the serial therefore invalidates
every token issued to that profile — and only that profile — instantly.

This module owns the storage and the cache and knows nothing about JWTs; the
minting/verifying side lives in :mod:`app.auth.tokens`.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Mapping

from sqlalchemy import text

from app.databases import get_database_provider
from app.utils.logger import logger


#: JWT claim carrying the serial the token was minted at.
TOKEN_SERIAL_CLAIM = "tsr"


def _ttl_seconds() -> float:
    """Snapshot lifetime, overridable via ``CREMIND_TOKEN_SERIAL_CACHE_TTL``.

    ``0`` disables caching entirely (every check hits the DB) for operators who
    want strictly-immediate revocation across replicas.
    """
    raw = os.environ.get("CREMIND_TOKEN_SERIAL_CACHE_TTL")
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            logger.warning(
                f"[auth] ignoring invalid CREMIND_TOKEN_SERIAL_CACHE_TTL={raw!r}"
            )
    return 5.0


_lock = threading.Lock()
_snapshot: dict[str, int] | None = None
_snapshot_at: float = 0.0


def all_serials(*, force: bool = False) -> dict[str, int]:
    """Every profile's current serial, from a short-lived cached snapshot.

    One whole-table read serves every profile, so the auth hot path costs at
    most one query per TTL per process rather than one per request. (For
    context: resolving the JWT secret already runs an *uncached* query on every
    authenticated request, so this is cheaper than what it sits next to.)

    A snapshot rather than per-profile caching because profile counts are tiny,
    and because a profile created *after* the snapshot is simply absent →
    serial 0 → its freshly-minted ``tsr: 0`` token validates, which is correct
    with no negative-cache special case.

    Staleness: a revoked token keeps working for at most the TTL (5s) in
    processes other than the one that rotated it. That window exists because
    write-invalidation alone cannot reach a second Helm replica or the separate
    OS process behind ``cremind auth regenerate --local``. The rotating process
    itself is always immediately consistent — :func:`bump_serial` invalidates.
    """
    global _snapshot, _snapshot_at

    ttl = _ttl_seconds()
    now = time.monotonic()
    with _lock:
        if not force and _snapshot is not None and (now - _snapshot_at) < ttl:
            return _snapshot
        try:
            engine = get_database_provider().sync_engine()
            with engine.connect() as conn:
                rows = conn.execute(
                    text("SELECT name, token_serial FROM profiles")
                ).fetchall()
            _snapshot = {row[0]: int(row[1] or 0) for row in rows}
            _snapshot_at = now
        except Exception as e:  # noqa: BLE001 — must never break authentication
            # Serve the last good snapshot. Falling back to ``{}`` would read as
            # "every profile is at serial 0" and quietly accept every revoked
            # token — a transient DB blip must not undo a revocation.
            logger.warning(f"[auth] token-serial snapshot refresh failed: {e}")
            if _snapshot is None:
                return {}
            _snapshot_at = now  # back off instead of retrying per request
        return _snapshot


def current_serial(profile: str) -> int:
    """The profile's current serial; ``0`` for an unknown profile."""
    if not profile:
        return 0
    return all_serials().get(profile, 0)


def invalidate_serial_cache() -> None:
    """Force the next :func:`all_serials` call to re-read the DB."""
    global _snapshot, _snapshot_at
    with _lock:
        _snapshot = None
        _snapshot_at = 0.0


def bump_serial(profile: str) -> int:
    """Increment ``profile``'s serial and return the new value.

    Every token issued to the profile before this call becomes invalid (within
    the cache TTL on other processes; immediately here).

    Raises ``LookupError`` if the profile has no row.
    """
    if not profile:
        raise LookupError("profile is required")

    engine = get_database_provider().sync_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE profiles SET token_serial = token_serial + 1, "
                "updated_at = :now WHERE name = :name"
            ),
            {"name": profile, "now": time.time()},
        )
        # Read back inside the same transaction rather than using RETURNING,
        # which isn't available on older SQLite builds.
        row = conn.execute(
            text("SELECT token_serial FROM profiles WHERE name = :name"),
            {"name": profile},
        ).fetchone()

    invalidate_serial_cache()
    if row is None:
        raise LookupError(f"profile {profile!r} not found")
    return int(row[0])


def serial_matches(payload: Mapping[str, Any]) -> bool:
    """Whether a decoded token's ``tsr`` claim matches the profile's serial.

    A **missing claim is read as 0**, not as "skip the check". That single rule
    gives both properties we need, because the migration backfills every
    existing row to 0:

    * a token minted before this feature (no claim) keeps working until its
      profile is first rotated — upgrades don't log anyone out;
    * once rotated, that same legacy token is rejected. Treating a missing
      claim as "always valid" would instead leave every token in existence
      today permanently un-revokable, which is exactly what this exists to fix.
    """
    claimed = payload.get(TOKEN_SERIAL_CLAIM, 0)
    if isinstance(claimed, bool) or not isinstance(claimed, int):
        # Reject strings, floats, None, containers — a well-formed token always
        # carries an int, so anything else is a forgery or a broken minter.
        return False
    profile = payload.get("profile") or payload.get("sub") or ""
    return claimed == current_serial(str(profile))
