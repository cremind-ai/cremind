"""The page an OAuth consent window lands on — which closes itself.

Google (and Atlassian, which shares ``/api/oauth/callback``) end a consent by
redirecting the browser to one of the backend callbacks in
app/api/oauth_callback.py. By then the callback has already done the real work:
the authorization response is in the skill inbox, or the Calendar token is
exchanged, or the picked Drive files are recorded. Nothing is left for that
window to do, so it does the one useful thing and goes away — it was opened for
this round trip, and the user is waiting on the page they started from.

The answer is a 303 to :data:`CLOSE_PATH`, and this page is what is served
there. The hop matters: the callback's own URL carries ``?code=…&state=…``, and
a document committed at that address would put a live authorization code in the
browser's history, its address bar and every content script on the origin — for
the skill flow the code stays redeemable until the waiting ``link`` exchanges
it. A redirect leaves no such document, and the address it lands on says only
what happened, in a fixed vocabulary this module owns.

The page then:

1. tells the window that opened the consent (``window.opener``) and every other
   tab of this origin (a ``BroadcastChannel``) that the provider answered, so
   the Calendar page and the Drive section react at once instead of on their
   next poll — see ui/src/services/oauthReturn.ts, which defines this payload;
2. calls ``window.close()``;
3. and, if it is still on screen a moment later, says so in one line.

Step 3 is not a formality. A browser lets a script close only a window
``window.open`` created (a ``target=_blank`` tab qualifies only while its
history holds a single entry, which a consent flow leaves behind long before
this). The SPA therefore opens recognised consent links with ``window.open`` for
exactly that reason — but a consent pasted into a fresh tab, one the desktop app
handed to the OS browser, or an Atlassian consent the SPA does not recognise,
stays put. That window gets a sentence, not a redirect: sending it into the SPA
is what put a stale-session sign-in page in front of people who had just signed
in with Google.

A consent that did NOT succeed keeps its window on purpose: "denied" or
"failed" is the one thing this page knows and the initiating page cannot show as
precisely. The wording stays provider-neutral unless the flow proves whose
consent it was, because the skills' callback is shared with jira/confluence; and
it stays at "response received", never "linked": for the skills the token
exchange happens afterwards, in the waiting ``link``.

Nothing here reads or writes state, so it is safe for the PRE-storage routes.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from typing import Optional
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route

#: Kept identical to ``OAUTH_RETURN_MESSAGE_TYPE``/``OAUTH_RETURN_CHANNEL`` in
#: ui/src/services/oauthReturn.ts. A test pins the pair.
MESSAGE_TYPE = "cremind:oauth-return"
CHANNEL_NAME = "cremind:oauth-return"

#: Where a callback sends the browser once its work is done.
CLOSE_PATH = "/api/oauth/close"

OUTCOMES = frozenset({"received", "denied", "failed", "invalid"})
FLOWS = frozenset({"skill", "calendar", "drive"})
#: Why, as a short key. The text lives here, never in the URL: this address is
#: reachable by anyone, so nothing on the page may be written by its visitor.
REASONS = frozenset({"denied", "inbox", "nocode", "exchange", "expired", "picks", "nostate"})
#: An RFC 6749 ``error`` code is a short token; anything else is not echoed.
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

#: How long a browser gets to honour ``window.close()`` before the page admits
#: it is staying.
_LINGER_MS = 400

_TITLES = {
    # Not "signed in": for the skills the token exchange happens afterwards, in
    # the waiting ``link``. What is true here is that the response arrived.
    "received": "Response received",
    "denied": "Not authorized",
    "failed": "Sign-in could not be completed",
    "invalid": "This sign-in request is no longer active",
}
_CONFIRMERS = {
    "skill": "The chat that asked for the link confirms once the account is connected.",
    "calendar": "The Calendar page confirms once Google Calendar is connected.",
    "drive": "The Drive section confirms which files were granted.",
}
_RETRY = {
    "skill": "Ask the agent to link the account again to retry.",
    "calendar": "Use Connect Google on the Calendar page to retry.",
    "drive": "Use Grant access under Settings → GSuite to retry.",
}
_DEFAULT_CONFIRMER = "The page that started the sign-in confirms once the account is connected."
_DEFAULT_RETRY = "Start the sign-in again from the page you began on."
_REASON_TEXT = {
    "inbox": "Cremind could not save the authorization response.",
    "nocode": "The response carried no authorization code.",
    "exchange": "Cremind could not finish connecting the account.",
    "picks": "Cremind could not record the picked files.",
    "nostate": "The response did not carry a request Cremind can identify.",
    ("expired", "calendar"): "This consent expired or was already used.",
    ("expired", "drive"): "This picker round expired or was already finished.",
    "expired": "This request expired or was already used.",
}

_STYLE = (
    "html,body{height:100%}"
    "body{margin:0;display:flex;align-items:center;justify-content:center;"
    "font:15px/1.5 system-ui,-apple-system,'Segoe UI',sans-serif;color:#1f2328;"
    "background:#f6f7f9}"
    "main{max-width:30rem;padding:2rem;text-align:center}"
    "h1{margin:0 0 .5rem;font-size:1.15rem}"
    "p{margin:.35rem 0;color:#57606a}"
    # The script hides the message while the close it is about to attempt has
    # its chance. Hidden by default instead would leave a blank page behind
    # whenever the script does not run at all.
    "body.closing main{visibility:hidden}"
    "@media (prefers-color-scheme:dark){body{color:#e6edf3;background:#0d1117}p{color:#9198a1}}"
)


def _one_of(value: object, allowed: frozenset) -> Optional[str]:
    return value if isinstance(value, str) and value in allowed else None


def _message(outcome: str, flow: Optional[str], reason: Optional[str],
             error_code: Optional[str]) -> Optional[str]:
    if outcome == "denied":
        code = error_code if error_code and _ERROR_CODE.fullmatch(error_code) else None
        return f"Access was not granted ({code})." if code else "Access was not granted."
    if reason is None:
        return None
    return _REASON_TEXT.get((reason, flow or "")) or _REASON_TEXT.get(reason)


def _lines(outcome: str, flow: Optional[str], message: Optional[str]) -> list[str]:
    if outcome == "received":
        return [_CONFIRMERS.get(flow or "", _DEFAULT_CONFIRMER)]
    return [message or "The response arrived, but Cremind could not process it.",
            _RETRY.get(flow or "", _DEFAULT_RETRY)]


def close_redirect(
    *,
    outcome: str,
    flow: Optional[str] = None,
    reason: Optional[str] = None,
    error_code: Optional[str] = None,
) -> RedirectResponse:
    """303 the consent window to :data:`CLOSE_PATH`, away from its own URL.

    The query carries only this fixed vocabulary — never the authorization
    code, never the state, never text a visitor could choose.
    """
    query = {"o": _one_of(outcome, OUTCOMES) or "failed"}
    if _one_of(flow, FLOWS):
        query["f"] = flow
    if _one_of(reason, REASONS):
        query["r"] = reason
    if error_code and _ERROR_CODE.fullmatch(error_code):
        query["e"] = error_code
    return RedirectResponse(
        f"{CLOSE_PATH}?{urlencode(query)}", status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


def close_page(
    *,
    outcome: str,
    flow: Optional[str] = None,
    reason: Optional[str] = None,
    error_code: Optional[str] = None,
) -> HTMLResponse:
    """The self-closing consent page (see the module docstring).

    Every input is coerced to the vocabulary above before it reaches the markup
    or the script: this page is served on the origin that holds every profile's
    session, so what it renders is never taken on trust.
    """
    outcome = _one_of(outcome, OUTCOMES) or "failed"
    flow = _one_of(flow, FLOWS)
    # ``profile`` is always null and deliberately not settable: this address is
    # reachable by anyone, so a profile read from its query would be a claim the
    # server never made. What ties a notice to a waiting page is that the page
    # recognises the window it opened (``fromPopup`` in oauthReturn.ts).
    notice = json.dumps({
        "type": MESSAGE_TYPE, "flow": flow, "outcome": outcome, "profile": None,
    }, separators=(",", ":")).replace("<", "\\u003c")
    closes = outcome == "received"
    body = "".join(
        f"<p>{html.escape(line)}</p>"
        for line in _lines(outcome, flow, _message(outcome, flow, _one_of(reason, REASONS), error_code))
        if line
    )
    script = (
        "(function(){"
        f"var n={notice};"
        "try{if(window.opener&&window.opener!==window)window.opener.postMessage(n,'*');}catch(e){}"
        f"try{{var c=new BroadcastChannel({json.dumps(CHANNEL_NAME)});"
        "c.postMessage(n);c.close();}catch(e){}"
        + (
            # Only a window ``window.open`` created can be closed this way;
            # anything else simply keeps the message it was about to hide.
            "document.body.className='closing';try{window.close();}catch(e){}"
            f"setTimeout(function(){{document.body.className='';}},{_LINGER_MS});"
            if closes else ""
        )
        + "})();"
    )
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    style_hash = base64.b64encode(hashlib.sha256(_STYLE.encode()).digest()).decode()
    return HTMLResponse(
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>Cremind</title>"
        f"<style>{_STYLE}</style></head><body>"
        f"<main><h1>{html.escape(_TITLES[outcome])}</h1>{body}"
        "<p>You can close this tab.</p></main>"
        f"<script>{script}</script></body></html>",
        status_code=200,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'sha256-{script_hash}'; "
                f"style-src 'sha256-{style_hash}'; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
        },
    )


async def _handle_close(request: Request) -> HTMLResponse:
    query = request.query_params
    return close_page(
        outcome=query.get("o", ""),
        flow=query.get("f"),
        reason=query.get("r"),
        error_code=query.get("e"),
    )


def get_oauth_close_routes() -> list[Route]:
    """Where the callbacks send a consent window once their work is done.

    Registered PRE-storage next to the callbacks, for the same reason: it has to
    answer whenever one of them can redirect to it."""
    return [Route(CLOSE_PATH, _handle_close, methods=["GET"])]
