"""Response headers that HTTP/2 forbids must not leave the app.

Fifteen SSE endpoints set ``Connection: keep-alive``, and ``sse_starlette``
(via a2a-sdk, so not ours to edit) sets it on every event stream. RFC 9113
§8.2.2 forbids those fields over HTTP/2, which the TLS path speaks.

The h2 library currently strips them on send, so these are not load-bearing
against today's stack — they pin the app's own output as legal, so correctness
does not rest on a private helper inside a transitive dependency.

Driven through ``asyncio.run`` rather than a test client: the unit under test
is an ASGI wrapper, and the messages it rewrites are the thing to assert on.
"""

from __future__ import annotations

import asyncio

from app.middleware import ConnectionHeaderFilter


def _drive(
    scope: dict,
    headers: list[tuple[bytes, bytes]],
) -> list[tuple[bytes, bytes]]:
    """Send one response through the filter; return the headers that got out."""
    seen: list[dict] = []

    async def _app(scope, receive, send):
        await send(
            {"type": "http.response.start", "status": 200, "headers": list(headers)}
        )
        await send({"type": "http.response.body", "body": b"ok"})

    async def _send(message):
        seen.append(message)

    asyncio.run(ConnectionHeaderFilter(_app)(scope, None, _send))
    start = next(m for m in seen if m["type"] == "http.response.start")
    return start["headers"]


def test_connection_specific_headers_are_dropped() -> None:
    out = _drive(
        {"type": "http"},
        [
            (b"content-type", b"text/event-stream"),
            (b"connection", b"keep-alive"),
            (b"keep-alive", b"timeout=5"),
            (b"proxy-connection", b"keep-alive"),
            (b"transfer-encoding", b"chunked"),
            (b"upgrade", b"h2c"),
        ],
    )
    assert out == [(b"content-type", b"text/event-stream")]


def test_the_headers_an_sse_stream_actually_needs_survive() -> None:
    """The point is to drop only what HTTP/2 rejects. Losing no-cache or
    X-Accel-Buffering would break streaming through a proxy instead."""
    out = _drive(
        {"type": "http"},
        [
            (b"content-type", b"text/event-stream"),
            (b"cache-control", b"no-cache"),
            (b"x-accel-buffering", b"no"),
            (b"connection", b"keep-alive"),
        ],
    )
    assert out == [
        (b"content-type", b"text/event-stream"),
        (b"cache-control", b"no-cache"),
        (b"x-accel-buffering", b"no"),
    ]


def test_header_names_are_matched_case_insensitively() -> None:
    """ASGI does not promise lowercase names, and Starlette is not the only
    thing that writes them here."""
    out = _drive(
        {"type": "http"},
        [(b"Connection", b"keep-alive"), (b"Content-Type", b"text/plain")],
    )
    assert out == [(b"Content-Type", b"text/plain")]


def test_a_response_with_nothing_to_strip_is_unchanged() -> None:
    headers = [(b"content-type", b"application/json"), (b"etag", b'"abc"')]
    assert _drive({"type": "http"}, headers) == headers


def test_websocket_scopes_pass_straight_through() -> None:
    """The PTY and process terminals are WebSockets, and their handshake
    messages are not http.response.start — the filter must not touch them."""
    seen: list[dict] = []

    async def _app(scope, receive, send):
        await send({"type": "websocket.accept", "subprotocol": "bearer"})

    async def _send(message):
        seen.append(message)

    asyncio.run(ConnectionHeaderFilter(_app)({"type": "websocket"}, None, _send))
    assert seen == [{"type": "websocket.accept", "subprotocol": "bearer"}]


def test_lifespan_scopes_pass_straight_through() -> None:
    seen: list[dict] = []

    async def _app(scope, receive, send):
        await send({"type": "lifespan.startup.complete"})

    async def _send(message):
        seen.append(message)

    asyncio.run(ConnectionHeaderFilter(_app)({"type": "lifespan"}, None, _send))
    assert seen == [{"type": "lifespan.startup.complete"}]


def test_the_response_body_still_arrives() -> None:
    """Rewriting the start message must not swallow what follows it."""
    seen: list[dict] = []

    async def _app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"connection", b"keep-alive")],
            }
        )
        await send({"type": "http.response.body", "body": b"data: hi\n\n"})

    async def _send(message):
        seen.append(message)

    asyncio.run(ConnectionHeaderFilter(_app)({"type": "http"}, None, _send))
    assert [m["type"] for m in seen] == ["http.response.start", "http.response.body"]
    assert seen[1]["body"] == b"data: hi\n\n"
