"""Durable, system-wide HTTPS activation and single-use browser handoffs.

Transition announcements are public metadata. Sessions are never announced: each
tab authenticates separately and receives its own opaque, expiring ticket.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from app.config.settings import BaseConfig

_lock = threading.RLock()
TICKET_TTL = 600
EXPIRED_ROUTE_TTL = 86400
UPLOAD_RECOVERY_TTL = 86400
MAX_ACTIVE_TICKETS_PER_PROFILE = 64
MAX_ACTIVE_TICKETS_TOTAL = 512
MAX_EXPIRED_ROUTE_RECEIPTS = 4096
MAX_SOURCE_ORIGINS = 64
_PREFERENCES = frozenset(("theme", "auto_connect", "conversations_panel_collapsed",
                          "sidebar_collapsed", "usage_chip_hover", "events_view_mode",
                          "terminalPanelWidth", "rightPanelSplitRatio", "rightPanelShowHidden",
                          "rightPanelViewMode", "rightPanelCollapsed", "agent_activity_panel_maximized",
                          "eventRunDrawerMaximized"))
_PUBLIC = ("version", "id", "phase", "source_origin", "target_origin", "instance_id",
           "ca_sha256", "certificate_kind", "certificate_sha256", "same_public_port", "public_port", "created_at", "expires_at")


class HandoffSessionExpired(PermissionError):
    def __init__(self, profile: str, route: str):
        super().__init__("The saved session expired or was revoked. Sign in again to restore this page.")
        self.profile = profile
        self.route = route


def directory() -> Path:
    return Path(BaseConfig.CREMIND_SYSTEM_DIR) / "tls"


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def instance_id() -> str:
    path = directory() / "instance-id"
    with _lock:
        try:
            value = path.read_text(encoding="ascii").strip()
            if not re.fullmatch(r"[0-9a-f]{48}", value):
                raise OSError("The TLS installation identity is invalid.")
            return value
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            value = secrets.token_hex(24)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(value)
                return value
            except FileExistsError:
                value = path.read_text(encoding="ascii").strip()
                if not re.fullmatch(r"[0-9a-f]{48}", value):
                    raise OSError("The TLS installation identity is invalid.")
                return value


def origin(value: str, *, scheme: str | None = None) -> str:
    if not isinstance(value, str):
        raise ValueError("An HTTP or HTTPS origin is required.")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment or any(c in value for c in "\r\n\\")):
        raise ValueError("An HTTP or HTTPS origin without a path or credentials is required.")
    # Validate the port even if it is not needed for normalisation.
    _ = parsed.port
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    effective_scheme = scheme or parsed.scheme
    default_port = 443 if effective_scheme == "https" else 80
    port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
    return f"{effective_scheme}://{host}{port}"


def https_target(source: str) -> str:
    """Keep a direct listener/port-forward's effective port, including HTTP 80."""
    source = origin(source)
    parsed = urlsplit(source)
    if parsed.scheme == "https":
        return origin(source)
    if port_facts()["same_public_port"]:
        return origin(source, scheme="https") + (":80" if parsed.port is None else "")
    if BaseConfig.APP_URL.startswith("https://"):
        configured = urlsplit(BaseConfig.APP_URL)
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return origin(f"https://{host}" + (f":{configured.port}" if configured.port else ""))
    return origin(source, scheme="https")


def http_source(value: str) -> str:
    value = origin(value)
    parsed = urlsplit(value)
    if port_facts()["same_public_port"] and parsed.scheme == "https" and parsed.port is None:
        return origin(value, scheme="http") + ":443"
    return origin(value, scheme="http")


def port_facts(*, external: bool = False) -> dict:
    from app.config.tls_mode import _public_port, edge_tls_termination
    public_port = _public_port()
    return {"same_public_port": public_port != 0 and not external and not edge_tls_termination(), "public_port": public_port}


def load_transition(system_dir: str | None = None) -> dict | None:
    root = Path(system_dir) / "tls" if system_dir is not None else directory()
    try:
        value = json.loads((root / "transition.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OSError("TLS transition metadata cannot be read safely.") from error
    except ValueError:
        raise OSError("TLS transition metadata is invalid; refusing to reset its credential boundary.") from None
    if not isinstance(value, dict) or value.get("version") != 1:
        raise OSError("TLS transition metadata is invalid; refusing to reset its credential boundary.")
    if (not isinstance(value.get("id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32}", value["id"])
            or value.get("phase") not in ("prepared", "quiescing", "activating", "active", "cancelled")
            or not isinstance(value.get("instance_id"), str)
            or not re.fullmatch(r"[0-9a-f]{48}", value["instance_id"])):
        raise OSError("TLS transition metadata is invalid; refusing to reset its credential boundary.")
    try:
        source = origin(value.get("source_origin", ""))
        target = origin(value.get("target_origin", ""))
        aliases = value.get("source_origins", [source])
        if (source != value["source_origin"] or not source.startswith("http://")
                or target != value["target_origin"] or not target.startswith("https://")
                or not isinstance(aliases, list) or not aliases
                or len(aliases) > MAX_SOURCE_ORIGINS
                or any(origin(item) != item or not item.startswith("http://") for item in aliases)):
            raise ValueError
        epoch = value.get("transport_epoch", 0)
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError
    except (TypeError, ValueError):
        raise OSError("TLS transition metadata is invalid; refusing to reset its credential boundary.") from None
    return value


def public_transition(value: dict | None = None) -> dict | None:
    value = value if value is not None else load_transition()
    return {key: value.get(key) for key in _PUBLIC} if value else None


def save_transition(value: dict, *, announce: bool = True) -> dict:
    with _lock:
        _write(directory() / "transition.json", value)
    if announce:
        from app.events.transport_state_bus import get_transport_state_bus
        get_transport_state_bus().publish(public_transition(value))
    return value


def update_transition(
    change: Callable[[dict], dict], *, announce: bool = True,
) -> dict:
    """Read, change, and replace transition metadata under one process lock.

    Read/modify/write calls from several tabs can otherwise overwrite one
    another's readiness acknowledgements.  The file replacement remains
    atomic for restart recovery, while the lock serialises live requests.
    """
    with _lock:
        value = load_transition()
        if value is None:
            raise ValueError("The HTTPS transition was not found.")
        updated = change(value)
        _write(directory() / "transition.json", updated)
    if announce:
        from app.events.transport_state_bus import get_transport_state_bus
        get_transport_state_bus().publish(public_transition(updated))
    return updated


def certificate_info(*, external: bool = False) -> dict:
    from app.config.tls_mode import _public_port, edge_tls_termination
    if external or edge_tls_termination() or _public_port() == 0:
        return {"certificate_kind": "external", "certificate_sha256": None, "ca_sha256": None}
    if BaseConfig.SSL_CERTFILE and BaseConfig.SSL_KEYFILE:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        try:
            certificate = x509.load_pem_x509_certificate(Path(BaseConfig.SSL_CERTFILE).read_bytes())
            digest = certificate.fingerprint(hashes.SHA256()).hex().upper()
            fingerprint = ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
        except (OSError, ValueError):
            fingerprint = None
        return {"certificate_kind": "custom", "certificate_sha256": fingerprint, "ca_sha256": None}
    from app.config.tls_auto import ca_fingerprint_sha256, leaf_fingerprint_sha256
    ca_fingerprint = ca_fingerprint_sha256(BaseConfig.CREMIND_SYSTEM_DIR)
    leaf_fingerprint = leaf_fingerprint_sha256(BaseConfig.CREMIND_SYSTEM_DIR)
    return {"certificate_kind": "local" if ca_fingerprint else "none",
            "certificate_sha256": leaf_fingerprint, "ca_sha256": ca_fingerprint}


def validate_custom_certificate(host: str) -> None:
    """Check the supplied leaf/key, validity and the browser's actual SAN."""
    if not (BaseConfig.SSL_CERTFILE and BaseConfig.SSL_KEYFILE):
        return
    import datetime
    import ipaddress
    import ssl
    from cryptography import x509
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(BaseConfig.SSL_CERTFILE, BaseConfig.SSL_KEYFILE,
                                BaseConfig.SSL_KEYFILE_PASSWORD or None)
        certificate = x509.load_pem_x509_certificate(Path(BaseConfig.SSL_CERTFILE).read_bytes())
    except (OSError, ValueError):
        raise ValueError("The supplied certificate and private key cannot be loaded or do not match.") from None
    now = datetime.datetime.now(datetime.timezone.utc)
    if certificate.not_valid_before_utc > now:
        raise ValueError("The supplied certificate is not valid yet. Check its dates and the server clock.")
    if certificate.not_valid_after_utc <= now:
        raise ValueError("The supplied certificate has expired. Renew it before enabling HTTPS.")
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise ValueError("The supplied certificate has no Subject Alternative Names for browser hostname verification.") from None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        normalized = host.encode("idna").decode("ascii").lower().rstrip(".")
        matched = False
        for pattern in san.get_values_for_type(x509.DNSName):
            pattern = pattern.lower().rstrip(".")
            if pattern == normalized or (pattern.startswith("*.") and pattern.count("*") == 1
                    and normalized.endswith(pattern[1:]) and normalized.count(".") == pattern.count(".")):
                matched = True
                break
    else:
        matched = address in san.get_values_for_type(x509.IPAddress)
    if not matched:
        raise ValueError(f"The supplied certificate does not cover {host}. Use a certificate with this hostname or IP in its Subject Alternative Names.")


def register_source(value: dict, source: str) -> dict:
    """Register an authenticated client's alias and cover it before TLS binds."""
    source = origin(source)
    if not source.startswith("http://"):
        raise ValueError("An HTTP source origin is required for HTTPS migration.")
    aliases = value.get("source_origins", [value["source_origin"]])
    if source in aliases:
        return value
    from app.config.tls_mode import edge_tls_termination
    if not edge_tls_termination():
        validate_custom_certificate(urlsplit(source).hostname or "")

    transition_id = value.get("id")

    def add_alias(current: dict) -> dict:
        if current.get("id") != transition_id:
            raise ValueError("The HTTPS transition changed while registering this address.")
        current_aliases = current.get("source_origins", [current["source_origin"]])
        if (source not in current_aliases
                and current.get("phase") not in ("prepared", "quiescing")):
            raise ValueError("HTTPS source origins can only be added before activation is committed.")
        if source not in current_aliases and len(current_aliases) >= MAX_SOURCE_ORIGINS:
            raise ValueError("This HTTPS transition has too many source origins.")
        current["source_origins"] = list(dict.fromkeys(current_aliases + [source]))
        from app.config.tls_mode import _public_port, boot_serving_https
        if (current["phase"] in ("prepared", "quiescing")
                and not boot_serving_https()
                and not (BaseConfig.SSL_CERTFILE or BaseConfig.SSL_KEYFILE)
                and _public_port() != 0 and not edge_tls_termination()):
            from app.config.tls_auto import ensure_local_tls
            names = [urlsplit(item).hostname or "" for item in current["source_origins"]]
            ensure_local_tls(
                BaseConfig.CREMIND_SYSTEM_DIR,
                list(BaseConfig.SSL_AUTO_HOSTS) + names,
            )
            # Adding a certificate-valid alias can rotate the generated leaf.
            # That rotation is part of this preparation round, so pin the new
            # leaf while retaining the stable CA fingerprint shown for trust.
            current.update(certificate_info())
        return current

    return update_transition(add_alias, announce=False)


def mark_active(*, source: str | None = None, external: bool = False) -> None:
    from app.config.tls_mode import boot_serving_https
    if not external and not boot_serving_https():
        return
    cleanup_tickets()
    value = load_transition()
    if not value:
        source = source or http_source(BaseConfig.APP_URL)
        save_transition({"version": 1, "id": secrets.token_urlsafe(24), "phase": "active",
                         "source_origin": source, "source_origins": [source],
                         "target_origin": https_target(source), "instance_id": instance_id(),
                         **certificate_info(external=external),
                         **port_facts(external=external),
                         "created_at": time.time(), "expires_at": None, "management": management(),
                         "upload_recovery_until": time.time() + UPLOAD_RECOVERY_TTL})
        return
    # A public HTTPS probe is evidence of a working transport, not permission
    # to activate or resurrect an administrator's prepared/cancelled change.
    if value["phase"] not in ("activating", "active"):
        return
    # An already-active transition keeps its stable id and recovery deadline,
    # but factual certificate/port metadata must follow later renewals and
    # configuration changes. Electron and browser readiness pin these values.
    current = {"phase": "active", "expires_at": None,
               **certificate_info(external=external), **port_facts(external=external)}
    if external:
        current["target_origin"] = https_target(value["source_origin"])
    if any(value.get(key) != item for key, item in current.items()):
        value.update(current)
        save_transition(value)


def startup_upload_recovery_window() -> bool:
    """A persisted, non-renewing grace period for tabs absent at activation."""
    value = load_transition()
    return bool(value and value.get("phase") in ("activating", "active")
                and isinstance(value.get("upload_recovery_until"), (int, float))
                and time.time() < value["upload_recovery_until"])


def management() -> str:
    from app.config.tls_mode import _public_port, edge_tls_termination
    mode = (os.environ.get("INSTALL_MODE") or "native").lower()
    if mode in ("docker", "kubernetes") or _public_port() == 0 or edge_tls_termination():
        return "external"
    return "electron" if os.environ.get("CREMIND_ELECTRON_PARENT") is not None else "native"


def persist_native(value: dict):
    """Replace all related env entries together; a failed write changes nothing."""
    path = Path(BaseConfig.CREMIND_SYSTEM_DIR) / ".env"
    original_bytes = path.read_bytes() if path.exists() else None
    original = original_bytes.decode("utf-8-sig") if original_bytes is not None else ""
    creds_path = Path(BaseConfig.CREMIND_INSTALL_DIR) / "credentials.toml"
    original_creds = creds_path.read_bytes() if creds_path.exists() else None
    original_attrs = {key: getattr(BaseConfig, key) for key in ("APP_URL", "SSL_AUTO_HOSTS", "CORS_ALLOWED_ORIGINS")}
    raw_sources = value.get("source_origins", [value.get("source_origin")])
    if (not isinstance(raw_sources, list) or not raw_sources
            or len(raw_sources) > MAX_SOURCE_ORIGINS):
        raise ValueError("HTTPS transition source origins are invalid.")
    try:
        sources = [origin(item) for item in raw_sources]
    except ValueError:
        raise ValueError("HTTPS transition source origins are invalid.") from None
    if any(item != raw or not item.startswith("http://")
           for item, raw in zip(sources, raw_sources)):
        raise ValueError("HTTPS transition source origins are invalid.")
    cors = list(BaseConfig.CORS_ALLOWED_ORIGINS)
    if "*" not in cors:
        # A transition is system-wide, but tabs may reach the same listener
        # through localhost, a LAN address, or a DNS alias.  Handoff creation
        # registers each authenticated source before activation; retain both
        # sides of every one of those origins in the canonical environment so
        # a migrated tab is not rejected simply because another tab initiated
        # the switch through a different hostname.
        affected_origins = [
            item
            for source in sources
            for item in (source, https_target(source))
        ]
        cors = list(dict.fromkeys(cors + affected_origins))
    app_url = https_target(BaseConfig.APP_URL)
    hosts = list(dict.fromkeys(list(BaseConfig.SSL_AUTO_HOSTS)
                              + [urlsplit(source).hostname or "" for source in sources]))
    from app.config.tls_mode import _public_port
    updates = {"CREMIND_SSL": "true", "APP_URL": app_url, "CREMIND_UI_PORT": str(_public_port()),
               "CORS_ALLOWED_ORIGINS": ",".join(cors), "CREMIND_SSL_AUTO_HOSTS": ",".join(hosts)}

    # Jira and Confluence intentionally share one fixed Atlassian callback,
    # independent of APP_URL. Move the bundled default (or a callback tied to
    # one of this transition's HTTP aliases) with the public origin. Preserve a
    # custom unrelated callback because Atlassian permits only the URI already
    # registered by the operator.
    atlassian_key = "CREMIND_ATLASSIAN_REDIRECT_URI"
    default_atlassian = "http://localhost:1515/api/oauth/callback"
    existing_atlassian = os.environ.get(atlassian_key)
    if existing_atlassian is None:
        for line in original.splitlines():
            match = re.match(rf"^\s*(?:export\s+)?{atlassian_key}\s*=\s*(.*?)\s*$", line)
            if match:
                existing_atlassian = match.group(1)
                if (len(existing_atlassian) >= 2
                        and existing_atlassian[0] == existing_atlassian[-1]
                        and existing_atlassian[0] in ("'", '"')):
                    existing_atlassian = existing_atlassian[1:-1]
                break
    callback_update = None
    callback = (existing_atlassian or "").strip()
    if not callback:
        callback_update = f"{app_url}/api/oauth/callback"
    else:
        try:
            parsed_callback = urlsplit(callback)
            callback_origin = origin(f"{parsed_callback.scheme}://{parsed_callback.netloc}")
        except ValueError:
            parsed_callback = None
            callback_origin = ""
        if (callback == default_atlassian
                or (parsed_callback is not None
                    and parsed_callback.path == "/api/oauth/callback"
                    and not parsed_callback.query and not parsed_callback.fragment
                    and callback_origin in sources)):
            callback_update = f"{https_target(callback_origin)}/api/oauth/callback"
    if callback_update is not None:
        updates[atlassian_key] = callback_update
    original_env = {key: os.environ.get(key) for key in updates}

    def restore_file(destination, data):
        if data is None:
            destination.unlink(missing_ok=True)
            return
        temporary = destination.with_name(f".{destination.name}.restore-{secrets.token_hex(8)}")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def rollback():
        restore_file(path, original_bytes)
        restore_file(creds_path, original_creds)
        for key, item in original_attrs.items():
            setattr(BaseConfig, key, item)
        for key, item in original_env.items():
            if item is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = item
    lines = [line for line in original.splitlines()
             if not any(re.match(rf"^\s*(?:export\s+)?{key}\s*=", line) for key in updates)]
    lines.extend(f"{key}={item}" for key, item in updates.items())
    temporary = path.with_name(f".env.tls-{secrets.token_hex(8)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            from app.config.credentials_file import write_credentials_file
            write_credentials_file(force_mode="native")
        except Exception:
            rollback()
            raise
    finally:
        temporary.unlink(missing_ok=True)
    # Keep the running agent card/OAuth URL aligned with the next public URL.
    BaseConfig.APP_URL = app_url
    BaseConfig.SSL_AUTO_HOSTS = hosts
    BaseConfig.CORS_ALLOWED_ORIGINS = cors
    for key, item in updates.items():
        os.environ[key] = item
    if callback_update is not None:
        value["atlassian_redirect_uri_migrated"] = callback_update
    return rollback


def mint_ticket(token: str, data: dict) -> dict:
    from app.auth.tokens import verify_token
    claims = verify_token(token)
    if not claims or not claims.get("sub"):
        raise PermissionError("A valid profile session is required.")
    transition = load_transition()
    if not transition or transition["phase"] == "cancelled" or data.get("transition_id") != transition["id"]:
        raise ValueError("This HTTPS transition is no longer available.")
    source = origin(data.get("source_origin", ""))
    target = origin(data.get("target_origin", ""))
    if source not in transition.get("source_origins", [transition["source_origin"]]) or target != https_target(source):
        raise ValueError("The handoff must use a registered HTTP origin and its HTTPS counterpart.")
    route = data.get("route", "/")
    if not isinstance(route, str) or not route.startswith("/") or route.startswith("//") or "\\" in route or len(route) > 8192:
        raise ValueError("A local application route is required.")
    state = data.get("state", {})
    if not isinstance(state, dict) or len(json.dumps(state).encode()) > 131072:
        raise ValueError("The saved browser state is too large or malformed.")
    draft_prefix = f"cremind:draft:{claims['sub']}:"
    drafts = state.get("drafts", {})
    preferences = state.get("preferences", {})
    state = {
        "mount": state.get("mount") if state.get("mount") in ("/", "/electron-renderer/") else "/",
        "drafts": {key: item for key, item in drafts.items()
                   if isinstance(key, str) and key.startswith(draft_prefix) and isinstance(item, str)}
                  if isinstance(drafts, dict) else {},
        "preferences": {key: item for key, item in preferences.items()
                        if (key in _PREFERENCES or key in (f"chat_mode_{claims['sub']}", f"reasoning_enabled_{claims['sub']}"))
                        and isinstance(item, str)}
                       if isinstance(preferences, dict) else {},
    }
    expires = min(time.time() + TICKET_TTL, float(claims.get("exp", time.time() + TICKET_TTL)))
    ticket = secrets.token_urlsafe(32)
    installation = instance_id()
    if transition.get("instance_id") != installation:
        raise ValueError("This HTTPS transition belongs to another Cremind installation.")
    record = {"claims": claims, "profile": claims["sub"], "route": route, "state": state,
              "source_origin": source, "target_origin": target, "expires_at": expires,
              "transition_id": transition["id"], "instance_id": installation}
    path = directory() / "handoffs" / f"{hashlib.sha256(ticket.encode()).hexdigest()}.json"
    # The state can legitimately be large enough to retain several drafts and
    # attachment references. Bound aggregate disk use without deleting a live
    # ticket that an enrolled/suspended tab is relying on for the restart.
    with _lock:
        cleanup_tickets()
        active_total = 0
        active_for_profile = 0
        now = time.time()
        for existing_path in (directory() / "handoffs").glob("*.json"):
            try:
                existing = json.loads(existing_path.read_text(encoding="utf-8"))
                if "claims" not in existing or float(existing.get("expires_at", 0)) < now:
                    continue
                active_total += 1
                existing_profile = existing.get("profile") or existing.get("claims", {}).get("sub")
                if existing_profile == claims["sub"]:
                    active_for_profile += 1
            except (OSError, TypeError, ValueError):
                continue
        if active_for_profile >= MAX_ACTIVE_TICKETS_PER_PROFILE:
            raise ValueError("This profile has too many active HTTPS handoff tickets. Wait for an existing ticket to expire and retry.")
        if active_total >= MAX_ACTIVE_TICKETS_TOTAL:
            raise ValueError("This server has too many active HTTPS handoff tickets. Wait for an existing ticket to expire and retry.")
        _write(path, record)
    return {"ticket": ticket, "expires_at": expires}


def redeem_ticket(ticket: str, target: str) -> dict:
    import jwt
    from app.auth.tokens import (
        TOKEN_TRANSPORT_EPOCH_CLAIM,
        current_transport_epoch,
        verify_token,
    )
    if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", ticket):
        raise ValueError("The handoff ticket is invalid or expired.")
    path = directory() / "handoffs" / f"{hashlib.sha256(ticket.encode()).hexdigest()}.json"
    claimed = path.with_suffix(f".{secrets.token_hex(8)}.used")
    try:
        os.replace(path, claimed)  # exactly one consumer can claim the source
    except FileNotFoundError:
        raise ValueError("The handoff ticket is invalid, expired, or already used.") from None
    try:
        record = json.loads(claimed.read_text(encoding="utf-8"))
        transition = load_transition()
        installation = instance_id()
        if (record["target_origin"] != origin(target)
                or not transition or transition["phase"] == "cancelled"
                or record.get("transition_id") != transition.get("id")
                or record.get("instance_id") != installation
                or transition.get("instance_id") != installation):
            raise ValueError("The handoff ticket no longer matches this HTTPS server.")
        if record["expires_at"] < time.time():
            raise HandoffSessionExpired(record.get("profile") or record["claims"]["sub"], record["route"])
        # Re-sign the original session into the now-current transport epoch.
        # ``exp``, ``iat`` and the profile serial stay unchanged: HTTPS does not
        # extend a login or undo a later per-profile revocation.
        epoch = current_transport_epoch()
        if epoch is None:
            raise ValueError("The HTTPS credential boundary is unavailable.")
        claims = dict(record["claims"])
        claims[TOKEN_TRANSPORT_EPOCH_CLAIM] = epoch
        token = jwt.encode(claims, BaseConfig.get_jwt_secret(), algorithm="HS256")
        if not verify_token(token):
            raise HandoffSessionExpired(record["claims"]["sub"], record["route"])
        # They now belong to the restored composer. Release the ticket's
        # retention exemption and give normal idle cleanup a fresh window.
        for uploaded in _record_upload_paths(record):
            try:
                os.utime(uploaded, None)
            except OSError:
                pass
        return {"profile": record["claims"]["sub"], "token": token,
                "route": record["route"], "state": record["state"]}
    finally:
        claimed.unlink(missing_ok=True)


def cleanup_tickets() -> None:
    """Discard expired credentials/drafts; retain a short login-route receipt."""
    with _lock:
        handoffs = directory() / "handoffs"
        for path in [*handoffs.glob("*.json"), *handoffs.glob("*.used")]:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                expires = float(record.get("expires_at", 0))
                if expires + EXPIRED_ROUTE_TTL < time.time():
                    path.unlink(missing_ok=True)
                elif expires < time.time() and path.suffix == ".json" and "claims" in record:
                    _write(path, {"profile": record["claims"]["sub"], "route": record["route"],
                                  "target_origin": record["target_origin"], "transition_id": record["transition_id"],
                                  "instance_id": record.get("instance_id"), "expires_at": expires})
            except (KeyError, OSError, TypeError, ValueError):
                pass

        # Receipts contain no credentials or draft state and are only a
        # convenience for an expired session's HTTPS login route. Keep the most
        # recent bounded set; live tickets above are never removed for space.
        receipts: list[tuple[float, Path]] = []
        for path in handoffs.glob("*.json"):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if "claims" not in record:
                    receipts.append((float(record.get("expires_at", 0)), path))
            except (OSError, TypeError, ValueError):
                continue
        receipts.sort(reverse=True)
        for _expires, path in receipts[MAX_EXPIRED_ROUTE_RECEIPTS:]:
            path.unlink(missing_ok=True)


def _record_upload_paths(record: dict) -> set[str]:
    from app.utils.uploads_tmp import is_inside_conversation_tmp
    profile = record.get("claims", {}).get("sub", "")
    if not isinstance(profile, str) or not re.fullmatch(r"[a-z0-9_-]{1,64}", profile):
        return set()
    prefix = f"cremind:draft:{profile}:"
    paths = set()
    for key, raw in record.get("state", {}).get("drafts", {}).items():
        if not key.startswith(prefix) or not isinstance(raw, str):
            continue
        conversation = key[len(prefix):]
        try:
            draft = json.loads(raw)
            for attachment in draft.get("attachments", []):
                path = attachment.get("path") if isinstance(attachment, dict) else None
                if isinstance(path, str) and is_inside_conversation_tmp(profile, conversation, path):
                    paths.add(os.path.realpath(path))
        except (ValueError, AttributeError):
            continue
    return paths


def retained_upload_paths() -> set[str]:
    """Only uploads referenced by this transition's unexpired profile tickets."""
    transition = load_transition()
    if not transition or transition["phase"] not in ("prepared", "quiescing", "activating", "active"):
        return set()
    paths = set()
    for path in (directory() / "handoffs").glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("expires_at", 0) >= time.time() and record.get("transition_id") == transition["id"]:
                paths.update(_record_upload_paths(record))
        except (OSError, ValueError):
            continue
    return paths
