import logging
import os
from pathlib import Path

from dotenv import load_dotenv

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
EVENTS_DIR = PROJECT_DIR / "events"
NEW_EMAIL_DIR = EVENTS_DIR / "new_email"
TOKEN_PATH = SCRIPTS_DIR / ".google_token.json"
STATE_FILE = SCRIPTS_DIR / ".listener_state.json"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"
LOCK_FILE = SCRIPTS_DIR / ".listener.lock"

load_dotenv(dotenv_path=ENV_PATH, override=True)

# The relay base URL (discovery + websocket). Defaults to the public org service.
CREMIND_CONNECT_URL = os.environ.get("CREMIND_CONNECT_URL", "https://connect.cremind.io").strip()

# The org "Desktop" OAuth client. client_id may also come from the discovery doc;
# the (non-confidential) Desktop secret is shipped here by the org.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

# Loopback OAuth callback server. In the Docker desktop image the host browser
# must reach the in-container server through a PUBLISHED port, so the port is
# fixed and the server binds 0.0.0.0 (not 127.0.0.1). On bare metal both are
# unset → the library's ephemeral-port loopback (port=0, bind localhost).
OAUTH_CALLBACK_PORT = int(os.environ.get("CREMIND_OAUTH_CALLBACK_PORT", "0"))
OAUTH_BIND_ADDR = os.environ.get("CREMIND_OAUTH_BIND_ADDR", "").strip() or None

# Browser-facing redirect URI to advertise to Google when the loopback listener
# is fronted by a single-port reverse proxy (the Kubernetes chart). When set,
# the redirect goes to "<APP_URL>/oauth/google/callback" (a loopback origin)
# instead of "http://127.0.0.1:<port>/", and nginx routes it back to the
# listener. Unset on Docker/native, where the browser reaches the port directly.
OAUTH_REDIRECT_URI = os.environ.get("CREMIND_OAUTH_REDIRECT_URI", "").strip() or None

# Bounded recent sync size when the Gmail historyId is too old (offline > ~7 days).
CATCHUP_MAX = int(os.environ.get("CATCHUP_MAX", "25"))

# Re-call users.watch() this often (Google requires <= 7 days; daily recommended).
WATCH_RENEW_INTERVAL = int(os.environ.get("WATCH_RENEW_INTERVAL", str(20 * 60 * 60)))


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return logging.getLogger("gmail")
