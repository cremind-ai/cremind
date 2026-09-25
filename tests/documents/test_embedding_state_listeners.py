"""Embedding-state listeners hear every settled transition, safely.

Documentation search re-embeds when the model or store changes. The change can
arrive through boot, the apply pipeline, ``defer_apply`` or a failure, and all
of them end in ``mark_ready`` / ``mark_failed`` / ``mark_disabled`` — so that
is where listeners are called: after the state lock is released, with errors
contained so a listener bug can never fail a model load.
"""

from __future__ import annotations

import threading

import pytest

from app.config import embedding_state as es
from app.config.embedding_state import EmbeddingStatus


@pytest.fixture
def state(monkeypatch):
    monkeypatch.setattr(es, "_publish_safe", lambda snapshot: None)
    return es._EmbeddingState()


@pytest.fixture
def listen():
    added: list = []

    def _add(fn):
        es.add_listener(fn)
        added.append(fn)
        return fn

    yield _add
    for fn in added:
        es.remove_listener(fn)


def test_settled_transitions_are_delivered(state, listen):
    heard: list = []
    listen(lambda status, emb, vs: heard.append((status, emb, vs)))
    emb, vs = object(), object()

    state.mark_ready(emb, vs)
    state.mark_failed("boom")
    state.mark_disabled()

    assert heard == [
        (EmbeddingStatus.READY, emb, vs),
        (EmbeddingStatus.FAILED, None, None),
        (EmbeddingStatus.DISABLED, None, None),
    ]


def test_busy_transitions_are_not_delivered(state, listen):
    heard: list = []
    listen(lambda *a: heard.append(a))
    assert state.mark_initializing()
    state.transition_to_rebuilding(phase="x")
    state.set_phase("y")
    assert heard == []
    state.mark_ready("e", "v")
    assert [h[0] for h in heard] == [EmbeddingStatus.READY]


def test_listeners_run_after_the_state_lock_is_released(state, listen):
    seen: list = []

    def check(status, emb, vs):
        seen.append(state._lock.locked())
        # Reading the state from a listener must not deadlock.
        seen.append(state.to_dict()["status"])

    listen(check)
    done = threading.Event()
    t = threading.Thread(target=lambda: (state.mark_ready("e", "v"), done.set()))
    t.start()
    assert done.wait(5), "mark_ready deadlocked calling a listener"
    assert seen == [False, "ready"]


def test_a_failing_listener_is_contained(state, listen):
    heard: list = []

    def broken(*a):
        raise RuntimeError("listener bug")

    listen(broken)
    listen(lambda *a: heard.append(a[0]))

    state.mark_ready("e", "v")  # must not raise
    state.mark_failed("x")

    assert state.status is EmbeddingStatus.FAILED
    assert heard == [EmbeddingStatus.READY, EmbeddingStatus.FAILED]


def test_add_is_idempotent_and_remove_is_forgiving(state, listen):
    heard: list = []

    def fn(*a):
        heard.append(a[0])

    listen(fn)
    es.add_listener(fn)
    state.mark_disabled()
    assert heard == [EmbeddingStatus.DISABLED]

    es.remove_listener(fn)
    es.remove_listener(fn)  # already gone: ignored
    state.mark_disabled()
    assert heard == [EmbeddingStatus.DISABLED]


def test_the_module_singleton_notifies_too(monkeypatch, listen):
    monkeypatch.setattr(es, "_publish_safe", lambda snapshot: None)
    heard: list = []
    listen(lambda *a: heard.append(a[0]))
    s = es.embedding_state
    fields = ("_status", "_error", "_phase", "_embedding", "_vector_store")
    saved = {f: getattr(s, f) for f in fields}
    try:
        s.mark_failed("probe")
    finally:
        # Put back exactly what the process had, without another transition.
        with s._lock:
            for f, val in saved.items():
                setattr(s, f, val)
    assert heard == [EmbeddingStatus.FAILED]
