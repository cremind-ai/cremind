"""What TLS this process is serving, and what it will serve next.

``CREMIND_SSL`` has three values:

``""``
    Plain HTTP. What an unset variable means — though not what a fresh
    install gets: the installers default to ``after-setup`` and write it
    into the ``.env`` they render (``--ssl none`` opts out).
``"auto"``
    HTTPS from the first boot, with a certificate signed by a CA generated
    into ``<system dir>/tls/``. Browsers warn until that CA is trusted, and
    the very first page a user opens — the Setup Wizard — is already behind
    the untrusted certificate.
``"after-setup"``
    Plain HTTP until the Setup Wizard completes, then HTTPS. The CA is still
    generated at the first boot, so the wizard can hand it to the user and
    walk them through trusting it *before* any HTTPS page is loaded. On
    completion the wizard restarts the server, which comes back serving TLS
    to a browser that already trusts the chain — no warning, ever.

TLS is bound once, at process start (hypercorn with a certificate, or uvicorn
without), so the "after" in after-setup is a real restart rather than a live
flip. ``bootstrap.toml`` existing is what marks setup as done.

This module is the single source of truth for that decision. ``server`` owns
the boot-time resolution (``_resolve_tls``) because only it can validate paths
and exit; the request handlers that report TLS state to the Setup Wizard need
the same answers but cannot import ``server`` (it imports the API modules).
So the *facts* live here — importable from both, dependency-free, and pinned
against ``_resolve_tls`` by a test that walks the whole matrix.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from app.config.settings import BaseConfig


MODE_AUTO = "auto"
MODE_AFTER_SETUP = "after-setup"
KNOWN_MODES = ("", MODE_AUTO, MODE_AFTER_SETUP)

# Whether this process actually bound TLS. Recorded once by ``server.main``
# from the return of ``_resolve_tls``; see ``record_boot_tls``.
_boot_serving_https: bool = False


def effective_ssl_mode() -> str:
    """``CREMIND_SSL`` normalised for comparison (may be an unknown value)."""
    return BaseConfig.SSL_MODE.strip().lower()


def environment_forces_plain_http(public_port: int) -> bool:
    """True when nothing this process does can result in TLS being served.

    Two deployments own the public origin themselves and terminate TLS in
    front of us: a proxy fronting a loopback-only bind (``CREMIND_UI_PORT=0``),
    and the Electron desktop app (which loads the UI over ``http://127.0.0.1``).
    ``_resolve_tls`` warns and bails for both; the predicate is shared so that
    what the wizard is told matches what the server will do — a server that can
    never serve TLS must never advertise a pending switch to it.
    """
    return public_port == 0 or os.environ.get("CREMIND_ELECTRON_PARENT") is not None


def https_origin_from_app_url(app_url: str) -> str:
    """``app_url`` as an https origin.

    Under after-setup the chart and installers write the *steady-state*
    ``APP_URL`` (https), because that is what it will be for the whole life of
    the install bar the wizard. The scheme swap is here for the case where an
    operator left it http.
    """
    url = (app_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("https://"):
        return url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return "https://" + url


def record_boot_tls(serving: bool) -> None:
    """Record whether this process bound TLS, for later reporting.

    Deliberately a recorded fact rather than something recomputed per request:
    between the wizard writing ``bootstrap.toml`` and the restart landing, a
    recomputation would say "serving https" while this process is still very
    much serving plain HTTP — and the setup response is read in exactly that
    window.
    """
    global _boot_serving_https
    _boot_serving_https = serving


def boot_serving_https() -> bool:
    return _boot_serving_https


@dataclass(frozen=True)
class TlsFacts:
    """What to tell a client about this server's TLS, now and next."""

    mode: str
    serving_https: bool
    pending_https: bool
    restart_supported: bool


def compute_tls_facts(
    *,
    mode: str,
    has_pair: bool,
    public_port: int,
    serving_https: bool,
    install_mode: str,
) -> TlsFacts:
    """Pure core of :func:`current_tls_facts`, parameterised for testing."""
    forced_plain = environment_forces_plain_http(public_port)
    pending = (
        mode == MODE_AFTER_SETUP
        and not has_pair
        and not serving_https
        and not forced_plain
    )
    return TlsFacts(
        # Report what this server *does*, not what was typed at it. An
        # unrecognised CREMIND_SSL is ignored (the server warns at boot and
        # serves plain HTTP), so reporting it verbatim would put a value
        # clients cannot interpret on the wire and describe behaviour that
        # isn't happening.
        mode=mode if mode in KNOWN_MODES else "",
        serving_https=serving_https,
        # Note: NOT gated on bootstrap.toml. The wizard writes it moments
        # before reading this, and the switch is still pending until the
        # restart actually happens.
        pending_https=pending,
        # A restart only comes back where something supervises the process.
        # Docker Compose restarts the container, kubelet restarts the pod; a
        # bare ``cremind serve`` in a terminal simply stays down, so the
        # wizard must ask the operator instead of killing their server.
        restart_supported=install_mode in ("docker", "kubernetes"),
    )


def current_tls_facts(public_port: int | None = None) -> TlsFacts:
    """TLS facts for this running process."""
    if public_port is None:
        public_port = _public_port()
    has_pair = bool(
        (BaseConfig.SSL_CERTFILE or "").strip()
        and (BaseConfig.SSL_KEYFILE or "").strip()
    )
    return compute_tls_facts(
        mode=effective_ssl_mode(),
        has_pair=has_pair,
        public_port=public_port,
        serving_https=boot_serving_https(),
        install_mode=(os.environ.get("INSTALL_MODE") or "").strip().lower(),
    )


def _public_port() -> int:
    """The public bind port, mirroring ``server._resolve_public_port``."""
    raw = (os.environ.get("CREMIND_UI_PORT") or "").strip()
    if not raw:
        return 1515
    try:
        return int(raw)
    except ValueError:
        return 1515
