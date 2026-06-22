"""Unit tests: jira_api request shaping for get_issue(expand=) and get_comments.

Monkeypatches the low-level _request so no network is touched — we only assert the
URL + query params the client builds.

Run standalone:  python scripts/tests/test_jira_api.py
Or via pytest:   pytest scripts/tests/test_jira_api.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import jira_api  # noqa: E402


def _capture():
    cap = {}

    def fake_request(method, url, *, access_token, params=None, body=None, **kw):
        cap.update({"method": method, "url": url, "params": params, "body": body})
        return {}

    return cap, fake_request


def test_get_issue_expand_changelog():
    cap, fake = _capture()
    orig = jira_api._request
    jira_api._request = fake
    try:
        jira_api.JiraClient("tok", "cloud").get_issue("ABC-1", fields=["created"], expand="changelog")
    finally:
        jira_api._request = orig
    assert cap["method"] == "GET", cap
    assert cap["url"].endswith("/issue/ABC-1"), cap
    assert cap["params"] == {"fields": "created", "expand": "changelog"}, cap


def test_get_issue_no_params_when_bare():
    cap, fake = _capture()
    orig = jira_api._request
    jira_api._request = fake
    try:
        jira_api.JiraClient("tok", "cloud").get_issue("ABC-1")
    finally:
        jira_api._request = orig
    assert cap["params"] is None, cap


def test_get_comments_params():
    cap, fake = _capture()
    orig = jira_api._request
    jira_api._request = fake
    try:
        jira_api.JiraClient("tok", "cloud").get_comments("ABC-1")
    finally:
        jira_api._request = orig
    assert cap["url"].endswith("/issue/ABC-1/comment"), cap
    assert cap["params"] == {"orderBy": "-created", "maxResults": 20, "startAt": 0}, cap


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: get_issue expand + get_comments build the right request.")
