"""The retrieval mode rides documentation_search's per-search INFO summary.

``DocumentSyncService.search`` records which path it took (``vector`` or one of
the full-scan fallbacks) on ``last_search_mode``. The tool reads it off the
service right after searching and appends ``mode=<path>`` to the one INFO
summary line it emits per search, so ``logs/app.log`` can tell a ranked search
from a degraded full scan. A service without the attribute reads as
``unknown`` rather than failing the search.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from app.constants import ChatCompletionTypeEnum
from app.utils.logger import logger

import app.tools.builtin.documentation_search as ds


_NAME = "widgets guide"
_QUERY = "how do I add a widget"
_BODY = "# Widgets\n\nShort body.\n"
_HIT = {
    "file_path": f"/docs/{_NAME}.md", "text": "How to manage widgets.",
    "name": _NAME, "scope": "shared", "score": 0.9,
}


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    # Deterministic sizes (and no tiktoken encoding download), a fixed clamp,
    # identity $VAR resolution, and a registry that is not there.
    monkeypatch.setattr(ds, "_tokens", lambda t: len(t) // 4 if t else 0)
    monkeypatch.setattr(ds, "resolve_system_var_tokens", lambda body, profile: body)
    monkeypatch.setattr(
        ds, "_resolve_clamp",
        lambda profile: SimpleNamespace(tool_result_enabled=True, tool_result_max_tokens=4000),
    )

    def _no_registry():
        raise RuntimeError("registry not initialized")

    monkeypatch.setattr("app.tools.registry.get_tool_registry", _no_registry)


class _FakeLLM:
    """Judge that makes ``function_calls`` then reports DONE; or raises."""

    def __init__(self, *, function_calls=None, raises: Optional[Exception] = None):
        self.provider_name = "fake"
        self.model_name = "fake-mini"
        self.model_label = "fake/fake-mini"
        self._function_calls = (
            function_calls if function_calls is not None
            else [{"name": "select_document", "arguments": {"index": 0}}]
        )
        self._raises = raises
        self.calls = 0

    async def chat_completion(self, **kwargs):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        yield {
            "type": ChatCompletionTypeEnum.FUNCTION_CALLING,
            "data": {"function": self._function_calls},
        }
        yield {"type": ChatCompletionTypeEnum.DONE, "input_tokens": 10, "output_tokens": 1}


class _SvcWithMode:
    """Exposes ``last_search_mode`` as a plain attribute."""

    def __init__(self, *, mode: Optional[str], hits: Optional[List[Dict[str, Any]]] = None):
        self.last_search_mode = mode
        self._hits = [dict(_HIT)] if hits is None else hits

    def search(self, *, query, profile, limit, scopes=None):
        return self._hits

    def read_body(self, path):
        return _BODY


class _SvcModeSetBySearch:
    """Like the real service: a read-only property that ``search`` updates."""

    def __init__(self, *, stale: str, current: str):
        self._mode = stale
        self._current = current

    @property
    def last_search_mode(self) -> Optional[str]:
        return self._mode

    def search(self, *, query, profile, limit, scopes=None):
        self._mode = self._current
        return [dict(_HIT)]

    def read_body(self, path):
        return _BODY


class _SvcWithoutMode:
    """An older service shape: no ``last_search_mode`` at all."""

    def search(self, *, query, profile, limit, scopes=None):
        return [dict(_HIT)]

    def read_body(self, path):
        return _BODY


def _run(svc, monkeypatch, *, llm: Optional[_FakeLLM] = None, **kwargs):
    """Run one search against ``svc``; return ``(result, summary lines)``."""
    monkeypatch.setattr(ds, "get_service", lambda: svc)
    args = {"query": _QUERY, "_llm": llm or _FakeLLM(), "_profile": "admin"}
    messages: List[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        res = asyncio.run(ds.run_doc_search(args, **kwargs))
    finally:
        logger.remove(sink_id)
    return res, [m for m in messages if " query=" in m and " decision=" in m]


def _no_result_payload() -> Dict[str, Any]:
    return {"message": ds.NO_RESULT_MESSAGE, "relevant": False}


# ── The mode reaches the summary line ───────────────────────────────────────


def test_summary_line_carries_the_services_search_mode(monkeypatch):
    res, lines = _run(_SvcWithMode(mode="fallback-disabled"), monkeypatch)

    assert res.content[0]["text"] == _BODY
    assert len(lines) == 1, lines  # one summary per search
    line = lines[0]
    assert f"[documentation_search] query={_QUERY!r} ranked=[{_NAME}=0.9000]" in line
    assert f"decision=select:{_NAME}[0] mode=fallback-disabled" in line


def test_summary_reads_the_mode_this_search_set_not_a_stale_one(monkeypatch):
    svc = _SvcModeSetBySearch(stale="fallback-error", current="vector")

    _, lines = _run(svc, monkeypatch)

    assert len(lines) == 1
    assert lines[0].rstrip().endswith("mode=vector")
    assert "fallback-error" not in lines[0]


@pytest.mark.parametrize("svc_factory", [
    _SvcWithoutMode,
    lambda: _SvcWithMode(mode=None),
], ids=["attribute-missing", "attribute-none"])
def test_a_service_without_a_mode_logs_unknown(monkeypatch, svc_factory):
    res, lines = _run(svc_factory(), monkeypatch)

    assert res.content[0]["text"] == _BODY  # the search itself is unaffected
    assert len(lines) == 1
    assert f"decision=select:{_NAME}[0] mode=unknown" in lines[0]


# ── Early exits log the mode too ────────────────────────────────────────────


def test_no_candidates_logs_the_mode_and_returns_the_no_result_payload(monkeypatch):
    llm = _FakeLLM()
    res, lines = _run(_SvcWithMode(mode="fallback-no-collection", hits=[]), monkeypatch, llm=llm)

    assert res.structured_content == _no_result_payload()
    assert res.content is None
    assert res.token_usage is None
    assert llm.calls == 0  # nothing to judge
    assert len(lines) == 1
    assert f"[documentation_search] query={_QUERY!r} ranked=[] decision=no-candidates mode=fallback-no-collection" in lines[0]


def test_no_usable_candidates_logs_the_mode(monkeypatch):
    # Hits without a description or a file path are dropped before judging.
    hits = [
        {"file_path": "/docs/a.md", "text": "", "name": "a", "scope": "shared", "score": 0.5},
        {"file_path": None, "text": "desc", "name": "b", "scope": "shared", "score": 0.4},
    ]
    llm = _FakeLLM()
    res, lines = _run(_SvcWithMode(mode="vector", hits=hits), monkeypatch, llm=llm)

    assert res.structured_content == _no_result_payload()
    assert llm.calls == 0
    assert len(lines) == 1
    assert "decision=no-usable-candidates mode=vector" in lines[0]


def test_judge_no_match_logs_the_mode(monkeypatch):
    llm = _FakeLLM(function_calls=[{"name": "no_relevant_result", "arguments": {}}])
    res, lines = _run(_SvcWithMode(mode="vector"), monkeypatch, llm=llm)

    assert res.structured_content == _no_result_payload()
    assert len(lines) == 1
    assert "decision=no_relevant_result mode=vector" in lines[0]


def test_judge_error_logs_the_mode(monkeypatch):
    llm = _FakeLLM(raises=RuntimeError("judge model not supported (400)"))
    res, lines = _run(_SvcWithMode(mode="fallback-error"), monkeypatch, llm=llm)

    assert res.structured_content["error"] is True
    assert res.structured_content["relevant"] is False
    assert len(lines) == 1
    assert "decision=error:judge-llm-failed mode=fallback-error" in lines[0]


# ── Plumbing ────────────────────────────────────────────────────────────────


def test_log_label_tags_the_summary_line(monkeypatch):
    _, lines = _run(_SvcWithMode(mode="vector"), monkeypatch, log_label="custom_docs")

    assert len(lines) == 1
    assert lines[0].count("[custom_docs] query=") == 1
    assert "[documentation_search] query=" not in lines[0]
    assert "mode=vector" in lines[0]


def test_select_best_candidate_defaults_the_mode_to_unknown():
    messages: List[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        idx, _, errored = asyncio.run(ds._select_best_candidate(
            llm=_FakeLLM(), query="q", candidates=[{"name": "a", "description": "d"}],
        ))
    finally:
        logger.remove(sink_id)

    assert (idx, errored) == (0, False)
    lines = [m for m in messages if " decision=" in m]
    assert len(lines) == 1
    assert "decision=select:a[0] mode=unknown" in lines[0]
    assert "ranked=[a=n/a]" in lines[0]  # a candidate without a score
