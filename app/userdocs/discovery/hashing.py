"""Reading a user's file safely: long paths, cloud placeholders, content hashes.

Three rules every reader in discovery follows, kept in one module so they
cannot drift apart:

- **Long Windows paths** go through the ``\\\\?\\`` prefix (:func:`fs_path`),
  or a deep OneDrive tree fails with "file not found" on a file that is there.
- **Cloud placeholders are never opened for reading.** OneDrive
  Files-On-Demand, iCloud "optimise storage" and Dropbox online-only files look
  like ordinary files, but reading one downloads it — for a user with a 1 TB
  cloud drive and a 256 GB disk, a full index would fill the disk.
  :func:`is_placeholder` recognises them from ``stat`` alone, without opening.
- **Hashing is streamed and interruptible.** A multi-gigabyte video is never
  held in memory, and a shutdown does not wait for it to finish.

``sha256_file`` is the content identity used by move matching and the
extractor cache; ``quick_hash`` stands in for it on metadata-only files over
64 MiB, where reading the whole file only to recognise a rename is not worth
the I/O.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
import threading

# Beyond this many characters a Windows path needs the ``\\?\`` prefix. 260 is
# MAX_PATH; 240 leaves headroom for the file name a caller appends.
_LONG_PATH_AT = 240

# Windows ``st_file_attributes`` bits that mean "the data is not on this disk".
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_WIN_PLACEHOLDER_BITS = (
    FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)
# macOS ``st_flags``: the file's content lives in a File Provider (iCloud
# Drive, Dropbox, OneDrive for Mac) and is fetched on first read.
SF_DATALESS = 0x40000000

QUICK_HASH_EDGE = 64 * 1024
QUICK_HASH_MIN_SIZE = 64 * 1024 * 1024


def _long_path(p: str) -> str:
    # The same rule as ``app.backup.rules.long_path``, repeated rather than
    # imported: ``app.backup``'s package ``__init__`` loads the whole backup
    # engine, and the extractor child process imports this module too.
    if p.startswith("\\\\?\\") or p.startswith("\\\\.\\"):
        return p
    ap = os.path.abspath(p)
    if ap.startswith("\\\\"):  # UNC share
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def fs_path(abs_path: str) -> str:
    """``abs_path`` in the form the OS will open: ``\\\\?\\``-prefixed when long on Windows."""
    if os.name == "nt" and len(abs_path) > _LONG_PATH_AT:
        return _long_path(abs_path)
    return abs_path


def is_icloud_stub(name: str) -> bool:
    """``.Report.pdf.icloud``: how iCloud Drive stood in for an evicted file
    before macOS 14 (and still does on some synced folders)."""
    return name.startswith(".") and name.lower().endswith(".icloud") and len(name) > len(".icloud") + 1


def is_placeholder(name: str, st: os.stat_result | None) -> bool:
    """Whether reading this file would download it. Decided from ``stat``
    (which never hydrates) and the name alone."""
    if is_icloud_stub(name):
        return True
    if st is None:
        return False
    attrs = getattr(st, "st_file_attributes", 0) or 0
    if attrs & _WIN_PLACEHOLDER_BITS:
        return True
    flags = getattr(st, "st_flags", 0) or 0
    return bool(flags & SF_DATALESS)


def _readable_regular_file(abs_path: str) -> bool:
    """A regular, non-placeholder file. Opening a FIFO blocks until a writer
    appears, and opening a placeholder starts a download."""
    try:
        st = os.stat(fs_path(abs_path))
    except OSError:
        return False
    if not stat_mod.S_ISREG(st.st_mode):
        return False
    return not is_placeholder(os.path.basename(abs_path), st)


def sha256_file(
    abs_path: str,
    *,
    stop: threading.Event | None = None,
    block: int = 1 << 20,
) -> str | None:
    """Streamed sha256 hex digest of a file's content.

    ``None`` when the file is unreadable, vanished, is not a regular file, is a
    cloud placeholder, or ``stop`` was set while hashing — the caller treats
    all of those as "identity unknown", never as a match.
    """
    if not _readable_regular_file(abs_path):
        return None
    digest = hashlib.sha256()
    try:
        with open(fs_path(abs_path), "rb") as fh:
            while True:
                if stop is not None and stop.is_set():
                    return None
                chunk = fh.read(block)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def read_text_head(abs_path: str, max_bytes: int) -> str | None:
    """Up to ``max_bytes`` of a small text file (a manifest, a git log), or
    ``None`` under the same conditions as :func:`sha256_file`."""
    if not _readable_regular_file(abs_path):
        return None
    try:
        with open(fs_path(abs_path), "rb") as fh:
            data = fh.read(max_bytes)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def read_text_tail(abs_path: str, max_bytes: int) -> str | None:
    """The last ``max_bytes`` of a file, decoded leniently (see :func:`read_text_head`)."""
    if not _readable_regular_file(abs_path):
        return None
    try:
        with open(fs_path(abs_path), "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read(max_bytes)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def quick_hash(abs_path: str, size: int) -> str | None:
    """sha256 over the size, the first 64 KiB and the last 64 KiB.

    Enough to recognise the *same* large file after a rename without reading
    gigabytes; not a content identity (two files sharing size, head and tail
    collide), so it is only ever compared with another quick hash. ``None``
    under the same conditions as :func:`sha256_file`.
    """
    if not _readable_regular_file(abs_path):
        return None
    digest = hashlib.sha256(str(int(size)).encode("ascii"))
    try:
        with open(fs_path(abs_path), "rb") as fh:
            digest.update(fh.read(QUICK_HASH_EDGE))
            if size > QUICK_HASH_EDGE:
                fh.seek(max(0, size - QUICK_HASH_EDGE))
                digest.update(fh.read(QUICK_HASH_EDGE))
    except OSError:
        return None
    return digest.hexdigest()


__all__ = [
    "QUICK_HASH_MIN_SIZE",
    "fs_path",
    "is_icloud_stub",
    "is_placeholder",
    "quick_hash",
    "read_text_head",
    "read_text_tail",
    "sha256_file",
]
