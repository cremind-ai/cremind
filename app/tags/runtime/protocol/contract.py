"""The pinned protocol contract (``pinned/``): what this runtime was built against.

The wire protocol belongs to the cremind-tag firmware repository. It publishes
an immutable contract artifact (its ``tools/contract.py``): ``contract.json``
(schema ``cremind-tag/contract@1``) with the contract version, the protocol
capabilities, the source revision, the SHA-256 of every file and one
``digest`` over them, plus ``spec.yaml``, the golden ``fixtures/`` and the
normative ``docs/``. Cremind keeps one such snapshot in ``pinned/`` (replace
it with ``scripts/tags/pin_contract.py``, never by hand) and generates
:mod:`.ids` and :mod:`.msgs` from its spec (``scripts/tags/codegen.py``).

Compatibility with devices follows the protocol capabilities recorded here,
not the application versions of Cremind or of the firmware.
"""

from __future__ import annotations

import functools
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "cremind-tag/contract@1"
PINNED_DIR = Path(__file__).resolve().parent / "pinned"
META_FILE = "contract.json"
_TEXT_SUFFIXES = (".yaml", ".json", ".md")


class ContractError(ValueError):
    """The pinned contract is missing, altered, or does not match the generated bindings."""


@dataclass(frozen=True)
class Contract:
    root: Path
    meta: dict[str, Any]

    @property
    def version(self) -> str:
        return str(self.meta["version"])

    @property
    def digest(self) -> str:
        return str(self.meta["digest"])

    @property
    def revision(self) -> str:
        return str((self.meta.get("source") or {}).get("revision") or "")

    @property
    def protocol(self) -> dict[str, int]:
        return dict(self.meta.get("protocol") or {})

    @property
    def spec_path(self) -> Path:
        return self.root / "spec.yaml"

    @property
    def fixtures_dir(self) -> Path:
        return self.root / "fixtures"

    def fixture(self, name: str) -> Path:
        return self.fixtures_dir / name

    def summary(self) -> dict[str, Any]:
        return {"version": self.version, "revision": self.revision, "digest": self.digest,
                "protocol": self.protocol}


def _file_sha256(path: Path) -> str:
    data = path.read_bytes()
    if path.suffix in _TEXT_SUFFIXES:
        data = data.replace(b"\r\n", b"\n")  # a Windows checkout may carry CRLF
    return hashlib.sha256(data).hexdigest()


def digest_of(files: dict[str, str]) -> str:
    """SHA-256 of the ``<sha256>  <path>`` listing, sorted by path (the artifact's ``digest``)."""
    listing = "".join(f"{files[path]}  {path}\n" for path in sorted(files))
    return hashlib.sha256(listing.encode("ascii")).hexdigest()


def load(root: Path = PINNED_DIR, *, verify: bool = True) -> Contract:
    """The contract in ``root``; with ``verify`` every file must match its recorded SHA-256."""
    try:
        meta = json.loads((root / META_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"{root / META_FILE}: {exc}") from None
    if meta.get("schema") != SCHEMA:
        raise ContractError(f"{root / META_FILE}: schema {meta.get('schema')!r}, expected {SCHEMA}")
    files = meta.get("files")
    if not isinstance(files, dict) or "spec.yaml" not in files:
        raise ContractError(f"{root / META_FILE} lists no spec.yaml")
    if verify:
        problems = [name for name, expected in sorted(files.items())
                    if not (root / name).is_file() or _file_sha256(root / name) != expected]
        if problems:
            raise ContractError(f"{root}: files do not match the contract: {', '.join(problems)}")
        if digest_of(files) != meta.get("digest"):
            raise ContractError(f"{root}: the digest does not match the file list")
    return Contract(root, meta)


@functools.cache
def pinned() -> Contract:
    """The contract this runtime was generated from (loaded once, not re-verified)."""
    return load(PINNED_DIR, verify=False)


def check_bindings(contract: Contract | None = None) -> None:
    """The generated :mod:`.ids` came from this contract's spec (raises :class:`ContractError`)."""
    from . import ids

    contract = contract or pinned()
    expected = contract.meta["files"]["spec.yaml"]
    if ids.SPEC_SHA256 != expected:
        raise ContractError(f"protocol/ids.py was generated from spec {ids.SPEC_SHA256[:12]}, the pinned contract "
                            f"holds {expected[:12]}; run scripts/tags/codegen.py")
    if ids.SPEC_VERSION != contract.protocol.get("spec_version"):
        raise ContractError(f"protocol/ids.py is spec version {ids.SPEC_VERSION}, the contract says "
                            f"{contract.protocol.get('spec_version')}")


__all__ = ["Contract", "ContractError", "PINNED_DIR", "SCHEMA", "check_bindings", "digest_of", "load", "pinned"]
