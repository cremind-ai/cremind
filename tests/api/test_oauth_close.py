"""Where a consent window is sent, and the page that closes it (app/api/oauth_close.py).

Four things have to hold. The callback's own URL carries a live authorization
code, so the browser must never commit a document there — the answer is a
redirect, and the address it lands on says only what happened. That page must
tell the page which opened the consent (the Calendar page and the Drive section
listen for exactly this notice, whose shape is shared with
ui/src/services/oauthReturn.ts). It must close a window it succeeded in, because
that is what the window was opened for. And because the address is reachable by
anyone, nothing a visitor writes may reach the markup or the script.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.api import oauth_close as oc

_REPO = Path(__file__).resolve().parents[2]
_CODE = "4/0AVGzR1CsecretAuthorizationCode"
_STATE = "yVuZU8nVnlXUnirYSBheNCnasvVPub"


def _page(**kwargs) -> str:
    response = oc.close_page(**kwargs)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.media_type == "text/html"
    return response.body.decode("utf-8")


def _notice(body: str) -> dict:
    return json.loads(re.search(r"var n=(\{.*?\});", body).group(1))


def _text(body: str) -> str:
    return " ".join(re.findall(r"<(?:h1|p)[^>]*>(.*?)</(?:h1|p)>", body))


def _query(response) -> dict:
    parts = urlsplit(response.headers["location"])
    assert parts.path == oc.CLOSE_PATH
    return {key: value[0] for key, value in parse_qs(parts.query).items()}


# ── the redirect: the code must not become a document ────────────────────────

def test_the_consent_window_is_redirected_off_the_url_that_holds_the_code():
    response = oc.close_redirect(outcome="received", flow="skill")
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert _query(response) == {"o": "received", "f": "skill"}


def test_the_redirect_carries_only_its_own_vocabulary():
    """Whatever a provider sends, only these keys and these values travel."""
    response = oc.close_redirect(
        outcome="denied", flow="calendar", reason="denied", error_code="access_denied",
    )
    assert _query(response) == {"o": "denied", "f": "calendar", "r": "denied", "e": "access_denied"}
    for junk in (_CODE, _STATE, "code=", "state="):
        assert junk not in response.headers["location"]

    # Anything outside the vocabulary is dropped rather than echoed.
    loose = oc.close_redirect(
        outcome="nonsense", flow="../etc", reason="<script>", error_code="a b",
    )
    assert _query(loose) == {"o": "failed"}


# ── the notice ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("flow", ["skill", "calendar", "drive", None])
@pytest.mark.parametrize("outcome", ["received", "denied", "failed", "invalid"])
def test_every_answer_posts_its_outcome_to_the_opener_and_the_channel(flow, outcome):
    body = _page(outcome=outcome, flow=flow)
    assert _notice(body) == {
        "type": "cremind:oauth-return", "flow": flow, "outcome": outcome, "profile": None,
    }
    assert "window.opener.postMessage(n,'*')" in body
    assert f"new BroadcastChannel({json.dumps(oc.CHANNEL_NAME)})" in body


def test_the_notice_contract_matches_the_spa():
    """Both halves name the same message type and channel; a rename in one alone
    silently stops the Calendar and Drive pages from ever hearing a return."""
    source = (_REPO / "ui" / "src" / "services" / "oauthReturn.ts").read_text(encoding="utf-8")
    assert f"OAUTH_RETURN_MESSAGE_TYPE = '{oc.MESSAGE_TYPE}'" in source
    assert f"OAUTH_RETURN_CHANNEL = '{oc.CHANNEL_NAME}'" in source


# ── closing ──────────────────────────────────────────────────────────────────

def test_a_successful_consent_closes_its_own_window():
    body = _page(outcome="received", flow="skill")
    assert "window.close()" in body
    # The message is hidden by the script, for as long as the close it is about
    # to attempt might take — never by the markup, which would leave a blank
    # page behind if the script did not run at all.
    assert "document.body.className='closing'" in body
    assert "<body>" in body and 'class="closing"' not in body


@pytest.mark.parametrize(("outcome", "reason", "says"), [
    ("denied", "denied", "Access was not granted"),
    ("failed", "exchange", "could not finish connecting"),
    ("invalid", "expired", "expired or was already used"),
])
def test_a_consent_that_did_not_succeed_stays_open_and_says_why(outcome, reason, says):
    body = _page(outcome=outcome, flow="calendar", reason=reason)
    assert "window.close()" not in body
    # Nothing hides the message either: there is no close to wait for.
    assert "document.body.className='closing'" not in body
    assert says in _text(body)
    # Named for the flow that failed, so the retry instruction is the right one.
    assert "Calendar page" in _text(body)


def test_the_page_never_navigates_anywhere():
    """The point of the change: no redirect into the app, no link back — the
    window closes, or it says it can be closed."""
    for outcome in ("received", "denied", "failed", "invalid"):
        body = _page(outcome=outcome, flow="skill", reason="inbox")
        assert "<a " not in body and "location" not in body and "href" not in body
        assert "You can close this tab." in _text(body)


# ── what may never reach the browser ─────────────────────────────────────────

def test_the_page_is_locked_down_by_its_own_content_security_policy():
    response = oc.close_page(outcome="received", flow="skill")
    policy = response.headers["content-security-policy"]
    script = re.search(r"<script>(.*?)</script>", response.body.decode("utf-8"), re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    # Only this exact script may run, nothing may be fetched, and the page can
    # neither be framed nor made to submit anywhere.
    style = re.search(r"<style>(.*?)</style>", response.body.decode("utf-8"), re.S).group(1)
    style_digest = base64.b64encode(hashlib.sha256(style.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in policy
    assert f"style-src 'sha256-{style_digest}'" in policy
    assert "default-src 'none'" in policy
    assert "frame-ancestors 'none'" in policy
    assert "form-action 'none'" in policy


@pytest.mark.parametrize("hostile", [
    "</script><img src=x onerror=alert(1)>", "'+alert(1)+'", "../../etc/passwd",
])
def test_a_value_outside_the_vocabulary_never_reaches_the_page(hostile):
    body = _page(outcome=hostile, flow=hostile, reason=hostile, error_code=hostile)
    assert hostile not in body
    assert body.count("<script>") == 1 and body.count("</script>") == 1
    # An unrecognised outcome is a failure, never a success: it must not close
    # the window on a consent that may not have been granted.
    assert _notice(body)["outcome"] == "failed"
    assert "window.close()" not in body


def test_only_a_short_provider_error_code_is_echoed():
    granted = _page(outcome="denied", flow="skill", error_code="access_denied")
    assert "access_denied" in _text(granted)
    shouty = _page(outcome="denied", flow="skill", error_code="<b>not a code</b>")
    assert "<b>" not in shouty
    assert "Access was not granted." in _text(shouty)


def test_the_wording_names_google_only_where_the_flow_proves_it():
    """``/api/oauth/callback`` is shared with the jira/confluence skills, so a
    skill return must not tell an Atlassian user that Google answered."""
    assert "Google" not in _text(_page(outcome="received", flow="skill"))
    assert "Google" not in _text(_page(outcome="denied", flow="skill"))
    assert "Google" not in _text(_page(outcome="received", flow=None))


# ── the route ────────────────────────────────────────────────────────────────

def test_the_close_route_is_a_get_on_the_path_the_redirect_names():
    routes = {route.path: route.methods for route in oc.get_oauth_close_routes()}
    assert set(routes) == {oc.CLOSE_PATH}
    assert set(routes[oc.CLOSE_PATH]) <= {"GET", "HEAD"}
