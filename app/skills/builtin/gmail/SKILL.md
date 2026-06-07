---
name: gmail
description: Read, search, send, reply to, label, and trash Gmail messages via OAuth2, and receive new-email events in real time. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. A persistent listener uses Gmail watch + Pub/Sub (via the relay) and drops new INBOX messages as markdown.
metadata: {
  environment_variables: ["CREMIND_CONNECT_URL", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"],
  events: {"event_type":[{"name":"new_email","description":"A new email arrived in the Gmail INBOX"}]},
  long_running_app: {
    command: "uv run scripts/event_listener.py",
    description: "Persistent Gmail listener. Maintains the Gmail watch, subscribes to the Cremind Connect relay, and drops new INBOX messages as markdown.",
  }
}
---

# gmail

**Purpose:** Python CLI + event listener for Gmail over OAuth2. Authorization goes
through the **Cremind Connect** service (`connect.cremind.io`) so you never touch
GCP. The OAuth code→token exchange happens locally (loopback PKCE); **tokens are
stored only on this machine** (`scripts/.google_token.json`) and the relay never
sees them. Runs via `uv` (PEP 723 inline metadata).

## How it works (token-less relay)

- **Actions** (list/send/…) call the Gmail API directly with your local token.
- **Events**: the listener calls Gmail `users.watch()` into the org's Pub/Sub
  topic (from the relay's discovery doc), then connects a WebSocket to the relay
  and proves account control with a short-lived Google **ID token**. When mail
  arrives, the relay sends a content-free `resync` nudge; the listener then runs
  `history.list()` locally and writes the new message to `events/new_email/`.
- The same account linked in two Cremind apps receives events in **both** — the
  relay fans out to every connected app for that account.

## Setup

`scripts/.env`:
```
CREMIND_CONNECT_URL=https://connect.cremind.io   # optional; this is the default
GOOGLE_CLIENT_ID=                                # optional; otherwise taken from discovery
GOOGLE_CLIENT_SECRET=                            # the org's (non-confidential) Desktop client secret
```

Then link the account (opens a browser for Google consent):
```bash
uv run scripts/__main__.py link
```
Use `--no-browser` on headless machines (prints the URL to open manually).

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (human-readable on a TTY; force JSON with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | `--no-browser` |
| `status` | — | — |
| `list` | — | `--query`, `--max-results` (10), `--detail summary\|full` |
| `search` | `--query` | `--max-results` (10), `--detail summary\|full` |
| `get` | `--id` | — |
| `send` | `--to` (repeatable), `--subject` | `--cc`, `--bcc` (repeatable), `--body`/`--body-file`/stdin |
| `reply` | `--id` | `--cc`, `--bcc`, body via `--body`/`--body-file`/stdin |
| `trash` | `--id` | — |
| `watch` | — | (establish the Gmail watch once; the listener does this automatically) |
| `unwatch` | — | — |

`--id` is the Gmail message id (from `list`/`search`). `--query` uses Gmail search syntax (e.g. `from:alice newer_than:7d`).

## Examples
```bash
uv run scripts/__main__.py status
uv run scripts/__main__.py list --max-results 5
uv run scripts/__main__.py search --query "from:boss is:unread"
uv run scripts/__main__.py get --id 1923abc...
uv run scripts/__main__.py send --to a@b.com --subject "Hi" --body "Hello there"
uv run scripts/__main__.py reply --id 1923abc... --body "Thanks!"
uv run scripts/__main__.py trash --id 1923abc...
```

## Event listener
```bash
uv run scripts/event_listener.py
```
Behavior:
- **Baseline on first run**: records the current `historyId`; emits nothing for existing mail.
- **Live**: on each relay `resync` nudge, runs incremental `history.list()` and writes new INBOX messages to `events/new_email/<YYYY-MM-DDTHH-MM-SS> <subject>.md`.
- **Catch-up**: on startup it also syncs anything that arrived while offline.
- **Watch renewal**: re-calls `users.watch()` well within Google's 7-day limit.
- **Offline > ~7 days**: if the `historyId` is too old, the cursor is reset and the bounded gap is not replayed (by design — no full-mailbox dump).
- **State**: `scripts/.listener_state.json` (gitignored). Shutdown on SIGINT/SIGTERM.

### Event markdown schema
```markdown
---
id: "1923abc..."
thread_id: "1923a..."
message_id: "<CABc...@mail.gmail.com>"
from: "Alice <alice@example.com>"
to: "you@gmail.com"
cc: ""
subject: "Lunch?"
date: "Fri, 06 Jun 2026 09:00:00 +0000"
labels: ["INBOX", "UNREAD"]
event_type: "new_email"
received_at: "2026-06-06T09:00:05+00:00"
---

<plain-text body>
```

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- `GOOGLE_CLIENT_SECRET missing` → the org's Desktop client secret must be in `scripts/.env`.
- `Google did not return a refresh token` → revoke at <https://myaccount.google.com/permissions> and re-link.
- No events arriving → confirm the listener is running, that `link` used `openid email` scopes, and that the relay is reachable (`curl $CREMIND_CONNECT_URL/.well-known/cremind-connect`).
- Restricted scopes: while the org's consent screen is in "Testing", only added test users can link.

## Module layout
```
gmail/
├── SKILL.md
├── events/new_email/                 # markdown drop-zone
└── scripts/
    ├── .env                          # CREMIND_CONNECT_URL, GOOGLE_CLIENT_ID/SECRET
    ├── __main__.py                   # CLI entry
    ├── event_listener.py             # listener entry
    ├── tests/test_account_key.py     # cross-repo routing-key parity test
    └── app/
        ├── config.py                 # env + paths + logging
        ├── gmail_api.py              # Gmail API wrapper (watch/history/list/get/send/...)
        ├── formatter.py              # message parsing + markdown
        ├── listener.py               # watch lifecycle + relay client + incremental sync
        ├── cli.py                    # argparse + dispatch
        └── google/                   # shared: account_key, discovery, auth (PKCE), relay_client
```
