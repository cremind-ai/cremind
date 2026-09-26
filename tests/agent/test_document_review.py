"""Automatic source review in ordinary document answers.

The incident: asked "what is the multiple agent mode of OpenClaw?", the agent
searched the user's documents, got two relevant guides — the English one
ranked first — read only the Vietnamese one and answered from it. Every
citation verified; the English guide simply never reached the answer.

What is pinned here, on the real path — the built-in Documentation Search
group, its adapter and renderer, over two small stand-ins for the guides
(:mod:`tests.documents._openclaw_guides`) — and by inspecting what the NEXT
model request actually receives, not what the prompt promises:

- a search that shows relevant passages from two files gets the matching
  passage of each read before the next model response, in result order,
  whatever the order and the language;
- a passage already delivered whole is not read again; a search restricted to
  one file triggers nothing; each file is read once per turn, at most four
  reads and 6,000 rendered tokens per turn, across searches;
- the reads are the agent's own tool-call group: unique ids, every call
  answered, Origin on the activity trace, no model request, replay-safe;
- new user input, a research job, Instant mode, plan parking, a disabled read
  function and cancellation all stop it; two profiles in parallel share
  nothing;
- the turn's ``document_review`` record says what was returned, examined and
  cited — Sources stay the answer's own citations.

The pure bookkeeping (:class:`app.agent.document_review.DocumentReview`) is
tested first, on hand-made delivery records.
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("a2a")

import app.agent.reasoning_agent as ra  # noqa: E402
from app.agent import document_review as dr  # noqa: E402
from app.constants import ChatCompletionTypeEnum as T  # noqa: E402
from app.documents.cite import make_token  # noqa: E402
from app.documents.delivery import (  # noqa: E402
    FOCUS_COMPLETE,
    FOCUS_PARTIAL,
    FOCUS_UNRESOLVED,
    DocumentEvidence,
    PassageDelivery,
    SourceDelivery,
)
from app.tools.base import FunctionSpec, ToolResultEvent, ToolType, make_leaf_name  # noqa: E402
from tests.documents import _openclaw_guides as G  # noqa: E402

READ = "documentation_search__read"
SEARCH = "documentation_search__search"


# ── the bookkeeping, on hand-made records ──────────────────────────────────


def _p(fid: str, c8: str, *, role="match", complete=False, confidence="high", order=1, substantive=True):
    return PassageDelivery(token=make_token(fid, c8 * 8), fid=fid, role=role, complete=complete,
                           substantive=substantive, order=order, confidence=confidence)


def _search(*passages: PassageDelivery) -> DocumentEvidence:
    fids = list(dict.fromkeys(p.fid for p in passages))
    return DocumentEvidence(op="search", passages=list(passages),
                            sources=[SourceDelivery(fid=f, token=make_token(f), order=i + 1)
                                     for i, f in enumerate(fids)], rendered_tokens=500)


def _read(fid: str, c8: str, *, status=FOCUS_COMPLETE, tokens=900, **kw) -> DocumentEvidence:
    token = make_token(fid, c8 * 8)
    return DocumentEvidence(op="read", passages=[PassageDelivery(token=token, fid=fid, role="body",
                                                                 complete=status == FOCUS_COMPLETE)],
                            fid=fid, focus=token, focus_status=status, rendered_tokens=tokens, **kw)


def test_two_eligible_files_become_candidates_in_result_order():
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a", order=1), _p("vivivivi", "b", order=2, confidence="medium"),
                           _p("lowlowlo", "c", order=3, confidence="low")))
    planned = review.plan()
    assert [s.fid for s in planned] == ["enenenen", "vivivivi"]
    assert [s.candidate for s in planned] == [make_token("enenenen", "a" * 8), make_token("vivivivi", "b" * 8)]


@pytest.mark.parametrize("passages", [
    # One file only (a search restricted to one document).
    [_p("enenenen", "a"), _p("enenenen", "b", order=1)],
    # The second file matched only weakly, or only as context, or by its card.
    [_p("enenenen", "a"), _p("vivivivi", "b", order=2, confidence="low")],
    [_p("enenenen", "a"), _p("vivivivi", "b", order=2, role="context", confidence=None)],
    [_p("enenenen", "a"), _p("vivivivi", "b", order=2, substantive=False)],
])
def test_a_search_without_two_eligible_files_triggers_nothing(passages):
    review = dr.DocumentReview()
    review.observe(_search(*passages))
    assert not review.has_pending() and review.plan() == []


def test_a_passage_a_read_already_delivered_whole_is_not_read_again():
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2)))
    review.observe(_read("enenenen", "a"))
    assert [s.fid for s in review.plan()] == ["vivivivi"]


def test_a_short_passage_a_search_showed_whole_is_still_read():
    """In the incident the English guide's matching passage was short enough
    for the search snippet to show it whole — and the model passed over it.
    A read adds what the snippet lacks (the passages around it) and puts the
    source in front of the model as a read of its own, so only a read covers."""
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a", complete=True), _p("vivivivi", "b", order=2)))
    assert [s.fid for s in review.plan()] == ["enenenen", "vivivivi"]
    # The snippet alone still counts as a passage examined in the record.
    assert "enenenen" in review.summary("")["examined"]


def test_a_stale_or_partial_normal_read_does_not_cover_a_candidate():
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2)))
    review.observe(_read("enenenen", "a", stale=True))
    review.observe(_read("vivivivi", "b", status=FOCUS_PARTIAL))
    assert [s.fid for s in review.plan()] == ["enenenen", "vivivivi"]


def test_batches_merge_one_read_per_file_and_four_per_turn():
    review = dr.DocumentReview()
    review.observe(_search(*[_p(f"f{i}f{i}f{i}f{i}"[:8], "a", order=i) for i in range(1, 4)]))
    first = review.plan()
    assert len(first) == 3
    for src in first:
        review.record_attempt(src, _read(src.fid, "a", tokens=700), note_tokens=30)
    # A later search brings the same files back with other passages, and three new ones.
    review.observe(_search(*[_p(f"f{i}f{i}f{i}f{i}"[:8], "z", order=i) for i in range(1, 7)]))
    second = review.plan()
    assert [s.fid for s in second] == ["f4f4f4f4"]  # the fourth and last automatic read
    assert review.sources["f1f1f1f1"].candidate == make_token("f1f1f1f1", "a" * 8)  # fixed at its first search
    summary = review.summary("")
    assert {summary["reasons"][f] for f in ("f5f5f5f5", "f6f6f6f6")} == {dr.REASON_CALL_LIMIT}


def test_reads_share_the_allowance_and_never_exceed_it():
    review = dr.DocumentReview()
    review.observe(_search(*[_p(f"g{i}g{i}g{i}g{i}"[:8], "a", order=i) for i in range(1, 5)]))
    planned = review.plan()
    assert len(planned) == 4
    spent = 0
    for i, src in enumerate(planned):
        budget = review.budget_for(len(planned) - i)
        used = budget - 100  # each read comes in a little under its budget
        review.record_attempt(src, _read(src.fid, "a", tokens=used), note_tokens=40)
        spent += used + 40
    review.add_tokens(dr.SUMMARY_RESERVE)
    assert review.tokens == spent + dr.SUMMARY_RESERVE <= dr.TOKEN_ALLOWANCE
    # Nothing is left for another round.
    review.observe(_search(_p("h1h1h1h1", "a"), _p("h2h2h2h2", "b", order=2)))
    assert review.plan() == []


def test_too_little_allowance_skips_rather_than_reads_a_scrap():
    review = dr.DocumentReview(allowance=dr.SUMMARY_RESERVE + 2 * dr.NOTE_RESERVE + dr.MIN_READ_TOKENS + 10)
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2)))
    assert [s.fid for s in review.plan()] == ["enenenen"]
    assert review.summary("")["reasons"]["vivivivi"] == dr.REASON_TOKEN_BUDGET


@pytest.mark.parametrize("evidence, outcome, reason", [
    (_read("enenenen", "a"), dr.COMPLETE, None),
    (_read("enenenen", "a", status=FOCUS_PARTIAL, continuation={"page": 2}), dr.PARTIAL, "budget"),
    (_read("enenenen", "a", status=FOCUS_UNRESOLVED), dr.FAILED, "passage_gone"),
    (_read("enenenen", "a", stale=True), dr.FAILED, "file_changed"),
    (_read("enenenen", "a", metadata_only=True), dr.FAILED, "metadata_only"),
    (DocumentEvidence(op="read", error="NotFound"), dr.FAILED, "read_failed:NotFound"),
    (None, dr.FAILED, "no_result"),
])
def test_an_automatic_read_counts_only_a_current_whole_passage(evidence, outcome, reason):
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2)))
    src = review.plan()[0]
    attempt = review.record_attempt(src, evidence, note_tokens=30)
    assert (attempt.outcome, attempt.reason) == (outcome, reason)
    summary = review.summary("")
    listed = {dr.COMPLETE: "examined", dr.PARTIAL: "partial", dr.FAILED: "failed"}[outcome]
    assert "enenenen" in summary[listed]


def test_the_summary_separates_examined_from_cited():
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2), _p("lowlowlo", "c", order=3,
                                                                                  confidence="low")))
    for src in review.plan():
        review.record_attempt(src, _read(src.fid, "a" if src.fid == "enenenen" else "b"), note_tokens=30)
    summary = review.summary(f"Only one guide is cited {make_token('enenenen', 'a' * 8)}.")
    assert summary["v"] == dr.SUMMARY_VERSION
    assert (summary["returned"], summary["eligible"]) == (3, 2)
    assert summary["examined"] == ["enenenen", "vivivivi"]
    assert summary["cited"] == ["enenenen"]
    assert summary["automatic"]["calls"] == 2 and summary["unexamined"] == []


def test_the_round_note_names_pages_and_asks_for_the_files_own_section():
    """The live evaluation's failure: the Vietnamese guide's automatic read
    was its comparison table (p. 49), the model answered from that and even
    said the guides do not describe delegation — its own section does. The
    note says which page each read covered, asks for the file's own section
    when the passage only touches the subject, and forbids claims about
    unread parts."""
    review = dr.DocumentReview()
    en, vi = _p("enenenen", "a"), _p("vivivivi", "b", order=2)
    vi = PassageDelivery(**{**vi.__dict__, "position": "p. 49"})
    review.observe(_search(en, vi))
    attempts = [review.record_attempt(src, _read(src.fid, "a" if src.fid == "enenenen" else "b"), note_tokens=30)
                for src in review.plan()]
    note = review.round_note(attempts, READ)
    assert "[doc:vivivivi] p. 49 complete" in note
    assert "file's own section on the subject is not above, read it before answering" in note
    assert "filters.file_ids" in note
    assert "Never state that a file does not cover something unless you read that part of it" in note


def test_a_partial_automatic_read_is_continued_by_a_plain_re_read():
    """Pages of a centred read are cut by budget: the automatic read's small
    budget and the model's own full one cut in different places, so pointing
    at "page=2" would skip text. The note asks for the passage again."""
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2)))
    attempts = [review.record_attempt(src, _read(src.fid, "a", status=FOCUS_PARTIAL, continuation={"page": 2}),
                                      note_tokens=30) for src in review.plan()]
    note = review.round_note(attempts, READ)
    assert f'partial (for the rest, read file="{make_token("enenenen", "a" * 8)}" again)' in note
    assert "page=" not in note


def test_a_failed_read_is_not_hidden_by_a_whole_search_snippet():
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a", complete=True), _p("vivivivi", "b", order=2)))
    for src in review.plan():
        evidence = (DocumentEvidence(op="read", error="NotFound") if src.fid == "enenenen"
                    else _read("vivivivi", "b"))
        review.record_attempt(src, evidence, note_tokens=30)
    summary = review.summary("")
    assert summary["failed"] == ["enenenen"] and summary["examined"] == ["vivivivi"]
    assert summary["reasons"] == {"enenenen": "read_failed:NotFound"}


def test_a_read_that_showed_only_a_neighbour_did_not_examine_the_file():
    """A read centred on a passage examines its file only when that passage
    came through whole — page 2 of it, showing a neighbour, does not."""
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2)))
    neighbour = PassageDelivery(token=make_token("vivivivi", "c" * 8), fid="vivivivi", role="body", complete=True)
    review.observe(DocumentEvidence(op="read", passages=[neighbour], fid="vivivivi",
                                    focus=make_token("vivivivi", "b" * 8), focus_status="omitted"))
    assert "vivivivi" not in review.summary("")["examined"]
    assert [s.fid for s in review.plan()] == ["enenenen", "vivivivi"]


def test_the_longest_round_note_fits_its_reserve():
    review = dr.DocumentReview()
    passages = [PassageDelivery(token=make_token(f"f{i}" * 4, "a" * 8), fid=f"f{i}" * 4, role="match",
                                complete=False, order=i, confidence="high", position=f"p. {100 * i}–{100 * i + 1}")
                for i in range(1, 9)]
    review.observe(_search(*passages))
    attempts = [review.record_attempt(src, _read(src.fid, "a", status=FOCUS_PARTIAL, continuation={"page": 12}),
                                      note_tokens=30) for src in review.plan()]
    assert len(attempts) == dr.MAX_AUTO_READS
    assert dr.count_tokens(review.round_note(attempts, READ)) <= dr.SUMMARY_RESERVE


def test_suppression_records_why_a_candidate_was_left():
    review = dr.DocumentReview()
    review.observe(_search(_p("enenenen", "a"), _p("vivivivi", "b", order=2)))
    review.suppress(dr.REASON_NEW_INPUT)
    assert not review.has_pending()
    summary = review.summary("")
    assert summary["unexamined"] == ["enenenen", "vivivivi"]
    assert set(summary["reasons"].values()) == {dr.REASON_NEW_INPUT}


def test_only_the_built_in_group_supplies_evidence():
    ev = _read("enenenen", "a")
    event = ToolResultEvent(observation_text="x", evidence=ev)
    builtin = SimpleNamespace(tool_type=ToolType.BUILTIN, tool_id="documentation_search")
    assert dr.trusted_evidence(builtin, event, tool_id="documentation_search") is ev
    for tool in (SimpleNamespace(tool_type=ToolType.MCP, tool_id="documentation_search"),
                 SimpleNamespace(tool_type=ToolType.BUILTIN, tool_id="web_search")):
        assert dr.trusted_evidence(tool, event, tool_id="documentation_search") is None
    # Anything that is not the typed record — a dict, text — is ignored.
    forged = ToolResultEvent(observation_text="x", evidence={"op": "read", "passages": []})
    assert dr.trusted_evidence(builtin, forged, tool_id="documentation_search") is None


# ── the real path: a turn over the two guides ─────────────────────────────


class _LLM:
    """Scripted model. Each entry is a list of ``(function, arguments)`` calls
    for one step, or a string (the final answer). Every request's messages are
    kept, deep-copied, for inspection."""

    provider_name = "fake"
    model_name = "fake-model"
    model_label = "Fake"

    def __init__(self, script: list, on_call=None, ack_text: str = "SKIP") -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.on_call = on_call
        self.ack_text = ack_text

    async def chat_completion_stream(self, *, messages, tools=None, tool_choice=None, **kwargs):
        last = messages[-1].get("content") if messages else ""
        ack = isinstance(last, str) and last.startswith("[Pause")
        self.calls.append({"messages": copy.deepcopy(messages), "tool_choice": tool_choice, "ack": ack})
        if ack:
            yield {"type": T.CONTENT, "data": self.ack_text}
            yield {"type": T.DONE, "input_tokens": 1, "output_tokens": 1}
            return
        main = [c for c in self.calls if not c["ack"]]
        if self.on_call is not None:
            self.on_call(len(main))
        action = self.script.pop(0)
        if isinstance(action, str):
            yield {"type": T.CONTENT, "data": action}
            yield {"type": T.DONE, "input_tokens": 3, "output_tokens": 1, "finish_reason": "stop"}
            return
        yield {"type": T.FUNCTION_CALLING, "data": {"function": [
            {"index": i, "id": f"call_{len(main)}_{i}", "name": name, "arguments": args}
            for i, (name, args) in enumerate(action)
        ]}}
        yield {"type": T.DONE, "input_tokens": 5, "output_tokens": 2, "finish_reason": "tool_calls"}

    @property
    def main(self) -> list[dict]:
        return [c for c in self.calls if not c["ack"]]


class _Group:
    """A plain built-in group (for research, plan tools…) that records calls."""

    tool_type = ToolType.BUILTIN

    def __init__(self, tool_id: str, *leaves: str, on_run=None) -> None:
        self.tool_id = tool_id
        self.config_name = tool_id
        self.name = tool_id
        self.hidden = True
        self._leaves = list(leaves)
        self.skills = [SimpleNamespace(name=leaf) for leaf in leaves]
        self.executed: list[str] = []
        self.on_run = on_run

    def leaf_function_specs(self, *, context_id, profile, query="", arguments=None):
        return [FunctionSpec(name=make_leaf_name(self.tool_id, leaf), leaf_name=leaf, schema={
            "type": "function", "function": {"name": make_leaf_name(self.tool_id, leaf), "description": leaf,
                                             "parameters": {"type": "object", "properties": {}}}})
            for leaf in self._leaves]

    async def execute_leaf(self, *, leaf_name, args, context_id, profile, arguments, variables):
        self.executed.append(leaf_name)
        if self.on_run is not None:
            await self.on_run(leaf_name, args)
        yield ToolResultEvent(observation_text=f"{self.tool_id}.{leaf_name} ran")


class _Registry:
    def __init__(self, by_profile: dict, disabled: dict | None = None) -> None:
        self.by_profile = by_profile
        self.disabled = disabled or {}

    def tools_for_profile(self, profile):
        return list(self.by_profile[profile])

    def disabled_leaves_by_tool(self, profile):
        return {k: set(v) for k, v in self.disabled.get(profile, {}).items()}


def _docs_group():
    """The real Documentation Search built-in group: its leaves, adapter,
    renderer and evidence transport, exactly as registered."""
    from app.tools.builtin import documentation_search as ds
    from app.tools.builtin.tool import BuiltInToolGroup

    group = BuiltInToolGroup(config_name="documentation_search", display_name=ds.SERVER_NAME,
                             description=ds.TOOL_CONFIG["description"], functions=ds.get_tools({}),
                             llm=SimpleNamespace(provider_name="fake", model_name="low", model_label="Low"))
    group.tool_id = "documentation_search"
    return group


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The agent's surroundings pinned, and a profile -> engine map the
    Documentation Search leaves answer from."""
    import app.documents.gate as gate
    import app.documents.query as query_pkg
    import app.tools.builtin.cremind_documentation_search as manual
    from app.documents import citations
    from app.documents.query.engine import Access

    monkeypatch.setenv("CREMIND_WORKSPACES_DIR", str(tmp_path / "workspaces"))
    monkeypatch.setattr(ra, "resolve_agent_config", lambda p: SimpleNamespace(
        max_llm_retries=0, reasoning_temperature=1.0, reasoning_max_tokens=1024, reasoning_retry=0,
        tool_result_enabled=True, tool_result_max_tokens=4000, enable_prompt_cache=False, max_steps=8))
    monkeypatch.setattr(ra, "read_persona_file", lambda p: "PERSONA")
    monkeypatch.setattr(ra, "read_instructions_file", lambda p: "")
    monkeypatch.setattr(ra, "get_user_working_directory", lambda *a, **k: str(tmp_path))

    async def _no_ltm(self):
        return ""

    monkeypatch.setattr(ra.ReasoningAgent, "_load_long_term_memory_block", _no_ltm)
    ra._LONG_TERM_MEMORY_SNAPSHOT.clear()
    monkeypatch.setattr(gate, "documents_tool_available", lambda p, o: True)
    monkeypatch.setattr(manual, "_delivery_budget", lambda profile, reserved: 3900)
    issued: list[tuple] = []
    monkeypatch.setattr(citations, "issue", lambda p, c, items: issued.append((p, c, list(items))))
    engines: dict[str, Any] = {}
    reads: list[tuple[str, dict]] = []

    def fake_open(profile):
        return Access(engine=engines[profile]) if profile in engines else Access(engine=None, code="disabled",
                                                                               message="off")

    monkeypatch.setattr(query_pkg, "open_engine", fake_open)
    # Every read the leaves perform, whoever asked for it.
    from app.tools.builtin import documentation_search as ds

    real_execute = ds.execute

    async def spy_execute(leaf, profile, args, **kw):
        if leaf == "read":
            reads.append((profile, dict(args), kw.get("max_tokens")))
        return await real_execute(leaf, profile, args, **kw)

    monkeypatch.setattr(ds, "execute", spy_execute)
    guides = G.build(str(tmp_path / "alice.db"))
    G.activate_vectors(guides.db)
    guides.en_multi = G.page_chunk(guides.db, guides.en, 10, contains="Step 11")
    guides.vi_multi = G.page_chunk(guides.db, guides.vi, 43)
    engines["alice"] = G.engine(guides, vector_hits=[(guides.en_multi["id"], 0.9), (guides.vi_multi["id"], 0.85)],
                                profile="alice")
    yield SimpleNamespace(engines=engines, guides=guides, issued=issued, reads=reads, tmp=tmp_path)
    guides.db.close()
    ra._LONG_TERM_MEMORY_SNAPSHOT.clear()


def _agent(llm, *, profile="alice", tools=None, mode="reasoning", plan_phase=None, disabled=None):
    registry = _Registry({profile: tools if tools is not None else [_docs_group()]}, disabled=disabled)
    return ra.ReasoningAgent(llm=llm, registry=registry, profile=profile, context_id=f"ctx-{profile}",
                             mode=mode, plan_phase=plan_phase)


def _run(agent, query="Please search in my documents and explain what the multiple agent mode of OpenClaw is."):
    async def go():
        return [c async for c in agent.run(query, history_messages=[])]
    return asyncio.run(go())


def _auto_group(messages: list[dict]) -> tuple[dict, list[dict]]:
    """The agent's own tool-call group in a request, and its results."""
    for i, m in enumerate(messages):
        calls = m.get("tool_calls") or []
        if m.get("role") == "assistant" and calls and all(c["id"].startswith("call_docreview_") for c in calls):
            ids = {c["id"] for c in calls}
            return m, [r for r in messages[i + 1:] if r.get("role") == "tool" and r.get("tool_call_id") in ids]
    raise AssertionError("no automatic review in this request")


def _tok(row, chunk) -> str:
    return make_token(row["cite_id"], chunk["text_hash"])


SEARCH_CALL = [(SEARCH, {"query": "OpenClaw multiple agent mode"})]


def test_the_incident_both_guides_reach_the_next_model_response(env):
    g = env.guides
    llm = _LLM([SEARCH_CALL, "answer"])
    chunks = _run(_agent(llm))

    second = llm.main[1]["messages"]
    call, results = _auto_group(second)
    # The English guide ranked first: it is read first.
    assert [json.loads(c["function"]["arguments"])["file"] for c in call["tool_calls"]] == [
        _tok(g.en, g.en_multi), _tok(g.vi, g.vi_multi)]
    assert [c["function"]["name"] for c in call["tool_calls"]] == [READ, READ]
    # The whole matching passage of each guide is in what the model reads.
    both = "\n".join(r["content"] for r in results)
    for passage in (G.EN_MULTI_AGENT, G.VI_MULTI_AGENT):
        assert " ".join(passage.split()) in " ".join(both.split())
    assert "Automatic document review" in results[0]["content"]
    assert "attribute any difference" in results[-1]["content"]
    # The reads fit their share of the allowance, within the profile's budget.
    assert all(0 < max_tokens <= 3900 for _p, _a, max_tokens in env.reads)
    done = chunks[-1]
    assert done["type"] == T.DONE
    review = done["document_review"]
    assert review["examined"] == [g.en["cite_id"], g.vi["cite_id"]]
    assert review["automatic"]["calls"] == 2 and review["automatic"]["tokens"] <= dr.TOKEN_ALLOWANCE
    # No model request was spent on it: one search step, one answer.
    assert len(llm.main) == 2


def test_the_activity_trace_shows_the_reads_as_the_agents_own(env):
    llm = _LLM([SEARCH_CALL, "answer"])
    chunks = _run(_agent(llm))
    thinking = [c["data"] for c in chunks if c["type"] == T.THINKING_ARTIFACT]
    auto = [t for t in thinking if t.get("Origin") == dr.ORIGIN]
    assert [t["Tool"] for t in auto] == [READ, READ]
    assert all(t["Token_Usage"] is None and t["Step"] == 1 for t in auto)
    results = {c["data"]["Call_Id"] for c in chunks if c["type"] == T.RESULT_ARTIFACT}
    assert {t["Call_Id"] for t in auto} <= results


def test_the_tool_history_pairs_every_call_and_replays(env):
    from app.lib.llm.anthropic import _convert_messages

    llm = _LLM([SEARCH_CALL, "answer"])
    done = _run(_agent(llm))[-1]
    trace = done["llm_messages"]
    ids = [c["id"] for m in trace if m.get("role") == "assistant" for c in (m.get("tool_calls") or [])]
    answered = [m["tool_call_id"] for m in trace if m.get("role") == "tool"]
    assert len(ids) == len(set(ids)) and sorted(ids) == sorted(answered)
    assert ra.ReasoningAgent._normalize_turn_messages(trace) == trace
    # And the shape converts for a provider that pairs tool_use with tool_result.
    _system, converted = _convert_messages(trace)
    uses = [b["id"] for m in converted if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]
    results = [b["tool_use_id"] for m in converted if m["role"] == "user" and isinstance(m["content"], list)
               for b in m["content"] if b.get("type") == "tool_result"]
    assert sorted(uses) == sorted(results)


def test_the_reverse_order_and_a_vietnamese_question_review_the_same_guides(env):
    g = env.guides
    env.engines["alice"] = G.engine(g, vector_hits=[(g.vi_multi["id"], 0.9), (g.en_multi["id"], 0.85)],
                                    profile="alice")
    llm = _LLM([[(SEARCH, {"query": "chế độ nhiều agent của OpenClaw"})], "câu trả lời"])
    _run(_agent(llm), query="Tìm trong tài liệu của tôi: chế độ nhiều agent của OpenClaw là gì?")
    call, _results = _auto_group(llm.main[1]["messages"])
    files = [json.loads(c["function"]["arguments"])["file"] for c in call["tool_calls"]]
    assert {f.split("#")[0] for f in files} == {f"[doc:{g.vi['cite_id']}", f"[doc:{g.en['cite_id']}"}
    assert files[0].startswith(f"[doc:{g.vi['cite_id']}#")  # its result order, not a language preference


def test_a_passage_the_model_already_read_is_not_read_again(env):
    g = env.guides
    llm = _LLM([SEARCH_CALL + [(READ, {"file": _tok(g.en, g.en_multi)})], "answer"])
    _run(_agent(llm))
    call, _ = _auto_group(llm.main[1]["messages"])
    assert [json.loads(c["function"]["arguments"])["file"] for c in call["tool_calls"]] == [_tok(g.vi, g.vi_multi)]
    assert [a["file"] for _p, a, _m in env.reads].count(_tok(g.en, g.en_multi)) == 1


def test_a_search_restricted_to_one_guide_reads_nothing_more(env):
    g = env.guides
    llm = _LLM([[(SEARCH, {"query": "OpenClaw multiple agent mode",
                           "filters": {"file_ids": [g.vi["cite_id"]]}})], "answer"])
    done = _run(_agent(llm))[-1]
    assert not [m for m in llm.main[1]["messages"] if any(
        c["id"].startswith("call_docreview_") for c in (m.get("tool_calls") or []))]
    assert env.reads == []
    assert done["document_review"]["eligible"] == 0


@pytest.mark.parametrize("group_by", ["chunk", "folder"])
def test_chunk_and_folder_results_review_each_file_once(env, group_by):
    g = env.guides
    llm = _LLM([[(SEARCH, {"query": "OpenClaw multiple agent mode", "group_by": group_by})], "answer"])
    _run(_agent(llm))
    call, _ = _auto_group(llm.main[1]["messages"])
    fids = [json.loads(c["function"]["arguments"])["file"].split("#")[0] for c in call["tool_calls"]]
    assert sorted(fids) == sorted({f"[doc:{g.en['cite_id']}", f"[doc:{g.vi['cite_id']}"})


def test_repeated_searches_stay_within_four_reads_and_the_allowance(env):
    g = env.guides
    extra = []
    for i in range(4):
        row = G._add(g.db, f"Guides/guide-{i}.pdf", [G._page(G.EN_MULTI_AGENT.replace("OpenClaw", f"Claw{i}"), 1)])
        extra.append(G.body(g.db, row)[0])
    hits = [(c["id"], 0.9 - 0.01 * n) for n, c in enumerate([g.en_multi, g.vi_multi] + extra)]
    env.engines["alice"] = G.engine(g, vector_hits=hits, profile="alice")
    llm = _LLM([SEARCH_CALL, [(SEARCH, {"query": "multiple agents configuration"})], "answer"])
    review = _run(_agent(llm))[-1]["document_review"]
    assert review["automatic"]["calls"] <= dr.MAX_AUTO_READS
    assert review["automatic"]["tokens"] <= dr.TOKEN_ALLOWANCE
    auto_reads = [m for m in llm.main[-1]["messages"] if m.get("role") == "tool"
                  and m["tool_call_id"].startswith("call_docreview_")]
    assert len(auto_reads) == review["automatic"]["calls"] <= 4
    assert set(review["reasons"].values()) <= {dr.REASON_CALL_LIMIT, dr.REASON_TOKEN_BUDGET}


def test_examined_but_uncited_sources_gain_no_citation(env):
    g = env.guides
    answer = f"OpenClaw can run multiple instances {_tok(g.en, g.en_multi)}."
    review = _run(_agent(_LLM([SEARCH_CALL, answer])))[-1]["document_review"]
    assert review["examined"] == [g.en["cite_id"], g.vi["cite_id"]]
    assert review["cited"] == [g.en["cite_id"]]


# ── what stops it ──────────────────────────────────────────────────────────


@pytest.fixture
def bound_run():
    from app.events import task_result_inbox
    from app.utils.task_context import current_task_id_var

    task_result_inbox.clear_all()
    run_id, conv = "msg:conv-dr:1", "conv-dr"
    task_result_inbox.bind_run(run_id, conv)
    token = current_task_id_var.set(run_id)
    yield SimpleNamespace(conv=conv, run_id=run_id, inbox=task_result_inbox)
    current_task_id_var.reset(token)
    task_result_inbox.clear_all()


def _no_review(llm) -> bool:
    return not any(c["id"].startswith("call_docreview_") for call in llm.main for m in call["messages"]
                   for c in (m.get("tool_calls") or []))


def test_new_user_input_goes_first_and_drops_the_candidates(env, bound_run):
    def on_call(n):
        if n == 1:
            bound_run.inbox.park_user_message_if_bound(bound_run.conv, {
                "message_id": "m1", "text": "only use the Vietnamese guide", "agent_text": "only use the Vietnamese guide"})

    llm = _LLM([SEARCH_CALL, "answer"], on_call=on_call)
    review = _run(_agent(llm))[-1]["document_review"]
    assert _no_review(llm) and env.reads == []
    assert "only use the Vietnamese guide" in json.dumps(llm.main[1]["messages"], ensure_ascii=False)
    assert set(review["reasons"].values()) == {dr.REASON_NEW_INPUT}


def test_an_automation_result_landing_does_not_cancel_the_review(env, bound_run):
    """A task notice changes nothing about the question: the reads still run,
    after the reply to the notice — which stays before them in the trace, as
    the user saw it."""
    def on_call(n):
        if n == 1:
            bound_run.inbox.park_if_bound(bound_run.conv, {"label": "nightly report", "status_word": "succeeded",
                                                           "once": False})

    llm = _LLM([SEARCH_CALL, "answer"], on_call=on_call, ack_text="The nightly report just finished.")
    done = _run(_agent(llm))[-1]
    call, results = _auto_group(llm.main[1]["messages"])
    assert call["content"] == "The nightly report just finished." and len(results) == 2
    assert done["document_review"]["automatic"]["calls"] == 2
    # The model's own next message does not repeat the reply.
    trace = done["llm_messages"]
    assert sum("nightly report just finished" in (m.get("content") or "") for m in trace
               if m.get("role") == "assistant") == 1


def test_a_research_job_in_the_same_batch_takes_over(env):
    docs = _docs_group()
    research = docs._adapter._tools_by_name["research"]

    async def fake_research(arguments):
        from app.tools.builtin.base import BuiltInToolResult
        return BuiltInToolResult(content=[{"type": "text", "text": "research job started"}])

    research.run = fake_research
    llm = _LLM([SEARCH_CALL + [("documentation_search__research", {"question": "multi-agent"})], "answer"])
    review = _run(_agent(llm, tools=[docs]))[-1]["document_review"]
    assert _no_review(llm) and env.reads == []
    assert set(review["reasons"].values()) == {dr.REASON_RESEARCH}


def test_instant_mode_keeps_its_one_round(env):
    llm = _LLM([SEARCH_CALL, "answer"])
    review = _run(_agent(llm, mode="instant"))[-1]["document_review"]
    assert _no_review(llm) and env.reads == []
    assert llm.main[1]["tool_choice"] == "none"
    assert set(review["reasons"].values()) == {dr.REASON_INSTANT}


def test_a_disabled_read_is_never_run_indirectly(env):
    llm = _LLM([SEARCH_CALL, "answer"])
    review = _run(_agent(llm, disabled={"alice": {"documentation_search": {"read"}}}))[-1]["document_review"]
    assert _no_review(llm) and env.reads == []
    assert set(review["reasons"].values()) == {dr.REASON_READ_UNAVAILABLE}


def test_a_plan_question_parks_the_turn_before_any_read(env, bound_run):
    from app.agent import plan_state

    async def ask(leaf, args):
        plan_state.mark_questions(bound_run.run_id, {"questions": [{"id": "q0", "question": "Which guide?"}]})

    asker = _Group("ask_user_question", "ask_user_question", on_run=ask)
    llm = _LLM([SEARCH_CALL + [("ask_user_question", {})]])
    try:
        chunks = _run(_agent(llm, tools=[_docs_group(), asker], mode="plan", plan_phase="planning"))
    finally:
        plan_state.clear(bound_run.run_id)
    assert len(llm.main) == 1 and env.reads == []
    assert "Which guide?" in chunks[-1]["data"]
    assert set(chunks[-1]["document_review"]["reasons"].values()) == {dr.REASON_NO_STEP}


def test_cancellation_stops_the_reads(env):
    from app.tools.builtin import documentation_search as ds

    started = asyncio.Event()
    real = ds.execute

    async def slow(leaf, profile, args, **kw):
        if leaf == "read":
            started.set()
            await asyncio.sleep(30)
        return await real(leaf, profile, args, **kw)

    llm = _LLM([SEARCH_CALL, "answer"])
    agent = _agent(llm)

    async def go():
        import unittest.mock as um

        with um.patch.object(ds, "execute", slow):
            task = asyncio.ensure_future(_collect(agent))
            await asyncio.wait_for(started.wait(), 10)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(go())
    assert len(llm.main) == 1  # no model request after the cancelled read


async def _collect(agent):
    return [c async for c in agent.run("q", history_messages=[])]


# ── isolation ──────────────────────────────────────────────────────────────


def test_two_profiles_in_parallel_share_nothing(env):
    from app.documents.index import IndexDB

    # Bob has only the English guide: one relevant file, nothing to weigh.
    bob_db = IndexDB.open(str(env.tmp / "bob.db"), profile_uid="u-bob")
    bob_en_row = G._add(bob_db, G.EN_NAME, G.english_blocks())
    bob_guides = SimpleNamespace(db=bob_db, en=bob_en_row)
    try:
        G.activate_vectors(bob_guides.db)
        bob_en = G.page_chunk(bob_guides.db, bob_guides.en, 10, contains="Step 11")
        env.engines["bob"] = G.engine(bob_guides, vector_hits=[(bob_en["id"], 0.9)], profile="bob")
        alice_llm = _LLM([SEARCH_CALL, "answer"])
        bob_llm = _LLM([SEARCH_CALL, "answer"])
        alice = _agent(alice_llm, profile="alice")
        bob = _agent(bob_llm, profile="bob")

        async def both():
            return await asyncio.gather(_collect(alice), _collect(bob))

        a_chunks, b_chunks = asyncio.run(both())
        assert alice._doc_review is not bob._doc_review and ra.ReasoningAgent._doc_review is None
        # Alice's two guides were reviewed from her index only; Bob's single
        # relevant file triggered nothing, and none of Alice's tokens reached him.
        read_profiles = {p for p, _a, _m in env.reads}
        assert read_profiles == {"alice"}
        assert a_chunks[-1]["document_review"]["automatic"]["calls"] == 2
        assert b_chunks[-1]["document_review"]["automatic"]["calls"] == 0
        alice_fids = {env.guides.en["cite_id"], env.guides.vi["cite_id"]}
        assert not any(fid in json.dumps(bob_llm.calls) for fid in alice_fids)
    finally:
        bob_guides.db.close()


# ── the prompt ─────────────────────────────────────────────────────────────


def test_the_guidance_asks_to_weigh_every_relevant_file(env):
    agent = _agent(_LLM([]))
    rules = agent._documentation_search_guidance
    assert "When the results come from several files, assess each relevant one before answering" in rules
    assert "`documentation_search__read`" in rules and "[doc:…#…] passage token" in rules
    assert "attribute any difference" in rules and "say so when a relevant file could not be read" in rules
    assert "name_query or path_glob for a file they name" in rules and "stay within it" in rules
    assert "at least" not in rules  # no citation quota
    # Without the read function the sentence names none.
    no_read = _agent(_LLM([]), disabled={"alice": {"documentation_search": {"read"}}})
    assert "`documentation_search__read`" not in no_read._build_instruction()
    assert "assess each relevant one before answering, then combine" in no_read._documentation_search_guidance
