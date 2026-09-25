"""`cremind userdocs ...` — User Document Search for the current profile.

Mirrors Settings → My Documents: turn indexing of your own files on or off,
choose the folder, manage exclude rules, and watch sync progress. `admin`
subcommands mirror the gate on the Vector Embedding page.

Changes that would remove indexed content (moving the folder, adding excludes
that drop files, deleting the index) are refused with a plan of what would go.
Re-run with `--yes` to apply — non-interactive callers (scripts, the agent's
own shell) must pass it explicitly; nothing is ever deleted by default.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors

userdocs_app = typer.Typer(
    name="userdocs",
    help="Search your own files: enable indexing, choose the folder, follow sync progress.",
    no_args_is_help=True,
)
excludes_app = typer.Typer(
    name="excludes",
    help="List, add or remove the folder's exclude rules.",
    no_args_is_help=True,
)
admin_app = typer.Typer(
    name="admin",
    help="The server-wide User Document Search gate (admin profile).",
    no_args_is_help=True,
)
deletions_app = typer.Typer(
    name="deletions",
    help="Confirm or reject a held mass deletion (many files vanished at once).",
    no_args_is_help=True,
)
userdocs_app.add_typer(excludes_app, name="excludes")
userdocs_app.add_typer(admin_app, name="admin")
userdocs_app.add_typer(deletions_app, name="deletions")


# ── helpers ────────────────────────────────────────────────────────────────


def _api_detail(e: Any) -> Optional[dict[str, Any]]:
    raw = getattr(e, "raw", b"") or b""
    try:
        detail = _json.loads(raw)
    except (ValueError, TypeError):
        return None
    return detail if isinstance(detail, dict) else None


def _explain_plan(plan: dict[str, Any]) -> None:
    labels = {
        "purge_all": "delete this folder's whole index",
        "purge_drive": "delete every indexed Google Drive file",
        "purge_out_of_scope": "remove files that are no longer in scope from the index",
        "reembed_all": "re-embed the whole index",
        "reextract_all": "re-read every file",
    }
    sys.stderr.write("This change would:\n")
    for eff in plan.get("effects") or []:
        what = labels.get(eff.get("kind"), eff.get("kind"))
        files = eff.get("files") or 0
        suffix = f" ({files} files)" if files else ""
        sys.stderr.write(f"  - {what}{suffix}\n")


def _handle_known_errors(e: Any) -> None:
    """Print a helpful message for the 409s this API returns, then exit.

    Returns normally (so the caller re-raises) for anything else.
    """
    detail = _api_detail(e)
    if not detail:
        return
    err = detail.get("error")
    if err == "FeatureNotInstalled":
        keys = [
            str(m.get("feature_key"))
            for m in (detail.get("missing") or [])
            if isinstance(m, dict) and m.get("feature_key")
        ]
        sys.stderr.write((detail.get("message") or "Required features are not installed.") + "\n")
        if keys:
            sys.stderr.write("Install them first: cremind features install " + " ".join(keys) + "\n")
        raise typer.Exit(code=1)
    if err == "ValidationFailed":
        for field, msg in (detail.get("details") or {}).items():
            sys.stderr.write(f"{field}: {msg}\n")
        raise typer.Exit(code=1)
    if err in ("FeatureDisabledByAdmin", "EmbeddingDisabled", "DriveNotLinked"):
        sys.stderr.write((detail.get("message") or err) + "\n")
        raise typer.Exit(code=1)


def _put_settings(ctx: typer.Context, body: dict[str, Any], yes: bool) -> dict[str, Any]:
    """PUT the settings, handling the confirm-before-destroy round trip."""
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.client.userdocs import put_settings
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run(b: dict[str, Any]) -> dict[str, Any]:
        async with Client(cfg) as client:
            return await put_settings(client, b)

    try:
        return asyncio.run(_run(body))
    except APIError as e:
        detail = _api_detail(e)
        if detail and detail.get("error") == "ConfirmationRequired":
            _explain_plan(detail.get("plan") or {})
            if not yes:
                sys.stderr.write("Nothing was changed. Re-run with --yes to apply.\n")
                raise typer.Exit(code=2) from e
            try:
                return asyncio.run(_run({**body, "confirm": detail.get("confirm")}))
            except APIError as e2:
                _handle_known_errors(e2)
                raise
        _handle_known_errors(e)
        raise


def _print(ctx: typer.Context, out: Any) -> None:
    from app.cli.output import OutputMode, print_json, print_map

    mode: OutputMode = ctx.obj["mode"]
    if mode.json:
        print_json(out)
    elif isinstance(out, dict):
        print_map(out)
    else:
        print_json(out)


def summarize_snapshot(snap: dict[str, Any]) -> str:
    """One human line for a progress snapshot (used by `status` and `-f`)."""
    state = snap.get("state") or "unknown"
    reason = snap.get("reason")
    head = f"{state}({reason})" if reason else state
    parts = [head]
    batch = snap.get("batch") or {}
    if batch.get("total"):
        parts.append(f"{batch.get('done', 0)}/{batch['total']} files")
        if batch.get("failed"):
            parts.append(f"{batch['failed']} failed")
        if batch.get("eta_s"):
            parts.append(f"~{int(batch['eta_s']) // 60} min left")
    current = snap.get("current") or []
    if current:
        c = current[0]
        parts.append(f"now: {c.get('rel_path') or c.get('name')} ({c.get('stage')})")
    tool = snap.get("tool_mode")
    if tool and tool != "normal":
        parts.append(f"search: {tool}")
    return " · ".join(parts)


# ── status / settings ──────────────────────────────────────────────────────


@userdocs_app.command("status")
@graceful_errors
def userdocs_status(
    ctx: typer.Context,
    follow: bool = typer.Option(
        False, "--follow", "-f",
        help="Follow sync progress live until interrupted (one line per update).",
    ),
) -> None:
    """Show what User Document Search is doing for this profile."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_status, userdocs_stream_path
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_jsonl

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    if follow:
        async def _tail() -> None:
            async with Client(cfg) as client:
                async for event in client.stream(userdocs_stream_path()):
                    if event.event != "userdocs":
                        continue
                    if mode.json:
                        print_jsonl(event.data)
                    else:
                        sys.stdout.write(summarize_snapshot(event.data) + "\n")
                        sys.stdout.flush()

        try:
            asyncio.run(_tail())
        except KeyboardInterrupt:
            raise typer.Exit(code=130)
        return

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_status(client)

    out = asyncio.run(_run())
    if mode.json:
        _print(ctx, out)
    else:
        sys.stdout.write(summarize_snapshot(out) + "\n")


@userdocs_app.command("settings")
@graceful_errors
def userdocs_settings(ctx: typer.Context) -> None:
    """Print this profile's folder, Drive and option settings."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_settings
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_settings(client)

    out = asyncio.run(_run())
    sys.stdout.write(_json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n")


# ── enable / disable / root ────────────────────────────────────────────────


@userdocs_app.command("enable")
@graceful_errors
def userdocs_enable(
    ctx: typer.Context,
    root: Optional[str] = typer.Option(
        None, "--root",
        help="Folder to index. Omit to use the working directory.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without asking if it removes content."),
) -> None:
    """Turn on User Document Search for this profile."""
    body: dict[str, Any] = {"kind": "local", "enabled": True}
    if root:
        body.update(root_mode="custom", root_path=root)
    out = _put_settings(ctx, body, yes)
    _print(ctx, (out.get("settings") or {}).get("local") or out)


@userdocs_app.command("disable")
@graceful_errors
def userdocs_disable(
    ctx: typer.Context,
    delete_index: bool = typer.Option(
        False, "--delete-index",
        help="Also delete the index. Without it the index is kept for a fast re-enable.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm deleting the index."),
) -> None:
    """Turn off User Document Search for this profile (the index is kept unless --delete-index)."""
    body: dict[str, Any] = {"kind": "local", "enabled": False}
    if delete_index:
        body["delete_index"] = True
    out = _put_settings(ctx, body, yes)
    _print(ctx, (out.get("settings") or {}).get("local") or out)


@userdocs_app.command("set-root")
@graceful_errors
def userdocs_set_root(
    ctx: typer.Context,
    path: Optional[str] = typer.Argument(None, help="Folder to index."),
    inherit: bool = typer.Option(False, "--inherit", help="Use the working directory instead."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply even if files leave the index."),
) -> None:
    """Change the indexed folder."""
    if bool(path) == inherit:
        typer.echo("give a PATH or --inherit (exactly one)", err=True)
        raise typer.Exit(code=1)
    body: dict[str, Any] = {"kind": "local"}
    if inherit:
        body["root_mode"] = "inherit"
    else:
        body.update(root_mode="custom", root_path=path)
    out = _put_settings(ctx, body, yes)
    _print(ctx, (out.get("settings") or {}).get("local") or out)


# ── sync control ───────────────────────────────────────────────────────────


def _control(ctx: typer.Context, action: str, *, yes: bool = False, **params: Any) -> dict[str, Any]:
    """POST /control, with the same confirm-before-destroy handling as settings."""
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.client.userdocs import control
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run(p: dict[str, Any]) -> dict[str, Any]:
        async with Client(cfg) as client:
            return await control(client, action, **p)

    try:
        return asyncio.run(_run(params))
    except APIError as e:
        detail = _api_detail(e)
        if detail and detail.get("error") == "ConfirmationRequired":
            _explain_plan(detail.get("plan") or {})
            if not yes:
                sys.stderr.write("Nothing was changed. Re-run with --yes to apply.\n")
                raise typer.Exit(code=2) from e
            return asyncio.run(_run({**params, "confirm": detail.get("confirm")}))
        if detail and detail.get("message"):
            sys.stderr.write(f"{detail['message']}\n")
            raise typer.Exit(code=1) from e
        raise


def _print_summary(ctx: typer.Context, out: dict[str, Any]) -> None:
    from app.cli.output import OutputMode, print_json

    mode: OutputMode = ctx.obj["mode"]
    if mode.json:
        print_json(out)
        return
    snap = out.get("snapshot") if isinstance(out.get("snapshot"), dict) else out
    sys.stdout.write(summarize_snapshot(snap) + "\n")


@userdocs_app.command("start")
@graceful_errors
def userdocs_start(ctx: typer.Context) -> None:
    """Start the first sync after reviewing its estimate (large folders wait for this)."""
    _print_summary(ctx, _control(ctx, "start"))


@userdocs_app.command("pause")
@graceful_errors
def userdocs_pause(ctx: typer.Context) -> None:
    """Pause syncing (the index stays searchable)."""
    _print_summary(ctx, _control(ctx, "pause"))


@userdocs_app.command("resume")
@graceful_errors
def userdocs_resume(ctx: typer.Context) -> None:
    """Resume syncing after a pause; changes made meanwhile are picked up."""
    _print_summary(ctx, _control(ctx, "resume"))


@userdocs_app.command("rescan")
@graceful_errors
def userdocs_rescan(ctx: typer.Context) -> None:
    """Walk the whole folder now and sync anything that changed."""
    _print_summary(ctx, _control(ctx, "rescan"))


@userdocs_app.command("reindex")
@graceful_errors
def userdocs_reindex(
    ctx: typer.Context,
    targets: list[str] = typer.Argument(..., help="Files or folders (path relative to the indexed folder, or a file id)."),
) -> None:
    """Re-read and re-index specific files or folders now, ahead of everything else."""
    _print_summary(ctx, _control(ctx, "reindex", targets=targets))


@userdocs_app.command("retry")
@graceful_errors
def userdocs_retry(
    ctx: typer.Context,
    file_id: Optional[list[str]] = typer.Option(
        None, "--file-id", help="Retry only this file (repeatable). Default: every failed file.",
    ),
) -> None:
    """Retry files that failed to index."""
    params: dict[str, Any] = {}
    if file_id:
        params["targets"] = list(file_id)
    _print_summary(ctx, _control(ctx, "retry_failed", **params))


@userdocs_app.command("rebuild")
@graceful_errors
def userdocs_rebuild(
    ctx: typer.Context,
    reextract: bool = typer.Option(
        False, "--reextract",
        help="Also re-read every file (default: re-embed from the stored text only).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Confirm the rebuild."),
) -> None:
    """Rebuild the index from scratch — needed rarely (after a corrupted vector store)."""
    _print_summary(ctx, _control(ctx, "rebuild", yes=yes, reextract=reextract))


@deletions_app.command("confirm")
@graceful_errors
def deletions_confirm(ctx: typer.Context) -> None:
    """Remove the files that vanished from the index (they are hidden until you decide)."""
    _print_summary(ctx, _control(ctx, "confirm_deletions"))


@deletions_app.command("reject")
@graceful_errors
def deletions_reject(ctx: typer.Context) -> None:
    """Keep them: they stay hidden and are re-checked on every scan for 14 days."""
    _print_summary(ctx, _control(ctx, "reject_deletions"))


# ── inspection ─────────────────────────────────────────────────────────────


@userdocs_app.command("files")
@graceful_errors
def userdocs_files(
    ctx: typer.Context,
    status: Optional[str] = typer.Option(
        None, "--status",
        help="indexed | dirty | metadata_only | awaiting_extractor | deferred | error | missing",
    ),
    kind: Optional[str] = typer.Option(None, "--kind", help="pdf, docx, image, executable, … "),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Match in the file name or path."),
    limit: int = typer.Option(50, "--limit", help="Rows per page."),
    all_pages: bool = typer.Option(False, "--all", help="Keep paging until the end."),
) -> None:
    """List indexed files with their status (and why a file failed)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import list_files
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        after: Optional[tuple[str, int]] = None
        async with Client(cfg) as client:
            while True:
                page = await list_files(
                    client, status=status, kind=kind, q=query, limit=limit,
                    after_path=after[0] if after else None,
                    after_id=after[1] if after else None,
                )
                items = page.get("files") or []
                rows.extend(items)
                nxt = page.get("next")
                if not all_pages or not nxt or not items:
                    return rows
                after = (nxt.get("rel_path") or "", int(nxt.get("id")))

    rows = asyncio.run(_run())
    if mode.json:
        print_json(rows)
        return
    if not rows:
        sys.stdout.write("(no files)\n")
    for r in rows:
        reason = f" ({r['status_reason']})" if r.get("status_reason") else ""
        sys.stdout.write(f"{r.get('fid', ''):9} {r.get('status', ''):18} {r.get('kind', ''):11} {r.get('rel_path', '')}{reason}\n")


@userdocs_app.command("activity")
@graceful_errors
def userdocs_activity(
    ctx: typer.Context,
    limit: int = typer.Option(30, "--limit", help="How many recent events."),
) -> None:
    """Recent sync activity: files added, updated (with chunk counts), removed, failed."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import activity
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await activity(client, limit=limit)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    import datetime as _dt

    for ev in out.get("events") or []:
        ts = _dt.datetime.fromtimestamp((ev.get("ts") or 0) / 1000).strftime("%Y-%m-%d %H:%M:%S")
        sys.stdout.write(f"{ts} {ev.get('kind', ''):10} {ev.get('message', '')}\n")


@userdocs_app.command("estimate")
@graceful_errors
def userdocs_estimate(
    ctx: typer.Context,
    wait: bool = typer.Option(False, "--wait", help="Start a fresh estimate and wait for it to finish."),
) -> None:
    """What a full sync would do: files by type, images to caption, storage, time."""
    import asyncio
    import time as _time

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_estimate, start_estimate
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            if wait:
                await start_estimate(client)
                deadline = _time.monotonic() + 3600
                while _time.monotonic() < deadline:
                    out = await get_estimate(client)
                    if (out.get("state") or "") != "running":
                        return out
                    await asyncio.sleep(1.0)
            return await get_estimate(client)

    _print(ctx, asyncio.run(_run()))


@userdocs_app.command("storage")
@graceful_errors
def userdocs_storage(ctx: typer.Context) -> None:
    """Index size, budget, free disk space, and whether syncing is paused for space."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_storage
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_storage(client)

    _print(ctx, asyncio.run(_run()))


# ── excludes ───────────────────────────────────────────────────────────────


def _current_excludes(ctx: typer.Context) -> list[dict[str, Any]]:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_settings
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_settings(client)

    return list(((asyncio.run(_run()).get("local") or {}).get("excludes")) or [])


@excludes_app.command("list")
@graceful_errors
def excludes_list(ctx: typer.Context) -> None:
    """List the folder's exclude rules."""
    from app.cli.output import OutputMode, print_json

    rules = _current_excludes(ctx)
    mode: OutputMode = ctx.obj["mode"]
    if mode.json:
        print_json(rules)
        return
    if not rules:
        sys.stdout.write("(no exclude rules)\n")
    for r in rules:
        sys.stdout.write(f"{r['type']:5} {r['mode']:13} {r['pattern']}\n")


@excludes_app.command("add")
@graceful_errors
def excludes_add(
    ctx: typer.Context,
    pattern: str = typer.Argument(..., help="Glob (e.g. 'Archive/**'), folder name, or extension."),
    rule_type: str = typer.Option("glob", "--type", help="glob | dir | ext"),
    metadata_only: bool = typer.Option(
        False, "--metadata-only",
        help="Index name/date/size only instead of skipping the files entirely.",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply even if indexed files are removed."),
) -> None:
    """Add an exclude rule."""
    rules = _current_excludes(ctx)
    rules.append({"pattern": pattern, "type": rule_type, "mode": "metadata_only" if metadata_only else "skip"})
    out = _put_settings(ctx, {"kind": "local", "excludes": rules}, yes)
    _print(ctx, {"excludes": ((out.get("settings") or {}).get("local") or {}).get("excludes")})


@excludes_app.command("remove")
@graceful_errors
def excludes_remove(
    ctx: typer.Context,
    pattern: str = typer.Argument(..., help="The rule's pattern, as `excludes list` shows it."),
) -> None:
    """Remove an exclude rule (files it hid are indexed on the next scan)."""
    rules = _current_excludes(ctx)
    kept = [r for r in rules if r.get("pattern") != pattern]
    if len(kept) == len(rules):
        typer.echo(f"no exclude rule with pattern {pattern!r}", err=True)
        raise typer.Exit(code=1)
    out = _put_settings(ctx, {"kind": "local", "excludes": kept}, yes=False)
    _print(ctx, {"excludes": ((out.get("settings") or {}).get("local") or {}).get("excludes")})


# ── admin ──────────────────────────────────────────────────────────────────


@admin_app.command("get")
@graceful_errors
def admin_get(ctx: typer.Context) -> None:
    """Show the server-wide gate, budgets and which profiles use the feature (admin)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_admin
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_admin(client)

    out = asyncio.run(_run())
    sys.stdout.write(_json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n")


@admin_app.command("set")
@graceful_errors
def admin_set(
    ctx: typer.Context,
    allow: Optional[bool] = typer.Option(
        None, "--allow/--disallow",
        help="Allow profiles to use User Document Search (needs Vector Embedding on).",
    ),
    budget_mb: Optional[int] = typer.Option(None, "--budget-mb", help="Storage budget for all indexes together."),
    profile_budget_mb: Optional[int] = typer.Option(
        None, "--profile-budget-mb", help="Per-profile storage budget (0 = none).",
    ),
    vision_cap: Optional[int] = typer.Option(
        None, "--vision-cap", help="Default photos/scans captioned per profile per day.",
    ),
    max_file_mb: Optional[int] = typer.Option(
        None, "--max-file-mb", help="Files larger than this are indexed by metadata only.",
    ),
    workers: Optional[int] = typer.Option(None, "--workers", help="Extraction subprocesses (1-8)."),
    vector_capacity_mb: Optional[int] = typer.Option(
        None, "--vector-capacity-mb", help="Size of an external/K8s vector store volume (0 = measure).",
    ),
    db_capacity_mb: Optional[int] = typer.Option(
        None, "--db-capacity-mb", help="Size of an external/K8s database volume (0 = measure).",
    ),
) -> None:
    """Change the server-wide gate or budgets (admin)."""
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.client.userdocs import put_admin
    from app.cli.config import Config

    policy = {
        k: v for k, v in {
            "allowed": allow,
            "storage_budget_mb": budget_mb,
            "per_profile_budget_mb": profile_budget_mb,
            "vision_daily_cap_default": vision_cap,
            "max_file_mb": max_file_mb,
            "workers": workers,
            "vector_capacity_mb": vector_capacity_mb,
            "db_capacity_mb": db_capacity_mb,
        }.items() if v is not None
    }
    if not policy:
        typer.echo("nothing to change — pass at least one option (see --help)", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await put_admin(client, policy)

    try:
        out = asyncio.run(_run())
    except APIError as e:
        _handle_known_errors(e)
        raise
    _print(ctx, out.get("policy") or out)
