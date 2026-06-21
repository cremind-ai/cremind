"""The reasoning `Input:` for fired events is the command (+ content for
file/skill events), while the UI bubble keeps the full fenced block.

These test the pure message builders; the runners just wire them into
``run_agent_to_bus`` (query -> reasoning Input, content -> trigger bubble).
"""

from __future__ import annotations

from app.events.schedule_event_runner import build_trigger_messages as sched_build
from app.events.file_watcher_runner import build_trigger_messages as fw_build


# ── schedule: Input is the bare command; bubble keeps the detail block ──────

def test_schedule_query_is_bare_action():
    payload = {"title": "tắt đèn hiên", "fired_at": "2026-06-21T15:07:40", "schedule_kind": "instant"}
    query, bubble = sched_build("tắt đèn hiên", payload)
    assert query == "tắt đèn hiên"
    # No verbose framing leaks into the reasoning Input.
    assert "Trigger:" not in query and "Content:" not in query and "fired_at" not in query


def test_schedule_bubble_unchanged():
    payload = {"title": "tắt đèn hiên", "fired_at": "2026-06-21T15:07:40", "schedule_kind": "instant"}
    _query, bubble = sched_build("tắt đèn hiên", payload)
    assert bubble.startswith("---") and bubble.endswith("---")
    assert "title: tắt đèn hiên" in bubble
    assert "fired_at: 2026-06-21T15:07:40" in bubble
    assert "schedule_kind: instant" in bubble


# ── file watcher: Input is action + event content; bubble keeps the block ───

def test_file_watcher_query_is_action_plus_content():
    payload = {
        "event_type": "modified", "target_kind": "file", "path": "/p/a.py",
        "watch_name": "w", "extension": ".py", "detected_at": "t",
    }
    query, bubble = fw_build("notify me", payload)
    assert query.startswith("notify me")
    assert "event_type: modified" in query and "path: /p/a.py" in query
    assert "Trigger:" not in query and "Content:" not in query  # no verbose framing
    assert bubble.startswith("---") and "event_type: modified" in bubble


def test_file_watcher_moved_includes_src_dest():
    payload = {
        "event_type": "moved", "target_kind": "file", "src_path": "/a", "dest_path": "/b",
        "watch_name": "w", "extension": "", "detected_at": "t",
    }
    query, _bubble = fw_build("act on it", payload)
    assert query.startswith("act on it")
    assert "src_path: /a" in query and "dest_path: /b" in query
