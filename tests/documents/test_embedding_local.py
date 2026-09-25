"""LocalEmbeddings: one lock, interactive priority, adaptive batches, prefixes.

The embedder is one process-wide instance shared by chat (documentation
search, memory) and Documentation search's background indexer. These tests
drive it with a fake provider that fails like a Hugging Face fast tokenizer
("Already borrowed") when two threads use it at once.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import List

import pytest

import app.lib.embedding as emb_mod
from app.embeddings.base import EmbeddingProvider
from app.lib.embedding import LocalEmbeddings, make_model_key
from app.documents import embed_prompts


def _vec(text: str) -> List[float]:
    """A deterministic 4-d 'embedding' of ``text``."""
    return [float(len(text)), float(sum(map(ord, text)) % 97), float(text.count(" ")), 1.0]


class FakeProvider(EmbeddingProvider):
    PROVIDER_KEY = "fake"
    QUERY_PREFIX = "q: "
    PASSAGE_PREFIX = "p: "

    def __init__(self, *, per_item_s: float = 0.0, clock=None) -> None:
        self.calls: list = []
        self.per_item_s = per_item_s
        self.clock = clock
        self._busy = False
        self.gate: threading.Event | None = None     # blocks the first batch
        self.entered = threading.Event()

    def _enter(self):
        if self._busy:
            raise RuntimeError("Already borrowed")
        self._busy = True

    def _work(self, n: int) -> None:
        if self.clock is not None:
            self.clock.advance(self.per_item_s * n)
        elif self.per_item_s:
            time.sleep(self.per_item_s * n)

    def encode(self, text: str) -> List[float]:
        self._enter()
        try:
            self.calls.append(("encode", text))
            self._work(1)
            return _vec(text)
        finally:
            self._busy = False

    def encode_batch(self, texts, batch_size=32, *, prefix=""):
        self._enter()
        try:
            self.calls.append(("batch", list(texts), batch_size, prefix))
            self.entered.set()
            if self.gate is not None:
                gate, self.gate = self.gate, None
                assert gate.wait(10), "test gate never released"
            self._work(len(texts))
            return [_vec(prefix + t) for t in texts]
        finally:
            self._busy = False

    @property
    def dimension(self) -> int:
        return 4

    @property
    def model_name(self) -> str:
        return "org/Fake Model_v1"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _wait_until(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


# ── identity ────────────────────────────────────────────────────────────────


def test_identity_properties():
    e = LocalEmbeddings(provider=FakeProvider())
    assert e.provider_key == "fake"
    assert e.model_name == "org/Fake Model_v1"
    assert e.model_key == "fake_fake-model-v1"
    assert e.dimension == 4


@pytest.mark.parametrize(("provider", "model_id", "key"), [
    ("me5", "intfloat/multilingual-e5-base", "me5_multilingual-e5-base"),
    ("gemma", "google/embeddinggemma-300m", "gemma_embeddinggemma-300m"),
    ("ME5", "some/Model.Name v2", "me5_model-name-v2"),
    ("", "", "local_model"),
])
def test_make_model_key(provider, model_id, key):
    assert make_model_key(provider, model_id) == key


def test_a_provider_without_a_key_falls_back_to_its_class_name():
    class Bare:
        dimension = 2
        model_name = "x/y"

        def encode(self, text):
            return [1.0, 0.0]

    e = LocalEmbeddings(provider=Bare())
    assert e.provider_key == "bare"
    assert e.query_prefix == "" and e.passage_prefix == ""
    # No encode_batch: passages fall back to encode() per text.
    assert e.embed_passages(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]


# ── outputs ─────────────────────────────────────────────────────────────────


def test_historical_methods_keep_their_exact_outputs():
    p = FakeProvider()
    e = LocalEmbeddings(provider=p)
    assert e.embed_documents(["a b", "c"]) == [_vec("a b"), _vec("c")]
    assert e.embed_query("hello") == _vec("hello")
    # No prefixes reach the provider from these two.
    assert all(text in ("a b", "c", "hello") for kind, text, *_ in p.calls if kind == "encode")


def test_documents_methods_add_the_trained_prompts():
    p = FakeProvider()
    e = LocalEmbeddings(provider=p)
    assert e.embed_search_query("puppies") == _vec("q: puppies")
    assert e.embed_passages(["one", "two"], batch_size=8) == [_vec("p: one"), _vec("p: two")]
    assert ("batch", ["one", "two"], 2, "p: ") in p.calls


def test_embed_passages_of_nothing_calls_nothing():
    p = FakeProvider()
    assert LocalEmbeddings(provider=p).embed_passages([]) == []
    assert p.calls == []


def test_a_provider_returning_the_wrong_count_is_an_error():
    p = FakeProvider()
    p.encode_batch = lambda texts, batch_size=32, *, prefix="": [[0.0]]
    with pytest.raises(RuntimeError, match="1 vectors for 2 texts"):
        LocalEmbeddings(provider=p).embed_passages(["a", "b"], batch_size=2)


# ── batching ────────────────────────────────────────────────────────────────


def _batch_sizes(p: FakeProvider) -> List[int]:
    return [len(c[1]) for c in p.calls if c[0] == "batch"]


def test_a_fixed_batch_size_is_honoured():
    p = FakeProvider()
    LocalEmbeddings(provider=p).embed_passages([str(i) for i in range(12)], batch_size=5)
    assert _batch_sizes(p) == [5, 5, 2]


def test_fast_batches_grow_at_most_doubling_up_to_the_cap(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(emb_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    p = FakeProvider(per_item_s=0.0001, clock=clock)
    e = LocalEmbeddings(provider=p)
    e.embed_passages([str(i) for i in range(8 + 16 + 32 + 64 + 64)])
    assert _batch_sizes(p) == [8, 16, 32, 64, 64]


def test_slow_batches_shrink_to_the_time_target(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(emb_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    # 100 ms per passage → 3 per 300 ms batch.
    p = FakeProvider(per_item_s=0.1, clock=clock)
    e = LocalEmbeddings(provider=p)
    e.embed_passages([str(i) for i in range(20)])
    sizes = _batch_sizes(p)
    assert sizes[0] == 8
    assert set(sizes[1:-1]) == {3}
    assert sum(sizes) == 20
    # The learned size carries over to the next call.
    p.calls.clear()
    e.embed_passages([str(i) for i in range(6)])
    assert _batch_sizes(p) == [3, 3]


def test_a_very_slow_model_goes_down_to_single_passages(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(emb_mod, "time", SimpleNamespace(monotonic=clock.monotonic))
    p = FakeProvider(per_item_s=2.0, clock=clock)
    e = LocalEmbeddings(provider=p)
    e.embed_passages([str(i) for i in range(10)])
    assert _batch_sizes(p)[1:] == [1, 1]


# ── thread safety and priority ──────────────────────────────────────────────


def test_concurrent_callers_never_overlap_inside_the_model():
    p = FakeProvider(per_item_s=0.001)
    e = LocalEmbeddings(provider=p)
    errors: list = []

    def run(fn):
        try:
            for _ in range(20):
                fn()
        except Exception as ex:  # noqa: BLE001
            errors.append(ex)

    threads = [
        threading.Thread(target=run, args=(lambda: e.embed_query("q"),)),
        threading.Thread(target=run, args=(lambda: e.embed_search_query("s"),)),
        threading.Thread(target=run, args=(lambda: e.embed_documents(["a", "b"]),)),
        threading.Thread(target=run, args=(lambda: e.embed_passages(["x"] * 5),)),
        threading.Thread(target=run, args=(lambda: e.embed_passages(["y"] * 3, batch_size=2),)),
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
        assert not th.is_alive()
    assert errors == []
    assert e._interactive_waiting == 0


def test_a_query_jumps_ahead_of_the_next_background_batch():
    p = FakeProvider()
    gate = p.gate = threading.Event()
    e = LocalEmbeddings(provider=p)
    out: dict = {}

    bg = threading.Thread(
        target=lambda: out.setdefault("bg", e.embed_passages(["p1", "p2", "p3"], batch_size=1)),
    )
    bg.start()
    assert p.entered.wait(5)  # batch 1 is inside the model, holding the lock

    fg = threading.Thread(target=lambda: out.setdefault("fg", e.embed_search_query("hi")))
    fg.start()
    assert _wait_until(lambda: e._interactive_waiting == 1)

    gate.set()
    bg.join(10)
    fg.join(10)
    assert not bg.is_alive() and not fg.is_alive()

    order = [c[1] if c[0] == "encode" else c[1][0] for c in p.calls]
    assert order == ["p1", "q: hi", "p2", "p3"]
    assert out["fg"] == _vec("q: hi")
    assert out["bg"] == [_vec("p: p1"), _vec("p: p2"), _vec("p: p3")]


def test_embed_query_counts_as_interactive_too():
    p = FakeProvider()
    gate = p.gate = threading.Event()
    e = LocalEmbeddings(provider=p)
    bg = threading.Thread(target=lambda: e.embed_passages(["p1", "p2"], batch_size=1))
    bg.start()
    assert p.entered.wait(5)
    fg = threading.Thread(target=lambda: e.embed_query("plain"))
    fg.start()
    assert _wait_until(lambda: e._interactive_waiting == 1)
    gate.set()
    bg.join(10)
    fg.join(10)
    order = [c[1] if c[0] == "encode" else c[1][0] for c in p.calls]
    assert order == ["p1", "plain", "p2"]


def test_yielding_is_bounded():
    p = FakeProvider()
    e = LocalEmbeddings(provider=p)
    e._YIELD_MAX_WAIT_S = 0.05
    e._interactive_waiting = 1  # a waiter that never arrives
    start = time.monotonic()
    assert e.embed_passages(["a", "b"], batch_size=1) == [_vec("p: a"), _vec("p: b")]
    assert time.monotonic() - start < 2.0


def test_the_interactive_counter_recovers_from_a_failing_model():
    class Boom(FakeProvider):
        def encode(self, text):
            raise ValueError("model exploded")

    e = LocalEmbeddings(provider=Boom())
    with pytest.raises(ValueError):
        e.embed_search_query("x")
    with pytest.raises(ValueError):
        e.embed_query("x")
    assert e._interactive_waiting == 0
    assert not e._lock.locked()


# ── providers ───────────────────────────────────────────────────────────────


def test_the_base_encode_batch_loops_over_encode_with_the_prefix():
    class OnlyEncode(EmbeddingProvider):
        def encode(self, text):
            return _vec(text)

        @property
        def dimension(self):
            return 4

        @property
        def model_name(self):
            return "m"

    assert OnlyEncode().encode_batch(["a", "b"], prefix="x: ") == [_vec("x: a"), _vec("x: b")]
    assert EmbeddingProvider.QUERY_PREFIX == "" and EmbeddingProvider.PASSAGE_PREFIX == ""


class _FakeSTModel:
    def __init__(self) -> None:
        self.calls: list = []

    def encode(self, inputs, **kwargs):
        import numpy as np

        self.calls.append((inputs, kwargs))
        if isinstance(inputs, str):
            return np.array(_vec(inputs))
        return np.array([_vec(t) for t in inputs])


@pytest.mark.parametrize("module_name, cls_name, key", [
    ("app.embeddings.me5", "Me5EmbeddingProvider", "me5"),
    ("app.embeddings.gemma", "GemmaEmbeddingProvider", "gemma"),
])
def test_sentence_transformer_providers_batch_with_prefixes(module_name, cls_name, key):
    pytest.importorskip("sentence_transformers")
    import importlib

    cls = getattr(importlib.import_module(module_name), cls_name)
    provider = cls.__new__(cls)  # skip loading the real model
    provider._model = _FakeSTModel()

    assert cls.PROVIDER_KEY == key
    # The class prompts and the documents table must agree.
    assert cls.QUERY_PREFIX == embed_prompts.query_prefix(key)
    assert cls.PASSAGE_PREFIX == embed_prompts.passage_prefix(key)

    got = provider.encode_batch(["a", "b"], batch_size=7, prefix=cls.PASSAGE_PREFIX)
    assert got == [_vec(cls.PASSAGE_PREFIX + "a"), _vec(cls.PASSAGE_PREFIX + "b")]
    inputs, kwargs = provider._model.calls[-1]
    assert inputs == [cls.PASSAGE_PREFIX + "a", cls.PASSAGE_PREFIX + "b"]
    assert kwargs == {"batch_size": 7, "normalize_embeddings": True, "show_progress_bar": False}
    assert provider.encode_batch([]) == []

    # encode() is unchanged: no prefix, a single string.
    assert provider.encode("plain") == _vec("plain")
    assert provider._model.calls[-1] == ("plain", {"normalize_embeddings": True})


def test_embed_prompts_table():
    assert embed_prompts.PROMPT_SCHEME == 1
    assert embed_prompts.query_prefix("me5") == "query: "
    assert embed_prompts.passage_prefix("ME5") == "passage: "
    assert embed_prompts.query_prefix("gemma") == "task: search result | query: "
    assert embed_prompts.passage_prefix("gemma") == "title: none | text: "
    assert embed_prompts.query_prefix("unknown") == ""
    assert embed_prompts.passage_prefix("") == ""
