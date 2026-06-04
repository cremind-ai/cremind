"""`cremind me` — show identity info for the current CREMIND_TOKEN.

Mirrors `cli/cmd/me.go`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import typer

from app.cli.commands._helpers import graceful_errors


@graceful_errors
def me(ctx: typer.Context) -> None:
    """Show identity info for the current CREMIND_TOKEN."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.me import get_me
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            return await get_me(client)

    me_info = asyncio.run(_run())

    if mode.json:
        print_json(me_info.to_dict())
        return

    print_kv([
        ("profile", me_info.profile),
        ("subject", me_info.subject),
        ("issued_at", _format_unix(me_info.issued_at)),
        ("expires_at", _format_unix(me_info.expires_at)),
        ("system_dir", me_info.system_dir),
        ("user_working_dir", me_info.user_working_dir),
    ])


def _format_unix(ts: int) -> str:
    if ts == 0:
        return ""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.isoformat(timespec='seconds')} ({ts})"
