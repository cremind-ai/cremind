"""Refuse outdated clients where an identifier changed meaning.

The search-tool rename reassigned a tool id: ``documentation_search`` used to be
Cremind's own manual and is now the user's own documents; the manual moved to
``cremind_documentation_search``. A web UI tab or a ``cremind`` CLI built before
the rename still sends the old meaning — "turn off ``documentation_search``" from
it would switch off the user's files instead of the manual, silently. Renaming
an endpoint makes an old client fail loudly (404/410); reassigning an id does
not, so the server has to be told which vocabulary a client speaks.

Updated clients say so with one request header, :data:`CLIENT_PROTOCOL_HEADER`
carrying :data:`CLIENT_PROTOCOL_VERSION`. The web UI adds it to its requests and
the CLI's :class:`app.cli.client._base.Client` to every request it makes. This
guard answers a mutation on a gated route that lacks the marker, or carries an
older one, with **426 Upgrade Required** and a ``ClientUpgradeRequired`` body —
before the handler runs, so nothing is written under the old meaning.

What is gated, and why only that:

- **Tool configuration** — every ``POST``/``PUT``/``PATCH``/``DELETE`` under
  ``/api/tools`` (enable, variables, arguments, sub-tools, long-running app)
  and ``PUT /api/agents/{tool_id}/enabled`` / ``PUT /api/agents/{tool_id}/config``,
  the legacy agent routes that write a tool's per-profile enabled state and its
  description for any tool id, built-ins included.
- **Setup** — ``POST /api/config/setup``, whose ``tool_configs`` payload is
  keyed by tool id. Its only in-repo callers are the web UI's Setup Wizard and
  the CLI (``cremind setup complete``, ``cremind profile wizard``), both of
  which send the marker; no installer script, Helm hook or Electron main-process
  code calls it. The rest of ``/api/config/*`` (server settings, embedding,
  reconfigure, orphaned-setup reset) carries no tool id and stays open.
- **Cleanup** — ``POST /api/clean``, whose component keys moved with the
  rename (``documentation_search`` is now the personal index).

Reads (``GET``/``HEAD``/``OPTIONS``) and every other route are never blocked:
an old client can still look, it just cannot write under a vocabulary the
server no longer means. A CORS preflight is answered by ``CORSMiddleware``
before this guard, and this guard's refusal passes back out through it, so a
cross-origin UI can read the explanation.

Why 426 and not 409: an old UI that does not parse the body falls back to the
response's status text, and "Upgrade Required" says what to do where
"Conflict" would not. The TLS recovery surface also answers 426, but clients
only interpret that one on ``/api/tls/*``, which is never gated here, and the
``error`` field tells the two apart.

Pure ASGI, like the rest of :mod:`app.middleware`: it reads only the method,
the path and one header, and never touches the body.
"""

from __future__ import annotations

import re

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.utils.logger import logger

#: The request header an updated client sends.
CLIENT_PROTOCOL_HEADER = "X-Cremind-Client-Protocol"
#: The protocol this server requires on gated routes. Bump it — and every
#: client with it — the next time an identifier changes meaning.
CLIENT_PROTOCOL_VERSION = 2

CLIENT_UPGRADE_REQUIRED = "ClientUpgradeRequired"
CLIENT_UPGRADE_MESSAGE = (
    "This Cremind client is older than the server: tool ids changed meaning "
    "(documentation_search is now the user's own documents; Cremind's manual is "
    "cremind_documentation_search). Update the web UI / CLI (pip install -U cremind) "
    "and retry."
)

_HEADER_KEY = CLIENT_PROTOCOL_HEADER.lower().encode("latin-1")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXACT_GATED_PATHS = frozenset({"/api/config/setup", "/api/clean", "/api/tools"})
_AGENT_ENABLED_RE = re.compile(r"^/api/agents/[^/]+/(?:enabled|config)$")


def requires_current_client(method: str, path: str) -> bool:
    """Whether a request must carry the current client protocol.

    ``path`` is the ASGI path (percent-decoded, no query). A trailing slash is
    ignored, so ``/api/clean/`` is gated like ``/api/clean`` rather than
    slipping through to Starlette's slash redirect.
    """
    if (method or "").upper() not in _MUTATING_METHODS:
        return False
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    if path in _EXACT_GATED_PATHS or path.startswith("/api/tools/"):
        return True
    return bool(_AGENT_ENABLED_RE.match(path))


def parse_client_protocol(raw: str | bytes | None) -> int | None:
    """The protocol number a header value names, or ``None`` when it names
    none (absent, empty, not an integer). A repeated header collapses to its
    first value."""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("latin-1", errors="replace")
    first = raw.split(",", 1)[0].strip()
    try:
        return int(first)
    except ValueError:
        return None


def client_upgrade_response(client_protocol: int | None) -> JSONResponse:
    """The refusal every gated route returns to an outdated client."""
    return JSONResponse(
        {
            "error": CLIENT_UPGRADE_REQUIRED,
            "message": CLIENT_UPGRADE_MESSAGE,
            "required_protocol": CLIENT_PROTOCOL_VERSION,
            "client_protocol": client_protocol,
        },
        status_code=426,
        headers={"Cache-Control": "no-store"},
    )


class ClientProtocolGuard:
    """Reject gated mutations from clients older than :data:`CLIENT_PROTOCOL_VERSION`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not requires_current_client(
            scope.get("method", ""), scope.get("path", "")
        ):
            await self.app(scope, receive, send)
            return
        raw = next((value for key, value in scope.get("headers") or () if key == _HEADER_KEY), None)
        version = parse_client_protocol(raw)
        if version is not None and version >= CLIENT_PROTOCOL_VERSION:
            await self.app(scope, receive, send)
            return
        logger.info(
            f"[client-protocol] refused {scope.get('method')} {scope.get('path')}: "
            f"client protocol {version if version is not None else 'missing'} < {CLIENT_PROTOCOL_VERSION}"
        )
        await client_upgrade_response(version)(scope, receive, send)


__all__ = [
    "CLIENT_PROTOCOL_HEADER",
    "CLIENT_PROTOCOL_VERSION",
    "CLIENT_UPGRADE_MESSAGE",
    "CLIENT_UPGRADE_REQUIRED",
    "ClientProtocolGuard",
    "client_upgrade_response",
    "parse_client_protocol",
    "requires_current_client",
]
