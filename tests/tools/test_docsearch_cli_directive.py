"""CLI-execution directive prepended to documentation_search results.

When ``documentation_search`` returns a ``cremind`` CLI reference (``[cli]…``)
and exec_shell is callable for the profile, the tool prepends an agent directive
telling the reasoning model to RUN the documented command via exec_shell and
answer from live output, instead of paraphrasing the man page (the original
incident: the model listed providers from the doc's example table and told the
user to run the command itself). These pin that behavior and its gating.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from app.constants import ChatCompletionTypeEnum

import app.tools.builtin.documentation_search as ds


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
