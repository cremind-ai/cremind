"""What the chat agent does with a research result before it answers.

The reported turns: a legal research job finished with no authority and no
finding, and the agent answered anyway — once with procedural advice no
document supported, once by asking the user to upload a decree the index
already held. Pinned here, on the real agent loop with the built-in
Documentation Search group (its research and read leaves scripted):

- a settled legal analysis with no usable evidence ends the turn with the
  server's insufficiency summary, before any further model request — for a
  result the tool returned and for a result delivered as its own turn;
- a job still running, a job with evidence, or another job of the same turn
  with evidence does not;
- verified evidence printed on a dossier page the model has not seen is read
  first, as the agent's own call (Origin on the trace), within the turn's
  token allowance — the whole of the reported 6-page dossier;
- the typed record only counts from the built-in group;
- each call's Thinking Process badge names the model it runs on: research
  its own group's, a dossier page none (the reported steps all showed the
  search tool's `low` model).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from app.agent import research_handoff as rh  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from app.documents.research.handoff import ResearchDelivery  # noqa: E402
from app.tools.base import ToolResultEvent, ToolType  # noqa: E402
from tests.agent.test_document_review import _LLM, _agent, _docs_group, _run, env  # noqa: E402,F401

RESEARCH = "documentation_search__research"
READ = "documentation_search__read"
SUMMARY = "I could not answer this from your documents: the research found no verified evidence."


def _record(job="job-a", status="partial", *, findings=0, evidence=(), delivered=(), pages=1, evidence_pages=(),
            page=1, domain="legal", insufficiency=None, page_tokens=0) -> ResearchDelivery:
    return ResearchDelivery(job_id=job, status=status, mode="analyze", domain=domain,
                            outcome="evidenced" if findings else "candidates_rejected", findings=findings,
                            evidence=tuple(evidence), delivered=tuple(delivered), page=page, pages=pages,
                            evidence_pages=tuple(evidence_pages), page_tokens=page_tokens,
                            insufficiency=insufficiency if insufficiency is not None else
                            (SUMMARY if (not findings and domain == "legal" and status != "running") else None))


def _scripted_docs(research_records: list, pages: dict | None = None):
    """The real group, its research leaf answering with ``research_records``
    in turn and its read leaf with ``pages[(job, page)]``."""
    from app.tools.builtin.base import BuiltInToolResult

    docs = _docs_group()
    records = list(research_records)
    reads: list[dict] = []

    async def research(arguments):
        rec = records.pop(0)
        return BuiltInToolResult(content=[{"type": "text", "text": f"[research] job {rec.job_id} {rec.status}"}],
                                 evidence=rec)

    async def read(arguments):
        reads.append({k: v for k, v in arguments.items() if not k.startswith("_")})
        job = str(arguments["file"]).split(":", 1)[1]
        text, rec = (pages or {})[(job, int(arguments.get("page") or 1))]
        return BuiltInToolResult(content=[{"type": "text", "text": text}], evidence=rec)

    docs._adapter._tools_by_name["research"].run = research
    docs._adapter._tools_by_name["read"].run = read
    return docs, reads


def test_a_legal_job_without_evidence_ends_the_turn_before_another_model_call(env):
    docs, _reads = _scripted_docs([_record()])
    llm = _LLM([[(RESEARCH, {"question": "Does the permit need reissuing?", "domain": "legal"})],
                "An answer from general knowledge that must never be written."])
    chunks = _run(_agent(llm, tools=[docs]))
    assert len(llm.main) == 1  # no synthesis call after the result
    done = chunks[-1]
    assert done["type"] == T.DONE and done["data"] == SUMMARY
    assert done["llm_messages"][-1] == {"role": "assistant", "content": SUMMARY}
    assert not any(c.get("type") == T.CONTENT for c in chunks)


def test_a_job_still_running_is_answered_as_running(env):
    docs, _ = _scripted_docs([_record(status="running")])
    llm = _LLM([[(RESEARCH, {"question": "q", "domain": "legal"})], "It is still running."])
    done = _run(_agent(llm, tools=[docs]))[-1]
    assert len(llm.main) == 2 and done["data"] == ""


def test_a_job_with_evidence_is_answered_by_the_model(env):
    rec = _record(status="complete", findings=2, evidence=("[doc:aaaaaaaa#00000001]",),
                  delivered=("[doc:aaaaaaaa#00000001]",), evidence_pages=(1,))
    docs, reads = _scripted_docs([rec])
    llm = _LLM([[(RESEARCH, {"question": "q", "domain": "legal"})], "Answer [doc:aaaaaaaa#00000001]."])
    _run(_agent(llm, tools=[docs]))
    assert len(llm.main) == 2 and reads == []


def test_evidence_on_a_page_not_shown_is_read_before_the_model_answers(env):
    first = _record(status="complete", findings=3, evidence=("[doc:aaaaaaaa#00000001]", "[doc:aaaaaaaa#00000002]"),
                    delivered=("[doc:aaaaaaaa#00000001]",), pages=3, evidence_pages=(1, 2))
    page2 = _record(status="complete", findings=3, evidence=first.evidence,
                    delivered=("[doc:aaaaaaaa#00000002]",), pages=3, evidence_pages=(1, 2), page=2)
    docs, reads = _scripted_docs([first], {("job-a", 2): ("PAGE TWO with [doc:aaaaaaaa#00000002]", page2)})
    llm = _LLM([[(RESEARCH, {"question": "q", "domain": "legal"})], "answer"])
    chunks = _run(_agent(llm, tools=[docs]))
    assert reads == [{"file": "research:job-a", "page": 2}]
    msgs = llm.main[1]["messages"]
    group = next(m for m in msgs if m.get("role") == "assistant" and m.get("tool_calls")
                 and m["tool_calls"][0]["id"].startswith("call_research_"))
    assert json.loads(group["tool_calls"][0]["function"]["arguments"]) == {"file": "research:job-a", "page": 2}
    result = next(m for m in msgs if m.get("tool_call_id") == group["tool_calls"][0]["id"])
    assert "PAGE TWO" in result["content"] and "read for you" in result["content"]
    assert any(c.get("type") == T.THINKING_ARTIFACT and c["data"].get("Origin") == rh.ORIGIN for c in chunks)
    # Page 3 carries no evidence: never read.
    assert len(reads) == 1


def _dossier_pages(n: int, *, page_tokens: int):
    """A settled job whose ``n`` pages each print one piece of evidence: its
    first page's record, and the read leaf's pages 2..n."""
    ev = tuple(f"[doc:aaaaaaaa#{i:08d}]" for i in range(1, n + 1))
    kw = dict(status="complete", findings=n, evidence=ev, pages=n, evidence_pages=tuple(range(1, n + 1)),
              page_tokens=page_tokens)
    pages = {("job-a", p): (f"PAGE {p}", _record(delivered=ev[p - 1:p], page=p, **kw)) for p in range(2, n + 1)}
    return _record(delivered=ev[:1], **kw), pages


def test_the_reported_dossier_is_read_whole_before_the_model_answers(env):
    """The reported turn: six 3,900-token pages, verified findings on each.
    The agent read three and the model spent a step fetching the other two;
    now every one is read before the model's next request."""
    first, pages = _dossier_pages(6, page_tokens=3900)
    docs, reads = _scripted_docs([first], pages)
    llm = _LLM([[(RESEARCH, {"question": "q", "domain": "legal"})], "answer"])
    _run(_agent(llm, tools=[docs]))
    assert [r["page"] for r in reads] == [2, 3, 4, 5, 6]
    assert len(llm.main) == 2 and "PAGE 6" in json.dumps(llm.main[1]["messages"], ensure_ascii=False)


def test_page_reads_stay_within_the_turns_allowance(env):
    first, pages = _dossier_pages(14, page_tokens=4000)
    docs, reads = _scripted_docs([first], pages)
    llm = _LLM([[(RESEARCH, {"question": "q", "domain": "legal"})], "answer"])
    agent = _agent(llm, tools=[docs])
    _run(agent)
    # The fake model has no catalog entry: a quarter of the default window.
    assert agent._research.allowance == 32_000 == rh.allowance_for("fake", "fake-model")
    assert [r["page"] for r in reads] == list(range(2, 10))  # 8 pages of 4,000 tokens
    assert agent._research.auto_tokens == 32_000


def test_the_allowance_is_capped_by_the_model_context_window(monkeypatch):
    import app.lib.llm.pricing as pricing

    windows = {"gpt-5.6-terra": 1_050_000, "local-32k": 32_000}
    monkeypatch.setattr(pricing, "context_window_for", lambda provider, model: windows.get(model))
    assert rh.allowance_for("openai", "gpt-5.6-terra") == rh.AUTO_READ_TOKENS == 40_000
    assert rh.allowance_for("ollama", "local-32k") == 8_000
    assert rh.allowance_for("x", "unknown") == pricing.DEFAULT_CONTEXT_WINDOW // rh.WINDOW_SHARE
    h = rh.ResearchHandoff(allowance=8_000)
    first, _pages = _dossier_pages(6, page_tokens=0)  # a record that does not say: 4,000 a page
    h.observe(first)
    assert h.pending_pages() == [("job-a", 2), ("job-a", 3)]


def test_another_job_with_evidence_in_the_same_turn_is_not_overridden(env):
    good = _record(job="job-b", status="complete", findings=1, evidence=("[doc:bbbbbbbb#00000001]",),
                   delivered=("[doc:bbbbbbbb#00000001]",), evidence_pages=(1,))
    docs, _ = _scripted_docs([_record(), good])
    llm = _LLM([[(RESEARCH, {"question": "a", "domain": "legal"}), (RESEARCH, {"question": "b", "domain": "legal"})],
                "answer from job b"])
    done = _run(_agent(llm, tools=[docs]))[-1]
    assert len(llm.main) == 2 and done["data"] == ""


def test_a_general_job_without_evidence_is_left_to_the_contract(env):
    docs, _ = _scripted_docs([_record(domain="general")])
    llm = _LLM([[(RESEARCH, {"question": "q"})], "The research found no evidence in your documents."])
    _run(_agent(llm, tools=[docs]))
    assert len(llm.main) == 2


def test_a_delivered_result_without_evidence_ends_its_turn_before_any_model_call(env):
    from app.agent import reasoning_agent as ra

    registry = SimpleNamespace(tools_for_profile=lambda p: [_docs_group()], disabled_leaves_by_tool=lambda p: {})
    llm = _LLM(["must not be called"])
    agent = ra.ReasoningAgent(llm=llm, registry=registry, profile="alice", context_id="ctx-alice",
                              research_delivery=_record().to_dict())
    done = _run(agent, query="[Document research result] …")[-1]
    assert llm.main == [] and done["data"] == SUMMARY


def test_the_record_only_counts_from_the_built_in_group():
    rec = _record()
    event = ToolResultEvent(observation_text="x", evidence=rec)
    builtin = SimpleNamespace(tool_type=ToolType.BUILTIN, tool_id="documentation_search")
    assert rh.trusted_research(builtin, event, tool_id="documentation_search") is rec
    for tool in (SimpleNamespace(tool_type=ToolType.MCP, tool_id="documentation_search"),
                 SimpleNamespace(tool_type=ToolType.BUILTIN, tool_id="web_search")):
        assert rh.trusted_research(tool, event, tool_id="documentation_search") is None
    forged = ToolResultEvent(observation_text="x", evidence=rec.to_dict())
    assert rh.trusted_research(builtin, forged, tool_id="documentation_search") is None


def test_the_turn_state_is_per_job_and_per_state():
    h = rh.ResearchHandoff()
    assert not h.observe(ResearchDelivery(error="ResearchBusy"))
    assert h.observe(_record(status="running"))
    assert h.insufficient() is None
    # The same job, now finished: judged on its new state.
    assert h.observe(_record(status="partial"))
    assert h.insufficient().job_id == "job-a"
    assert h.observe(_record(job="job-b", status="complete", findings=1, evidence=("[doc:b#1]",)))
    assert h.insufficient() is None


def test_each_call_is_labelled_with_the_model_it_runs_on(env, monkeypatch):
    """The reported steps: the research call and the dossier pages after it
    all carried the search tool's own `low` model, though the job runs on its
    research group and reading a page runs no model."""
    from app.documents.research import jobs

    monkeypatch.setattr(jobs, "research_model_label", lambda profile: f"Research model of {profile}")
    first, pages = _dossier_pages(3, page_tokens=3900)
    docs, _reads = _scripted_docs([first], pages)
    llm = _LLM([[(RESEARCH, {"question": "q", "domain": "legal"})], [(READ, {"file": "research:job-a", "page": 2})],
                "answer"])
    chunks = _run(_agent(llm, tools=[docs]))
    calls = [(c["data"]["Tool"], c["data"].get("Origin"), c["data"]["Model_Label"])
             for c in chunks if c.get("type") == T.THINKING_ARTIFACT]
    assert calls == [(RESEARCH, None, "Research model of alice"),
                     (READ, rh.ORIGIN, None), (READ, rh.ORIGIN, None),  # read for the agent: no model at all
                     (READ, None, "Fake")]  # the model's own read: the model that chose it


def test_search_calls_name_a_model_only_when_one_runs():
    from app.tools.builtin import documentation_search as ds

    low = SimpleNamespace(model_label="Openai gpt-5.6-luna")
    assert ds.DocumentsSearchTool().model_label({"query": "q", "thorough": True}, "alice", low) == low.model_label
    assert ds.DocumentsSearchTool().model_label({"query": "q"}, "alice", low) is None
    assert ds.DocumentsFindFilesTool().model_label({}, "alice", low) is None
    assert ds.DocumentsReadTool().model_label({"file": "research:job-a"}, "alice", low) is None
    assert ds.DocumentsResearchTool().model_label({"continue_job": "job-a", "cancel": True}, "alice", low) is None


def test_the_research_label_is_the_research_groups_model(monkeypatch):
    import app.storage as storage_pkg
    from app.documents.research import jobs
    from app.lib.llm.model_groups import ModelGroupManager

    monkeypatch.setattr(storage_pkg, "get_dynamic_config_storage", lambda: None)
    monkeypatch.setattr(jobs, "_configured_variables", lambda profile: {jobs.VAR_MODEL_GROUP: "low"})
    monkeypatch.setattr(ModelGroupManager, "get_provider_and_model",
                        lambda self, group, profile=None: ("openai", f"model-{group}"))
    assert jobs.research_model_label("alice") == "Openai model-low"

    def unset(self, group, profile=None):
        raise RuntimeError("no model configured")

    monkeypatch.setattr(ModelGroupManager, "get_provider_and_model", unset)
    assert jobs.research_model_label("alice") is None
