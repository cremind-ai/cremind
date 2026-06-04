import logging
import os
from pathlib import Path

from dotenv import load_dotenv


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
EVENTS_DIR = PROJECT_DIR / "events"
NEW_EMAIL_DIR = EVENTS_DIR / "new_email"
STATE_FILE = SCRIPTS_DIR / ".listener_state.json"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"

load_dotenv(dotenv_path=ENV_PATH, override=True)

USERNAME = os.environ.get("USERNAME", "")
PASSWORD = os.environ.get("PASSWORD", "")

IMAP_HOST = os.environ.get("IMAP_HOST", "")
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993"))
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
RECONNECT_MAX_SECONDS = int(os.environ.get("RECONNECT_MAX_SECONDS", "1500"))


def require_credentials() -> tuple[str, str]:
    missing = []
    if not USERNAME:
        missing.append("USERNAME")
    if not PASSWORD:
        missing.append("PASSWORD")
    if not IMAP_HOST:
        missing.append("IMAP_HOST")
    if not SMTP_HOST:
        missing.append("SMTP_HOST")
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}. "
            f"Populate {ENV_PATH} with your email provider's IMAP/SMTP settings. "
            "See SKILL.md for per-provider examples (Gmail, Outlook, Yahoo, iCloud, Fastmail, etc.)."
        )
    return USERNAME, PASSWORD


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("email-cli")
