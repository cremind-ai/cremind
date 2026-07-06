"""CLI renderers surface the Plan-mode SSE events.

Raw renderer prints question/plan/todo prompts to stderr (stdout stays
assistant-text-only); the TUI renderer produces styled lines; the JSON renderer
passes the new event types through.
"""

from __future__ import annotations

import json

from app.cli.client._sse import Event
from app.cli.plan_render import plan_hint_lines, questions_lines, todos_lines


def _event(event_type: str, payload: dict) -> Event:
    frame = {"seq": 1, "type": event_type, "data": payload}
    return Event(type=event_type, data=frame, raw=json.dumps(frame))


# ── shared formatters ──────────────────────────────────────────────────────

def test_questions_lines():
    lines = questions_lines({"questions": [
        {"question": "Which?", "options": [{"label": "A", "description": "first"}, {"label": "B"}],
         "allow_free_text": True},
    ]})
    joined = "\n".join(lines)
    assert "Which?" in joined
    assert "a) A — first" in joined
    assert "b) B" in joined
    assert "free text" in joined


def test_todos_lines_marks():
    lines = todos_lines({"todos": [
        {"content": "done", "status": "completed"},
        {"content": "wip", "status": "in_progress"},
        {"content": "todo", "status": "pending"},
    ]})
    joined = "\n".join(lines)
    assert "[x] done" in joined
    assert "[>] wip" in joined
    assert "[ ] todo" in joined


def test_plan_hint_lines_includes_accept_command():
    lines = plan_hint_lines({"filename": "p.md", "path": "/x/p.md", "title": "T"}, "c1")
    joined = "\n".join(lines)
    assert "Plan ready: p.md" in joined
    assert "/x/p.md" in joined
    assert "cremind conv send c1" in joined


# ── RawRenderer: events go to stderr, stdout stays clean ───────────────────

def test_raw_renderer_prints_plan_events_to_stderr(capsys):
    from app.cli.streaming import RawRenderer
    r = RawRenderer(conversation_id="c1")

    assert r.render(_event("ask_user_question", {"questions": [{"question": "Q?"}]})) is True
    assert r.render(_event("plan_ready", {"filename": "p.md", "path": "/x/p.md"})) is True
    assert r.render(_event("todos", {"todos": [{"content": "a", "status": "pending"}]})) is True

    out = capsys.readouterr()
    assert out.out == ""  # stdout stays assistant-text-only
    assert "Q?" in out.err
    assert "Plan ready: p.md" in out.err
    assert "[ ] a" in out.err


def test_raw_renderer_still_streams_text_to_stdout(capsys):
    from app.cli.streaming import RawRenderer
    r = RawRenderer(conversation_id="c1")
    r.render(_event("text", {"token": "hello"}))
    out = capsys.readouterr()
    assert out.out == "hello"


# ── JSONRenderer: new events pass through, don't terminate ─────────────────

def test_json_renderer_passes_new_events(capsys):
    from app.cli.streaming import JSONRenderer
    r = JSONRenderer()
    assert r.render(_event("ask_user_question", {"questions": []})) is True
    assert r.render(_event("plan_ready", {"filename": "p.md"})) is True
    assert r.render(_event("todos", {"todos": []})) is True
    assert r.render(_event("complete", {})) is False
    out = capsys.readouterr()
    assert '"ask_user_question"' in out.out
    assert '"plan_ready"' in out.out


# ── TUI renderer: styled lines with distinct kinds ─────────────────────────

def test_tui_renderer_new_event_kinds():
    from app.cli.tui.renderer import default_theme, format_event
    theme = default_theme()

    q = format_event(_event("ask_user_question", {"questions": [{"question": "Q?"}]}), theme)
    assert q is not None and q.kind == "question" and "Q?" in q.body

    p = format_event(_event("plan_ready", {"filename": "p.md", "path": "/x/p.md"}), theme)
    assert p is not None and p.kind == "plan" and "p.md" in p.body

    t = format_event(_event("todos", {"todos": [{"content": "a", "status": "completed"}]}), theme)
    assert t is not None and t.kind == "todos" and "[x] a" in t.body
