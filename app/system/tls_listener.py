"""Same-port HTTP recovery and unchanged TLS bytes relayed to Hypercorn.

Private Hypercorn sockets are reserved before serving. Only connections in the
relay's in-memory map reach ASGI; the map restores the original public TCP peer
without trusting headers. Hypercorn owns TLS, ALPN, HTTP/2 and WebSockets.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
import socket


# Plaintext is only a compatibility landing page, so its first request can be
# bounded much more tightly than an application stream.  Do not put a read
# timeout on Hypercorn itself: that would also close legitimate quiet SSE and
# WebSocket connections after TLS has been negotiated.
HTTP_HEADER_LIMIT = 16 * 1024
HTTP_HEADER_TIMEOUT = 5.0
TLS_HANDSHAKE_TIMEOUT = 10.0
_HTTP_METHODS = frozenset(
    (b"GET", b"HEAD", b"POST", b"PUT", b"PATCH", b"DELETE", b"OPTIONS", b"TRACE", b"CONNECT")
)


async def _read_protocol_prefix(reader: asyncio.StreamReader) -> tuple[bool, bytes]:
    """Classify TLS without inspecting it, or read one bounded HTTP header.

    TLS is deliberately returned after its first byte so every subsequent byte
    reaches Hypercorn unchanged.  Plain HTTP is restricted to HTTP/1.x and a
    complete, reasonably sized initial header.  This prevents idle or
    slow-drip plaintext sockets from consuming one relay and one private-server
    connection forever.
    """
    first = await asyncio.wait_for(reader.readexactly(1), timeout=HTTP_HEADER_TIMEOUT)
    if first == b"\x16":  # TLS handshake record; Hypercorn validates the rest.
        return True, first

    if first not in (b"G", b"H", b"P", b"O", b"D", b"T", b"C"):
        raise ValueError("Unsupported public protocol")
    remainder = await asyncio.wait_for(
        reader.readuntil(b"\r\n\r\n"), timeout=HTTP_HEADER_TIMEOUT
    )
    prefix = first + remainder
    if len(prefix) > HTTP_HEADER_LIMIT:
        raise ValueError("Plaintext header is too large")
    request_line = prefix.split(b"\r\n", 1)[0]
    parts = request_line.split(b" ")
    if (
        len(parts) != 3
        or parts[0] not in _HTTP_METHODS
        or not parts[1]
        or parts[2] not in (b"HTTP/1.0", b"HTTP/1.1")
    ):
        raise ValueError("Unsupported plaintext request")
    return False, prefix


class PrivateRelayApp:
    """Reject direct private-listener access and restore mapped public peers."""

    def __init__(self, app, peers):
        self.app = app
        self.peers = peers

    async def __call__(self, scope, receive, send):
        from app.api.tls_recovery import recovery_app
        from starlette.responses import JSONResponse
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        peer = self.peers.get(tuple(scope.get("client") or ()))
        if peer is None:
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await JSONResponse({"error": "This private listener only accepts the public relay."},
                                   status_code=403)(scope, receive, send)
            return
        client, server, secure = peer
        scope = {**scope, "client": client, "server": server}
        await (self.app if secure else recovery_app)(scope, receive, send)


async def serve_with_http_recovery(app, config, *, shutdown_trigger) -> None:
    from hypercorn.asyncio import serve
    from hypercorn.config import Sockets

    loop = asyncio.get_running_loop()
    public_sockets = config.create_sockets().secure_sockets
    reserved = []
    for _ in range(2):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        listener.listen(config.backlog)
        reserved.append(listener)
    tls_socket, plain_socket = reserved
    private_config = copy.copy(config)
    private_config.bind = [f"127.0.0.1:{tls_socket.getsockname()[1]}"]
    private_config.insecure_bind = [f"127.0.0.1:{plain_socket.getsockname()[1]}"]
    # Supply the already-reserved sockets through Config's socket factory;
    # closing/rebinding the ephemeral ports would create an impersonation race.
    private_config.create_sockets = lambda: Sockets([tls_socket], [plain_socket], [])
    # Explicit parser bounds make the recovery listener's security properties
    # independent of Hypercorn defaults.  The initial plaintext header also has
    # the absolute deadline above; TLS handshake parsing is bounded here while
    # established HTTP/2, SSE and WebSocket streams remain unlimited in length.
    private_config.h11_max_incomplete_size = HTTP_HEADER_LIMIT
    private_config.h2_max_header_list_size = 64 * 1024
    private_config.ssl_handshake_timeout = TLS_HANDSHAKE_TIMEOUT
    peers: dict = {}
    clients: set[asyncio.Task] = set()
    stopping = asyncio.Event()
    server = asyncio.create_task(serve(PrivateRelayApp(app, peers), private_config,
                                      shutdown_trigger=stopping.wait))

    async def copy_bytes(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()

    async def handle_client(reader, writer):
        task = asyncio.current_task()
        clients.add(task)
        outbound = None
        upstream = None
        key = None
        pumps = []
        try:
            secure, prefix = await _read_protocol_prefix(reader)
            outbound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            outbound.setblocking(False)
            outbound.bind(("127.0.0.1", 0))
            key = outbound.getsockname()
            peers[key] = (writer.get_extra_info("peername")[:2], writer.get_extra_info("sockname")[:2], secure)
            destination = (tls_socket if secure else plain_socket).getsockname()
            await asyncio.wait_for(loop.sock_connect(outbound, destination), timeout=5)
            upstream_reader, upstream = await asyncio.open_connection(sock=outbound)
            outbound = None  # ownership transferred to the StreamWriter
            upstream.write(prefix)
            await upstream.drain()
            client_to_upstream = asyncio.create_task(copy_bytes(reader, upstream))
            upstream_to_client = asyncio.create_task(copy_bytes(upstream_reader, writer))
            pumps = [client_to_upstream, upstream_to_client]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                finished.result()
            if upstream_to_client in done:
                # Once the server side is gone there can be no further useful
                # client bytes.  A client-side EOF is different: it is a TCP
                # half-close, and the response (including a streamed response)
                # must still be allowed to finish in the other direction.
                client_to_upstream.cancel()
            else:
                await upstream_to_client
        except (
            OSError,
            TimeoutError,
            ConnectionError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            pass
        finally:
            for pump in pumps:
                pump.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            if key is not None:
                peers.pop(key, None)
            if outbound is not None:
                outbound.close()
            for stream in (upstream, writer):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(OSError, TimeoutError):
                        await asyncio.wait_for(stream.wait_closed(), 2)
            clients.discard(task)

    public = []
    stop = asyncio.create_task(shutdown_trigger())
    try:
        for listener in public_sockets:
            public.append(
                await asyncio.start_server(
                    handle_client,
                    sock=listener,
                    backlog=config.backlog,
                    limit=HTTP_HEADER_LIMIT,
                )
            )
        done, _ = await asyncio.wait([stop, server], return_when=asyncio.FIRST_COMPLETED)
        if server in done:
            server.result()
    finally:
        # On Python 3.13 ``Server.wait_closed`` also waits for every accepted
        # connection handler. Stop accepting first, but drain/cancel those
        # handlers before awaiting the listener or an unidentified client can
        # hold shutdown until the classification timeout (or longer for an
        # established stream).
        for listener in public:
            listener.close()
        for listener in public_sockets:
            listener.close()
        stopping.set()
        stop.cancel()
        await asyncio.gather(stop, return_exceptions=True)
        # Let callbacks for sockets accepted immediately before ``close`` enter
        # ``handle_client`` and register themselves before taking the snapshot.
        await asyncio.sleep(0)
        client_tasks = tuple(clients)
        if client_tasks:
            _, pending = await asyncio.wait(
                client_tasks, timeout=config.graceful_timeout
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*client_tasks, return_exceptions=True)
        for listener in public:
            await listener.wait_closed()
        try:
            await server
        finally:
            for listener in reserved:
                listener.close()
