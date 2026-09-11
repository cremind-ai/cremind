"""The ``read_documentation_section`` leaf of Documentation Search.

``run_read_section`` returns one section of a document looked up BY NAME in the
caller's own ``[shared, profile]`` scopes — no embedding, no judge. These pin:

- the happy path and its header (``[Section "<title>" of "<doc>" (<scope>) —
  N tokens]``), with the CLI directive in front of a ``[cli]`` doc only when
  exec_shell is callable;
- the no-``section`` overview: whole document when it fits the delivery
  budget, else its table of contents (or, with no sections, its beginning);
- the structured errors the agent recovers from (DocumentNotFound,
  SectionNotFound, AmbiguousSection, MissingDocument, ServiceUnavailable);
- oversized sections (children listed / beginning shown), ``$VAR`` resolution
  for the calling profile, profile isolation, and dispatch through the real
  built-in adapter (which is what injects ``_profile``).

Sizes are made deterministic by patching ``ds._tokens`` to chars/4 — the
module sizes sections with the same function, so the two always agree.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

import app.tools.builtin.documentation_search as ds
from app.documents.sections import section_text, split_sections
from app.documents.sync import DocumentSyncService
from app.tools.base import ToolResultEvent


def _tok(text: str) -> int:
    return len(text) // 4 if text else 0


# ── documents ────────────────────────────────────────────────────────────────

_WIDGETS = """\
# cremind widgets

Manage widgets from the command line.

## Commands

Every widget command talks to the running server.

### `cremind widgets list`

List every widget.

```bash
cremind widgets list
## not a heading: inside a fence
```

### `cremind widgets add`

Add a widget.

- `--name` the widget's name
- `--color` the widget's color

### `cremind widgets groups add`

Add a widget group.

### `cremind widgets remove`

Remove a widget.

## Examples

Example text.
"""


def _filler(tag: str, n: int) -> str:
    return "\n".join(f"{tag} filler line {i:03d} " + "." * 60 for i in range(n))


# Long enough that the whole body (and ``## Commands``, inclusive of its three
# children) is over a 1200-token budget, while each child fits on its own.
_GIZMO = "\n".join([
    "# gizmo guide",
    "",
    "Gizmos, explained.",
    "",
    "## Commands",
    "",
    "The gizmo commands.",
    "",
    "### `cremind gizmo init`",
    "",
    _filler("INIT", 20),
    "",
    "### `cremind gizmo sync`",
    "",
    _filler("SYNC", 20),
    "",
    "### `cremind gizmo drop`",
    "",
    _filler("DROP", 20),
    "",
    "## Appendix",
    "",
    _filler("APPX", 70),
    "",
])


def _write(directory: Path, stem: str, body: str, description: str = "A document") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stem}.md"
    path.write_text(f'---\ndescription: "{description}"\n---\n\n{body}', encoding="utf-8")
    return path


def _section(body: str, title: str) -> str:
    """``section_text`` of the section titled ``title`` in ``body``."""
    lines, _, sections = split_sections(body)
    return section_text(lines, next(s for s in sections if s.title == title))


# ── patching helpers ─────────────────────────────────────────────────────────


def _set_clamp(monkeypatch, max_tokens: int = 4000, *, enabled: bool = True,
               calls: Optional[List[str]] = None) -> None:
    def _cfg(profile):
        if calls is not None:
            calls.append(profile)
        return SimpleNamespace(tool_result_enabled=enabled, tool_result_max_tokens=max_tokens)

    monkeypatch.setattr(ds, "_resolve_clamp", _cfg)


def _patch_registry(monkeypatch, *, leaves: Optional[List[Dict[str, Any]]] = None,
                    raises: Optional[Exception] = None) -> None:
    """Patch app.tools.registry.get_tool_registry (imported lazily by the module)."""
    def _factory():
        if raises is not None:
            raise raises
        return SimpleNamespace(leaves_for_profile=lambda profile, tool_id: {"leaves": leaves or []})

    monkeypatch.setattr("app.tools.registry.get_tool_registry", _factory)


_EXEC_SHELL_ON = [{"leaf_name": "exec_shell", "enabled": True}]
_EXEC_SHELL_OFF = [{"leaf_name": "exec_shell", "enabled": False}]


@pytest.fixture(autouse=True)
def _deterministic(monkeypatch):
    monkeypatch.setattr(ds, "_tokens", _tok)
    monkeypatch.setattr(ds, "resolve_system_var_tokens", lambda text, profile: text)
    _set_clamp(monkeypatch, 4000)
    # Uninitialized registry: no directive unless a test turns exec_shell on.
    _patch_registry(monkeypatch, raises=RuntimeError("registry not initialized"))


@pytest.fixture
def svc(tmp_path, monkeypatch) -> DocumentSyncService:
    service = DocumentSyncService(working_dir=tmp_path)
    monkeypatch.setattr(ds, "get_service", lambda: service)
    return service


@pytest.fixture
def library(tmp_path, svc) -> Path:
    """A shared corpus: the widgets CLI reference, a long guide, and a skill."""
    shared = tmp_path / "documents"
    _write(shared, "[cli]cremind widgets", _WIDGETS, "Manage widgets")
    _write(shared, "[cli]cremind gadgets", "# cremind gadgets\n\n## Commands\n\nNone.\n", "Gadgets")
    _write(shared, "gizmo guide", _GIZMO, "Gizmo guide")
    _write(shared, "sample-skill", "# sample\n\nNo sections here.\n", "A skill")
    return shared


# ── calling helpers ──────────────────────────────────────────────────────────


def _read(document: Any = None, section: Any = None, *, profile: str = "admin", omit: bool = False):
    args: Dict[str, Any] = {"_profile": profile}
    if not omit:
        args["document"] = document
    if section is not None:
        args["section"] = section
    return asyncio.run(ds.ReadDocumentationSectionTool().run(args))


def _text(result) -> str:
    assert result.structured_content is None, result.structured_content
    assert result.content and result.content[0]["type"] == "text"
    assert result.token_usage is None
    return result.content[0]["text"]


def _error(result) -> Dict[str, Any]:
    assert not result.content, "an error must not also carry text content"
    assert result.token_usage is None
    return result.structured_content


# ── 1. happy path ────────────────────────────────────────────────────────────


def test_reads_one_section_by_name(library):
    result = _read("[cli]cremind widgets", "widgets add")
    text = _text(result)

    expected = _section(_WIDGETS, "`cremind widgets add`")
    header = (
        f'[Section "`cremind widgets add`" of "[cli]cremind widgets" (shared) — '
        f"{_tok(expected)} tokens]"
    )
    assert text.startswith(header)
    assert text == f"{header}\n\n{expected}"
    assert "Add a widget." in text and "`--color`" in text
    # Stops at the next heading: neither the sibling nor anything after it.
    assert "`cremind widgets groups add`" not in text
    assert "Add a widget group." not in text
    assert "## Examples" not in text


@pytest.mark.parametrize(
    "section",
    ["`cremind widgets add`", "cremind widgets add", "### `cremind widgets add`",
     "WIDGETS ADD", "widgets add:"],
)
def test_section_names_are_forgiving(library, section):
    text = _text(_read("[cli]cremind widgets", section))
    assert text.startswith('[Section "`cremind widgets add`" of "[cli]cremind widgets"')


@pytest.mark.parametrize("document", ["cremind widgets", "[CLI]Cremind Widgets", "[cli]cremind widgets.md"])
def test_the_header_names_the_resolved_document(library, document):
    text = _text(_read(document, "widgets add"))
    assert text.startswith('[Section "`cremind widgets add`" of "[cli]cremind widgets" (shared)')


def test_an_h2_section_includes_its_children(library):
    text = _text(_read("[cli]cremind widgets", "Commands"))
    assert text.endswith(_section(_WIDGETS, "Commands"))
    for child in ("`cremind widgets list`", "`cremind widgets add`", "`cremind widgets remove`"):
        assert f"### {child}" in text
    assert "## Examples" not in text


def test_a_heading_inside_a_code_fence_is_not_a_section(library):
    err = _error(_read("[cli]cremind widgets", "not a heading: inside a fence"))
    assert err["error"] == "SectionNotFound"
    text = _text(_read("[cli]cremind widgets", "widgets list"))
    assert "## not a heading: inside a fence" in text


def test_duplicate_headings_serve_the_first_with_a_note(tmp_path, svc):
    body = "# notes\n\n## Notes\n\nfirst copy\n\n## Other\n\nx\n\n## Notes\n\nsecond copy\n"
    _write(tmp_path / "documents", "notes doc", body)

    text = _text(_read("notes doc", "notes"))

    assert text.startswith('[Section "Notes" of "notes doc" (shared) — ')
    assert "[note: 2 sections share this heading; showing the first]" in text
    assert "first copy" in text
    assert "second copy" not in text


# ── 2. CLI directive ─────────────────────────────────────────────────────────


def test_cli_doc_gets_the_directive_first_when_exec_shell_is_callable(library, monkeypatch):
    _patch_registry(monkeypatch, leaves=_EXEC_SHELL_ON)

    text = _text(_read("[cli]cremind widgets", "widgets add"))

    directive = ds._CLI_EXECUTION_DIRECTIVE.format(fn="exec_shell") + "\n"
    assert text.startswith("[Agent directive")
    assert text.startswith(directive)
    assert text[len(directive):].startswith('[Section "`cremind widgets add`"')


def test_cli_doc_overview_also_gets_the_directive(library, monkeypatch):
    _patch_registry(monkeypatch, leaves=_EXEC_SHELL_ON)

    text = _text(_read("[cli]cremind widgets"))

    directive = ds._CLI_EXECUTION_DIRECTIVE.format(fn="exec_shell") + "\n"
    assert text.startswith(directive)
    assert text[len(directive):].startswith('[Document "[cli]cremind widgets" (shared)')


@pytest.mark.parametrize(
    "registry",
    [
        {"leaves": _EXEC_SHELL_OFF},
        {"leaves": []},
        {"raises": RuntimeError("registry not initialized")},
    ],
    ids=["leaf-disabled", "group-without-leaf", "no-registry"],
)
def test_cli_doc_without_a_callable_exec_shell_has_no_directive(library, monkeypatch, registry):
    _patch_registry(monkeypatch, **registry)

    text = _text(_read("[cli]cremind widgets", "widgets add"))

    assert text.startswith('[Section "`cremind widgets add`"')
    assert "Agent directive" not in text


def test_a_non_cli_doc_never_gets_the_directive(library, monkeypatch):
    _patch_registry(monkeypatch, leaves=_EXEC_SHELL_ON)

    text = _text(_read("gizmo guide", "gizmo init"))

    assert text.startswith('[Section "`cremind gizmo init`" of "gizmo guide" (shared)')
    assert "Agent directive" not in text


def test_the_directive_is_reserved_out_of_the_section_budget(tmp_path, svc, monkeypatch):
    # The budget is clamp - measured directive - 100 (floored at 1200), so a
    # section that fits WITHOUT the directive can be over budget WITH it.
    _patch_registry(monkeypatch, leaves=_EXEC_SHELL_ON)
    directive = ds._directive_for("[cli]cremind big", "admin")
    reserved = _tok(directive)
    assert reserved > 0
    clamp = 1200 + reserved + 100 + 400
    budget_with, budget_without = clamp - reserved - 100, clamp - 100
    _set_clamp(monkeypatch, clamp)

    lines = ["# cremind big", "", "## Dump", ""]
    i = 0
    while _tok("\n".join(lines[2:])) < budget_with + reserved // 2:
        lines.append(f"dump line {i:04d} " + "-" * 60)
        i += 1
    body = "\n".join(lines) + "\n"
    _write(tmp_path / "documents", "[cli]cremind big", body)
    total = _tok(_section(body, "Dump"))
    assert budget_with < total <= budget_without  # precondition of the test

    over = _text(_read("[cli]cremind big", "dump"))
    assert over.startswith(directive)
    assert f"{total} tokens, over the {budget_with}-token budget" in over

    _patch_registry(monkeypatch, leaves=_EXEC_SHELL_OFF)
    whole = _text(_read("[cli]cremind big", "dump"))
    assert whole == f'[Section "Dump" of "[cli]cremind big" (shared) — {total} tokens]\n\n{_section(body, "Dump")}'


# ── 3. no section: whole document / table of contents / beginning ────────────


def test_no_section_on_a_short_doc_returns_the_whole_document(library):
    text = _text(_read("[cli]cremind widgets"))
    assert text == (
        f'[Document "[cli]cremind widgets" (shared) — {_tok(_WIDGETS)} tokens, complete]'
        f"\n\n{_WIDGETS}"
    )


@pytest.mark.parametrize("section", ["", "   "])
def test_a_blank_section_means_no_section(library, section):
    text = _text(_read("[cli]cremind widgets", section))
    assert text.startswith('[Document "[cli]cremind widgets" (shared) — ')
    assert text.endswith(_WIDGETS)


def test_no_section_on_a_long_doc_returns_the_table_of_contents(library, monkeypatch):
    _set_clamp(monkeypatch, 1300)  # budget = max(1200, 1300 - 0 - 100) = 1200
    assert _tok(_GIZMO) > 1200

    text = _text(_read("gizmo guide"))

    assert text.startswith(
        f'[Table of contents of "gizmo guide" (shared) — {_tok(_GIZMO)} tokens in total.'
    )
    assert f"`{ds.SECTION_LEAF_FN}`" in text
    lines, _, sections = split_sections(_GIZMO)
    toc_lines = text.splitlines()[2:]
    assert toc_lines == [
        f"{'  ' * (s.level - 2)}- {s.title} ({_tok(section_text(lines, s))})" for s in sections
    ]
    assert [s.title for s in sections] == [
        "Commands", "`cremind gizmo init`", "`cremind gizmo sync`", "`cremind gizmo drop`", "Appendix",
    ]
    # Only the outline — no section bodies.
    assert "filler line" not in text
    assert _tok(text) <= 1200


def test_no_section_on_a_long_doc_without_sections_returns_its_beginning(tmp_path, svc, monkeypatch):
    _set_clamp(monkeypatch, 1300)
    body = "# flat\n\n" + _filler("FLAT", 100) + "\n"
    assert _tok(body) > 1200
    _write(tmp_path / "documents", "flat doc", body)

    text = _text(_read("flat doc"))

    assert text.startswith(
        f'[Document "flat doc" (shared) — {_tok(body)} tokens, over the 1200-token budget '
        "and without sections, so only its beginning is shown]"
    )
    assert text.endswith("\n[… document truncated]")
    assert "FLAT filler line 000" in text
    assert "FLAT filler line 099" not in text
    assert _tok(text) <= 1200


def test_with_the_clamp_off_the_whole_long_document_is_returned(library, monkeypatch):
    _set_clamp(monkeypatch, 1300, enabled=False)

    text = _text(_read("gizmo guide"))
    assert text == f'[Document "gizmo guide" (shared) — {_tok(_GIZMO)} tokens, complete]\n\n{_GIZMO}'

    # ...and an oversized section comes back whole too.
    appendix = _text(_read("gizmo guide", "appendix"))
    assert appendix.endswith(_section(_GIZMO, "Appendix"))
    assert "over the" not in appendix


# ── 4. unknown document ──────────────────────────────────────────────────────


def test_unknown_document_suggests_close_names(library):
    err = _error(_read("cremind widget", "widgets add"))

    assert err["error"] == "DocumentNotFound"
    assert err["document"] == "cremind widget"
    assert err["message"]
    assert err["candidates"], "a near miss must come with suggestions"
    assert err["candidates"][0] == "[cli]cremind widgets"


def test_unknown_document_with_no_close_name_lists_the_library(library):
    err = _error(_read("zzzzqqqq"))

    assert err["error"] == "DocumentNotFound"
    assert set(err["candidates"]) == {
        "[cli]cremind widgets", "[cli]cremind gadgets", "gizmo guide", "sample-skill",
    }


def test_a_document_that_vanished_after_lookup_is_not_found(monkeypatch):
    class _Svc:
        def find_document(self, name, scopes):
            return {"name": "ghost", "scope": "shared", "file_path": "/nowhere/ghost.md"}

        def list_document_names(self, scopes):
            raise AssertionError("not needed once the name resolved")

        def read_body(self, path):
            return None

    monkeypatch.setattr(ds, "get_service", lambda: _Svc())

    err = _error(_read("ghost", "anything"))

    assert err["error"] == "DocumentNotFound"
    assert err["document"] == "ghost"
    assert err["candidates"] == []


# ── 5. unknown section ───────────────────────────────────────────────────────


def test_unknown_section_suggests_the_best_match_first(library):
    err = _error(_read("[cli]cremind widgets", "widgets ad"))

    assert err["error"] == "SectionNotFound"
    assert err["document"] == "[cli]cremind widgets"
    assert err["section"] == "widgets ad"
    assert err["candidates"], "a typo must come with suggestions"
    assert err["candidates"][0] == "`cremind widgets add`"


def test_a_section_asked_of_a_doc_without_sections(library):
    err = _error(_read("sample-skill", "usage"))

    assert err["error"] == "SectionNotFound"
    assert err["document"] == "sample-skill"
    assert err["candidates"] == []


def test_the_introduction_of_a_doc_without_sections_is_its_whole_text(library):
    text = _text(_read("sample-skill", "Introduction"))

    assert text.startswith('[Introduction of "sample-skill"')
    assert "No sections here." in text


# ── 6. ambiguous section ─────────────────────────────────────────────────────


def test_an_ambiguous_section_lists_every_match(library):
    err = _error(_read("[cli]cremind widgets", "add"))

    assert err["error"] == "AmbiguousSection"
    assert err["document"] == "[cli]cremind widgets"
    assert err["section"] == "add"
    assert sorted(err["candidates"]) == ["`cremind widgets add`", "`cremind widgets groups add`"]


# ── 7. oversized section with children ───────────────────────────────────────


def test_an_oversized_section_with_children_lists_its_subsections(library, monkeypatch):
    _set_clamp(monkeypatch, 1300)  # budget 1200
    lines, _, sections = split_sections(_GIZMO)
    commands = next(s for s in sections if s.title == "Commands")
    total = _tok(section_text(lines, commands))
    assert total > 1200
    kids = [s for s in sections if s.level == 3]
    for kid in kids:
        assert _tok(section_text(lines, kid)) <= 1200

    text = _text(_read("gizmo guide", "commands"))

    assert text.startswith(
        f'[Section "Commands" of "gizmo guide" (shared) — {total} tokens, over the '
        "1200-token budget. Showing its own text and a list of its subsections;"
    )
    assert f"`{ds.SECTION_LEAF_FN}`" in text
    assert "The gizmo commands." in text
    assert "## Subsections (section → tokens when read)" in text
    for kid in kids:
        assert f"  - {kid.title} ({_tok(section_text(lines, kid))})" in text.splitlines()
    # Only the section's own text, never its children's bodies or a sibling.
    assert "filler line" not in text
    assert "Appendix" not in text
    assert _tok(text) <= 1200


def test_a_child_of_an_oversized_section_reads_whole(library, monkeypatch):
    _set_clamp(monkeypatch, 1300)

    text = _text(_read("gizmo guide", "gizmo sync"))

    expected = _section(_GIZMO, "`cremind gizmo sync`")
    assert text == f'[Section "`cremind gizmo sync`" of "gizmo guide" (shared) — {_tok(expected)} tokens]\n\n{expected}'


# ── 8. oversized section without children ────────────────────────────────────


def test_an_oversized_section_without_children_shows_its_beginning(library, monkeypatch):
    _set_clamp(monkeypatch, 1300)
    total = _tok(_section(_GIZMO, "Appendix"))
    assert total > 1200

    text = _text(_read("gizmo guide", "appendix"))

    assert text.startswith(
        f'[Section "Appendix" of "gizmo guide" (shared) — {total} tokens, over the '
        "1200-token budget and without subsections, so only its beginning is shown]"
    )
    assert "\n[… section truncated" in text
    assert "## Appendix" in text
    assert "APPX filler line 000" in text
    assert "APPX filler line 069" not in text
    assert _tok(text) <= 1200


def test_a_long_single_line_doc_still_shows_its_beginning(tmp_path, svc, monkeypatch):
    """Regression: whole-line fitting stopped at the first line that did not
    fit, so a heading-less one-line doc was 'shown from its beginning' with no
    content at all."""
    _set_clamp(monkeypatch, 1300)
    body = "word " * 2000 + "\n"  # one line, ~2500 tokens, no headings
    _write(tmp_path / "documents", "blob doc", body)

    text = _text(_read("blob doc"))

    assert "only its beginning is shown" in text
    assert "word word word" in text


def test_an_oversized_section_whose_paragraph_is_one_line_still_shows_its_beginning(tmp_path, svc, monkeypatch):
    """Regression: an oversized childless section whose paragraph is one long
    line came back as just its heading."""
    _set_clamp(monkeypatch, 1300)
    body = "# big\n\n## Blob\n\n" + "word " * 2000 + "\n"
    _write(tmp_path / "documents", "blob section doc", body)

    text = _text(_read("blob section doc", "blob"))

    assert "only its beginning is shown" in text
    assert "word word word" in text


# ── 9. $VAR resolution ───────────────────────────────────────────────────────

_PROFILE_GUIDE = """\
# profile guide

## Where your files live

Files for $CREMIND_PROFILE live under `~/.cremind/$CREMIND_PROFILE/`.

## Notes for $CREMIND_PROFILE

Only for you.
"""


def test_var_tokens_resolve_for_the_calling_profile(tmp_path, svc, monkeypatch):
    seen: List[str] = []

    def _resolve(text, profile):
        seen.append(profile)
        return text.replace("$CREMIND_PROFILE", profile)

    monkeypatch.setattr(ds, "resolve_system_var_tokens", _resolve)
    _write(tmp_path / "documents", "profile guide", _PROFILE_GUIDE)

    text = _text(_read("profile guide", "where your files live", profile="bob"))

    assert "Files for bob live under `~/.cremind/bob/`." in text
    assert "$CREMIND_PROFILE" not in text
    assert seen and set(seen) == {"bob"}
    # Sized on the resolved text — what the agent actually receives.
    resolved = _section(_PROFILE_GUIDE.replace("$CREMIND_PROFILE", "bob"), "Where your files live")
    assert text.startswith(f'[Section "Where your files live" of "profile guide" (shared) — {_tok(resolved)} tokens]')


def test_var_tokens_resolve_before_headings_are_matched(tmp_path, svc, monkeypatch):
    monkeypatch.setattr(
        ds, "resolve_system_var_tokens", lambda text, profile: text.replace("$CREMIND_PROFILE", profile),
    )
    _write(tmp_path / "documents", "profile guide", _PROFILE_GUIDE)

    text = _text(_read("profile guide", "notes for bob", profile="bob"))
    assert text.startswith('[Section "Notes for bob" of "profile guide" (shared)')

    # Another profile sees its own resolution, not bob's.
    err = _error(_read("profile guide", "notes for bob", profile="admin"))
    assert err["error"] == "SectionNotFound"
    assert "Notes for admin" in err["candidates"]


# ── 10. missing document / service unavailable ───────────────────────────────


@pytest.mark.parametrize("document", ["", "   ", None, "<omitted>"])
def test_a_missing_document_argument_is_rejected_before_the_service(monkeypatch, document):
    def _no_service():
        raise AssertionError("the service must not be consulted without a document")

    monkeypatch.setattr(ds, "get_service", _no_service)

    if document == "<omitted>":
        err = _error(_read(omit=True))
    else:
        err = _error(_read(document, "anything"))

    assert err["error"] == "MissingDocument"
    assert err["message"]


def test_an_uninitialized_service_is_reported(monkeypatch):
    monkeypatch.setattr(ds, "get_service", lambda: None)

    err = _error(_read("[cli]cremind widgets", "widgets add"))

    assert err["error"] == "ServiceUnavailable"
    assert err["message"]


# ── 11. profile isolation ────────────────────────────────────────────────────


@pytest.fixture
def two_profiles(tmp_path, svc) -> Path:
    _write(tmp_path / "documents", "shared guide", "# shared\n\n## Hello\n\nfor everyone\n")
    admin_path = _write(
        tmp_path / "admin" / "documents", "admin notes",
        "# admin notes\n\n## Secrets\n\nadmin eyes only\n",
    )
    _write(tmp_path / "bob" / "documents", "bob notes", "# bob notes\n\n## Mine\n\nbob stuff\n")
    return admin_path


def test_a_profile_cannot_read_another_profiles_document(two_profiles):
    err = _error(_read("admin notes", "secrets", profile="bob"))

    assert err["error"] == "DocumentNotFound"
    # Suggestions come from bob's own scopes only — not even the NAME leaks.
    assert "admin notes" not in err["candidates"]
    assert set(err["candidates"]) <= {"shared guide", "bob notes"}

    text = _text(_read("admin notes", "secrets", profile="admin"))
    assert text.startswith('[Section "Secrets" of "admin notes" (admin)')
    assert "admin eyes only" in text


def test_a_path_never_reaches_another_profiles_document(two_profiles):
    admin_path = two_profiles
    for document in (
        str(admin_path),
        admin_path.as_posix(),
        "../admin/documents/admin notes",
        "..\\admin\\documents\\admin notes.md",
    ):
        err = _error(_read(document, "secrets", profile="bob"))
        assert err["error"] == "DocumentNotFound", document
        assert "admin notes" not in err["candidates"]


def test_each_profile_reads_its_own_and_the_shared_docs(two_profiles):
    assert "bob stuff" in _text(_read("bob notes", "mine", profile="bob"))
    assert "for everyone" in _text(_read("shared guide", "hello", profile="bob"))
    assert _error(_read("bob notes", "mine", profile="admin"))["error"] == "DocumentNotFound"


def test_the_budget_comes_from_the_calling_profile(library, monkeypatch):
    calls: List[str] = []
    _set_clamp(monkeypatch, 4000, calls=calls)

    _text(_read("[cli]cremind widgets", "widgets add", profile="bob"))

    assert calls and set(calls) == {"bob"}


# ── 12. dispatch through the built-in adapter ─────────────────────────────────


def _group():
    from app.tools.builtin.tool import BuiltInToolGroup

    group = BuiltInToolGroup(
        config_name=ds._TOOL_ID,
        display_name=ds.SERVER_NAME,
        description=ds.TOOL_CONFIG["description"],
        functions=ds.get_tools({}),
        llm=object(),
    )
    group.tool_id = ds._TOOL_ID
    return group


def _execute_leaf(group, *, leaf_name: str, args: Dict[str, Any], profile: str) -> ToolResultEvent:
    events: list = []

    async def _consume() -> None:
        async for ev in group.execute_leaf(
            leaf_name=leaf_name, args=args, context_id="ctx-read-section",
            profile=profile, arguments={}, variables={},
        ):
            events.append(ev)

    asyncio.run(_consume())
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results, f"no ToolResultEvent in {events!r}"
    return results[-1]


def test_the_group_exposes_both_leaves_under_the_module_constants():
    specs = _group().leaf_function_specs(context_id="c", profile="admin")
    by_name = {s.name: s for s in specs}

    assert set(by_name) == {ds.SEARCH_LEAF_FN, ds.SECTION_LEAF_FN}
    assert by_name[ds.SECTION_LEAF_FN].leaf_name == ds.SECTION_LEAF_NAME
    params = by_name[ds.SECTION_LEAF_FN].schema["function"]["parameters"]
    assert params["required"] == ["document"]
    assert set(params["properties"]) == {"document", "section"}


def test_dispatch_through_the_adapter_injects_the_profile(tmp_path, svc):
    # The doc exists ONLY in bob's scope, so it resolves only if the adapter
    # injected _profile="bob" (the leaf falls back to "admin" without it).
    _write(tmp_path / "bob" / "documents", "bob notes", "# bob notes\n\n## Mine\n\nbob stuff\n")
    group = _group()

    ok = _execute_leaf(
        group, leaf_name=ds.SECTION_LEAF_NAME,
        args={"document": "bob notes", "section": "mine"}, profile="bob",
    )
    assert '[Section "Mine" of "bob notes" (bob)' in ok.observation_text
    assert "bob stuff" in ok.observation_text

    denied = _execute_leaf(
        group, leaf_name=ds.SECTION_LEAF_NAME,
        args={"document": "bob notes", "section": "mine"}, profile="admin",
    )
    assert "DocumentNotFound" in denied.observation_text
    assert "bob stuff" not in denied.observation_text


# ── review regressions ───────────────────────────────────────────────────────


def test_an_oversized_section_caps_its_subsection_list_to_the_budget(tmp_path, svc, monkeypatch):
    """Regression: the subsection list was uncapped, so an FAQ-style section
    with hundreds of children rendered past the clamp and the agent's own
    clamp cut the list — the one thing it needed to navigate."""
    _set_clamp(monkeypatch, 1500)
    parts = ["# faq", "", "Questions and answers.", "", "## Questions", "", "Pick one."]
    for i in range(220):
        parts += ["", f"### Question number {i:03d} about widgets", "", _filler(f"q{i}", 3)]
    _write(tmp_path / "documents", "faq", "\n".join(parts))

    text = _text(_read("faq", "Questions"))

    assert _tok(text) <= 1500 - 100, "the whole result stays under the clamp"
    assert "## Subsections" in text
    assert "Question number 000 about widgets" in text
    assert "more entries not listed" in text, "the cut must be announced"


def test_a_head_the_envelope_cut_is_readable_as_the_introduction(tmp_path, svc, monkeypatch):
    """Regression: the envelope cuts the head at a third of the budget, and no
    reader call could return the rest of it."""
    _set_clamp(monkeypatch, 4000)
    head = _filler("HEAD", 60)
    body = f"# guide\n\n{head}\n\n## Setup\n\n{_filler('setup', 10)}\n"
    _write(tmp_path / "documents", "long intro guide", body)

    text = _text(_read("long intro guide", "Introduction"))

    assert text.startswith('[Introduction of "long intro guide"')
    assert "HEAD filler line 000" in text and "HEAD filler line 059" in text
    assert "setup filler line" not in text


def test_a_real_heading_named_introduction_wins_over_the_head(tmp_path, svc, monkeypatch):
    _set_clamp(monkeypatch, 4000)
    body = "# guide\n\nPREAMBLE\n\n## Introduction\n\nREAL-SECTION\n"
    _write(tmp_path / "documents", "intro guide", body)

    text = _text(_read("intro guide", "Introduction"))

    assert "REAL-SECTION" in text and "PREAMBLE" not in text


def test_an_ambiguous_document_name_lists_qualified_references(tmp_path, svc, monkeypatch):
    """Regression: the shared copy used to win silently, so an envelope built
    for the profile's copy sent the reader to a different document."""
    _set_clamp(monkeypatch, 4000)
    _write(tmp_path / "documents", "guide", "# guide\n\n## Alpha\n\nshared\n")
    _write(tmp_path / "admin" / "documents", "guide", "# guide\n\n## Rollback\n\nADMIN-COPY\n")

    err = _error(_read("guide", "Rollback"))
    assert err["error"] == "AmbiguousDocument"
    assert err["candidates"] == ["shared/guide.md", "admin/guide.md"]

    text = _text(_read("admin/guide.md", "Rollback"))
    assert "ADMIN-COPY" in text


def test_a_hyphenated_heading_is_found_with_spaces(tmp_path, svc, monkeypatch):
    _set_clamp(monkeypatch, 4000)
    body = "# cremind tools\n\n## Subcommands\n\n### `cremind tools set-args`\n\nSET-ARGS-BODY\n"
    _write(tmp_path / "documents", "[cli]cremind tools", body)

    text = _text(_read("[cli]cremind tools", "set args"))

    assert "SET-ARGS-BODY" in text
