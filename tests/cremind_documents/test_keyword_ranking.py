"""Keyword seats in the Cremind manual's search.

The bug: asked to "please list all Cremind LLM provider", the agent searched
the manual for "list all configured Cremind LLM providers CLI command" and got
"no relevant result". The vector ranking put ``[cli]cremind llm`` 22nd of 51 —
every CLI page's description shares "Cremind", "CLI", "list" and "command",
and those words dominated the query's embedding — so the page never reached
the relevance judge's top 10. Keyword (BM25) ranking weighs "llm" by its
rarity, and the best keyword matches now take turns with the vector hits in
the candidate list.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config.embedding_state import embedding_state
from app.cremind_documents import lexical
from app.cremind_documents.parser import parse_document
from app.cremind_documents.sync import (
    COLLECTION_NAME,
    SHARED_SCOPE,
    CremindDocumentSyncService,
    _cap_description_for_embedding,
    _clean_name,
)

_BUNDLE = Path(__file__).resolve().parents[2] / "app" / "cremind_documents" / "bundled"
_FAILING_QUERY = "list all configured Cremind LLM providers CLI command"


# ── tokens ──────────────────────────────────────────────────────────────────


def test_tokens_fold_case_markup_inflections_and_stop_words():
    assert lexical.tokens("Please list all `cremind llm` PROVIDERS") == ["list", "cremind", "llm", "provider"]
    assert lexical.tokens("set-args") == ["set", "arg"]


@pytest.mark.parametrize("forms", [
    ("configured", "configure", "configuring", "configures"),
    ("provider", "providers"),
    ("list", "lists", "listed", "listing"),
    ("embed", "embedded", "embedding", "embeddings"),
    ("install", "installed", "installing"),
    ("entry", "entries"),
])
def test_inflections_of_one_word_compare_equal(forms):
    assert len({lexical.tokens(f)[0] for f in forms}) == 1, forms


def test_words_that_only_look_plural_keep_their_ending():
    assert lexical.tokens("process status analysis") == ["process", "status", "analysis"]


def test_vietnamese_words_survive_and_function_words_go():
    assert lexical.tokens("tạo lời nhắc cho tôi") == ["tạo", "lời", "nhắc"]


# ── rank ────────────────────────────────────────────────────────────────────


def _cli_page(group: str, extra: str = "") -> tuple[str, str]:
    """A CLI page as the manual writes them: the shared boilerplate plus
    whatever makes it this page."""
    return (
        f"cremind {group}",
        f"Use the Cremind CLI command `cremind {group}` to list and configure "
        f"{group} settings. {extra}",
    )


_CORPUS = [
    _cli_page("channels", "Connect Telegram and Slack."),
    _cli_page("profile", "Create and delete profiles."),
    _cli_page("llm", "Configure LLM providers and models."),
    _cli_page("backup", "Create and restore backups."),
    _cli_page("tools", "Enable or disable tools."),
    _cli_page("calendar", "Schedule reminders and timers."),
]


def test_a_rare_word_outranks_the_boilerplate_every_page_shares():
    ranked = lexical.rank(_FAILING_QUERY, _CORPUS)

    assert ranked, "the query names the LLM page"
    assert _CORPUS[ranked[0][0]][0] == "cremind llm"


def test_a_query_of_common_words_ranks_nothing():
    """Words at least half the pages contain say nothing about which page is
    meant: such a query is left to the vector ranking."""
    assert lexical.rank("Cremind CLI command to list settings", _CORPUS) == []


def test_a_query_with_no_word_in_common_ranks_nothing():
    assert lexical.rank("xem chi phí tháng này", _CORPUS) == []


def test_a_rare_stop_word_ranks_nothing():
    corpus = [*_CORPUS, ("document", "How to write a documentation file.")]
    assert lexical.rank("how", corpus) == []


def test_the_page_name_outweighs_a_passing_mention():
    corpus = [
        ("cremind group", "Group chats: several profiles share one room."),
        ("cremind channels", "Channels; a Telegram group can be connected too."),
        *_CORPUS[1:4],
    ]
    ranked = lexical.rank("group", corpus)
    assert [corpus[i][0] for i, _ in ranked][:2] == ["cremind group", "cremind channels"]


def test_empty_inputs_rank_nothing():
    assert lexical.rank("", _CORPUS) == []
    assert lexical.rank("llm", []) == []


# ── merge ───────────────────────────────────────────────────────────────────


def _hits(*names: str, **extra) -> list[dict]:
    return [{"id": n, "name": n, **extra} for n in names]


def _merge(dense, keyword, limit):
    return [h["name"] for h in lexical.merge(dense, keyword, limit, key=lambda h: h["id"])]


def test_no_keyword_hits_leaves_the_vector_hits_untouched():
    dense = _hits("a", "b", "c", score=0.9)
    merged = lexical.merge(dense, [], 2, key=lambda h: h["id"])
    assert merged == dense[:2]
    assert merged[0] is dense[0]


def test_the_rankings_take_turns_vector_first():
    dense = _hits(*"abcdefghij")
    assert _merge(dense, _hits("x", "y"), 10) == ["a", "x", "b", "y", "c", "d", "e", "f", "g", "h"]


def test_keyword_places_at_most_half_and_none_of_a_single_seat():
    dense = _hits(*"abcdefghij")
    keyword = _hits("v", "w", "x", "y", "z")
    assert _merge(dense, keyword, 5) == ["a", "v", "b", "w", "c"]
    assert _merge(dense, keyword, 1) == ["a"]


def test_a_page_both_rankings_return_appears_once_as_its_vector_hit():
    dense = _hits("a", "b", "c", score=0.9)
    keyword = _hits("b", "x", match="keyword", score=None)

    merged = lexical.merge(dense, keyword, 4, key=lambda h: h["id"])

    assert [h["name"] for h in merged] == ["a", "b", "c", "x"]
    assert merged[1]["score"] == 0.9 and "match" not in merged[1]
    assert merged[3]["match"] == "keyword"


def test_the_other_ranking_fills_when_one_runs_out():
    assert _merge(_hits("a"), _hits("x", "y", "z"), 3) == ["a", "x", "y"]
    assert _merge(_hits("a", "b", "c", "d"), _hits("x"), 4) == ["a", "x", "b", "c"]


# ── The service ─────────────────────────────────────────────────────────────


def _uid(profile: str) -> str:
    return f"uid-{profile}"


def _page(scope: str, name: str, text: str, pid: int) -> dict:
    return {
        "id": pid, "name": name, "text": text, "scope": scope,
        "relpath": f"{name}.md", "file_path": f"/stale/{name}.md", "content_hash": "h",
    }


class _Store:
    """The vector ranking returns ``vector``; the store holds ``points``."""

    def __init__(self, *, vector: list[dict], points: list[dict], list_error: bool = False):
        self.vector = vector
        self.points = points
        self.list_error = list_error
        self.listed_scopes: list[str] = []
        self._client = self

    def list_collections(self):
        return [COLLECTION_NAME]

    def collection_exists(self, collection_name):
        return True

    def query_by_vector(self, *, collection_name, vector, limit, filter=None):
        scopes = set((filter or {}).get("scope") or [])
        return [dict(h) for h in self.vector if h["scope"] in scopes][:limit]

    def list_all_points(self, *, collection_name, with_vectors=False, filter=None):
        if self.list_error:
            raise ConnectionError("store unreachable")
        scope = (filter or {}).get("scope")
        self.listed_scopes.append(scope)
        return [
            {"id": p["id"], "vector": None, "payload": {k: v for k, v in p.items() if k != "id"}}
            for p in self.points if p["scope"] == scope
        ]


class _Embedder:
    def embed_query(self, text):
        return [0.1, 0.2, 0.3]


def _decoys(scope: str = SHARED_SCOPE) -> list[dict]:
    return [
        _page(scope, f"[cli]cremind {name}", f"The Cremind CLI command `cremind {name}`.", pid)
        for pid, name in enumerate(("profile", "setup", "tools", "server", "agents"), start=1)
    ]


_LLM_PAGE = _page(SHARED_SCOPE, "[cli]cremind llm",
                  "Configure LLM providers and models: list and configure providers.", 99)


def _service(tmp_path) -> CremindDocumentSyncService:
    (tmp_path / "storage" / "cremind_documents" / "shared").mkdir(parents=True)
    return CremindDocumentSyncService(working_dir=tmp_path, profile_uid_resolver=_uid)


def test_search_seats_a_keyword_match_the_vector_ranking_missed(tmp_path):
    svc = _service(tmp_path)
    decoys = _decoys()
    embedding_state.mark_ready(_Embedder(), _Store(vector=decoys, points=[*decoys, _LLM_PAGE]))

    hits = svc.search(query=_FAILING_QUERY, profile="admin", limit=4)

    assert [h["name"] for h in hits][:2] == ["[cli]cremind profile", "[cli]cremind llm"]
    assert len(hits) == 4
    llm = hits[1]
    assert llm["match"] == "keyword" and llm["score"] is None
    # Served from where the page lives now, like any vector hit.
    assert llm["file_path"] == str(svc.shared_dir() / "[cli]cremind llm.md")
    assert svc.last_search_mode == "vector"


def test_the_keyword_corpus_is_only_the_callers_scopes(tmp_path):
    """Profile isolation: bob's search must never rank alice's own pages, even
    when her page is the one that matches his query best."""
    svc = _service(tmp_path)
    alices = _page("alice", "llm notes", "My LLM providers and their API keys.", 7)
    decoys = _decoys()
    store = _Store(vector=decoys, points=[*decoys, alices])
    embedding_state.mark_ready(_Embedder(), store)

    hits = svc.search(query=_FAILING_QUERY, profile="bob", limit=6)

    assert "llm notes" not in [h["name"] for h in hits]
    assert set(store.listed_scopes) == {SHARED_SCOPE, "bob"}

    store.listed_scopes.clear()
    hits = svc.search(query=_FAILING_QUERY, profile="alice", limit=6)
    assert "llm notes" in [h["name"] for h in hits]
    assert set(store.listed_scopes) == {SHARED_SCOPE, "alice"}


def test_a_keyword_ranking_failure_keeps_the_vector_hits(tmp_path):
    svc = _service(tmp_path)
    decoys = _decoys()
    embedding_state.mark_ready(_Embedder(), _Store(vector=decoys, points=decoys, list_error=True))

    hits = svc.search(query=_FAILING_QUERY, profile="admin", limit=3)

    assert [h["name"] for h in hits] == [d["name"] for d in decoys[:3]]
    assert svc.last_search_mode == "vector"


# ── The real manual ─────────────────────────────────────────────────────────


def _bundle_corpus() -> tuple[list[str], list[tuple[str, str]]]:
    names, corpus = [], []
    for path in sorted(_BUNDLE.glob("*.md")):
        parsed = parse_document(path)
        if parsed is None:
            continue
        names.append(path.stem)
        corpus.append((_clean_name(path.stem), _cap_description_for_embedding(parsed.description)))
    return names, corpus


@pytest.mark.parametrize("query, page, within", [
    (_FAILING_QUERY, "[cli]cremind llm", 1),
    ("please list all Cremind LLM provider", "[cli]cremind llm", 1),
    ("Cremind CLI command to schedule a recurring reminder", "[cli]cremind calendar", 3),
    ("Cremind CLI command to view calendar events", "[cli]cremind calendar", 1),
    ("Cremind CLI command to register an MCP server", "[cli]cremind agents", 3),
])
def test_the_bundled_manual_ranks_the_page_a_query_names(query, page, within):
    """Pinned against the real bundle: these are the queries the vector
    ranking alone kept outside the judge's top 10."""
    names, corpus = _bundle_corpus()
    ranked = [names[i] for i, _ in lexical.rank(query, corpus)]
    assert page in ranked[:within], ranked[:5]
