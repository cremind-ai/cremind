"""Guardrail: skill-creator's ``validate.py`` parses frontmatter identically to
the Cremind scanner.

``validate.py`` must run standalone under ``uv`` from a profile skills dir where
``app.*`` is not importable, so it *duplicates* ``app/skills/scanner.py``'s
frontmatter parsing instead of importing it. That duplication can silently drift
— and when it does, a skill can pass ``validate.py`` yet be rejected (silently
skipped) by the scanner on load, which is exactly the failure this test guards
against. Here we load both ``_parse_frontmatter`` implementations and assert they
make the **same accept/reject decision** (and yield the same dict on accept)
across a battery of tricky inputs.
"""
from __future__ import annotations

import importlib.util
from typing import Any

import pytest

from app.skills import scanner
from app.skills.sync import BUILTIN_SKILLS_DIR

_VALIDATE_PATH = BUILTIN_SKILLS_DIR / "skill-creator" / "scripts" / "validate.py"


def _load_validate_module():
    spec = importlib.util.spec_from_file_location("skill_creator_validate", _VALIDATE_PATH)
    assert spec and spec.loader, f"cannot load {_VALIDATE_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_validate = _load_validate_module()


def _scanner_decision(text: str) -> tuple[bool, dict[str, Any] | None]:
    data, _clean = scanner._parse_frontmatter(text)
    return (bool(data), data if data else None)


def _validate_decision(text: str) -> tuple[bool, dict[str, Any] | None]:
    data, _body, err = _validate._parse_frontmatter(text)
    ok = err is None and isinstance(data, dict) and bool(data)
    return (ok, data if ok else None)


_VALID = (
    "---\n"
    "name: demo\n"
    "description: A valid one-line description.\n"
    "metadata:\n"
    "  events:\n"
    "    event_type:\n"
    "      - name: ping\n"
    "        description: A ping event.\n"
    "---\n\n# demo\nbody\n"
)
_COLON_UNQUOTED = (
    "---\nname: demo\n"
    "description: Shows how events work: emits a ping and reacts.\n"
    "---\n\n# demo\n"
)
_COLON_QUOTED = (
    "---\nname: demo\n"
    'description: "Shows how events work: emits a ping and reacts."\n'
    "---\n\n# demo\n"
)
_DASHES_IN_VALUE = (
    "---\nname: demo\ndescription: has --- inside\n---\n\n# demo\n"
)
_NO_CLOSING_FENCE = "---\nname: demo\ndescription: no closing fence\n\n# demo\n"
_NON_MAPPING = "---\n- a\n- b\n---\n\n# demo\n"
_NO_FRONTMATTER = "# demo\n\nno frontmatter here\n"

# (label, text, expected_accept) — expected_accept documents intent; the core
# assertion is that scanner and validator AGREE with each other.
_CASES = [
    ("valid", _VALID, True),
    ("colon_unquoted_rejected", _COLON_UNQUOTED, False),
    ("colon_quoted_accepted", _COLON_QUOTED, True),
    ("dashes_in_value_truncated_same", _DASHES_IN_VALUE, True),
    ("no_closing_fence", _NO_CLOSING_FENCE, False),
    ("non_mapping", _NON_MAPPING, False),
    ("no_frontmatter", _NO_FRONTMATTER, False),
]


@pytest.mark.parametrize("label,text,expected_accept", _CASES, ids=[c[0] for c in _CASES])
def test_validator_matches_scanner(label: str, text: str, expected_accept: bool) -> None:
    s_ok, s_data = _scanner_decision(text)
    v_ok, v_data = _validate_decision(text)

    assert s_ok == v_ok, (
        f"[{label}] scanner accept={s_ok} but validate.py accept={v_ok} — the "
        f"skill-creator validator has drifted from app/skills/scanner.py"
    )
    assert s_ok == expected_accept, f"[{label}] expected accept={expected_accept}, got {s_ok}"
    if s_ok:
        assert s_data == v_data, (
            f"[{label}] both accept but parsed to different dicts:\n"
            f"  scanner:  {s_data}\n  validate: {v_data}"
        )
