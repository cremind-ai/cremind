"""Small HTTP recovery surface and narrowly scoped cross-origin handoffs.

Once HTTPS is enabled, plaintext accepts only this document and public CA.
It never forwards credentials or mutating requests to the real application.
"""
from __future__ import annotations

import base64
import hashlib
import json
import ipaddress
from html import escape

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from app.config.tls_transition import certificate_info, https_target, instance_id, load_transition, origin


class EdgeTlsRecovery:
    """After edge activation, public HTTP has only the recovery surface.

The real internal loopback port remains available to the CLI. ASGI's scheme
must already have been set by the server's trusted-proxy middleware; raw
forwarding headers are never used here to grant secure application access.
"""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from app.config.settings import BaseConfig
        from app.config.tls_mode import edge_tls_termination
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        request = Request({**scope, "type": "http"})
        transition = load_transition()
        secure = scope.get("scheme") in ("https", "wss")
        server_port = (scope.get("server") or (None, None))[1]
        try:
            local_host = ipaddress.ip_address(request.url.hostname or "").is_loopback
        except ValueError:
            local_host = request.url.hostname == "localhost"
        internal = (server_port == BaseConfig.PORT and request.url.port == BaseConfig.PORT
                    and local_host and not any(key in request.headers for key in
                        ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto")))
        # Once activation is durable, the old public HTTP process must stop
        # serving the application immediately, even when a native service or
        # Docker container has not restarted yet.  Otherwise a caller could
        # obtain a fresh current-epoch login token over plaintext during that
        # gap.  A fresh edge-terminated install has no transition to consult,
        # so its configured HTTPS URL is the additional edge-only signal.
        # Do not use APP_URL alone for native installs: after-setup writes its
        # steady-state HTTPS URL before the wizard finishes and still needs the
        # full HTTP application through the prepared/quiescing phases.
        enabled = bool(
            (transition and transition.get("phase") in ("activating", "active"))
            or (edge_tls_termination() and BaseConfig.APP_URL.startswith("https://"))
        )
        if not secure and not internal and enabled:
            await recovery_app(scope, receive, send)
            return
        await self.app(scope, receive, send)


class TlsHandoffCors:
    """Override general API CORS only for the migration's narrow endpoints."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        paths = ("/api/tls/status", "/api/tls/handoff", "/api/tls/handoff/redeem")
        if scope["type"] != "http" or scope.get("path") not in paths:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        supplied = request.headers.get("origin")
        if not supplied:
            await self.app(scope, receive, send)
            return
        try:
            supplied = origin(supplied)
            current = origin(str(request.base_url))
            transition = load_transition()
            # Ticket redemption returns the freshly re-signed profile token.
            # Only the HTTPS destination page may read that response; allowing
            # the old plaintext origin here would hand an injected HTTP script
            # a valid HTTPS credential.  The old origin needs cross-origin
            # access only for readiness status and, while its old token is
            # valid, handoff creation.
            old_origin_allowed = scope.get("path") != "/api/tls/handoff/redeem"
            allowed = supplied == current or (
                old_origin_allowed
                and transition is not None and transition["phase"] != "cancelled"
                and supplied.startswith("http://") and https_target(supplied) == current
            )
        except ValueError:
            allowed = False
        if not allowed:
            await JSONResponse({"error": "This origin is not allowed to perform an HTTPS handoff."}, status_code=403)(scope, receive, send)
            return
        headers = {"Access-Control-Allow-Origin": supplied, "Vary": "Origin",
                   "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                   "Access-Control-Allow-Headers": "Authorization, Content-Type",
                   "Cache-Control": "no-store"}
        if request.method == "OPTIONS":
            await JSONResponse({}, headers=headers)(scope, receive, send)
            return

        async def cors_send(message):
            if message["type"] == "http.response.start":
                existing = [(key, value) for key, value in message.get("headers", [])
                            if not key.lower().startswith(b"access-control-")
                            and key.lower() not in (b"vary", b"cache-control")]
                message = {**message, "headers": existing + [
                    (key.lower().encode(), value.encode()) for key, value in headers.items()]}
            await send(message)
        await self.app(scope, receive, cors_send)


def recovery_document(source_origin: str) -> HTMLResponse:
    # Target comes from the browser's address bar, never an untrusted forwarded
    # Host header. JSON is escaped before insertion into a script element.
    expected = json.dumps(instance_id()).replace("<", "\\u003c")
    transition = load_transition()
    expected_transition = json.dumps(transition["id"] if transition else None).replace("<", "\\u003c")
    configured_target = json.dumps(https_target(source_origin)).replace("<", "\\u003c")
    script = r"""
const expected=EXPECTED;
let expectedTransition=TRANSITION_ID;
const target=new URL(CONFIGURED_TARGET);target.hostname=location.hostname;
const targetOrigin=target.origin;
const route=location.hash.slice(1)||'/';
const mount=location.pathname.startsWith('/electron-renderer')?'/electron-renderer/':'/';
const message=document.getElementById('message');
const link=document.getElementById('open');
const match=route.match(/^\/([a-z0-9_-]+)(?:[/?]|$)/);
const candidate=match?match[1]:'';
const profile=['setup','setup-handoff','tls-handoff','login'].includes(candidate)?'':candidate;
const destination=profile
 ? targetOrigin+mount+'#/login/'+encodeURIComponent(profile)+'?redirect='+encodeURIComponent(route)
 : targetOrigin+mount+'#'+route;
link.href=destination;
let busy=false;
async function recover(){
 if(busy)return;busy=true;
 try{
  const response=await fetch(targetOrigin+'/api/tls/status',{cache:'no-store',credentials:'omit',signal:AbortSignal.timeout(5000)});
  if(!response.ok)throw Error('HTTPS is not ready yet.');
  const status=await response.json();
  if(!status.serving_https||status.ready===false||status.instance_id!==expected||!status.transition||status.transition.phase!=='active'
    ||typeof status.transition.id!=='string'||(expectedTransition&&status.transition.id!==expectedTransition))throw Error('The HTTPS address is not ready for the expected Cremind transition.');
  expectedTransition=status.transition.id;
  location.replace(destination);
 }catch(error){message.textContent=error.message+' This page cannot reach the secure address from here, which usually means this device does not trust the certificate yet. Use Open HTTPS page below and continue past the browser warning to get in now, or trust the CA first to stop the warning. If the server or the port-forward is simply not running, start it and retry.';}
 finally{busy=false;}
}
document.getElementById('retry').onclick=recover;recover();
""".replace("EXPECTED", expected).replace("TRANSITION_ID", expected_transition).replace("CONFIGURED_TARGET", configured_target)
    script_hash = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    info = certificate_info()
    if info["certificate_kind"] == "local":
        advice = ("<p>Download <a href='/ca.pem'>Cremind's CA</a> and trust it on this device. "
                  "Verify its SHA-256 fingerprint with the server administrator: "
                  f"<code>{escape(info['ca_sha256'] or '')}</code>. "
                  "Windows: Current User → Trusted Root Certification Authorities. "
                  "macOS: Keychain Access → Always Trust. "
                  "Linux: your distribution's CA trust store. Restart the browser after importing.</p>")
    else:
        advice = ("<p>This installation uses a supplied certificate or an HTTPS proxy. "
                  "Check that its certificate covers this hostname and is current. "
                  "Follow the certificate issuer's trust instructions; a Cremind CA is not used.</p>")
    html = ("<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
            "<title>Opening Cremind over HTTPS</title></head><body>"
            "<h1>Opening Cremind over HTTPS</h1><p id='message'>Checking the secure address… "
            "For your security, this plaintext recovery page never reads or transfers login credentials. "
            "You may need to sign in again after it preserves this page's route.</p>"
            "<button id='retry'>Retry connection</button> <a id='open'>Open HTTPS page</a>"
            # The way in when the certificate cannot be trusted on this device.
            # Without this the page reads as a dead end, which is how a stalled
            # switch turns into "I cannot reach my data".
            "<p><strong>Locked out?</strong> Cremind is already answering on the "
            "secure address; this plaintext page is all that is left here, and "
            "it deliberately cannot carry application data. Open the HTTPS page "
            "above and accept the browser's certificate warning to get in right "
            "now — trusting the CA below removes the warning for good, but you "
            "do not have to do it first.</p>"
            + advice +
            "<noscript>Enable JavaScript to restore the existing session, or change http:// to https:// in the address bar.</noscript>"
            f"<script>{script}</script></body></html>")
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": f"default-src 'none'; script-src 'sha256-{script_hash}'; connect-src https:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    })


async def recovery_app(scope, receive, send) -> None:
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    if scope["type"] != "http":
        return
    request = Request(scope, receive)
    if request.method in ("GET", "HEAD") and request.url.path == "/ca.pem":
        from app.api.tls import get_ca_pem
        response = await get_ca_pem(request)
    elif (request.method in ("GET", "HEAD")
          and not request.url.path.startswith(("/api", "/.well-known/"))
          and (request.url.path in ("/", "/index.html", "/electron-renderer", "/electron-renderer/", "/electron-renderer/index.html")
               or request.headers.get("sec-fetch-mode") == "navigate"
               or "text/html" in request.headers.get("accept", ""))):
        try:
            response = recovery_document(origin(str(request.base_url)))
        except ValueError:
            response = JSONResponse({"error": "Invalid public origin."}, status_code=400)
    else:
        response = JSONResponse({"error": "HTTPS is required. Update the server URL to https:// before retrying."},
                                status_code=426, headers={"Cache-Control": "no-store"})
    # The plaintext endpoint exists only to complete one navigation or CA
    # download. Closing it after every response avoids a second, partially
    # supplied request occupying a recovery connection indefinitely and makes
    # it explicit that this is not a reusable application transport.
    response.headers["Connection"] = "close"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    await response(scope, receive, send)
