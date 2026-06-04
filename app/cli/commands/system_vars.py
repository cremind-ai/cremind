"""`cremind system-vars` — list env vars Cremind injects into exec_shell subprocesses.

Mirrors `cli/cmd/system_vars.go`.
"""

from __future__ import annotations

import typer

from app.cli.commands._helpers import graceful_errors


@graceful_errors
def system_vars(ctx: typer.Context) -> None:
    """List the env vars Cremind injects into exec_shell subprocesses."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.system_vars import get_system_vars
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list:
        async with Client(cfg) as client:
            return await get_system_vars(client)

    vars_list = asyncio.run(_run())

    if mode.json:
        print_json([
            {"name": v.name, "description": v.description, "value": v.value}
            for v in vars_list
        ])
        return

    table = Table(mode, "NAME", "VALUE", "DESCRIPTION")
    for v in vars_list:
        table.add_row(v.name, v.value, v.description)
    table.render()
