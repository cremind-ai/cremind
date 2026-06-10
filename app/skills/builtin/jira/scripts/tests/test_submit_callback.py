"""Unit test: `complete-link` (auth.submit_callback) writes the OAuth inbox.

The manual paste fallback lets a user finish Atlassian linking on a
remote/Kubernetes deployment where the registered loopback callback can't reach
the backend: they copy the URL the browser was sent to, and submit_callback drops
its query into the same per-state inbox file the backend loopback listener would
have written (`<CREMIND_SYSTEM_DIR>/oauth_inbox/<state>.txt`), so the still-running
`link` reads the code and completes the backend-mediated exchange. The exchange's
redirect_uri stays the registered `http://127.0.0.1:<port>/` (link holds it).

Run standalone:  python scripts/tests/test_submit_callback.py
Or via pytest:   pytest scripts/tests/test_submit_callback.py
"""
import os
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app.atlassian import auth  # noqa: E402

_STATE = "G1Ec7aHzRQ2aEiJGvR3ePE56huKzG84o"
_QUERY = f"state={_STATE}&code=eyJraWQiOiJBVVRI-abc.def&scope=read%3Ajira-work"


def _with_system_dir(fn):
    prev = os.environ.get("CREMIND_SYSTEM_DIR")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["CREMIND_SYSTEM_DIR"] = tmp
        try:
            fn(tmp)
        finally:
            if prev is None:
                os.environ.pop("CREMIND_SYSTEM_DIR", None)
            else:
                os.environ["CREMIND_SYSTEM_DIR"] = prev


def test_full_loopback_url_writes_inbox():
    def body(tmp):
        # The registered loopback redirect the browser was sent to (and failed to load).
        url = f"http://127.0.0.1:1516/?{_QUERY}"
        assert auth.submit_callback(url) == {"submitted": True, "state": _STATE}
        dst = Path(tmp) / "oauth_inbox" / f"{_STATE}.txt"
        assert dst.exists() and dst.read_text(encoding="utf-8") == _QUERY
        assert not (Path(tmp) / "oauth_inbox" / f"{_STATE}.txt.tmp").exists()
    _with_system_dir(body)


def test_bare_query_writes_inbox():
    def body(tmp):
        auth.submit_callback(_QUERY)
        assert (Path(tmp) / "oauth_inbox" / f"{_STATE}.txt").read_text(encoding="utf-8") == _QUERY
    _with_system_dir(body)


def _expect_autherror(label, response):
    raised = None
    try:
        auth.submit_callback(response)
    except auth.AuthError as e:
        raised = str(e)
    assert raised is not None, f"{label}: expected AuthError, got none"


def test_rejects_bad_inputs():
    def body(_tmp):
        _expect_autherror("empty", "")
        _expect_autherror("no state", "code=abc&scope=read")
        _expect_autherror("bad state chars", "state=has spaces&code=abc")
        _expect_autherror("short state", "state=short&code=abc")
        _expect_autherror("consent error", f"error=access_denied&state={_STATE}")
        _expect_autherror("no code", f"state={_STATE}&scope=read")
    _with_system_dir(body)


if __name__ == "__main__":
    test_full_loopback_url_writes_inbox()
    test_bare_query_writes_inbox()
    test_rejects_bad_inputs()
    print("OK: Atlassian submit_callback writes the inbox and rejects malformed responses.")
