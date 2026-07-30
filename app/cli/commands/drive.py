"""`cremind drive ...` — grant Cremind access to individual Google Drive files.

Cremind holds per-file Drive access (the ``drive.file`` scope), so it reaches only
files the user picked through Google's file picker plus files it created itself.
These commands drive that picker and show what is currently reachable — the
terminal counterpart of the **Settings -> Google Drive** page.

Linking the Google account itself belongs to the ``gdrive`` skill (it owns the
OAuth token); this only grants files on an already-linked account.
"""

from __future__ import annotations

import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


drive_app = typer.Typer(
    name="drive",
    help="Grant and inspect per-file Google Drive access.",
    no_args_is_help=True,
)

_POLL_INTERVAL_S = 2.5


def _print_status(out: dict[str, Any]) -> None:
    if not out.get("linked"):
        sys.stdout.write(
            "Google Drive is not linked.\n"
            "Ask the agent to link the gdrive skill, then run 'cremind drive grant'.\n"
        )
        return
    sys.stdout.write(f"Linked as {out.get('email') or 'unknown account'}\n")
    sys.stdout.write("Access: per-file (granted files + files Cremind created)\n")
    if out.get("scopes_stale"):
        sys.stdout.write(f"\nAction needed: {out.get('hint', '')}\n")


@drive_app.command("status")
@graceful_errors
def drive_status(ctx: typer.Context) -> None:
    """Show the Drive link status for this profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.drive import get_status
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_status(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
    else:
        _print_status(out)


@drive_app.command("files")
@graceful_errors
def drive_files(
    ctx: typer.Context,
    page_token: Optional[str] = typer.Option(None, "--page-token", help="Continue a previous page."),
    page_size: int = typer.Option(50, "--page-size", help="Files per page (max 100)."),
) -> None:
    """List the Drive files Cremind can reach."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.drive import list_files
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_files(client, page_token=page_token, page_size=page_size)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    files = out.get("files") or []
    if not files:
        sys.stdout.write(
            "No Drive files are reachable yet. Run 'cremind drive grant' to pick some.\n"
        )
        return
    table = Table(["NAME", "TYPE", "MODIFIED", "ID"])
    for f in files:
        table.add_row([
            f.get("name", ""),
            (f.get("mime_type", "") or "").rsplit(".", 1)[-1],
            f.get("modified_time", ""),
            f.get("id", ""),
        ])
    table.render()
    if out.get("next_page_token"):
        sys.stdout.write(f"\nMore: --page-token {out['next_page_token']}\n")


@drive_app.command("grant")
@graceful_errors
def drive_grant(
    ctx: typer.Context,
    file: Optional[list[str]] = typer.Option(
        None, "--file", help="Pre-select a Drive file id or URL (repeatable)."
    ),
    single: bool = typer.Option(False, "--single", help="Allow only one file to be picked."),
    no_folders: bool = typer.Option(False, "--no-folders", help="Hide folders from the picker."),
    mime_type: Optional[list[str]] = typer.Option(
        None, "--mime-type", help="Restrict the picker to a mimeType (repeatable)."
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open a browser."),
    print_only: bool = typer.Option(
        False, "--print-only", help="Print the picker URL and exit without waiting."
    ),
    timeout: float = typer.Option(600.0, "--timeout", help="Seconds to wait for the picker."),
) -> None:
    """Open Google's file picker so the user can grant Cremind access to files."""
    import asyncio
    import time
    import webbrowser

    from app.cli.client._base import Client
    from app.cli.client.drive import complete_grant, grant_status, start_grant
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _start(client) -> dict[str, Any]:
        return await start_grant(
            client,
            file_ids=list(file or []) or None,
            allow_multiple=not single,
            allow_folders=not no_folders,
            mime_types=list(mime_type or []) or None,
        )

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            started = await _start(client)
            url = started.get("authorize_url") or ""
            state = started.get("state") or ""
            if not url:
                return started
            if print_only:
                return {"authorize_url": url, "state": state, "waiting": False}

            if not mode.json:
                sys.stdout.write(f"Open this URL to choose the files to share:\n{url}\n\n")
                hint = started.get("capture_hint")
                if hint:
                    sys.stdout.write(f"{hint}\n\n")
            if not no_browser:
                try:
                    webbrowser.open(url)
                except Exception:  # noqa: BLE001 - a headless box has no browser
                    pass

            # The grant lands with Google when the user approves, so polling
            # observes the result even on installs where the redirect can't come
            # back to this server.
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(_POLL_INTERVAL_S)
                out = await grant_status(client, state)
                if out.get("status") == "completed" and out.get("files"):
                    return out
                if out.get("status") == "error":
                    return out
            return {"status": "timeout", "state": state, "files": []}

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    if out.get("error") and not out.get("files"):
        sys.stdout.write(f"{out.get('message') or out['error']}\n")
        raise typer.Exit(code=1)
    if out.get("waiting") is False:
        sys.stdout.write(f"{out.get('authorize_url', '')}\n")
        return
    files = out.get("files") or []
    if files:
        sys.stdout.write(f"Granted access to {len(files)} file(s):\n")
        for f in files:
            sys.stdout.write(f"  {f.get('name', '')}  ({f.get('id', '')})\n")
        return

    # Nothing observed: offer the paste path rather than leaving the user stuck.
    sys.stdout.write(
        "\nNo newly granted files were detected. If you completed the picker and your "
        "browser showed an error page, paste the URL it landed on below.\n"
    )
    if not sys.stdin.isatty():
        sys.stdout.write(
            "Run: cremind drive grant-complete '<redirect-url>'\n"
        )
        raise typer.Exit(code=1)
    pasted = typer.prompt("Redirect URL (blank to skip)", default="", show_default=False)
    if not pasted.strip():
        raise typer.Exit(code=1)

    async def _finish() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await complete_grant(client, pasted.strip())

    done = asyncio.run(_finish())
    granted = done.get("files") or []
    if not granted:
        sys.stdout.write(f"{done.get('message') or 'No files were granted.'}\n")
        raise typer.Exit(code=1)
    sys.stdout.write(f"Granted access to {len(granted)} file(s).\n")


@drive_app.command("grant-complete")
@graceful_errors
def drive_grant_complete(
    ctx: typer.Context,
    redirect_url: str = typer.Argument(..., help="The full URL your browser was redirected to."),
) -> None:
    """Finish a grant from the redirect URL, when the picker couldn't be captured."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.drive import complete_grant
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await complete_grant(client, redirect_url)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    files = out.get("files") or []
    if not files:
        sys.stdout.write(f"{out.get('message') or 'No files were granted.'}\n")
        raise typer.Exit(code=1)
    sys.stdout.write(f"Granted access to {len(files)} file(s):\n")
    for f in files:
        sys.stdout.write(f"  {f.get('name', '')}  ({f.get('id', '')})\n")
