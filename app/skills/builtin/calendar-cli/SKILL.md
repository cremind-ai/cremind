---
name: calendar-cli
description: List, view, create, update, and delete calendar events via CalDAV. Works with Apple iCloud, Fastmail, Nextcloud, Posteo, Radicale, and generic CalDAV servers using username + password. A persistent event listener drops new and updated events as markdown. Does NOT support Google Calendar (requires OAuth2) or Microsoft (does not implement CalDAV).
metadata: {
  environment_variables: ["CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD", "CALDAV_CALENDAR"],
  events: {"event_type":[
    {"name":"new_event","description":"A new calendar event was added on the server"},
    {"name":"updated_event","description":"An existing calendar event was modified on the server"}
  ]},
  long_running_app: {
    command: "uv run scripts/event_listener.py",
    description: "Persistent listener for new and updated calendar events. Drops change notifications as markdown.",
  }
}
---

# calendar-cli

**Purpose:** Python CLI over CalDAV (RFC 4791). Reads/writes events on any CalDAV server using username + password — no OAuth2. Auto-discovers calendars per account. Runs via `uv` (PEP 723 inline metadata).

## Setup
Credentials in `scripts/.env`:
```
CALDAV_URL=https://caldav.icloud.com/
CALDAV_USERNAME=you@example.com
CALDAV_PASSWORD=your-app-specific-password
CALDAV_CALENDAR=         # optional; default = first writable calendar
# Optional: POLL_INTERVAL (60 seconds)
```

### Provider table

| Provider | `CALDAV_URL` | Auth notes |
|---|---|---|
| Apple iCloud | `https://caldav.icloud.com/` | **App-specific password** at <https://appleid.apple.com> (regular Apple ID password is rejected when 2FA is on) |
| Fastmail | `https://caldav.fastmail.com/dav/` | App password at <https://app.fastmail.com/settings/security> |
| Nextcloud | `https://<your-host>/remote.php/dav/` | App password recommended (account → Security → Devices & sessions) |
| Posteo | `https://posteo.de:8443/` | Account password |
| Radicale | `https://<your-host>/<user>/` | As configured by your admin |
| Zoho Calendar | `https://calendar.zoho.com/caldav/` | App-specific password |
| Generic / self-hosted | your provider's CalDAV root | per provider docs |

### Known limitations
- **Google Calendar is not supported.** Google's CalDAV endpoint requires OAuth2 Bearer tokens, not username + password. Use Apple iCloud, Fastmail, or another listed provider instead.
- **Microsoft (Outlook.com / Microsoft 365) is not supported.** Microsoft does not implement CalDAV.
- **No invitations sent.** `--attendees` writes ATTENDEE properties into the event but does NOT trigger iTIP/iMIP invitation emails to attendees.
- **`delete` is irreversible.** CalDAV has no Trash equivalent; deleted events are gone immediately on the server.

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (or human-readable on TTY; force with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `list-calendars` | — | — |
| `list` | — | `--calendar NAME`, `--since YYYY-MM-DD`, `--before YYYY-MM-DD`, `--query STR`, `--max-results N` (50), `--detail summary\|full` |
| `get` | `--uid` | `--calendar NAME` |
| `create` | `--summary`, `--start`, `--end` | `--location`, `--description`, `--attendees a@b` (repeatable), `--all-day`, `--calendar NAME` |
| `update` | `--uid` | same as `create` (omitted flags preserve existing values; pass `--summary ""` to clear) |
| `delete` | `--uid` | `--calendar NAME` |

`--start` / `--end` accept ISO 8601 (`2026-06-05T09:00:00+07:00`) or `YYYY-MM-DD` for all-day events. **All-day end is exclusive** per RFC 5545 — the CLI accepts inclusive end dates from the user and converts on write (a one-day event with `--all-day --start 2026-06-05 --end 2026-06-05` is correct).

`list` defaults to today → +30 days when no `--since` / `--before` are given. Recurring events are expanded server-side: one row per occurrence.

## Examples
```bash
# Discover calendars
uv run scripts/__main__.py list-calendars

# Upcoming events (next 30 days, first writable calendar)
uv run scripts/__main__.py list

# Filtered list
uv run scripts/__main__.py list --calendar "Work" --since 2026-06-01 --before 2026-06-30 --query "standup"

# Create
uv run scripts/__main__.py create \
    --summary "Team standup" \
    --start 2026-06-10T09:00:00+07:00 \
    --end 2026-06-10T09:30:00+07:00 \
    --location "Zoom" \
    --attendees alice@example.com --attendees bob@example.com

# All-day event
uv run scripts/__main__.py create --all-day --summary "Conference" --start 2026-07-15 --end 2026-07-17

# Update (omitted fields preserved)
uv run scripts/__main__.py update --uid abc-123@example.com --summary "Team standup (rescheduled)"

# Get details
uv run scripts/__main__.py get --uid abc-123@example.com

# Delete
uv run scripts/__main__.py delete --uid abc-123@example.com
```

## Event listener

Run persistently to capture inbound changes:
```bash
uv run scripts/event_listener.py
```
Behavior:
- **Baseline on startup**: records the current event set per calendar and emits **nothing** for pre-existing events.
- **Polling**: every 60 seconds (override with `POLL_INTERVAL`).
- **New events** → `events/new_event/<YYYY-MM-DDTHH-MM-SS> <summary>.md`.
- **Modified events** → `events/updated_event/<YYYY-MM-DDTHH-MM-SS> <summary>.md`.
- **State**: persisted to `scripts/.listener_state.json`. Gitignored. Delete it to force re-baseline. Auto-wipes if `CALDAV_URL` changes (provider migration).
- **Diff strategy**: tries RFC 6578 sync-collection first for efficiency; falls back to full-listing + ETag diff on servers that don't support it.
- **Shutdown**: SIGINT / SIGTERM stops cleanly.

### Event markdown schema
```markdown
---
uid: "abc-123@example.com"
href: "https://caldav.icloud.com/.../calendars/personal/abc.ics"
etag: "\"63b8c4f0-1\""
calendar: "Personal"
summary: "Team standup"
start: "2026-06-05T09:00:00+07:00"
end: "2026-06-05T09:30:00+07:00"
all_day: false
location: "Zoom"
organizer: "alice@example.com"
attendees: ["alice@example.com", "bob@example.com"]
status: "CONFIRMED"
recurrence: ""
event_type: "new_event"
received_at: "2026-06-05T08:55:00+07:00"
---

<plain text description>
```

Note: events created via this skill's own `create` verb will also be picked up by the listener and emitted as `new_event` on the next poll. This is by design (mirrors email-cli's send-then-see-in-events behavior).

## Troubleshooting
- `CalDAV login failed` on iCloud → you need an **app-specific password** from <https://appleid.apple.com>, not your Apple ID password.
- `No writable calendars found` → check the account has a calendar created; some providers don't create one by default. Verify with `list-calendars`.
- `Event not found: <uid>` → the UID doesn't exist on the selected calendar. Try `--calendar` to target a different one, or `list` to find the correct UID.
- Listener emits a flood of events on first poll → state file was wiped (e.g. on a fresh install or after `CALDAV_URL` change). Baseline runs on startup; in-flight events during the baseline transaction may produce one extra batch.
- All-day off-by-one: pass `--end` as the LAST day of the event (inclusive). The skill converts to RFC 5545 exclusive end on write.

## Module layout
```
calendar-cli/
├── SKILL.md
├── events/
│   ├── new_event/                  # markdown drop-zone for added events
│   └── updated_event/              # markdown drop-zone for modified events
└── scripts/
    ├── .env                        # CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD, CALDAV_CALENDAR
    ├── __main__.py                 # CLI entry (uv run scripts/__main__.py ...)
    ├── event_listener.py           # listener entry (uv run scripts/event_listener.py)
    └── app/
        ├── config.py               # env loading + paths + logging
        ├── caldav_client.py        # DAVClient wrapper: connect, discover, find calendar
        ├── ical.py                 # VEVENT build/parse via `icalendar`
        ├── operations.py           # verbs: list-calendars / list / get / create / update / delete
        ├── formatter.py            # list rows + event markdown
        ├── listener.py             # polling loop, sync-token diff, atomic event writes
        └── cli.py                  # argparse builder + dispatch
```
