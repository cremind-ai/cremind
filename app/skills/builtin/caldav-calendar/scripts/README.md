# caldav-calendar

A Python CLI for any CalDAV server using **username + password** auth — no OAuth2. Wraps the [`caldav`](https://pypi.org/project/caldav/) library and parses VEVENTs via [`icalendar`](https://pypi.org/project/icalendar/).

Run via [`uv`](https://docs.astral.sh/uv/). Each entry script carries PEP 723 inline metadata so `uv` auto-provisions a venv; no `pyproject.toml` needed.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) installed.
- A CalDAV account. Most providers require an **app-specific password** when 2FA is enabled.
- Credentials in [scripts/.env](scripts/.env):
  ```
  CALDAV_URL=https://caldav.icloud.com/
  CALDAV_USERNAME=you@example.com
  CALDAV_PASSWORD=your-app-specific-password
  CALDAV_CALENDAR=             # optional; default = first calendar
  # Optional: POLL_INTERVAL (60), HTTP_TIMEOUT (30)
  ```

### Provider settings

| Provider | `CALDAV_URL` | Auth notes |
|---|---|---|
| Apple iCloud | `https://caldav.icloud.com/` | App-specific password at <https://appleid.apple.com> |
| Fastmail | `https://caldav.fastmail.com/dav/` | App password at <https://app.fastmail.com/settings/security> |
| Nextcloud | `https://<your-host>/remote.php/dav/` | App password recommended |
| Posteo | `https://posteo.de:8443/` | Account password |
| Radicale | `https://<your-host>/<user>/` | As configured by admin |
| Zoho | `https://calendar.zoho.com/caldav/` | App-specific password |
| Custom / self-hosted | your provider's CalDAV root | per provider docs |

### Not supported

- **Google Calendar** — Google's CalDAV endpoint requires OAuth2 Bearer tokens.
- **Microsoft (Outlook.com / Microsoft 365)** — Microsoft does not implement CalDAV.

## One-shot CLI commands

```bash
# Discover calendars on the account
uv run scripts/__main__.py list-calendars

# Upcoming events (today -> +30 days, first calendar)
uv run scripts/__main__.py list

# Filtered list
uv run scripts/__main__.py list --calendar "Work" \
    --since 2026-06-01 --before 2026-06-30 --query "standup"

# Full detail of one event
uv run scripts/__main__.py get --uid abc-123@example.com

# Create a timed event
uv run scripts/__main__.py create \
    --summary "Team standup" \
    --start 2026-06-10T09:00:00+07:00 \
    --end 2026-06-10T09:30:00+07:00 \
    --location "Zoom" \
    --attendees alice@example.com --attendees bob@example.com

# Create an all-day event (end is INCLUSIVE; converted to RFC 5545 exclusive on write)
uv run scripts/__main__.py create --all-day \
    --summary "Conference" --start 2026-07-15 --end 2026-07-17

# Update (omitted fields preserved; pass --summary "" to clear)
uv run scripts/__main__.py update --uid abc-123@example.com --summary "Standup (rescheduled)"

# Delete (permanent — no Trash equivalent in CalDAV)
uv run scripts/__main__.py delete --uid abc-123@example.com
```

Global: `--json` forces JSON output even on a TTY.

## Event listener

```bash
uv run scripts/event_listener.py
```

- **Baseline on startup**: records the current event set per calendar; no events emitted.
- **Polling**: every 60 seconds (override with `POLL_INTERVAL`).
- **New events** → `events/new_event/<YYYY-MM-DDTHH-MM-SS> <summary>.md`.
- **Modified events** → `events/updated_event/<YYYY-MM-DDTHH-MM-SS> <summary>.md`.
- **State**: `scripts/.listener_state.json` (gitignored). Delete to force a re-baseline. Auto-wipes when `CALDAV_URL` changes (provider migration).
- **Diff strategy**: RFC 6578 sync-collection where supported; falls back to full listing + ETag diff on legacy servers. The choice is remembered per-calendar in state.

## Troubleshooting

- **CalDAV login failed on iCloud** → you need an **app-specific password** from <https://appleid.apple.com>. The regular Apple ID password will not work with 2FA enabled.
- **`No calendars found`** → check the account has a calendar; some providers don't create one by default.
- **`Event not found`** → the UID may be on a different calendar. Try `list-calendars` then `list --calendar OTHER`.
- **Listener emits a flood after restart** → state was wiped (fresh install or `CALDAV_URL` changed). Baseline runs again on first poll cycle.
- **All-day off-by-one** → pass the *last* day of the event as `--end` (inclusive). The skill writes RFC 5545's exclusive end date for you.
