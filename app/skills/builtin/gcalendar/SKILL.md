---
name: gcalendar
description: List, view, create, update, and delete Google Calendar events via OAuth2, and receive calendar-change events in real time. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. A persistent listener uses Calendar watch channels (via the relay) and drops changed events as markdown. This is the Google Calendar skill (for CalDAV providers like iCloud/Fastmail, use caldav-calendar instead).
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
    - name: CALENDAR_ID
      description: Calendar ID to operate on
      required: false
      type: string
      default: primary
  events:
    event_type:
      - name: event_changed
        description: A calendar event was created, modified, or cancelled
  long_running_app:
    command: uv run scripts/event_listener.py
    description: Persistent Google Calendar listener. Maintains the watch channel, subscribes to the Cremind Connect relay, and drops changed events as markdown.
---

# gcalendar

**Purpose:** Python CLI + event listener for **Google** Calendar over OAuth2.
Authorization goes through the **Cremind Connect** service (`connect.cremind.io`)
so you never touch GCP. The OAuth code→token exchange happens locally (loopback
PKCE); **tokens are stored only on this machine** (`scripts/.google_token.json`)
and the relay never sees them. Runs via `uv` (PEP 723 inline metadata).

> For CalDAV providers (Apple iCloud, Fastmail, Nextcloud, …) use the separate
> **caldav-calendar** skill. This skill is specifically for Google Calendar.

## How it works (token-less relay)

- **Actions** (list/create/…) call the Calendar API directly with your local token.
- **Events**: the listener calls `events.watch()` with a channel id that encodes
  the routing key (`cm.<accountKey>.<nonce>`), pointing at the org's webhook URL
  (from discovery). It connects a WebSocket to the relay and proves account
  control with a short-lived Google **ID token**. On a change, the relay sends a
  content-free `resync` nudge; the listener then runs `events.list(syncToken)`
  locally and writes changed events to `events/event_changed/`.
- The same account linked in two Cremind apps receives events in **both**.

## Setup

No configuration is required by default. `CREMIND_CONNECT_URL` defaults to
`https://connect.cremind.io`, and the OAuth `GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET` are fetched dynamically from Cremind Connect
(`GET /credentials/google`) so the org can rotate them without a client update.
Set any of these in `scripts/.env` (or via the Settings UI) **only to override**:
```
CREMIND_CONNECT_URL=https://connect.cremind.io   # optional; this is the default
GOOGLE_CLIENT_ID=                                # optional; otherwise fetched from cremind-connect
GOOGLE_CLIENT_SECRET=                            # optional; otherwise fetched from cremind-connect
CALENDAR_ID=primary                              # optional; default "primary"
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
(`--no-browser` only affects the standalone fallback used when the Cremind
backend isn't running; under the app the URL is always printed for the user.)

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON.

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | `--no-browser` |
| `status` | — | — |
| `list` | — | `--calendar`, `--since YYYY-MM-DD`, `--before YYYY-MM-DD`, `--query`, `--max-results` (50) |
| `get` | `--id` | `--calendar` |
| `create` | `--summary`, `--start`, `--end` | `--location`, `--description`, `--attendees a@b` (repeatable), `--all-day`, `--calendar` |
| `update` | `--id` | same as `create` (omitted fields preserved) |
| `delete` | `--id` | `--calendar` |
| `watch` | — | `--calendar` (the listener establishes this automatically) |

`--start` / `--end` accept ISO 8601 (`2026-06-10T09:00:00+07:00`) or `YYYY-MM-DD`
for all-day events. All-day `--end` is inclusive (converted to RFC 5545 exclusive on write).

## Examples
```bash
uv run scripts/__main__.py list --since 2026-06-01 --before 2026-06-30
uv run scripts/__main__.py create --summary "Standup" --start 2026-06-10T09:00:00+07:00 --end 2026-06-10T09:30:00+07:00 --location Zoom
uv run scripts/__main__.py create --all-day --summary "Conference" --start 2026-07-15 --end 2026-07-17
uv run scripts/__main__.py update --id abc123 --summary "Standup (moved)"
uv run scripts/__main__.py delete --id abc123
```

## Event listener
```bash
uv run scripts/event_listener.py
```
Behavior:
- **Baseline on first run**: records a `syncToken`; emits nothing for existing events.
- **Live**: on each relay `resync` nudge, runs incremental `events.list(syncToken)` and writes changed events to `events/event_changed/<YYYY-MM-DDTHH-MM-SS> <summary>.md` (cancellations included, `status: cancelled`).
- **Catch-up**: on startup it also syncs anything that changed while offline.
- **Watch renewal**: re-creates the channel every ~6 hours (channels expire ≤7 days).
- **syncToken expiry (410)**: re-baselines; the bounded gap is not replayed.
- **State**: `scripts/.listener_state.json` (gitignored). Shutdown on SIGINT/SIGTERM.

### Event markdown schema
```markdown
---
id: "abc123"
calendar: "primary"
status: "confirmed"
summary: "Standup"
start: "2026-06-10T09:00:00+07:00"
end: "2026-06-10T09:30:00+07:00"
all_day: false
location: "Zoom"
organizer: "you@gmail.com"
attendees: ["a@b.com"]
recurrence: ""
html_link: "https://www.google.com/calendar/event?eid=..."
updated: "2026-06-06T09:00:00.000Z"
event_type: "event_changed"
received_at: "2026-06-06T09:00:05+00:00"
---

<event description>
```

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- `No GOOGLE_CLIENT_SECRET available` → cremind-connect must be reachable (it serves the secret), or set it in `scripts/.env` to override.
- No events arriving → confirm the listener is running and the relay is reachable (`curl $CREMIND_CONNECT_URL/.well-known/cremind-connect`); the webhook domain must be verified in Google for Calendar push.
- Calendar webhooks aren't signed by Google; the relay treats a nudge purely as a trigger and the listener re-syncs with your own token, so a spurious nudge only causes a harmless re-sync.

## Module layout
```
gcalendar/
├── SKILL.md
├── events/event_changed/             # markdown drop-zone
└── scripts/
    ├── .env
    ├── __main__.py                   # CLI entry
    ├── event_listener.py             # listener entry
    ├── tests/test_account_key.py     # cross-repo routing-key parity test
    └── app/
        ├── config.py
        ├── gcal_api.py               # Calendar API wrapper (watch/syncToken/CRUD)
        ├── formatter.py              # event parsing + markdown + body building
        ├── listener.py               # watch lifecycle + relay client + incremental sync
        ├── cli.py                    # argparse + dispatch
        └── google/                   # shared: account_key, discovery, auth (PKCE), relay_client
```
