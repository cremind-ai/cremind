---
name: jira
description: Search, view, create, comment on, and transition Jira Cloud issues via OAuth2 (Atlassian 3LO), and receive issue-change events in real time. Authorizes through the Cremind Connect service (no Atlassian app setup on the client); tokens stay on this machine. A persistent listener registers a Jira webhook and (via the relay) drops changed issues as markdown.
metadata: {
  environment_variables: [
    {"name": "CREMIND_CONNECT_URL", "description": "Cremind Connect base URL (OAuth broker)", "required": false, "type": "string", "default": "https://connect.cremind.io"},
    {"name": "ATLASSIAN_CLIENT_ID", "description": "Atlassian OAuth Client ID (auto-fetched from Cremind Connect when blank)", "required": false, "type": "string", "default": ""},
    {"name": "JIRA_SITE_URL", "description": "Jira site URL (default: first accessible site)", "required": false, "type": "string", "default": ""},
    {"name": "JIRA_WEBHOOK_JQL", "description": "JQL filter selecting which issues raise events", "required": false, "type": "string", "default": "assignee = currentUser()"}
  ],
  events: {"event_type":[{"name":"issue_changed","description":"A Jira issue was created, updated, transitioned, or commented on"}]},
  long_running_app: {
    command: "uv run scripts/event_listener.py",
    description: "Persistent Jira listener. Registers + refreshes a Jira dynamic webhook, subscribes to the Cremind Connect relay, and drops changed issues as markdown.",
  }
}
---

# jira

**Purpose:** Python CLI + event listener for Jira Cloud over OAuth2 (Atlassian
3LO). Authorization goes through the **Cremind Connect** service
(`connect.cremind.io`). Because Atlassian 3LO is a *confidential* flow (no public
PKCE; the client secret is required at the token exchange), the code→token exchange
is **mediated by the backend** — which holds the secret — but **tokens are stored
only on this machine** (`scripts/.atlassian_token.json`) and the relay never keeps
them. Runs via `uv` (PEP 723 inline metadata).

## How it works (token-less relay)

- **Actions** (search/get/create/…) call the Jira REST API v3 directly with your
  local token, through `https://api.atlassian.com/ex/jira/<cloudId>/rest/api/3`.
- **Events**: the listener registers a Jira **dynamic webhook** pointing at the
  org ingress (`.../ingress/atlassian/jira?rk=<accountKey>`), then connects a
  WebSocket to the relay using a short-lived **relay-session** (Atlassian issues no
  id_token, so the backend mints the session from your access token via `/me`).
  When an issue changes, the relay sends a content-free `resync` nudge; the listener
  then pulls changed issues with JQL and writes them to `events/issue_changed/`.
- Jira dynamic webhooks **expire after 30 days**; the listener refreshes them well
  inside that window.

## Setup

No per-skill configuration is required by default — the client id and scopes come
from the Cremind Connect discovery doc. One-time org setup (already done centrally):
an Atlassian OAuth 2.0 (3LO) app with the Jira scopes, the client secret stored in
cremind-connect (`ATLASSIAN_CLIENT_SECRET`), and ONE callback URL registered in the
Atlassian developer console (3LO apps allow only a single, exact-match callback per
app). Cremind advertises a single FIXED redirect, `CREMIND_ATLASSIAN_REDIRECT_URI`,
which defaults to `http://localhost:1515/api/oauth/callback` (the documented
K8s `port-forward svc/cremind 1515:80`). Register that exact URL — or set
`CREMIND_ATLASSIAN_REDIRECT_URI` (chart: `cremind.atlassianRedirectUri`) to your own
single URL and register that. Where the browser can't reach it, finish with
`complete-link` (below).

Override defaults in `scripts/.env` only if needed:
```
CREMIND_CONNECT_URL=https://connect.cremind.io   # optional; this is the default
ATLASSIAN_CLIENT_ID=                             # optional; otherwise from discovery
JIRA_SITE_URL=https://your-site.atlassian.net    # optional; default = first accessible site
JIRA_WEBHOOK_JQL=assignee = currentUser()        # optional; default = issues assigned to you
```

> **Event scope (`JIRA_WEBHOOK_JQL`).** This scopes both the webhook subscription
> and the incremental pull. Jira's dynamic-webhook filter accepts only a restricted
> JQL subset — `=`/`!=` on `assignee`, `issuetype`, `status`, `project`, `reporter`,
> plus `currentUser()`. Date clauses (`created`/`updated`) and `IS [NOT] EMPTY` are
> rejected, so there's no true "all issues" filter. Useful values: `issuetype = Task`,
> `status != Done`, `project = ABC`, `assignee = currentUser() AND issuetype = Task`.

Then link the account:
```bash
uv run scripts/__main__.py link
```
`link` prints an Atlassian consent URL, then waits for consent to complete.
**Surface that URL to the user and ask them to open it and approve access.** The
redirect is captured by the always-running Cremind backend (persistent loopback
listener), so linking completes even though the command keeps running. Confirm with:
```bash
uv run scripts/__main__.py status
```
> Note: Atlassian allows only a single, pre-registered callback URL, so linking
> requires running under `cremind serve` (the fixed-port backend listener). The
> standalone/ephemeral fallback used by the Google skills is not available here.

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (human-readable on a TTY; force JSON with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | — |
| `status` | — | — |
| `myself` | — | — |
| `projects` | — | — |
| `search` | `--query` (JQL) | `--max-results` (25) |
| `get` | `--key` | — |
| `create` | `--project`, `--summary` | `--type` (Task), `--body`/`--body-file`/stdin |
| `comment` | `--key` | `--body`/`--body-file`/stdin |
| `transitions` | `--key` | — |
| `transition` | `--key`, `--to` (id) | — |
| `watch` | — | (register the webhook once; the listener does this automatically) |
| `unwatch` | — | — |

## Examples
```bash
uv run scripts/__main__.py status
uv run scripts/__main__.py search --query "assignee = currentUser() AND statusCategory != Done"
uv run scripts/__main__.py get --key ABC-123
uv run scripts/__main__.py create --project ABC --summary "Fix login" --type Bug --body "Steps to reproduce..."
uv run scripts/__main__.py comment --key ABC-123 --body "On it."
uv run scripts/__main__.py transitions --key ABC-123
uv run scripts/__main__.py transition --key ABC-123 --to 31
```

## Event listener
```bash
uv run scripts/event_listener.py
```
Behavior:
- **Baseline on first run**: records the current time as the cursor; emits nothing for existing issues.
- **Live**: on each relay `resync` nudge, runs an incremental JQL pull (`updated >= <cursor>`) and writes changed issues to `events/issue_changed/<YYYY-MM-DDTHH-MM-SS> <KEY> <summary>.md`.
- **Catch-up**: on startup it also syncs anything changed while offline (bounded by `CATCHUP_MAX`).
- **Webhook renewal**: refreshes the Jira dynamic webhook well inside the 30-day expiry; re-registers if a refresh fails.
- **De-dup**: Jira JQL `updated` is minute-precision, so a small emitted-set suppresses duplicate event files.
- **State**: `scripts/.listener_state.json` (gitignored). Shutdown on SIGINT/SIGTERM; single-instance lock.

### Event markdown schema
```markdown
---
key: "ABC-123"
summary: "Fix login"
status: "In Progress"
type: "Bug"
assignee: "Alice"
reporter: "Bob"
priority: "High"
updated: "2026-06-08T10:20:30.000-0700"
url: "https://your-site.atlassian.net/browse/ABC-123"
event_type: "issue_changed"
received_at: "2026-06-08T10:20:35+00:00"
---

<issue description as plain text>
```

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- Linking error about the backend OAuth callback → run under `cremind serve`; the callback registered in the Atlassian console must exactly equal `CREMIND_ATLASSIAN_REDIRECT_URI` (default `http://localhost:1515/api/oauth/callback`). If the consent redirect can't reach the backend (remote/Ingress/another port), finish with `uv run scripts/__main__.py complete-link --response "<the URL the browser landed on>"`.
- `Atlassian /me returned no email` → the `read:me` scope wasn't granted; re-link.
- No events arriving → confirm the listener is running, the webhook registered (`uv run scripts/__main__.py watch`), and the relay is reachable (`curl $CREMIND_CONNECT_URL/.well-known/cremind-connect`).
- Webhook registers but **no events ever arrive** (and `GET /rest/api/3/webhook/failed` is empty) → the OAuth app is **private**, so Atlassian only delivers when the app owner == the registering user. Enable **Distribution → Sharing** (make the app public) in the developer console — no Marketplace approval needed.
- `Clause ... is unsupported` / `Operator is not is unsupported` on registration → `JIRA_WEBHOOK_JQL` used a clause/operator outside the webhook subset (no dates, no `IS [NOT] EMPTY`, no functions); use `=`/`!=`/`IN`/`NOT IN` on assignee/issuetype/status/project/etc.
- 5-webhook limit / no delivery → OAuth apps are capped at 5 dynamic webhooks per user per tenant; `unwatch` to clear stale ones.

## Module layout
```
jira/
├── SKILL.md
├── events/issue_changed/             # markdown drop-zone
└── scripts/
    ├── .env                          # optional overrides
    ├── __main__.py                   # CLI entry
    ├── event_listener.py             # listener entry
    └── app/
        ├── config.py                 # env + paths + logging
        ├── jira_api.py               # Jira REST v3 wrapper (search/issue/webhook/...)
        ├── formatter.py              # issue parsing + ADF→text + markdown
        ├── listener.py               # webhook lifecycle + relay client + incremental pull
        ├── cli.py                    # argparse + dispatch
        └── atlassian/                # shared: account_key, discovery, auth (backend-mediated), relay_client
```
