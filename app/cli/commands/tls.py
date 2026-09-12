"""`cremind tls ...` — enable HTTPS and manage the device's CA trust.

Status, prepare, enable and cancel use the running server through
``app.cli.client.tls``. Administrative changes require the admin token.
Trust, export and fingerprint instead read a certificate under
``<system dir>/tls/`` and remain usable offline without a session. That local
path matters when an HTTPS client cannot reach the server yet because its
certificate is not trusted.

Subcommands:

  tls export       Copy the local CA certificate out (file or stdout).
  tls fingerprint  Show the CA's SHA-256 fingerprint, as browsers display it.
  tls trust        Install the CA into this device's OS trust store.
  tls status       Inspect current HTTPS state and the deployment steps (commands indented).
  tls prepare      Prepare a certificate and an authenticated transition.
  tls enable       Activate HTTPS, optionally restarting supervised native installs.
  tls cancel       Cancel a switch whose HTTPS listener has not started yet.

``trust`` is a one-off per device. A certificate is trusted because it chains
to a root the *device* already has, so nothing the server does can skip this
step; the CA exists so that it only ever happens once, and re-issued server
certificates (on expiry, or when a hostname is added) stay trusted under it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

import typer


tls_app = typer.Typer(
    name="tls",
    help="Enable HTTPS, inspect its status, and trust or export the local CA.",
    no_args_is_help=True,
)

# How long to let a trust-store tool run. update-ca-certificates rehashes the
# whole store, so this is generous; anything slower is hung, not working.
_TOOL_TIMEOUT = 120

_FIREFOX_NOTE = (
    "If Firefox still warns after OS trust, import the same file under Settings → "
    "Privacy & Security → Certificates → View Certificates → Authorities."
)

# The server groups its runbook by position: what to edit, what to run, what to
# expect. Headings appear only when there is more than one group to separate.
_STEP_HEADINGS = ("Before you start", "Run in order", "What to expect")
# Four spaces, not a "$ " prompt: selecting the line in a terminal must yield a
# command that runs as-is.
_COMMAND_INDENT = "    "


def _print_steps(steps: list) -> None:
    """Render the server's ``steps`` so commands stand out from prose.

    Notes print flush-left, commands indented. Grouping is derived from where
    the commands sit, so the wire format stays two fields wide.
    """
    items = [
        step for step in steps
        if isinstance(step, dict) and str(step.get("text") or "").strip()
    ]
    command_positions = [
        index for index, step in enumerate(items) if step.get("kind") == "command"
    ]
    if command_positions:
        first, last = command_positions[0], command_positions[-1]
        groups = [items[:first], items[first:last + 1], items[last + 1:]]
    else:
        groups = [items, [], []]
    labelled = [
        (heading, group)
        for heading, group in zip(_STEP_HEADINGS, groups) if group
    ]
    show_headings = len(labelled) > 1
    for heading, group in labelled:
        if show_headings:
            typer.echo(f"{heading}:")
        for step in group:
            text = str(step["text"]).strip()
            if step.get("kind") == "command":
                typer.echo(f"{_COMMAND_INDENT}{text}")
            else:
                typer.echo(text)


def _switch_outcome(transition: dict) -> list[str]:
    """What became of a switch that finished, or is running out of time.

    Two facts the operator has no other way to learn. A switch Cremind applied
    itself waits for a browser to reach the new address and undoes itself if
    none does, so a status call made in that window has to say how long is
    left — otherwise "activating" reads as "forever". And once it *has* undone
    itself the installation is back on HTTP with nothing in the phase to
    explain why, which looks indistinguishable from a switch that was never
    started.

    Both fields come off the wire (``app/cli/`` must not import server code),
    and both are absent on an older server, where this prints nothing.
    """
    import time

    lines: list[str] = []
    reverted = transition.get("auto_reverted")
    if isinstance(reverted, dict) and reverted.get("reason"):
        lines.append(f"HTTPS was switched off automatically: {reverted['reason']}")
        if reverted.get("restored") is False:
            lines.append(
                "The previous settings could NOT be restored — check CREMIND_SSL "
                "and APP_URL in the system directory's .env before restarting."
            )
    deadline = transition.get("confirmation_deadline")
    if (isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
            and transition.get("phase") == "activating"):
        remaining = int(deadline - time.time())
        lines.append(
            f"Waiting for a browser to reach the HTTPS address ({remaining // 60}m "
            f"{remaining % 60}s left). If none does, the previous settings are "
            "restored automatically and this server returns to HTTP."
            if remaining > 0 else
            "The confirmation deadline has passed; the previous settings are "
            "being restored and this server will return to HTTP."
        )
    return lines


def _remote_tls(ctx: typer.Context, action: str, source_origin: str | None = None,
                *, restart: bool = False) -> None:
    import asyncio
    from app.cli.client._base import Client
    from app.cli.client import tls as tls_client
    from app.cli.commands._helpers import graceful_errors
    from app.cli.output import print_json, print_kv

    @graceful_errors
    def run():
        cfg = ctx.obj["cfg"]
        if action != "status":
            cfg.require_token()

        async def call():
            async with Client(cfg) as client:
                if action == "status":
                    return await tls_client.status(client)
                if action == "prepare":
                    return await tls_client.prepare(client, source_origin)
                current = await tls_client.status(client)
                transition = current.get("transition") or {}
                if not transition.get("id"):
                    from app.cli.config import ConfigError
                    raise ConfigError("Run cremind tls prepare, then trust the CA before enabling HTTPS.")
                if action == "cancel":
                    return await tls_client.cancel(client, transition["id"])
                result = await tls_client.activate(
                    client,
                    transition["id"],
                    current.get("certificate_sha256") or current.get("ca_sha256"),
                    restart=restart,
                )
                if result.get("restart_scheduled"):
                    result["restart_requested"] = True
                return result

        result = asyncio.run(call())
        if ctx.obj["mode"].json:
            print_json(result)
        else:
            print_kv([(key, str(result.get(key, ""))) for key in (
                "serving_https", "https_url", "management", "certificate_kind", "certificate_sha256", "restart_supported")])
            if result.get("certificate_error"):
                typer.echo(result["certificate_error"])
            if result.get("restart_error"):
                typer.echo(result["restart_error"])
            if result.get("activation_error"):
                typer.echo(result["activation_error"])
            if result.get("revert_error"):
                typer.echo(result["revert_error"])
            for line in _switch_outcome(result.get("transition") or {}):
                typer.echo(line)
            steps = result.get("steps")
            if isinstance(steps, list) and steps:
                _print_steps(steps)
            else:
                # A server older than the note/command split publishes only the
                # flat list; restart_error was already echoed above.
                for instruction in result.get("instructions") or []:
                    if instruction != result.get("restart_error"):
                        typer.echo(instruction)
            if result.get("restart_required") and not result.get("restart_requested"):
                typer.echo("HTTPS is prepared. Restart through the desktop app or follow the deployment instructions above.")
            # Until HTTPS actually answers, staying on HTTP is still an option —
            # and the operator has no other way of knowing that.
            if (result.get("can_cancel")
                    and (result.get("transition") or {}).get("phase") == "activating"):
                waiting_on = (
                    "This switch is waiting for a browser to reach the HTTPS address."
                    if (result.get("transition") or {}).get("confirmation_deadline")
                    else "This switch is waiting for the deployment change."
                )
                typer.echo(
                    f"{waiting_on} To stay on HTTP instead: "
                    "cremind --profile admin tls cancel"
                )
    run()


@tls_app.command("status")
def tls_status(ctx: typer.Context) -> None:
    """Show HTTP/HTTPS status, certificate fingerprint and deployment steps."""
    _remote_tls(ctx, "status")


@tls_app.command("prepare")
def tls_prepare(ctx: typer.Context, source_origin: Optional[str] = typer.Option(
        None, "--source-origin", help="The HTTP browser origin (not the internal CLI port).")) -> None:
    """Generate the certificate and prepare a switch; requires the admin profile."""
    _remote_tls(ctx, "prepare", source_origin)


@tls_app.command("enable")
def tls_enable(ctx: typer.Context, yes: bool = typer.Option(False, "--yes", "-y"),
               restart: bool = typer.Option(True, "--restart/--no-restart")) -> None:
    """Activate prepared HTTPS and restart a supervised native install."""
    if not yes and not typer.confirm("Have you verified and trusted the certificate issuer on each device and saved your work? Enable HTTPS now?", default=False):
        raise typer.Exit(1)
    _remote_tls(ctx, "enable", restart=restart)


@tls_app.command("cancel")
def tls_cancel(
    ctx: typer.Context,
    local: bool = typer.Option(
        False,
        "--local",
        help="Cancel in the system directory directly, for when no server is running.",
    ),
) -> None:
    """Cancel a switch whose HTTPS listener has not started yet.

    ``--local`` is the way back when activation persisted and the restart into
    HTTPS then failed: there is no server left to ask, but nothing has been
    invalidated either, so restoring the previous settings is a complete undo.
    It refuses once HTTPS has served — at that point sessions are already tied
    to the new transport and only a deployment change can go back.
    """
    if not local:
        _remote_tls(ctx, "cancel")
        return

    from app.cli.output import print_json, print_kv

    # Imported in-body, like every other server-side import in this package: the
    # slim `pip install cremind` must not pull server config in to run a command
    # that does not need it.
    from app.config.tls_transition import cancel_locally

    try:
        result = cancel_locally()
    except (ValueError, OSError) as error:
        typer.secho(str(error), fg="red", err=True)
        raise typer.Exit(1) from None
    if ctx.obj["mode"].json:
        print_json(result)
        return
    print_kv([
        ("Phase", result["phase"]),
        ("Previous settings restored", "yes" if result["reverted"] else "not needed"),
    ])
    typer.echo("The HTTPS switch was cancelled. Start Cremind again to serve HTTP.")


def _default_ca_path() -> Path:
    """``<system dir>/tls/ca.pem``.

    The layout is duplicated from ``app.config.tls_auto._paths`` rather than
    imported, for the reason ``app.cli.session.system_dir`` documents: the slim
    ``pip install cremind`` must not pull in server config to run a CLI command.
    Keep the two in step if that layout ever changes.
    """
    from app.cli import session

    return session.system_dir() / "tls" / "ca.pem"


def _read_ca(path: Path):
    """Load ``path`` as an X.509 CA certificate, or exit with a clear message."""
    from cryptography import x509

    if not path.is_file():
        typer.secho(f"No CA certificate at {path}", fg="red", err=True)
        typer.secho(
            "A local CA is generated at boot when CREMIND_SSL is set to auto or "
            "after-setup. For a server running elsewhere (Docker, Kubernetes, "
            "another host), download <host>:1515/ca.pem and pass it with --file.",
            err=True,
        )
        raise typer.Exit(1)
    try:
        cert = x509.load_pem_x509_certificate(path.read_bytes())
    except Exception as e:  # noqa: BLE001 - any unreadable file is a user error here
        typer.secho(f"{path} is not a PEM certificate: {e}", fg="red", err=True)
        raise typer.Exit(1) from None
    return cert


def _assert_is_ca(cert, path: Path) -> None:
    """Refuse anything that is not a CA.

    Installing a leaf certificate as a trusted root is the mistake worth
    preventing: the file is about to be given root authority on this device,
    and ``--file`` accepts arbitrary paths.
    """
    from cryptography import x509

    try:
        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        basic = None
    if basic is None or not basic.ca:
        typer.secho(
            f"Refusing to install {path}: it is not a CA certificate "
            "(BasicConstraints CA is not true). Point --file at ca.pem, not at "
            "the server certificate.",
            fg="red",
            err=True,
        )
        raise typer.Exit(1)


def _sha256_fingerprint(cert) -> str:
    """Colon-separated uppercase hex over the DER — what cert viewers show."""
    import hashlib
    from cryptography.hazmat.primitives import serialization

    der = cert.public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _common_name(cert) -> str:
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    try:
        return cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    except (IndexError, x509.ExtensionNotFound):  # pragma: no cover - defensive
        return ""


class _UnsupportedPlatform(Exception):
    """No known trust-store command for this OS/distribution."""


def _platform_commands(ca_path: Path) -> tuple[str, list[list[str]]]:
    """``(store description, [argv, ...])`` to install ``ca_path`` here.

    Pure: it decides, it does not run anything. ``trust --print-only`` shows
    exactly what this returns, so the printed commands and the executed ones
    can never drift apart.
    """
    ca = str(ca_path)
    if sys.platform == "win32":
        # The per-user root store: no elevated shell needed. Windows shows a
        # confirmation dialog before it lands.
        return "the current user's Trusted Root store", [
            ["certutil", "-addstore", "-user", "Root", ca]
        ]

    # Running as root (in a container, say) means sudo is both unnecessary and
    # often absent.
    sudo: list[str] = []
    if not (hasattr(os, "geteuid") and os.geteuid() == 0):
        sudo = ["sudo"]

    if sys.platform == "darwin":
        return "the System keychain", [
            sudo + [
                "security", "add-trusted-cert", "-d", "-r", "trustRoot",
                "-k", "/Library/Keychains/System.keychain", ca,
            ]
        ]

    # Linux: the two families put anchors in different places and rehash with
    # different tools. Detect by which one is actually present.
    anchor = "cremind-local-ca.crt"
    if os.path.isdir("/usr/local/share/ca-certificates"):
        return "/usr/local/share/ca-certificates", [
            sudo + ["cp", ca, f"/usr/local/share/ca-certificates/{anchor}"],
            sudo + ["update-ca-certificates"],
        ]
    if os.path.isdir("/etc/pki/ca-trust/source/anchors"):
        return "/etc/pki/ca-trust/source/anchors", [
            sudo + ["cp", ca, f"/etc/pki/ca-trust/source/anchors/{anchor}"],
            sudo + ["update-ca-trust", "extract"],
        ]
    raise _UnsupportedPlatform(
        "Could not find a system trust anchor directory "
        "(/usr/local/share/ca-certificates or /etc/pki/ca-trust/source/anchors). "
        "Install the CA using whatever your distribution provides:\n"
        f"  Debian/Ubuntu: sudo cp {ca} /usr/local/share/ca-certificates/{anchor} "
        "&& sudo update-ca-certificates\n"
        f"  RHEL/Fedora:   sudo cp {ca} /etc/pki/ca-trust/source/anchors/{anchor} "
        "&& sudo update-ca-trust extract"
    )


def _quote(argv: list[str]) -> str:
    """Render argv as a copy-pasteable command line."""
    parts = []
    for arg in argv:
        parts.append(f'"{arg}"' if " " in arg else arg)
    return " ".join(parts)


@tls_app.command("export")
def tls_export(
    out: str = typer.Option(
        "cremind-local-ca.pem",
        "--out",
        "-o",
        help="Where to write the CA. Use '-' for stdout.",
    ),
    file: Optional[str] = typer.Option(
        None,
        "--file",
        help="CA to export (default: <CREMIND_SYSTEM_DIR>/tls/ca.pem).",
    ),
) -> None:
    """Copy the local CA certificate out, to hand to another device."""
    src = Path(file) if file else _default_ca_path()
    cert = _read_ca(src)
    pem = src.read_bytes()
    if out == "-":
        # Bytes, not typer.echo: on PowerShell a redirected text write is
        # re-encoded to UTF-16 and the PEM stops parsing.
        sys.stdout.buffer.write(pem)
        return
    dest = Path(out)
    try:
        dest.write_bytes(pem)
    except OSError as e:
        typer.secho(f"Could not write {dest}: {e}", fg="red", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"Wrote {dest} ({_common_name(cert) or 'CA certificate'})")


@tls_app.command("fingerprint")
def tls_fingerprint(
    ctx: typer.Context,
    file: Optional[str] = typer.Option(
        None,
        "--file",
        help="CA to inspect (default: <CREMIND_SYSTEM_DIR>/tls/ca.pem).",
    ),
) -> None:
    """Show the CA's SHA-256 fingerprint, so you can match it in a browser."""
    from app.cli.output import print_json, print_kv

    path = Path(file) if file else _default_ca_path()
    cert = _read_ca(path)
    fingerprint = _sha256_fingerprint(cert)
    mode = ctx.obj["mode"]
    if mode.json:
        print_json({
            "path": str(path),
            "subject": _common_name(cert),
            "sha256": fingerprint,
            "not_valid_after": cert.not_valid_after_utc.isoformat(),
        })
        return
    print_kv([
        ("Subject", _common_name(cert)),
        ("SHA-256", fingerprint),
        ("Expires", cert.not_valid_after_utc.strftime("%Y-%m-%d")),
        ("Path", str(path)),
    ])


@tls_app.command("trust")
def tls_trust(
    ctx: typer.Context,
    file: Optional[str] = typer.Option(
        None,
        "--file",
        help="CA to install, e.g. one downloaded from https://<host>/ca.pem "
             "(default: <CREMIND_SYSTEM_DIR>/tls/ca.pem).",
    ),
    print_only: bool = typer.Option(
        False,
        "--print-only",
        help="Print the command(s) for this OS without running anything.",
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the confirmation prompt.",
    ),
) -> None:
    """Install the local CA into this device's trust store, so browsers stop warning."""
    from app.cli.output import print_json

    mode = ctx.obj["mode"]
    path = (Path(file) if file else _default_ca_path()).resolve()
    cert = _read_ca(path)
    _assert_is_ca(cert, path)

    try:
        store, commands = _platform_commands(path)
    except _UnsupportedPlatform as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(1) from None

    if print_only:
        if mode.json:
            print_json({"store": store, "commands": commands})
        else:
            for argv in commands:
                typer.echo(_quote(argv))
        return

    fingerprint = _sha256_fingerprint(cert)
    subject = _common_name(cert) or "(no common name)"
    if mode.json and not yes:
        typer.secho(
            "--json needs --yes (or --print-only): installing a trusted root "
            "asks for confirmation, which has no meaning in JSON mode.",
            fg="red",
            err=True,
        )
        raise typer.Exit(1)

    if not yes:
        typer.echo(f"About to trust this certificate as a root authority in {store}:")
        typer.echo(f"  Subject  {subject}")
        typer.echo(f"  SHA-256  {fingerprint}")
        typer.echo(f"  File     {path}")
        if sys.platform == "win32":
            typer.echo("Windows will show its own confirmation dialog as well.")
        if not typer.confirm("Continue?", default=False):
            raise typer.Exit(1)

    from shutil import which

    # Check every tool up front. The Linux path is two commands — copy the
    # anchor, then rehash the store — and a missing tool discovered between
    # them would abort with the anchor already in place, which the next
    # unrelated `update-ca-certificates` run would silently pick up and trust.
    for argv in commands:
        if which(argv[0]) is None:
            typer.secho(f"Cannot run {argv[0]}: it is not on PATH.", fg="red", err=True)
            _print_manual(commands)
            raise typer.Exit(1)

    for index, argv in enumerate(commands):
        tool = argv[0]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=_TOOL_TIMEOUT,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            typer.secho(f"Failed to run {_quote(argv)}: {e}", fg="red", err=True)
            _fail_midway(commands, index)
            raise typer.Exit(1) from None
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            typer.secho(
                f"{tool} failed (exit {proc.returncode})"
                + (f": {detail}" if detail else ""),
                fg="red",
                err=True,
            )
            _fail_midway(commands, index)
            raise typer.Exit(1)

    if mode.json:
        print_json({
            "trusted": True,
            "store": store,
            "subject": subject,
            "sha256": fingerprint,
            "path": str(path),
        })
        return
    typer.secho(f"Trusted {subject} in {store}.", fg="green")
    typer.echo("Reload Cremind in the browser — the warning should be gone.")
    typer.echo(_FIREFOX_NOTE)


def _print_manual(commands: list[list[str]]) -> None:
    """Show the exact command(s) to finish by hand after an automated failure."""
    typer.secho("Run this yourself to finish:", err=True)
    for argv in commands:
        typer.secho(f"  {_quote(argv)}", err=True)


def _fail_midway(commands: list[list[str]], failed_at: int) -> None:
    """Report a failure that may have left earlier steps applied.

    On Linux, trusting is a copy followed by a rehash. If the copy landed and
    the rehash did not, the CA is not trusted *yet* — but the anchor is sitting
    where the next unrelated ``update-ca-certificates`` will pick it up. Saying
    "nothing happened" would be a lie the user acts on, so name what ran.
    """
    if failed_at > 0:
        typer.secho(
            "This step ran before the failure and was NOT undone:", err=True
        )
        for argv in commands[:failed_at]:
            typer.secho(f"  {_quote(argv)}", err=True)
        typer.secho(
            "Either finish with the command(s) below, or undo the above — until "
            "one or the other, the certificate may be trusted by a later "
            "unrelated refresh of the store.",
            err=True,
        )
    _print_manual(commands[failed_at:])
