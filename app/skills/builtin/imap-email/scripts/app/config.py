import logging
import os
from pathlib import Path

from dotenv import dotenv_values, load_dotenv


SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
EVENTS_DIR = PROJECT_DIR / "events"
NEW_EMAIL_DIR = EVENTS_DIR / "new_email"
STATE_FILE = SCRIPTS_DIR / ".listener_state.json"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"

load_dotenv(dotenv_path=ENV_PATH, override=True)

# Values straight from the .env file, consulted before the process environment.
_FILE_ENV = {k: (v or "").strip() for k, v in dotenv_values(ENV_PATH).items()}


def _env(name: str, default: str = "") -> str:
    """Read config, preferring scripts/.env over the process environment.

    ``USERNAME`` is a standard Windows environment variable holding the OS login
    name, so a bare shell hands us something that looks set but is not a mailbox
    credential — masking the "missing credential" error with an authentication
    failure. Only trust an inherited ``USERNAME`` that looks like an address; the
    supported channel is .env (written from Settings by the app), which always wins.
    """
    from_file = _FILE_ENV.get(name, "")
    if from_file:
        return from_file
    value = os.environ.get(name, default)
    if name == "USERNAME" and os.name == "nt" and "@" not in value:
        return ""
    return value


USERNAME = _env("USERNAME")
PASSWORD = _env("PASSWORD")

IMAP_HOST = _env("IMAP_HOST")
IMAP_PORT = int(_env("IMAP_PORT", "993") or "993")
SMTP_HOST = _env("SMTP_HOST")
SMTP_PORT = int(_env("SMTP_PORT", "587") or "587")

POLL_INTERVAL = int(_env("POLL_INTERVAL", "30") or "30")
RECONNECT_MAX_SECONDS = int(_env("RECONNECT_MAX_SECONDS", "1500") or "1500")


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
        hint = ""
        if "USERNAME" in missing and os.name == "nt":
            hint = (
                " Note: USERNAME must be set in .env (or the Settings UI) — an "
                "inherited Windows USERNAME is the OS login name, not a mailbox, so "
                "it is ignored unless it looks like an address."
            )
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}. "
            f"Populate {ENV_PATH} with your email provider's IMAP/SMTP settings. "
            "See SKILL.md for per-provider examples (Gmail, Outlook, Yahoo, iCloud, "
            f"Fastmail, etc.).{hint}"
        )
    return USERNAME, PASSWORD


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    return logging.getLogger("imap-email")
