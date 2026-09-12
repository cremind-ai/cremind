"""TLS boot facts shared by the server, installer and HTTPS settings.

New installations default to HTTP (an unset/false CREMIND_SSL). Explicit true
or auto enables a locally signed certificate immediately. Legacy after-setup
installations keep HTTP for the wizard and enable TLS after bootstrap.toml
exists. A later settings-page activation persists true and restarts the public
listener. Electron supports the same public HTTPS listener; the dedicated
internal CLI listener remains plain HTTP. CREMIND_UI_PORT=0 delegates TLS to
an external proxy.

The actual bound transport is recorded once at boot, so a pending restart
cannot make status incorrectly claim the current HTTP listener is HTTPS.
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
    mode = BaseConfig.SSL_MODE.strip().lower()
    if mode in ("true", "1", "yes"):
        return MODE_AUTO
    if mode in ("false", "0", "no", "none"):
        return ""
    return mode


def env_supervised() -> bool:
    """``CREMIND_SUPERVISED`` — something respawns this process when it exits.

    Set by the boot service ``cremind boot enable`` registers (the systemd
    unit's ``Environment=``, the LaunchAgent's ``EnvironmentVariables``, the
    Windows respawn loop), so it is true exactly when a supervisor is really
    watching — a hand-run ``cremind serve`` never sees it.

    Deliberately independent of ``INSTALL_MODE``: it states one fact about the
    process, not how Cremind was installed.
    """
    return (os.environ.get("CREMIND_SUPERVISED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def environment_forces_plain_http(public_port: int) -> bool:
    """A loopback-only deployment delegates its public TLS to an external proxy."""
    return public_port == 0 or edge_tls_termination()


def edge_tls_termination() -> bool:
    """Explicit deployment-owned TLS (for example a Helm Ingress)."""
    return os.environ.get("CREMIND_TLS_TERMINATION", "").strip().lower() == "edge"


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


def app_url_names_internal_bind(internal_port: int, public_port: int | None = None) -> bool:
    """Whether ``APP_URL`` points at the port nothing outside can reach.

    The internal bind (``PORT``) listens on 127.0.0.1 only and is never
    published, so in a container it is the container's own loopback. An
    ``APP_URL`` naming it is an address no browser can open.

    Shared by the boot warning and the HTTPS switch so the two can never
    disagree about what counts as wrong. Boot only warns — the value may have
    been wrong for months and refusing to start would be worse. The switch
    *derives* the new HTTPS origin from this value, so it would otherwise bake
    the unreachable address into the agent card, the OAuth redirects and the
    Atlassian callback: where Cremind owns the setting it repairs it instead
    (:func:`public_app_url`), and where it does not — a deployment that keeps
    its own environment — it refuses.
    """
    from urllib.parse import urlsplit

    if public_port is None:
        public_port = _public_port()
    if not public_port or public_port == internal_port:
        return False  # no public bind, or the two are the same: nothing to confuse
    try:
        return urlsplit((BaseConfig.APP_URL or "").strip()).port == internal_port
    except ValueError:
        return False


def public_app_url(internal_port: int | None = None) -> str:
    """``APP_URL`` as an address a browser can actually open.

    The value is usually returned untouched. Two configurations cannot be used
    as they stand, and both are somebody else's mistake rather than this
    operator's:

    * It names the internal bind. Cremind's own v0.0.1 Docker installer wrote
      ``http://localhost:1112`` while that port was still published; the port
      map went away three releases later and nothing rewrites a ``.env`` on
      upgrade, so installs from that era still carry it.
    * It is empty or unparseable — Compose substitutes ``${APP_URL}`` with the
      empty string when the key is missing from the ``.env`` beside
      ``docker-compose.yml``.

    The repair keeps the scheme and the hostname, which are deliberate installer
    choices (``localhost`` for a local install, the operator's host for a server
    one), and moves only the port onto the public bind — which is exactly what
    every installer since has written. An empty value falls back to the same
    ``http://localhost:<public port>`` that :class:`BaseConfig` defaults to when
    the variable is absent altogether.

    Returns ``BaseConfig.APP_URL`` itself when nothing needs repairing, so a
    caller can detect one with ``!=`` and report what it changed.
    """
    from urllib.parse import urlsplit

    public = _public_port()
    if not public:
        # No public bind of our own: the address belongs to whatever is in
        # front of us, and naming the internal port may even be correct (the
        # dev loop in CONTRIBUTING.md does exactly that).
        return BaseConfig.APP_URL
    try:
        parsed = urlsplit((BaseConfig.APP_URL or "").strip())
        scheme, host = parsed.scheme, parsed.hostname
        _ = parsed.port  # raises on a non-numeric port
    except ValueError:
        scheme = host = None
    if not host or scheme not in ("http", "https"):
        return f"http://localhost:{public}"
    if not app_url_names_internal_bind(
            BaseConfig.PORT if internal_port is None else internal_port, public):
        return BaseConfig.APP_URL
    return f"{scheme}://{f'[{host}]' if ':' in host else host}:{public}"


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
    supervised: bool = False,
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
        # Docker Compose restarts the container, kubelet restarts the pod, and
        # on a native install the boot service from ``cremind boot enable``
        # does (it sets CREMIND_SUPERVISED). A bare ``cremind serve`` in a
        # terminal simply stays down, so the wizard must ask the operator
        # instead of killing their server.
        restart_supported=supervised or install_mode in ("docker", "kubernetes")
        or os.environ.get("CREMIND_ELECTRON_PARENT") is not None,
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
        supervised=env_supervised(),
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
