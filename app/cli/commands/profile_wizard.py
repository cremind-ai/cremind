"""`cremind profile wizard ...` — set up a profile step by step.

The web UI creates a profile through a wizard: LLM Providers → Tools → Memory →
Channels → Complete, finishing with one ``POST /api/config/setup`` that mints
the profile's token and hands over a configuration file. ``cremind profile
create`` does none of that — it makes an empty shell with no token — so an agent
asked to "create a profile" produced something the user could not sign in as.

This is that wizard for the terminal, shaped for an agent driving it from chat:
one command per conversational turn, answers accumulating in a draft file, and
a single POST at the end. Each question-printing command ends with a line
telling the agent to stop and ask the *user*, because the alternative — the
agent choosing someone's LLM provider and pasting a key it invented — is the
failure this whole feature exists to prevent.

Every subcommand runs as ``admin``: ``POST /api/config/setup`` refuses any other
profile for a non-first setup, so checking here turns a mid-wizard 403 into a
first-command refusal that names the fix.
"""

from __future__ import annotations

import sys
from typing import Optional

import typer

from app.cli.commands._helpers import graceful_errors

wizard_app = typer.Typer(
    name="wizard",
    help=(
        "Set up a profile step by step (LLM, tools, memory, channels), then mint "
        "its token and configuration file."
    ),
    no_args_is_help=True,
)

#: Printed after every block of questions. The shared documentation-search
#: directive tells the agent to run state-changing commands without asking;
#: for this family that is exactly wrong, and a chat turn has no structured
#: question form to fall back on — ending the turn IS the question.
_STOP_LINE = (
    "STOP: ask the user the questions above and end your turn — "
    "do not choose values for them."
)


# ── shared plumbing ──────────────────────────────────────────────────────


def _fail(message: str) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code=1)


def _require_admin_client(ctx: typer.Context):
    """``(cfg, acting_profile)`` for a wizard command, or exit 1 explaining why.

    Returns the acting profile so every path writes its draft into that
    profile's own slice of the System Directory.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.me import get_me
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run():
        async with Client(cfg) as client:
            return await get_me(client)

    me = asyncio.run(_run())
    if me.profile != "admin":
        _fail(
            f"the profile wizard runs as admin (this token is '{me.profile}'). "
            "Creating a profile is an admin action. Re-run with admin's token:\n"
            '  cremind --token "$(cremind auth show --profile admin)" profile wizard ...'
        )
    return cfg, me.profile


def _load_draft_or_fail(acting: str, name: str):
    from app.cli import wizard_draft

    try:
        draft = wizard_draft.load(acting, name)
    except wizard_draft.DraftError as e:
        _fail(str(e))
    if draft is None:
        _fail(
            f"no wizard draft for '{name}'. Start one with: "
            f"cremind profile wizard start {name}"
        )
    return draft


def _status_rows(acting: str, draft) -> list[tuple[str, str]]:
    from app.cli import wizard_draft

    rows = [
        ("profile", draft.profile),
        ("adopt", "true" if draft.adopt else "false"),
        ("draft", str(wizard_draft.draft_path(acting, draft.profile)).replace("\\", "/")),
    ]
    for step in wizard_draft.STEPS:
        rows.append((step, wizard_draft.summarize_step(step, draft)))
    nxt = wizard_draft.next_step(draft)
    rows.append(("next_step", nxt or "-"))
    rows.append(("ready", "yes" if wizard_draft.is_ready(draft) else "no"))
    return rows


def _print_progress(acting: str, draft, mode, *, step: Optional[str] = None) -> None:
    """The status header, then the questions for the step that needs answering."""
    from app.cli import wizard_draft, wizard_steps
    from app.cli.output import print_json, print_kv

    target = step or wizard_draft.next_step(draft)

    if mode.json:
        payload = {
            "profile": draft.profile,
            "adopt": draft.adopt,
            "draft": str(wizard_draft.draft_path(acting, draft.profile)).replace("\\", "/"),
            "created_at": draft.created_at,
            "updated_at": draft.updated_at,
            "steps": {
                s: {
                    "status": draft.status(s),
                    "payload": wizard_draft.mask_secrets(draft.payload(s)),
                }
                for s in wizard_draft.STEPS
            },
            "next_step": wizard_draft.next_step(draft),
            "ready": wizard_draft.is_ready(draft),
        }
        if target:
            payload["questions"] = wizard_steps.questions_json(target, draft.profile)
            payload["agent_next_step"] = _STOP_LINE
        print_json(payload)
        return

    print_kv(_status_rows(acting, draft))
    sys.stdout.write("\n")
    if target:
        sys.stdout.write(wizard_steps.questions(target, draft.profile) + "\n\n")
        sys.stdout.write(_STOP_LINE + "\n")
    else:
        sys.stdout.write(
            "all steps answered — create the profile with: "
            f"cremind profile wizard finish {draft.profile}\n"
        )


# ── start ────────────────────────────────────────────────────────────────


@wizard_app.command("start")
@graceful_errors
def wizard_start(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile to set up."),
    adopt: bool = typer.Option(
        False,
        "--adopt",
        help=(
            "Configure a profile that already exists (one `cremind profile create` "
            "left bare) instead of refusing."
        ),
    ),
) -> None:
    """Begin setting up a profile, and print the first step's questions."""
    import asyncio

    from app.cli import wizard_draft
    from app.cli.client._base import Client
    from app.cli.client.setup import get_setup_status
    from app.cli.output import OutputMode

    mode: OutputMode = ctx.obj["mode"]
    try:
        wizard_draft.validate_profile_name(name)
    except ValueError as e:
        _fail(str(e))
    if name == "admin":
        _fail(
            "the admin profile is created by first-run setup, not by this wizard. "
            "To reconfigure it: cremind setup reconfigure"
        )

    cfg, acting = _require_admin_client(ctx)

    async def _status():
        async with Client(cfg) as client:
            return await get_setup_status(client, name)

    status = asyncio.run(_status())

    if not status.get("setup_complete"):
        _fail(
            "this server has not finished its own first-run setup yet. "
            "Bootstrap it first: cremind setup complete --json-file <payload>"
        )
    exists = bool(status.get("profile_exists"))
    if exists and not adopt:
        _fail(
            f"profile '{name}' already exists. Re-run with --adopt to configure it "
            "with the wizard: the configuration is applied on top of it and a new "
            "token is minted, and tokens issued to it earlier stay valid."
        )
    if adopt and not exists:
        _fail(f"profile '{name}' does not exist — drop --adopt to create it.")

    existing = None
    try:
        existing = wizard_draft.load(acting, name)
    except wizard_draft.DraftError:
        # Unreadable: replaced below rather than blocking the user on a file
        # they never chose to create.
        existing = None
    if existing is not None:
        _fail(
            f"a wizard draft for '{name}' is already open (started {existing.created_at}). "
            f"Continue it with: cremind profile wizard status {name}\n"
            f"Or start over with: cremind profile wizard cancel {name}"
        )

    draft = wizard_draft.new_draft(name, adopt=adopt)
    wizard_draft.save(acting, draft)
    _print_progress(acting, draft, mode)


# ── status ───────────────────────────────────────────────────────────────


@wizard_app.command("status")
@graceful_errors
def wizard_status(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile whose draft to show."),
    step: Optional[str] = typer.Option(
        None,
        "--step",
        help="Show one step's questions instead of the next unanswered one.",
    ),
) -> None:
    """Show a draft's progress, and what the next step needs.

    Also how to recover a finished wizard: ``finish`` can run for minutes, long
    enough for a shell capture to be cut short, so it writes its result down
    and this prints it back.
    """
    from app.cli import wizard_draft
    from app.cli.output import OutputMode, print_json, print_kv

    mode: OutputMode = ctx.obj["mode"]
    cfg, acting = _require_admin_client(ctx)
    del cfg  # only the identity is needed; nothing here calls the server again

    if step is not None and step not in wizard_draft.STEPS:
        _fail(f"unknown step {step!r}: use one of {', '.join(wizard_draft.STEPS)}")

    result = wizard_draft.load_result(acting, name)
    if result is not None:
        if mode.json:
            print_json(result)
        else:
            print_kv([(k, str(v)) for k, v in result.items() if not isinstance(v, (dict, list))])
            sys.stdout.write(
                f"\nthis wizard finished — profile '{name}' exists and its token is above\n"
            )
        return

    draft = _load_draft_or_fail(acting, name)
    _print_progress(acting, draft, mode, step=step)


# ── set ──────────────────────────────────────────────────────────────────


@wizard_app.command("set")
@graceful_errors
def wizard_set(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile whose draft to update."),
    step: str = typer.Argument(..., help="llm | tools | memory | channels."),
    pairs: Optional[list[str]] = typer.Argument(
        None, help="KEY=VALUE answers for this step.", metavar="KEY=VALUE...",
    ),
    json_file: Optional[str] = typer.Option(
        None,
        "--json-file",
        help="Read the whole step payload from a JSON file instead (replaces it).",
    ),
    no_validate: bool = typer.Option(
        False,
        "--no-validate",
        help="Store the answers without checking them against the live catalogs.",
    ),
) -> None:
    """Record one step's answers.

    ``KEY=VALUE`` pairs merge into whatever the step already holds, so answers
    can arrive over several turns; ``--json-file`` replaces the step outright.
    """
    from app.cli import wizard_draft, wizard_steps
    from app.cli.output import OutputMode

    mode: OutputMode = ctx.obj["mode"]
    if step not in wizard_draft.STEPS:
        _fail(f"unknown step {step!r}: use one of {', '.join(wizard_draft.STEPS)}")

    cfg, acting = _require_admin_client(ctx)
    draft = _load_draft_or_fail(acting, name)

    if not pairs and not json_file:
        # Never a silent no-op: this is the command an agent reaches for when it
        # has not actually asked anybody anything yet.
        sys.stdout.write(wizard_steps.questions(step, draft.profile) + "\n\n")
        sys.stdout.write(_STOP_LINE + "\n")
        _fail(
            f"nothing to set for step {step!r}: pass KEY=VALUE pairs (see above) "
            "or --json-file <path>."
        )
    if pairs and json_file:
        _fail("pass either KEY=VALUE pairs or --json-file, not both")

    current = draft.payload(step)
    try:
        if json_file:
            allow = (list, dict) if step == "channels" else (dict,)
            payload = wizard_draft.load_json_payload(json_file, allow=allow)
            if step == "channels" and isinstance(payload, dict):
                payload = [payload]
        else:
            kv = wizard_steps.parse_pairs(pairs or [])
            if step == "llm":
                payload = wizard_steps.apply_llm(current if isinstance(current, dict) else {}, kv)
            elif step == "tools":
                payload = wizard_steps.apply_tools(current if isinstance(current, dict) else {}, kv)
            elif step == "memory":
                payload = wizard_steps.apply_memory(current if isinstance(current, dict) else {}, kv)
            else:
                catalog = {} if no_validate else _fetch_catalog(cfg, "channels")
                payload = wizard_steps.apply_channel(
                    list(current) if isinstance(current, list) else [], kv, catalog,
                )
    except (wizard_steps.StepInputError, ValueError) as e:
        _fail(str(e))

    validation = wizard_steps.Validation()
    if not no_validate:
        validation = _validate_step(cfg, step, payload)
        if validation.errors:
            for err in validation.errors:
                typer.echo(err, err=True)
            _fail("nothing was saved — fix the answers above and run set again")

    draft.set_payload(step, payload)
    wizard_draft.save(acting, draft)

    for warning in validation.warnings:
        sys.stderr.write(f"Warning: {warning}\n")
    for item in validation.missing:
        sys.stderr.write(f"Still needed for {step}: {item}\n")

    _print_progress(acting, draft, mode)


def _fetch_catalog(cfg, step: str):
    """The one catalog a step's validation needs, fetched as admin."""
    import asyncio

    from app.cli.client._base import Client

    async def _run():
        async with Client(cfg) as client:
            if step == "channels":
                from app.cli.client.channels import get_channel_catalog

                return await get_channel_catalog(client)
            if step == "tools":
                from app.cli.client.tools import list_tools

                return await list_tools(client)
            if step == "memory":
                from app.cli.client.config import get_config_schema

                return await get_config_schema(client)
            from app.cli.client.llm import list_llm_providers

            return await list_llm_providers(client)

    return asyncio.run(_run())


def _validate_step(cfg, step: str, payload):
    """Run the step's rules against the live catalog."""
    import asyncio

    from app.cli import wizard_steps
    from app.cli.client._base import Client

    if step == "tools":
        return wizard_steps.validate_tools(payload, _fetch_catalog(cfg, "tools"))
    if step == "memory":
        return wizard_steps.validate_memory(payload, _fetch_catalog(cfg, "memory"))
    if step == "channels":
        return wizard_steps.validate_channels(payload, _fetch_catalog(cfg, "channels"))

    async def _llm():
        from app.cli.client.llm import get_provider_models, list_llm_providers

        async with Client(cfg) as client:
            providers = await list_llm_providers(client)
            models: dict[str, list[str]] = {}
            # Only the providers this payload actually names, and only for the
            # sign-in method it selects: the model list differs per credential.
            wanted = {
                str(payload.get(f"model_group.{role}") or "").partition("/")[0]
                for role in set(wizard_steps.MODEL_ROLE_KEYS.values())
            }
            for provider_name in sorted(n for n in wanted if n):
                entry = next(
                    (p for p in providers if str(p.get("name")) == provider_name), None,
                )
                method = (
                    wizard_steps.active_auth_method(entry, payload) if entry else None
                )
                try:
                    resp = await get_provider_models(
                        client, provider_name,
                        auth_method=str(method.get("id")) if method else None,
                    )
                except Exception:  # noqa: BLE001
                    # A provider whose catalogue cannot be listed (no key yet,
                    # network blip) must not block storing the answer — the
                    # model name is checked again at finish.
                    continue
                models[provider_name] = [
                    str(m.get("id")) for m in (resp.get("models") or []) if m.get("id")
                ]
            return providers, models

    providers, models = asyncio.run(_llm())
    return wizard_steps.validate_llm(payload, providers, models)


# ── skip ─────────────────────────────────────────────────────────────────


@wizard_app.command("skip")
@graceful_errors
def wizard_skip(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile whose draft to update."),
    step: str = typer.Argument(..., help="llm | tools | memory | channels."),
) -> None:
    """Leave a step unanswered and move on. It can be filled in later."""
    from app.cli import wizard_draft
    from app.cli.output import OutputMode

    mode: OutputMode = ctx.obj["mode"]
    if step not in wizard_draft.STEPS:
        _fail(f"unknown step {step!r}: use one of {', '.join(wizard_draft.STEPS)}")

    cfg, acting = _require_admin_client(ctx)
    del cfg
    draft = _load_draft_or_fail(acting, name)
    draft.skip(step)
    wizard_draft.save(acting, draft)

    notes = {
        "llm": (
            f"WARNING: without a model, '{name}' cannot answer anything — web chat "
            "reports the missing model, direct messages error, and group chats are "
            "ignored with no reply at all. Set one later in Settings → LLM Providers, "
            f'or: cremind --token "$(cremind auth show --profile {name})" '
            "llm providers configure <provider> --api-key <key>"
        ),
        "tools": "Skipped tools: the server's defaults apply (default-enabled tools on).",
        "memory": "Skipped memory: it stays off. Turn it on later with `cremind config set memory.enabled true`.",
        "channels": "Skipped channels: none are created. Add them later with `cremind channels add`.",
    }
    sys.stderr.write(notes[step] + "\n")
    _print_progress(acting, draft, mode)


# ── cancel ───────────────────────────────────────────────────────────────


@wizard_app.command("cancel")
@graceful_errors
def wizard_cancel(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile whose draft to discard."),
) -> None:
    """Discard a draft (and any finished-result record). Creates nothing."""
    import os

    from app.cli import wizard_draft

    cfg, acting = _require_admin_client(ctx)
    del cfg
    path = wizard_draft.draft_path(acting, name)
    if not wizard_draft.delete(acting, name):
        _fail(f"no wizard draft for '{name}' at {path}")
    typer.echo(f"cancelled the wizard draft for '{name}' ({str(path).replace(os.sep, '/')})")


# ── finish ───────────────────────────────────────────────────────────────


@wizard_app.command("finish")
@graceful_errors
def wizard_finish(
    ctx: typer.Context,
    name: str = typer.Argument(..., help="Profile to create."),
    fmt: str = typer.Option(
        "md", "--format", "-f", help="Configuration file format: md | json | env.",
    ),
    out: Optional[str] = typer.Option(
        None,
        "--out",
        help="Where to write the configuration file (default: the profile's exports directory).",
    ),
    no_file: bool = typer.Option(
        False, "--no-file", help="Skip writing the configuration file.",
    ),
    agent_url: Optional[str] = typer.Option(
        None,
        "--agent-url",
        help=(
            "The address the login URL should use, when this server's APP_URL is "
            "not what a browser reaches (a split-origin dev box, a tunnel)."
        ),
    ),
    pending_https: bool = typer.Option(
        False,
        "--pending-https",
        help="Mark that agent URL as the HTTPS origin that answers after a restart.",
    ),
) -> None:
    """Create the profile, mint its token, and write its configuration file."""
    import asyncio
    import dataclasses
    import time

    from app.cli import session, wizard_draft, wizard_steps
    from app.cli.client._base import APIError, Client
    from app.cli.client.setup import complete_setup, get_setup_status
    from app.cli.output import OutputMode, print_json, print_kv

    mode: OutputMode = ctx.obj["mode"]
    if fmt.strip().lower() not in ("md", "json", "env"):
        _fail(f"unknown --format {fmt!r}: use md, json or env")
    fmt = fmt.strip().lower()

    cfg, acting = _require_admin_client(ctx)
    draft = _load_draft_or_fail(acting, name)

    pending = wizard_draft.pending_steps(draft)
    if pending:
        _fail(
            f"these steps are still unanswered: {', '.join(pending)}. "
            f"Set or skip each one first (cremind profile wizard status {name})."
        )

    # A "set" LLM step that is missing its main model is the one incomplete
    # answer that matters: the profile would be created agent-dead.
    if draft.status("llm") == wizard_draft.STATUS_SET:
        providers = _fetch_catalog(cfg, "llm")
        missing = wizard_steps.llm_missing(draft.payload("llm"), providers)
        if missing:
            _fail(
                "the LLM step is incomplete: " + "; ".join(missing) + ".\n"
                f"Finish it (cremind profile wizard set {name} llm ...) or skip it "
                f"deliberately (cremind profile wizard skip {name} llm)."
            )

    async def _status():
        async with Client(cfg) as client:
            return await get_setup_status(client, name)

    status = asyncio.run(_status())
    if not status.get("setup_complete"):
        _fail("this server has not finished its own first-run setup yet.")
    exists = bool(status.get("profile_exists"))
    if exists and not draft.adopt:
        _fail(
            f"profile '{name}' now exists but this draft was started without --adopt. "
            f"Discard it and start again: cremind profile wizard cancel {name} && "
            f"cremind profile wizard start {name} --adopt"
        )
    if draft.adopt and not exists:
        _fail(
            f"profile '{name}' no longer exists but this draft was started with --adopt. "
            f"Discard it and start again: cremind profile wizard cancel {name} && "
            f"cremind profile wizard start {name}"
        )

    body = wizard_draft.build_setup_body(draft)

    sys.stderr.write(
        "Applying setup — this can take several minutes when tools need packages installed.\n"
    )
    sys.stderr.flush()

    async def _apply():
        # No request timeout: the POST pip-installs whatever features the
        # payload implies before it answers.
        async with Client(cfg, timeout=None) as client:
            task = asyncio.ensure_future(complete_setup(client, body))
            started = time.monotonic()
            while True:
                done, _pending = await asyncio.wait({task}, timeout=5.0)
                if done:
                    return task.result()
                # A heartbeat more often than every 10s of silence, because a
                # silent command is exactly what the agent's shell classifies
                # as "long-running" and stops reading.
                sys.stderr.write(
                    f"  … still applying ({int(time.monotonic() - started)}s)\n"
                )
                sys.stderr.flush()

    resp = asyncio.run(_apply())

    # Everything below is best-effort: the profile and its token now exist, so
    # a failure here must report itself, not fail the command and leave the
    # user thinking nothing happened.
    token_file = ""
    try:
        token_file = str(session.write_token(name, resp.token)).replace("\\", "/")
    except (OSError, ValueError) as e:
        sys.stderr.write(f"Warning: could not save the token file for '{name}': {e}\n")

    export_cfg = dataclasses.replace(cfg, token=resp.token)
    export_agent_url = agent_url or (resp.next_origin if resp.tls_pending else None)
    export_pending = pending_https or bool(resp.tls_pending)

    async def _export(export_fmt: str):
        from app.cli.client.config import export_config

        async with Client(export_cfg) as client:
            return await export_config(
                client, export_fmt,
                agent_url=export_agent_url,
                pending_https=export_pending,
            )

    login_url = ""
    expires_at = resp.expires_at
    try:
        import json as _json

        snapshot = _json.loads(asyncio.run(_export("json")).content.decode("utf-8"))
        login_url = str(snapshot.get("loginUrl") or "")
        token_file = str(snapshot.get("tokenFile") or token_file)
        expires_at = str(snapshot.get("tokenExpiresAt") or expires_at)
    except (APIError, OSError, ValueError) as e:
        sys.stderr.write(f"Warning: could not read the configuration snapshot: {e}\n")

    config_file = ""
    if not no_file:
        try:
            export = asyncio.run(_export(fmt))
            if out:
                from pathlib import Path

                target = Path(out)
                if target.is_dir():
                    target = target / export.filename
            else:
                # The acting profile's own slice: inside the `system_file`
                # tool's allowed roots (so the agent can hand the file to the
                # user as a download) and out of every other profile's reach.
                target = session.system_dir() / acting / "exports" / export.filename
            config_file = str(
                wizard_draft.write_private_file(target, export.content),
            ).replace("\\", "/")
        except (APIError, OSError, ValueError) as e:
            sys.stderr.write(f"Warning: could not write the configuration file: {e}\n")

    pairing = []
    catalog = {}
    if resp.channels:
        try:
            catalog = _fetch_catalog(cfg, "channels")
        except Exception:  # noqa: BLE001
            catalog = {}
    for channel in resp.channels:
        channel_type = str(channel.get("channel_type") or "")
        channel_mode = str(channel.get("mode") or "")
        if wizard_steps.mode_needs_pairing(catalog, channel_type, channel_mode):
            pairing.append({
                "id": str(channel.get("id") or ""),
                "channel_type": channel_type,
                "mode": channel_mode,
                "command": (
                    f'cremind --token "$(cremind auth show --profile {name})" '
                    f"channels pair {channel.get('id')}"
                ),
            })

    skipped = wizard_draft.skipped_steps(draft)
    adopted = any(w.get("code") == "adopted_existing" for w in resp.warnings)
    agent_next_steps = _agent_next_steps(
        name, config_file, login_url, token_file, pairing, skipped,
    )

    result = {
        "success": True,
        "profile": resp.profile or name,
        "expires_at": expires_at,
        "token": resp.token,
        "login_url": login_url,
        "token_file": token_file,
        "config_file": config_file,
        "config_format": "" if no_file else fmt,
        "adopted": adopted,
        "skipped": skipped,
        "channels": [dict(c) for c in resp.channels],
        "channel_errors": [dict(c) for c in resp.channel_errors],
        "pairing_required": pairing,
        "warnings": [dict(w) for w in resp.warnings],
        "restart_required": resp.restart_required,
        "installed_features": list(resp.installed_features),
        "failed_features": list(resp.failed_features),
        "tls_pending": resp.tls_pending,
        "next_origin": resp.next_origin,
        "agent_next_steps": agent_next_steps,
    }
    # The draft has served its purpose; the result replaces it, so a shell
    # capture cut short by a slow setup can still be recovered with `status`.
    wizard_draft.delete(acting, name, include_result=False)
    try:
        wizard_draft.save_result(acting, name, result)
    except OSError:
        pass  # the output below is still complete

    if mode.json:
        print_json(result)
    else:
        rows = [
            ("profile", result["profile"]),
            ("expires_at", expires_at),
            ("token", resp.token),
            ("login_url", login_url or "(unknown — see --agent-url)"),
            ("token_file", token_file),
        ]
        if config_file:
            rows.append(("config_file", config_file))
        if skipped:
            rows.append(("skipped", ", ".join(skipped)))
        for channel in resp.channels:
            suffix = " (needs pairing)" if any(
                p["id"] == str(channel.get("id")) for p in pairing
            ) else ""
            rows.append((
                "channel",
                f"{channel.get('channel_type')}/{channel.get('mode')} "
                f"id={channel.get('id')}{suffix}",
            ))
        print_kv(rows)

    for warning in resp.warnings:
        sys.stderr.write(f"Warning: {warning.get('message') or warning.get('code')}\n")
    for err in resp.channel_errors:
        sys.stderr.write(
            f"Warning: channel {err.get('channel_type')}: {err.get('error')}\n"
        )
    if resp.failed_features:
        sys.stderr.write(
            f"Warning: these features failed to install: {', '.join(resp.failed_features)}\n"
        )
    if resp.restart_required:
        sys.stderr.write(
            "Restart Cremind so the newly installed features "
            f"({', '.join(resp.installed_features) or 'just installed'}) load.\n"
        )
    if resp.tls_pending and resp.next_origin:
        sys.stderr.write(
            f"HTTPS switch pending — after the restart use {resp.next_origin}\n"
        )
    sys.stderr.write("\n" + agent_next_steps + "\n")


def _agent_next_steps(
    name: str,
    config_file: str,
    login_url: str,
    token_file: str,
    pairing: list[dict],
    skipped: list[str],
) -> str:
    """What to do with what this command just produced.

    Written for the agent that ran it, because that is who reads this output —
    and because the two things a user needs (a file they can download, a URL
    they can open) are both things chat renders only if asked correctly: a
    Markdown link to a file path 401s, and only a tool result carrying the file
    becomes a download.
    """
    lines = ["Next steps for an agent:"]
    if config_file:
        lines += [
            "  1. Give the user the configuration file as a download: call the",
            f'     system_file tool\'s read_file on "{config_file}".',
            "     Its result carries the file, and the chat renders a Download chip.",
            "     Do not write a Markdown link to that path (file links carry no auth",
            "     header and will not open), and do not restate the file's contents.",
        ]
    else:
        lines += [
            "  1. No configuration file was written. The user can produce one with",
            f'     cremind --token "$(cremind auth show --profile {name})" config export',
        ]
    lines += [
        f"  2. Tell the user the login URL {login_url or '(run with --agent-url)'} and",
        "     the token printed above: they open the URL in another tab and paste the",
        "     token into the sign-in box. The server also keeps it at",
        f"     {token_file or '<system dir>/tokens/' + name + '.token'}",
    ]
    step = 3
    if pairing:
        lines.append(f"  {step}. Channels awaiting pairing:")
        for entry in pairing:
            lines.append(f"       {entry['channel_type']} ({entry['id']}) — {entry['command']}")
        step += 1
    if skipped:
        lines += [
            f"  {step}. Skipped steps ({', '.join(skipped)}) can be filled in later as",
            f'       the new profile: cremind --token "$(cremind auth show --profile {name})" ...',
        ]
        step += 1
    lines.append(
        f"  {step}. If this output was cut short, run: "
        f"cremind --json profile wizard status {name}"
    )
    return "\n".join(lines)
