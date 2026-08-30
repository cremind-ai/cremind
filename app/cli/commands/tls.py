"""`cremind tls ...` — trust the CA behind ``CREMIND_SSL=auto``.

Like ``cremind db``, these don't go through the HTTP API: they read a file
under ``<system dir>/tls/`` and hand it to the operating system's trust store.
That is deliberate — the whole point of the command is the case where an HTTP
client *cannot* talk to the server yet, because its certificate is not trusted.
So there is no client wrapper in ``app/cli/client/``; there is nothing to call.

Subcommands:

  tls export       Copy the local CA certificate out (file or stdout).
  tls fingerprint  Show the CA's SHA-256 fingerprint, as browsers display it.
  tls trust        Install the CA into this device's OS trust store.

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
    help="Trust, export, and fingerprint the CREMIND_SSL=auto local CA.",
    no_args_is_help=True,
)

# How long to let a trust-store tool run. update-ca-certificates rehashes the
# whole store, so this is generous; anything slower is hung, not working.
_TOOL_TIMEOUT = 120

_FIREFOX_NOTE = (
    "Firefox keeps its own trust store — import the same file under Settings → "
    "Privacy & Security → Certificates → View Certificates → Authorities."
)


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
            "A local CA is generated at boot when CREMIND_SSL=auto is set. For a "
            "server running elsewhere (Docker, Kubernetes, another host), "
            "download https://<host>:1515/ca.pem and pass it with --file.",
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
