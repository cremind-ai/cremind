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
) -> None:
    """Start the Cremind HTTP server in-process."""
    import asyncio

    from app.server import main as server_main, DEFAULT_HOST, DEFAULT_PORT

    asyncio.run(
        server_main(
            host=host if host is not None else DEFAULT_HOST,
            port=port if port is not None else DEFAULT_PORT,
        )
    )
