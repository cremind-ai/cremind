"""The client-protocol guard: outdated clients cannot write where ids changed meaning.

After the search-tool rename ``documentation_search`` names the user's own
documents and Cremind's manual is ``cremind_documentation_search``. A UI tab or
CLI built before that would still send the old meaning, so tool-configuration,
setup and cleanup writes require ``X-Cremind-Client-Protocol: 2`` and answer
anything older with 426 ``ClientUpgradeRequired`` — before the handler runs.

The app here is the production middleware order around stub handlers on the
real paths: the guard's contract is about paths and methods, not what the
handlers do, and a stub that records its calls proves nothing was written.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.middleware import CLIENT_PROTOCOL_HEADER, CLIENT_PROTOCOL_VERSION, ClientProtocolGuard
from app.middleware.client_protocol import (
    CLIENT_UPGRADE_MESSAGE,
    parse_client_protocol,
    requires_current_client,
)

ORIGIN = "http://localhost:1515"

# (method, path) of every gated write, one per route family and handler.
GATED = [
    ("PUT", "/api/tools/documentation_search/enabled"),
    ("PUT", "/api/tools/cremind_documentation_search/variables"),
    ("PUT", "/api/tools/exec_shell/arguments"),
    ("PUT", "/api/tools/documentation_search/leaves"),
    ("POST", "/api/tools/skill.daily-brief/long-running-app/register"),
    ("PUT", "/api/agents/documentation_search/enabled"),
    ("PUT", "/api/agents/documentation_search/config"),
    ("POST", "/api/config/setup"),
    ("POST", "/api/clean"),
]

# Reads on the same families, and writes elsewhere: never blocked.
OPEN = [
    ("GET", "/api/tools"),
    ("GET", "/api/tools/documentation_search"),
    ("GET", "/api/tools/documentation_search/leaves"),
    ("GET", "/api/tools/claude_code/variable-options"),
    ("GET", "/api/config/setup-status"),
    ("POST", "/api/config/reconfigure"),
    ("PUT", "/api/config/server"),
    ("POST", "/api/agents/linear/reconnect"),
    ("POST", "/api/conversations"),
    ("POST", "/api/features/install"),
    ("POST", "/api/tls/handoff"),
]


def _app(calls: list[str]) -> Starlette:
    async def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        return JSONResponse({"ok": True})

    paths = {path for _, path in GATED + OPEN}
    routes = [
        Route(path, handler, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        for path in sorted(paths)
    ]
    return Starlette(routes=routes, middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=[ORIGIN],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        ),
        Middleware(ClientProtocolGuard),
    ])


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def client(calls) -> TestClient:
    return TestClient(_app(calls))


def _current() -> dict[str, str]:
    return {CLIENT_PROTOCOL_HEADER: str(CLIENT_PROTOCOL_VERSION)}


@pytest.mark.parametrize("method,path", GATED)
def test_a_gated_write_without_the_marker_is_refused_before_the_handler(client, calls, method, path):
    response = client.request(method, path, json={})

    assert response.status_code == 426
    body = response.json()
    assert body["error"] == "ClientUpgradeRequired"
    assert body["message"] == CLIENT_UPGRADE_MESSAGE
    assert body["required_protocol"] == CLIENT_PROTOCOL_VERSION
    assert body["client_protocol"] is None
    assert response.headers["cache-control"] == "no-store"
    assert calls == []


@pytest.mark.parametrize("method,path", GATED)
@pytest.mark.parametrize("stale", ["1", "0", "banana", ""])
def test_an_older_or_unreadable_marker_is_refused(client, calls, method, path, stale):
    response = client.request(method, path, json={}, headers={CLIENT_PROTOCOL_HEADER: stale})

    assert response.status_code == 426
    assert response.json()["client_protocol"] == parse_client_protocol(stale)
    assert calls == []


@pytest.mark.parametrize("method,path", GATED)
def test_a_current_marker_reaches_the_handler(client, calls, method, path):
    response = client.request(method, path, json={}, headers=_current())

    assert response.status_code == 200
    assert calls == [f"{method} {path}"]


def test_a_newer_client_is_let_through(client, calls):
    response = client.put(
        "/api/tools/documentation_search/enabled", json={"enabled": True},
        headers={CLIENT_PROTOCOL_HEADER: str(CLIENT_PROTOCOL_VERSION + 1)},
    )
    assert response.status_code == 200
    assert calls


@pytest.mark.parametrize("method,path", OPEN)
def test_reads_and_unrelated_writes_are_never_blocked(client, calls, method, path):
    response = client.request(method, path, json={} if method != "GET" else None)

    assert response.status_code == 200
    assert calls == [f"{method} {path}"]


def test_the_header_name_is_case_insensitive(client, calls):
    response = client.post("/api/clean", json={},
                           headers={CLIENT_PROTOCOL_HEADER.lower(): str(CLIENT_PROTOCOL_VERSION)})
    assert response.status_code == 200


def test_a_cross_origin_preflight_is_answered_and_allows_the_marker(client, calls):
    """The browser asks before it sends the marker cross-origin; CORS answers
    first, and the guard never sees an OPTIONS."""
    response = client.options("/api/tools/documentation_search/enabled", headers={
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": f"authorization, content-type, {CLIENT_PROTOCOL_HEADER.lower()}",
    })

    assert response.status_code == 200
    assert CLIENT_PROTOCOL_HEADER.lower() in response.headers["access-control-allow-headers"].lower()
    assert calls == []


def test_the_refusal_carries_cors_headers_so_a_cross_origin_ui_can_read_it(client):
    response = client.put("/api/tools/documentation_search/enabled", json={}, headers={"Origin": ORIGIN})

    assert response.status_code == 426
    assert response.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.parametrize("method,path,expected", [
    ("put", "/api/tools/x/enabled", True),
    ("PATCH", "/api/tools/x/variables", True),
    ("DELETE", "/api/tools/x", True),
    ("POST", "/api/tools", True),
    ("POST", "/api/clean/", True),
    ("POST", "/api/config/setup/", True),
    ("PUT", "/api/agents/x/enabled/", True),
    ("HEAD", "/api/clean", False),
    ("OPTIONS", "/api/tools/x/enabled", False),
    ("GET", "/api/config/setup", False),
    ("POST", "/api/config/setup-profiles", False),
    ("POST", "/api/config/setup/stream", False),
    ("POST", "/api/tools-extra", False),
    ("POST", "/api/cleaner", False),
    ("PUT", "/api/agents/x/config", True),
    ("DELETE", "/api/agents/x", False),
    ("POST", "/api/documentation-search/control", False),
    ("PUT", "/api/conversations/c1/search-tools", False),
])
def test_the_gate_matches_exactly_the_routes_whose_ids_changed(method, path, expected):
    assert requires_current_client(method, path) is expected


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    ("", None),
    ("2", 2),
    (" 2 ", 2),
    (b"2", 2),
    ("2, 1", 2),
    ("v2", None),
    ("1", 1),
])
def test_parse_client_protocol(raw, expected):
    assert parse_client_protocol(raw) == expected


def test_websocket_scopes_pass_straight_through():
    import asyncio

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    asyncio.run(ClientProtocolGuard(inner)({"type": "websocket", "path": "/api/tools/x/enabled"}, None, None))
    assert seen == ["websocket"]


def test_the_tls_handoff_preflight_allows_the_marker(monkeypatch):
    """/api/tls/* answers its own preflights with a fixed header list. Updated
    clients send the marker on every request, so a list without it would fail
    a cross-origin HTTPS handoff at the preflight."""
    import app.api.tls_recovery as tls_recovery

    monkeypatch.setattr(tls_recovery, "load_transition", lambda: None)

    async def handoff(request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[Route("/api/tls/handoff", handoff, methods=["POST", "OPTIONS"])],
        middleware=[Middleware(tls_recovery.TlsHandoffCors)],
    )
    response = TestClient(app).options("/api/tls/handoff", headers={
        "Origin": "http://testserver",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": CLIENT_PROTOCOL_HEADER.lower(),
    })

    assert response.status_code == 200
    allowed = [h.strip().lower() for h in response.headers["access-control-allow-headers"].split(",")]
    assert CLIENT_PROTOCOL_HEADER.lower() in allowed
    assert {"authorization", "content-type"} <= set(allowed)


def test_the_cli_and_the_server_agree_on_the_marker():
    """The CLI spells the marker out (it may not import server modules); this
    pins the two copies together."""
    from app.cli.client import _base

    assert _base.CLIENT_PROTOCOL_HEADER == CLIENT_PROTOCOL_HEADER
    assert _base.CLIENT_PROTOCOL_VERSION == CLIENT_PROTOCOL_VERSION


def test_the_web_ui_and_the_server_agree_on_the_marker():
    """The SPA carries a third copy; a version bump must move all three."""
    import re
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "ui" / "src" / "services" / "clientProtocol.ts"
    if not source.exists():
        pytest.skip("web UI sources not present")
    text = source.read_text(encoding="utf-8")
    header = re.search(r"CLIENT_PROTOCOL_HEADER\s*=\s*'([^']+)'", text)
    version = re.search(r"CLIENT_PROTOCOL_VERSION\s*=\s*'?(\d+)'?", text)
    assert header and header.group(1) == CLIENT_PROTOCOL_HEADER
    assert version and int(version.group(1)) == CLIENT_PROTOCOL_VERSION


def test_the_server_stack_installs_the_guard_inside_cors_and_before_auth():
    """Order matters: inside CORS so preflights are answered first and the
    refusal still gets CORS headers; before auth so a stale client learns to
    upgrade rather than chase a 401."""
    import inspect

    import app.server as server

    source = inspect.getsource(server)
    stack = source[source.index("middleware_stack = ["):]
    stack = stack[:stack.index("]\n")]
    cors = stack.index("CORSMiddleware")
    guard = stack.index("Middleware(ClientProtocolGuard)")
    auth = stack.index("AuthenticationMiddleware")
    assert cors < guard < auth
