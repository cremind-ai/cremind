"""`cremind logs ...` — tail the live server-log stream.

The web UI's Developer page renders the same feed (`GET /api/server/logs/stream`,
admin-only): a ring-buffer backfill of the most recent records followed by a
live tail. This exposes it to the CLI. Distinct from `cremind proc stream`,
which streams per-process snapshots — this is the whole-server log.

Filtering (`--level`, `--grep`) is client-side: the server sends every record
and the command drops the ones that don't match, exactly as the Developer page
does.
"""

from __future__ import annotations

import json as _json
import re
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


logs_app = typer.Typer(
    name="logs",
    help="Tail the live server-log stream (admin).",
    no_args_is_help=True,
)


# Loguru level → severity rank, for the `--level` minimum-level filter.
_LEVEL_RANK = {
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 25,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}
# The levels the Developer page exposes as filter chips (accepted `--level`
# values); `WARN` is tolerated as an alias for `WARNING`.
_ALLOWED_LEVEL_INPUTS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_ALIASES = {"WARN": "WARNING"}

_TS_RE = re.compile(r"T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?")


def _resolve_level(level: Optional[str]) -> Optional[int]:
    """Turn a `--level` string into a minimum severity rank (or None)."""
    if not level:
        return None
    key = _LEVEL_ALIASES.get(level.strip().upper(), level.strip().upper())
    if key not in _ALLOWED_LEVEL_INPUTS:
        raise typer.BadParameter(
            f"unknown level {level!r}; choose one of "
            + ", ".join(_ALLOWED_LEVEL_INPUTS)
        )
    return _LEVEL_RANK[key]


def _fmt_ts(iso: str) -> str:
    """ISO 8601 → `HH:MM:SS.mmm` (mirrors the Developer page); raw on no match."""
    if not iso:
        return ""
    m = _TS_RE.search(iso)
    if not m:
        return iso
    millis = (m.group(4) or "").ljust(3, "0")[:3]
    return f"{m.group(1)}:{m.group(2)}:{m.group(3)}.{millis}"


def _matches(rec: dict[str, Any], min_rank: Optional[int], needle: str) -> bool:
    if min_rank is not None:
        rank = _LEVEL_RANK.get(str(rec.get("level") or "").upper())
        # Unknown levels are kept — a level filter never silently hides a
        # record whose severity we can't rank.
        if rank is not None and rank < min_rank:
            return False
    if needle:
        source = str(rec.get("source") or "").lower()
        message = str(rec.get("message") or "").lower()
        if needle not in source and needle not in message:
            return False
    return True


def _render(rec: dict[str, Any]) -> str:
    ts = _fmt_ts(str(rec.get("ts") or ""))
    level = str(rec.get("level") or "")
    source = str(rec.get("source") or "")
    message = str(rec.get("message") or "")
    return f"{ts:<12} {level:<8} {source}  {message}"


@logs_app.command("tail")
@graceful_errors
def logs_tail(
    ctx: typer.Context,
    level: Optional[str] = typer.Option(
        None, "--level",
        help="Minimum level to show (DEBUG|INFO|WARNING|ERROR|CRITICAL). "
             "Client-side filter; the server always sends every record.",
    ),
    grep: Optional[str] = typer.Option(
        None, "--grep",
        help="Only show records whose source or message contains this text "
             "(case-insensitive).",
    ),
    no_follow: bool = typer.Option(
        False, "--no-follow",
        help="Print the ring-buffer backfill then exit at the `ready` marker, "
             "instead of tailing live records.",
    ),
    lines: int = typer.Option(
        0, "-n", "--lines",
        help="Keep only the last N matching backfill records (0 = all). "
             "Applies to the backfill; live records always print in full.",
    ),
) -> None:
    """Tail the server log stream (admin-only).

    Replays the most recent records the server is holding (the backfill), then
    follows new records live until you press Ctrl-C. Use --no-follow for a
    one-shot dump of just the backfill.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.logs import logs_stream_path
    from app.cli.config import Config
    from app.cli.output import OutputMode

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    min_rank = _resolve_level(level)
    needle = (grep or "").strip().lower()

    def _emit(rec: dict[str, Any]) -> None:
        if mode.json:
            sys.stdout.write(_json.dumps(rec, ensure_ascii=False) + "\n")
        else:
            sys.stdout.write(_render(rec) + "\n")
        sys.stdout.flush()

    async def _run() -> None:
        backlog: list[dict[str, Any]] = []
        ready = False

        async with Client(cfg) as client:
            async for event in client.stream(logs_stream_path()):
                if event.type == "ready":
                    ready = True
                    kept = backlog[-lines:] if lines > 0 else backlog
                    for rec in kept:
                        _emit(rec)
                    backlog = []
                    if no_follow:
                        return
                    continue
                if event.type != "log":
                    continue
                rec = event.data.get("data")
                if not isinstance(rec, dict) or not _matches(rec, min_rank, needle):
                    continue
                if ready:
                    _emit(rec)
                else:
                    backlog.append(rec)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)
