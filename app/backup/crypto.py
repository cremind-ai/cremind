"""Optional passphrase encryption for backup archives.

A backup contains every secret Cremind holds (LLM API keys, channel bot tokens,
OAuth refresh tokens, DB passwords) in the clear, so archives can optionally be
encrypted with a user passphrase.

Envelope format (bytes):

    magic  = b"CREMINDBK"            # 9 bytes
    version= b"\\x01"                 # 1 byte
    hlen   = uint32 big-endian       # length of the header JSON
    header = { kdf, n, r, p, salt(b64), cipher, chunk_size,
               nonce_prefix(b64), manifest }   # UTF-8 JSON, plaintext
    frames = ( uint32 len + ciphertext )* , terminated by a zero-length
             plaintext frame (a bare 16-byte GCM tag) as the truncation sentinel

The header is plaintext (and embeds a copy of the manifest, which holds no
secrets) so ``/api/backup/list`` and pre-restore validation can read metadata
without the passphrase. Payload confidentiality/integrity is AES-256-GCM in
≤1 MiB chunks; each chunk's nonce is ``nonce_prefix (4B) || counter (8B BE)`` and
its counter is also the AAD, so reordering/truncation is detected.

Key derivation is stdlib ``hashlib.scrypt``; the cipher needs ``cryptography``
(already an install dependency), imported lazily with a clear error.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
from typing import Any, BinaryIO

from app.backup.manifest import BackupPassphraseError

_MAGIC = b"CREMINDBK"
_VERSION = b"\x01"
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_LEN = 16
_NONCE_PREFIX_LEN = 4
_CHUNK_SIZE = 1 << 20  # 1 MiB
_GZIP_MAGIC = b"\x1f\x8b"


def _import_aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover
        raise BackupPassphraseError(
            "Encrypted backups require the 'cryptography' package, which is not "
            "installed in this environment."
        ) from e
    return AESGCM


def _derive_key(passphrase: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_KEY_LEN
    )


def _b64(b: bytes) -> str:
    import base64

    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    import base64

    return base64.b64decode(s.encode("ascii"))


def is_encrypted(source: "str | os.PathLike[str] | BinaryIO") -> bool:
    """True if ``source`` (a path or a seekable binary stream) is an encrypted
    envelope rather than a bare gzip tar."""
    if hasattr(source, "read"):
        pos = source.tell()
        head = source.read(len(_MAGIC))
        source.seek(pos)
        return head == _MAGIC
    with open(source, "rb") as f:
        head = f.read(len(_MAGIC))
    return head == _MAGIC


def read_envelope_header(fileobj: BinaryIO) -> dict[str, Any]:
    """Read magic + version + header JSON, leaving ``fileobj`` at the first frame."""
    magic = fileobj.read(len(_MAGIC))
    if magic != _MAGIC:
        raise BackupPassphraseError("Not a Cremind encrypted backup (bad magic).")
    version = fileobj.read(1)
    if version != _VERSION:
        raise BackupPassphraseError(f"Unsupported encryption version {version!r}.")
    (hlen,) = struct.unpack(">I", fileobj.read(4))
    header = json.loads(fileobj.read(hlen).decode("utf-8"))
    return header


def write_envelope_header(fileobj: BinaryIO, header: dict[str, Any]) -> None:
    payload = json.dumps(header).encode("utf-8")
    fileobj.write(_MAGIC)
    fileobj.write(_VERSION)
    fileobj.write(struct.pack(">I", len(payload)))
    fileobj.write(payload)


def new_header(manifest_dict: dict[str, Any]) -> tuple[dict[str, Any], bytes, bytes]:
    """Build a fresh envelope header + return (header, salt, nonce_prefix)."""
    salt = os.urandom(_SALT_LEN)
    nonce_prefix = os.urandom(_NONCE_PREFIX_LEN)
    header = {
        "kdf": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": _b64(salt),
        "cipher": "aes-256-gcm",
        "chunk_size": _CHUNK_SIZE,
        "nonce_prefix": _b64(nonce_prefix),
        "manifest": manifest_dict,
    }
    return header, salt, nonce_prefix


def derive_key_from_header(passphrase: str, header: dict[str, Any]) -> tuple[bytes, bytes]:
    """Return (key, nonce_prefix) for an existing header."""
    salt = _unb64(header["salt"])
    nonce_prefix = _unb64(header["nonce_prefix"])
    key = _derive_key(
        passphrase, salt, n=int(header["n"]), r=int(header["r"]), p=int(header["p"])
    )
    return key, nonce_prefix


def _nonce(prefix: bytes, counter: int) -> bytes:
    return prefix + struct.pack(">Q", counter)


class EncryptingWriter(io.RawIOBase):
    """Binary write-only stream: buffers plaintext into ≤chunk frames, encrypts.

    Wrap a destination file with this, then hand it to ``tarfile.open(mode="w|gz",
    fileobj=...)``. After ``tf.close()``, call :meth:`finalize` to emit the
    truncation sentinel. Does not close the underlying file (the caller owns it).
    """

    def __init__(self, dest: BinaryIO, key: bytes, nonce_prefix: bytes, chunk_size: int = _CHUNK_SIZE):
        self._dest = dest
        self._aead = _import_aesgcm()(key)
        self._nonce_prefix = nonce_prefix
        self._chunk_size = chunk_size
        self._buf = bytearray()
        self._counter = 0
        self._finalized = False

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        data = bytes(b)
        self._buf.extend(data)
        while len(self._buf) >= self._chunk_size:
            self._emit(bytes(self._buf[: self._chunk_size]))
            del self._buf[: self._chunk_size]
        return len(data)

    def _emit(self, plaintext: bytes) -> None:
        nonce = _nonce(self._nonce_prefix, self._counter)
        aad = struct.pack(">Q", self._counter)
        ct = self._aead.encrypt(nonce, plaintext, aad)
        self._dest.write(struct.pack(">I", len(ct)))
        self._dest.write(ct)
        self._counter += 1

    def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        if self._buf:
            self._emit(bytes(self._buf))
            self._buf.clear()
        # Zero-length sentinel frame proves the stream wasn't truncated.
        self._emit(b"")

    def close(self) -> None:
        try:
            self.finalize()
        finally:
            super().close()


class DecryptingReader(io.RawIOBase):
    """Binary read-only stream over an encrypted payload.

    Position ``src`` just past the envelope header (via :func:`read_envelope_header`)
    before constructing. Hand to ``tarfile.open(mode="r|gz", fileobj=...)``.
    """

    def __init__(self, src: BinaryIO, key: bytes, header: dict[str, Any], nonce_prefix: bytes):
        self._src = src
        self._aead = _import_aesgcm()(key)
        self._nonce_prefix = nonce_prefix
        self._counter = 0
        self._buf = bytearray()
        self._eof = False

    def readable(self) -> bool:
        return True

    def _fill(self) -> bool:
        """Decrypt one frame into the buffer. Returns False at the sentinel."""
        lenbytes = self._src.read(4)
        if len(lenbytes) < 4:
            raise BackupPassphraseError("Encrypted backup is truncated (missing frame length).")
        (clen,) = struct.unpack(">I", lenbytes)
        ct = self._src.read(clen)
        if len(ct) < clen:
            raise BackupPassphraseError("Encrypted backup is truncated (short frame).")
        nonce = _nonce(self._nonce_prefix, self._counter)
        aad = struct.pack(">Q", self._counter)
        try:
            pt = self._aead.decrypt(nonce, ct, aad)
        except Exception as e:  # noqa: BLE001 — InvalidTag et al.
            raise BackupPassphraseError(
                "Could not decrypt backup — wrong passphrase or corrupt archive."
            ) from e
        self._counter += 1
        if not pt:
            self._eof = True
            return False
        self._buf.extend(pt)
        return True

    def readinto(self, b) -> int:
        if not self._buf and not self._eof:
            while not self._buf and self._fill():
                pass
        if not self._buf:
            return 0
        n = min(len(b), len(self._buf))
        b[:n] = self._buf[:n]
        del self._buf[:n]
        return n


def verify_passphrase(path: "str | os.PathLike[str]", passphrase: str) -> bool:
    """Decrypt only the first frame to check the passphrase. Non-mutating.

    A plain (unencrypted) archive needs no passphrase, so it always verifies.
    """
    if not is_encrypted(path):
        return True
    with open(path, "rb") as f:
        header = read_envelope_header(f)
        key, nonce_prefix = derive_key_from_header(passphrase, header)
        reader = DecryptingReader(f, key, header, nonce_prefix)
        try:
            reader._fill()
        except BackupPassphraseError:
            return False
    return True


__all__ = [
    "DecryptingReader",
    "EncryptingWriter",
    "derive_key_from_header",
    "is_encrypted",
    "new_header",
    "read_envelope_header",
    "verify_passphrase",
    "write_envelope_header",
]
