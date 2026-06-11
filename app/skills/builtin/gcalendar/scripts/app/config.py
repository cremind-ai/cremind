import logging
import os
from pathlib import Path

from dotenv import load_dotenv

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
EVENTS_DIR = PROJECT_DIR / "events"
EVENT_CHANGED_DIR = EVENTS_DIR / "event_changed"
TOKEN_PATH = SCRIPTS_DIR / ".google_token.json"
STATE_FILE = SCRIPTS_DIR / ".listener_state.json"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"
LOCK_FILE = SCRIPTS_DIR / ".listener.lock"

load_dotenv(dotenv_path=ENV_PATH, override=True)

CREMIND_CONNECT_URL = os.environ.get("CREMIND_CONNECT_URL", "https://connect.cremind.io").strip()
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

# Browser-facing OAuth redirect, injected by ``cremind serve`` (system_vars) as
# <APP_URL>/api/oauth/google/callback when APP_URL is a loopback origin. The
# backend captures the consent redirect there; the skill polls oauth_inbox and
# does the local PKCE exchange. Unset → fall back to an ephemeral loopback server
# (standalone CLI) or the manual ``complete-link`` paste (non-loopback APP_URL).
OAUTH_REDIRECT_URI = os.environ.get("CREMIND_OAUTH_REDIRECT_URI", "").strip() or None

CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary").strip()

# Calendar channels expire faster than Gmail watches; renew every ~6 hours.
WATCH_RENEW_INTERVAL = int(os.environ.get("WATCH_RENEW_INTERVAL", str(6 * 60 * 60)))


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return logging.getLogger("gcalendar")
