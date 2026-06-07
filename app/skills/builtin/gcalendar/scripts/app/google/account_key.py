"""Routing-key derivation — CROSS-REPO CONTRACT with cremind-connect.

routing_key(provider, email) = base32(sha256(provider + ":" + normalize(email))[:16])

This MUST stay byte-identical to the TypeScript implementation in cremind-connect
(src/routing/account-key.ts) and the shared golden vectors, or event nudges route
to the wrong Durable Object hub and silently never arrive.
"""
from __future__ import annotations

import hashlib

# RFC 4648 base32 alphabet, lowercased, no padding.
_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567"


def base32_encode(data: bytes) -> str:
    bits = 0
    value = 0
    out: list[str] = []
    for byte in data:
        value = (value << 8) | byte
        bits += 8
        while bits >= 5:
            out.append(_ALPHABET[(value >> (bits - 5)) & 31])
            bits -= 5
    if bits > 0:
        out.append(_ALPHABET[(value << (5 - bits)) & 31])
    return "".join(out)


def normalize_email(email: str) -> str:
    """Lowercase + trim only. Do NOT collapse Gmail dots/+suffix (Workspace-safe)."""
    return email.strip().lower()


def account_key_for(provider: str, email: str) -> str:
    normalized = normalize_email(email)
    digest = hashlib.sha256(f"{provider}:{normalized}".encode("utf-8")).digest()
    return base32_encode(digest[:16])
