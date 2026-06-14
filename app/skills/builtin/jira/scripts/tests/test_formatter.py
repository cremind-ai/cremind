"""Unit tests: per-event markdown rendering (transition, comment, deleted).

Run standalone:  python scripts/tests/test_formatter.py
Or via pytest:   pytest scripts/tests/test_formatter.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import formatter  # noqa: E402

SITE = "https://acme.atlassian.net"


def _issue(summary="Fix login", description="Steps to reproduce"):
    return {"key": "ABC-1", "fields": {
        "summary": summary, "status": {"name": "In Progress"}, "issuetype": {"name": "Bug"},
        "assignee": {"displayName": "Alice"}, "reporter": {"displayName": "Bob"},
        "priority": {"name": "High"}, "updated": "2026-06-08T10:20:30.000-0700",
        "description": description,
    }}


def test_transition_adds_from_to():
    out = formatter.format_issue_markdown(
        _issue(), event_type="issue_transitioned", site_url=SITE,
        transition={"from": "To Do", "to": "Done"},
    )
    assert "event_type: issue_transitioned" in out, out
    assert "from_status: To Do" in out, out
    assert "to_status: Done" in out, out


def test_comment_sets_body_and_author():
    out = formatter.format_issue_markdown(
        _issue(), event_type="issue_commented", site_url=SITE,
        comment={"author": "Alice", "body": "On it."},
    )
    assert "comment_author: Alice" in out, out
    body = out.split("---", 2)[2]
    assert "On it." in body, out
    assert "## Issue" in body, "the issue description should move under a heading"
    assert "Steps to reproduce" in body, out


def test_deleted_is_minimal():
    out = formatter.format_deleted_markdown("ABC-1", site_url=SITE)
    assert "event_type: issue_deleted" in out, out
    assert "key: ABC-1" in out, out
    assert f"{SITE}/browse/ABC-1" in out, out  # url value (YAML-quoted because it has ':')
    assert "Issue ABC-1 was deleted." in out, out
    # No issue fields are rendered (the issue is gone).
    assert "assignee:" not in out, out


def test_yaml_quotes_status_with_colon():
    out = formatter.format_issue_markdown(
        _issue(), event_type="issue_transitioned", site_url=SITE,
        transition={"from": "Blocked: waiting", "to": "Done"},
    )
    assert 'from_status: "Blocked: waiting"' in out, out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: transition/comment/deleted rendering and YAML quoting.")
