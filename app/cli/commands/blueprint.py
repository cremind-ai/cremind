"""`cremind blueprint ...` — package a profile's design and import it elsewhere.

A ``.cremind-blueprint`` captures the *design* of one profile — persona, tool
enable/config, LLM provider/model choice, changed settings, skills, events, and
skill listeners — with **no secrets**, so it can be shared and imported into a
new profile in another install.

Online subcommands go through the server's REST API (``--server`` /
``CREMIND_TOKEN``). ``inspect`` reads a local archive's manifest offline. The
``import`` command runs the whole wizard non-interactively; supply secrets with
``--set`` and use ``--skip-all`` to apply the design without them (missing
secrets surface as runtime errors later, exactly like the UI's skip). Import
discipline: every ``app.*`` import lives inside a function body (see
``app/cli/main.py``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors

blueprint_app = typer.Typer(
    name="blueprint",
    help="Export a profile's design as a blueprint and import it into a new profile.",
    no_args_is_help=True,
)

# The canonical wizard step order (import applies in this sequence).
_STEP_ORDER = ("profile", "settings", "persona", "llm", "tools", "skills", "events", "listeners")


# ── export ─────────────────────────────────────────────────────────────────


@blueprint_app.command("exportable")
@graceful_errors
def bp_exportable(ctx: typer.Context) -> None:
    """Show which components of your profile can be exported."""
    import asyncio

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await api.get_exportable(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    comps = (out or {}).get("components") or {}
    typer.echo(f"Exportable components for profile '{(out or {}).get('profile')}':")
    for key in _STEP_ORDER:
        c = comps.get(key)
        if not c or not c.get("available"):
            continue
        detail = _exportable_detail(key, c)
        typer.echo(f"  [x] {key:<10} {detail}")


def _exportable_detail(key: str, c: dict) -> str:
    if key == "settings":
        return f"{c.get('count', 0)} changed setting(s)"
    if key == "skills":
        items = c.get("items") or []
        return ", ".join(i.get("name", "?") for i in items) or "-"
    if key == "events":
        counts = c.get("counts") or {}
        return f"schedule={counts.get('schedule', 0)} watchers={counts.get('file_watcher', 0)} skill={counts.get('skill_event', 0)}"
    if key == "llm":
        s = c.get("summary") or {}
        return f"provider={s.get('default_provider') or '-'}"
    if key == "tools":
        items = c.get("items") or []
        return ", ".join(i.get("name", "?") for i in items) or "-"
    if key == "listeners":
        return f"{len(c.get('items') or [])} listener(s)"
    return ""


@blueprint_app.command("export")
@graceful_errors
def bp_export(
    ctx: typer.Context,
    components: Optional[str] = typer.Option(
        None, "--components", help="Comma-separated component keys to include (e.g. persona,llm,skills)."
    ),
    all_: bool = typer.Option(False, "--all", help="Include every available component."),
    skills: Optional[str] = typer.Option(None, "--skills", help="Comma-separated skill slugs to bundle (default: all)."),
    tools: Optional[str] = typer.Option(None, "--tools", help="Comma-separated tool ids to include (default: all)."),
    name: Optional[str] = typer.Option(None, "--name", help="Blueprint name (filename slug)."),
    display_name: Optional[str] = typer.Option(None, "--display-name", help="Human-readable name."),
    description: Optional[str] = typer.Option(None, "--description", help="One-line description."),
    out: Optional[Path] = typer.Option(None, "-o", "--out", help="Download the archive to this local path."),
) -> None:
    """Export the current profile's design into a blueprint archive."""
    import asyncio

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            if all_ or not components:
                ex = await api.get_exportable(client)
                comp_list = [k for k, v in (ex.get("components") or {}).items() if v.get("available")]
            else:
                comp_list = [c.strip() for c in components.split(",") if c.strip()]
            body = {
                "components": comp_list,
                "name": name or display_name or "",
                "display_name": display_name or "",
                "description": description or "",
            }
            if skills:
                body["skills"] = [s.strip() for s in skills.split(",") if s.strip()]
            if tools:
                body["tools"] = [t.strip() for t in tools.split(",") if t.strip()]
            result = await api.export_blueprint(client, body)
            file_name = (result.get("file") or {}).get("name")
            saved = None
            if out and file_name:
                dest = Path(out)
                with open(dest, "wb") as sink:
                    await api.download(client, file_name, sink)
                saved = dest
            return result, saved

    result, saved = asyncio.run(_run())
    if mode.json:
        print_json(result)
        return
    f = result.get("file") or {}
    typer.echo(f"Exported {f.get('name')} ({f.get('bytes', 0):,} bytes)")
    for w in result.get("warnings") or []:
        typer.echo(f"  warning: {w}", err=True)
    if saved is not None:
        typer.echo(f"Saved {saved}")
    else:
        typer.echo("Download it with:  cremind blueprint download " + str(f.get("name")))


@blueprint_app.command("list")
@graceful_errors
def bp_list(ctx: typer.Context) -> None:
    """List blueprints stored on the server."""
    import asyncio

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await api.list_blueprints(client)

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    rows = (out or {}).get("blueprints") or []
    if not rows:
        typer.echo("No blueprints found.")
        return
    table = Table(mode, "NAME", "PROFILE", "APP", "SIZE", "CREATED")
    for r in rows:
        man = r.get("manifest") or {}
        table.add_row(
            r.get("name"),
            man.get("source_profile") or "-",
            man.get("app_version") or "-",
            f"{r.get('size_bytes', 0):,}",
            man.get("created_at") or "-",
        )
    table.render()


@blueprint_app.command("download")
@graceful_errors
def bp_download(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Blueprint archive name (see `cremind blueprint list`)."),
    to: Optional[Path] = typer.Option(None, "--to", help="Destination file (default: ./<name>)."),
) -> None:
    """Download a blueprint archive to a local file."""
    import asyncio

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()
    dest = Path(to) if to else Path(os.getcwd()) / name

    async def _run():
        async with Client(cfg) as client, open(dest, "wb") as sink:
            await api.download(client, name, sink)

    asyncio.run(_run())
    typer.echo(f"Saved {dest}")


@blueprint_app.command("delete")
@graceful_errors
def bp_delete(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Blueprint archive name to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete a blueprint archive from the server."""
    import asyncio

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()
    if not yes and not typer.confirm(f"Delete blueprint {name}?", default=False):
        typer.echo("Aborted.")
        raise typer.Exit(code=1)

    async def _run():
        async with Client(cfg) as client:
            await api.delete(client, name)

    asyncio.run(_run())
    typer.echo(f"Deleted {name}")


@blueprint_app.command("inspect")
@graceful_errors
def bp_inspect(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Path to a local .cremind-blueprint archive."),
) -> None:
    """Read a local blueprint's manifest (offline — never extracts skills)."""
    from app.blueprint.engine import read_blueprint_manifest
    from app.cli.output import OutputMode, print_json

    mode: OutputMode = ctx.obj["mode"]
    manifest = read_blueprint_manifest(Path(path))
    summary = manifest.summary()
    if mode.json:
        print_json(summary)
        return
    typer.echo(f"Blueprint: {summary.get('display_name')} ({summary.get('name')})")
    typer.echo(f"  author: {summary.get('author') or '-'}")
    typer.echo(f"  source profile: {summary.get('source_profile')}  app: {summary.get('app_version')}")
    typer.echo(f"  created: {summary.get('created_at')}")
    typer.echo(f"  description: {summary.get('description') or '-'}")
    typer.echo("  components: " + ", ".join(sorted((summary.get('components') or {}).keys())))
    reqs = summary.get("requirements") or {}
    secrets = reqs.get("secrets") or []
    if secrets:
        typer.echo("  secrets you'll need to provide on import:")
        for s in secrets:
            who = s.get("provider") or s.get("tool_id") or s.get("skill")
            typer.echo(f"    - {who}: {s.get('field') or s.get('variable')}")


# ── import (non-interactive wizard) ────────────────────────────────────────────


def _parse_set(pairs: list[str]) -> dict:
    """Parse ``--set`` values into per-step input buckets.

    Grammar:
      llm:<provider>.<field>=v   OR   <provider>.<field>=v   (default → LLM)
      tool:<tool_id>.<VAR>=v
      skill:<slug>.<VAR>=v
      watcher:<name>=v
    """
    llm: dict = {}
    tools: dict = {}
    skills: dict = {}
    watchers: dict = {}
    for raw in pairs:
        if "=" not in raw:
            raise typer.BadParameter(f"--set expects KEY=VALUE, got {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if key.startswith("tool:"):
            rest = key[len("tool:"):]
            tid, _, var = rest.partition(".")
            tools.setdefault(tid, {})[var] = value
        elif key.startswith("skill:"):
            rest = key[len("skill:"):]
            slug, _, var = rest.partition(".")
            skills.setdefault(slug, {})[var] = value
        elif key.startswith("watcher:"):
            watchers[key[len("watcher:"):]] = value
        else:
            if key.startswith("llm:"):
                key = key[len("llm:"):]
            llm[key] = value
    return {"llm": llm, "tools": tools, "skills": skills, "watchers": watchers}


def _step_inputs(step_key: str, sets: dict, *, start_listeners: bool, plan_step: dict) -> dict:
    if step_key == "llm":
        return {"secrets": sets["llm"]}
    if step_key == "tools":
        return {"secrets": sets["tools"]}
    if step_key == "skills":
        return {"secrets": sets["skills"], "conflicts": {}}
    if step_key == "events":
        return {"watcher_paths": sets["watchers"]}
    if step_key == "listeners":
        confirmed = [r.get("skill_dir") for r in plan_step.get("requirements", [])] if start_listeners else []
        return {"confirmed": confirmed}
    return {}


@blueprint_app.command("import")
@graceful_errors
def bp_import(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="Path to a .cremind-blueprint archive."),
    profile: Optional[str] = typer.Option(
        None, "--profile",
        help="Assert the import targets this profile (defaults to your token's profile). "
             "Create a fresh profile first if you don't want to change an existing one.",
    ),
    set_: list[str] = typer.Option(
        [], "--set", help="Provide a secret/path, e.g. --set openai.api_key=sk-... or --set skill:jira.JIRA_SITE=..."
    ),
    skip_all: bool = typer.Option(False, "--skip-all", help="Apply the design without missing secrets (they error at runtime)."),
    start_listeners: bool = typer.Option(False, "--start-listeners", help="Start skill listeners now (else on next restart)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Upload + validate + print requirements, then stop."),
    replace: bool = typer.Option(False, "--replace", help="Abort any in-progress import first."),
) -> None:
    """Import a blueprint into the current profile, non-interactively.

    The blueprint applies to your token's profile — create and switch to a fresh
    profile first if you don't want to change an existing one.
    """
    import asyncio

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()
    sets = _parse_set(set_)

    async def _run():
        async with Client(cfg) as client:
            session = await api.upload(client, str(path), replace=replace)
            return await _drive_wizard(
                client, session, sets,
                profile=profile, skip_all=skip_all,
                start_listeners=start_listeners, dry_run=dry_run,
            )

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    if out.get("dry_run"):
        typer.echo(f"Importing into profile: {out.get('target')}")
        _print_plan(out["session"])
        return
    _print_report(out.get("report") or {})


async def _drive_wizard(
    client,
    session: dict,
    sets: dict,
    *,
    profile: Optional[str],
    skip_all: bool,
    start_listeners: bool,
    dry_run: bool,
) -> dict:
    """Drive an already-staged import session to completion (shared by import/install)."""
    import typer as _typer

    from app.cli.client import blueprint as api

    target = session.get("target_profile") or session.get("owner")
    if profile and profile != target:
        raise _typer.BadParameter(
            f"Import targets your token's profile ({target!r}), not {profile!r}. "
            f"Use a token for {profile!r} (create/select that profile first)."
        )
    plan = session.get("plan") or []
    if dry_run:
        return {"dry_run": True, "session": session, "target": target}

    for step in plan:
        key = step.get("key")
        # Guard: required secrets not supplied and not skipping → stop.
        missing = _missing_secrets(key, step, sets)
        if missing and not skip_all:
            raise _typer.BadParameter(
                f"Step {key!r} needs: {', '.join(missing)}. Provide via --set or pass --skip-all."
            )
        inputs = _step_inputs(key, sets, start_listeners=start_listeners, plan_step=step)
        await api.apply_step(client, key, inputs)
    report_resp = await api.finalize(client)
    return {"dry_run": False, "report": report_resp.get("report"), "target": target}


@blueprint_app.command("install")
@graceful_errors
def bp_install(
    ctx: typer.Context,
    link: str = typer.Argument(
        ..., help="Cremind Hub link (https://hub.cremind.io/blueprints/<name>) or a blueprint name."
    ),
    profile: Optional[str] = typer.Option(
        None, "--profile",
        help="Assert the import targets this profile (defaults to your token's profile).",
    ),
    set_: list[str] = typer.Option(
        [], "--set", help="Provide a secret/path, e.g. --set openai.api_key=sk-... or --set skill:jira.JIRA_SITE=..."
    ),
    skip_all: bool = typer.Option(False, "--skip-all", help="Apply the design without missing secrets."),
    start_listeners: bool = typer.Option(False, "--start-listeners", help="Start skill listeners now."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Download + validate + print requirements, then stop."),
    replace: bool = typer.Option(False, "--replace", help="Abort any in-progress import first."),
) -> None:
    """Download a blueprint from the Cremind Hub and import it into the current profile.

    Downloads + stages the blueprint server-side, then runs the same non-interactive
    wizard as ``import`` — so all of the ``import`` flags apply.
    """
    import asyncio

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()
    sets = _parse_set(set_)

    async def _run():
        async with Client(cfg) as client:
            session = await api.import_hub(client, link, replace=replace)
            return await _drive_wizard(
                client, session, sets,
                profile=profile, skip_all=skip_all,
                start_listeners=start_listeners, dry_run=dry_run,
            )

    out = asyncio.run(_run())
    if mode.json:
        print_json(out)
        return
    if out.get("dry_run"):
        typer.echo(f"Importing into profile: {out.get('target')}")
        _print_plan(out["session"])
        return
    _print_report(out.get("report") or {})


# ── upload (to the Cremind Hub, device-code flow) ──────────────────────────────


@blueprint_app.command("upload")
@graceful_errors
def bp_upload(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Blueprint archive name on the server (see `cremind blueprint list`)."),
    display_name: Optional[str] = typer.Option(None, "--display-name", help="Human-readable name for the Hub listing."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Don't open the approval URL in a browser."),
) -> None:
    """Upload a blueprint to the Cremind Hub marketplace (stored as a draft).

    Downloads the archive from your local server, then runs a browser device-code
    approval against the Hub (log in / approve there), and uploads on your behalf. The
    blueprint is stored as a DRAFT — publish it from its page on the Hub. No Hub
    credentials are stored locally. Set ``CREMIND_HUB_URL`` to target a non-default hub.
    """
    import asyncio
    import io

    from app.cli.client import blueprint as api
    from app.cli.client._base import Client
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()
    base = name[: -len(".cremind-blueprint")] if name.endswith(".cremind-blueprint") else name

    async def _run():
        # 1. Fetch the archive bytes from the LOCAL server.
        async with Client(cfg) as client:
            sink = io.BytesIO()
            await api.download(client, name, sink)
        data = sink.getvalue()
        if not data:
            raise RuntimeError(f"Blueprint '{name}' is empty or not found on the server.")

        # 2. Start the Hub device-code flow.
        start = await api.publish_device_start(base, display_name or base)
        if not mode.json:
            typer.echo("To upload, approve this request on the Cremind Hub:")
            print_kv([
                ("verification_uri", start.verification_uri),
                ("user_code", start.user_code),
            ])
        if not no_browser and start.verification_uri_complete:
            import webbrowser

            webbrowser.open(start.verification_uri_complete)

        # 3. Poll until approved (honor interval; deny/expiry are terminal).
        interval = start.interval or 5
        token = ""
        while True:
            poll = await api.publish_device_poll(start.device_code)
            if poll.status == "complete":
                token = poll.publish_token
                break
            if poll.status == "expired":
                raise RuntimeError("Approval expired; run `cremind blueprint upload` again.")
            if poll.status == "denied":
                raise RuntimeError("Upload request was denied.")
            await asyncio.sleep(interval)

        # 4. Upload with the Bearer upload token.
        result = await api.upload_to_hub(token, name, data)
        return result

    try:
        result = asyncio.run(_run())
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    url = result.get("url") or ""
    full = f"{api.hub_base()}{url}" if url else ""
    if mode.json:
        print_json({"ok": True, "url": url, "hub_url": full})
        return
    typer.echo(f"Uploaded to Cremind Hub: {full or '(see the Hub)'} — publish it there when ready.")
    if full and not no_browser:
        import webbrowser

        webbrowser.open(full)


def _missing_secrets(key: str, step: dict, sets: dict) -> list[str]:
    missing: list[str] = []
    for req in step.get("requirements") or []:
        t = req.get("type")
        if t == "llm_provider":
            for field in req.get("fields") or []:
                if f"{req.get('provider')}.{field}" not in sets["llm"]:
                    missing.append(f"{req.get('provider')}.{field}")
        elif t == "tool_secrets":
            tid = req.get("tool_id")
            for var in req.get("variables") or []:
                if var not in (sets["tools"].get(tid) or {}):
                    missing.append(f"tool:{tid}.{var}")
        elif t == "skill":
            slug = req.get("slug")
            for var in req.get("secret_variables") or []:
                if var not in (sets["skills"].get(slug) or {}):
                    missing.append(f"skill:{slug}.{var}")
    return missing


def _print_plan(session: dict) -> None:
    typer.echo("Blueprint validated. Steps:")
    for step in session.get("plan") or []:
        reqs = step.get("requirements") or []
        typer.echo(f"  - {step.get('key')} ({len(reqs)} requirement(s))")
    for w in session.get("warnings") or []:
        typer.echo(f"  compat: {w.get('message')}", err=True)


def _print_report(report: dict) -> None:
    typer.echo(f"Imported into profile '{report.get('profile')}'.")
    applied = report.get("applied") or []
    typer.echo(f"  applied: {len(applied)} item(s)")
    for n in report.get("needs_attention") or []:
        typer.echo(f"  needs attention: {n}", err=True)
