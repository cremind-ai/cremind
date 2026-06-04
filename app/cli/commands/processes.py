"""`cremind proc ...` — list and control long-running processes.

Mirrors `cli/cmd/proc.go`. Phase 2 covers list/get/stop/stdin/resize plus
the autostart subgroup. Phase 3 adds `proc stream` (SSE), and Phase 4 adds
`proc attach` (WebSocket + raw TTY).
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


proc_app = typer.Typer(
    name="proc",
    help="List and control long-running processes.",
    no_args_is_help=True,
)
autostart_app = typer.Typer(
    name="autostart",
    help="Manage autostart-process registrations.",
    no_args_is_help=True,
)
proc_app.add_typer(autostart_app, name="autostart")


@proc_app.command("list")
@graceful_errors
def proc_list(ctx: typer.Context) -> None:
    """List running processes for the active profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import list_processes
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_processes(client)

    procs = asyncio.run(_run())

    if mode.json:
        print_json(procs)
        return

    table = Table(mode, "PID", "STATUS", "COMMAND", "WORKING_DIR")
    for p in procs:
        table.add_row(
            string_field(p, "id"),
            string_field(p, "status"),
            string_field(p, "command"),
            string_field(p, "working_dir"),
        )
    table.render()


@proc_app.command("get")
@graceful_errors
def proc_get(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Process id."),
) -> None:
    """Show details for a single process."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import get_process
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_process(client, pid)

    p = asyncio.run(_run())

    if mode.json:
        print_json(p)
    else:
        sys.stdout.write(_json.dumps(p, indent=2, ensure_ascii=False, default=str) + "\n")


@proc_app.command("stop")
@graceful_errors
def proc_stop(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Process id."),
) -> None:
    """Terminate a process."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import stop_process
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await stop_process(client, pid)

    asyncio.run(_run())


@proc_app.command("stdin")
@graceful_errors
def proc_stdin(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Process id."),
    text: Optional[str] = typer.Option(
        None, "--text", help="Literal text to send.",
    ),
    keys: Optional[list[str]] = typer.Option(
        None, "--keys",
        help="Named keys to send (Enter, Tab, Up, Down, Esc, ...). Repeat or comma-separate.",
    ),
    line_ending: Optional[str] = typer.Option(
        None, "--line-ending",
        help="Line ending to append: none | lf | crlf.",
    ),
) -> None:
    """Send input to a running process.

    With --keys, sends named keys. With --text, sends a literal string.
    Without either, reads from stdin and sends verbatim.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import send_process_stdin
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    body: dict[str, Any] = {}
    if line_ending:
        body["line_ending"] = line_ending

    # Flatten any comma-separated keys (typer's --keys repeats produce a list,
    # but users from cobra's --keys=Up,Enter are used to commas).
    flat_keys: list[str] = []
    if keys:
        for entry in keys:
            for part in entry.split(","):
                part = part.strip()
                if part:
                    flat_keys.append(part)

    if flat_keys:
        body["keys"] = flat_keys
    elif text is not None and text != "":
        body["input_text"] = text
    else:
        body["input_text"] = sys.stdin.read()

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await send_process_stdin(client, pid, body)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)


@proc_app.command("stream")
@graceful_errors
def proc_stream(ctx: typer.Context) -> None:
    """Stream live process snapshots (SSE) until interrupted."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import processes_stream_path
    from app.cli.config import Config
    from app.cli.output import OutputMode
    from app.cli.streaming import run_admin_stream

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await run_admin_stream(client, processes_stream_path(), mode)

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


@proc_app.command("attach")
@graceful_errors
def proc_attach(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Process id."),
    no_resize: bool = typer.Option(
        False, "--no-resize",
        help="Don't forward terminal resize events to the remote PTY.",
    ),
    detach_key: str = typer.Option(
        "ctrl-\\", "--detach-key",
        help="Single-byte detach key. Use 'ctrl-X' for control codes.",
    ),
) -> None:
    """Attach to a process's PTY interactively (raw mode).

    Pumps stdin/stdout through a WebSocket. Ctrl-C is forwarded to the remote
    process; the configured detach key (default Ctrl-\\) exits the session
    without killing the remote process. Terminal resize is forwarded
    automatically — pass --no-resize to opt out.
    """
    import asyncio

    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    detach_byte = _parse_detach_key(detach_key)

    try:
        asyncio.run(_run_attach(cfg, pid, not no_resize, detach_byte))
    except KeyboardInterrupt:
        raise typer.Exit(code=130)


def _parse_detach_key(spec: str) -> int:
    """Translate a user-friendly detach-key spec into a single byte.

    Accepts: 'ctrl-\\', 'ctrl-q', 'q', or a literal single character.
    """
    s = spec.strip().lower()
    if s.startswith("ctrl-") and len(s) == 6:
        ch = s[5]
        # Ctrl-A..Z = 0x01..0x1A; Ctrl-\ = 0x1C, Ctrl-] = 0x1D, etc.
        if "a" <= ch <= "z":
            return ord(ch) - ord("a") + 1
        if ch == "\\":
            return 0x1C
        if ch == "]":
            return 0x1D
        if ch == "_":
            return 0x1F
        if ch == " ":
            return 0x00
    if len(spec) == 1:
        return ord(spec)
    if spec == "":
        return 0
    raise typer.BadParameter(f"unrecognized --detach-key: {spec!r}")


async def _run_attach(
    cfg,
    pid: str,
    resize_enabled: bool,
    detach_byte: int,
) -> None:
    """Open the PTY WebSocket and pump stdin/stdout/resize until the user
    detaches, the server closes, or Ctrl-C is pressed.
    """
    import contextlib
    import json
    import os

    import websockets

    from app.cli.client._ws import connect_ws
    from app.cli.client.processes import process_websocket_path
    from app.cli.io.raw_tty import (
        get_terminal_size,
        is_stdin_tty,
        is_stdout_tty,
        raw_terminal,
    )

    class _DetachSignal(Exception):
        pass

    async def _send_resize(ws, cols: int, rows: int) -> None:
        await ws.send(json.dumps({"type": "resize", "cols": cols, "rows": rows}))

    async def _pump_server_to_stdout(ws) -> None:
        async for message in ws:
            if isinstance(message, bytes):
                try:
                    text = message.decode("utf-8", errors="replace")
                except Exception:
                    continue
            else:
                text = message
            try:
                env = json.loads(text)
            except json.JSONDecodeError:
                # Non-JSON — write raw so the user still sees output.
                sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
                sys.stdout.buffer.flush()
                continue
            kind = env.get("type")
            if kind == "snapshot":
                chunks = env.get("chunks") or []
                for c in chunks:
                    if isinstance(c, dict):
                        data = str(c.get("data") or "")
                        if data:
                            sys.stdout.buffer.write(
                                data.encode("utf-8", errors="replace")
                            )
            elif kind in ("stdout", "stderr"):
                data = env.get("data")
                if isinstance(data, str) and data:
                    sys.stdout.buffer.write(data.encode("utf-8", errors="replace"))
            elif kind == "overflow":
                raise RuntimeError(
                    "server closed connection: output buffer overflowed"
                )
            sys.stdout.buffer.flush()

    async def _pump_stdin_to_server(ws) -> None:
        loop = asyncio.get_running_loop()
        stdin_fd = sys.stdin.fileno()
        while True:
            chunk: bytes = await loop.run_in_executor(
                None, lambda: os.read(stdin_fd, 4096)
            )
            if not chunk:
                return  # stdin EOF
            if detach_byte and detach_byte in chunk:
                raise _DetachSignal()
            payload = json.dumps({
                "type": "stdin",
                "data": chunk.decode("utf-8", errors="replace"),
                "line_ending": "none",
            })
            await ws.send(payload)

    async def _pump_resize(ws) -> None:
        prev = get_terminal_size()
        while True:
            await asyncio.sleep(0.5)
            curr = get_terminal_size()
            if curr != prev:
                prev = curr
                await _send_resize(ws, curr[0], curr[1])

    raw_cm = raw_terminal() if is_stdin_tty() else contextlib.nullcontext()
    forward_resize = resize_enabled and is_stdout_tty()

    async with connect_ws(cfg.server, process_websocket_path(pid), cfg.token) as ws:
        with raw_cm:
            if forward_resize:
                cols, rows = get_terminal_size()
                with contextlib.suppress(Exception):
                    await _send_resize(ws, cols, rows)

            tasks = [
                asyncio.create_task(_pump_server_to_stdout(ws), name="proc.attach.stdout"),
                asyncio.create_task(_pump_stdin_to_server(ws), name="proc.attach.stdin"),
            ]
            if forward_resize:
                tasks.append(
                    asyncio.create_task(_pump_resize(ws), name="proc.attach.resize")
                )

            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            for t in pending:
                with contextlib.suppress(asyncio.CancelledError, BaseException):
                    await t

            for t in done:
                exc = t.exception()
                if exc is None:
                    continue
                if isinstance(exc, _DetachSignal):
                    return  # user-initiated clean detach
                if isinstance(exc, websockets.exceptions.ConnectionClosed):
                    return  # remote process exited
                raise exc


@proc_app.command("resize")
@graceful_errors
def proc_resize(
    ctx: typer.Context,
    pid: str = typer.Argument(..., help="Process id."),
    cols: int = typer.Option(0, "--cols", help="Terminal columns."),
    rows: int = typer.Option(0, "--rows", help="Terminal rows."),
) -> None:
    """Resize a process's PTY (cols x rows)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import resize_process_pty
    from app.cli.config import Config

    if cols <= 0 or rows <= 0:
        typer.echo("--cols and --rows are required and must be positive", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await resize_process_pty(client, pid, cols, rows)

    asyncio.run(_run())


# ── autostart ─────────────────────────────────────────────────────────────

@autostart_app.command("list")
@graceful_errors
def autostart_list(ctx: typer.Context) -> None:
    """List autostart registrations."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import list_autostart_processes
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_autostart_processes(client)

    rows = asyncio.run(_run())

    if mode.json:
        print_json(rows)
        return

    table = Table(mode, "ID", "COMMAND", "WORKING_DIR", "PTY", "ERROR")
    for r in rows:
        table.add_row(
            string_field(r, "id"),
            string_field(r, "command"),
            string_field(r, "working_dir"),
            bool_field(r, "is_pty", False),
            string_field(r, "error"),
        )
    table.render()


@autostart_app.command("add")
@graceful_errors
def autostart_add(
    ctx: typer.Context,
    pid: Optional[str] = typer.Option(None, "--pid", help="Process id to register."),
    force: bool = typer.Option(False, "--force", help="Bypass duplicate-command check."),
) -> None:
    """Register a live process as autostart so it relaunches at boot."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import create_autostart_from_process
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    if not pid or not pid.strip():
        typer.echo("--pid is required", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await create_autostart_from_process(client, pid, force)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(f"{string_field(out, 'id')}\n")


@autostart_app.command("delete")
@graceful_errors
def autostart_delete(
    ctx: typer.Context,
    autostart_id: str = typer.Argument(..., help="Autostart id."),
) -> None:
    """Remove an autostart registration."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import delete_autostart_process
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_autostart_process(client, autostart_id)

    asyncio.run(_run())


@autostart_app.command("run")
@graceful_errors
def autostart_run(
    ctx: typer.Context,
    autostart_id: str = typer.Argument(..., help="Autostart id."),
) -> None:
    """Spawn the command from an autostart registration immediately."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.processes import run_autostart_process
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await run_autostart_process(client, autostart_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(f"{string_field(out, 'process_id')}\n")
