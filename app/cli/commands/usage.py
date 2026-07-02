"""`cremind usage` — token-usage + estimated-cost summary.

The terminal-side counterpart to the **Usage & Cost** dashboard. Shows grand
totals, cache-hit rate, and breakdowns by model / provider / source plus the
top conversations. Results are scoped to the caller's profile; the ``admin``
profile may pass ``--profile`` to inspect another (or omit it to span all).
"""

from __future__ import annotations

from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


def _dump_rows(mode, title: str, rows: list) -> None:
    """Render a list-of-dicts as a table with headers from the first row."""
    import sys

    from app.cli.output import Table

    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return
    sys.stdout.write(f"\n{title}\n")
    keys = list(rows[0].keys())
    table = Table(mode, *[k.upper() for k in keys])
    for r in rows:
        table.add_row(*[r.get(k) for k in keys])
    table.render()


@graceful_errors
def usage(
    ctx: typer.Context,
    start: Optional[float] = typer.Option(None, "--start", help="Window start (epoch milliseconds)."),
    end: Optional[float] = typer.Option(None, "--end", help="Window end (epoch milliseconds)."),
    tz_offset: int = typer.Option(0, "--tz-offset", help="Minutes east of UTC for daily bucketing."),
    profile: Optional[str] = typer.Option(None, "--profile", help="Inspect another profile (admin only)."),
) -> None:
    """Show the token-usage & cost summary."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.usage import get_summary
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_map

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_summary(
                client,
                start_ms=start,
                end_ms=end,
                tz_offset_min=tz_offset,
                profile=profile,
            )

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    headline = {
        "request_count": out.get("request_count"),
        "conversation_count": out.get("conversation_count"),
        "cache_hit_rate": out.get("cache_hit_rate"),
        "cache_read_usd": out.get("cache_read_usd"),
        "cache_write_usd": out.get("cache_write_usd"),
        "has_unpriced": out.get("has_unpriced"),
    }
    totals = out.get("totals")
    if isinstance(totals, dict):
        headline.update(totals)
    print_map(headline)

    _dump_rows(mode, "By model", out.get("by_model") or [])
    _dump_rows(mode, "By provider", out.get("by_provider") or [])
    _dump_rows(mode, "By source", out.get("by_source") or [])
    _dump_rows(mode, "Top conversations", out.get("top_conversations") or [])
