import logging
import os
from pathlib import Path

from dotenv import load_dotenv

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
TOKEN_PATH = SCRIPTS_DIR / ".atlassian_token.json"

load_dotenv(dotenv_path=ENV_PATH, override=True)

# The relay base URL (discovery + OAuth mediation). Defaults to the org service.
CREMIND_CONNECT_URL = os.environ.get("CREMIND_CONNECT_URL", "https://connect.cremind.io").strip()

# Optional override of the shared Atlassian 3LO client id (else from the discovery doc).
ATLASSIAN_CLIENT_ID = os.environ.get("ATLASSIAN_CLIENT_ID", "").strip()

# Optional: pick a specific Atlassian site (by base url, e.g. https://acme.atlassian.net)
# when the account can access more than one. Defaults to the first accessible site.
CONFLUENCE_SITE_URL = os.environ.get("CONFLUENCE_SITE_URL", "").strip()

# Browser-facing OAuth redirect, injected by ``cremind serve`` (system_vars) as
# <APP_URL>/api/oauth/atlassian/callback. Atlassian 3LO requires a FIXED,
# pre-registered callback URL, so this MUST be registered (exact match) in the
# Atlassian developer console. Unset → use the manual ``complete-link`` paste.
OAUTH_REDIRECT_URI = os.environ.get("CREMIND_ATLASSIAN_REDIRECT_URI", "").strip() or None


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return logging.getLogger("confluence")
