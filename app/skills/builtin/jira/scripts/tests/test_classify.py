# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "python-dotenv",
# ]
# ///
"""Unit tests: lifecycle-event classification + sync routing/dedup.

The listener splits a changed issue into one of issue_created / issue_transitioned /
issue_commented / issue_updated (priority order), and emits issue_deleted from the
relay nudge. These tests drive `_classify` and `_sync` with a fake Jira client — no
network, no real files.

Run standalone:  python scripts/tests/test_classify.py
Or via pytest:   pytest scripts/tests/test_classify.py
"""
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app import listener  # noqa: E402

OLD = "2026-06-08T10:00:00.000+0000"
NEW = "2026-06-08T11:00:00.000+0000"
CURSOR_MS = listener._parse_jira_dt_ms("2026-06-08T10:30:00.000+0000")


class FakeClient:
    def __init__(self, *, histories=None, comments=None):
        self._histories = histories or []
        self._comments = comments or []
        self.get_issue_calls = 0
        self.get_comments_calls = 0

    def get_issue(self, key, *, fields=None, expand=None):
        self.get_issue_calls += 1
        return {"changelog": {"histories": self._histories}}

    def get_comments(self, key, *, order_by="-created", max_results=20, start_at=0):
        self.get_comments_calls += 1
        return {"comments": self._comments}


def _issue(*, created=OLD, updated=NEW, key="ABC-1", summary="Fix login"):
    return {"key": key, "fields": {"created": created, "updated": updated, "summary": summary}}


def _history(created, items):
    return {"created": created, "items": items}


def test_created_short_circuits():
    svc = FakeClient()
    out = listener._classify(_issue(created=NEW), CURSOR_MS, svc=svc)
    assert out == [("issue_created", {})], out
    assert svc.get_issue_calls == 0, "created should not fetch the changelog"


def test_status_item_is_transitioned():
    svc = FakeClient(histories=[_history(NEW, [{"field": "status", "fromString": "To Do", "toString": "Done"}])])
    out = listener._classify(_issue(), CURSOR_MS, svc=svc)
    assert out == [("issue_transitioned", {"transition": {"from": "To Do", "to": "Done"}})], out


def test_non_status_item_is_updated():
    svc = FakeClient(histories=[_history(NEW, [{"field": "summary", "fromString": "a", "toString": "b"}])])
    out = listener._classify(_issue(), CURSOR_MS, svc=svc)
    assert out == [("issue_updated", {})], out


def test_history_before_cursor_ignored():
    svc = FakeClient(histories=[_history(OLD, [{"field": "status", "toString": "Done"}])])
    out = listener._classify(_issue(), CURSOR_MS, svc=svc)
    # The only history predates the cursor → no kept change → comment check (empty) → updated.
    assert out == [("issue_updated", {})], out
    assert svc.get_comments_calls == 1, "unexplained bump should probe comments"


def test_new_comment_is_commented():
    svc = FakeClient(comments=[{"created": NEW, "author": {"displayName": "Alice"}, "body": "On it."}])
    out = listener._classify(_issue(), CURSOR_MS, svc=svc, nudge_event="comment_created")
    assert out[0][0] == "issue_commented", out
    assert out[0][1]["comment"] == {"author": "Alice", "body": "On it."}, out


def test_priority_created_beats_status():
    svc = FakeClient(histories=[_history(NEW, [{"field": "status", "toString": "Done"}])])
    out = listener._classify(_issue(created=NEW), CURSOR_MS, svc=svc)
    assert out == [("issue_created", {})], out


def test_priority_status_beats_other_fields():
    svc = FakeClient(histories=[_history(NEW, [
        {"field": "summary", "toString": "b"},
        {"field": "status", "fromString": "To Do", "toString": "In Progress"},
    ])])
    out = listener._classify(_issue(), CURSOR_MS, svc=svc)
    assert out[0][0] == "issue_transitioned", out


# --- _sync routing / dedup / delete nudge ---

def _patched_sync(monkey_writes, *, search_issues, nudge_event=None, nudge_key=None, state=None, fake=None):
    """Run _sync with _svc/_write_*/_save_state monkeypatched; return emit count."""
    fake = fake or FakeClient()

    class _Svc(FakeClient):
        def search(self, jql, *, fields=None, max_results=50, next_page_token=None):
            return {"issues": search_issues}

    svc = _Svc()
    orig = (listener._svc, listener._write_event, listener._write_deleted_event, listener._save_state)
    listener._svc = lambda: svc
    listener._write_event = lambda issue, site_url, et, ctx=None: monkey_writes.append((issue.get("key"), et))
    listener._write_deleted_event = lambda key, site_url: monkey_writes.append((key, "issue_deleted"))
    listener._save_state = lambda s: None
    try:
        return listener._sync(state if state is not None else {"since_ms": CURSOR_MS}, "site",
                              nudge_event=nudge_event, nudge_key=nudge_key)
    finally:
        listener._svc, listener._write_event, listener._write_deleted_event, listener._save_state = orig


def test_sync_dedups_on_second_run():
    writes = []
    state = {"since_ms": CURSOR_MS}
    n1 = _patched_sync(writes, search_issues=[_issue(created=NEW)], state=state)
    n2 = _patched_sync(writes, search_issues=[_issue(created=NEW)], state=state)
    assert n1 == 1, writes
    assert n2 == 0, "same (key, updated, event_type) must dedup on re-pull"
    assert writes == [("ABC-1", "issue_created")], writes


def test_sync_delete_nudge_writes_deleted():
    writes = []
    n = _patched_sync(writes, search_issues=[], nudge_event="jira:issue_deleted", nudge_key="ABC-9")
    assert n == 1, writes
    assert writes == [("ABC-9", "issue_deleted")], writes


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("OK: classification, priority order, sync dedup, and delete-nudge routing.")
