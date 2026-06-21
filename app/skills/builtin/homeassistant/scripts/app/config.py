import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
EVENTS_DIR = PROJECT_DIR / "events"
STATE_FILE = SCRIPTS_DIR / ".listener_state.json"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"
# Concise, always-current device inventory (one line per entity). Maintained at
# runtime by the listener (and the `sync-devices` CLI verb); see app.devices.
REFERENCES_DIR = PROJECT_DIR / "references"
DEVICES_FILE = REFERENCES_DIR / "devices.md"
# Low-churn name<->entity_id index (one line per entity). Maintained at runtime by the
# listener (only on add/remove/rename) and the `sync-devices` CLI verb; see app.device_names.
DEVICE_NAMES_FILE = REFERENCES_DIR / "device_names.md"
# OAuth token store (used only when HA_TOKEN is not set). Gitignored.
TOKEN_PATH = SCRIPTS_DIR / ".ha_token.json"


def event_dir(name: str) -> Path:
    """Markdown drop-zone folder for one event type (see app.classify.EVENT_TYPES)."""
    return EVENTS_DIR / name


load_dotenv(dotenv_path=ENV_PATH, override=True)


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("false", "0", "no", "off")


# Base URL of the Home Assistant instance, e.g. http://homeassistant.local:8123
# (or an https:// URL for Nabu Casa / reverse-proxied instances).
HA_URL = os.environ.get("HA_URL", "").strip().rstrip("/")

# OPTIONAL Long-Lived Access Token (Profile -> Security -> Long-Lived Access Tokens).
# If set, it is used directly as the bearer token (no OAuth, no refresh). If left
# empty, the skill falls back to the OAuth 2.0 browser flow (`link`); see app.auth.
HA_TOKEN = os.environ.get("HA_TOKEN", "").strip()

# Comma-separated entity globs (fnmatch), e.g. "light.*,switch.*,sensor.kitchen_temp".
# Empty = match ALL entities (can be very noisy on busy instances).
HA_ENTITY_FILTER = [
    p.strip() for p in os.environ.get("HA_ENTITY_FILTER", "").split(",") if p.strip()
]

# Verify TLS certs. Set HA_VERIFY_SSL=false for self-signed/local certificates.
HA_VERIFY_SSL = _as_bool(os.environ.get("HA_VERIFY_SSL"), default=True)

HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))

# WebSocket receive timeout (seconds): how often the listener wakes to send a
# keepalive ping and check the shutdown flag while otherwise idle.
WS_RECV_TIMEOUT = int(os.environ.get("WS_RECV_TIMEOUT", "30"))

# Proactively recycle the socket this often (seconds) to avoid silent half-open sockets.
RECONNECT_MAX_SECONDS = int(os.environ.get("RECONNECT_MAX_SECONDS", "1500"))

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip() or "INFO"


def require_url() -> str:
    """Validate and return HA_URL. (Token presence is enforced by app.auth.)"""
    if not HA_URL:
        raise RuntimeError(
            f"Missing required env var HA_URL. Populate {ENV_PATH} with your Home Assistant "
            "URL, e.g. http://homeassistant.local:8123. See SKILL.md for setup details."
        )
    parsed = urlparse(HA_URL)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(
            f"HA_URL must be a full http(s) URL (got {HA_URL!r}). "
            "Example: http://homeassistant.local:8123"
        )
    return HA_URL


def ws_url(base: str | None = None) -> str:
    """Derive the WebSocket URL: http->ws, https->wss, with /api/websocket appended.

    Preserves any sub-path (some reverse proxies host HA under a prefix). Pure
    function of `base` (defaults to HA_URL) so it is unit-testable.
    """
    base = (base if base is not None else HA_URL).rstrip("/")
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):] + "/api/websocket"
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):] + "/api/websocket"
    return base + "/api/websocket"


def setup_logging(level: str | int | None = None) -> logging.Logger:
    logging.basicConfig(
        level=level or LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("homeassistant")
