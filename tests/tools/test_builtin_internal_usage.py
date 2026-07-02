"""Internal-LLM token usage capture for built-in tools.

Two built-in tools make their own ``chat_completion`` call inside ``run()`` — the
``documentation_search`` relevance judge and ``image_understanding``'s vision
call. These pin that each captures the four-way usage off the terminal ``DONE``
chunk, surfaces it on ``BuiltInToolResult.token_usage``, and that the adapter
folds that into the ``token_usage`` artifact (which downstream becomes a
``source_kind="tool"`` usage record). Tools that report no usage fold nothing.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List

from app.constants import ChatCompletionTypeEnum
from app.tools.builtin.adapter import BuiltInToolAdapter
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.utils.event_parser import parse_agent_events

import app.tools.builtin.documentation_search as ds
import app.tools.builtin.image_understanding as iu


class _FakeLLM:
    """LLMProvider stand-in: optional FUNCTION_CALLING/CONTENT chunk then DONE+tokens."""

    def __init__(self, *, function_calls=None, content=None, tokens=None):
        self._function_calls = function_calls
        self._content = content
        self._tokens = tokens or {}
        self.provider_name = "fake"
        self.model_name = "fake-mini"
        self.model_label = "fake/fake-mini"

    async def chat_completion(self, **kwargs):
        if self._function_calls is not None:
            yield {
                "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
                "data": {"function": self._function_calls},
            }
        if self._content is not None:
            yield {"type": ChatCompletionTypeEnum.CONTENT, "data": self._content}
        done = {"type": ChatCompletionTypeEnum.DONE}
        done.update(self._tokens)
        yield done


def _collect(agen) -> list:
    out: list = []

    async def _consume() -> None:
        async for ev in agen:
            out.append(ev)

    asyncio.run(_consume())
    return out


# --- documentation_search judge ---------------------------------------------

def test_select_best_candidate_returns_index_and_usage():
    llm = _FakeLLM(
        function_calls=[{"name": "select_document", "arguments": {"index": 0}}],
        tokens={"input_tokens": 42, "output_tokens": 3},
    )
    idx, usage = asyncio.run(ds._select_best_candidate(
        llm=llm, query="how to write a skill",
        candidates=[{"name": "a", "description": "d"}],
    ))
    assert idx == 0
    assert usage["input_tokens"] == 42
    assert usage["output_tokens"] == 3


def test_select_best_candidate_returns_usage_on_no_match():
    llm = _FakeLLM(
        function_calls=[{"name": "no_relevant_result", "arguments": {}}],
        tokens={"input_tokens": 30, "output_tokens": 1},
    )
    idx, usage = asyncio.run(ds._select_best_candidate(
        llm=llm, query="q", candidates=[{"name": "a", "description": "d"}],
    ))
    assert idx is None
    assert usage["input_tokens"] == 30  # cost is attributed even on no-match


def _patch_docsearch_service(monkeypatch, *, body="BODY: how to write a skill"):
    hits = [{
        "file_path": "/docs/skill.md", "text": "How to write a sample skill",
        "name": "sample-skill", "scope": "shared", "score": 0.91,
    }]

    class _Svc:
        def search(self, *, query, profile, limit, scopes=None):
            return hits

        def read_body(self, path):
            return body

    monkeypatch.setattr(ds, "get_service", lambda: _Svc())
    monkeypatch.setattr(ds, "resolve_system_var_tokens", lambda b, profile: b)


def test_docsearch_run_attaches_usage_on_match(monkeypatch):
    _patch_docsearch_service(monkeypatch)
    llm = _FakeLLM(
        function_calls=[{"name": "select_document", "arguments": {"index": 0}}],
        tokens={"input_tokens": 200, "output_tokens": 6},
    )
    res = asyncio.run(ds.DocumentationSearchTool().run(
        {"query": "how to write a skill", "_llm": llm, "_profile": "admin"}
    ))
    assert res.content and "BODY" in res.content[0]["text"]
    assert res.token_usage == {
        "input_tokens": 200, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 6,
    }


def test_docsearch_run_attaches_usage_on_no_match(monkeypatch):
    _patch_docsearch_service(monkeypatch)
    llm = _FakeLLM(
        function_calls=[{"name": "no_relevant_result", "arguments": {}}],
        tokens={"input_tokens": 150, "output_tokens": 2},
    )
    res = asyncio.run(ds.DocumentationSearchTool().run(
        {"query": "unrelated", "_llm": llm, "_profile": "admin"}
    ))
    assert res.structured_content["relevant"] is False
    assert res.token_usage["input_tokens"] == 150


# --- image_understanding vision call ----------------------------------------

def test_image_understanding_attaches_usage(monkeypatch, tmp_path):
    img = tmp_path / "pic.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n not-a-real-image")
    # Bypass the capability gate and image encoding to focus on usage wiring.
    monkeypatch.setattr(iu, "model_supports_vision", lambda provider, model: True)
    monkeypatch.setattr(
        iu, "_prepare_image_data_url",
        lambda *a, **k: ("data:image/png;base64,AAAA", None),
    )
    llm = _FakeLLM(content="A cat sitting on a mat.",
                   tokens={"input_tokens": 500, "output_tokens": 12})
    res = asyncio.run(iu.AnalyzeImageTool(data_dir=str(tmp_path)).run(
        {"path": "pic.png", "query": "what is this?", "_llm": llm}
    ))
    assert res.content and "cat" in res.content[0]["text"].lower()
    assert res.token_usage["input_tokens"] == 500
    assert res.token_usage["output_tokens"] == 12


# --- adapter folds tool-reported usage into the emitted artifact ------------

class _UsageTool(BuiltInTool):
    name = "docjudge"
    description = "fake tool that reports internal-LLM usage"
    parameters: Dict[str, Any] = {
        "type": "object", "properties": {}, "additionalProperties": False,
    }

    def __init__(self, token_usage=None) -> None:
        self._tu = token_usage

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return BuiltInToolResult(structured_content={"text": "ok"}, token_usage=self._tu)


def _run_adapter(tool) -> Dict[str, int]:
    adapter = BuiltInToolAdapter(tools=[tool], llm=object(), name="documentation_search")
    events = _collect(adapter.request(
        query="docjudge", decided_calls=[{"name": "docjudge", "arguments": {}}],
    ))
    _obs, usage, _parts = parse_agent_events(events)
    return usage


def test_adapter_folds_tool_usage_into_artifact():
    usage = _run_adapter(_UsageTool(token_usage={
        "input_tokens": 80, "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 0, "output_tokens": 9,
    }))
    assert usage["input_tokens"] == 80
    assert usage["cache_read_input_tokens"] == 4
    assert usage["output_tokens"] == 9


def test_adapter_emits_zero_usage_when_tool_reports_none():
    usage = _run_adapter(_UsageTool(token_usage=None))
    assert usage == {
        "input_tokens": 0, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "output_tokens": 0,
    }
