"""Most-recent browser loopback origin, for the Google OAuth loopback redirect.

The gmail/gcalendar skills must advertise an ABSOLUTE ``redirect_uri`` (Google
has no relative-redirect option), and a Desktop OAuth client only accepts a
loopback host. Under Kubernetes the user reaches Cremind through
``kubectl port-forward <localport>:80``, and the pod cannot know ``<localport>``
— it exists only on the client. But the browser sends it on every request as the
``Host`` header (e.g. ``localhost:8081``), including the chat message that
triggers linking.

This module records the most recent **loopback** origin the backend has seen, so
the redirect can be built to match whatever port the user is actually on — no
chart-fixed ``APP_URL`` required. The chart is single-pod / single-desktop, so
"the origin the user is currently using" is unambiguous.

Set by :class:`app.middleware.RequestOriginRecorder` on every backend HTTP
request; read by :mod:`app.config.system_vars` when it builds the skill
subprocess env. Only consulted in the proxied (Kubernetes) deployment — see the
``CREMIND_OAUTH_REDIRECT_URI`` resolver, which gates on the chart having opted
into the proxied redirect, so Docker/native (direct ``127.0.0.1:<port>/``) are
untouched.
"""
from __future__ import annotations

import threading

# Loopback hostnames a Google Desktop client accepts as a redirect host.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

_lock = threading.Lock()
_origin: str | None = None


def _hostname(host: str) -> str:
    """Bare hostname from a ``Host`` header value (strip ``:port`` / IPv6 ``[]``)."""
    h = host.strip().lower()
    if not h:
        return ""
    if h.startswith("["):  # bracketed IPv6, e.g. [::1]:8081
        end = h.find("]")
        return h[1:end] if end != -1 else ""
    # host[:port] — split a single trailing :port (bare IPv6 has many colons and
    # is only valid bracketed, handled above).
    if h.count(":") == 1:
        h = h.rsplit(":", 1)[0]
    return h


def is_loopback_host(host: str) -> bool:
    return _hostname(host) in _LOOPBACK_HOSTNAMES


def record_origin(host: str) -> None:
    """Record ``http://<host>`` as the current origin when ``<host>`` is loopback.

    Non-loopback hosts (e.g. an Ingress domain) are ignored — a Desktop OAuth
    client can't redirect there anyway, so linking falls back to manual paste.
    Loopback access is always plain http here (the port-forward terminates at the
    in-pod nginx over http), so the scheme is fixed to http.
    """
    if not host or not is_loopback_host(host):
        return
    value = f"http://{host.strip()}"
    global _origin
    with _lock:
        _origin = value


def get_loopback_origin() -> str | None:
    """The most recent loopback origin seen (e.g. ``http://localhost:8081``), or None."""
    with _lock:
        return _origin
