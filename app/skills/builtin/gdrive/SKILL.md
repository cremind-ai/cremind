---
name: gdrive
description: Read, download, upload, organize (move/rename/folders), and trash/restore Google Drive files via OAuth2, and receive file-change events in real time. Access is per-file - Cremind reaches only files the user picked through the Google file picker (the grant command) plus files Cremind created; there is no whole-Drive search. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. Downloads export Google Docs as markdown and Sheets as xlsx.
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
    - name: GOOGLE_SCOPES
      description: Space-separated OAuth scopes to request at link. Only useful with your own OAuth client - it is how a bring-your-own-credentials user asks for whole-Drive access.
      required: false
      type: string
      default: ''
  events:
    event_type:
      - name: file_changed
        description: A granted Drive file was created, modified, trashed, or removed
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

## Access model — read this first

Cremind holds the `https://www.googleapis.com/auth/drive.file` scope. It reaches
**only**:

1. files the user explicitly picked through Google's file picker (the `grant`
   command below), and
2. files Cremind itself created (`upload`, `mkdir`).

**Knowing a file's id or URL is never enough.** If the user pastes a Drive link
Cremind was never granted, every call against it fails with 403/404 — Google does
not distinguish "not granted" from "does not exist". The fix is always a `grant`,
never a retry.

What this scope cannot do, at all:

- **No whole-Drive search.** `list` returns the granted + created set, not the
  user's Drive. To act on a file the user mentions by *name*, ask them for the URL
  or run `grant` so they can pick it.
- **No whole-Drive monitoring.** The listener only sees changes to granted files.

Sheets and Docs are different: the **gsheets** and **gdocs** skills read and write
any spreadsheet/document the user owns straight from a URL or id, with no Drive
grant at all. Prefer those for in-place content work; you only need gdrive (and a
grant) for Drive-level operations — download/export, move, rename, trash.

## How it works (token-less relay)

- **Actions** call the Drive API v3 directly with your local token.
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
GOOGLE_SCOPES=                                   # optional; only with your own OAuth client
WATCH_RENEW_INTERVAL=21600                       # optional; watch renewal seconds (default 6h)
```

### 1. Link the account
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

### 2. Grant the files Cremind may touch
Linking alone grants access to **no existing files**. Run:
```bash
uv run scripts/__main__.py grant                       # user picks anything
uv run scripts/__main__.py grant --file <drive-url>     # pre-select a known file
```
Like `link`, this prints a URL that **you must show to the user**; it then waits
for them to pick files and approve. On success it reports each granted file, and
for a granted folder it reports whether the files inside it came along
(`children_visible`) — Google does not document that either way, so it is measured
rather than assumed.

Grants are permanent until the user revokes Cremind at
<https://myaccount.google.com/connections> (which removes **all** of them —
Google has no per-file revoke). They survive token refresh and re-linking.

The user can also grant files without you: **Settings → Google Drive** in the web
UI, or `cremind drive grant --file <url>` in the terminal.

## Unattended runs (event runs, schedules, file watchers)

**Never run `grant` when no user is present.** It waits up to 10 minutes for a
browser consent that nobody will complete, and the shell tool's timeout is far
shorter — the command gets backgrounded and abandoned while your run stalls.

When an unattended run hits a file Cremind cannot reach:

1. Send the user a notification naming the file and asking them to grant it
   (Settings → Google Drive, or `cremind drive grant --file <id>`).
2. **Stop the run.** Do not retry, do not wait, do not open a consent URL.

Every 403/404 from this skill returns both an `interactive_fix` and an
`unattended_fix` string — pick the one that matches your context.

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON.

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | `--no-browser` |
| `complete-link` | `--response` | — |
| `status` | — | — |
| `grant` | — | `--file` (repeatable id/URL), `--single`, `--no-folders`, `--mime-types`, `--no-wait`, `--timeout` (600) |
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

`list` searches only what Cremind can already reach, so `--name` is a filter over
that set — not a way to find a file in the user's Drive. `move` needs the
**destination folder** granted too, not just the file.

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
- **Coverage**: only granted + Cremind-created files ever appear. Granting a new
  file adds it to the feed from that point on; the feed is not a whole-Drive watch.
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

## Migrating from whole-Drive access

Accounts linked before per-file access carry the old `.../auth/drive` scope.
`status` reports `scopes_stale: true` for those. They keep working until Google
retires the grant, but the fix is to re-link and re-grant:

```bash
uv run scripts/__main__.py link      # re-consents at the new, smaller scope
uv run scripts/__main__.py grant     # pick the files Cremind should keep reaching
```

The listener needs no attention: its saved `pageToken` stays valid and the feed
simply narrows to granted files. If the listener logs `invalid_grant`, the stored
refresh token is dead — re-link.

## Bring your own Google credentials

Whole-Drive access is only unavailable because Google would charge the org a
recurring security assessment for it. Your *own* Google Cloud project falls under
Google's personal-use exception (fewer than 100 users), which needs no
verification and no assessment — so you can request the broad scope there. Set in
`scripts/.env`:

```
GOOGLE_CLIENT_ID=<your desktop client id>
GOOGLE_CLIENT_SECRET=<your desktop client secret>
GOOGLE_SCOPES=openid email https://www.googleapis.com/auth/drive
```

then re-run `link`. `list` then searches your whole Drive, every file is readable
by id, and `grant` becomes unnecessary. `status` reports the wider access model
and will not ask you to re-link.

Set your OAuth app's publishing status to **Production without submitting for
verification** — "Testing" expires refresh tokens after 7 days and silently breaks
automation. Note that `file_changed` **events stop working** under your own
client: the Cremind relay only accepts ID tokens issued to the shared client. See
the Cremind docs, *Setup → Bring your own Google credentials*.

## Not in this skill (v1)
- **No whole-Drive search or monitoring** — impossible under per-file access; see
  the access model above (unless you bring your own credentials).
- **No hard delete** — `trash` is reversible; permanent deletion is intentionally
  omitted as the one unrecoverable action.
- **No sharing / permissions** — changing who can access a file is high-risk and
  irreversible; `web_view_link` is returned for every file instead.
- **No per-file revoke** — Google offers none; the user revokes Cremind entirely at
  <https://myaccount.google.com/connections>.
- **No manual `watch` verb** — the listener establishes and renews the channel
  automatically.

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- `drive_file_not_granted` (exit 3) → the file was never granted, or doesn't
  exist. Interactive: run `grant --file <id>` and show the user the URL.
  Unattended: notify and stop (see above).
- `scopes_stale: true` in `status` → re-link, then re-grant (see migration above).
- `No GOOGLE_CLIENT_SECRET available` → cremind-connect must be reachable (it
  serves the secret), or set it in `scripts/.env` to override.
- A picked file still unreadable → confirm the user approved with the **linked**
  account (`status` shows which email); a pick under a different Google account
  grants nothing here.
- No events arriving → confirm the listener is running and the relay is reachable
  (`curl $CREMIND_CONNECT_URL/.well-known/cremind-connect`); the webhook domain
  must be verified in Google for Drive push. Also confirm the file is granted.
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
        ├── errors.py                # ungranted-file errors with interactive/unattended fixes
        ├── formatter.py             # file parsing + change classification + markdown
        ├── grant.py                 # Google picker grant flow (per-file access)
        ├── listener.py              # watch lifecycle + relay client + incremental sync
        ├── cli.py                   # argparse + dispatch
        └── google/                  # shared: account_key, discovery, auth (PKCE), relay_client
```
