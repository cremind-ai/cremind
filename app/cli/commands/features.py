"""`cremind features ...` — list and install optional feature extras.

Cremind keeps heavier dependencies (vector-embedding models, vector-store
backends, some LLM SDKs) out of the slim `pip install cremind` as optional
"features". This group lists their install state and installs one, streaming
the live pip output over SSE — the same flow the Setup Wizard and Settings page
use. Features whose `requires_restart_after_install` is set only activate after
`cremind server restart`.

"Installed" only means importable. A feature can be importable and still too
old — a runtime venv is never resynced, so a Cremind upgrade that raises a
feature's version range leaves the old package in place (Codex's SDK is the
case that forced this: an old one cannot read the account's live model list).
`list` flags such a feature under UPDATE, and `install` updates it; the process
still has the old package loaded, so an update always needs a restart.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any

import typer

from app.cli.commands._helpers import graceful_errors


features_app = typer.Typer(
    name="features",
    help="List and install optional feature extras.",
    no_args_is_help=True,
)


@features_app.command("list")
@graceful_errors
def features_list(ctx: typer.Context) -> None:
    """List every optional feature and its install state.

    UPDATE is `required` when an installed feature's packages are outside the
    version range this Cremind needs, and `restart` once it has been updated
    but the server still runs the old code. A server that predates version
    checks sends neither field, which reads as `-`.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.features import get_features
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_features(client)

    feats = asyncio.run(_run())

    if mode.json:
        print_json(feats)
        return

    table = Table(mode, "FEATURE", "INSTALLED", "UPDATE", "RESTART_AFTER", "EXTRAS")
    notes: list[str] = []
    for fid in sorted(feats):
        info = feats[fid] if isinstance(feats[fid], dict) else {}
        extras = info.get("extras")
        extras_str = ", ".join(str(e) for e in extras) if isinstance(extras, list) else ""
        table.add_row(
            fid,
            "true" if info.get("installed") else "false",
            _update_cell(info),
            "true" if info.get("requires_restart_after_install") else "false",
            extras_str,
        )
        # Once the update has run, the only step left is the restart the cell
        # already names; repeating "run install" would send the user round the
        # loop again.
        if info.get("outdated") is True and info.get("restart_pending") is not True:
            notes.append(_outdated_note(fid, info))
    table.render()
    for note in notes:
        sys.stderr.write(note + "\n")


def _update_cell(info: dict[str, Any]) -> str:
    """Render one feature's UPDATE cell: `restart`, `required` or `-`.

    `restart` wins over `required`: the feature is still outdated as far as the
    running process is concerned (the old package is loaded), but installing it
    again would change nothing — only the restart does.
    """
    if info.get("restart_pending") is True:
        return "restart"
    if info.get("outdated") is True:
        return "required"
    return "-"


def _outdated_note(fid: str, info: dict[str, Any]) -> str:
    """The stderr line naming what is installed, what is needed, and the fix."""
    versions = info.get("installed_versions")
    have = (
        ", ".join(f"{dist} {ver or 'unknown'}" for dist, ver in versions.items())
        if isinstance(versions, dict) and versions else "an older version"
    )
    required = info.get("required")
    needs = (
        ", ".join(str(r) for r in required)
        if isinstance(required, list) and required else "a newer version"
    )
    return (
        f"({fid}: {have} installed, needs {needs} - run "
        f"`cremind features install {fid}`, then `cremind server restart`)"
    )


@features_app.command("install")
@graceful_errors
def features_install(
    ctx: typer.Context,
    names: list[str] = typer.Argument(
        ..., help="Feature id(s) to install (e.g. embedding.me5). See `features list`.",
    ),
) -> None:
    """Install one or more features, streaming the pip output live.

    Also updates a feature that is installed but outdated (UPDATE `required`
    in `features list`); an update only takes effect after
    `cremind server restart`, as does a feature marked
    requires_restart_after_install. Exits non-zero if any feature fails.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.features import features_install_path
    from app.cli.config import Config
    from app.cli.output import OutputMode

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    body = {"features": list(names)}
    failed = False

    async def _run() -> bool:
        nonlocal failed
        async with Client(cfg) as client:
            async for event in client.stream_post(features_install_path(), body):
                kind = event.event
                data = event.data if isinstance(event.data, dict) else {}

                if mode.json:
                    sys.stdout.write(
                        _json.dumps({"event": kind, "data": data}, ensure_ascii=False) + "\n"
                    )
                    sys.stdout.flush()
                    if kind in ("done", "error"):
                        if kind == "error" or data.get("failed") or data.get("ok") is False:
                            failed = True
                        return failed
                    continue

                if kind == "done":
                    _render_done(data)
                    if data.get("failed") or data.get("ok") is False or data.get("error"):
                        failed = True
                    return failed
                if kind == "error":
                    # An `error` event carries its text under `message`.
                    sys.stderr.write(
                        str(data.get("message") or data.get("error") or "install failed") + "\n"
                    )
                    failed = True
                    return failed
                # `log` / `start` / any progress frame carrying a message.
                message = str(data.get("message") or "")
                if message:
                    sys.stdout.write(message + "\n")
                    sys.stdout.flush()
        return failed

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    if failed:
        raise typer.Exit(code=1)


def _render_done(data: dict[str, Any]) -> None:
    """Print the human summary of a completed install (`done` frame)."""
    def _names(key: str) -> str:
        val = data.get(key)
        return ", ".join(str(x) for x in val) if isinstance(val, list) else ""

    installed = _names("installed")
    # Features that were installed but outdated and are now updated (never also
    # under `installed`). An older server's frame has no `upgraded` at all.
    upgraded = _names("upgraded")
    already = _names("already_present")
    failed = _names("failed")
    if installed:
        sys.stdout.write("installed: " + installed + "\n")
    if upgraded:
        sys.stdout.write("updated: " + upgraded + "\n")
    if already:
        sys.stdout.write("already present: " + already + "\n")
    if failed:
        sys.stderr.write("failed: " + failed + "\n")
    if data.get("error"):
        sys.stderr.write(str(data.get("error")) + "\n")
    if data.get("restart_required"):
        sys.stdout.write("restart required — run `cremind server restart`\n")
