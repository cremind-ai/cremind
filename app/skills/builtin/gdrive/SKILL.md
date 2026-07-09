---
name: gdrive
description: Search, list, download, upload, organize (move/rename/folders), and trash/restore Google Drive files via OAuth2, and receive file-change events in real time. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. A persistent listener uses Drive changes.watch channels (via the relay) and drops changed files as markdown. Downloads export Google Docs as markdown and Sheets as xlsx.
metadata:
  environment_variables:
    - name: CREMIND_CONNECT_URL
      description: Cremind Connect base URL (OAuth broker)
      required: false
      type: string
      default: https://connect.cremind.io
    - name: GOOGLE_CLIENT_ID
      description: Google OAuth Client ID (auto-fetched from Cremind Connect when blank)
      required: false
      type: string
      default: ''
    - name: GOOGLE_CLIENT_SECRET
      description: Google OAuth Client Secret (auto-fetched from Cremind Connect when blank)
      required: false
      secret: true
      type: string
      default: ''
  events:
    event_type:
      - name: file_changed
        description: A Drive file was created, modified, trashed, or removed
  long_running_app:
    command: uv run scripts/event_listener.py
    description: Persistent Google Drive listener. Maintains the changes.watch channel, subscribes to the Cremind Connect relay, and drops changed files as markdown.
---

# gdrive

**Purpose:** Python CLI + event listener for **Google** Drive over OAuth2.
Authorization goes through the **Cremind Connect** service (`connect.cremind.io`)
so you never touch GCP. The OAuth code→token exchange happens locally (loopback
PKCE); **tokens are stored only on this machine** (`scripts/.google_token.json`)
and the relay never sees them. Runs via `uv` (PEP 723 inline metadata).

## How it works (token-less relay)

- **Actions** (list/download/upload/…) call the Drive API v3 directly with your
  local token. Scope is full `https://www.googleapis.com/auth/drive`.
- **Events**: the listener calls `changes.watch()` with a channel id that encodes
  the routing key (`cm-<accountKey>-<nonce>`), pointing at the org's webhook URL
  (from discovery). It connects a WebSocket to the relay and proves account
  control with a short-lived Google **ID token**. On a change, the relay sends a
  content-free `resync` nudge; the listener then runs `changes.list(pageToken)`
  locally and writes changed files to `events/file_changed/`.
- The same account linked in two Cremind apps receives events in **both**.

## Setup

No configuration is required by default. `CREMIND_CONNECT_URL` defaults to
`https://connect.cremind.io`, and the OAuth `GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET` are fetched dynamically from Cremind Connect
(`GET /credentials/google`). Set any of these in `scripts/.env` **only to
override**:
```
CREMIND_CONNECT_URL=https://connect.cremind.io   # optional; this is the default
GOOGLE_CLIENT_ID=                                # optional; otherwise fetched from cremind-connect
GOOGLE_CLIENT_SECRET=                            # optional; otherwise fetched from cremind-connect
WATCH_RENEW_INTERVAL=21600                       # optional; watch renewal seconds (default 6h)
```

Then link the account:
```bash
uv run scripts/__main__.py link
```
`link` prints a Google consent URL, then waits (in the background) for consent
to complete. **Surface that URL to the user and ask them to open it and approve
access.** The consent redirect is received by the always-running Cremind backend
(its `/api/oauth/callback` route), so linking completes even though the command
keeps running in the background. Once the user says they've approved, confirm:
```bash
uv run scripts/__main__.py status
```

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON.

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | `--no-browser` |
| `complete-link` | `--response` | — |
| `status` | — | — |
| `list` | — | `--query` (raw Drive q=), `--name`, `--folder`, `--mime-type`, `--trashed`, `--max-results` (50), `--page-token`, `--order-by` (`modifiedTime desc`) |
| `info` | `--id` | — |
| `download` | `--id`, `--out` | `--mime` (export MIME override) |
| `upload` | `--file` | `--name`, `--parent`, `--mime` |
| `mkdir` | `--name` | `--parent` |
| `move` | `--id`, `--parent` | — |
| `rename` | `--id`, `--name` | — |
| `trash` | `--id` | — |
| `restore` | `--id` | — |

All `--id`/`--folder`/`--parent` flags accept a bare id or a full Drive/Docs URL.
`--out` may be a file path or a directory (the file name + extension is derived
automatically).

### Downloads & exports
Google-native files are **exported** with sensible defaults:

| Type | Default export | Override |
|---|---|---|
| Google Doc | `text/markdown` (falls back to `text/plain`) | `--mime application/vnd.openxmlformats-officedocument.wordprocessingml.document` for .docx |
| Google Sheet | `.xlsx` | `--mime text/csv` |
| Google Slides | `application/pdf` | — |
| Google Drawing | `image/png` | — |

Binary/uploaded files download as-is. **Export size limit:** Google caps
`files.export` at ~10 MB; larger Docs/Sheets exports will fail — request a smaller
range/format or download a binary copy.

## Event listener
```bash
uv run scripts/event_listener.py
```
Behavior:
- **Baseline on first run**: records a `startPageToken`; emits nothing for
  existing files.
- **Live**: on each relay `resync` nudge, runs incremental
  `changes.list(pageToken)` and writes changed files to
  `events/file_changed/<YYYY-MM-DDTHH-MM-SS> <name>.md`. Within one sync, multiple
  changes to the same file are collapsed to a single event (last state wins).
- **Catch-up**: on startup it also syncs anything that changed while offline.
- **Watch renewal**: re-creates the channel every ~6 hours (channels expire
  ≤7 days).
- **pageToken expiry (400/404)**: re-baselines; the bounded gap is not replayed.
- **State**: `scripts/.listener_state.json` (gitignored). Shutdown on SIGINT/SIGTERM.

> **Self-caused changes:** files you upload/move/rename/trash **through this skill**
> also appear in the changes feed and will emit `file_changed` events (Drive can't
> distinguish the actor). The subscription-level relevance/anti-recursion gate
> handles loops; there is no listener-side suppression.

### Event markdown schema
```markdown
---
id: "1AbCdEf..."
name: "Q3 report"
mime_type: "application/vnd.google-apps.document"
change: "updated"            # created | updated | trashed | removed (hint)
parents: ["0BxFolderId"]
created_time: "2026-07-01T02:11:00.000Z"
modified_time: "2026-07-09T04:33:21.000Z"
trashed: false
removed: false
size: ""                     # empty for Google-native types
web_view_link: "https://docs.google.com/document/d/1AbCdEf.../edit"
last_modified_by: "Alice (alice@example.com)"
event_type: "file_changed"
received_at: "2026-07-09T11:33:25+07:00"
---

File "Q3 report" (Google Doc) was updated by Alice.
Open: https://docs.google.com/document/d/1AbCdEf.../edit
```
The `change` field is a heuristic hint (`removed`/`trashed` are exact; `created`
vs `updated` is inferred from how close `created_time` is to the change time).
Both timestamps are in the frontmatter so subscribers can apply their own logic.

## Not in this skill (v1)
- **No hard delete** — `trash` is reversible; permanent deletion is intentionally
  omitted as the one unrecoverable action.
- **No sharing / permissions** — changing who can access a file is high-risk and
  irreversible; `web_view_link` is returned for every file instead.
- **No manual `watch` verb** — the listener establishes and renews the channel
  automatically.

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- `No GOOGLE_CLIENT_SECRET available` → cremind-connect must be reachable (it
  serves the secret), or set it in `scripts/.env` to override.
- No events arriving → confirm the listener is running and the relay is reachable
  (`curl $CREMIND_CONNECT_URL/.well-known/cremind-connect`); the webhook domain
  must be verified in Google for Drive push.
- Drive webhooks aren't signed by Google; the relay treats a nudge purely as a
  trigger and the listener re-syncs with your own token, so a spurious nudge only
  causes a harmless re-sync.

## Module layout
```
gdrive/
├── SKILL.md
├── events/file_changed/             # markdown drop-zone
└── scripts/
    ├── .env
    ├── __main__.py                  # CLI entry
    ├── event_listener.py            # listener entry
    ├── tests/test_account_key.py    # cross-repo routing-key parity test
    └── app/
        ├── config.py
        ├── drive_api.py             # Drive API v3 wrapper (changes.watch + files CRUD)
        ├── formatter.py             # file parsing + change classification + markdown
        ├── listener.py              # watch lifecycle + relay client + incremental sync
        ├── cli.py                   # argparse + dispatch
        └── google/                  # shared: account_key, discovery, auth (PKCE), relay_client
```
