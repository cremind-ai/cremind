"""Server-side CA trust — the wizard's "Trust it on this device" button.

On a *native* install the server process runs on the same machine, in the same
user session, as the browser showing the Setup Wizard. That is the one
topology where "trust the CA on this device" is something the server itself
can do: it hands its own ``tls/ca.pem`` to the OS trust store and the browser
in front of it stops warning. Everywhere else the device that must trust the
CA is out of reach — a Docker container cannot modify the host's store, and a
Kubernetes pod or a remote server cannot modify a visiting laptop — so the
API layer refuses before this module is ever consulted.

This is intentionally NOT ``app/cli/commands/tls.py``'s ``_platform_commands``.
The CLI runs in an interactive terminal and may escalate (``sudo``, the System
keychain); a server process cannot answer a sudo prompt, so the plans here are
strictly non-escalating:

- Windows: ``certutil -addstore -user Root`` — the per-user root store. The
  same command the CLI uses (a test pins them equal). Windows itself shows a
  confirmation dialog in the user's session before the root lands; that
  dialog is the OS-level consent and cannot (and should not) be suppressed
  without elevation.
- macOS: the *login* keychain, not the System keychain — no sudo needed.
  macOS may still show its own password prompt in the GUI session.
- Linux: the system anchor directories need root. Only a server already
  running as root (rare for native installs) gets a plan; everyone else is
  told why and handed the manual commands.

Stdlib-only on purpose (``cryptography`` is imported lazily where a
certificate must be parsed), so importing this costs nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field

# Matches the CLI's timeout: update-ca-certificates rehashes the whole store,
# and on Windows the clock is really the user finding the confirmation dialog.
_TOOL_TIMEOUT = 120

_ANCHOR_NAME = "cremind-local-ca.crt"


@dataclass(frozen=True)
class TrustPlan:
    """What this server process could do to trust ``ca_path`` locally."""

    supported: bool
    #: Human description of the store the plan writes to, for UI copy.
    store: str | None = None
    commands: list[list[str]] = field(default_factory=list)
    #: Why ``supported`` is False, in words the wizard can show.
    reason: str | None = None
    #: OS hint for the UI ("windows" / "macos" / "linux"), so it can warn
    #: about the confirmation dialog / password prompt the OS will show.
    os_prompt: str | None = None


def server_trust_plan(ca_path: str) -> TrustPlan:
    """The non-escalating trust plan for this platform, or why there is none."""
    ca = str(ca_path)
    if sys.platform == "win32":
        return TrustPlan(
            supported=True,
            store="the current user's Trusted Root store",
            # Kept identical to the CLI's Windows command — a test pins it.
            commands=[["certutil", "-addstore", "-user", "Root", ca]],
            os_prompt="windows",
        )
    if sys.platform == "darwin":
        keychain = os.path.expanduser("~/Library/Keychains/login.keychain-db")
        return TrustPlan(
            supported=True,
            store="your login keychain",
            commands=[
                ["security", "add-trusted-cert", "-r", "trustRoot", "-k", keychain, ca]
            ],
            os_prompt="macos",
        )
    if sys.platform.startswith("linux"):
        if not (hasattr(os, "geteuid") and os.geteuid() == 0):
            return TrustPlan(
                supported=False,
                reason=(
                    "Adding a trust anchor on Linux needs root, and this "
                    "server is not running as root — run the command below "
                    "yourself instead."
                ),
            )
        if os.path.isdir("/usr/local/share/ca-certificates"):
            return TrustPlan(
                supported=True,
                store="/usr/local/share/ca-certificates",
                commands=[
                    ["cp", ca, f"/usr/local/share/ca-certificates/{_ANCHOR_NAME}"],
                    ["update-ca-certificates"],
                ],
                os_prompt="linux",
            )
        if os.path.isdir("/etc/pki/ca-trust/source/anchors"):
            return TrustPlan(
                supported=True,
                store="/etc/pki/ca-trust/source/anchors",
                commands=[
                    ["cp", ca, f"/etc/pki/ca-trust/source/anchors/{_ANCHOR_NAME}"],
                    ["update-ca-trust", "extract"],
                ],
                os_prompt="linux",
            )
        return TrustPlan(
            supported=False,
            reason=(
                "No known trust anchor directory on this distribution — "
                "install the CA with whatever it provides."
            ),
        )
    return TrustPlan(
        supported=False,
        reason=f"No known trust-store tool for platform {sys.platform!r}.",
    )


def run_trust_plan(plan: TrustPlan) -> tuple[bool, str | None]:
    """Execute ``plan.commands`` in order. ``(ok, error_detail)``.

    Mirrors the CLI's discipline: every tool is checked for existence up
    front, because the Linux plan is copy-then-rehash and failing between the
    two leaves an anchor in place that the next unrelated rehash would
    silently trust.
    """
    from shutil import which

    for argv in plan.commands:
        if which(argv[0]) is None:
            return False, f"{argv[0]} is not on PATH on the server."

    for argv in plan.commands:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=_TOOL_TIMEOUT
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return False, f"Failed to run {argv[0]}: {e}"
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            # certutil's cancel comes back as a nonzero exit — surface the
            # tool's own words, they name the cancellation clearly.
            return False, (
                f"{argv[0]} exited with {proc.returncode}"
                + (f": {detail}" if detail else "")
            )
    return True, None


def render_command(argv: list[str]) -> str:
    """argv as a copy-pasteable line, for manual-fallback payloads."""
    return " ".join(f'"{a}"' if " " in a else a for a in argv)


def _ca_der(ca_path: str) -> bytes | None:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        with open(ca_path, "rb") as fh:
            cert = x509.load_pem_x509_certificate(fh.read())
    except (OSError, ValueError):
        return None
    return cert.public_bytes(serialization.Encoding.DER)


def already_trusted(ca_path: str) -> bool | None:
    """Whether this machine's store already holds the CA, where knowable.

    Windows exposes the user's Root store to Python directly
    (``ssl.enum_certificates``), so the answer is definitive there — it is
    what lets a re-run of the wizard show "already trusted" instead of
    popping the confirmation dialog again. macOS and Linux have no
    equally cheap, equally honest check (keychain presence does not imply
    trust settings), so they return ``None`` — unknown, not false.
    """
    if sys.platform != "win32":
        return None
    der = _ca_der(ca_path)
    if der is None:
        return None
    import ssl

    try:
        entries = ssl.enum_certificates("ROOT")
    except (OSError, PermissionError):  # pragma: no cover - store access denied
        return None
    return any(cert == der for cert, _encoding, _trust in entries)
