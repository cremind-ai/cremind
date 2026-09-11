"""CLI-execution directive prepended to documentation_search results.

When ``documentation_search`` returns a ``cremind`` CLI reference (``[cli]…``)
and exec_shell is callable for the profile, the tool prepends an agent directive
telling the reasoning model to RUN the documented command via exec_shell and
answer from live output, instead of paraphrasing the man page (the original
incident: the model listed providers from the doc's example table and told the
user to run the command itself). These pin that behavior and its gating.

They also pin the leaf set the directive now travels with: ``search_documentation``
plus the ``read_documentation_section`` reader a long document's envelope points
at, each naming the other by its exposed function name, and the directive
riding in front of both leaves' results.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.constants import ChatCompletionTypeEnum

import app.tools.builtin.documentation_search as ds


# settings.toml's ``[tool_result].max_tokens`` default.
_DEFAULT_CLAMP = 4000


def _agent_config(*, enabled: bool = True, max_tokens: int = _DEFAULT_CLAMP):
    return SimpleNamespace(tool_result_enabled=enabled, tool_result_max_tokens=max_tokens)


@pytest.fixture(autouse=True)
def _hermetic_clamp(monkeypatch):
    """Resolve the delivery budget from a fixed clamp, never the dev's config DB.

    ``_resolve_clamp`` reads the per-profile ``user_config`` rows through
    SQLite; a test must not depend on what a developer set locally. Tests that
    need another clamp re-patch it.
    """
    monkeypatch.setattr(ds, "_resolve_clamp", lambda profile: _agent_config())


class _FakeLLM:
    """Minimal judge LLM that always selects candidate 0 then reports DONE."""

    def __init__(self):
        self.provider_name = "fake"
        self.model_name = "fake-mini"
        self.model_label = "fake/fake-mini"

    async def chat_completion(self, **kwargs):
        yield {
            "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
            "data": {"function": [
                {"name": "select_document", "arguments": {"index": 0}}
            ]},
        }
        yield {"type": ChatCompletionTypeEnum.DONE,
               "input_tokens": 10, "output_tokens": 1}


_BODY = "# cremind llm\n\nSome man-page body with an example table.\n"


def _patch_service(monkeypatch, *, name: str, body: str = _BODY):
    """Patch the doc-search service so the judge sees one hit with ``name``."""
    hits = [{
        "file_path": f"/docs/{name}.md", "text": "one-line description",
        "name": name, "scope": "shared", "score": 0.9,
    }]

    class _Svc:
        def search(self, *, query, profile, limit, scopes=None):
            return hits

        def read_body(self, path):
            return body

    monkeypatch.setattr(ds, "get_service", lambda: _Svc())
    monkeypatch.setattr(ds, "resolve_system_var_tokens", lambda b, profile: b)


def _patch_registry(monkeypatch, *, leaves: Optional[List[Dict[str, Any]]] = None,
                    raises: Optional[Exception] = None):
    """Patch app.tools.registry.get_tool_registry (imported lazily in _exec_shell_fn)."""
    def _factory():
        if raises is not None:
            raise raises
        registry = SimpleNamespace(
            leaves_for_profile=lambda profile, tool_id: {"leaves": leaves or []}
        )
        return registry

    monkeypatch.setattr("app.tools.registry.get_tool_registry", _factory)


def _run(name: str) -> str:
    res = asyncio.run(ds.DocumentationSearchTool().run(
        {"query": "list llm providers", "_llm": _FakeLLM(), "_profile": "admin"}
    ))
    assert res.content, "expected a text result"
    return res.content[0]["text"]


def test_cli_doc_with_exec_shell_enabled_prepends_directive(monkeypatch):
    _patch_service(monkeypatch, name="[cli]cremind llm")
    _patch_registry(monkeypatch, leaves=[{"leaf_name": "exec_shell", "enabled": True}])
    text = _run("[cli]cremind llm")
    # Directive is at the HEAD (survives head-truncation of long results).
    assert text.startswith("[Agent directive")
    assert "`exec_shell`" in text
    assert "CREMIND_SERVER" in text
    assert "EXAMPLES" in text
    # The original body still follows the directive intact.
    assert _BODY in text


def test_non_cli_doc_is_unchanged(monkeypatch):
    # Neither a [tool] doc nor a bare skill triggers the directive; the registry
    # is never consulted (prefix check short-circuits first).
    for name in ("[tool]shell executor", "sample-skill"):
        _patch_service(monkeypatch, name=name)
        _patch_registry(monkeypatch, raises=AssertionError("registry must not be used"))
        text = _run(name)
        assert text == _BODY
        assert "Agent directive" not in text


def test_cli_doc_with_exec_shell_leaf_disabled_omits_directive(monkeypatch):
    _patch_service(monkeypatch, name="[cli]cremind llm")
    _patch_registry(monkeypatch, leaves=[{"leaf_name": "exec_shell", "enabled": False}])
    text = _run("[cli]cremind llm")
    assert text == _BODY
    assert "Agent directive" not in text


def test_cli_doc_when_registry_uninitialized_omits_directive(monkeypatch):
    # Early boot / unit context: get_tool_registry raises. Degrade silently.
    _patch_service(monkeypatch, name="[cli]cremind llm")
    _patch_registry(monkeypatch, raises=RuntimeError("registry not initialized"))
    text = _run("[cli]cremind llm")
    assert text == _BODY


def test_cli_doc_group_unregistered_omits_directive(monkeypatch):
    # exec_shell not registered => leaves_for_profile raises KeyError.
    _patch_service(monkeypatch, name="[cli]cremind llm")

    def _factory():
        def _leaves(profile, tool_id):
            raise KeyError(tool_id)
        return SimpleNamespace(leaves_for_profile=_leaves)

    monkeypatch.setattr("app.tools.registry.get_tool_registry", _factory)
    text = _run("[cli]cremind llm")
    assert text == _BODY


def test_directive_function_name_matches_real_exec_shell(monkeypatch):
    # Drift guard: the name embedded in the directive is derived the same way the
    # reasoning agent derives it, and the real exec_shell module still defines a
    # run leaf named "exec_shell". A rename breaks this loudly.
    from app.tools.base import make_leaf_name
    from app.tools.builtin.exec_shell import ExecShellTool

    assert ExecShellTool.name == "exec_shell"
    expected_fn = make_leaf_name("exec_shell", ExecShellTool.name)

    _patch_service(monkeypatch, name="[cli]cremind llm")
    _patch_registry(monkeypatch, leaves=[{"leaf_name": "exec_shell", "enabled": True}])
    text = _run("[cli]cremind llm")
    assert f"`{expected_fn}`" in text


# ── --json placement ────────────────────────────────────────────────────────


def _machine_readable_bullet() -> str:
    """The directive's ``--json`` bullet (raw constant, no registry needed)."""
    bullets = [
        line for line in ds._CLI_EXECUTION_DIRECTIVE.split("\n")
        if line.startswith("- Machine-readable output")
    ]
    assert len(bullets) == 1, "the directive lost its --json placement bullet"
    return bullets[0]


def _payload_json_commands_named() -> set[str]:
    """Commands the directive names as owning a post-command ``--json`` payload.

    Every backticked item in the bullet that is neither a flag nor a full
    ``cremind ...`` invocation, i.e. the ``<group> <command>`` paths.
    """
    items = _machine_readable_bullet().split("`")[1::2]
    return {i for i in items if not i.startswith("-") and not i.startswith("cremind")}


def _cli_json_options() -> tuple[bool, set[str]]:
    """``(root has a --json flag, command paths with their own --json)``, read
    off the real Typer app (the slim CLI; no server imports)."""
    import click
    import typer

    from app.cli.main import app

    root = typer.main.get_command(app)
    root_flag = any(
        "--json" in getattr(p, "opts", []) and getattr(p, "is_flag", False)
        for p in root.params
    )
    owners: set[str] = set()

    def walk(cmd, path: list[str]) -> None:
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                walk(sub, path + [name])
            return
        if any("--json" in getattr(p, "opts", []) for p in cmd.params):
            owners.add(" ".join(path))

    walk(root, [])
    return root_flag, owners


def test_directive_teaches_root_level_json_placement(monkeypatch):
    _patch_service(monkeypatch, name="[cli]cremind llm")
    _patch_registry(monkeypatch, leaves=[{"leaf_name": "exec_shell", "enabled": True}])
    text = _run("[cli]cremind llm")
    directive = text[: text.index(_BODY)]

    assert "cremind --json <group> <command>" in directive
    assert "A trailing `--json` is rejected" in directive
    # The old advice produced exactly the trailing flag the CLI rejects.
    assert "add --json for machine-readable output" not in text.lower()


def test_directive_json_payload_commands_really_own_a_json_option():
    # Everything the directive claims about the CLI must be true of the real
    # app: --json is a ROOT flag, and each command it names as the exception
    # really has its own --json option (so a trailing one there is a payload).
    root_flag, owners = _cli_json_options()
    named = _payload_json_commands_named()

    assert root_flag, "--json is no longer a root-level flag of `cremind`"
    assert named, "the directive no longer names any payload --json command"
    assert named <= owners, f"directive names commands without --json: {named - owners}"


def test_directive_names_every_command_with_its_own_json_option():
    # The reverse direction: a command that gains its own --json must join the
    # directive's exception list, or the model is told a trailing --json there
    # is rejected and "fixes" a legitimate payload flag into the root position.
    _, owners = _cli_json_options()
    named = _payload_json_commands_named()
    assert owners <= named, f"directive omits: {sorted(owners - named)}"


# ── The leaf set ────────────────────────────────────────────────────────────


def test_leaf_set_is_search_plus_section_reader():
    tools = ds.get_tools({})
    assert {t.name for t in tools} == {"search_documentation", "read_documentation_section"}
    assert {t.name for t in tools} == {ds.SEARCH_LEAF_NAME, ds.SECTION_LEAF_NAME}
    for tool in tools:
        assert tool.description.strip(), f"{tool.name} has an empty description"


def test_section_leaf_parameters_schema():
    params = ds.ReadDocumentationSectionTool().parameters
    assert params["type"] == "object"
    assert params["required"] == ["document"]
    assert set(params["properties"]) == {"document", "section"}
    assert params["properties"]["document"]["type"] == "string"
    assert params["properties"]["section"]["type"] == "string"
    assert params["additionalProperties"] is False


def test_the_reader_names_search_but_search_does_not_name_the_reader():
    # The reader's description points back at search by its exposed name. The
    # search leaf's static description must not name the reader: a profile can
    # disable that leaf, so only the envelope footer — which checks — names it.
    assert f"`{ds.SEARCH_LEAF_FN}`" in ds.ReadDocumentationSectionTool().description
    assert f"`{ds.SECTION_LEAF_FN}`" not in ds.DocumentationSearchTool().description
    assert "closing line" in ds.DocumentationSearchTool().description


def test_exposed_function_names_match_registration():
    # SEARCH_LEAF_FN / SECTION_LEAF_FN are spelled out by hand (the builtin <->
    # registry import cycle forbids calling make_leaf_name at module load); they
    # must equal what registration exposes: tool_id = slugify(SERVER_NAME).
    from app.tools.base import make_leaf_name
    from app.tools.ids import slugify

    tool_id = slugify(ds.SERVER_NAME)
    assert tool_id == ds._TOOL_ID == ds.TOOL_CONFIG["name"]
    assert make_leaf_name(tool_id, ds.DocumentationSearchTool.name) == ds.SEARCH_LEAF_FN
    assert make_leaf_name(tool_id, ds.ReadDocumentationSectionTool.name) == ds.SECTION_LEAF_FN


def test_search_tool_class_keeps_its_leaf_name():
    # The reasoning agent imports DocumentationSearchTool as its local search
    # tool and derives the guidance's function name from this ``name``.
    assert ds.DocumentationSearchTool.name == "search_documentation"
    assert ds.ReadDocumentationSectionTool.name == "read_documentation_section"


# ── The directive in front of an envelope / a section read ─────────────────


def _chars_over_4(text: str) -> int:
    return len(text) // 4 if text else 0


def _long_cli_body() -> str:
    """A CLI reference well over a 2000-token budget, one command per H3."""
    commands = (
        "providers list", "providers configure", "models list", "device-code start",
        "model-groups get", "model-groups set", "custom add", "custom remove",
    )
    parts = ["# cremind llm", "", "Manage LLM providers, models and model groups.", ""]
    parts += ["## Commands", ""]
    for cmd in commands:
        parts += [f"### `cremind llm {cmd}`", ""]
        parts.append("MARKER_" + cmd.upper().replace(" ", "_").replace("-", "_"))
        parts += [
            f"Line {i} documenting `cremind llm {cmd}`, its flags and its output."
            for i in range(24)
        ]
        parts.append("")
    parts += ["## Troubleshooting", "", "Read logs/app.log when a command fails.", ""]
    return "\n".join(parts)


def _search(query: str) -> str:
    res = asyncio.run(ds.DocumentationSearchTool().run(
        {"query": query, "_llm": _FakeLLM(), "_profile": "admin"}
    ))
    assert res.content, "expected a text result"
    return res.content[0]["text"]


_BOTH_LEAVES_ENABLED = [
    {"leaf_name": "exec_shell", "enabled": True},
    {"leaf_name": "read_documentation_section", "enabled": True},
]


def test_long_cli_doc_envelope_keeps_the_directive_at_the_head_within_the_clamp(monkeypatch):
    monkeypatch.setattr(ds, "_tokens", _chars_over_4)
    body = _long_cli_body()
    _patch_service(monkeypatch, name="[cli]cremind llm", body=body)
    _patch_registry(monkeypatch, leaves=_BOTH_LEAVES_ENABLED)
    directive = ds._directive_for("[cli]cremind llm", "admin")
    assert directive, "exec_shell is enabled, so the directive applies"

    # A clamp that leaves exactly 2000 tokens for the body once the MEASURED
    # directive and the margin are reserved.
    clamp = ds._tokens(directive) + ds._BUDGET_MARGIN_TOKENS + 2000
    monkeypatch.setattr(ds, "_resolve_clamp", lambda p: _agent_config(max_tokens=clamp))
    assert ds._tokens(body) > 2000

    text = _search("list llm providers")

    assert text.startswith(directive), "the directive must lead, ahead of the envelope"
    envelope = text[len(directive):]
    assert envelope.startswith('[Document "[cli]cremind llm" (shared)')
    assert "within the 2000-token budget" in envelope
    # The WHOLE result, directive + envelope, survives the agent's clamp.
    assert ds._tokens(text) <= clamp
    assert "MARKER_PROVIDERS_LIST" in envelope, "the section matching the query is inlined"
    assert "MARKER_DEVICE_CODE_START" not in envelope, "an unmatched section is only listed"
    assert "`cremind llm device-code start`" in envelope, "(in the table of contents)"
    assert f"`{ds.SECTION_LEAF_FN}`" in envelope


def test_non_cli_envelope_reserves_nothing_for_a_directive(monkeypatch):
    # Contrast with the CLI case: no directive, so the whole clamp minus the
    # margin is the body's budget; the reservation is measured, not guessed.
    monkeypatch.setattr(ds, "_tokens", _chars_over_4)
    _patch_service(monkeypatch, name="[tool]llm notes", body=_long_cli_body())
    _patch_registry(monkeypatch, leaves=_BOTH_LEAVES_ENABLED)
    monkeypatch.setattr(ds, "_resolve_clamp", lambda p: _agent_config(max_tokens=2600))

    text = _search("list llm providers")

    assert text.startswith('[Document "[tool]llm notes" (shared)')
    assert "Agent directive" not in text
    assert f"within the {2600 - ds._BUDGET_MARGIN_TOKENS}-token budget" in text


def _patch_section_service(monkeypatch, *, name: str, body: str):
    """A service the section reader can resolve ``name`` against."""
    row = {
        "name": name, "scope": "shared", "relpath": f"{name}.md",
        "file_path": f"/docs/{name}.md", "description": "one-line description",
    }

    class _Svc:
        def find_document(self, document, scopes):
            return dict(row) if document == name else None

        def list_document_names(self, scopes):
            return [dict(row)]

        def read_body(self, path):
            return body

    monkeypatch.setattr(ds, "get_service", lambda: _Svc())
    monkeypatch.setattr(ds, "resolve_system_var_tokens", lambda b, profile: b)


def _read(document: str, section: Optional[str] = None) -> str:
    args: Dict[str, Any] = {"document": document, "_profile": "admin"}
    if section is not None:
        args["section"] = section
    res = asyncio.run(ds.ReadDocumentationSectionTool().run(args))
    assert res.content, f"expected a text result, got {res.structured_content!r}"
    return res.content[0]["text"]


def test_section_reader_prepends_the_directive_for_cli_docs(monkeypatch):
    _patch_section_service(monkeypatch, name="[cli]cremind llm", body=_long_cli_body())
    _patch_registry(monkeypatch, leaves=_BOTH_LEAVES_ENABLED)
    directive = ds._directive_for("[cli]cremind llm", "admin")
    assert directive

    text = _read("[cli]cremind llm", "cremind llm providers list")

    assert text.startswith("[Agent directive")
    assert text.startswith(directive)
    rest = text[len(directive):]
    assert rest.startswith('[Section "`cremind llm providers list`" of "[cli]cremind llm"')
    assert "MARKER_PROVIDERS_LIST" in rest
    assert "MARKER_PROVIDERS_CONFIGURE" not in rest

    # The no-section overview rides behind the same directive.
    overview = _read("[cli]cremind llm")
    assert overview.startswith(directive)


def test_section_reader_omits_the_directive_for_non_cli_docs(monkeypatch):
    calls: List[str] = []

    def _factory():
        calls.append("get_tool_registry")
        return SimpleNamespace(leaves_for_profile=lambda p, t: {"leaves": _BOTH_LEAVES_ENABLED})

    _patch_section_service(monkeypatch, name="[tool]shell executor", body=_long_cli_body())
    monkeypatch.setattr("app.tools.registry.get_tool_registry", _factory)

    text = _read("[tool]shell executor", "cremind llm providers list")

    assert "Agent directive" not in text
    assert text.startswith('[Section "`cremind llm providers list`"')
    assert calls == [], "the [cli] prefix check must short-circuit before the registry"


def test_section_reader_omits_the_directive_when_exec_shell_is_disabled(monkeypatch):
    _patch_section_service(monkeypatch, name="[cli]cremind llm", body=_long_cli_body())
    _patch_registry(monkeypatch, leaves=[{"leaf_name": "exec_shell", "enabled": False}])

    text = _read("[cli]cremind llm", "cremind llm providers list")

    assert "Agent directive" not in text
    assert text.startswith('[Section "`cremind llm providers list`"')
