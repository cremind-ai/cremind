"""Unit test: `complete-link` (auth.submit_callback) writes the OAuth inbox.

The manual paste fallback lets a user finish linking on a remote/Ingress
deployment where the consent redirect can't reach the backend: they copy the
URL the browser was sent to, and submit_callback drops its query into the same
per-state inbox file the backend callback route would have written
(`<CREMIND_SYSTEM_DIR>/oauth_inbox/<state>.txt`), so the still-running `link`
picks it up and performs the local PKCE exchange.

Run standalone (no pytest needed):  python scripts/tests/test_submit_callback.py
Or via pytest:                      pytest scripts/tests/test_submit_callback.py
"""
import os
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from app.google import auth  # noqa: E402

_STATE = "yVuZU8nVnlXUnirYSBheNCnasvVPub"
_QUERY = f"state={_STATE}&code=4%2F0AdkVLPw-abc&scope=email+openid"


def _with_system_dir(fn):
    """Run fn() with CREMIND_SYSTEM_DIR pointed at a fresh temp dir; return the dir."""
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


def test_full_url_writes_inbox():
    def body(tmp):
        url = f"http://localhost:1112/api/oauth/callback?{_QUERY}"
        result = auth.submit_callback(url)
        assert result == {"submitted": True, "state": _STATE}
        dst = Path(tmp) / "oauth_inbox" / f"{_STATE}.txt"
        assert dst.exists(), "inbox file not written"
        assert dst.read_text(encoding="utf-8") == _QUERY
        # No leftover temp file.
        assert not (Path(tmp) / "oauth_inbox" / f"{_STATE}.txt.tmp").exists()
    _with_system_dir(body)


def test_proxied_url_path_writes_inbox():
    """A proxied redirect (localhost:1515/api/oauth/callback, the K8s
    port-forward origin) is handled the same — only the query matters."""
    def body(tmp):
        url = f"http://localhost:1515/api/oauth/callback?{_QUERY}"
        auth.submit_callback(url)
        assert (Path(tmp) / "oauth_inbox" / f"{_STATE}.txt").read_text(encoding="utf-8") == _QUERY
    _with_system_dir(body)


def test_bare_query_writes_inbox():
    def body(tmp):
        auth.submit_callback(_QUERY)
        assert (Path(tmp) / "oauth_inbox" / f"{_STATE}.txt").read_text(encoding="utf-8") == _QUERY
    _with_system_dir(body)


def test_leading_question_mark_query():
    def body(tmp):
        auth.submit_callback("?" + _QUERY)
        assert (Path(tmp) / "oauth_inbox" / f"{_STATE}.txt").exists()
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
        _expect_autherror("no state", "code=abc&scope=email")
        _expect_autherror("bad state chars", "state=has spaces&code=abc")
        _expect_autherror("short state", "state=short&code=abc")  # < 8 chars
        _expect_autherror("consent error", f"error=access_denied&state={_STATE}")
        _expect_autherror("no code", f"state={_STATE}&scope=email")
    _with_system_dir(body)


if __name__ == "__main__":
    test_full_url_writes_inbox()
    test_proxied_url_path_writes_inbox()
    test_bare_query_writes_inbox()
    test_leading_question_mark_query()
    test_rejects_bad_inputs()
    print("OK: submit_callback writes the inbox and rejects malformed responses.")
