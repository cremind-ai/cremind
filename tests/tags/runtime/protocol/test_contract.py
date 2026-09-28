"""The pinned protocol contract: intact, and the source of the generated bindings.

The firmware repository owns the wire protocol and publishes it as a contract
artifact; Cremind pins one (app/tags/runtime/protocol/pinned/). These tests
fail when the snapshot is edited by hand, when the bindings were not
regenerated after a re-pin, or when the Python reference implementation
stops producing the contract's golden fixtures (test_generated.py).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.tags.runtime.protocol import contract, ids


def test_the_pinned_contract_verifies() -> None:
    pinned = contract.load(contract.PINNED_DIR, verify=True)
    assert pinned.meta["schema"] == contract.SCHEMA
    assert pinned.revision and len(pinned.revision) == 40
    assert pinned.protocol["spec_version"] == ids.SPEC_VERSION
    assert pinned.protocol["proto_version"] == ids.PROTO_VERSION
    assert pinned.protocol["secure_proto_version"] == ids.SECURE_PROTO_VERSION
    contract.check_bindings(pinned)


def test_every_file_is_listed_and_the_digest_covers_them() -> None:
    pinned = contract.pinned()
    on_disk = {p.relative_to(pinned.root).as_posix() for p in pinned.root.rglob("*") if p.is_file()}
    assert on_disk == set(pinned.meta["files"]) | {contract.META_FILE}
    assert contract.digest_of(pinned.meta["files"]) == pinned.digest
    assert (pinned.fixtures_dir / "v2_secure.json").is_file()


def test_an_edited_file_or_digest_is_refused(tmp_path: Path) -> None:
    copy = tmp_path / "contract"
    shutil.copytree(contract.PINNED_DIR, copy)
    render = copy / "fixtures" / "render.json"
    render.write_bytes(render.read_bytes() + b" ")
    with pytest.raises(contract.ContractError, match="fixtures/render.json"):
        contract.load(copy)
    shutil.copy2(contract.PINNED_DIR / "fixtures" / "render.json", render)
    meta = json.loads((copy / contract.META_FILE).read_text(encoding="utf-8"))
    meta["digest"] = "0" * 64
    (copy / contract.META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    with pytest.raises(contract.ContractError, match="digest"):
        contract.load(copy)


def test_bindings_from_another_spec_are_refused() -> None:
    pinned = contract.pinned()
    other = contract.Contract(pinned.root, {**pinned.meta, "files": {**pinned.meta["files"], "spec.yaml": "f" * 64}})
    with pytest.raises(contract.ContractError, match="codegen"):
        contract.check_bindings(other)
