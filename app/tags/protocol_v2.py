"""Protocol-v2 helpers the server needs itself (the contract's ``docs/connect-setup.md``):
setup codes (§2.2), device identity (§2.1) and canonical grants (§3.1).

A deliberate copy of the hardware runtime's reference (``app.tags.runtime.secure``):
setup sessions and ownership must work on a server without the runtime's
dependencies (the optional ``tags`` extra). ``tests/tags`` checks this module
against vectors taken from the pinned ``fixtures/v2_secure.json``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

ROLES = {"gateway": 1, "bridge": 2, "tag": 3}
ROLE_NAMES = {v: k for k, v in ROLES.items()}
GRANT_OPS = {"claim": 1, "recover": 2, "pair": 3, "rekey": 4, "release": 5, "maint": 6}
SECURE_PROTO_VERSION = 2

SETUP_SECRET_LEN = 10
SETUP_PAYLOAD_LEN = 15
SETUP_CODE_LEN = 25
SETUP_CODE_POLY = 0x25
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_DECODE = {c: i for i, c in enumerate(ALPHABET)}
_ALIASES = {"O": "0", "I": "1", "L": "1"}
QR_PREFIX = "CTAG:"

DEVICE_ID_LABEL = b"cremind-tag/v2/device-id"
GRANT_LABEL = b"cremind-tag/v2/grant"
GRANT_VERSION = 2


class SetupCodeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SetupPayload:
    role: str
    short_id: int
    secret: bytes

    @property
    def short_id_text(self) -> str:
        return f"{self.short_id:08X}"

    def __repr__(self) -> str:  # never print the secret
        return f"SetupPayload(role={self.role!r}, short_id={self.short_id_text})"


def _gf_mul(a: int, b: int) -> int:
    out = 0
    while b:
        if b & 1:
            out ^= a
        b >>= 1
        a <<= 1
        if a & 0x20:
            a ^= SETUP_CODE_POLY
    return out


def _check_value(symbols: str) -> int:
    check, weight = 0, 1
    for ch in symbols:
        weight = _gf_mul(weight, 2)
        check ^= _gf_mul(weight, _DECODE[ch])
    return check


def normalize_code(text: str) -> str:
    raw = (text or "").strip()
    if raw.upper().startswith(QR_PREFIX):
        raw = raw[len(QR_PREFIX):]
    return "".join(_ALIASES.get(ch, ch) for ch in raw.upper() if ch not in " -\t\r\n_")


def parse_setup_code(text: str, *, role: str | None = None) -> SetupPayload:
    """Parse a typed code or QR text; raises :class:`SetupCodeError`
    (``setup_code_invalid`` / ``setup_code_unsupported`` / ``setup_code_wrong_role``)."""
    if not isinstance(text, str):
        raise SetupCodeError("setup_code_invalid", "Enter the setup code printed on the label.")
    symbols = normalize_code(text)
    if len(symbols) != SETUP_CODE_LEN:
        raise SetupCodeError("setup_code_invalid",
                             f"A setup code has {SETUP_CODE_LEN} characters; this one has {len(symbols)}.")
    if any(ch not in _DECODE for ch in symbols):
        raise SetupCodeError("setup_code_invalid", "The setup code contains a character labels never use.")
    if ALPHABET[_check_value(symbols[:-1])] != symbols[-1]:
        raise SetupCodeError("setup_code_invalid", "The setup code does not check out; look for a typo.")
    value = 0
    for ch in symbols[:-1]:
        value = (value << 5) | _DECODE[ch]
    payload = value.to_bytes(SETUP_PAYLOAD_LEN, "big")
    if payload[0] & 0xF0 != 0x20:
        raise SetupCodeError("setup_code_unsupported", "This label is not for this kind of setup.")
    found = ROLE_NAMES.get(payload[0] & 0x0F)
    if found not in ("bridge", "tag"):
        raise SetupCodeError("setup_code_invalid", "The label names an unknown device.")
    if role is not None and found != role:
        raise SetupCodeError("setup_code_wrong_role", f"This is a {found} label, not a {role} label.")
    return SetupPayload(found, int.from_bytes(payload[1:5], "little"), bytes(payload[5:]))


def device_id(role: str, identity_pub: bytes) -> bytes:
    return hashlib.sha256(DEVICE_ID_LABEL + bytes([ROLES[role]]) + bytes(identity_pub)).digest()[:16]


def short_id(dev_id: bytes) -> int:
    value = int.from_bytes(bytes(dev_id)[:4], "little")
    if value in (0, 0xFFFFFFFF):
        value ^= 0x5A5A5A5A
    return value


def hw_id(role: str, dev_id: bytes) -> str:
    """The ``tag_devices.hw_id`` of a v2 device (the tag's is its tag id)."""
    if role == "tag":
        return f"{short_id(dev_id):08X}"
    return ("gw-" if role == "gateway" else "br-") + bytes(dev_id).hex()


# ── canonical CBOR (unsigned ints and byte strings in an int-keyed map) ──


def _head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 0x100:
        return bytes([(major << 5) | 24, value])
    if value < 0x10000:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, "big")
    if value < 0x100000000:
        return bytes([(major << 5) | 26]) + value.to_bytes(4, "big")
    return bytes([(major << 5) | 27]) + value.to_bytes(8, "big")


def _item(value: int | bytes) -> bytes:
    if isinstance(value, bool):
        raise TypeError("booleans are not grant fields")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("grant integers are unsigned")
        return _head(0, value)
    if isinstance(value, (bytes, bytearray)):
        return _head(2, len(value)) + bytes(value)
    raise TypeError(f"unsupported grant field {type(value).__name__}")


def encode_grant(*, op: str, device: bytes, role: str, authority_pub: bytes, owner: bytes,
                 controller: bytes, gen_from: int, challenge: bytes) -> bytes:
    """The canonical CBOR grant (keys 0..9 ascending, shortest forms)."""
    for name, value, size in (("device", device, 16), ("authority_pub", authority_pub, 32),
                              ("owner", owner, 16), ("controller", controller, 32), ("challenge", challenge, 16)):
        if len(value) != size:
            raise ValueError(f"{name} must be {size} bytes")
    if not 0 <= gen_from < 0xFFFFFFFF:
        raise ValueError("gen_from must leave room for gen_from + 1 in a u32")
    fields: list[int | bytes] = [GRANT_VERSION, GRANT_OPS[op], bytes(device), ROLES[role], bytes(authority_pub),
                                 bytes(owner), bytes(controller), gen_from, gen_from + 1, bytes(challenge)]
    return _head(5, len(fields)) + b"".join(_head(0, key) + _item(value) for key, value in enumerate(fields))


def owner_bytes(profile_uuid: str) -> bytes:
    """A profile UUID as the 16 owner bytes of a grant (any 36-char UUID
    text; other ids are hashed, so a test fixture's non-UUID id still maps)."""
    import uuid

    try:
        return uuid.UUID(str(profile_uuid)).bytes
    except ValueError:
        return hashlib.sha256(b"cremind-tag/v2/owner" + str(profile_uuid).encode("utf-8")).digest()[:16]


__all__ = [
    "GRANT_LABEL", "GRANT_OPS", "ROLES", "ROLE_NAMES", "SECURE_PROTO_VERSION", "SetupCodeError", "SetupPayload",
    "device_id", "encode_grant", "hw_id", "normalize_code", "owner_bytes", "parse_setup_code", "short_id",
]
