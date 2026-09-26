"""The pure half of per-conversation search-tool selection (``app.agent.search_tools``).

Stdlib-only rules every path shares: how a selection is stored and read back,
what a run's frozen snapshot allows, the priority-ordered guidance text, and
the cache-baseline / warning rules the API reports. The agent wiring is pinned
in test_search_tools_agent.py.
"""

from __future__ import annotations

import pytest

from app.agent import search_tools as st


# ── storing and reading a selection ────────────────────────────────────────


def test_normalize_orders_by_priority_and_folds_all_four_into_the_default():
    assert st.normalize_selection(None) is None
    assert st.normalize_selection([]) == []
    assert st.normalize_selection(["web_search", "documentation_search"]) == [
        "documentation_search", "web_search",
    ]
    # "all four" and "the default" are the same choice, so Reset == Select all.
    assert st.normalize_selection(list(reversed(st.SEARCH_TOOL_IDS))) is None


@pytest.mark.parametrize("bad", [
    "web_search", 3, [1], ["nope"], ["web_search", "web_search"], {"web_search": True},
])
def test_normalize_rejects_anything_else(bad):
    with pytest.raises(st.SelectionError):
        st.normalize_selection(bad)


def test_read_stored_tolerates_what_a_row_may_hold():
    assert st.read_stored(None) is None
    assert st.read_stored("null") is None
    assert st.read_stored('["memory_search", "retired_tool"]') == ["memory_search"]
    assert st.read_stored("not json") is None
    assert st.read_stored(["web_search", "user_documents"]) == ["web_search"]
    assert st.read_stored(list(st.SEARCH_TOOL_IDS)) is None


def test_a_snapshot_is_frozen_and_only_governs_the_four_sources():
    snap = st.Snapshot.of('["web_search"]', "4")
    assert snap == st.Snapshot(("web_search",), 4, "conversation")
    assert snap.allows("web_search") and not snap.allows("memory_search")
    assert snap.allows("exec_shell") and snap.allows(None)  # outside the policy
    with pytest.raises(Exception):
        snap.selection = None  # type: ignore[misc]
    assert st.Snapshot.of([], None).desired() == []
    assert st.Snapshot.of(None, "garbage").version == 0
    assert st.DEFAULT_SNAPSHOT.desired() == list(st.SEARCH_TOOL_IDS)


def test_filter_tools_drops_whole_groups_only():
    class T:
        def __init__(self, tool_id):
            self.tool_id = tool_id

    tools = [T(t) for t in ("calc", *st.SEARCH_TOOL_IDS, "exec_shell")]
    kept = st.filter_tools(tools, st.Snapshot.of(["memory_search"], 1))
    assert [t.tool_id for t in kept] == ["calc", "memory_search", "exec_shell"]
    assert st.filter_tools(tools, None) == tools


# ── guidance ───────────────────────────────────────────────────────────────


_ALL = {
    "web_search": ["web_search__search_web"],
    "memory_search": ["memory_search__search_memory"],
    "cremind_documentation_search": ["cremind_documentation_search__search_documentation"],
    "documentation_search": ["documentation_search__search", "documentation_search__read"],
}


def test_guidance_is_deterministic_and_priority_ordered():
    text = st.build_priority_guidance(_ALL)
    assert text == st.build_priority_guidance(dict(reversed(list(_ALL.items()))))
    assert text.startswith("\n") and text.endswith("\n")
    rows = [line for line in text.splitlines() if line[:2] in ("1.", "2.", "3.", "4.")]
    assert [r.split(" (")[0] for r in rows] == [
        "1. Documentation search", "2. Cremind documentation search",
        "3. Memory search", "4. Web search",
    ]
    assert "`documentation_search__search`, `documentation_search__read`" in rows[0]


def test_guidance_skips_empty_sources_and_renders_nothing_for_none():
    assert st.build_priority_guidance({}) == ""
    assert st.build_priority_guidance({"web_search": []}) == ""
    text = st.build_priority_guidance({"memory_search": ["m"], "web_search": []})
    assert "Web search" not in text and "1. Memory search" in text


def test_web_search_alone_is_not_a_fallback():
    alone = st.build_priority_guidance({"web_search": ["web_search__search_web"]})
    assert "fallback" not in alone and "sources above" not in alone
    with_others = st.build_priority_guidance(
        {"memory_search": ["m"], "web_search": ["web_search__search_web"]})
    assert "the fallback when the sources above cannot answer" in with_others


# ── baseline and warning ───────────────────────────────────────────────────


def test_the_fingerprint_depends_on_names_schemas_and_text_only():
    a = st.fingerprint([("f", {"x": 1}), ("g", {})], "guide")
    assert a == st.fingerprint([("g", {}), ("f", {"x": 1})], "guide")  # order-free
    assert a != st.fingerprint([("f", {"x": 2}), ("g", {})], "guide")
    assert a != st.fingerprint([("f", {"x": 1}), ("g", {})], "guide!")


def test_a_baseline_round_trips_and_marks_pending_until_adopted():
    base = st.make_baseline(version=3, effective=["web_search", "memory_search"],
                            fingerprint_hex="ab", now_ms=1)
    assert base["effective"] == ["memory_search", "web_search"]
    assert st.read_baseline(base) == base
    assert st.read_baseline('{"effective": []}') == {"effective": []}
    assert st.read_baseline("junk") is None and st.read_baseline({"x": 1}) is None
    assert st.pending_next_response(4, base) is True
    assert st.pending_next_response(3, base) is False
    assert st.pending_next_response(9, None) is False


def test_the_cache_warning_rules():
    web, mem_web = ["web_search"], ["memory_search", "web_search"]
    base = st.make_baseline(version=1, effective=mem_web, fingerprint_hex="x")
    # A no-op (or a change to an unavailable source only) never warns.
    assert not st.edit_may_miss_cache(before_effective=web, after_effective=web,
                                      baseline=base, prior_activity=True)
    # Restoring what the last request sent does not warn; moving away does.
    assert not st.edit_may_miss_cache(before_effective=web, after_effective=mem_web,
                                      baseline=base, prior_activity=True)
    assert st.edit_may_miss_cache(before_effective=mem_web, after_effective=web,
                                  baseline=base, prior_activity=True)
    # No baseline: conservative only when the conversation already talked.
    assert st.edit_may_miss_cache(before_effective=mem_web, after_effective=web,
                                  baseline=None, prior_activity=True)
    assert not st.edit_may_miss_cache(before_effective=mem_web, after_effective=web,
                                      baseline=None, prior_activity=False)


def test_room_availability_is_an_aggregate():
    A = st.Availability
    alice = {t: A(t, True) for t in st.SEARCH_TOOL_IDS}
    bob = {**alice, "web_search": A("web_search", False, st.REASON_TURNED_OFF),
           "documentation_search": A("documentation_search", False, None, hidden=True)}
    merged = st.merge_room_availability([alice, bob])
    assert merged["web_search"].available and merged["web_search"].reason == st.REASON_VARIES
    assert merged["memory_search"].reason is None
    nobody = st.merge_room_availability([bob, bob])
    assert nobody["documentation_search"].hidden
    assert not nobody["web_search"].available
    rows = st.room_tool_rows(nobody)
    assert [r.id for r in rows] == ["cremind_documentation_search", "memory_search", "web_search"]


class _AvailRegistry:
    """Every source registered and on, with no leaf turned off."""

    def __init__(self, enabled=st.SEARCH_TOOL_IDS):
        self._enabled = set(enabled)

    def tools_for_profile(self, profile):
        from types import SimpleNamespace

        return [SimpleNamespace(tool_id=t) for t in self._enabled]

    def get(self, tool_id):
        return object() if tool_id in st.SEARCH_TOOL_IDS else None

    def leaves_for_profile(self, profile, tool_id):
        return {"leaves": [{"enabled": True}]}


def test_the_picker_offers_documentation_search_exactly_when_the_agent_gate_does(monkeypatch):
    """One rule on both sides: the agent's gate (admin allowed, a source on,
    this origin allowed). Missing optional extras hide nothing — the index is
    still searchable — so the picker never hides a source a run exposes."""
    import app.documents.gate as gate
    import app.features.manifest as manifest

    monkeypatch.setattr(manifest, "missing_features", lambda keys: list(keys))
    monkeypatch.setattr(gate, "documents_tool_available", lambda profile, origin: True)
    avail = st.availability("alice", "web_cli", registry=_AvailRegistry())
    assert avail["documentation_search"].available and not avail["documentation_search"].hidden

    monkeypatch.setattr(gate, "documents_tool_available", lambda profile, origin: origin != "rooms")
    assert st.availability("alice", "rooms", registry=_AvailRegistry())["documentation_search"].hidden


def test_a_source_turned_off_in_tools_settings_stays_visible_with_a_reason(monkeypatch):
    import app.documents.gate as gate

    monkeypatch.setattr(gate, "documents_tool_available", lambda profile, origin: True)
    reg = _AvailRegistry(enabled=[t for t in st.SEARCH_TOOL_IDS if t != "web_search"])
    web = st.availability("alice", "web_cli", registry=reg)["web_search"]
    assert (web.available, web.hidden, web.reason) == (False, False, st.REASON_TURNED_OFF)
