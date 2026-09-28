"""The account-recovery vault and sealed setup secrets (cremind-tag
``docs/connect-setup.md`` §10).

What the vault protects is a worker's operational state — tag roots, bridge
maintenance keys, the controller key, assignments, epoch floors — so that the
SAME profile can recover its hardware on a replacement computer. Cremind can
decrypt it (that is the point of account recovery), but only for an
authorised recovery of the owning profile (:mod:`app.tags.operations`).

Every saved version gets a fresh AES-256-GCM data key and nonce; the
associated data binds it to ``profile UUID | subject | generation | version``
(canonical JSON), so a record copied onto another profile, subject or
version does not open. The data key is wrapped with AES key wrap (RFC 3394)
under the authority's master key (:mod:`app.tags.authority`). Records name
the master key id; a record whose key is not this installation's fails with
``recovery_key_unavailable`` — never with garbage, never by re-keying.
"""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from app.tags.authority import Authority, AuthorityUnavailable


class VaultError(Exception):
    """A record could not be opened (tampered, wrong binding, wrong key)."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def _aad(context: dict[str, Any]) -> bytes:
    return json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seal(authority: Authority, plaintext: dict[str, Any], context: dict[str, Any]) -> dict[str, str]:
    """Encrypt ``plaintext`` (a JSON object) bound to ``context``. Returns the
    stored fields ``{key_id, wrapped_key, nonce, ciphertext}``."""
    data_key = AESGCM.generate_key(bit_length=256)
    nonce = secrets.token_bytes(12)
    body = json.dumps(plaintext, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(data_key).encrypt(nonce, body, _aad(context))
    return {
        "key_id": authority.master_kid,
        "wrapped_key": _b64(aes_key_wrap(authority.master_key, data_key)),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
    }


def open_record(authority: Authority, record: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Decrypt a stored record for ``context``. Raises
    :class:`AuthorityUnavailable` (``recovery_key_unavailable``) when it was
    sealed under another master key, :class:`VaultError` when it does not
    open."""
    if record.get("key_id") != authority.master_kid:
        raise AuthorityUnavailable(
            "recovery_key_unavailable",
            "This recovery data was protected by a key this server does not have.")
    try:
        data_key = aes_key_unwrap(authority.master_key, _unb64(record["wrapped_key"]))
        body = AESGCM(data_key).decrypt(_unb64(record["nonce"]), _unb64(record["ciphertext"]), _aad(context))
        value = json.loads(body.decode("utf-8"))
    except (InvalidUnwrap, ValueError, KeyError, TypeError) as exc:
        raise VaultError("the record does not open") from exc
    except Exception as exc:  # noqa: BLE001 - InvalidTag
        raise VaultError("the record does not open") from exc
    if not isinstance(value, dict):
        raise VaultError("the record is not an object")
    return value


def vault_context(owner_profile_id: str, subject: str, generation: int, version: int) -> dict[str, Any]:
    return {"profile": owner_profile_id, "subject": subject, "generation": int(generation), "version": int(version)}


def secret_context(operation_id: str, owner_profile_id: str) -> dict[str, Any]:
    return {"operation": operation_id, "profile": owner_profile_id, "purpose": "setup-secret"}


def seal_secret(authority: Authority, secret: bytes, operation_id: str, owner_profile_id: str) -> dict[str, str]:
    """Seal an operation's setup secret (kept only while the operation needs it)."""
    return seal(authority, {"setup_secret": secret.hex()}, secret_context(operation_id, owner_profile_id))


def open_secret(authority: Authority, record: dict[str, Any], operation_id: str, owner_profile_id: str) -> bytes:
    value = open_record(authority, record, secret_context(operation_id, owner_profile_id))
    return bytes.fromhex(str(value.get("setup_secret") or ""))


__all__ = ["VaultError", "open_record", "open_secret", "seal", "seal_secret", "vault_context"]
