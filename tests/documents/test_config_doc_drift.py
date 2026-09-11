"""Doc/code drift pin for `[cli]cremind config.md`.

Three things this doc got wrong and must not lose again.

First, the Developer page's configuration-file re-download shipped with no
documentation at all. The whole point of that card is recovering a file the
Setup Wizard hands over exactly once, so a user who lost it asks
``documentation_search`` for it — and only the frontmatter ``description`` is
embedded, so the retrieval intent has to live there, not just in the body.

Second, ``--json`` is a *root* flag on the `cremind` app, not an option on any
`cremind config` subcommand: ``cremind config schema --json`` exits with
``No such option: --json``. The doc's examples are copy-pasted by users and by
the agent, so a wrong form there is a broken command.

Third — and this is what the table tests below exist for — the keys table
drifted away from ``CONFIG_SCHEMA`` in every direction at once. It documented
``tool_result.preserve_recent`` / ``head_tokens`` / ``tail_tokens``, three knobs
nothing in ``app/`` ever read; it printed ``tool_result.max_tokens``'s default
as ``1000`` when the real default was ``4000``; and every "the Nth card on the
page" ordinal was one short because the ``channels`` group was inserted into the
schema and never into the doc. A user reading this doc is reading it *instead*
of the code, and the agent quotes it verbatim, so a stale table is a wrong
answer with a citation attached. The tests below re-derive every cell from
``CONFIG_SCHEMA`` + ``settings.toml`` so the doc cannot silently fall behind
again in either direction: a key in the schema must be documented, and a key in
the doc must exist in the schema.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config.config_schema import CONFIG_SCHEMA, Field
from app.config.user_config import resolve_default

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind config.md"
)

#: "(the third card on the page)" → 3. Only as many as there are groups.
_ORDINALS = (
    "first", "second", "third", "fourth", "fifth",
    "sixth", "seventh", "eighth", "ninth", "tenth",
)
#: "grouped into six areas" → 6.
_NUMBER_WORDS = (
    "zero", "one", "two", "three", "four", "five",
    "six", "seven", "eight", "nine", "ten",
)

_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")
#: Leading "<min> – <max>" of a Range cell; the en dash is what the doc uses.
_RANGE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[–—-]\s*(-?\d+(?:\.\d+)?)")


def _doc_text() -> str:
    assert DOC.exists(), f"missing bundled doc: {DOC.name}"
    return DOC.read_text(encoding="utf-8")


def _description() -> str:
    text = _doc_text().lstrip()
    end = text.find("---", 3)
    assert end != -1, "frontmatter is not closed"
    return text[3:end]


def _code_blocks() -> list[str]:
    return re.findall(r"```(?:bash|text)?\n(.*?)```", _doc_text(), re.S)


# ---------------------------------------------------------------- doc parsing


def _group_sections() -> dict[str, str]:
    """``{group_name: section body}`` for every ``### Group `x` — Label`` heading."""
    text = _doc_text()
    heads = list(re.finditer(r"^### Group `(\w+)`.*$", text, re.M))
    assert heads, "the doc has no '### Group `x`' sections"
    sections: dict[str, str] = {}
    for i, head in enumerate(heads):
        start = head.end()
        # A section runs to the next group heading, or to the next `##`/`###`
        # heading of any kind (e.g. "## Worked examples") for the last group.
        nxt = re.search(r"^##\s", text[start:], re.M)
        end = start + nxt.start() if nxt else len(text)
        sections[head.group(1)] = text[start:end]
    return sections


def _split_row(line: str) -> list[str]:
    cells = line.split("|")
    # A well-formed markdown row is fenced by pipes, producing empty edge cells.
    if cells and not cells[0].strip():
        cells = cells[1:]
    if cells and not cells[-1].strip():
        cells = cells[:-1]
    return [c.strip() for c in cells]


def _first_table(section: str) -> tuple[dict[str, int], list[list[str]]]:
    """First markdown table in ``section`` as ``(header name -> index, body rows)``.

    Column *names* are the contract, never positions: the ``channels`` table has
    no "UI label" and no "Range" column, so a positional read would silently
    compare a Type against a Default.
    """
    rows: list[list[str]] = []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("|"):
            rows.append(_split_row(stripped))
        elif rows:
            break  # table ended
    assert len(rows) >= 3, "section has no markdown table with a body"
    header = {name.lower(): idx for idx, name in enumerate(rows[0])}
    body = [
        row for row in rows[1:]
        if not all(_SEPARATOR_CELL.match(c) for c in row if c)
    ]
    return header, body


def _cell(row: list[str], header: dict[str, int], name: str) -> str | None:
    idx = header.get(name)
    if idx is None or idx >= len(row):
        return None
    return row[idx].strip().strip("`").strip()


def _documented_keys() -> dict[str, tuple[str, dict[str, int], list[str]]]:
    """``{dotted key: (group it was documented under, header map, row)}``."""
    found: dict[str, tuple[str, dict[str, int], list[str]]] = {}
    for group_name, section in _group_sections().items():
        header, body = _first_table(section)
        for row in body:
            key = _cell(row, header, "key")
            if not key or "." not in key:
                continue
            assert key not in found, (
                f"{key!r} is documented twice (second time under "
                f"'### Group `{group_name}`')"
            )
            found[key] = (group_name, header, row)
    return found


def _rendered_default(key: str, field: Field) -> str:
    value = resolve_default(key)
    if field.type == "boolean":
        return "true" if value else "false"
    return str(value)


# ------------------------------------------------------------- existing pins


def test_the_description_carries_the_config_file_recovery_intent():
    description = _description().lower()
    for keyword in (
        "configuration file",
        "setup wizard",
        "re-download",
        "lost my setup file",
        "recover my token",
        "developer",
    ):
        assert keyword in description, f"description never mentions {keyword!r}"


def test_the_body_documents_what_the_developer_card_actually_ships():
    text = _doc_text()
    assert "Sidebar → Developer → Configuration File" in text, (
        "the doc must name the exact path to the card"
    )
    assert "cremind-<profile>-config." in text, "the filename shape is missing"
    for label in ("Markdown (.md)", "JSON (.json)", "Env file (.env)"):
        assert label in text, f"format option {label!r} is not documented"
    assert "admin-only" in text, "the admin-only gate is not documented"
    assert "plain text" in text, "the JWT/passwords warning is missing"
    # No CLI command produces the file, so the doc has to point at the pieces.
    for command in ("cremind auth show", "cremind server environment"):
        assert command in text, f"CLI equivalent {command!r} is not mentioned"


def test_no_example_puts_json_after_a_config_subcommand():
    """``--json`` is declared on the root callback, never on a subcommand."""
    bad = re.compile(r"cremind\s+config\s+\w+[^\n|]*--json")
    for block in _code_blocks():
        for line in block.splitlines():
            assert not bad.search(line), (
                "`--json` is a ROOT flag — write `cremind --json config <sub>`; "
                f"this example fails at parse time: {line.strip()!r}"
            )
    assert "cremind --json config" in _doc_text(), (
        "the doc must show the working root-flag form at least once"
    )


# ------------------------------------------------------- schema ↔ table drift


def test_every_schema_key_is_documented_with_its_real_type_default_and_range():
    documented = _documented_keys()
    for group_name, group in CONFIG_SCHEMA.items():
        for field_name, field in group.fields.items():
            key = f"{group_name}.{field_name}"
            assert key in documented, (
                f"{key} is in CONFIG_SCHEMA but documented nowhere in "
                f"{DOC.name} — add a row to the `{group_name}` table"
            )
            doc_group, header, row = documented[key]
            assert doc_group == group_name, (
                f"{key} is documented under '### Group `{doc_group}`' but the "
                f"schema puts it in `{group_name}`"
            )

            assert _cell(row, header, "type") == field.type, (
                f"{key}: doc says type {_cell(row, header, 'type')!r}, "
                f"schema says {field.type!r}"
            )

            expected_default = _rendered_default(key, field)
            assert _cell(row, header, "default") == expected_default, (
                f"{key}: doc says default "
                f"{_cell(row, header, 'default')!r}, settings.toml says "
                f"{expected_default!r}"
            )

            label_cell = _cell(row, header, "ui label")
            if label_cell is not None and field.label is not None:
                assert label_cell == field.label, (
                    f"{key}: doc's UI label {label_cell!r} does not match the "
                    f"schema label {field.label!r} the Config page renders"
                )

            range_cell = _cell(row, header, "range")
            if range_cell is not None and field.min is not None and field.max is not None:
                match = _RANGE.match(range_cell)
                assert match, (
                    f"{key}: schema declares min={field.min} max={field.max}, but "
                    f"the Range cell {range_cell!r} does not start with a range"
                )
                low, high = float(match.group(1)), float(match.group(2))
                assert (low, high) == (float(field.min), float(field.max)), (
                    f"{key}: doc's range is {low}–{high}, schema says "
                    f"{field.min}–{field.max}"
                )


def test_no_documented_key_is_missing_from_the_schema():
    """The direction that catches a knob the code stopped honouring.

    ``tool_result.head_tokens`` and friends outlived their only consumer and sat
    in this table for releases, telling users to tune something inert.
    """
    for key, (group_name, _header, _row) in _documented_keys().items():
        group, _, field_name = key.partition(".")
        assert group in CONFIG_SCHEMA, (
            f"{key} is documented under '### Group `{group_name}`' but "
            f"CONFIG_SCHEMA has no group {group!r}"
        )
        assert field_name in CONFIG_SCHEMA[group].fields, (
            f"{key} is documented but no longer exists in CONFIG_SCHEMA — "
            "delete the row (or restore the field)"
        )


def test_each_group_states_its_real_card_position():
    """The "(the Nth card on the page)" ordinals follow CONFIG_SCHEMA order."""
    sections = _group_sections()
    for index, group_name in enumerate(CONFIG_SCHEMA, start=1):
        assert group_name in sections, (
            f"CONFIG_SCHEMA group {group_name!r} has no '### Group "
            f"`{group_name}`' section in {DOC.name}"
        )
        flat = " ".join(sections[group_name].split())
        match = re.search(r"\(the (\w+) card on the page\)", flat)
        assert match, (
            f"group {group_name!r} is missing its "
            "'**Settings → Config card:** ... (the Nth card on the page)' line"
        )
        assert match.group(1) == _ORDINALS[index - 1], (
            f"group {group_name!r} is card #{index} in CONFIG_SCHEMA but the "
            f"doc calls it the {match.group(1)} card"
        )


def test_the_overview_counts_and_names_every_group():
    text = _doc_text()
    match = re.search(r"grouped into (\w+) areas", text)
    assert match, "the intro no longer says how many areas the settings form"
    expected = _NUMBER_WORDS[len(CONFIG_SCHEMA)]
    assert match.group(1) == expected, (
        f"the intro says {match.group(1)!r} areas but CONFIG_SCHEMA has "
        f"{len(CONFIG_SCHEMA)} groups ({expected!r})"
    )

    # The bullet list runs from the intro line to the first blank line after it.
    bullets: list[str] = []
    for line in text[match.end():].splitlines():
        if line.startswith("- ") or (bullets and line.startswith("  ")):
            bullets.append(line)
        elif bullets:
            break
    overview = "\n".join(bullets)
    assert overview, "the intro's bullet list of areas is missing"
    for group_name, group in CONFIG_SCHEMA.items():
        assert f"**{group.label}**" in overview, (
            f"group {group_name!r} ({group.label!r}) is in CONFIG_SCHEMA but "
            "not listed in the intro's areas"
        )
