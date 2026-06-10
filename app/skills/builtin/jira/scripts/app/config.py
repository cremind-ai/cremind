import logging
import os
from pathlib import Path

from dotenv import load_dotenv

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = SCRIPTS_DIR.parent
ENV_PATH = SCRIPTS_DIR / ".env"
EVENTS_DIR = PROJECT_DIR / "events"
ISSUE_CHANGED_DIR = EVENTS_DIR / "issue_changed"
TOKEN_PATH = SCRIPTS_DIR / ".atlassian_token.json"
STATE_FILE = SCRIPTS_DIR / ".listener_state.json"
HEARTBEAT_FILE = SCRIPTS_DIR / ".listener_heartbeat"
LOCK_FILE = SCRIPTS_DIR / ".listener.lock"

load_dotenv(dotenv_path=ENV_PATH, override=True)

# The relay base URL (discovery + websocket + OAuth mediation). Defaults to the org service.
CREMIND_CONNECT_URL = os.environ.get("CREMIND_CONNECT_URL", "https://connect.cremind.io").strip()

# Optional override of the shared Atlassian 3LO client id (else taken from the discovery doc).
ATLASSIAN_CLIENT_ID = os.environ.get("ATLASSIAN_CLIENT_ID", "").strip()

# Optional: pick a specific Atlassian site (by base url, e.g. https://acme.atlassian.net)
# when the account can access more than one. Defaults to the first accessible site.
JIRA_SITE_URL = os.environ.get("JIRA_SITE_URL", "").strip()

# JQL filter for the Jira dynamic webhook AND the incremental pull.
# The webhook jqlFilter accepts only a RESTRICTED subset of JQL: operators
# =, !=, IN, NOT IN on fields issueKey, project, issuetype, status, priority,
# assignee, reporter (+ issue.property, cf[id]). Date clauses (created/updated),
# IS [NOT] EMPTY, ~, range operators, and JQL FUNCTIONS are rejected. As a
# convenience, `currentUser()` here is auto-substituted with your accountId before
# registering the webhook (the webhook matcher has no user context, so the function
# would silently match nothing), and used as-is for the pull (normal search resolves
# it). An EMPTY value matches ALL issues.
# Default = issues assigned to you. Examples: 'issuetype = Task', 'status != Done',
# 'project = ABC', 'assignee = currentUser() AND issuetype = Task', '' (all issues).
JIRA_WEBHOOK_JQL = os.environ.get("JIRA_WEBHOOK_JQL", "assignee = currentUser()").strip()

# Loopback OAuth callback (the persistent backend listener under `cremind serve`).
# Atlassian requires a FIXED, pre-registered callback URL, so the port must be set
# and must equal http://127.0.0.1:<port>/ registered in the Atlassian console.
OAUTH_CALLBACK_PORT = int(os.environ.get("CREMIND_OAUTH_CALLBACK_PORT", "0"))
OAUTH_BIND_ADDR = os.environ.get("CREMIND_OAUTH_BIND_ADDR", "").strip() or None

# Re-register/refresh the Jira dynamic webhook this often. Jira expires dynamic
# webhooks after 30 days; refresh well inside that window (default ~20 days).
WEBHOOK_RENEW_INTERVAL = int(os.environ.get("WEBHOOK_RENEW_INTERVAL", str(20 * 24 * 60 * 60)))

# Max issues pulled per resync (bounds a catch-up after a long offline gap).
CATCHUP_MAX = int(os.environ.get("CATCHUP_MAX", "50"))


def setup_logging(level: str | int = "INFO") -> logging.Logger:
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    return logging.getLogger("jira")
