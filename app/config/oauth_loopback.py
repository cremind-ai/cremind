"""The loopback redirect every Google OAuth flow advertises, derived in one place.

All of Cremind's Google flows — the five Google skills, the Calendar & Schedule
connect and the Drive Picker — run under a Google *Desktop* OAuth client. That
client type accepts loopback redirects only (``localhost``, ``127.0.0.1``,
``[::1]``, any port), and only over plain ``http``: the installed-app loopback
flow it implements (RFC 8252 §7.3) is specified over HTTP. A real hostname is
refused with a 400 before the consent screen renders, and an ``https`` loopback
URI fails with ``redirect_uri_mismatch``. So the redirect is always
``http://<loopback>:<port>/...``, whatever scheme the UI itself is served on.

That used to leave HTTPS installs (the installers' default) with no working
redirect. It no longer does: whenever this process terminates TLS on its public
port, the same port also answers plaintext (``app.system.tls_listener``), and
the plaintext recovery surface (``app.api.tls_recovery``) redirects GETs for
exactly the Google callback paths on to their HTTPS handlers. An ``https``
loopback ``APP_URL`` therefore maps to the ``http`` origin on the SAME host and
port — Google sends the browser there, Cremind bounces it to HTTPS, and the
handler receives Google's query untouched.

The resolution order (:func:`google_loopback_origin`):

1. ``APP_URL``'s own loopback origin — ``http`` always; ``https`` only where
   plaintext on that port still reaches this server (not behind edge TLS
   termination, and a public port is bound);
2. a loopback ``CREMIND_OAUTH_REDIRECT_URI`` the operator pinned in the server's
   environment — the address they arranged to be reachable (a port-forward in
   front of an Ingress install), normalised to its ``http`` origin;
3. with ``fallback``: ``http://localhost:<port>`` on APP_URL's explicit port
   (unless that is an ``https`` port step 1 declined as TLS-only), else the
   public bind port, else 1515 (every documented port-forward's port).

Never portless and never a public hostname. Pure apart from the config and
environment reads, which are imported lazily so this stays cheap to import from
the system-var registry.
"""

from __future__ import annotations

import ipaddress
import os
from typing import Literal, Optional, overload
from urllib.parse import urlsplit

#: The port every documented port-forward and the default ``APP_URL`` use.
DEFAULT_PORT = 1515

#: The operator's pin, read from the server's own environment (the skills read
#: the same name from the env the server injects into their subprocess).
PIN_ENV = "CREMIND_OAUTH_REDIRECT_URI"


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_loopback(url: object) -> Optional[tuple[str, str, int]]:
    """``(scheme, host, effective port)`` for a loopback http(s) URL, else None.

    ``host`` comes back bracketed when it is IPv6, ready to put in an origin.
    Anything malformed — a non-numeric or out-of-range port makes
    ``urlsplit(...).port`` raise — reads as "not loopback" rather than raising
    into a caller that is only trying to build a consent URL.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = parts.hostname  # already lower-cased, brackets stripped
        port = parts.port
    except ValueError:
        return None
    if scheme not in ("http", "https") or not host or "%" in host:
        return None  # a zone id is not a browser-usable loopback origin
    if not _is_loopback_host(host):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    elif port == 0:
        return None  # "any port" names nothing a browser can be sent to
    return scheme, (f"[{host}]" if ":" in host else host), port


def loopback_http_origin(url: object) -> Optional[str]:
    """``http://<host>:<port>`` for an http(s) loopback URL, else None.

    The scheme is dropped, the effective port kept — ``https://localhost`` is
    ``http://localhost:443`` — and any path, query or credentials discarded.
    """
    parts = _split_loopback(url)
    if parts is None:
        return None
    _scheme, host, port = parts
    return f"http://{host}:{port}"


def _plaintext_reaches_public_port() -> bool:
    """Whether a plain-HTTP request to the public port lands on this process.

    True for a plain-HTTP bind and for a TLS bind (whose same-port relay hands
    plaintext to the recovery surface). False when an edge proxy owns TLS or no
    public port is bound at all (``CREMIND_UI_PORT=0``): an ``https`` APP_URL
    there names whatever terminates TLS in front of us, which will not speak
    HTTP on that port.
    """
    from app.config.tls_mode import _public_port, edge_tls_termination

    return not edge_tls_termination() and _public_port() != 0


def app_url_loopback_origin() -> Optional[str]:
    """Step 1: APP_URL's own loopback origin as Google's ``http`` redirect origin.

    Also the definition of "the browser can reach the callback": it is set
    exactly when the redirect goes back to the address the user is already
    browsing, which is why the Drive page's local-capture flag reads it.
    """
    from app.config.settings import BaseConfig

    parts = _split_loopback(BaseConfig.APP_URL or "")
    if parts is None:
        return None
    scheme, host, port = parts
    if scheme == "https" and not _plaintext_reaches_public_port():
        return None
    return f"http://{host}:{port}"


def _fallback_port() -> int:
    """APP_URL's explicit port, else the public bind port, else 1515.

    APP_URL's port wins because a remapped publish (Docker ``-p 8080:1515``) is
    reachable there, not on the container's own bind — except for an ``https``
    APP_URL whose port answers only TLS for us (edge termination, or no public
    bind): that is exactly the address step 1 declined, and naming it here would
    send Google's plaintext redirect to a TLS-only listener. ``CREMIND_UI_PORT=0``
    ("serve loopback-only behind an external proxy") names no reachable port, so
    it — like an unparseable value — falls through to 1515.
    """
    from app.config.settings import BaseConfig

    try:
        parts = urlsplit((BaseConfig.APP_URL or "").strip())
        scheme, port = parts.scheme.lower(), parts.port
    except ValueError:
        scheme, port = "", None
    if port and not (scheme == "https" and not _plaintext_reaches_public_port()):
        return port
    try:
        public = int((os.environ.get("CREMIND_UI_PORT") or "").strip())
    except ValueError:
        return DEFAULT_PORT
    return public if public > 0 else DEFAULT_PORT


@overload
def google_loopback_origin(*, fallback: Literal[True]) -> str: ...
@overload
def google_loopback_origin(*, fallback: bool) -> Optional[str]: ...


def google_loopback_origin(*, fallback: bool) -> Optional[str]:
    """The ``http`` loopback origin Google should redirect to (see module doc).

    ``fallback=False`` returns None when neither APP_URL nor a pin names one —
    the skills' system variable is then omitted and each skill applies its own
    default. ``fallback=True`` (Calendar, Drive) always names one, best-effort.
    """
    derived = app_url_loopback_origin()
    if derived:
        return derived
    pinned = loopback_http_origin(os.environ.get(PIN_ENV, ""))
    if pinned:
        return pinned
    if not fallback:
        return None
    return f"http://localhost:{_fallback_port()}"


@overload
def google_redirect_uri(path: str, *, fallback: Literal[True]) -> str: ...
@overload
def google_redirect_uri(path: str, *, fallback: bool) -> Optional[str]: ...


def google_redirect_uri(path: str, *, fallback: bool) -> Optional[str]:
    """:func:`google_loopback_origin` joined with a callback ``path``."""
    base = google_loopback_origin(fallback=fallback)
    return base + path if base else None
