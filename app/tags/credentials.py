"""Connector credentials: ``Authorization: CremindTag <credential-id>.<secret>``.

- ``credential-id`` is ``tagc_`` + 26 lowercase base32 characters (16 random
  bytes) — a public identifier, the ``tag_credentials`` primary key;
- ``secret`` is 32 random bytes, base64url without padding (43 characters),
  shown once at creation. Only ``SHA-256(secret)`` (hex) is stored, and the
  check is :func:`hmac.compare_digest`. Never derived from the JWT secret, so
  a restore that keeps the local JWT secret has no bearing on it.

A credential is ``hardware`` (bound to one companion) or ``content`` (bound to
one companion AND one profile). The profile a request acts for is always the
credential's, never a request field.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from typing import Any

SCHEME = "CremindTag"
KIND_HARDWARE = "hardware"
KIND_CONTENT = "content"
KINDS = (KIND_HARDWARE, KIND_CONTENT)

_ID_RE = re.compile(r"^tagc_[a-z2-7]{26}$")
_SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

# Compared against when the id is unknown, so a miss costs the same as a hit.
_DUMMY_HASH = hashlib.sha256(b"cremind-tag-unknown-credential").hexdigest()


def new_credential_id() -> str:
    return "tagc_" + base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()


def new_secret() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def authorization_value(credential_id: str, secret: str) -> str:
    """The full ``Authorization`` header value a companion sends."""
    return f"{SCHEME} {credential_id}.{secret}"


def uses_scheme(header: str | None) -> bool:
    """Whether an ``Authorization`` value uses the CremindTag scheme."""
    if not header:
        return False
    return header.strip().split(" ", 1)[0].lower() == SCHEME.lower()


@dataclass(frozen=True)
class ParsedCredential:
    credential_id: str
    secret: str


def parse_authorization(header: str | None) -> ParsedCredential | None:
    """Parse ``CremindTag <id>.<secret>``; ``None`` for anything malformed."""
    if not uses_scheme(header):
        return None
    parts = header.strip().split(None, 1)
    if len(parts) != 2:
        return None
    token = parts[1].strip()
    cred_id, sep, secret = token.partition(".")
    if not sep or not _ID_RE.match(cred_id) or not _SECRET_RE.match(secret):
        return None
    return ParsedCredential(cred_id, secret)


def verify(row: dict[str, Any] | None, secret: str) -> bool:
    """Constant-time check of ``secret`` against a credential row (or none)."""
    expected = (row or {}).get("secret_sha256") or _DUMMY_HASH
    ok = hmac.compare_digest(hash_secret(secret), str(expected))
    return ok and row is not None
