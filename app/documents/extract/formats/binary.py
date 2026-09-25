"""Executables and installers: never run, never unpacked, but described.

The file card of a program or installer is only useful with what it is for:
"the arm64 Mac build", "the 32-bit Windows installer". So the container
format (elf/pe/macho/dmg/msi/rpm/deb/pkg) and the CPU architecture are read
from the header, which is all this module ever touches.
"""

from __future__ import annotations

import os
import struct
from typing import Any

from ._base import Ctx

_ELF_MACHINES = {
    0x02: "sparc", 0x03: "x86", 0x08: "mips", 0x14: "ppc", 0x15: "ppc64", 0x16: "s390",
    0x28: "arm", 0x2B: "sparc64", 0x32: "ia64", 0x3E: "x86_64", 0xB7: "arm64",
    0xF3: "riscv", 0x102: "loongarch",
}
_PE_MACHINES = {
    0x014C: "x86", 0x8664: "x86_64", 0x01C0: "arm", 0x01C4: "arm", 0xAA64: "arm64",
    0xA641: "arm64ec", 0x0200: "ia64", 0x5064: "riscv64",
}
_MACHO_CPUS = {
    7: "x86", 0x01000007: "x86_64", 12: "arm", 0x0100000C: "arm64",
    0x0200000C: "arm64_32", 18: "ppc", 0x01000012: "ppc64",
}
_EXT_FORMATS = {
    ".msi": "msi", ".msp": "msi", ".dmg": "dmg", ".pkg": "pkg", ".mpkg": "pkg",
    ".deb": "deb", ".rpm": "rpm", ".appimage": "elf", ".app": "macho",
}


def _tail(ctx: Ctx, size: int) -> bytes:
    if ctx.req.data is not None:
        return ctx.req.data[-size:]
    with ctx.open() as fh:
        fh.seek(0, os.SEEK_END)
        length = fh.tell()
        fh.seek(max(0, length - size))
        return fh.read(size)


def executable_info(head: bytes, tail: bytes, ext: str, read_at: Any = None) -> dict[str, Any]:
    """``{format, arch, ...}`` from an executable's header bytes.

    ``read_at(offset, size)`` fetches bytes past ``head`` when a PE header
    sits further in than the head reaches.
    """
    info: dict[str, Any] = {}
    if head[:4] == b"\x7fELF" and len(head) >= 20:
        big = head[5] == 2
        machine = struct.unpack_from(">H" if big else "<H", head, 18)[0]
        e_type = struct.unpack_from(">H" if big else "<H", head, 16)[0]
        info = {"format": "elf", "bits": 64 if head[4] == 2 else 32,
                "type": {1: "object", 2: "executable", 3: "shared", 4: "core"}.get(e_type, "other")}
        arch = _ELF_MACHINES.get(machine)
        if arch == "riscv":
            arch = "riscv64" if head[4] == 2 else "riscv32"
        info["arch"] = arch
    elif head[:2] == b"MZ" and len(head) >= 0x40:
        lfanew = struct.unpack_from("<I", head, 0x3C)[0]
        pe = head[lfanew:lfanew + 26] if lfanew + 26 <= len(head) else (
            read_at(lfanew, 26) if read_at and lfanew < 64 * 1024 * 1024 else b"")
        info = {"format": "pe"}
        if len(pe) >= 24 and pe[:4] == b"PE\x00\x00":
            machine, characteristics = struct.unpack_from("<H", pe, 4)[0], struct.unpack_from("<H", pe, 22)[0]
            info["arch"] = _PE_MACHINES.get(machine)
            info["dll"] = bool(characteristics & 0x2000)
        else:
            info["arch"] = None  # a DOS-era MZ program
    elif head[:4] in (b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"):
        big = head[:2] == b"\xfe\xed"
        cpu = struct.unpack_from(">I" if big else "<I", head, 4)[0]
        info = {"format": "macho", "arch": _MACHO_CPUS.get(cpu),
                "bits": 64 if head[:4] in (b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe") else 32}
    elif head[:4] == b"\xca\xfe\xba\xbe" and len(head) >= 8:
        count = struct.unpack_from(">I", head, 4)[0]
        archs = []
        for k in range(min(count, 20)):
            off = 8 + 20 * k
            if off + 4 > len(head):
                break
            archs.append(_MACHO_CPUS.get(struct.unpack_from(">I", head, off)[0], "other"))
        info = {"format": "macho", "arch": "universal", "archs": archs}
    elif len(tail) >= 512 and tail[-512:-508] == b"koly":
        info = {"format": "dmg"}
    elif head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        info = {"format": "msi"}
    elif head[:4] == b"\xed\xab\xee\xdb":
        info = {"format": "rpm"}  # the lead's archnum is 1 for both i386 and x86_64: useless
    elif head[:8] == b"!<arch>\n":
        info = {"format": "deb" if b"debian-binary" in head[:128] else "ar"}
    elif head[:4] == b"xar!":
        info = {"format": "pkg"}
    else:
        info = {"format": _EXT_FORMATS.get(ext, ext.lstrip(".") or "unknown")}
    return {key: value for key, value in info.items() if value is not None}


def extract_executable(ctx: Ctx) -> None:
    head = ctx.head(4096)

    def read_at(offset: int, size: int) -> bytes:
        return ctx.read_bytes(offset + size)[0][offset:offset + size]

    ctx.result.doc_meta.update(executable_info(head, _tail(ctx, 512), ctx.ext, read_at))
    ctx.metadata_only("executable")
