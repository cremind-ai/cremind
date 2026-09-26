"""cite.py against the fixture the web UI is also held to.

The web chat numbers citation chips itself (ui/src/utils/citations.ts), while
channels and the CLI number them from :mod:`app.documents.cite`. A reader who
sees "[2]" in a Telegram footer and "[2]" in the browser must be looking at
the same source, so both grammars are pinned to one set of cases:
``ui/tests/fixtures/citation-grammar.json``. Change the grammar on either side
and this test or ``ui/tests/citation-grammar.test.mjs`` fails until the other
side (and the fixture) follow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.documents.cite import number_tokens, parse_tokens

FIXTURE = Path(__file__).resolve().parents[2] / "ui" / "tests" / "fixtures" / "citation-grammar.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_parse_matches_the_shared_fixture(case: dict) -> None:
    assert parse_tokens(case["text"]) == case["tokens"]


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_numbering_matches_the_shared_fixture(case: dict) -> None:
    assert list(number_tokens(case["text"]).items()) == [tuple(p) for p in case["numbers"]]
