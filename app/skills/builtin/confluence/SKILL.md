---
name: confluence
description: Search, read, create, and update Confluence Cloud pages via OAuth2 (Atlassian 3LO). Authorizes through the Cremind Connect service (no Atlassian app setup on the client); tokens stay on this machine. Phase 1 is API-only — no event listener (Confluence has no OAuth webhook path; real-time push is planned via a Forge app).
metadata: {
  environment_variables: ["CREMIND_CONNECT_URL", "ATLASSIAN_CLIENT_ID", "CONFLUENCE_SITE_URL"],
  optional_environment_variables: ["CREMIND_CONNECT_URL", "ATLASSIAN_CLIENT_ID", "CONFLUENCE_SITE_URL"]
}
---

# confluence

**Purpose:** Python CLI for Confluence Cloud over OAuth2 (Atlassian 3LO).
Authorization goes through the **Cremind Connect** service (`connect.cremind.io`).
Because Atlassian 3LO is a *confidential* flow (no public PKCE; the client secret
is required at the token exchange), the code→token exchange is **mediated by the
backend** — which holds the secret — but **tokens are stored only on this machine**
(`scripts/.atlassian_token.json`). Runs via `uv` (PEP 723 inline metadata).

Reads/writes use the Confluence **v2** REST API
(`https://api.atlassian.com/ex/confluence/<cloudId>/wiki/api/v2`); free-text search
uses the v1 CQL endpoint (no v2 equivalent yet). The jira and confluence skills
share one Atlassian OAuth app, but each links and stores its own token.

> **No events in Phase 1.** Confluence Cloud has no webhook path for plain OAuth
> (3LO) apps — webhooks require a Connect or Forge app. Real-time change events are
> planned for a later phase via a Forge app (Forge Remote → the relay). For now,
> poll with `search`/`pages` (e.g. CQL ordered by `lastmodified`).

## Setup

No per-skill configuration is required by default — the client id and scopes come
from the Cremind Connect discovery doc (one-time org setup: an Atlassian 3LO app
with the Confluence scopes, the secret in cremind-connect, and the loopback
callback URL registered). Override in `scripts/.env` only if needed:
```
CREMIND_CONNECT_URL=https://connect.cremind.io       # optional; this is the default
ATLASSIAN_CLIENT_ID=                                 # optional; otherwise from discovery
CONFLUENCE_SITE_URL=https://your-site.atlassian.net  # optional; default = first accessible site
```

Then link the account:
```bash
uv run scripts/__main__.py link
```
`link` prints an Atlassian consent URL, then waits for consent to complete.
**Surface that URL to the user and ask them to open it and approve access.**
Confirm with `uv run scripts/__main__.py status`.

**If the post-approval page does NOT load** (the browser shows "can't connect" /
`ERR_CONNECTION_REFUSED`) — this happens on remote/Kubernetes deployments where
the registered loopback callback can't reach the backend — ask the user to **copy
the full URL from their browser's address bar** (it contains `code=...&state=...`)
and finish linking while the original `link` is still running:
```bash
uv run scripts/__main__.py complete-link --response "<the full redirect URL>"
```
Then confirm with `status`. The exchange still uses the registered
`http://127.0.0.1:<port>/` callback (the pasted URL only supplies the code).

> Note: Atlassian allows only a single, pre-registered callback URL, so linking
> requires running under `cremind serve` (the fixed-port backend listener).

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (human-readable on a TTY; force JSON with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | — |
| `complete-link` | `--response` | — |
| `status` | — | — |
| `spaces` | — | `--limit` (25) |
| `pages` | — | `--space` (id), `--title`, `--limit` (25) |
| `get` | `--id` | — |
| `create` | `--space` (id), `--title` | `--body`/`--body-file`/stdin |
| `update` | `--id` | `--title`, `--body`/`--body-file`/stdin |
| `search` | `--cql` | `--limit` (25) |

## Examples
```bash
uv run scripts/__main__.py status
uv run scripts/__main__.py spaces
uv run scripts/__main__.py pages --space 12345 --limit 10
uv run scripts/__main__.py get --id 67890
uv run scripts/__main__.py create --space 12345 --title "Release notes" --body "First line\nSecond line"
uv run scripts/__main__.py update --id 67890 --title "Release notes (v2)" --body "Updated content"
uv run scripts/__main__.py search --cql 'text ~ "roadmap" AND type = page ORDER BY lastmodified DESC'
```

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- Linking error about the backend listener → Atlassian needs the fixed-port loopback; run under `cremind serve`.
- `Atlassian /me returned no email` → the `read:me` scope wasn't granted; re-link.
- Page `update` requires the current version → handled automatically (fetched before write).

## Module layout
```
confluence/
├── SKILL.md
└── scripts/
    ├── .env                          # optional overrides
    ├── __main__.py                   # CLI entry
    └── app/
        ├── config.py                 # env + paths + logging
        ├── confluence_api.py         # Confluence REST v2 (+ v1 CQL search) wrapper
        ├── formatter.py              # page/space/search parsing + storage<->text
        ├── cli.py                    # argparse + dispatch
        └── atlassian/                # shared: account_key, discovery, auth (backend-mediated)
```
