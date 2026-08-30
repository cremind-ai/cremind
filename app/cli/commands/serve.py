"""`cremind serve` — boot the Cremind HTTP server in-process.

The `app.server` import is lazy (inside the function) to keep `cremind --help`
fast — starlette/uvicorn pull in a lot at module-import time.
"""

from __future__ import annotations

import typer


def serve(
    host: str = typer.Option(
        None,
        "--host",
        "-H",
        envvar="HOST",
        help="Bind address. Defaults to BaseConfig.HOST.",
    ),
    port: int = typer.Option(
        None,
        "--port",
        "-p",
        envvar="PORT",
        help="Bind port. Defaults to BaseConfig.PORT.",
    ),
    ssl_certfile: str = typer.Option(
        None,
        "--ssl-certfile",
        envvar="CREMIND_SSL_CERTFILE",
        help=(
            "TLS certificate (PEM). With --ssl-keyfile, serves the public origin "
            "over HTTPS with HTTP/2. The internal API port stays plain HTTP. "
            "Set CREMIND_SSL=auto instead to generate a locally-signed pair."
        ),
    ),
    ssl_keyfile: str = typer.Option(
        None,
        "--ssl-keyfile",
        envvar="CREMIND_SSL_KEYFILE",
        help="TLS private key (PEM). Required with --ssl-certfile.",
    ),
) -> None:
    """Start the Cremind HTTP server in-process.

    These flags configure only this invocation. An in-app restart re-execs the
    server without them, so a persistent setup belongs in ``~/.cremind/.env``.
    """
    import asyncio

    from app.server import main as server_main, DEFAULT_HOST, DEFAULT_PORT

    asyncio.run(
        server_main(
            host=host if host is not None else DEFAULT_HOST,
            port=port if port is not None else DEFAULT_PORT,
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
        )
    )
