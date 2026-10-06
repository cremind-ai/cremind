"""`cremind tags tools preview text` typography options: --weight, --leading, --tracking, --no-grid-fit."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from app.tags.runtime.cli.main import app
from app.tags.runtime.layout import FontContext

pytestmark = pytest.mark.fonts
runner = CliRunner()
CACHE = Path(__file__).resolve().parents[4] / "fonts" / "cache"
TEXT = "Approve deployment of release 2.4 to production? Tóm tắt cuộc họp"


def _text(fonts: Any, tmp_path: Path, *args: str) -> Any:
    return runner.invoke(app, ["preview", "text", TEXT, "--size", "16", "--width", "200", "--lang", "vi",
                               "--out", str(tmp_path / "t.png"), *args, "--pack", str(fonts.pack_path),
                               "--cache", str(CACHE)])


def test_typography_options(fonts: Any, tmp_path: Path) -> None:
    plain = _text(fonts, tmp_path)
    assert plain.exit_code == 0, plain.output
    assert "faces [1]" in plain.output and "top=0 baseline=18" in plain.output
    tight = _text(fonts, tmp_path, "--leading", "tight", "--tracking", "1")
    assert tight.exit_code == 0, tight.output
    assert "top=0 baseline=13" in tight.output and tight.output != plain.output
    explicit = _text(fonts, tmp_path, "--leading", "20,6", "--no-grid-fit")
    assert explicit.exit_code == 0 and "top=0 baseline=20" in explicit.output
    bold = _text(fonts, tmp_path, "--weight", "bold")
    assert bold.exit_code == 0, bold.output
    if FontContext.for_fontset(fonts).has_weight("bold", 16):
        assert "faces [172]" in bold.output and "note:" not in bold.output
    else:
        assert "no bold face" in bold.output and "faces [1]" in bold.output
    for args in (("--weight", "heavy"), ("--leading", "loose"), ("--leading", "3"), ("--leading", "-1,2")):
        r = _text(fonts, tmp_path, *args)
        assert r.exit_code == 1, (args, r.output)
    assert _text(fonts, tmp_path, "--tracking", "9").exit_code == 2  # outside the option's range
    r = runner.invoke(app, ["preview", "text", "x", "--size", "20", "--out", str(tmp_path / "x.png"),
                            "--pack", str(fonts.pack_path), "--cache", str(CACHE)])
    assert r.exit_code == 1 and "its text sizes" in r.output


def test_bold_on_a_pack_without_bold_says_so(dev_fonts: Any, tmp_path: Path) -> None:
    r = _text(dev_fonts, tmp_path, "--weight", "bold")
    assert r.exit_code == 0, r.output
    assert "no bold face at 16 px" in r.output and "faces [1]" in r.output
