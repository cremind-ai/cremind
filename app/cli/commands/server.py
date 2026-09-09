"""`cremind server ...` — operate the running Cremind server.

Mirrors the Developer page's operational controls: restart the backend
(with an install-mode-aware confirmation), probe `/health`, read the
server's build version + tray/install capabilities, and describe the install
it is running in (Docker/native/Kubernetes, release channel, VNC, paths).

Note the distinction from the root `cremind version` command, which prints the
*locally installed* package version. `cremind server version` reports what the
*connected server* is running — they can differ (e.g. an older Electron pinned
to a newer wheel, or a remote `--server`).

`health`, `version` and `capabilities` are unauthenticated, so they work
without a token — handy for probing a server before login. `environment` and
`restart` are admin-only: the description names the server's paths and bind
host, which is not public the way a tray-gating list is.
"""

from __future__ import annotations

import sys
from typing import Any

import typer

from app.cli.commands._helpers import graceful_errors


server_app = typer.Typer(
    name="server",
    help="Operate the running Cremind server (health, version, restart).",
    no_args_is_help=True,
)


# install_mode → the restart caveat the Developer page shows.
_RESTART_CAVEAT = {
    "docker": "Docker install — the container will restart automatically "
              "(usually 5-15 seconds).",
    "electron": "Electron install — Cremind will relaunch the backend "
                "automatically.",
}
_RESTART_CAVEAT_SUPERVISED = (
    "Boot service detected — the OS (systemd / launchd / Scheduled Task) will "
    "restart the backend automatically (a few seconds)."
)
_RESTART_CAVEAT_DEFAULT = (
    "No supervisor detected — the backend will stay DOWN after it stops. "
    "You will need to relaunch `cremind serve` manually, or register a boot "
    "service with `cremind boot enable`."
)


@server_app.command("health")
@graceful_errors
def server_health(ctx: typer.Context) -> None:
    """Probe the server's /health endpoint (no token required).

    Exits non-zero when the server reports a degraded subsystem (HTTP 503);
    a `disabled` vector store is healthy, not an error.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_health
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run() -> tuple[int, Any]:
        async with Client(cfg) as client:
            return await get_health(client)

    status_code, body = asyncio.run(_run())
    body = body if isinstance(body, dict) else {}

    if mode.json:
        print_json(body)
    else:
        print_kv([
            ("status", str(body.get("status") or "")),
            ("db", str(body.get("db") or "")),
            ("vectorstore", str(body.get("vectorstore") or "")),
        ])

    if status_code >= 400:
        raise typer.Exit(code=1)


@server_app.command("version")
@graceful_errors
def server_version(ctx: typer.Context) -> None:
    """Show the connected server's build version and release channel.

    Distinct from `cremind version`, which prints the locally installed CLI
    package version.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_server_version
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_server_version(client)

    info = asyncio.run(_run())

    if mode.json:
        print_json(info)
        return
    print_kv([
        ("backend", str(info.get("backend") or "")),
        ("schema", str(info.get("schema") or "")),
        ("channel", str(info.get("channel") or "")),
        ("min_supported_upgrade_from", str(info.get("min_supported_upgrade_from") or "")),
    ])


@server_app.command("capabilities")
@graceful_errors
def server_capabilities(ctx: typer.Context) -> None:
    """Show the server's install mode, deployment and the UI features it exposes.

    Reads the public tray-capabilities endpoint. The richer admin
    `/api/services/capabilities` (per-service deployment modes) is
    Setup-Wizard-scoped and intentionally not exposed here; the paths and
    versions live behind `server environment`, which needs a token.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_tray_capabilities
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tray_capabilities(client)

    caps = asyncio.run(_run())

    if mode.json:
        print_json(caps)
        return
    ui_features = caps.get("ui_features")
    features_str = ", ".join(ui_features) if isinstance(ui_features, list) else ""
    # `vnc_enabled` says the desktop exists; `vnc_access` says how a browser
    # would reach it, which is the half that differs between installs — a
    # published Docker port, a path on this same origin, or nothing at all until
    # a kubectl tunnel is up. The unauthenticated endpoint carries only that
    # word (the namespaces and ready-to-run commands stay behind `environment`).
    vnc = caps.get("vnc")
    vnc_access = str(vnc.get("access") or "") if isinstance(vnc, dict) else ""
    print_kv([
        ("install_mode", str(caps.get("install_mode") or "")),
        ("supervised", "true" if caps.get("supervised") else "false"),
        ("ui_features", features_str),
        ("deployment", str(caps.get("deployment") or "")),
        ("release_channel", str(caps.get("release_channel") or "")),
        ("vnc_enabled", "true" if caps.get("vnc_enabled") else "false"),
        ("vnc_access", vnc_access),
        ("container", "true" if caps.get("container") else "false"),
    ])


# The install description in the order the Developer page's Environment card
# reads it: what kind of install first, then what it is built from, then where
# it lives. Kept explicit rather than dumping the dict so a new backend field
# can't reorder a user's output from under them.
_ENVIRONMENT_ROWS: tuple[str, ...] = (
    "release_channel",
    "install_mode",
    "deployment",
    "container",
    "image_flavor",
    "vnc_enabled",
    "supervised",
    "electron",
    "backend_version",
    "python_version",
    "os",
    "os_release",
    "app_url",
    "host",
    "system_dir",
    "install_dir",
    # Two different answers, both worth printing: the zone this admin's
    # schedules actually fire in (resolved through the profile's own setting,
    # then the admin's, then the env var, then the OS) and the CREMIND_TIMEZONE
    # boot default on its own, which is blank on most installs.
    "effective_timezone",
    "boot_timezone",
)


# The Kubernetes identity, in the order it answers "where am I?": which cluster
# corner, which release put me here, which objects I am, and the one command
# that reconnects to me. Printed even when a value is blank — an older chart
# states no release, and seeing that row empty is the answer to "why does the
# HTTPS runbook still say <release>?".
_KUBERNETES_ROWS: tuple[str, ...] = (
    "namespace",
    "release",
    "workload",
    "service",
    "service_port",
    "source",
    "port_forward",
)


def _env_cell(value: Any) -> str:
    """Render one environment value: bools as true/false, absent as blank."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _direct_novnc_url(server: str, vnc: dict) -> str:
    """Compose the noVNC URL for a `direct` (Docker) install.

    The server deliberately reports none for this access shape: websockify is
    published on whatever address the operator reaches Docker on, and only the
    client knows which address that was. For a browser that is
    `window.location`; for the CLI it is the server URL this invocation is
    already talking to, which is the same answer arrived at from the other side.

    Always plain http — the published port is websockify's own, and Cremind's
    TLS never covers it (that is what the scheme note says too).
    """
    from urllib.parse import urlsplit

    port = vnc.get("novnc_port")
    path = str(vnc.get("novnc_path") or "")
    host = urlsplit(server).hostname or ""
    if not port or not path or not host:
        return ""
    if ":" in host:  # IPv6 literal — brackets are part of the authority
        host = f"[{host}]"
    return f"http://{host}:{port}{path}"


def _vnc_rows(vnc: dict, server: str) -> list[tuple[str, str]]:
    """Rows describing how to reach the VNC desktop, or none when it is off.

    `port_forward.<n>` lines are the tunnels that have to exist FIRST on
    Kubernetes — noVNC answers on a Service port nothing forwards by default —
    and `open_url` is where to point a browser once one of them is running.
    """
    if not vnc.get("enabled"):
        return []
    rows: list[tuple[str, str]] = [("vnc.access", _env_cell(vnc.get("access")))]
    url = str(vnc.get("novnc_url") or "")
    if not url and vnc.get("access") == "direct":
        url = _direct_novnc_url(server, vnc)
    if url:
        rows.append(("vnc.novnc_url", url))

    open_url = ""
    numbered = 0
    for entry in vnc.get("port_forward_commands") or []:
        if not isinstance(entry, dict):
            continue
        command = str(entry.get("command") or "")
        if not command:
            continue
        numbered += 1
        rows.append((f"vnc.port_forward.{numbered}", command))
        # Every command in a set opens the same page — they differ in how many
        # ports one tunnel carries, not in where you end up.
        open_url = open_url or str(entry.get("open_url") or "")
    if open_url:
        rows.append(("vnc.open_url", open_url))

    note = str(vnc.get("scheme_note") or "")
    if note:
        rows.append(("vnc.note", note))
    return rows


@server_app.command("environment")
@graceful_errors
def server_environment(ctx: typer.Context) -> None:
    """Show what kind of install the server is running in (admin).

    Answers "is this Docker or native?", "is this dev, test or production?",
    "is the VNC desktop there?", "which timezone does the agent schedule in?"
    — the same facts the Developer page's Environment card shows, and the same
    ones the agent carries in its system prompt.

    The timezone answer is two rows: `effective_timezone` is the zone the
    token's profile actually schedules in, and `boot_timezone` is only the
    `CREMIND_TIMEZONE` default it may have been resolved from.

    On Kubernetes the `kubernetes.*` rows name the namespace, Helm release and
    Deployment/Service the pod is, plus the `kubectl port-forward` line that
    reconnects to it; a blank `release` with `source: inferred` means the chart
    predates those variables. The `vnc.*` rows say how a browser reaches the
    desktop — a published Docker port, a path on this same origin, or a tunnel
    that has to be running first.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_server_environment
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_server_environment(client)

    env = asyncio.run(_run())

    if mode.json:
        print_json(env)
        return

    rows = [(key, _env_cell(env.get(key))) for key in _ENVIRONMENT_ROWS if key in env]
    # The custom-deployment answers only exist because the operator gave them,
    # so show them only where they mean something — everywhere else `host` and
    # `app_url` above already say how the server is reached.
    custom = env.get("deployment_custom_fields")
    if env.get("deployment") == "custom" and isinstance(custom, dict):
        rows += [(f"custom.{k}", _env_cell(v)) for k, v in custom.items()]
    # A pod cannot infer any of this — the chart states it — so the block is a
    # dict only on Kubernetes, and `None` everywhere else.
    kubernetes = env.get("kubernetes")
    if isinstance(kubernetes, dict):
        rows += [
            (f"kubernetes.{key}", _env_cell(kubernetes.get(key)))
            for key in _KUBERNETES_ROWS
        ]
    vnc = env.get("vnc")
    if isinstance(vnc, dict):
        rows += _vnc_rows(vnc, cfg.server)
    print_kv(rows)


@server_app.command("restart")
@graceful_errors
def server_restart(
    ctx: typer.Context,
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt.",
    ),
) -> None:
    """Restart the backend process (admin).

    Active HTTP, SSE, and chat connections drop while the server is
    unavailable. Whether it comes back on its own depends on the install
    mode — this command reads it and warns accordingly before confirming.
    """
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.server import get_tray_capabilities, restart_server
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _capabilities() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_tray_capabilities(client)

    caps = asyncio.run(_capabilities())
    install_mode = str(caps.get("install_mode") or "")
    # A native install with a boot service is supervised even though its
    # install_mode says nothing about it, so fall back through `supervised`
    # before assuming nothing will bring the backend back.
    caveat = _RESTART_CAVEAT.get(
        install_mode,
        _RESTART_CAVEAT_SUPERVISED
        if caps.get("supervised")
        else _RESTART_CAVEAT_DEFAULT,
    )

    # Caveat/prompt go to stderr so stdout stays clean for --json.
    if not yes:
        sys.stderr.write(caveat + "\n")
        if not typer.confirm("Restart the Cremind server now?", default=False):
            raise typer.Exit(code=1)

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await restart_server(client)

    result = asyncio.run(_run())

    if mode.json:
        print_json(result)
        return
    pid = string_field(result, "pid")
    sys.stdout.write(f"restarting{f' (pid {pid})' if pid else ''}\n")
