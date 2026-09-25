"""`cremind userdocs ...` — User Document Search for the current profile.

Mirrors Settings → My Documents: turn indexing of your own files on or off,
choose the folder, manage exclude rules, and watch sync progress. `admin`
subcommands mirror the gate on the Vector Embedding page. `search`, `find`,
`read` and `cite` query the index the way the agent does (documented
separately, in `[cli]cremind userdocs search.md`), and `research` runs the
agent's deep-research jobs (in `[cli]cremind userdocs research.md`).

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
research_app = typer.Typer(
    name="research",
    help="Deep research over your documents: analyze a question with verified quotes, or compile a folder.",
    no_args_is_help=True,
)
userdocs_app.add_typer(excludes_app, name="excludes")
userdocs_app.add_typer(admin_app, name="admin")
userdocs_app.add_typer(deletions_app, name="deletions")
userdocs_app.add_typer(research_app, name="research")


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


# ── search / find / read / cite ────────────────────────────────────────────
#
# The same leaves the agent calls (`user_documents__search` / `__find_files` /
# `__read`), for a person at a terminal: the printed text is exactly what the
# agent reads (untrusted file content inside its data block), but not cut to
# the agent's tool-result budget.


def _filters(
    *,
    folder: Optional[list[str]] = None,
    types: Optional[list[str]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    date_field: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    out: dict[str, Any] = {}
    if folder:
        out["folder"] = list(folder)
    if types:
        out["types"] = list(types)
    if date_from:
        out["date_from"] = date_from
    if date_to:
        out["date_to"] = date_to
    if date_field:
        out["date_field"] = date_field
    return out or None


def _query(ctx: typer.Context, leaf: str, body: dict[str, Any]) -> None:
    """POST a query and print its text (or the whole result with --json)."""
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.client.userdocs import query
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await query(client, leaf, body)

    try:
        out = asyncio.run(_run())
    except APIError as e:
        detail = _api_detail(e)
        if detail and detail.get("message"):
            sys.stderr.write(f"{detail['message']}\n")
            for cand in detail.get("candidates") or []:
                sys.stderr.write(f"  {cand}\n")
            raise typer.Exit(code=1) from e
        raise
    if mode.json:
        print_json(out)
    else:
        sys.stdout.write((out.get("text") or "") + "\n")


@userdocs_app.command("search")
@graceful_errors
def userdocs_search(
    ctx: typer.Context,
    query: str = typer.Argument(..., help="What to look for, in your own words."),
    folder: Optional[list[str]] = typer.Option(None, "--folder", help="Only inside this folder (repeatable)."),
    types: Optional[list[str]] = typer.Option(
        None, "--type", help="document, pdf, word, spreadsheet, presentation, text, code, image, … (repeatable)."),
    date_from: Optional[str] = typer.Option(None, "--from", help="First day, YYYY-MM-DD (or YYYY-MM, YYYY)."),
    date_to: Optional[str] = typer.Option(None, "--to", help="Last day, inclusive."),
    date_field: Optional[str] = typer.Option(None, "--date-field", help="any (default) | modified | created | taken"),
    group_by: Optional[str] = typer.Option(None, "--group-by", help="file (default) | folder | chunk"),
    top_k: Optional[int] = typer.Option(None, "--top-k", help="Results per page (default 8)."),
    thorough: bool = typer.Option(False, "--thorough", help="Restore accents, translate, and rerank with a model."),
) -> None:
    """Search inside your indexed files; every passage comes with a \\[ud:…] citation token."""
    _query(ctx, "search", {
        "query": query,
        "filters": _filters(folder=folder, types=types, date_from=date_from, date_to=date_to,
                            date_field=date_field),
        "group_by": group_by, "top_k": top_k, "thorough": thorough or None,
    })


@userdocs_app.command("find")
@graceful_errors
def userdocs_find(
    ctx: typer.Context,
    query: Optional[str] = typer.Argument(None, help="What the file or folder is called or is about."),
    kind: Optional[str] = typer.Option(None, "--kind", help="file (default) | folder | project"),
    folder: Optional[list[str]] = typer.Option(None, "--folder", help="Only inside this folder (repeatable)."),
    types: Optional[list[str]] = typer.Option(None, "--type", help="File type (repeatable)."),
    date_from: Optional[str] = typer.Option(None, "--from", help="First day, YYYY-MM-DD (or YYYY-MM, YYYY)."),
    date_to: Optional[str] = typer.Option(None, "--to", help="Last day, inclusive."),
    sort: Optional[str] = typer.Option(
        None, "--sort", help="relevance | newest | oldest | name | largest | smallest"),
    limit: Optional[int] = typer.Option(None, "--limit", help="Results per page (default 20)."),
) -> None:
    """Find files, folders or projects by name, type, date or topic."""
    _query(ctx, "find", {
        "query": query, "kind": kind,
        "filters": _filters(folder=folder, types=types, date_from=date_from, date_to=date_to),
        "sort": sort, "limit": limit,
    })


@userdocs_app.command("read")
@graceful_errors
def userdocs_read(
    ctx: typer.Context,
    file: str = typer.Argument(..., help="A \\[ud:…] token, file id, path inside the indexed folder, or name."),
    pages: Optional[str] = typer.Option(None, "--pages", help="PDF pages, e.g. 3-5."),
    lines: Optional[str] = typer.Option(None, "--lines", help="Line range, e.g. 40-80."),
    section: Optional[str] = typer.Option(None, "--section", help="A heading or a legal reference ('Điều 203')."),
    sheet: Optional[str] = typer.Option(None, "--sheet", help="Spreadsheet sheet name."),
    slide: Optional[str] = typer.Option(None, "--slide", help="Slide number or range."),
) -> None:
    """Print an indexed file's text (or part of it), each passage with its citation token."""
    _query(ctx, "read", {
        "file": file, "pages": pages, "lines": lines, "section": section, "sheet": sheet, "slide": slide,
    })


@userdocs_app.command("cite")
@graceful_errors
def userdocs_cite(
    ctx: typer.Context,
    token: str = typer.Argument(..., help="A citation token such as \\[ud:k7m2xq9a#3f9c2e1b] (quote it in the shell)."),
) -> None:
    """Show where a \\[ud:…] citation points: the file, the location in it, and the cited text."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import resolve_citations
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.userdocs.cite import normalize_token, parse_tokens

    raw = token.strip()
    parsed = parse_tokens(raw if raw.startswith(("[", "【")) else f"[{raw}]")
    if not parsed:
        typer.echo("not a citation token — expected [ud:<8 chars>] or [ud:<8 chars>#<8 hex>]", err=True)
        raise typer.Exit(code=1)
    canonical = normalize_token(parsed[0]["cite_id"], parsed[0]["c8"])

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await resolve_citations(client, [canonical])

    out = asyncio.run(_run())
    item = (out.get("items") or {}).get(canonical)
    if mode.json:
        print_json(item if item is not None else out)
        return
    if not item:
        sys.stdout.write(f"{canonical}: not found in this profile's index\n")
        raise typer.Exit(code=1)
    f = item.get("file") or {}
    where = item.get("locator_label") or ""
    sys.stdout.write(f"{canonical} · {item.get('status', '?')}\n")
    sys.stdout.write(f"  file: {f.get('name') or ''} ({f.get('rel_path') or ''})\n")
    if where:
        sys.stdout.write(f"  where: {where}\n")
    if f.get("web_link"):
        sys.stdout.write(f"  link: {f['web_link']}\n")
    if item.get("snippet"):
        sys.stdout.write("  text: " + " ".join(str(item["snippet"]).split()) + "\n")


# ── image captions ─────────────────────────────────────────────────────────


@userdocs_app.command("caption")
@graceful_errors
def userdocs_caption(
    ctx: typer.Context,
    enable: Optional[bool] = typer.Option(
        None, "--enable/--disable", help="Caption photos and scanned pages with the vision model.",
    ),
    daily_cap: Optional[int] = typer.Option(
        None, "--daily-cap", help="Images captioned per day for this profile (default: the admin's).",
    ),
    min_px: Optional[int] = typer.Option(None, "--min-px", help="Skip images smaller than this (shorter side)."),
    min_kb: Optional[int] = typer.Option(None, "--min-kb", help="Skip image files smaller than this."),
    consent_vision: bool = typer.Option(
        False, "--consent-vision",
        help="Agree to send your images to the Specialized Vision Model shown by `settings`.",
    ),
    revoke_consent: bool = typer.Option(False, "--revoke-consent", help="Stop sending images to the vision model."),
) -> None:
    """Configure image captions and consent to the vision model (Settings → LLM Providers)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_settings
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()
    caption: dict[str, Any] = {}
    if enable is not None:
        caption["enabled"] = enable
    if daily_cap is not None:
        caption["daily_cap"] = daily_cap
    if min_px is not None:
        caption["min_px"] = min_px
    if min_kb is not None:
        caption["min_kb"] = min_kb
    if caption:
        _put_settings(ctx, {"kind": "local", "options": {"caption": caption}}, yes=False)
    if consent_vision and revoke_consent:
        typer.echo("--consent-vision and --revoke-consent are mutually exclusive", err=True)
        raise typer.Exit(code=1)
    if consent_vision or revoke_consent:
        async def _shown() -> dict[str, Any]:
            async with Client(cfg) as client:
                return await get_settings(client)

        vision = (asyncio.run(_shown()).get("vision") or {})
        model = f"{vision.get('provider')}/{vision.get('model')}" if vision.get("model") else None
        if consent_vision:
            if not model:
                typer.echo("No Specialized Vision Model is configured (Settings → LLM Providers).", err=True)
                raise typer.Exit(code=1)
            sys.stderr.write(f"Images and scanned pages will be sent to {model}.\n")
            _control(ctx, "consent_vision", model=model)
        else:
            _control(ctx, "revoke_vision_consent")

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_settings(client)

    out = asyncio.run(_run())
    _print(ctx, {
        "caption": ((out.get("local") or {}).get("options") or {}).get("caption"),
        "vision": out.get("vision"),
    })


def _split_list(values: Optional[list[str]]) -> Optional[list[str]]:
    """Repeated flags and comma-separated values both work: ``--name A --name
    "B, C"`` is ``[A, B, C]``. ``--name ""`` clears the list."""
    if values is None:
        return None
    out: list[str] = []
    for v in values:
        out += [p.strip() for p in str(v).split(",") if p.strip()]
    return out


def _local_option(ctx: typer.Context, key: str) -> Any:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.userdocs import get_settings
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_settings(client)

    return (((asyncio.run(_run()).get("local") or {}).get("options")) or {}).get(key)


@userdocs_app.command("identity")
@graceful_errors
def userdocs_identity(
    ctx: typer.Context,
    name: Optional[list[str]] = typer.Option(
        None, "--name", help="A name you write as (document author). Repeat or comma-separate; replaces the list.",
    ),
    email: Optional[list[str]] = typer.Option(
        None, "--email", help="An email address of yours. Repeat or comma-separate; replaces the list.",
    ),
    camera: Optional[list[str]] = typer.Option(
        None, "--camera", help="A camera or phone you shoot with (EXIF make/model). Replaces the list.",
    ),
) -> None:
    """Who "me" is, for "documents I wrote" and "photos I took"."""
    ctx.obj["cfg"].require_token()
    identity: dict[str, Any] = {}
    for key, values in (("author_names", name), ("emails", email), ("camera_devices", camera)):
        parsed = _split_list(values)
        if parsed is not None:
            identity[key] = parsed
    if identity:
        _put_settings(ctx, {"kind": "local", "options": {"identity": identity}}, yes=False)
    _print(ctx, {"identity": _local_option(ctx, "identity")})


@userdocs_app.command("allow-in")
@graceful_errors
def userdocs_allow_in(
    ctx: typer.Context,
    web_cli: Optional[bool] = typer.Option(
        None, "--web-cli/--no-web-cli", help="The web app, the CLI and your automations (default on).",
    ),
    channels: Optional[bool] = typer.Option(
        None, "--channels/--no-channels", help="Messaging channels such as Telegram or Zalo (default off).",
    ),
    rooms: Optional[bool] = typer.Option(
        None, "--rooms/--no-rooms", help="Group rooms, where other people read the answers (default off).",
    ),
) -> None:
    """Where the agent may use your documents."""
    ctx.obj["cfg"].require_token()
    allow = {k: v for k, v in (("web_cli", web_cli), ("channels", channels), ("rooms", rooms)) if v is not None}
    if allow:
        _put_settings(ctx, {"kind": "local", "options": {"allow_in": allow}}, yes=False)
    _print(ctx, {"allow_in": _local_option(ctx, "allow_in")})


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


# ── research ───────────────────────────────────────────────────────────────
#
# Deep-research jobs (the agent's `user_documents__research`) run on the
# server; these commands start, follow, answer and cancel them through
# /api/userdocs/research. The printed text is what the agent reads, whole.
# The statuses mirror app.userdocs.research.types, which is not imported
# here: the CLI stays free of server modules.

_RESEARCH_ACTIVE = frozenset({"queued", "planning", "running"})
_RESEARCH_WAITING = frozenset({"needs_clarification", "needs_confirmation", "interrupted"})
_RESEARCH_DONE = frozenset({"complete", "partial"})
# Seconds each --follow poll asks the server to hold the request open (the
# server caps it), and the least time between polls, so a server that
# answers at once is not hammered.
_FOLLOW_WAIT = 20.0
_FOLLOW_MIN_INTERVAL = 1.0


def research_exit_code(status: str) -> int:
    """0 finished (complete/partial) or still running; 2 waiting for you (a
    question, a confirmation, or a resume after a server restart); 1 failed,
    cancelled, or anything unrecognised."""
    if status in _RESEARCH_DONE or status in _RESEARCH_ACTIVE:
        return 0
    if status in _RESEARCH_WAITING:
        return 2
    return 1


def research_progress_line(job: dict[str, Any]) -> str:
    """One human line for a job's progress (`--follow` prints one per change)."""
    prog = job.get("progress") or {}
    parts = [f"{job.get('job_id') or '?'}: {job.get('status') or '?'}"]
    phase = prog.get("phase") or job.get("phase")
    if phase:
        parts.append(str(phase))
    if prog.get("total"):
        parts.append(f"{prog.get('done') or 0}/{prog['total']}")
    steps = prog.get("steps") or []
    if steps and isinstance(steps[-1], dict) and steps[-1].get("label"):
        parts.append(str(steps[-1]["label"]))
    used = int(job.get("tokens_in") or 0) + int(job.get("tokens_out") or 0)
    if used and job.get("budget"):
        parts.append(f"{used:,}/{int(job['budget']):,} tokens")
    return " · ".join(parts)


def _progress_key(job: dict[str, Any]) -> tuple[Any, ...]:
    # Token counts move on every poll; only a new phase, count or step is news.
    prog = job.get("progress") or {}
    steps = prog.get("steps") or []
    last = steps[-1] if steps and isinstance(steps[-1], dict) else {}
    return (job.get("status"), prog.get("phase") or job.get("phase"), prog.get("done"), prog.get("total"),
            last.get("id"), last.get("label"))


def parse_answers(values: Optional[list[str]]) -> dict[str, Any]:
    """``--answer KEY=VALUE`` flags → the answers object. ``true``/``false``
    become booleans (``confirm=true``); everything else stays a string, file
    ids included. Exits 1 on a flag without ``=``."""
    out: dict[str, Any] = {}
    for raw in values or []:
        key, sep, value = str(raw).partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            typer.echo(f"--answer takes KEY=VALUE (got {raw!r})", err=True)
            raise typer.Exit(code=1)
        low = value.lower()
        out[key] = True if low == "true" else False if low == "false" else value
    return out


def _research_scope(folders: Optional[list[str]], files: Optional[list[str]]) -> Optional[dict[str, Any]]:
    scope: dict[str, Any] = {}
    if folders:
        scope["folder"] = list(folders)
    if files:
        scope["file_ids"] = list(files)
    return scope or None


def _research_run(
    ctx: typer.Context,
    action: Any,
    *,
    timeout: float = 60.0,
    following: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Run ``action(client)``; research errors print their message (and, for
    a busy profile, how to follow or cancel the running job) and exit 1."""
    import asyncio

    from app.cli.client._base import APIError, Client
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg, timeout=timeout) as client:
            return await action(client)

    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        jid = (following or {}).get("job_id")
        again = f" — `cremind userdocs research status {jid} --follow` picks it up again" if jid else ""
        sys.stderr.write(f"Stopped following; the job keeps running on the server{again}.\n")
        raise typer.Exit(code=130)
    except APIError as e:
        detail = _api_detail(e)
        if detail and detail.get("message"):
            sys.stderr.write(f"{detail['message']}\n")
            if detail.get("error") == "ResearchBusy" and detail.get("job_id"):
                jid = detail["job_id"]
                sys.stderr.write(f"Follow it: cremind userdocs research status {jid} --follow\n"
                                 f"Cancel it: cremind userdocs research cancel {jid}\n")
            raise typer.Exit(code=1) from e
        raise


async def _research_follow(
    client: Any,
    out: dict[str, Any],
    *,
    page: Optional[int] = None,
    following: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Long-poll the job until it finishes or stops to ask, printing a
    progress line to stderr whenever it moves. Returns the last answer."""
    import asyncio
    import time as _time

    from app.cli.client.userdocs import research_get

    last: Optional[tuple[Any, ...]] = None
    while True:
        job = out.get("job") or {}
        if following is not None and job.get("job_id"):
            following["job_id"] = job["job_id"]
        key = _progress_key(job)
        if key != last:
            sys.stderr.write(research_progress_line(job) + "\n")
            sys.stderr.flush()
            last = key
        if (job.get("status") or "") not in _RESEARCH_ACTIVE or not job.get("job_id"):
            return out
        started = _time.monotonic()
        out = await research_get(client, job["job_id"], page=page, wait=_FOLLOW_WAIT)
        spare = _FOLLOW_MIN_INTERVAL - (_time.monotonic() - started)
        if spare > 0:
            await asyncio.sleep(spare)


def _research_hint(job: dict[str, Any], pages: Any) -> None:
    """What to run next, on stderr. The text itself is written for the agent
    (it says "call user_documents__research"); this is the terminal's copy."""
    jid = job.get("job_id") or "JOB"
    status = job.get("status") or ""
    if status in _RESEARCH_ACTIVE:
        sys.stderr.write(f"Still running. Follow it: cremind userdocs research status {jid} --follow\n")
    elif status == "interrupted":
        sys.stderr.write(f"The server restarted during this job. Resume it: "
                         f"cremind userdocs research continue {jid} --follow\n")
    elif status in _RESEARCH_WAITING:
        clar = (job.get("dossier") or {}).get("clarification") or {}
        keys = clar.get("answer_keys") or {}
        flags = " ".join(f"--answer {k}=..." for k in keys) or "--answer KEY=VALUE"
        sys.stderr.write(f"Answer it: cremind userdocs research continue {jid} {flags} --follow\n")
        for k, meaning in keys.items():
            sys.stderr.write(f"  {k}: {meaning}\n")
    elif status in _RESEARCH_DONE and isinstance(pages, int) and pages > 1:
        sys.stderr.write(f"The dossier has {pages} pages: cremind userdocs research status {jid} "
                         f"--page N, or --all-pages\n")


def _research_emit(ctx: typer.Context, out: dict[str, Any]) -> None:
    """Print the job's text (or, with --json, the whole answer) and exit with
    the job's status code."""
    from app.cli.output import OutputMode, print_json

    mode: OutputMode = ctx.obj["mode"]
    job = out.get("job") or {}
    texts = out.get("texts")
    if mode.json:
        print_json(out)
    else:
        if isinstance(texts, list):
            total = len(texts)
            for n, text in enumerate(texts, start=1):
                if n > 1:
                    sys.stdout.write(f"\n── page {n} of {total} ──\n")
                sys.stdout.write(str(text).rstrip("\n") + "\n")
        else:
            sys.stdout.write(str(out.get("text") or "").rstrip("\n") + "\n")
        _research_hint(job, None if isinstance(texts, list) else out.get("pages"))
    raise typer.Exit(code=research_exit_code(job.get("status") or ""))


@research_app.command("run")
@graceful_errors
def research_run_cmd(
    ctx: typer.Context,
    question: str = typer.Argument(..., help="What the research should answer, in your own words (any language)."),
    mode: Optional[str] = typer.Option(
        None, "--mode",
        help="analyze (default): answer a question from verified quotes | compile: read every file in scope "
             "and build one table.",
    ),
    domain: Optional[str] = typer.Option(
        None, "--domain",
        help="general (default) | legal (chooses the edition of each law, may ask) | financial.",
    ),
    folder: Optional[list[str]] = typer.Option(
        None, "--folder", help="Research these folders (the case, the reports; repeatable)."),
    file: Optional[list[str]] = typer.Option(
        None, "--file", help="Research these files: file ids or citation tokens, ud:… (repeatable)."),
    reference_folder: Optional[list[str]] = typer.Option(
        None, "--reference-folder", help="Where the law, policy or standard lives (analyze; repeatable)."),
    reference_file: Optional[list[str]] = typer.Option(
        None, "--reference-file", help="A reference file id or citation token (analyze; repeatable)."),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Wait for the result, printing progress; exits 0, 2 or 1 by outcome."),
    wait: Optional[int] = typer.Option(
        None, "--wait", help="Seconds the start request may wait for the job (the server caps it)."),
) -> None:
    """Start a research job: analyze a question with verified quotes, or compile a folder into a table."""
    body: dict[str, Any] = {
        "question": question,
        "mode": mode,
        "domain": domain,
        "scope": _research_scope(folder, file),
        "reference_scope": _research_scope(reference_folder, reference_file),
        "wait": wait or None,
    }
    following: dict[str, str] = {}

    async def action(client: Any) -> dict[str, Any]:
        from app.cli.client.userdocs import research_start

        out = await research_start(client, body)
        job = out.get("job") or {}
        following["job_id"] = str(job.get("job_id") or "")
        sys.stderr.write(f"Research job {job.get('job_id')} started "
                         f"({job.get('mode') or mode or 'analyze'}/{job.get('domain') or domain or 'general'}).\n")
        if follow:
            out = await _research_follow(client, out, following=following)
        return out

    out = _research_run(ctx, action, timeout=max(60.0, float(wait or 0) + 30.0), following=following)
    _research_emit(ctx, out)


@research_app.command("status")
@graceful_errors
def research_status_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="The job id (`research list` shows them)."),
    page: Optional[int] = typer.Option(None, "--page", help="Dossier page of a finished job (default 1: the summary)."),
    all_pages: bool = typer.Option(False, "--all-pages", help="Print every page of the dossier."),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Wait until the job finishes or asks something, printing progress."),
) -> None:
    """Show a research job: its progress, the question it asked, or its result."""
    if all_pages and page is not None:
        typer.echo("--page and --all-pages are exclusive", err=True)
        raise typer.Exit(code=1)
    following = {"job_id": job_id}

    async def action(client: Any) -> dict[str, Any]:
        from app.cli.client.userdocs import research_get

        out = await research_get(client, job_id, page=page)
        if follow:
            out = await _research_follow(client, out, page=page, following=following)
        if all_pages:
            texts = [out.get("text") or ""]
            for n in range(2, int(out.get("pages") or 1) + 1):
                texts.append((await research_get(client, job_id, page=n)).get("text") or "")
            out = {"job": out.get("job"), "pages": out.get("pages"), "texts": texts}
        return out

    _research_emit(ctx, _research_run(ctx, action, following=following))


@research_app.command("continue")
@graceful_errors
def research_continue_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="The job that asked (or was interrupted)."),
    answer: Optional[list[str]] = typer.Option(
        None, "--answer", "-a",
        help="KEY=VALUE answering the job's question (repeatable), e.g. edition=k7m2xq9a or confirm=true.",
    ),
    follow: bool = typer.Option(
        False, "--follow", "-f", help="Wait for the result, printing progress; exits 0, 2 or 1 by outcome."),
) -> None:
    """Answer what a research job asked, or resume one the server restart interrupted."""
    answers = parse_answers(answer)
    following = {"job_id": job_id}

    async def action(client: Any) -> dict[str, Any]:
        from app.cli.client.userdocs import research_continue

        out = await research_continue(client, job_id, answers=answers or None)
        if follow:
            out = await _research_follow(client, out, following=following)
        return out

    _research_emit(ctx, _research_run(ctx, action, following=following))


@research_app.command("cancel")
@graceful_errors
def research_cancel_cmd(
    ctx: typer.Context,
    job_id: str = typer.Argument(..., help="The job to stop."),
) -> None:
    """Stop a research job; what it found so far stays readable with `status`."""
    from app.cli.output import OutputMode, print_json

    async def action(client: Any) -> dict[str, Any]:
        from app.cli.client.userdocs import research_cancel

        return await research_cancel(client, job_id)

    out = _research_run(ctx, action)
    mode: OutputMode = ctx.obj["mode"]
    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(str(out.get("text") or "").rstrip("\n") + "\n")


@research_app.command("list")
@graceful_errors
def research_list_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(20, "--limit", help="How many recent jobs (at most 50)."),
) -> None:
    """This profile's recent research jobs, newest first."""
    import datetime as _dt

    from app.cli.output import OutputMode, print_json

    async def action(client: Any) -> dict[str, Any]:
        from app.cli.client.userdocs import research_list

        return await research_list(client, limit=limit)

    out = _research_run(ctx, action)
    mode: OutputMode = ctx.obj["mode"]
    if mode.json:
        print_json(out)
        return
    jobs = out.get("jobs") or []
    if not jobs:
        sys.stdout.write("(no research jobs)\n")
    for j in jobs:
        created = j.get("created_at")
        when = _dt.datetime.fromtimestamp(created / 1000).strftime("%Y-%m-%d %H:%M") if created else ""
        question = " ".join(str(j.get("question") or "").split())
        if len(question) > 70:
            question = question[:69] + "…"
        kind = f"{j.get('mode', '')}/{j.get('domain', '')}"
        sys.stdout.write(f"{j.get('job_id', ''):12}  {j.get('status', ''):19}  {kind:17}  {when:16}  {question}\n")
