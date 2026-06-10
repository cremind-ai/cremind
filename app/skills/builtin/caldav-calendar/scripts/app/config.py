import logging
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
EVENTS_DIR = PROJECT_DIR / "events"
NEW_EVENT_DIR = EVENTS_DIR / "new_event"
UPDATED_EVENT_DIR = EVENTS_DIR / "updated_event"
STATE_FILE = SCRIPTS_DIR / ".listener_state.json"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"

load_dotenv(dotenv_path=ENV_PATH, override=True)

CALDAV_URL = os.environ.get("CALDAV_URL", "").strip()
CALDAV_USERNAME = os.environ.get("CALDAV_USERNAME", "")
CALDAV_PASSWORD = os.environ.get("CALDAV_PASSWORD", "")
CALDAV_CALENDAR = os.environ.get("CALDAV_CALENDAR", "").strip()

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))
HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "30"))


def require_credentials() -> tuple[str, str, str]:
    missing = []
    if not CALDAV_URL:
        missing.append("CALDAV_URL")
    if not CALDAV_USERNAME:
        missing.append("CALDAV_USERNAME")
    if not CALDAV_PASSWORD:
        missing.append("CALDAV_PASSWORD")
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}. "
            f"Populate {ENV_PATH} with your CalDAV provider's URL and credentials. "
            "See SKILL.md for per-provider examples (iCloud, Fastmail, Nextcloud, etc.). "
            "Note: Google Calendar requires OAuth2 (not supported); Microsoft does not implement CalDAV."
        )
    parsed = urlparse(CALDAV_URL)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise RuntimeError(
            f"CALDAV_URL must be a full http(s) URL (got {CALDAV_URL!r}). "
            "Example: https://caldav.icloud.com/"
        )
    return CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("caldav-calendar")
