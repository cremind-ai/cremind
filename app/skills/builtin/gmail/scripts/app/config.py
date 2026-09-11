import logging
import os
from pathlib import Path

from dotenv import load_dotenv

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
TOKEN_PATH = SCRIPTS_DIR / ".google_token.json"

load_dotenv(dotenv_path=ENV_PATH, override=True)

# The relay base URL (discovery + websocket). Defaults to the public org service.
CREMIND_CONNECT_URL = os.environ.get("CREMIND_CONNECT_URL", "https://connect.cremind.io").strip()

# The org "Desktop" OAuth client. client_id may also come from the discovery doc;
# the (non-confidential) Desktop secret is shipped here by the org.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

# Browser-facing OAuth redirect, injected by ``cremind serve`` (system_vars): an
# http loopback callback, http://<loopback host>:<port>/api/oauth/callback, derived
# from a loopback APP_URL — http or https, since Google only accepts http loopback
# redirects and an HTTPS install forwards that plaintext callback to its HTTPS
# handler — or from an operator's loopback pin. The backend captures the consent
# redirect there; ``link`` polls oauth_inbox and does the local PKCE exchange.
# Unset under the backend → ``link`` advertises
# http://localhost:1515/api/oauth/callback and ``complete-link`` finishes it when
# the browser cannot reach that. Unset standalone → an ephemeral loopback server.
OAUTH_REDIRECT_URI = os.environ.get("CREMIND_OAUTH_REDIRECT_URI", "").strip() or None

# Space-separated scopes to request at ``link``, overriding whatever
# cremind-connect advertises. Only useful with your own OAuth client: it is how a
# bring-your-own-credentials user asks for a Gmail read scope, which the shared
# client cannot request (Google classes every mailbox-reading scope as restricted).
GOOGLE_SCOPES = os.environ.get("GOOGLE_SCOPES", "").strip()


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return logging.getLogger("gmail")
