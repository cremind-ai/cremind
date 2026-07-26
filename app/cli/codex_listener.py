"""Client-side capture of the Codex ("Sign in with ChatGPT") OAuth redirect.

OpenAI's Codex OAuth client has a **fixed** redirect URI,
``http://localhost:1455/auth/callback``. "localhost" means *the machine running
the browser* — which for `cremind llm codex-oauth login` against a remote server
(a VPS behind an Ingress, a Docker/K8s install without the port mapped) is this
machine, not the server. The server's own listener is then unreachable no matter
what, and the user is left copy-pasting the redirect URL by hand.

So the CLI binds port 1455 here, catches the redirect itself, and relays the
authorization code to the server through the ordinary
``POST /api/llm/auth/codex/complete`` endpoint. The code is single-use and
PKCE-bound to a flow the server minted, so nothing sensitive is decided locally —
this is purely a transport hop for a value the browser can only hand to
``localhost``.

On a **native** install the server already owns port 1455, so :meth:`start` fails
and the caller simply relies on server-side capture. The two are complementary
and the caller races them.

Stdlib only, and no imports from ``app.server`` / ``app.api`` / ``app.tools`` /
``app.storage`` — see the import-discipline note in ``app/cli/main.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlsplit

# Must match app.lib.llm.codex_auth.CODEX_CALLBACK_PORT, but is duplicated
# rather than imported: that module pulls in httpx and lives on the server side
# of the CLI import boundary.
CALLBACK_PORT = 1455
CALLBACK_PATHS = ("/auth/callback", "/callback")

_MAX_REQUEST_BYTES = 16384
_READ_TIMEOUT = 10.0

_SUCCESS_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    "<h1>Signed in to ChatGPT</h1>"
    "<p>You can close this window and return to your terminal.</p>"
    "<script>setTimeout(function(){window.close()},2000)</script></body></html>"
)
_ERROR_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Cremind</title></head>"
    "<body style='font-family:sans-serif;text-align:center;padding-top:3rem'>"
    "<h1>Sign-in failed</h1>"
    "<p>You can close this window and try signing in again.</p></body></html>"
)


@dataclass(frozen=True)
class CallbackResult:
    """One captured redirect. Exactly one of the two fields is non-empty."""

    redirect_url: str = ""
    error: str = ""


def _http_response(status_line: str, html: str) -> bytes:
    body = html.encode("utf-8")
    head = (
        f"HTTP/1.1 {status_line}\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode("latin-1") + body


class LocalCallbackListener:
    """Loopback HTTP server that catches exactly one Codex OAuth redirect.

    Bound to ``127.0.0.1`` only — the redirect is a local browser hop and has no
    business being reachable from the network. Requests that don't carry the
    ``state`` this flow is waiting for are answered with the error page but do
    **not** resolve the wait, so a stale tab from an earlier attempt can't
    hijack or abort the current sign-in.
    """

    def __init__(self, state: str, port: int = CALLBACK_PORT) -> None:
        self._state = state
        self._port = port
        self._server: Optional[asyncio.AbstractServer] = None
        # Created in start(), so the future is bound to the loop that will
        # actually run the server rather than whichever one existed at
        # construction time.
        self._result: "Optional[asyncio.Future[CallbackResult]]" = None

    async def start(self) -> bool:
        """Try to bind. ``False`` means the port is taken (native install, or a
        `codex` CLI login in progress) — not an error, just no local capture."""
        self._result = asyncio.get_running_loop().create_future()
        try:
            self._server = await asyncio.start_server(
                self._handle, "127.0.0.1", self._port,
            )
        except OSError:
            self._server = None
            return False
        return True

    async def wait(self) -> CallbackResult:
        """Block until a matching redirect arrives. Never times out on its own —
        the caller bounds it (the sign-in request itself expires server-side)."""
        if self._result is None:
            raise RuntimeError("start() must succeed before wait()")
        return await self._result

    def close(self) -> None:
        """Release the port. Deliberately synchronous: this also runs while a
        ``KeyboardInterrupt`` unwinds the event loop, where awaiting
        ``wait_closed()`` is not dependable."""
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except Exception:  # noqa: BLE001
                pass
        if self._result is not None and not self._result.done():
            self._result.cancel()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Serve one callback request. Never raises out — an exception here would
        kill the serving task and strand the sign-in."""
        try:
            data = b""
            try:
                while b"\r\n\r\n" not in data and len(data) < _MAX_REQUEST_BYTES:
                    chunk = await asyncio.wait_for(reader.read(1024), timeout=_READ_TIMEOUT)
                    if not chunk:
                        break
                    data += chunk
            except asyncio.TimeoutError:
                pass

            request_line = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
            parts = request_line.split(" ")
            method = parts[0] if parts else ""
            target = parts[1] if len(parts) > 1 else "/"

            if method != "GET":
                writer.write(_http_response("405 Method Not Allowed", _ERROR_HTML))
                await writer.drain()
                return

            split = urlsplit(target)
            if split.path not in CALLBACK_PATHS:
                writer.write(_http_response("404 Not Found", _ERROR_HTML))
                await writer.drain()
                return

            params = parse_qs(split.query)
            state = (params.get("state") or [""])[0]
            error = (params.get("error") or [""])[0]
            code = (params.get("code") or [""])[0]

            ok = False
            if state == self._state and self._result is not None and not self._result.done():
                if error:
                    self._result.set_result(CallbackResult(
                        error=(params.get("error_description") or [error])[0],
                    ))
                elif code:
                    # Hand the server the canonical redirect URL rather than the
                    # bare query, so the value matches what `codex-oauth
                    # complete` documents and what the user would have pasted.
                    self._result.set_result(CallbackResult(
                        redirect_url=f"http://localhost:{self._port}{split.path}?{split.query}",
                    ))
                    ok = True

            writer.write(_http_response("200 OK", _SUCCESS_HTML if ok else _ERROR_HTML))
            await writer.drain()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
