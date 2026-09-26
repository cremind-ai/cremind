"""Archives: a listing of member names, never their contents.

Unpacking user archives is how zip bombs and path-traversal members become
problems, and indexing a backup's contents twice (once in the archive, once
where it was unpacked) is noise. So only the directory is read: the ZIP
central directory, or tar headers one at a time (a compressed tar has to be
decompressed to reach them, which the worker's timeout bounds). The listing is
capped at ``max_archive_names`` and the file is ``metadata_only(archive)``.
"""

from __future__ import annotations

import lzma
import struct
import tarfile
import zipfile
import zlib
from typing import Any

from ._base import Ctx

_TAR_ERRORS = (tarfile.TarError, OSError, EOFError, zlib.error, lzma.LZMAError, ValueError)


def _gzip_member_name(head: bytes) -> str | None:
    """The original file name a gzip header records (FNAME), if any."""
    if len(head) < 10 or head[:2] != b"\x1f\x8b":
        return None
    flags = head[3]
    pos = 10
    if flags & 0x04:  # FEXTRA
        if pos + 2 > len(head):
            return None
        (xlen,) = struct.unpack_from("<H", head, pos)
        pos += 2 + xlen
    if not flags & 0x08:  # FNAME
        return None
    end = head.find(b"\x00", pos)
    if end < 0:
        return None
    return head[pos:end].decode("latin-1") or None


def _list_zip(ctx: Ctx, cap: int) -> tuple[list[str], dict[str, Any]]:
    with zipfile.ZipFile(ctx.source()) as zf:
        infos = zf.infolist()
    names = [info.filename for info in infos[:cap]]
    meta = {"format": "zip", "members": len(infos),
            "uncompressed_size": sum(info.file_size for info in infos),
            "listing_truncated": len(infos) > len(names)}
    return names, meta


def _list_tar(ctx: Ctx, cap: int) -> tuple[list[str], dict[str, Any]] | None:
    names: list[str] = []
    more = False
    try:
        with ctx.open() as fh, tarfile.open(fileobj=fh, mode="r:*") as tar:
            while True:
                member = tar.next()
                if member is None:
                    break
                if len(names) >= cap:
                    more = True
                    break
                names.append(member.name + ("/" if member.isdir() else ""))
    except _TAR_ERRORS:
        if not names:
            return None
    meta: dict[str, Any] = {"format": "tar", "listing_truncated": more}
    if not more:
        meta["members"] = len(names)
    return names, meta


_FORMAT_BY_MIME = {
    "application/gzip": "gzip", "application/x-7z-compressed": "7z", "application/vnd.rar": "rar",
    "application/x-xz": "xz", "application/x-bzip2": "bzip2", "application/zstd": "zstd",
    "application/x-lz4": "lz4", "application/vnd.ms-cab-compressed": "cab",
    "application/x-lzip": "lzip", "application/x-tar": "tar", "application/x-archive": "ar",
    "application/x-xar": "xar", "application/java-archive": "jar",
    "application/vnd.android.package-archive": "apk", "application/zip": "zip",
}


def extract_archive(ctx: Ctx) -> None:
    from ..detect import sniff

    cap = max(0, ctx.limit("max_archive_names"))
    head = ctx.head(8192)
    _kind, mime = sniff(head, b"", ctx.ext)
    ctx.metadata_only("archive")
    listing: tuple[list[str], dict[str, Any]] | None = None
    if head[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        try:
            listing = _list_zip(ctx, cap)
        except (zipfile.BadZipFile, OSError, ValueError, EOFError):
            listing = None
        if listing and mime in _FORMAT_BY_MIME:
            listing[1]["format"] = _FORMAT_BY_MIME[mime]
    elif mime in ("application/gzip", "application/x-bzip2", "application/x-xz", "application/x-tar"):
        listing = _list_tar(ctx, cap)
        if listing is None and mime == "application/gzip":
            name = _gzip_member_name(head)
            listing = ([name] if name else [], {"format": "gzip", "members": 1})
        elif listing is not None and mime != "application/x-tar":
            listing[1]["compression"] = _FORMAT_BY_MIME[mime]
    if listing is None:
        listing = ([], {"format": _FORMAT_BY_MIME.get(mime or "", (ctx.ext or ".").lstrip(".") or "unknown")})
    names, meta = listing
    ctx.result.listing = names
    ctx.result.doc_meta.update(meta)
