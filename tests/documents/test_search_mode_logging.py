"""Which search path ran is visible in the default log.

Every per-path log line in ``search()`` used to be DEBUG, while the file sink
defaults to INFO — so ``logs/app.log`` never said whether documentation search
was ranking by vector similarity or scanning everything. Dozens of degraded
searches left no trace at all. The mode is now logged once per transition (so a
steady state stays quiet) and exposed for the tool's per-search summary line.
"""

from __future__ import annotations

from app.config.embedding_state import embedding_state
from app.documents.sync import COLLECTION_NAME, DocumentSyncService
from app.utils.logger import logger

_DOC = '---\ndescription: "Widgets."\n---\n\nBody.\n'


class _Store:
    def __init__(self, *, has_collection=True, unreachable=False):
        self.has_collection = has_collection
        self.unreachable = unreachable
        self._client = self

    def collection_exists(self, name):
        # Like both real adapters: any failure reads as "missing".
        return False if self.unreachable else self.has_collection

    def list_collections(self):
        if self.unreachable:
            raise ConnectionError("store unreachable")
        return [COLLECTION_NAME] if self.has_collection else []

    def query_by_vector(self, *, collection_name, vector, limit, filter=None):
        return [{"name": "hit", "score": 0.5}]


class _Emb:
    def embed_query(self, text):
        return [0.0, 1.0]


def _svc(tmp_path):
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "widgets.md").write_text(_DOC, encoding="utf-8")
    return DocumentSyncService(working_dir=tmp_path)


def _capture(fn):
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="INFO")
    try:
        fn()
    finally:
        logger.remove(sink_id)
    return [m for m in messages if "search mode ->" in m]


def test_one_line_per_transition_and_silence_in_steady_state(tmp_path):
    svc = _svc(tmp_path)

    first = _capture(lambda: svc.search(query="q", profile="admin"))
    again = _capture(lambda: svc.search(query="q", profile="admin"))

    assert len(first) == 1 and "fallback-disabled" in first[0]
    assert again == []


def test_transition_to_vector_is_logged_with_the_previous_mode(tmp_path):
    svc = _svc(tmp_path)
    svc.search(query="q", profile="admin")
    embedding_state.mark_ready(_Emb(), _Store())

    lines = _capture(lambda: svc.search(query="q", profile="admin"))

    assert len(lines) == 1
    assert "-> vector" in lines[0] and "was fallback-disabled" in lines[0]


def test_missing_collection_is_named_in_the_transition(tmp_path):
    svc = _svc(tmp_path)
    embedding_state.mark_ready(_Emb(), _Store(has_collection=False))

    lines = _capture(lambda: svc.search(query="q", profile="admin"))

    assert len(lines) == 1 and "fallback-no-collection" in lines[0]
    assert svc.last_search_mode == "fallback-no-collection"


def test_an_unreachable_store_is_reported_as_an_error_not_a_missing_collection(tmp_path):
    """The adapters' collection_exists turns any failure into False, which used
    to be logged as "collection not built yet" — sending an operator after a
    rebuild when the store is simply down."""
    svc = _svc(tmp_path)
    embedding_state.mark_ready(_Emb(), _Store(unreachable=True))

    lines = _capture(lambda: svc.search(query="q", profile="admin"))

    assert svc.last_search_mode == "fallback-error"
    assert len(lines) == 1 and "unreachable" in lines[0]
