"""Walking a documents root: every file and folder the index may cover, once.

Built on ``os.scandir`` with an explicit stack instead of ``os.walk`` because
three things here are decided per entry, from the entry's own ``stat``:

- **Links are never followed into directories.** A symlink or an NTFS
  junction to a directory is how a walk escapes its root (``~/Documents/x ->
  /``) or loops forever. A symlinked *file* is kept only when its target sits
  inside the root and is not itself somewhere the matcher refuses (a link
  ``notes.txt -> .ssh/id_rsa`` must not smuggle a key past layer 1).
- **Cloud placeholders are listed, never read** (see
  :func:`app.documents.discovery.hashing.is_placeholder`). They come back
  ``placeholder=True`` and ``metadata_only``.
- **Pruning happens before descending**, so an excluded ``node_modules`` with
  200k files costs one ``stat``, not 200k.

The stack makes depth irrelevant (no recursion limit), entries within a folder
are visited in name order (deterministic output, stable progress), and the walk
checks its ``stop`` flag between entries, so a shutdown never waits for a big
tree. Directories are yielded as well as files (``is_dir=True``) so folder rows
can be maintained; a bundle (``Tool.app``) is yielded once, as a file-like
``metadata_only`` entry, and never entered.

Stat fields, per platform:

- ``ino``/``dev``: ``DirEntry.stat()`` reports zero for both on Windows, so a
  file there gets one extra ``os.stat`` (not for placeholders — opening one can
  start a recall). Values are folded into 63 bits: ReFS file ids are 128-bit
  and a Linux inode may exceed SQLite's signed 64-bit INTEGER.
- ``birthtime``: ``st_birthtime`` on macOS and on Windows (Python 3.12+; on
  older Pythons ``st_ctime`` *is* the creation time on Windows). ``None`` on
  Linux, which does not expose it through ``stat``.
"""

from __future__ import annotations

import hashlib
import os
import threading
import unicodedata
from typing import Callable, Iterator

from app.documents.discovery.hashing import fs_path, is_placeholder
from app.documents.discovery.ignore import CASE_INSENSITIVE, METADATA_ONLY, SKIP, IgnoreMatcher, norm_rel
from app.documents.types import FsEntry
from app.utils.logger import logger

ErrorCallback = Callable[[str, BaseException], None]

_IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
_I63 = (1 << 63) - 1


def path_hash(rel_path: str, *, case_insensitive: bool | None = None) -> str:
    """The manifest key of a relative path: blake2b-128 of its canonical spelling.

    Canonical = POSIX separators, NFC, and lowercased where the filesystem is
    case-insensitive (Windows, macOS), so ``Report.PDF`` renamed to
    ``report.pdf`` there is the same file. On a case-sensitive volume mounted
    on those platforms two files differing only in case would collide; that is
    the accepted price. ``case_insensitive`` overrides the platform default
    (tests, and a caller that knows better).
    """
    key = norm_rel(rel_path)
    ci = CASE_INSENSITIVE if case_insensitive is None else case_insensitive
    if ci:
        key = key.lower()
    # surrogatepass: a name that is not valid Unicode still hashes (stably).
    return hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=16).hexdigest()


def fit_i63(n: int | None) -> int:
    """An inode/device number that fits a signed 64-bit SQLite INTEGER.

    Values already in range pass through unchanged; larger ones are XOR-folded,
    deterministically, so the same file always gets the same number.
    """
    n = int(n or 0)
    if 0 <= n <= _I63:
        return n
    n &= (1 << 128) - 1
    return (n ^ (n >> 63) ^ (n >> 126)) & _I63


def _norm_real(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def _inside(child: str, parent: str) -> bool:
    if child == parent:
        return True
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _is_junction(entry: os.DirEntry) -> bool:
    fn = getattr(entry, "is_junction", None)  # Python 3.12+
    if fn is not None:
        return bool(fn())
    if os.name != "nt":
        return False
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    return getattr(st, "st_reparse_tag", 0) == _IO_REPARSE_TAG_MOUNT_POINT


def _birthtime(st: os.stat_result) -> float | None:
    bt = getattr(st, "st_birthtime", None)
    if bt is None and os.name == "nt":
        bt = st.st_ctime  # creation time on Windows before 3.12
    return float(bt) if bt is not None else None


def _report(on_error: ErrorCallback | None, rel: str, exc: BaseException) -> None:
    logger.debug(f"[documents] walk: skipped {rel or '<root>'}: {exc}")
    if on_error is None:
        return
    try:
        on_error(rel, exc)
    except Exception as cb_exc:  # noqa: BLE001 — a reporting hook must not end the walk
        logger.debug(f"[documents] walk: on_error callback failed: {cb_exc}")


def _file_entry(rel: str, abs_path: str, name: str, st: os.stat_result, disposition: str) -> FsEntry:
    placeholder = is_placeholder(name, st)
    full = st
    if os.name == "nt" and not placeholder:
        try:
            full = os.stat(fs_path(abs_path), follow_symlinks=False)
        except OSError:
            full = st
    return FsEntry(
        rel_path=rel,
        abs_path=abs_path,
        is_dir=False,
        size=int(full.st_size),
        mtime_ns=int(full.st_mtime_ns),
        ino=fit_i63(full.st_ino),
        dev=fit_i63(full.st_dev),
        birthtime=_birthtime(full),
        placeholder=placeholder,
        disposition=METADATA_ONLY if placeholder else disposition,
    )


def _visit_link(
    entry: os.DirEntry, rel: str, abs_path: str, name: str,
    matcher: IgnoreMatcher, root_real: str,
) -> FsEntry | None:
    """A symlink: kept only as a file whose target is inside the root and
    allowed there. Never descended, whatever it points at."""
    try:
        if entry.is_dir():  # follows the link
            return None
        if not entry.is_file():
            return None  # dangling, or a link to a device/FIFO
    except OSError:
        return None
    real = os.path.realpath(abs_path)
    real_n = os.path.normcase(real)
    if not _inside(real_n, root_real):
        return None
    disposition = matcher.classify(rel, abs_path)
    if disposition == SKIP:
        return None
    target_rel = os.path.relpath(real_n, root_real).replace(os.sep, "/")
    target_disp = matcher.classify(target_rel, real)
    if target_disp == SKIP:
        return None
    if target_disp == METADATA_ONLY:
        disposition = METADATA_ONLY
    # The content a reader gets is the target's, so is its size and mtime.
    st = os.stat(fs_path(abs_path))
    return _file_entry(rel, abs_path, name, st, disposition)


def walk(
    root: str,
    matcher: IgnoreMatcher,
    *,
    stop: threading.Event | None = None,
    max_entries: int = 500_000,
    on_error: ErrorCallback | None = None,
) -> Iterator[FsEntry]:
    """Yield every non-skipped file and directory under ``root`` (not ``root`` itself).

    Stops after ``max_entries`` yields or when ``stop`` is set; the caller
    cannot tell a finished walk from a cut-short one by the output alone, which
    is why :func:`app.documents.discovery.scan.scan_diff` asks for one more than
    its cap. A directory that cannot be listed (permission denied, vanished
    mid-walk) is skipped and reported through ``on_error(rel_path, exc)``; its
    files are then *unknown*, not deleted. Entries whose name is not valid
    Unicode (possible on Linux and NTFS) are reported the same way and skipped:
    they cannot be stored, searched or cited as text.

    ``abs_path`` values are plain paths; open them through
    :func:`app.documents.discovery.hashing.fs_path` so deep Windows paths work.
    """
    root_abs = os.path.abspath(root)
    root_real = _norm_real(root_abs)
    yielded = 0
    stack: list[tuple[str, str]] = [(root_abs, "")]
    while stack:
        if stop is not None and stop.is_set():
            return
        abs_dir, rel_dir = stack.pop()
        try:
            with os.scandir(fs_path(abs_dir)) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            _report(on_error, rel_dir, exc)
            continue
        subdirs: list[tuple[str, str]] = []
        for entry in entries:
            if stop is not None and stop.is_set():
                return
            name = entry.name
            try:
                name.encode("utf-8")
            except UnicodeEncodeError as exc:
                _report(on_error, f"{rel_dir}/{name!r}" if rel_dir else repr(name), exc)
                continue
            nfc = unicodedata.normalize("NFC", name)
            rel = f"{rel_dir}/{nfc}" if rel_dir else nfc
            abs_path = os.path.join(abs_dir, name)
            descend = False
            try:
                if entry.is_symlink():
                    fs_entry = _visit_link(entry, rel, abs_path, name, matcher, root_real)
                elif _is_junction(entry):
                    fs_entry = None
                elif entry.is_dir(follow_symlinks=False):
                    disposition = matcher.classify(rel, abs_path, is_dir=True)
                    if disposition == SKIP:
                        continue
                    st = entry.stat(follow_symlinks=False)
                    if matcher.is_bundle(name):
                        # One entry for the whole bundle, file-like, never entered.
                        fs_entry = FsEntry(
                            rel_path=rel, abs_path=abs_path, is_dir=False, size=0,
                            mtime_ns=int(st.st_mtime_ns), ino=fit_i63(st.st_ino),
                            dev=fit_i63(st.st_dev), birthtime=_birthtime(st),
                            disposition=METADATA_ONLY,
                        )
                    else:
                        fs_entry = FsEntry(
                            rel_path=rel, abs_path=abs_path, is_dir=True,
                            mtime_ns=int(st.st_mtime_ns), birthtime=_birthtime(st),
                            disposition=disposition,
                        )
                        descend = True
                elif entry.is_file(follow_symlinks=False):
                    disposition = matcher.classify(rel, abs_path)
                    if disposition == SKIP:
                        continue
                    fs_entry = _file_entry(rel, abs_path, name, entry.stat(follow_symlinks=False), disposition)
                else:
                    fs_entry = None  # socket, FIFO, device: not a document
            except OSError as exc:
                _report(on_error, rel, exc)
                continue
            if fs_entry is None:
                continue
            yield fs_entry
            yielded += 1
            if yielded >= max_entries:
                return
            if descend:
                subdirs.append((abs_path, rel))
        stack.extend(reversed(subdirs))


__all__ = ["ErrorCallback", "fit_i63", "fs_path", "path_hash", "walk"]
