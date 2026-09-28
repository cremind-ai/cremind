#!/usr/bin/env python3
"""Pin a cremind-tag protocol contract artifact into Cremind.

The firmware repository publishes the contract as
``cremind-tag-contract-<version>.tar.gz`` (its ``tools/contract.py``). This
script verifies an artifact (every file against ``contract.json``, the
digest, the archive's ``.sha256`` when one sits next to it or ``--sha256`` is
given), replaces ``app/tags/runtime/protocol/pinned/`` with it and
regenerates the Python bindings (``scripts/tags/codegen.py``). Then run the
runtime's protocol tests (``tests/tags/runtime/protocol``): they compare the
Python reference implementation byte for byte with the new fixtures.

Usage::

    .venv/Scripts/python.exe scripts/tags/pin_contract.py cremind-tag-contract-0.2.0.tar.gz
    .venv/Scripts/python.exe scripts/tags/pin_contract.py path/to/unpacked/cremind-tag-contract-0.2.0
    .venv/Scripts/python.exe scripts/tags/pin_contract.py --check    # verify the pinned copy only
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.tags.runtime.protocol import contract  # noqa: E402


def _unpack(archive: Path, into: Path, expected_sha256: str | None) -> Path:
    data = archive.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    sidecar = archive.with_name(archive.name + ".sha256")
    if expected_sha256 is None and sidecar.is_file():
        expected_sha256 = sidecar.read_text(encoding="ascii").split()[0]
    if expected_sha256 is not None and actual != expected_sha256.lower():
        raise contract.ContractError(f"{archive.name}: SHA-256 {actual}, expected {expected_sha256}")
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not (member.isfile() or member.isdir()):
                raise contract.ContractError(f"{archive.name}: unsafe member {member.name!r}")
        tar.extractall(into, members=members, filter="data")
    roots = [p for p in into.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise contract.ContractError(f"{archive.name} must hold exactly one top-level directory")
    return roots[0]


def pin(source: Path, expected_sha256: str | None = None) -> contract.Contract:
    with tempfile.TemporaryDirectory(prefix="ctag-contract-") as tmp:
        directory = _unpack(source, Path(tmp), expected_sha256) if source.is_file() else source
        new = contract.load(directory, verify=True)
        target = contract.PINNED_DIR
        staging = target.with_name(".pinned.new")
        shutil.rmtree(staging, ignore_errors=True)
        shutil.copytree(directory, staging)
        contract.load(staging, verify=True)
        old = target.with_name(".pinned.old")
        shutil.rmtree(old, ignore_errors=True)
        if target.exists():
            target.rename(old)
        staging.rename(target)
        shutil.rmtree(old, ignore_errors=True)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "tags" / "codegen.py")], check=True)
    return new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("source", nargs="?", type=Path, help="a contract .tar.gz or an unpacked contract directory")
    parser.add_argument("--sha256", help="the archive's expected SHA-256 (default: its .sha256 file, if present)")
    parser.add_argument("--check", action="store_true", help="only verify the pinned contract and the bindings")
    args = parser.parse_args(argv)
    try:
        if args.check or args.source is None:
            pinned = contract.load(contract.PINNED_DIR, verify=True)
            contract.check_bindings(pinned)
            print(f"pinned: cremind-tag contract {pinned.version} ({pinned.revision[:12]}), digest {pinned.digest}")
            return 0
        new = pin(args.source, args.sha256)
        print(f"pinned cremind-tag contract {new.version} ({new.revision[:12]}), digest {new.digest}")
        print("next: .venv/Scripts/python.exe -m pytest tests/tags/runtime/protocol -q")
        return 0
    except (contract.ContractError, OSError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"pin_contract: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
