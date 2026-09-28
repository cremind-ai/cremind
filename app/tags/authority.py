"""The grant authority and the recovery-vault master key (cremind-tag
``docs/connect-setup.md`` §3.1, §10).

Two secrets live OUTSIDE the database, in ``<SYS>/.tag-authority/`` (a name no
profile can take — profile names are ``[a-z0-9_-]+`` — hidden from every file
API by :data:`app.utils.credential_paths.CREDENTIAL_DIR_NAMES` and excluded
from ordinary backups by :mod:`app.backup.rules`):

- ``signing-<kid>.key``: the Ed25519 seed that signs every grant. Devices pin
  its public half at their first pairing; lose it and they refuse every
  future ownership change.
- ``master-<kid>.key``: the AES-256 key that wraps each vault record's data
  key (:mod:`app.tags.vault`).

The ``tag_authority`` row (in the database, so it travels with backups)
names the key ids, the public key and an HMAC check of the master key, so a
restore that brings the row without the files — or with another
installation's files — is recognised instead of silently re-keyed:

- no row, no files → a fresh authority is created;
- row + matching files → ready;
- row, files missing or not matching, and nothing depends on them yet (no
  binding, no vault record) → a fresh authority replaces the row;
- otherwise → **unavailable**: grants and recovery fail with
  ``authority_unavailable`` / ``recovery_key_unavailable``. A key is never
  generated over existing bindings or vault data.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, insert, select, update

from app.storage.models import TagAuthorityModel, TagBindingModel, TagVaultModel

AUTHORITY = TagAuthorityModel.__table__
BINDINGS = TagBindingModel.__table__
VAULT = TagVaultModel.__table__

DIR_NAME = ".tag-authority"
ROW_ID = "authority"
GRANT_LABEL = b"cremind-tag/v2/grant"
_MASTER_CHECK_LABEL = b"cremind-tag/vault/master-check"

_lock = threading.Lock()


class AuthorityUnavailable(Exception):
    """The keys the stored authority needs are not on this installation.

    ``code`` is ``authority_unavailable`` (signing) or
    ``recovery_key_unavailable`` (vault)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Authority:
    installation_id: str
    authority_pub: bytes
    signing_kid: str
    master_kid: str
    _signing_seed: bytes
    _master_key: bytes

    @property
    def authority_id(self) -> bytes:
        return hashlib.sha256(self.authority_pub).digest()[:16]

    def sign(self, message: bytes) -> bytes:
        return Ed25519PrivateKey.from_private_bytes(self._signing_seed).sign(message)

    @property
    def master_key(self) -> bytes:
        return self._master_key

    def __repr__(self) -> str:  # never print key material
        return (f"Authority(installation_id={self.installation_id!r}, "
                f"authority_pub={self.authority_pub.hex()!r}, signing_kid={self.signing_kid!r}, "
                f"master_kid={self.master_kid!r})")


def directory() -> Path:
    from app.config.settings import BaseConfig

    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / DIR_NAME


def _write_private(path: Path, data: bytes) -> None:
    """Create ``path`` with owner-only permissions, atomically; never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError(str(path))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_key(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if len(data) == 32 else None


def master_check(master_key: bytes) -> str:
    return hmac.new(master_key, _MASTER_CHECK_LABEL, hashlib.sha256).hexdigest()


def _public(seed: bytes) -> bytes:
    return Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def _load_files(row: Any) -> tuple[bytes | None, bytes | None]:
    base = directory()
    seed = _read_key(base / f"signing-{row.signing_kid}.key")
    master = _read_key(base / f"master-{row.master_kid}.key")
    if seed is not None and _public(seed).hex() != row.authority_pub:
        seed = None
    if master is not None and not hmac.compare_digest(master_check(master), row.master_check):
        master = None
    return seed, master


def _new_keys() -> tuple[str, bytes, str, bytes]:
    signing_kid, master_kid = secrets.token_hex(8), secrets.token_hex(8)
    seed, master = secrets.token_bytes(32), secrets.token_bytes(32)
    base = directory()
    _write_private(base / f"signing-{signing_kid}.key", seed)
    _write_private(base / f"master-{master_kid}.key", master)
    return signing_kid, seed, master_kid, master


def _write_pointer(values: dict[str, Any]) -> None:
    """``authority.json``: which key files are current (informational)."""
    path = directory() / "authority.json"
    tmp = path.with_name(f".authority.{secrets.token_hex(6)}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({k: values[k] for k in ("installation_id", "authority_pub", "signing_kid", "master_kid")},
                      handle, indent=2)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _dependents(conn) -> int:
    bindings = conn.execute(select(func.count()).select_from(BINDINGS)).scalar_one()
    vault = conn.execute(select(func.count()).select_from(VAULT)).scalar_one()
    return int(bindings or 0) + int(vault or 0)


def _ensure_sync(sync_conn) -> Authority:
    now = time.time() * 1000
    row = sync_conn.execute(select(AUTHORITY).where(AUTHORITY.c.id == ROW_ID)).first()
    if row is not None:
        seed, master = _load_files(row)
        if seed is not None and master is not None:
            return Authority(row.installation_id, bytes.fromhex(row.authority_pub), row.signing_kid,
                             row.master_kid, seed, master)
        if _dependents(sync_conn):
            if seed is None:
                raise AuthorityUnavailable(
                    "authority_unavailable",
                    "This server no longer has the signing key its paired devices trust. Restore the "
                    "key from an encrypted backup made with the tag recovery keys.")
            raise AuthorityUnavailable(
                "recovery_key_unavailable",
                "This server no longer has the key that protects its tag recovery data. Restore the "
                "key from an encrypted backup made with the tag recovery keys.")
    signing_kid, seed, master_kid, master = _new_keys()
    values = {
        "id": ROW_ID, "installation_id": row.installation_id if row is not None else str(uuid.uuid4()),
        "authority_pub": _public(seed).hex(), "signing_kid": signing_kid, "master_kid": master_kid,
        "master_check": master_check(master), "created_at": now, "updated_at": now,
    }
    if row is None:
        sync_conn.execute(insert(AUTHORITY), [values])
    else:
        sync_conn.execute(update(AUTHORITY).where(AUTHORITY.c.id == ROW_ID).values(
            **{k: v for k, v in values.items() if k not in ("id", "created_at")}))
    _write_pointer(values)
    return Authority(values["installation_id"], _public(seed), signing_kid, master_kid, seed, master)


_cache: dict[tuple[str, str, str, str], Authority] = {}
_creating: dict[int, asyncio.Lock] = {}


def _cached_sync(sync_conn) -> Authority | None:
    """The authority when its row exists and its keys were loaded before
    (one SELECT; the key files are read once per row version)."""
    row = sync_conn.execute(select(AUTHORITY).where(AUTHORITY.c.id == ROW_ID)).first()
    if row is None:
        return None
    key = (row.authority_pub, row.signing_kid, row.master_kid, row.master_check)
    hit = _cache.get(key)
    if hit is None:
        seed, master = _load_files(row)
        if seed is None or master is None:
            return None
        hit = Authority(row.installation_id, bytes.fromhex(row.authority_pub), row.signing_kid,
                        row.master_kid, seed, master)
        with _lock:
            _cache.clear()
            _cache[key] = hit
    return hit


async def get_authority() -> Authority:
    """The ready authority (created on first use). Raises
    :class:`AuthorityUnavailable` when its keys are missing and something
    depends on them.

    Call it BEFORE opening a write transaction: creating the authority
    commits in a transaction of its own (serialised by an asyncio lock —
    never a thread lock held across an ``await``), which on SQLite would wait
    forever behind a caller that already holds the write lock."""
    from app.tags.storage import get_tag_storage

    async with get_tag_storage().engine.connect() as own:
        hit = await own.run_sync(_cached_sync)
    if hit is not None:
        return hit
    loop_lock = _creating.setdefault(id(asyncio.get_running_loop()), asyncio.Lock())
    async with loop_lock:
        async with get_tag_storage().engine.begin() as own:
            return await own.run_sync(_ensure_sync)


def reset_cache() -> None:
    """Forget loaded keys (tests; after a restore replaced the row)."""
    with _lock:
        _cache.clear()


def authority_status_sync(sync_conn) -> dict[str, Any]:
    """For diagnostics: ``{state: ready|unavailable|absent, code?, installation_id?, authority_id?}``."""
    row = sync_conn.execute(select(AUTHORITY).where(AUTHORITY.c.id == ROW_ID)).first()
    if row is None:
        return {"state": "absent"}
    seed, master = _load_files(row)
    out = {"installation_id": row.installation_id,
           "authority_id": hashlib.sha256(bytes.fromhex(row.authority_pub)).digest()[:16].hex()}
    if seed is not None and master is not None:
        return {"state": "ready", **out}
    return {"state": "unavailable", "code": "authority_unavailable" if seed is None else "recovery_key_unavailable",
            **out}


def key_files() -> list[Path]:
    """Every file of the authority directory (for an explicitly requested,
    encrypted backup)."""
    base = directory()
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_file() and not p.name.startswith("."))


__all__ = [
    "Authority", "AuthorityUnavailable", "DIR_NAME", "GRANT_LABEL", "authority_status_sync", "directory",
    "get_authority", "key_files", "master_check", "reset_cache",
]
