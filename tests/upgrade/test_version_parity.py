"""Cross-surface parity guard for version/RC-tag handling.

``app/upgrade/channel.py`` is the single source of truth for version /
RC-tag parsing and PEP 440 ordering. PowerShell, bash, and TypeScript
can't import it, so they each carry a thin mirror (the shell installers
shell out to channel.py for the hard ordering; only a fail-fast shape
check stays inline, and the Electron main process mirrors parseVersion).

These tests are a tripwire: if someone teaches one surface a new version
shape but forgets the others, the relevant assertion fails. They are
deliberately coarse — they check the *dev-awareness markers* are present,
not full behaviour (that lives in each surface's own tests).
"""

from __future__ import annotations

from pathlib import Path

from app.upgrade import channel

_REPO = Path(__file__).resolve().parents[2]


def test_canonical_corpus_ordering() -> None:
    """The ordering every surface must agree on."""
    corpus = [
        "0.2.8",
        "0.2.9rc1.dev1",
        "0.2.9rc1.dev2",
        "0.2.9rc1",
        "0.2.9rc2.dev1",
        "0.2.9",
    ]
    parsed = [channel.parse_pep440(v) for v in corpus]
    assert parsed == sorted(parsed), "corpus is already in strictly ascending PEP 440 order"


def test_install_ps1_is_dev_aware() -> None:
    text = (_REPO / "install" / "install.ps1").read_text(encoding="utf-8")
    # Fail-fast -Version shape check accepts the optional .devM suffix.
    assert r"rc\d+(\.dev\d+)?" in text
    # Test resolution is delegated to the canonical resolver, not an
    # in-script sort that can't order rcN.devM.
    assert "Resolve-CremindWheel" in text
    assert "[version]::new" not in text, "in-script version sort must not resurface"


def test_install_sh_is_dev_aware() -> None:
    text = (_REPO / "install" / "install.sh").read_text(encoding="utf-8")
    assert r"rc[0-9]+(\.dev[0-9]+)?" in text
    assert "resolve_cremind_wheel" in text
    assert "sort -V" not in text, "PEP-440-unaware sort -V must not resurface for version resolution"


def test_electron_main_ts_is_dev_aware() -> None:
    text = (_REPO / "ui" / "electron" / "main.ts").read_text(encoding="utf-8")
    # RC_TAG_RE matches the canonical v<X.Y.Z>[.W]rc<N>.dev<M> shape
    # (mandatory .dev<M>; mirrors _RC_TAG in app/upgrade/channel.py).
    assert r"v(\d+\.\d+\.\d+(?:\.\d+)?)rc(\d+)\.dev(\d+)" in text
    # parseVersion accepts rcN and rcN.devM as PEP 440 version strings.
    assert r"rc(\d+)(?:\.dev(\d+))?" in text
