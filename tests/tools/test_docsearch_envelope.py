"""documentation_search delivers a long document as a navigable envelope.

The reasoning agent head-clips every tool result to the profile's
``tool_result.max_tokens``. A document longer than that used to arrive as its
preamble plus a truncation notice every single time, so the subcommand the
agent asked for was never visible. The search leaf now owns a delivery budget:
a body that fits goes out exactly as before, one that does not comes back as
its head, a sized table of contents and the sections whose headings match the
query, closed by a footer that points at the section-reader leaf.

Token counts are made deterministic by patching ``ds._tokens`` to chars/4. The
same function sizes the sections, so one patch covers every measurement — and
it keeps tiktoken from fetching an encoding over the network.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.constants import ChatCompletionTypeEnum
from app.documents.sections import rank_sections_for_query, split_sections
from app.documents.sync import DESCRIPTION_MAX_CHARS
from app.utils.logger import logger

import app.tools.builtin.documentation_search as ds


# ── Fixtures and fakes ──────────────────────────────────────────────────────


def _quarter(text: str) -> int:
    """The deterministic tokenizer every test in this file runs under."""
    return len(text) // 4 if text else 0


@pytest.fixture(autouse=True)
def _deterministic_tokens(monkeypatch):
    monkeypatch.setattr(ds, "_tokens", _quarter)
    monkeypatch.setattr(ds, "resolve_system_var_tokens", lambda body, profile: body)


_JUDGE_USAGE = {"input_tokens": 10, "output_tokens": 1}


class _FakeLLM:
    """Judge that selects candidate 0, then reports ``usage`` on DONE."""

    def __init__(self, *, usage: Optional[Dict[str, int]] = None):
        self.provider_name = "fake"
        self.model_name = "fake-mini"
        self.model_label = "fake/fake-mini"
        self.usage = dict(usage or _JUDGE_USAGE)
        self.calls: List[Dict[str, Any]] = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        yield {
            "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
            "data": {"function": [
                {"name": "select_document", "arguments": {"index": 0}}
            ]},
        }
        yield {"type": ChatCompletionTypeEnum.DONE, **self.usage}


class _Svc:
    """Doc service stand-in: one hit named ``name`` whose body is ``body``."""

    def __init__(self, *, name: str, body: str, scope: Optional[str], description: str):
        self.hits = [{
            "file_path": f"/docs/{name}.md", "text": description,
            "name": name, "scope": scope, "score": 0.9,
        }]
        self.body = body

    def search(self, *, query, profile, limit, scopes=None):
        return self.hits

    def read_body(self, path):
        return self.body


def _patch_service(monkeypatch, *, name: str, body: str, scope: Optional[str] = "shared",
                   description: str = "Reference for one command group.") -> _Svc:
    svc = _Svc(name=name, body=body, scope=scope, description=description)
    monkeypatch.setattr(ds, "get_service", lambda: svc)
    return svc


def _patch_config(monkeypatch, *, max_tokens: int = 1500, enabled: bool = True,
                  per_profile: Optional[Dict[str, int]] = None):
    """Patch the per-profile agent config the delivery budget is derived from."""
    def _resolve(profile: str):
        clamp = (per_profile or {}).get(profile, max_tokens)
        return SimpleNamespace(tool_result_enabled=enabled, tool_result_max_tokens=clamp)

    monkeypatch.setattr(ds, "_resolve_clamp", _resolve)


def _patch_registry(monkeypatch, *, exec_shell: bool = True, section_leaf: Any = True,
                    raises: Optional[Exception] = None):
    """Patch the lazily imported registry with per-tool leaves.

    ``section_leaf`` is the ``enabled`` flag of the section-reader leaf: a bool,
    ``None`` to leave the leaf out of the payload, or a ``{profile: bool}`` map.
    """
    def _leaves(profile: str, tool_id: str) -> Dict[str, Any]:
        if tool_id == "exec_shell":
            return {"leaves": [{"leaf_name": "exec_shell", "enabled": exec_shell}]}
        if tool_id == "documentation_search":
            enabled = section_leaf.get(profile, True) if isinstance(section_leaf, dict) else section_leaf
            leaves = [{"leaf_name": ds.SEARCH_LEAF_NAME, "enabled": True}]
            if enabled is not None:
                leaves.append({"leaf_name": ds.SECTION_LEAF_NAME, "enabled": enabled})
            return {"leaves": leaves}
        raise KeyError(tool_id)

    def _factory():
        if raises is not None:
            raise raises
        return SimpleNamespace(leaves_for_profile=_leaves)

    monkeypatch.setattr("app.tools.registry.get_tool_registry", _factory)


def _search(query: str, *, profile: str = "admin", llm: Optional[_FakeLLM] = None):
    return asyncio.run(ds.DocumentationSearchTool().run(
        {"query": query, "_llm": llm or _FakeLLM(), "_profile": profile}
    ))


def _text(res) -> str:
    assert res.content, f"expected a text result, got {res.structured_content!r}"
    return res.content[0]["text"]


def _capture(level: str, fn):
    messages: List[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level=level)
    try:
        out = fn()
    finally:
        logger.remove(sink_id)
    return out, messages


def _directive() -> str:
    """The CLI directive as served when exec_shell's leaf is enabled."""
    return ds._CLI_EXECUTION_DIRECTIVE.format(fn="exec_shell") + "\n"


# ── Synthetic documents ─────────────────────────────────────────────────────

_LOREM = (
    "lorem ipsum dolor sit amet consectetur adipiscing elit sed eiusmod tempor "
    "incididunt labore dolore magna aliqua enim minim veniam quis nostrud"
).split()


def _para(n_words: int, seed: int = 0) -> str:
    return " ".join(_LOREM[(seed + i) % len(_LOREM)] for i in range(n_words))


INTRO = (
    "INTRO-MARKER Widgets are small reusable panels managed from the command "
    "line; this paragraph is the head of the document."
)

# Unique marker opening each section's body, so a test can tell which section
# bodies made it into the envelope. The head only carries INTRO-MARKER.
_SECTION_MARKERS = (
    "MARK-OVERVIEW", "MARK-CONCEPTS", "MARK-STORAGE", "MARK-COMMANDS",
    "MARK-LIST", "MARK-ADD", "MARK-REMOVE", "MARK-CONNECTION",
    "MARK-PERMISSION", "MARK-GLOSSARY", "MARK-CHANGELOG",
)


def _add_block(group: str = "widgets", *, add_paras: int = 0) -> str:
    """The ``add`` section verbatim, including a bash fence of ``#`` comments."""
    cmd = f"cremind {group}"
    lines = [
        f"### `{cmd} add`",
        "",
        "MARK-ADD Creates a new entry. Server: $CREMIND_SERVER",
        "",
        "```bash",
        "# create one with the defaults",
        f"{cmd} add --name demo",
        "## a doubled comment, still not a heading",
        f"{cmd} add --name demo --force",
        "```",
    ]
    for i in range(add_paras):
        lines += ["", _para(60, i)]
    return "\n".join(lines)


def _long_body(group: str = "widgets", *, appendix_paras: int = 8, add_paras: int = 0) -> str:
    """H1 + intro, then ## sections with ### subsections; one is ``<cmd> add``."""
    cmd = f"cremind {group}"
    appendix = "\n\n".join(_para(60, i) for i in range(appendix_paras))
    return "\n".join([
        f"# {cmd}", "", INTRO, "",
        "## Overview", "", "MARK-OVERVIEW " + _para(30), "",
        "### Concepts", "", "MARK-CONCEPTS " + _para(40, 1), "",
        "### Storage", "", "MARK-STORAGE " + _para(40, 2), "",
        "## Commands", "", "MARK-COMMANDS " + _para(20, 3), "",
        f"### `{cmd} list`", "", "MARK-LIST " + _para(40, 4), "",
        _add_block(group, add_paras=add_paras), "",
        f"### `{cmd} remove`", "", "MARK-REMOVE " + _para(40, 5), "",
        "## Troubleshooting", "",
        "### Connection errors", "", "MARK-CONNECTION " + _para(40, 6), "",
        "### Permission errors", "", "MARK-PERMISSION " + _para(40, 7), "",
        "## Appendix", "",
        "### Glossary", "", "MARK-GLOSSARY " + appendix, "",
        "### Changelog", "", "MARK-CHANGELOG " + appendix, "",
    ])


def _short_body() -> str:
    return "\n".join([
        "# cremind widgets", "", INTRO, "",
        "## Commands", "",
        "### `cremind widgets add`", "", "MARK-ADD Creates a new entry.", "",
    ])


def _cli_doc(group: str, verbs, *, notes_paras: int = 16) -> str:
    """A CLI reference whose every command heading repeats the group name."""
    cmd = f"cremind {group}"
    parts = [f"# {cmd}", "", f"INTRO-MARKER Reference for the {cmd} command group.", "",
             "## Subcommands", ""]
    for verb in verbs:
        parts += [f"### `{cmd} {verb}`", "", f"MARK-{verb.upper()} " + _para(40, len(verb)), ""]
    notes = "\n\n".join(_para(60, i) for i in range(notes_paras))
    parts += ["## Appendix", "", "### Notes", "", "MARK-NOTES " + notes, ""]
    return "\n".join(parts)


def _expected_toc_lines(body: str) -> List[str]:
    """One ``- title (inclusive tokens)`` line per section, sized independently."""
    lines, _, sections = split_sections(body)
    return [
        f"{'  ' * (s.level - 2)}- {s.title} ({_quarter(chr(10).join(lines[s.start:s.end]))})"
        for s in sections
    ]


_TOC_HEADING = "## Table of contents (section → tokens when read)"
_MATCH_HEADING = "## Sections matching the query"
_NO_MATCH_LINE = "(No section heading matched the query."


def _toc_block(text: str) -> str:
    start = text.index(_TOC_HEADING) + len(_TOC_HEADING) + 1
    return text[start:text.index("\n\n" + _MATCH_HEADING)]


def _footer(text: str) -> str:
    return text.splitlines()[-1]


# ── 1. Under budget: unchanged behaviour ────────────────────────────────────


def test_under_budget_non_cli_body_is_byte_identical(monkeypatch):
    body = _short_body()
    _patch_config(monkeypatch, max_tokens=4000)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    assert _text(_search("how do I add a widget")) == body


def test_under_budget_cli_body_is_directive_plus_body(monkeypatch):
    body = _short_body()
    _patch_config(monkeypatch, max_tokens=4000)
    _patch_registry(monkeypatch, exec_shell=True)
    _patch_service(monkeypatch, name="[cli]cremind widgets", body=body)

    assert _text(_search("how do I add a widget")) == _directive() + body


@pytest.mark.parametrize("extra_chars, enveloped", [(0, False), (4, True)])
def test_a_body_exactly_at_the_budget_is_still_delivered_whole(monkeypatch, extra_chars, enveloped):
    # clamp 1500 -> budget 1400 tokens == 5600 chars; one token more tips it over.
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    body = ("# Plain notes\n\n" + "lorem ipsum dolor sit amet " * 400)[: 1400 * 4 + extra_chars]
    _patch_service(monkeypatch, name="plain notes", body=body)

    text = _text(_search("anything at all"))

    if enveloped:
        assert text.startswith('[Document "plain notes" (shared) — 1401 tokens')
        assert _quarter(text) <= 1400
    else:
        assert text == body


# ── 2. Over budget: the envelope ────────────────────────────────────────────


def test_over_budget_non_cli_doc_comes_back_as_an_envelope(monkeypatch):
    name = "widgets guide"
    body = _long_body()
    budget = 1500 - 100  # clamp minus the margin; no directive for a non-CLI doc
    assert _quarter(body) > budget, "fixture must exceed the budget"
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch, section_leaf=True)
    _patch_service(monkeypatch, name=name, body=body, scope="admin")

    text = _text(_search("how do I add a widget"))

    # Header: name, scope, the whole body's size and the budget it missed.
    assert text.startswith(
        f'[Document "{name}" (admin) — {_quarter(body)} tokens, too long to deliver '
        f"whole within the {budget}-token budget."
    )
    # The head (H1 + intro) rides along.
    assert "# cremind widgets" in text
    assert INTRO in text
    assert "[… head truncated]" not in text
    # One sized TOC line per section, in document order — and the '#'/'##'
    # comment lines inside the bash fence are not mistaken for headings.
    toc = _toc_block(text).splitlines()
    assert toc == _expected_toc_lines(body)
    assert len(toc) == 13
    assert not any("doubled comment" in line or "create one" in line for line in toc)
    # The section whose heading matches the query is included in full.
    assert _add_block() in text
    # No other section body is.
    for marker in _SECTION_MARKERS:
        if marker != "MARK-ADD":
            assert marker not in text, marker
    # Footer: names the section reader and the exact document argument, and
    # suggests a heading that has not been shown yet (backticks dropped).
    footer = _footer(text)
    assert footer.startswith(f'[End of excerpt from "{name}".')
    assert f"`{ds.SECTION_LEAF_FN}`" in footer
    assert f'document="{name}"' in footer
    assert 'e.g. section="cremind widgets list"' in footer
    assert footer.endswith("]")
    # And the whole envelope fits the budget.
    assert _quarter(text) <= budget


def test_envelope_defaults_to_the_shared_scope_when_the_hit_has_none(monkeypatch):
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=_long_body(), scope=None)

    text = _text(_search("how do I add a widget"))

    assert text.startswith('[Document "widgets guide" (shared) — ')


def test_several_matching_sections_are_included_best_first(monkeypatch):
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=_long_body())

    text = _text(_search("add or remove a widget"))

    assert "MARK-ADD" in text and "MARK-REMOVE" in text
    # Equal scores keep document order.
    assert text.index("MARK-ADD") < text.index("MARK-REMOVE")
    assert "MARK-LIST" not in text
    assert _quarter(text) <= 1400


def test_system_vars_are_resolved_before_the_body_is_sized_and_sliced(monkeypatch):
    body = _long_body()
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)
    resolved_server = "https://cremind.test:8443"
    monkeypatch.setattr(
        ds, "resolve_system_var_tokens",
        lambda b, profile: b.replace("$CREMIND_SERVER", resolved_server),
    )

    text = _text(_search("how do I add a widget"))

    assert "$CREMIND_SERVER" not in text
    assert resolved_server in text
    # The TOC sizes are those of the text the agent actually receives.
    resolved = body.replace("$CREMIND_SERVER", resolved_server)
    assert _toc_block(text).splitlines() == _expected_toc_lines(resolved)


# ── 3. [cli] doc: directive first, then the envelope, all under the clamp ───


def test_over_budget_cli_doc_keeps_the_directive_first_and_the_total_under_the_clamp(monkeypatch):
    name = "[cli]cremind widgets"
    clamp = 2000
    directive = _directive()
    budget = clamp - _quarter(directive) - 100
    assert budget > 1200, "the floor must not decide this case"
    body = _long_body()
    assert _quarter(body) > budget
    _patch_config(monkeypatch, max_tokens=clamp)
    _patch_registry(monkeypatch, exec_shell=True, section_leaf=True)
    _patch_service(monkeypatch, name=name, body=body)

    text = _text(_search("how do I add a widget"))

    assert text.index("[Agent directive") == 0
    assert text.startswith(directive)
    envelope = text[len(directive):]
    assert envelope.startswith(
        f'[Document "{name}" (shared) — {_quarter(body)} tokens, too long to deliver '
        f"whole within the {budget}-token budget."
    )
    assert _add_block() in envelope
    assert _quarter(envelope) <= budget
    assert _quarter(text) <= clamp


# ── 4. A query naming only the document matches no section ─────────────────


def test_query_naming_only_the_document_matches_no_section(monkeypatch):
    body = _long_body()
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch, exec_shell=False)
    _patch_service(monkeypatch, name="[cli]cremind widgets", body=body)

    text = _text(_search("cremind widgets"))

    assert text.startswith('[Document "[cli]cremind widgets"')
    assert _NO_MATCH_LINE in text
    after_heading = text.split(_MATCH_HEADING, 1)[1]
    assert after_heading.lstrip("\n").startswith(_NO_MATCH_LINE)
    for marker in _SECTION_MARKERS:
        assert marker not in text, marker
    # The head and the full table of contents are still there to navigate by.
    assert INTRO in text
    assert _toc_block(text).splitlines() == _expected_toc_lines(body)


# ── 5. Section reader disabled: the footer never points at it ───────────────


def test_footer_says_the_reader_is_disabled_when_its_leaf_is_off(monkeypatch):
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch, section_leaf=False)
    _patch_service(monkeypatch, name="widgets guide", body=_long_body())

    text = _text(_search("how do I add a widget"))

    footer = _footer(text)
    assert footer.startswith('[End of excerpt from "widgets guide".')
    assert "is disabled for this profile" in footer
    assert f"cremind tools set-leaf documentation_search {ds.SECTION_LEAF_NAME}=true" in footer
    # Nowhere is the gone function offered as callable.
    assert ds.SECTION_LEAF_FN not in text
    assert "To read another section, call" not in text
    # The matched section is still delivered — only the pointer changes.
    assert "MARK-ADD" in text


@pytest.mark.parametrize("registry_kwargs", [
    {"section_leaf": True},
    {"section_leaf": None},  # the leaf is absent from the payload
    {"raises": RuntimeError("registry not initialized")},
], ids=["enabled", "absent", "registry-unavailable"])
def test_footer_names_the_reader_unless_it_is_explicitly_disabled(monkeypatch, registry_kwargs):
    # Fails OPEN: the reader ships in this very group, so only an explicit
    # per-leaf disable can take it away.
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch, **registry_kwargs)
    _patch_service(monkeypatch, name="widgets guide", body=_long_body())

    footer = _footer(_text(_search("how do I add a widget")))

    assert f"call `{ds.SECTION_LEAF_FN}`" in footer
    assert "disabled" not in footer


def test_the_set_leaf_command_the_disabled_footer_names_exists():
    # Drift guard: the footer tells the user to run `cremind tools set-leaf`.
    from app.cli.commands.tools import tools_app

    assert "set-leaf" in {cmd.name for cmd in tools_app.registered_commands}


# ── 6. The clamp turned off: whole body, however large ─────────────────────


def test_clamp_disabled_delivers_the_whole_body_even_when_huge(monkeypatch):
    body = _long_body(appendix_paras=60)
    assert _quarter(body) > 10_000
    _patch_config(monkeypatch, max_tokens=1500, enabled=False)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    assert _text(_search("how do I add a widget")) == body


def test_clamp_disabled_cli_doc_is_directive_plus_whole_body(monkeypatch):
    body = _long_body(appendix_paras=60)
    _patch_config(monkeypatch, enabled=False)
    _patch_registry(monkeypatch, exec_shell=True)
    _patch_service(monkeypatch, name="[cli]cremind widgets", body=body)

    assert _text(_search("how do I add a widget")) == _directive() + body


# ── 7. Unresolvable config: the 4000-token default clamp ────────────────────


def test_unresolvable_agent_config_falls_back_to_the_default_clamp(monkeypatch):
    def _boom(profile):
        raise RuntimeError("config store offline")

    monkeypatch.setattr(ds, "_resolve_clamp", _boom)
    _patch_registry(monkeypatch)

    # Over any small budget but under 4000 - 100: delivered whole.
    medium = _long_body()
    assert 1400 < _quarter(medium) <= 3900
    _patch_service(monkeypatch, name="widgets guide", body=medium)
    res, warnings = _capture("WARNING", lambda: _search("how do I add a widget"))
    assert _text(res) == medium
    assert any(
        "could not resolve the agent config" in m and "'admin'" in m and "4000-token clamp" in m
        for m in warnings
    ), warnings

    # Over 3900: an envelope sized against the fallback clamp.
    big = _long_body(appendix_paras=24)
    assert _quarter(big) > 3900
    _patch_service(monkeypatch, name="widgets guide", body=big)
    text = _text(_search("how do I add a widget"))
    assert "within the 3900-token budget" in text
    assert _quarter(text) <= 3900


@pytest.mark.parametrize("clamp, reserved, expected", [
    (4000, "", 3900),
    (4000, "x" * 400, 3800),   # the reserved directive is measured, not guessed
    (1500, "", 1400),
    (1300, "x" * 400, 1100),   # a small clamp still keeps the whole result under it
    (350, "", 300),            # floor: below it no excerpt is useful
    (500, "x" * 400, 300),     # floor after the reserve
])
def test_delivery_budget_arithmetic(monkeypatch, clamp, reserved, expected):
    _patch_config(monkeypatch, max_tokens=clamp)
    assert ds._delivery_budget("admin", reserved) == expected


def test_delivery_budget_is_none_when_the_clamp_is_off(monkeypatch):
    _patch_config(monkeypatch, max_tokens=1500, enabled=False)
    assert ds._delivery_budget("admin", "x" * 4000) is None


def test_budget_and_reader_are_resolved_per_profile(monkeypatch):
    # Profiles are independent: one profile's clamp and leaf toggle never
    # change what another profile receives.
    body = _long_body()
    _patch_config(monkeypatch, per_profile={"alice": 1500, "admin": 100_000})
    _patch_registry(monkeypatch, section_leaf={"alice": False, "admin": True})
    _patch_service(monkeypatch, name="widgets guide", body=body)

    assert _text(_search("how do I add a widget", profile="admin")) == body

    alice = _text(_search("how do I add a widget", profile="alice"))
    assert alice.startswith('[Document "widgets guide"')
    assert "is disabled for this profile" in _footer(alice)


# ── 8. Internal-usage contract survives the envelope path ───────────────────


def test_envelope_result_carries_the_judge_usage(monkeypatch):
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=_long_body())
    llm = _FakeLLM(usage={
        "input_tokens": 321, "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 5, "output_tokens": 4,
    })

    res = _search("how do I add a widget", llm=llm)

    assert _text(res).startswith("[Document ")
    assert res.token_usage == {
        "input_tokens": 321, "cache_read_input_tokens": 7,
        "cache_creation_input_tokens": 5, "output_tokens": 4,
    }
    assert len(llm.calls) == 1


# ── 9. The envelope decision is logged at INFO ──────────────────────────────


def test_envelope_emits_an_info_summary(monkeypatch):
    body = _long_body()
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    res, messages = _capture("INFO", lambda: _search("how do I add a widget"))

    lines = [m for m in messages if "envelope doc=" in m]
    assert len(lines) == 1, messages
    line = lines[0]
    assert "[documentation_search] envelope doc='widgets guide' scope=shared" in line
    assert f"body_tokens={_quarter(body)} budget=1400" in line
    assert "toc_entries=13" in line
    assert "matched=['`cremind widgets add`']" in line
    assert f"delivered={_quarter(_text(res))}" in line


def test_under_budget_logs_no_envelope_line(monkeypatch):
    _patch_config(monkeypatch, max_tokens=4000)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=_short_body())

    _, messages = _capture("INFO", lambda: _search("how do I add a widget"))

    assert not [m for m in messages if "envelope doc=" in m]


# ── 10. Stop words: the document's own name, singular and plural ────────────


def test_query_using_the_singular_of_the_doc_name_matches_only_the_named_section(monkeypatch):
    name = "[cli]cremind channels"
    verbs = ("list", "add", "edit", "remove", "pair", "send")
    body = _cli_doc("channels", verbs)
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch, exec_shell=False)
    _patch_service(monkeypatch, name=name, body=body)
    assert _quarter(body) > 1400

    text = _text(_search("channel add"))

    assert "MARK-ADD" in text
    for verb in verbs:
        if verb != "add":
            assert f"MARK-{verb.upper()}" not in text, verb
    assert "MARK-NOTES" not in text


def test_the_singular_stop_word_is_what_keeps_the_toc_from_matching():
    # The premise of the test above: without the singular, "channel" (a word
    # long enough to prefix-match) hits "channels" in every command heading.
    _, _, sections = split_sections(_cli_doc("channels", ("list", "add", "edit")))
    commands = [s.title for s in sections if s.title.startswith("`")]

    with_plural_only = rank_sections_for_query(
        sections, "channel add", set(ds._STOP_WORDS) | {"channels"},
    )
    assert sorted(s.title for s in with_plural_only) == sorted(commands)

    ranked = rank_sections_for_query(sections, "channel add", ds._stop_words_for("[cli]cremind channels"))
    assert [s.title for s in ranked] == ["`cremind channels add`"]


@pytest.mark.parametrize("name, expected", [
    ("[cli]cremind channels", {"cremind", "channels", "channel"}),
    ("[cli]cremind profile", {"cremind", "profile", "profiles"}),
    ("widgets guide", {"widgets", "widget", "guide", "guides"}),
])
def test_stop_words_for_adds_both_forms_of_each_name_word(name, expected):
    words = ds._stop_words_for(name)
    assert expected <= words
    assert set(ds._STOP_WORDS) <= words
    # The tag is not a word of the name.
    assert "[cli]cremind" not in words and "cli" not in words


def test_hyphenated_doc_name_typed_as_is_is_a_stop_word(monkeypatch):
    body = _cli_doc("skill-events", ("list", "pause", "resume"))
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch, exec_shell=False)
    _patch_service(monkeypatch, name="[cli]cremind skill-events", body=body)

    text = _text(_search("skill-events pause"))

    assert "MARK-PAUSE" in text
    assert "MARK-LIST" not in text and "MARK-RESUME" not in text


def test_hyphenated_doc_name_spelled_as_words_does_not_match_every_heading(monkeypatch):
    """Regression: the doc name was split on whitespace only, so for
    '[cli]cremind skill-events' a query saying "skill events" kept "skill",
    which prefix-matches "skill-events" in EVERY heading."""
    body = _cli_doc("skill-events", ("list", "pause", "resume"))
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch, exec_shell=False)
    _patch_service(monkeypatch, name="[cli]cremind skill-events", body=body)

    text = _text(_search("skill events pause"))

    assert "MARK-PAUSE" in text
    assert "MARK-LIST" not in text and "MARK-RESUME" not in text


# ── Envelope layout under pressure ──────────────────────────────────────────


def test_a_matched_section_over_the_budget_is_skipped_never_cut(monkeypatch):
    body = _long_body(add_paras=16)
    _, _, sections = split_sections(body)
    lines = body.splitlines()
    add = next(s for s in sections if s.title == "`cremind widgets add`")
    assert _quarter("\n".join(lines[add.start:add.end])) > 1400
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    text = _text(_search("how do I add a widget"))

    assert "MARK-ADD" not in text
    assert _quarter(text) <= 1400
    # The footer still offers the matching heading as the one to read.
    assert 'e.g. section="cremind widgets add"' in _footer(text)


def test_a_matched_but_oversized_section_is_not_reported_as_no_match(monkeypatch):
    """Regression: with no section INCLUDED the envelope said nothing matched,
    even when a heading matched and was skipped only for size — contradicting
    its own footer, which suggested that very section."""
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=_long_body(add_paras=16))

    text = _text(_search("how do I add a widget"))

    assert _NO_MATCH_LINE not in text


def test_a_long_head_is_cut_at_a_line_and_marked(monkeypatch):
    intro_lines = [f"HEAD-LINE-{i:02d} " + _para(20, i) for i in range(40)]
    body = "\n".join(["# cremind widgets", "", *intro_lines, "", _long_body().split("\n", 4)[4]])
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    text = _text(_search("how do I add a widget"))

    assert "HEAD-LINE-00" in text
    assert "HEAD-LINE-39" not in text
    assert "[… head truncated" in text
    head = text.split("\n\n", 1)[1].split("[… head truncated", 1)[0]
    assert _quarter(head.strip("\n")) <= int(1400 * 0.35)
    assert "MARK-ADD" in text
    assert _quarter(text) <= 1400


def test_an_oversized_toc_falls_back_to_h2_entries_with_a_note(monkeypatch):
    parts = ["# cremind widgets", "", INTRO, ""]
    for h2 in range(3):
        parts += [f"## Part {h2}", "", _para(10, h2), ""]
        for h3 in range(15):
            parts += [f"### Subsection {h2}-{h3:02d} about lorem ipsum dolor", "", _para(20, h3), ""]
    body = "\n".join(parts)
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)
    assert _quarter("\n".join(_expected_toc_lines(body))) > int(1400 * 0.25)

    text = _text(_search("zebra"))

    toc = _toc_block(text).splitlines()
    assert [line for line in toc if line.startswith("- ")] == [
        line for line in _expected_toc_lines(body) if line.startswith("- Part ")
    ]
    assert not any("Subsection" in line for line in toc)
    assert toc[-1] == "(45 subsections not listed — read an H2 section to see its subsections.)"
    assert _quarter(text) <= 1400


def test_a_flat_oversized_toc_is_cut_to_fit_and_counts_what_it_dropped(monkeypatch):
    parts = ["# cremind widgets", "", INTRO, ""]
    for i in range(60):
        parts += [f"## Topic number {i:02d} about lorem ipsum dolor sit", "", _para(12, i), ""]
    body = "\n".join(parts)
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    text = _text(_search("zebra"))

    block = _toc_block(text).splitlines()
    entries, note = block[:-1], block[-1]
    assert 0 < len(entries) < 60
    assert entries == _expected_toc_lines(body)[: len(entries)]
    assert note == f"(table of contents cut to fit: {60 - len(entries)} more entries not listed.)"
    assert _quarter("\n".join(entries)) <= int(1400 * 0.25)
    assert _quarter(text) <= 1400


def test_a_collapsed_toc_that_still_overflows_keeps_both_notes(monkeypatch):
    """Regression: collapsing to ## entries and THEN cutting overwrote the
    'subsections not listed' note, so the agent lost the hint that each ##
    hides its subsections."""
    parts = ["# cremind widgets", "", INTRO, ""]
    for h2 in range(60):
        parts += [f"## Topic number {h2:02d} about lorem ipsum dolor sit", "", _para(6, h2), ""]
        for h3 in range(2):
            parts += [f"### Detail {h2:02d}-{h3}", "", _para(6, h3), ""]
    body = "\n".join(parts)
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    text = _text(_search("zebra"))

    note = _toc_block(text).splitlines()[-1]
    assert "(120 subsections not listed" in note
    assert "(table of contents cut to fit:" in note
    assert _quarter(text) <= 1400


def test_matches_that_do_not_fit_are_named_instead_of_dropped(monkeypatch):
    """A second matching section too big for what is left is not silently
    lost: the envelope names it, with its size, so the agent can read it."""
    body = "\n".join([
        "# cremind widgets", "", INTRO, "",
        "## Commands", "",
        "### `cremind widgets add`", "", _para(20, 1), "",
        "### `cremind widgets remove`", "", _para(900, 2), "",
    ])
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=body)

    text = _text(_search("add or remove a widget"))

    assert "### `cremind widgets add`" in text
    assert "### `cremind widgets remove`" not in text
    assert "(Also matching, not shown here: cremind widgets remove (" in text
    assert _NO_MATCH_LINE not in text


def test_the_clamp_is_read_from_the_tool_result_group_only(monkeypatch):
    """Two settings, two config reads — not the whole agent config, whose
    every field is a synchronous config-store query and whose unrelated
    settings must not be able to break delivery."""
    calls: List[tuple] = []

    def _group(name, profile):
        calls.append((name, profile))
        return {"enabled": True, "max_tokens": 2500}

    monkeypatch.setattr(ds, "resolve_group", _group)

    cfg = ds._resolve_clamp("bob")

    assert calls == [("tool_result", "bob")]
    assert (cfg.tool_result_enabled, cfg.tool_result_max_tokens) == (True, 2500)


def test_fit_lines_closes_a_code_fence_it_cut_open():
    text = "\n".join(["Intro.", "```bash"] + [f"cremind widgets run {i}" for i in range(400)] + ["```"])

    fitted, cut = ds._fit_lines(text, 200)

    assert cut
    assert fitted.splitlines()[-1] == "```", "whatever follows must not read as code"
    assert fitted.count("```") == 2


def test_fit_lines_cuts_inside_a_line_rather_than_deliver_nothing():
    blob = "word " * 3000

    fitted, cut = ds._fit_lines(blob, 300)

    assert cut and fitted.startswith("word word word") and fitted.endswith("…")
    assert _quarter(fitted) <= 300


def test_fit_lines_leaves_a_toc_line_whole():
    toc = "\n".join(f"- `cremind widgets verb{i:03d}` ({i})" for i in range(300))

    fitted, _ = ds._fit_lines(toc, 150, split_long_line=False)

    assert all(line in toc.splitlines() for line in fitted.splitlines())


# ── 11. Judge prompt: descriptions capped like the embedder's ───────────────


def _rendered_descriptions(prompt: str) -> List[str]:
    prefix = "    description: "
    return [line[len(prefix):] for line in prompt.splitlines() if line.startswith(prefix)]


def test_judge_prompt_caps_a_long_description_and_keeps_a_short_one():
    long_desc = "alpha beta gamma " * 150  # 2550 chars
    short_desc = "How to add a widget."
    prompt = ds._format_judge_prompt(query="add a widget", candidates=[
        {"name": "long doc", "description": long_desc},
        {"name": "short doc", "description": short_desc},
    ])

    long_rendered, short_rendered = _rendered_descriptions(prompt)
    assert long_rendered.endswith("…")
    assert len(long_rendered) - 1 <= DESCRIPTION_MAX_CHARS
    assert long_rendered[:-1] == long_desc[:DESCRIPTION_MAX_CHARS].rstrip()
    assert short_rendered == short_desc
    assert "[0] name: long doc" in prompt and "[1] name: short doc" in prompt


@pytest.mark.parametrize("length, capped", [
    (DESCRIPTION_MAX_CHARS, False),
    (DESCRIPTION_MAX_CHARS + 1, True),
])
def test_judge_description_cap_boundary(length, capped):
    desc = "x" * length
    rendered = ds._judge_description(desc)
    if capped:
        assert rendered == "x" * DESCRIPTION_MAX_CHARS + "…"
    else:
        assert rendered == desc


def test_the_judge_never_receives_more_than_the_capped_description(monkeypatch):
    long_desc = "widget " * 400  # 2800 chars
    _patch_config(monkeypatch, max_tokens=4000)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="widgets guide", body=_short_body(), description=long_desc)
    llm = _FakeLLM()

    _search("how do I add a widget", llm=llm)

    user_prompt = llm.calls[0]["messages"][1]["content"]
    assert long_desc.strip() not in user_prompt
    (rendered,) = _rendered_descriptions(user_prompt)
    assert rendered == long_desc[:DESCRIPTION_MAX_CHARS].rstrip() + "…"


# ── 12. Drift guards ────────────────────────────────────────────────────────


def test_leaf_function_names_match_make_leaf_name():
    from app.tools.base import make_leaf_name

    assert make_leaf_name("documentation_search", ds.SECTION_LEAF_NAME) == ds.SECTION_LEAF_FN
    assert make_leaf_name("documentation_search", "search_documentation") == ds.SEARCH_LEAF_FN


def test_the_group_exposes_both_leaves_under_the_pinned_names():
    assert ds.TOOL_CONFIG["name"] == "documentation_search"
    assert [tool.name for tool in ds.get_tools({})] == [ds.SEARCH_LEAF_NAME, ds.SECTION_LEAF_NAME]
    # The search leaf's static description must NOT name the reader: a profile
    # can disable that leaf, and a model told to call it would get "Unknown
    # tool". The envelope footer names it — only when it is enabled.
    assert f"`{ds.SECTION_LEAF_FN}`" not in ds.DocumentationSearchTool.description


def test_a_small_clamp_keeps_the_whole_cli_result_under_it(monkeypatch):
    """Regression: a 1,200-token floor on the BODY, plus the directive in
    front of it, pushed a CLI reference's result past clamps up to ~1,600 —
    so the agent's own clamp cut the footer that says how to read on."""
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)  # exec_shell enabled: the directive applies
    _patch_service(monkeypatch, name="[cli]cremind widgets", body=_long_body())

    text = _text(_search("how do I add a widget"))

    assert text.startswith("[Agent directive")
    assert _quarter(text) <= 1500
    assert "[End of excerpt" in text


def test_without_tiktoken_the_body_is_delivered_whole(monkeypatch):
    """The agent's clamp is truncate_to_tokens, a no-op when tiktoken is not
    installed; enveloping then would deliver LESS than the agent would get."""
    monkeypatch.setattr(ds, "_agent_clamp_is_active", lambda: False)
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    body = _long_body()
    _patch_service(monkeypatch, name="widgets guide", body=body)

    assert ds._delivery_budget("admin", "") is None
    assert _text(_search("how do I add a widget")) == body


def test_a_headingless_doc_over_budget_is_delivered_as_its_beginning(monkeypatch):
    """Regression: it used to get the head/TOC layout — a third of the budget,
    an empty table of contents, and instructions to pick a heading from it."""
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    body = "# Notes\n\n" + "\n\n".join(_para(60, i) for i in range(40))
    _patch_service(monkeypatch, name="widgets notes", body=body)

    text = _text(_search("anything"))

    assert "without sections, so only its beginning is shown" in text
    assert _TOC_HEADING not in text and "[End of excerpt" not in text
    assert text.rstrip().endswith("[… document truncated]")
    assert 1200 < _quarter(text) <= 1400, "most of the budget carries document text"


def test_query_words_that_restate_the_doc_name_do_not_match_every_heading(monkeypatch):
    """Regression: stop words are exact but ranking prefix-matches, so
    "conversation" hit every `cremind conv …` heading through "conv"."""
    verbs = ("list", "new", "delete", "rename")
    parts = ["# cremind conv", "", INTRO, "", "## Subcommands", ""]
    for verb in verbs:
        parts += [f"### `cremind conv {verb}`", "", f"MARK-{verb.upper()}", _para(300, 1), ""]
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    _patch_service(monkeypatch, name="[cli]cremind conv", body="\n".join(parts))

    text = _text(_search("how do I delete a conversation"))

    assert "MARK-DELETE" in text
    for other in ("MARK-LIST", "MARK-NEW", "MARK-RENAME"):
        assert other not in text


def test_the_footer_names_a_shared_name_by_its_qualified_reference(monkeypatch):
    """When the chosen document's name is shared with another, the footer must
    lead the reader back to THIS one, not to whichever the name finds first."""
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    svc = _patch_service(monkeypatch, name="guide", body=_long_body(), scope="admin")
    svc.hits[0]["relpath"] = "guide.md"
    svc.reference_for = lambda *, scope, relpath, scopes: f"{scope}/{relpath}"

    footer = _footer(_text(_search("how do I add a widget")))

    assert 'document="admin/guide.md"' in footer


def test_a_cut_head_can_be_read_whole_as_the_introduction(monkeypatch):
    _patch_config(monkeypatch, max_tokens=1500)
    _patch_registry(monkeypatch)
    head = "\n".join(f"HEAD-LINE-{i:02d} " + _para(12, i) for i in range(40))
    body = f"# cremind widgets\n\n{head}\n\n## Commands\n\n{_para(1200, 9)}\n"
    _patch_service(monkeypatch, name="widgets guide", body=body)
    assert _quarter(body) > 1400 and _quarter(head) > int(1400 * 0.35)

    text = _text(_search("zebra"))

    assert '[… head truncated — read all of it with section="Introduction"]' in text
