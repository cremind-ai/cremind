"""What a Documentation Search result delivers, measured from its final text.

A result is fitted to a token budget, so what it could show and what it did
show differ. These tests pin that the delivery record (and the citations
registered for the answer) describe the text as sent:

- a search reports only the passages its text prints, each as a match or as
  context, whole or cut, under the file it belongs to — whatever the grouping;
- a read centred on a passage (its token, or ``around``) puts that passage
  first: a long passage before it can no longer push it off the first page,
  and one too long for any page comes as a marked excerpt with the rest on
  the next page;
- every result fits the exact limit — header, data block and footer counted —
  down to budgets smaller than a header, in Vietnamese too;
- the tokens registered are exactly the passages whose text is shown, with the
  text shown as their snippet; a token in a navigation line is not delivered;
- a stale token, a file without text and a changed file never count as a
  passage read;
- ``SectionNotFound`` suggests the heading the index really has.

The fixtures are the two small OpenClaw guides of the multi-agent incident
(:mod:`tests.documents._openclaw_guides`).
"""

from __future__ import annotations

import re

import pytest

from app.documents.cite import TOKEN_RE, make_token
from app.documents.delivery import (
    FOCUS_COMPLETE,
    FOCUS_OMITTED,
    FOCUS_PARTIAL,
    FOCUS_UNRESOLVED,
    ROLE_CONTEXT,
    ROLE_MATCH,
)
from app.documents.query import ReadError
from app.documents.query.reader import ReadOutcome
from app.documents.query.render import RenderContext, render_read, render_search
from app.documents.types import ANCHOR_HARD, Block
from app.tools.builtin.cremind_documentation_search import _cut_tokens, _fit_lines, _tokens
from app.tools.builtin.external_content import wrap_document_content
from tests.documents import _openclaw_guides as G

_DOC_BLOCK = re.compile(r"<<<USER_DOCUMENT_CONTENT[^>]*>>>\n(.*?)\n<<<END_USER_DOCUMENT_CONTENT", re.S)


def _ctx(limit: int | None) -> RenderContext:
    """The agent's own measures: the tokenizer its clamp counts with."""
    return RenderContext(limit=limit, tokens=_tokens, fit_lines=_fit_lines, cut_tokens=_cut_tokens,
                         wrap=wrap_document_content)


@pytest.fixture
def guides(tmp_path):
    g = G.build(str(tmp_path / "index.db"))
    G.activate_vectors(g.db)
    g.en_multi = G.page_chunk(g.db, g.en, 10, contains="Step 11")
    g.vi_multi = G.page_chunk(g.db, g.vi, 43)
    yield g
    g.db.close()


def _search(g, query="OpenClaw multiple agent mode", **kw):
    engine = G.engine(g, vector_hits=[(g.en_multi["id"], 0.9), (g.vi_multi["id"], 0.85)])
    return engine.search(query, **kw)


def _token(row, chunk) -> str:
    return make_token(row["cite_id"], chunk["text_hash"])


def _body(text: str) -> str:
    m = _DOC_BLOCK.search(text)
    return m.group(1) if m else ""


def _outside(text: str) -> str:
    return _DOC_BLOCK.sub("", text)


def _passage_tokens(text: str) -> set[str]:
    return {m.group(0) for m in TOKEN_RE.finditer(_body(text)) if m.group(2)}


def _assert_parity(rendered) -> None:
    """Every passage token printed in the data block is registered, every
    registered passage is printed there, and the delivery record agrees.

    Outside the data block the only passage token ever printed is the one a
    read was asked for ("Showing: around [doc:…]") — a navigation line, which
    delivers nothing: that token counts only if the block shows its text."""
    printed = _passage_tokens(rendered.text)
    registered = {c.token for c in rendered.citations if c.text_hash}
    assert printed == registered
    assert {p.token for p in rendered.evidence.passages} == printed
    outside = {m.group(0) for m in TOKEN_RE.finditer(_outside(rendered.text)) if m.group(2)}
    assert outside <= {rendered.evidence.focus}
    if rendered.evidence.focus and rendered.evidence.focus not in printed:
        assert rendered.evidence.focus_status in (FOCUS_OMITTED, FOCUS_UNRESOLVED)


# ── search ─────────────────────────────────────────────────────────────────


def test_search_reports_matches_and_context_as_printed(guides):
    rendered = render_search(_search(guides), _ctx(3900))
    _assert_parity(rendered)
    ev = rendered.evidence
    assert [s.fid for s in ev.sources] == [guides.en["cite_id"], guides.vi["cite_id"]]
    by_token = {p.token: p for p in ev.passages}
    en = by_token[_token(guides.en, guides.en_multi)]
    vi = by_token[_token(guides.vi, guides.vi_multi)]
    # Both guides' matching passages were only shown in part: long pages.
    assert (en.role, en.confidence, en.complete, en.order) == (ROLE_MATCH, "high", False, 1)
    assert (vi.role, vi.confidence, vi.complete, vi.order) == (ROLE_MATCH, "high", False, 2)
    assert any(p.role == ROLE_CONTEXT for p in ev.passages)
    assert rendered.data["delivery"]["passages"] and rendered.data["delivery"]["truncated"] is False


def test_a_tight_budget_reports_only_what_survives(guides):
    full = render_search(_search(guides), _ctx(3900))
    tight = render_search(_search(guides), _ctx(450))
    assert _tokens(tight.text) <= 450
    _assert_parity(tight)
    assert tight.evidence.truncated
    assert len(tight.evidence.passages) < len(full.evidence.passages)
    # The structured items still list both passages of each result; the ones
    # the text left out say so.
    listed = [p for item in tight.data["items"] for p in item["passages"]]
    delivered = {p.token for p in tight.evidence.passages}
    assert any(p["visible"] is False for p in listed) and any(p["visible"] for p in listed)
    assert {p["token"] for p in listed if p["visible"]} == {p["token"] for p in listed} & delivered


def test_a_search_snippet_registers_the_text_it_showed(guides):
    rendered = render_search(_search(guides, query="ClawLite switch-agent personal list-agents"), _ctx(3900))
    body = _body(rendered.text)
    for c in rendered.citations:
        if not c.text_hash:
            continue
        # The snippet recorded for the answer's source chip is (part of) the
        # line the model read, not the passage's first 800 characters.
        assert c.snippet and c.snippet[:60] in body


def test_several_files_ask_to_assess_each_one_and_one_file_does_not(guides):
    two = render_search(_search(guides), _ctx(3900)).text
    assert "These results come from 2 files" in two and "attribute any difference" in two
    one = render_search(_search(guides, filters={"file_ids": [guides.en["cite_id"]]}), _ctx(3900))
    assert [s.fid for s in one.evidence.sources] == [guides.en["cite_id"]]
    assert "come from" not in one.text and "Read more of a file with documentation_search__read" in one.text


def test_a_table_of_contents_is_not_substantive_evidence(guides):
    """The guide's contents page names "Phần 9: Multi-Agent - Xây Dựng Đội AI"
    and so matches the query — but says nothing about it. It is printed and
    citable like any passage, never a passage that counts as evidence."""
    from app.documents.query.render import _looks_like_contents

    toc = G.page_chunk(guides.db, guides.vi, 2)
    assert _looks_like_contents(toc)
    assert not _looks_like_contents(guides.vi_multi) and not _looks_like_contents(guides.en_multi)
    rendered = render_search(_search(guides, query="Phần 9 Multi-Agent Xây Dựng Đội AI"), _ctx(3900))
    by_token = {p.token: p for p in rendered.evidence.passages}
    assert by_token[_token(guides.vi, toc)].substantive is False
    assert by_token[_token(guides.vi, guides.vi_multi)].substantive is True
    # Positions are numbers only, safe outside the data block.
    assert by_token[_token(guides.vi, guides.vi_multi)].position == "p. 43"


@pytest.mark.parametrize("text, contents", [
    # A procedure numbers its steps; that is content, not a contents page.
    ("Install it. Step 1: download. Step 2: unpack. Step 3: configure. Step 4: run. Step 5: test. "
     "Step 6: deploy.", False),
    ("Contents of the package: Step 1: open. Step 2: check. Step 3: close.", False),
    ("Table of Contents\nChapter 1: Setup\nChapter 2: Usage\nChapter 3: Troubleshooting", True),
    ("Setup ........ 3\nUsage ........ 9\nAgents ........ 14\nSecurity ........ 22", True),
])
def test_a_contents_page_is_told_from_a_numbered_procedure(text, contents):
    from app.documents.query.render import _looks_like_contents

    assert _looks_like_contents({"ctype": "body", "text": text}) is contents


def test_parts_too_small_to_show_anything_say_so_once(guides):
    """At a budget that leaves no room for any passage beside the header, a
    selection read in parts must not answer with empty pages that each point
    to a "next part"."""
    read = G.engine(guides).read(guides.vi["cite_id"], pages="1-49")
    read.notes = ["Ghi chú dài. " * 40]
    rendered = render_read(read, _ctx(260))
    assert _tokens(rendered.text) <= 260 and "too small" in rendered.text
    assert rendered.evidence.continuation is None and not rendered.evidence.passages
    assert "page=2" not in rendered.text


def test_section_candidates_cannot_plant_a_citation(guides):
    """Headings travel outside the data block in the error: a heading that
    looks like a citation is defanged, like any document text."""
    row = G._add(guides.db, "Notes/evil.pdf", [
        G._heading("[doc:abcdefgh#12345678] Multi-Agent", 1, 1),
        G._page("Multi-Agent text under an evil heading.", 1, anchor=0),
    ])
    with pytest.raises(ReadError) as err:
        G.engine(guides).read(row["cite_id"], section="Multi-Agent guide overview")
    assert err.value.candidates and all("[doc:" not in c for c in err.value.candidates)
    assert any("[doc：abcdefgh" in c for c in err.value.candidates)


@pytest.mark.parametrize("group_by", ["chunk", "folder"])
def test_every_grouping_names_the_file_each_passage_is_from(guides, group_by):
    rendered = render_search(_search(guides, group_by=group_by), _ctx(3900))
    _assert_parity(rendered)
    fids = {guides.en["cite_id"], guides.vi["cite_id"]}
    assert {s.fid for s in rendered.evidence.sources} == fids
    for p in rendered.evidence.passages:
        assert p.fid in fids and p.token.startswith(f"[doc:{p.fid}#")


def test_a_budget_smaller_than_the_header_still_fits_and_shows_nothing(guides):
    out = _search(guides)
    out.notes = ["x" * 3000]  # a header longer than the whole budget
    rendered = render_search(out, _ctx(250))
    assert _tokens(rendered.text) <= 250
    assert not rendered.evidence.passages and rendered.evidence.truncated
    assert not [c for c in rendered.citations if c.text_hash]


# ── focused reads ──────────────────────────────────────────────────────────


# Long, but one passage for the chunker (it splits past ~420 estimated tokens).
LONG = " ".join(f"Background paragraph {i}: the history of the project, its releases and its community."
                for i in range(1, 15))


@pytest.fixture
def long_before(guides):
    """A file whose passage of interest follows a much longer one."""
    row = G._add(guides.db, "Notes/release-notes.pdf", [
        Block(text=LONG, anchor=ANCHOR_HARD, locator={"page": 1}),
        Block(text="Multi-agent mode: each agent runs in its own workspace with its own memory, and "
                   "agents pass tasks to each other through sessions. " * 3, anchor=ANCHOR_HARD,
              locator={"page": 2}),
        Block(text="Upgrade notes: back up every workspace before you update. " * 4, anchor=ANCHOR_HARD,
              locator={"page": 3}),
    ])
    chunks = G.body(guides.db, row)
    assert len(chunks) == 3 and _tokens(chunks[0]["text"]) > 2 * _tokens(chunks[1]["text"])
    return row, chunks


def test_a_long_passage_before_cannot_push_the_focus_off_the_first_page(guides, long_before):
    row, (before, focus, after) = long_before
    engine = G.engine(guides)
    token = _token(row, focus)
    read = engine.read(token)
    whole = render_read(read, _ctx(None))
    budget = _tokens(whole.text) - _tokens(before["text"]) // 2  # everything but most of `before`
    first = render_read(read, _ctx(budget))
    assert _tokens(first.text) <= budget
    ev = first.evidence
    assert ev.focus == token and ev.focus_status == FOCUS_COMPLETE
    printed = _passage_tokens(first.text)
    assert token in printed and _token(row, before) not in printed
    assert "Left out here to fit the budget: the passage before it (p. 1)" in first.text
    assert ev.continuation == {"page": 2} and ev.truncated
    _assert_parity(first)
    # The passage before it follows on the next page.
    second = render_read(engine.read(token, page=2), _ctx(budget))
    assert _tokens(second.text) <= budget
    assert _passage_tokens(second.text) == {_token(row, before)}
    assert second.evidence.focus_status == FOCUS_OMITTED


def test_neighbours_that_fit_are_shown_in_reading_order(guides, long_before):
    row, (before, focus, after) = long_before
    rendered = render_read(G.engine(guides).read(_token(row, focus)), _ctx(5000))
    body = _body(rendered.text)
    positions = [body.index(_token(row, c)) for c in (before, focus, after)]
    assert positions == sorted(positions)
    assert rendered.evidence.focus_status == FOCUS_COMPLETE and rendered.evidence.shape == "whole"


def test_an_oversized_focus_comes_as_a_marked_excerpt_with_the_rest_next(guides, long_before):
    row, (before, focus, after) = long_before
    engine = G.engine(guides)
    token = _token(row, before)  # the long passage is the one asked for now
    # Below the room for a header and a few words, nothing is passed off as read.
    tiny = render_read(engine.read(token), _ctx(200))
    assert _tokens(tiny.text) <= 200 and "too small" in tiny.text
    assert tiny.evidence.focus_status == FOCUS_OMITTED and not tiny.evidence.passages
    budget = 300
    pages = []
    for page in range(1, 12):
        rendered = render_read(engine.read(token, page=page), _ctx(budget))
        assert _tokens(rendered.text) <= budget
        _assert_parity(rendered)
        pages.append(rendered)
        if rendered.evidence.continuation is None:
            break
    first = pages[0]
    assert first.evidence.focus_status == FOCUS_PARTIAL and first.evidence.continuation == {"page": 2}
    assert "(beginning of the passage)" in first.text and "the rest of the passage asked for" in first.text
    # The pieces, in order, are the whole passage: nothing is lost between pages.
    pieces = [r for r in pages if r.evidence.passages and r.evidence.passages[0].token == token]
    assert len(pieces) >= 2 and all(not r.evidence.passages[0].complete for r in pieces)
    text = " ".join(_body(r.text).split("\n", 1)[1].replace("…", " ") for r in pieces)
    assert " ".join(text.split()) == " ".join(before["text"].split())


@pytest.mark.parametrize("budget", [200, 300, 450, 700, 1200])
def test_every_read_shape_fits_the_exact_limit_in_vietnamese(guides, budget):
    engine = G.engine(guides)
    vi = guides.vi
    reads = [
        engine.read(_token(vi, guides.vi_multi)),               # focus
        engine.read(vi["cite_id"], pages="1-49"),               # parts
        engine.read(vi["cite_id"], query="Multi-Agent"),        # envelope
        engine.read(vi["cite_id"], section="PHẦN 9"),           # a section
    ]
    for read in reads:
        for page in (1, 2):
            read.page = page
            rendered = render_read(read, _ctx(budget))
            assert _tokens(rendered.text) <= budget, (read.selection, page, _tokens(rendered.text))
            _assert_parity(rendered)
            assert rendered.evidence.rendered_tokens == _tokens(rendered.text)


def test_a_long_header_is_counted_too(guides):
    read = G.engine(guides).read(_token(guides.vi, guides.vi_multi))
    read.notes = ["Ghi chú rất dài về tệp này. " * 60]
    for budget in (300, 700):
        rendered = render_read(read, _ctx(budget))
        assert _tokens(rendered.text) <= budget
        _assert_parity(rendered)


# ── what never counts as a passage read ────────────────────────────────────


def test_a_stale_token_leaves_the_focus_unresolved(guides):
    gone = f"[doc:{guides.vi['cite_id']}#deadbeef]"
    read = G.engine(guides).read(gone)
    rendered = render_read(read, _ctx(3900))
    assert "no longer in this file" in rendered.text
    assert rendered.evidence.focus == gone and rendered.evidence.focus_status == FOCUS_UNRESOLVED
    assert gone not in {c.token for c in rendered.citations}


def test_a_file_without_text_and_a_changed_file_are_flagged(guides):
    engine = G.engine(guides)
    read = engine.read(guides.en["cite_id"], pages="10")
    stale = ReadOutcome(**{**read.__dict__, "stale": True})
    assert render_read(stale, _ctx(3900)).evidence.stale is True
    card = next(c for c in guides.db.chunks_of_file(int(guides.en["id"])) if c["ctype"] == "file_card")
    bare = ReadOutcome(file=guides.en, card=card, body=[], selected=[], selection=None, toc=[], ranked=[],
                       query=None)
    ev = render_read(bare, _ctx(3900)).evidence
    assert ev.metadata_only is True
    assert all(not p.substantive for p in ev.passages)


# ── the REST query API ─────────────────────────────────────────────────────


def test_the_query_api_adds_a_delivery_object_and_keeps_its_fields(guides, monkeypatch):
    import asyncio
    import json
    from types import SimpleNamespace

    import app.documents.query as query_pkg
    from app.api import documents_query as api
    from app.documents.query.engine import Access

    engine = G.engine(guides, vector_hits=[(guides.en_multi["id"], 0.9), (guides.vi_multi["id"], 0.85)])
    monkeypatch.setattr(query_pkg, "open_engine", lambda profile: Access(engine=engine))
    route = api.get_documents_query_routes()[0].endpoint

    class _Req:
        def __init__(self, leaf, body):
            self.user = SimpleNamespace(is_authenticated=True, username="alice")
            self.path_params = {"leaf": leaf}
            self._body = body

        async def json(self):
            return self._body

    body = json.loads(asyncio.run(route(_Req("search", {"query": "OpenClaw multiple agent mode"}))).body)
    assert {"text", "mode", "coverage", "items", "total"} <= set(body)  # unchanged
    delivery = body["delivery"]
    assert delivery["v"] == 1 and delivery["rendered_tokens"] > 0 and delivery["truncated"] is False
    assert {"token", "role", "complete"} == set(delivery["passages"][0])
    # Unbudgeted (the CLI's default) the read is whole, and says its focus is.
    token = _token(guides.en, guides.en_multi)
    read = json.loads(asyncio.run(route(_Req("read", {"file": token}))).body)["delivery"]
    assert read["focus"] == {"token": token, "status": "complete"}
    assert read["stale"] is False and read["metadata_only"] is False
    # Found files only: nothing in the delivery object is document text.
    assert "OpenClaw" not in json.dumps(delivery) + json.dumps(read)


# ── a section that is not there ────────────────────────────────────────────


def test_section_not_found_suggests_the_heading_the_index_has(guides):
    engine = G.engine(guides)
    with pytest.raises(ReadError) as err:
        # How the guide's table of contents prints the section — not a heading.
        engine.read(guides.vi["cite_id"], section="Phần 9: Multi-Agent - Xây Dựng Đội AI")
    assert err.value.code == "SectionNotFound"
    assert err.value.candidates[0] == "PHẦN 9"
    assert "p. 43" in err.value.message and "[doc:…#…] token" in err.value.message
    assert 'pages="43-44"' in err.value.message
    # Never a silent substitute — but the suggestion works as given.
    ok = engine.read(guides.vi["cite_id"], section=err.value.candidates[0])
    assert {c["locator"]["page"] for c in ok.selected} == {43, 44}
