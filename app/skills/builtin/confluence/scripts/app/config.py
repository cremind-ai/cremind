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

# Browser-facing OAuth redirect for the Atlassian 3LO flow. Atlassian requires a
# FIXED, pre-registered callback URL (exact match in the developer console), so —
# unlike Google — this is a single fixed value independent of APP_URL. Defaults to
# the documented localhost (suits native + the K8s ``port-forward svc/cremind 1515:80``);
# override via CREMIND_ATLASSIAN_REDIRECT_URI (e.g. Helm cremind.atlassianRedirectUri).
OAUTH_REDIRECT_URI = os.environ.get(
    "CREMIND_ATLASSIAN_REDIRECT_URI", "http://localhost:1515/api/oauth/callback"
).strip() or None


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return logging.getLogger("confluence")
