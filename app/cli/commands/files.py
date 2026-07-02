"""`cremind files ...` — browse and manage files in the Cremind workspace.

Talks to the file-serving API (`/api/files/*`), which is sandboxed to the
Cremind system dir + the user working dir (plus a conversation's custom cwd
when `--conversation` is passed). Paths are absolute server-side paths — start
from `cremind files cwd`.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from typing import Any, List, Optional

import typer

from app.cli.commands._helpers import graceful_errors


files_app = typer.Typer(
    name="files",
    help="Browse and manage files in the Cremind workspace.",
    no_args_is_help=True,
)


def _fmt_mtime(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return ""


@files_app.command("cwd")
@graceful_errors
def files_cwd(ctx: typer.Context) -> None:
    """Print the workspace working directory (the file-tree seed path)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import get_cwd
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await get_cwd(client)

    cwd = asyncio.run(_run())
    if mode.json:
        print_json({"cwd": cwd})
    else:
        sys.stdout.write(f"{cwd}\n")


@files_app.command("set-cwd")
@graceful_errors
def files_set_cwd(
    ctx: typer.Context,
    conversation_id: str = typer.Argument(..., help="Conversation whose cwd override to set."),
    path: str = typer.Argument(..., help="Absolute directory to switch the conversation into."),
) -> None:
    """Set a conversation's working-directory override."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import set_cwd
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> str:
        async with Client(cfg) as client:
            return await set_cwd(client, conversation_id, path)

    working_directory = asyncio.run(_run())
    if mode.json:
        print_json({"working_directory": working_directory})
    else:
        sys.stdout.write(f"{working_directory}\n")


@files_app.command("list")
@graceful_errors
def files_list(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Absolute directory to list."),
    show_hidden: bool = typer.Option(False, "--show-hidden", help="Include dotfiles / hidden entries."),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation", help="Widen the allowlist to this conversation's custom cwd."
    ),
) -> None:
    """List the entries in a directory."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import list_directory
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_directory(client, path, show_hidden=show_hidden, conversation_id=conversation_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    entries = out.get("entries") or []
    table = Table(mode, "NAME", "DIR", "SIZE", "MODIFIED")
    for e in entries:
        if not isinstance(e, dict):
            continue
        size = e.get("size")
        table.add_row(
            string_field(e, "name"),
            bool_field(e, "is_dir", False),
            "" if size is None else str(size),
            _fmt_mtime(e.get("modified")),
        )
    table.render()
    if out.get("truncated"):
        sys.stderr.write("(listing truncated)\n")


@files_app.command("download")
@graceful_errors
def files_download(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Absolute path of the file to download."),
    out: Optional[str] = typer.Option(None, "--out", help="Write to this local file (default: stdout)."),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation", help="Widen the allowlist to this conversation's custom cwd."
    ),
) -> None:
    """Download a file (to --out or stdout)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import download
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            if out:
                with open(out, "wb") as fh:
                    await download(client, path, fh, conversation_id=conversation_id)
            else:
                await download(client, path, sys.stdout.buffer, conversation_id=conversation_id)
                sys.stdout.buffer.flush()

    asyncio.run(_run())
    if out:
        sys.stderr.write(f"saved {out}\n")


@files_app.command("upload")
@graceful_errors
def files_upload(
    ctx: typer.Context,
    directory: str = typer.Argument(..., help="Absolute destination directory on the server."),
    local_files: List[str] = typer.Argument(..., help="Local file(s) to upload."),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation", help="Widen the allowlist to this conversation's custom cwd."
    ),
) -> None:
    """Upload one or more local files into a server directory."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import upload
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    parts: list[tuple[str, bytes]] = []
    for lf in local_files:
        if not os.path.isfile(lf):
            typer.echo(f"not a file: {lf}", err=True)
            raise typer.Exit(code=1)
        with open(lf, "rb") as fh:
            parts.append((os.path.basename(lf), fh.read()))

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await upload(client, directory, parts, conversation_id=conversation_id)

    results = asyncio.run(_run())

    if mode.json:
        print_json(results)
        return
    table = Table(mode, "NAME", "SAVED_AS", "STATUS", "ERROR")
    for r in results:
        table.add_row(
            string_field(r, "name"),
            string_field(r, "saved_as"),
            string_field(r, "status"),
            string_field(r, "error"),
        )
    table.render()


@files_app.command("mkdir")
@graceful_errors
def files_mkdir(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Absolute path of the directory to create."),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation", help="Widen the allowlist to this conversation's custom cwd."
    ),
) -> None:
    """Create a new directory."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import mkdir
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await mkdir(client, path, conversation_id=conversation_id)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(f"{out.get('path') or path}\n")


@files_app.command("move")
@graceful_errors
def files_move(
    ctx: typer.Context,
    src: str = typer.Argument(..., help="Absolute source path."),
    dest: str = typer.Argument(..., help="Absolute destination path (including the new basename)."),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation", help="Widen the allowlist to this conversation's custom cwd."
    ),
) -> None:
    """Move or rename a file or directory."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import move
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await move(client, src, dest, conversation_id=conversation_id)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(f"{out.get('dest') or dest}\n")


@files_app.command("delete")
@graceful_errors
def files_delete(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Absolute path to delete (files or directories)."),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation", help="Widen the allowlist to this conversation's custom cwd."
    ),
) -> None:
    """Delete a file or (recursively) a directory."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import delete
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete(client, path, conversation_id=conversation_id)

    asyncio.run(_run())


@files_app.command("watch")
@graceful_errors
def files_watch(
    ctx: typer.Context,
    path: str = typer.Argument(..., help="Absolute directory to watch (recursively)."),
    conversation_id: Optional[str] = typer.Option(
        None, "--conversation", help="Widen the allowlist to this conversation's custom cwd."
    ),
) -> None:
    """Stream filesystem-change events for a directory (SSE)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.files import file_watch_path
    from app.cli.config import Config
    from app.cli.output import OutputMode
    from app.cli.streaming import run_admin_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_admin_stream(client, file_watch_path(path, conversation_id=conversation_id), mode)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)
