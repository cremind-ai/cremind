"""`cremind tools ...` — list and configure tools & skills.

Mirrors `cli/cmd/tools.go`.
"""

from __future__ import annotations

import json as _json
import sys
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


tools_app = typer.Typer(
    name="tools",
    help="List and configure tools & skills.",
    no_args_is_help=True,
)


@tools_app.command("list")
@graceful_errors
def tools_list(
    ctx: typer.Context,
    type_filter: Optional[str] = typer.Option(
        None,
        "--type",
        help="Filter by tool_type: built-in, mcp, a2a, skill, intrinsic.",
    ),
) -> None:
    """List all tools (built-in, mcp, a2a, skill, intrinsic)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import list_tools
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_tools(client, type_filter or "")

    tools = asyncio.run(_run())

    if mode.json:
        print_json(tools)
        return

    table = Table(mode, "TOOL_ID", "TYPE", "ENABLED", "CONFIGURED", "NAME")
    for row in tools:
        table.add_row(
            string_field(row, "tool_id"),
            string_field(row, "tool_type"),
            bool_field(row, "enabled", True),
            bool_field(row, "configured", False),
            string_field(row, "name"),
        )
    table.render()


@tools_app.command("get")
@graceful_errors
def tools_get(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Show detailed configuration for a tool."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import get_tool
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tool(client, tool_id)

    tool = asyncio.run(_run())

    if mode.json:
        print_json(tool)
        return

    print_kv([
        ("tool_id", string_field(tool, "tool_id")),
        ("name", string_field(tool, "name")),
        ("tool_type", string_field(tool, "tool_type")),
        ("description", string_field(tool, "description")),
        ("configured", bool_field(tool, "configured", False)),
    ])
    if isinstance(tool.get("config"), dict):
        sys.stdout.write("\n--- config ---\n")
        sys.stdout.write(_json.dumps(tool["config"], indent=2, ensure_ascii=False, default=str) + "\n")


@tools_app.command("enable")
@graceful_errors
def tools_enable(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Enable a tool (A2A, MCP, skill, or an optional built-in like claude_code)."""
    _set_tool_enabled(ctx, tool_id, True)


@tools_app.command("disable")
@graceful_errors
def tools_disable(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Disable a tool (A2A, MCP, skill, or an optional built-in like claude_code)."""
    _set_tool_enabled(ctx, tool_id, False)


def _set_tool_enabled(ctx: typer.Context, tool_id: str, enabled: bool) -> None:
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_enabled
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_enabled(client, tool_id, enabled)

    asyncio.run(_run())


@tools_app.command("set-var")
@graceful_errors
def tools_set_var(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    pairs: list[str] = typer.Argument(
        ...,
        help="One or more KEY=VALUE pairs.",
        metavar="KEY=VALUE",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Set even if the value isn't a recognized option (e.g. a custom or "
            "unverified model id for a variable with a live option list)."
        ),
    ),
) -> None:
    """Set Tool Variables (env-style key/value pairs)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_variables
    from app.cli.config import Config

    if not pairs:
        typer.echo("expected at least one KEY=VALUE pair", err=True)
        raise typer.Exit(code=1)

    variables: dict[str, str] = {}
    for kv in pairs:
        if "=" not in kv:
            typer.echo(f"expected KEY=VALUE, got '{kv}'", err=True)
            raise typer.Exit(code=1)
        k, v = kv.split("=", 1)
        variables[k] = v

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_variables(client, tool_id, variables, allow_unknown=force)

    asyncio.run(_run())


@tools_app.command("set-args")
@graceful_errors
def tools_set_args(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    json_body: str = typer.Option(
        ...,
        "--json",
        help="Tool arguments as a JSON object.",
    ),
) -> None:
    """Set Tool Arguments from a JSON object."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_arguments
    from app.cli.config import Config

    try:
        values = _json.loads(json_body)
    except _json.JSONDecodeError as e:
        typer.echo(f"invalid --json: {e}", err=True)
        raise typer.Exit(code=1) from e
    if not isinstance(values, dict):
        typer.echo("--json must be an object", err=True)
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_arguments(client, tool_id, values)

    asyncio.run(_run())


@tools_app.command("get-args")
@graceful_errors
def tools_get_args(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """Show a tool's arguments schema and its saved argument values.

    Complements `set-args`. Derived from the tool detail (`tools get`) — there
    is no dedicated GET arguments endpoint.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import get_tool_arguments
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tool_arguments(client, tool_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    sys.stdout.write("--- arguments_schema ---\n")
    sys.stdout.write(
        _json.dumps(out.get("arguments_schema"), indent=2, ensure_ascii=False, default=str) + "\n"
    )
    sys.stdout.write("\n--- arguments ---\n")
    sys.stdout.write(
        _json.dumps(out.get("arguments"), indent=2, ensure_ascii=False, default=str) + "\n"
    )


@tools_app.command("leaves")
@graceful_errors
def tools_leaves(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
) -> None:
    """List a tool's sub-tools ("leaves") with their per-profile enabled state."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import list_tool_leaves
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await list_tool_leaves(client, tool_id)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    if out.get("disconnected"):
        sys.stderr.write("(tool is disconnected — live sub-tool list unavailable)\n")
    leaves = out.get("leaves") or []
    table = Table(mode, "LEAF", "NAME", "ENABLED", "DESCRIPTION")
    for leaf in leaves:
        if not isinstance(leaf, dict):
            continue
        table.add_row(
            string_field(leaf, "leaf_name"),
            string_field(leaf, "name"),
            bool_field(leaf, "enabled", True),
            string_field(leaf, "description"),
        )
    table.render()


@tools_app.command("options")
@graceful_errors
def tools_options(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    refresh: bool = typer.Option(
        False, "--refresh", help="Bypass the server-side cache and refetch.",
    ),
) -> None:
    """List live option values for a tool's dynamic variables.

    For ``claude_code`` this lists the Claude models available to the logged-in
    account (``CLAUDE_CODE_MODEL``) and the permission modes the installed Claude
    Agent SDK accepts (``CLAUDE_CODE_PERMISSION_MODE``). When the list resolves,
    ``set-var`` rejects a value that isn't in it (pass ``--force`` to override);
    if it can't be fetched, any value is accepted. Pipe with ``set-var`` to
    apply, e.g. ``cremind tools set-var claude_code CLAUDE_CODE_MODEL=<id>``.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import get_tool_variable_options
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tool_variable_options(client, tool_id, refresh=refresh)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    variables = out.get("variables") or {}
    if not variables:
        sys.stderr.write("(tool has no dynamic variables)\n")
        return
    table = Table(mode, "VARIABLE", "VALUE", "LABEL")
    for var_name, info in variables.items():
        if not isinstance(info, dict):
            continue
        error = info.get("error")
        if error:
            sys.stderr.write(f"({var_name}: {error})\n")
        for opt in info.get("options") or []:
            if not isinstance(opt, dict):
                continue
            table.add_row(var_name, str(opt.get("id", "")), str(opt.get("label", "")))
    table.render()


@tools_app.command("set-leaf")
@graceful_errors
def tools_set_leaf(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    pairs: list[str] = typer.Argument(
        ...,
        help="One or more NAME=true|false sub-tool toggles.",
        metavar="NAME=BOOL",
    ),
) -> None:
    """Enable or disable a tool's sub-tools ("leaves")."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import set_tool_leaves
    from app.cli.config import Config

    if not pairs:
        typer.echo("expected at least one NAME=true|false pair", err=True)
        raise typer.Exit(code=1)

    leaves: dict[str, bool] = {}
    for kv in pairs:
        if "=" not in kv:
            typer.echo(f"expected NAME=true|false, got '{kv}'", err=True)
            raise typer.Exit(code=1)
        name, raw = kv.split("=", 1)
        v = raw.strip().lower()
        if v in ("true", "1", "yes", "on"):
            leaves[name] = True
        elif v in ("false", "0", "no", "off"):
            leaves[name] = False
        else:
            typer.echo(f"'{raw}' is not a boolean (use true/false)", err=True)
            raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await set_tool_leaves(client, tool_id, leaves)

    asyncio.run(_run())


# A group that is also a command: bare `cremind tools coding-agents` still
# prints the table it always printed, and `login` / `logout` hang off it. No
# `help=` here on purpose — the callback's docstring is the group's help, so the
# long explanation is not duplicated in two places that can drift.
coding_agents_app = typer.Typer(
    name="coding-agents",
    no_args_is_help=False,
    invoke_without_command=True,
)
tools_app.add_typer(coding_agents_app, name="coding-agents")


@coding_agents_app.callback()
@graceful_errors
def tools_coding_agents(
    ctx: typer.Context,
    probe: bool = typer.Option(
        False,
        "--probe",
        help=(
            "Also run each installed agent's live sign-in check (one cheap "
            "request per agent; no coding task, no file changes)."
        ),
    ),
) -> None:
    """Show whether Claude Code / Codex are installed, on, and signed in.

    Reports the three things that have to line up before the agent can delegate
    coding work: the SDK (which bundles the CLI binary) is installed, the tool
    is enabled for this profile, and a credential resolves. Without `--probe`
    the LOGGED_IN column is `-` — the credential is only read from config, not
    exercised.

    `login` and `logout` (below) sign this profile in and out of the delegate's
    own CLI.
    """
    # The listing is what `cremind tools coding-agents` does when it is the
    # whole command; with a subcommand this callback only carries the group,
    # and must not require a token or open a connection on its behalf.
    if ctx.invoked_subcommand is not None:
        return

    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import get_coding_agents, probe_coding_agent
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            agents = await get_coding_agents(client)
            if not probe:
                return agents
            # Probing an agent whose SDK is absent can only report "not
            # installed" again, so spend the round-trip only where it can tell
            # the user something new.
            for agent in agents:
                if not agent.get("sdk_installed"):
                    continue
                # Someone who typed --probe asked for the live answer, not the
                # one cached from the burst 15 seconds ago.
                agent["probe"] = await probe_coding_agent(
                    client, str(agent.get("tool_id") or ""), fresh=True,
                )
            return agents

    agents = asyncio.run(_run())

    if mode.json:
        print_json(agents)
        return

    table = Table(mode, "AGENT", "INSTALLED", "ENABLED", "CREDENTIAL", "LOGGED_IN")
    for row in agents:
        table.add_row(
            string_field(row, "tool_id"),
            bool_field(row, "sdk_installed", False),
            bool_field(row, "enabled", False),
            string_field(row, "credential_source") or "none",
            _logged_in_cell(row.get("probe") if probe else None),
        )
    table.render()
    for row in agents:
        message = string_field(row, "message")
        if message:
            sys.stderr.write(f"({string_field(row, 'tool_id')}: {message})\n")


def _logged_in_cell(probe: Optional[dict]) -> str:
    """Render one agent's probe outcome.

    ``-`` means "not checked" (no ``--probe``, or the SDK isn't installed);
    ``unknown`` means the check ran but could not decide — a timeout or a
    missing binary — which is NOT the same as "logged out".
    """
    if not isinstance(probe, dict):
        return "-"
    logged_in = probe.get("logged_in")
    if logged_in is True:
        return "yes"
    if logged_in is False:
        return "no"
    return "unknown"


# Where a user without shell access on the server does the same thing. Named at
# the moment the CLI refuses, because that is when the alternative matters.
_SIGN_IN_IN_THE_UI = (
    "Settings -> Tools & Skills -> Coding Agents -> Sign in does it from a browser."
)


def _account_summary(probe: dict) -> str:
    """One line naming who the delegate now says it is.

    The two delegates describe an account with different keys (Claude Code
    reports `auth_method` / `org_name` / `subscription_type`, Codex a `type`
    and a `plan_type`), so take whichever are present rather than teaching the
    CLI each SDK's shape — it only has to be readable.
    """
    account = probe.get("account")
    if not isinstance(account, dict):
        account = {}
    email = str(account.get("email") or "").strip()
    extras = [
        str(account[key]).strip()
        for key in ("plan_type", "subscription_type", "org_name", "type", "auth_method")
        if str(account.get(key) or "").strip()
    ]
    if email and extras:
        return f"{email} ({', '.join(extras)})"
    if email:
        return email
    if extras:
        return ", ".join(extras)
    return ""


@coding_agents_app.command("login")
@graceful_errors
def coding_agents_login(
    ctx: typer.Context,
    agent: str = typer.Argument(
        ..., metavar="AGENT", help="claude_code or codex.",
    ),
    shared: bool = typer.Option(
        False,
        "--shared",
        help=(
            "Sign in to the server-wide login that profiles without one of "
            "their own inherit, instead of this profile's."
        ),
    ),
) -> None:
    """Run the agent's own CLI login, here, in this terminal.

    The sign-in is the vendor CLI's (`claude auth login`, `codex login
    --device-auth`) — Cremind only says which binary to run and which home to
    write the credential into, then hands it this terminal so the browser
    prompt and the pasted code behave exactly as they would outside Cremind.

    That means this only works where the CLI actually lives. When `cremind` is
    pointed at a remote server the command refuses rather than logging you into
    the wrong machine's home directory.
    """
    import asyncio
    import os
    import subprocess
    from pathlib import Path

    from app.cli.client._base import Client
    from app.cli.client.tools import get_coding_agent_cli, probe_coding_agent
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _describe() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_coding_agent_cli(client, agent)

    info = asyncio.run(_describe())
    binary = str(info.get("binary") or "")
    system_dir = str(info.get("system_dir") or "")
    hostname = str(info.get("server_hostname") or "")

    if not binary:
        typer.echo(
            f"The Cremind server reports no {agent} CLI to sign in with. Install "
            f"the feature first (`cremind features install {agent}`), then run "
            f"this again.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Two independent checks, because either one alone lies. The binary path
    # can exist locally by coincidence (both machines have the same SDK
    # installed at the same path), and the system dir can exist locally on a
    # developer box that also runs its own Cremind. Only both together mean the
    # server's filesystem is this filesystem — and if it is not, running the
    # login here would write a credential into a home directory the server will
    # never read.
    on_this_host = Path(binary).is_file() and bool(system_dir) and Path(system_dir).is_dir()
    if not on_this_host:
        where = f" ({hostname})" if hostname else ""
        typer.echo(
            f"The {agent} CLI lives on the Cremind server{where}, not on this "
            f"machine, so the sign-in has to happen there. Run this same command "
            f"on that host, or sign in without a shell: {_SIGN_IN_IN_THE_UI}",
            err=True,
        )
        raise typer.Exit(code=1)

    argv = [str(a) for a in (info.get("login_argv") or []) if str(a)]
    if not argv:
        typer.echo(f"The server did not report a login command for {agent}.", err=True)
        raise typer.Exit(code=1)

    env_key = "shared_env" if shared else "profile_env"
    overrides = info.get(env_key)
    env = dict(os.environ)
    if isinstance(overrides, dict):
        env.update({str(k): str(v) for k, v in overrides.items()})

    # subprocess.call, not run(capture_output=...): the whole point is that the
    # child inherits this terminal. Both CLIs print a URL and then read the
    # pasted code from stdin, so capturing either stream would hang the login
    # behind output the user never sees.
    typer.echo(f"Running: {' '.join(argv)}", err=True)
    code = subprocess.call(argv, env=env)
    if code != 0:
        typer.echo(f"{agent} login exited with status {code}.", err=True)
        # A child killed by a signal reports a negative code on POSIX, which is
        # not a usable exit status; collapse those to a plain failure.
        raise typer.Exit(code=code if code > 0 else 1)

    async def _probe() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await probe_coding_agent(client, agent, fresh=True)

    probe = asyncio.run(_probe())

    if mode.json:
        print_json(probe)
        return
    if probe.get("logged_in") is True:
        who = _account_summary(probe)
        typer.echo(f"Signed in{f' as {who}' if who else ''}.")
        return
    # The login command said it succeeded but the server still cannot see it.
    # Usually the scope: a `--shared` login when this profile has its own home,
    # or the reverse. Report it rather than claiming success.
    detail = str(probe.get("probe_detail") or probe.get("message") or "").strip()
    typer.echo(
        "The login command finished, but the server does not report a sign-in "
        f"for this profile yet.{f' {detail}' if detail else ''}",
        err=True,
    )
    raise typer.Exit(code=1)


@coding_agents_app.command("logout")
@graceful_errors
def coding_agents_logout(
    ctx: typer.Context,
    agent: str = typer.Argument(
        ..., metavar="AGENT", help="claude_code or codex.",
    ),
    shared: bool = typer.Option(
        False,
        "--shared",
        help=(
            "Sign out the server-wide login every profile without one of its "
            "own inherits (admin). Affects every such profile."
        ),
    ),
) -> None:
    """Sign this profile out of a coding delegate's CLI.

    Unlike `login`, this works from anywhere: the server runs the CLI's own
    logout against the resolved home and removes the credential file, so no
    binary is needed on this machine.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import logout_coding_agent
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    scope = "shared" if shared else "profile"

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await logout_coding_agent(client, agent, scope=scope)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    detail = string_field(out, "detail")
    scope_out = string_field(out, "scope") or scope
    if out.get("ok"):
        typer.echo(f"Signed out of {agent} ({scope_out}).{f' {detail}' if detail else ''}")
        return
    typer.echo(detail or f"Could not sign out of {agent}.", err=True)
    raise typer.Exit(code=1)


@tools_app.command("register-long-running")
@graceful_errors
def tools_register_long_running(
    ctx: typer.Context,
    tool_id: str = typer.Argument(..., help="Tool id."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Bypass the duplicate-command check.",
    ),
) -> None:
    """Spawn a skill's long_running_app and persist it as autostart."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.tools import register_long_running_app
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await register_long_running_app(client, tool_id, force)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return
    print_kv([
        ("process_id", string_field(out, "process_id")),
        ("autostart_id", str(out.get("autostart_id", ""))),
        ("command", string_field(out, "command")),
        ("working_dir", string_field(out, "working_dir")),
    ])
