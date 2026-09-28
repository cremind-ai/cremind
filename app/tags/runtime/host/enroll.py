"""This computer as a gateway computer of a Cremind server elsewhere.

A Cremind in a container or on another machine cannot see the USB ports of
the computer a gateway plugs into. The person starts "Set up a gateway
computer" on the Cremind page; its link, ``cremind://tags/setup?…``, reaches
this computer (the Cremind app's link handler, or ``cremind tags host
enroll``), and :func:`enroll`:

1. **binds** the setup session with this computer's installation key
   (``remote/installation.key``, owner-only); Cremind answers the profile,
   its origin and four words it also shows on the page;
2. asks the person to **approve** here (``approve(bound)``: the Cremind app's
   dialog, or the terminal) after comparing the words;
3. waits until the page **confirms** the same words (the same sign-in that
   started it);
4. **redeems** with the SHA-256 of a fresh host credential secret; Cremind
   answers the host id and the credential id — the secret never leaves this
   computer;
5. **saves** ``remote/enrollment.json`` (public) and
   ``remote/enrollment.key`` (owner-only), and the server's CA when the link
   pinned one.

The credential works only on ``/api/tag-host/v1`` for this computer and that
profile. The link's token is a credential too: it is never logged or kept.
A crash between redeem and save loses nothing: the secret and the redeem key
are staged first (``remote/enrollment.pending``, owner-only) and reused when
the same link is opened again.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from ..connect.links import _PIN, _TOKEN, MAX_LINK_LEN, SUPPORTED_VERSION, LinkError, SetupLink, validate_origin

log = logging.getLogger(__name__)

LINK_SCHEME = "cremind"
LINK_HOST = "tags"
LINK_PATH = "/setup"
ENROLLMENT_SCHEMA = "cremind/tag-host-enrollment@1"
KEY_SCHEMA = "cremind/tag-host-credential@1"
POLL_EVERY_S = 1.0
TIMEOUT_S = 330.0  # a setup session lives five minutes


class EnrollError(RuntimeError):
    """Enrolling did not finish. ``code`` for programs; the message is written for a person."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Enrollment:
    """This computer's enrollment (the secret stays inside; ``repr`` never shows it)."""

    server: str
    host_id: str
    credential_id: str
    secret: str
    profile: str
    profile_id: str
    host_name: str
    server_installation_id: str
    enrolled_at: str
    ca_pem: str | None = None

    def __repr__(self) -> str:
        return f"Enrollment(server={self.server!r}, host_id={self.host_id!r}, profile={self.profile!r})"

    @property
    def authorization(self) -> str:
        return f"CremindHost {self.credential_id}.{self.secret}"

    def public_json(self) -> dict[str, Any]:
        return {"schema": ENROLLMENT_SCHEMA, "server": self.server, "host_id": self.host_id,
                "credential_id": self.credential_id, "profile": self.profile, "profile_id": self.profile_id,
                "host_name": self.host_name, "server_installation_id": self.server_installation_id,
                "enrolled_at": self.enrolled_at, "ca_pinned": self.ca_pem is not None}


# ---------------------------------------------------------------------------
# The link
# ---------------------------------------------------------------------------


def parse_host_link(url: str) -> SetupLink:
    """Validate ``cremind://tags/setup?v=1&server=…&session=…&token=…[&pin=…]`` (the rules of
    :func:`~app.tags.runtime.connect.links.parse_setup_link`: exact action, one of each parameter, a bare
    ``http(s)`` origin, a UUID session, a URL-safe token, a pin only with ``https``)."""
    if not isinstance(url, str) or not url.strip():
        raise LinkError("empty", "No link was given.")
    url = url.strip()
    if len(url) > MAX_LINK_LEN:
        raise LinkError("too_long", "The link is too long to be a Cremind setup link.")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise LinkError("malformed", "The link contains control characters.")
    parts = urlsplit(url)
    if parts.scheme.lower() != LINK_SCHEME:
        raise LinkError("wrong_scheme", "This is not a Cremind link.")
    if parts.netloc.lower() != LINK_HOST or parts.path != LINK_PATH:
        raise LinkError("unknown_action", "This Cremind link asks for something this version cannot do.")
    if parts.fragment:
        raise LinkError("malformed", "The link has an unexpected '#' part.")
    try:
        pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise LinkError("malformed", "The link's parameters are not readable.") from None
    params: dict[str, str] = {}
    for key, value in pairs:
        if key in params:
            raise LinkError("malformed", f"The link gives '{key}' more than once.")
        params[key] = value
    if params.get("v") != SUPPORTED_VERSION:
        raise LinkError("unsupported_version", "This link needs a newer Cremind. Update Cremind and try again.")
    server = validate_origin(params.get("server", ""))
    try:
        session = str(uuid.UUID(params.get("session", "")))
    except ValueError:
        raise LinkError("bad_session", "The link's setup session is not valid.") from None
    if params.get("session", "").lower() != session:
        raise LinkError("bad_session", "The link's setup session is not valid.")
    token = params.get("token", "")
    if not _TOKEN.fullmatch(token):
        raise LinkError("bad_token", "The link's setup code is missing or damaged. Start again from Cremind.")
    pin = params.get("pin")
    if pin is not None:
        if not _PIN.fullmatch(pin) or not server.startswith("https://"):
            raise LinkError("bad_pin", "The link's certificate fingerprint is not valid.")
        pin = pin.lower()
    return SetupLink(1, server, session, token, pin)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def _write_private(path: Path, text: str) -> None:
    from ..connect.installation import _write_private as write

    write(path, text)


def _write_public(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_enrollment(paths: Any, enrollment: Enrollment) -> None:
    """``enrollment.key`` first (owner-only), then ``ca.pem``, then ``enrollment.json`` (what marks it done)."""
    _write_private(paths.enrollment_key, json.dumps({"schema": KEY_SCHEMA, "credential_id": enrollment.credential_id,
                                                     "secret": enrollment.secret}) + "\n")
    ca_path = paths.remote_dir / "ca.pem"
    if enrollment.ca_pem:
        _write_public(ca_path, enrollment.ca_pem)
    else:
        ca_path.unlink(missing_ok=True)
    _write_public(paths.enrollment_file, json.dumps(enrollment.public_json(), indent=2) + "\n")


def load_enrollment(paths: Any) -> Enrollment | None:
    """This computer's enrollment, or ``None`` (never enrolled, forgotten, or incomplete files)."""
    try:
        public = json.loads(paths.enrollment_file.read_text(encoding="utf-8"))
        private = json.loads(paths.enrollment_key.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise EnrollError("unreadable", f"This computer's enrollment cannot be read ({exc}).") from None
    if not isinstance(public, dict) or public.get("schema") != ENROLLMENT_SCHEMA or not isinstance(private, dict) \
            or private.get("credential_id") != public.get("credential_id") or not private.get("secret"):
        raise EnrollError("unreadable", "This computer's enrollment files do not match; enroll it again.")
    ca_pem = None
    if public.get("ca_pinned"):
        try:
            ca_pem = (paths.remote_dir / "ca.pem").read_text(encoding="ascii")
        except OSError as exc:
            raise EnrollError("unreadable", f"The server's certificate authority is missing ({exc}).") from None
    return Enrollment(server=str(public["server"]), host_id=str(public["host_id"]),
                      credential_id=str(public["credential_id"]), secret=str(private["secret"]),
                      profile=str(public.get("profile") or ""), profile_id=str(public.get("profile_id") or ""),
                      host_name=str(public.get("host_name") or ""),
                      server_installation_id=str(public.get("server_installation_id") or ""),
                      enrolled_at=str(public.get("enrolled_at") or ""), ca_pem=ca_pem)


def forget_enrollment(paths: Any) -> None:
    """Delete this computer's enrollment files (the workers' own directories stay)."""
    for path in (paths.enrollment_file, paths.enrollment_key, paths.remote_dir / "ca.pem",
                 paths.remote_dir / "enrollment.pending"):
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _pending(paths: Any, session_id: str) -> tuple[str, str]:
    """``(redeem key, secret)`` for this session, staged owner-only before redeem."""
    path = paths.remote_dir / "enrollment.pending"
    with contextlib.suppress(OSError, ValueError, KeyError, TypeError):
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc["session"] == session_id:
            return str(doc["idempotency_key"]), str(doc["secret"])
    key, secret = secrets.token_hex(16), secrets.token_urlsafe(32)
    _write_private(path, json.dumps({"session": session_id, "idempotency_key": key, "secret": secret}) + "\n")
    return key, secret


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


def enroll(link_url: str, paths: Any, *, approve: Callable[[Any], bool],
           on_event: Callable[[str, dict[str, Any]], None] | None = None, version: str = "",
           transport: Any = None, poll_every_s: float = POLL_EVERY_S, timeout_s: float = TIMEOUT_S,
           computer: str | None = None) -> Enrollment:
    """Enroll this computer (see the module docstring); blocking. ``approve(bound)`` gets the
    :class:`~app.tags.runtime.connect.bootstrap_client.Bound` answer (profile, words, server) and says whether the
    person approved. ``on_event(name, fields)`` reports each step (``bound``, ``approved``, ``confirmed``,
    ``enrolled``) — nothing secret in it."""
    from ..connect.bootstrap_client import BootstrapClient, BootstrapError
    from ..connect.installation import computer_name, load_or_create, platform_name

    report = on_event or (lambda _name, _fields: None)
    link = parse_host_link(link_url)
    paths.remote_dir.mkdir(parents=True, exist_ok=True)
    installation = load_or_create(paths)
    name = (computer or computer_name())[:255]
    try:
        with BootstrapClient(link, installation, transport=transport) as client:
            bound = client.bind(computer=name, platform=platform_name(), version=version)
            if bound.operation != "enroll_host":
                raise EnrollError("wrong_operation", "This link is not for setting up a gateway computer.")
            report("bound", {"server": link.server, "profile": bound.profile_name, "computer": name,
                             "words": bound.verification_phrase, "state": bound.state})
            if bound.state not in ("waiting_for_approval", "waiting_for_confirmation", "redeeming"):
                raise EnrollError("wrong_state", f"This setup is {bound.state.replace('_', ' ')}. Start again.")
            if bound.state == "waiting_for_approval":
                if not approve(bound):
                    with contextlib.suppress(BootstrapError):
                        client.fail("declined", "The person declined on the computer.")
                    raise EnrollError("declined", "Setting up this computer was declined.")
                client.approve_host()
                report("approved", {})
            deadline = time.monotonic() + timeout_s
            while True:
                state = client.poll()
                current = str(state.get("state") or "")
                if current == "redeeming":
                    break
                if current in ("completed", "cancelled", "expired", "failed"):
                    raise EnrollError(current, {"cancelled": "The setup was cancelled on the Cremind page.",
                                                "expired": "The setup expired. Start again from the Cremind page.",
                                                "completed": "This setup link was already used.",
                                                }.get(current, "The setup failed. Start again from the Cremind page."))
                if time.monotonic() > deadline:
                    raise EnrollError("expired", "The Cremind page did not confirm in time. Start again.")
                time.sleep(poll_every_s)
            report("confirmed", {})
            key, secret = _pending(paths, link.session)
            result = client.redeem_host(idempotency_key=key,
                                        credential_sha256=hashlib.sha256(secret.encode("utf-8")).hexdigest())
            ca_pem = client.ca_pem
    except BootstrapError as exc:
        raise EnrollError(exc.code, str(exc)) from None
    try:
        server = result.get("server") or {}
        profile = result.get("profile") or {}
        enrollment = Enrollment(
            server=link.server, host_id=str(result["host_id"]), credential_id=str(result["credential_id"]),
            secret=secret, profile=str(profile.get("name") or bound.profile_name),
            profile_id=str(profile.get("id") or bound.profile_id),
            host_name=str((result.get("host") or {}).get("name") or name),
            server_installation_id=str(server.get("installation_id") or bound.installation_id),
            enrolled_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), ca_pem=ca_pem)
    except (KeyError, TypeError) as exc:
        raise EnrollError("bad_answer", f"Cremind's answer to redeem was not understood ({exc}).") from None
    save_enrollment(paths, enrollment)
    with contextlib.suppress(FileNotFoundError):
        (paths.remote_dir / "enrollment.pending").unlink()
    log.info("host: this computer is now a gateway computer of %s for profile %s (host %s)",
             enrollment.server, enrollment.profile, enrollment.host_id)
    report("enrolled", {"server": enrollment.server, "profile": enrollment.profile, "host_id": enrollment.host_id,
                        "computer": enrollment.host_name})
    return enrollment


__all__ = ["EnrollError", "Enrollment", "enroll", "forget_enrollment", "load_enrollment", "parse_host_link",
           "save_enrollment"]
