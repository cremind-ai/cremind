---
name: imap-email
description: Read, search, list, send, reply to, and trash email over IMAP/SMTP with any provider (Gmail, Outlook, Yahoo, iCloud, Fastmail, Zoho, custom), using an app password. A persistent listener drops new messages as markdown events. This is Cremind's primary way to read email and to react to new mail - the gmail skill is send-only because every Gmail scope that can read a mailbox requires a paid Google security assessment.
metadata:
  environment_variables:
    - name: USERNAME
      description: IMAP/SMTP account username (usually your email address)
      required: true
      type: string
    - name: PASSWORD
      description: IMAP/SMTP password or app-specific password
      required: true
      secret: true
      type: string
    - name: IMAP_HOST
      description: IMAP server hostname, e.g. imap.gmail.com
      required: true
      type: string
    - name: SMTP_HOST
      description: SMTP server hostname, e.g. smtp.gmail.com
      required: true
      type: string
    - name: IMAP_PORT
      description: IMAP port (SSL)
      required: false
      type: string
      default: '993'
    - name: SMTP_PORT
      description: SMTP port (STARTTLS)
      required: false
      type: string
      default: '587'
    - name: POLL_INTERVAL
      description: Seconds between INBOX polls in the event listener
      required: false
      type: string
      default: '30'
    - name: RECONNECT_MAX_SECONDS
      description: Proactively reconnect the listener's IMAP session after this many seconds
      required: false
      type: string
      default: '1500'
  events:
    event_type:
      - name: new_email
        description: Event of receiving a new email
  long_running_app:
    command: uv run scripts/event_listener.py
    description: Persistent listener for new emails. Drops incoming messages as markdown.
---

# imap-email

**Purpose:** Python CLI over IMAP/SMTP (stdlib only). Auto-detects Gmail for extensions; falls back to standard IMAP. Runs via `uv` (PEP 723 inline metadata).

**This is the email-reading path in Cremind.** The `gmail` skill can only *send*:
Google classes every Gmail scope that can return message content as "restricted"
(including headers-only metadata), and the watch/history APIs behind push
notifications accept only those scopes, so Cremind's shared OAuth client requests
none of them. Reading, searching, and new-mail events all live here — and this
works with any provider, not just Gmail.

## Setup

You need an **app password**, not your normal account password, on any provider
with 2-factor authentication enabled. Put the credentials in `scripts/.env` (or
fill them in via **Settings → Tools & Skills → imap-email**):

```
USERNAME=you@example.com
PASSWORD=your-app-password
IMAP_HOST=imap.example.com
SMTP_HOST=smtp.example.com
# Optional overrides — defaults are 993 (IMAP SSL) and 587 (SMTP STARTTLS)
# IMAP_PORT=993
# SMTP_PORT=587
```

### Where to get an app password

| Provider | `IMAP_HOST` | `SMTP_HOST` | App password |
|---|---|---|---|
| Gmail / Google Workspace | `imap.gmail.com` | `smtp.gmail.com` | <https://myaccount.google.com/apppasswords> (2FA required) |
| Outlook.com / Hotmail / Office 365 | `outlook.office365.com` | `smtp.office365.com` | <https://account.microsoft.com/security> (some tenants require OAuth2) |
| Yahoo Mail | `imap.mail.yahoo.com` | `smtp.mail.yahoo.com` | Account Security settings |
| iCloud Mail | `imap.mail.me.com` | `smtp.mail.me.com` | <https://appleid.apple.com> |
| Fastmail | `imap.fastmail.com` | `smtp.fastmail.com` | <https://app.fastmail.com/settings/security> |
| Zoho Mail | `imap.zoho.com` | `smtp.zoho.com` | Zoho account security |
| Custom / self-hosted | your provider's hostnames | your provider's hostnames | whatever your admin configured |

On Gmail, an app password gives this skill full mailbox access — so `list`,
`search`, and `get` do everything the old Gmail read verbs did, plus Gmail's own
search grammar (see below).

> **Windows note:** `USERNAME` must come from `.env` or the Settings UI. Windows
> defines `USERNAME` as the OS login name, so an inherited value is ignored unless
> it looks like an email address — otherwise a missing credential would surface as
> a confusing authentication failure. If your IMAP username is not an address, set
> it in `.env`.

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (or human-readable on TTY; force with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `list` | — | `--max-results` (10), `--query`, `--detail title_only\|summary\|full`, `--category primary\|promotions\|social\|updates\|forums\|spam\|all`, `--since/--before YYYY-MM-DD` |
| `search` | `--query` | `--max-results` (10), `--detail title_only\|summary\|full`, `--since/--before YYYY-MM-DD` |
| `list-sent` | — | `--max-results`, `--since`, `--before` |
| `send` | `--to` (repeatable), `--subject` | `--cc`, `--bcc` (repeatable), `--body`/`--body-file`/stdin |
| `reply` | `--message-id`, body | `--cc`, `--bcc` |
| `trash` | `--message-id` | — |
| `get` | `--message-id` | — |

`--message-id` = RFC822 Message-ID (angle brackets optional). Resolved to UID at op time.

### Searching

Use **`search`** to look through the whole mailbox and **`list`** to look at one
folder. `search --query "..."` is `list` pinned to All Mail: it covers every
category tab plus everything archived, and requires a query. `list --query "..."`
searches only the folder `--category` selected (INBOX by default), which is what
you want for "what's in my inbox right now".

`--query` on a **Gmail** server accepts Gmail's full search grammar (it is passed
through as `X-GM-RAW`), so `search --query "from:alice newer_than:7d is:unread"`
works as it would in the Gmail UI. On other servers each whitespace-separated term
becomes an IMAP `TEXT` search across headers and body.

### Sending a Gmail reply through the gmail skill

The `message_id` this skill reports (from `get`, `list`, or a `new_email` event)
is exactly what the send-only `gmail` skill needs to thread a reply:

```bash
uv run scripts/__main__.py get --message-id "<CABc123@mail.gmail.com>"
# then, in the gmail skill:
#   reply --to alice@example.com --subject "Lunch?" \
#         --in-reply-to "<CABc123@mail.gmail.com>" --body "Sounds good."
```

Replying with this skill's own `reply` works too and needs no second account.

## Event listener
```bash
uv run scripts/event_listener.py
```
Polls the INBOX every `POLL_INTERVAL` seconds (default 30) and writes new messages
to `events/new_email/<subject>.md`.

- **Baseline on start**: the listener starts from the current end of the mailbox,
  so mail that arrived while it was down is **not** replayed. A restart therefore
  drops anything in flight — expected, and it keeps a restart from dumping the
  whole mailbox into events.
- **State**: `scripts/.listener_state.json` (`last_seen_uid`, `uidvalidity`),
  heartbeat in `scripts/.listener_heartbeat`. A server-side `UIDVALIDITY` change
  re-baselines.
- **Reconnects**: proactively every `RECONNECT_MAX_SECONDS` (default 1500), plus
  exponential backoff on failure.

## Troubleshooting highlights
- `AUTHENTICATIONFAILED` → you need an **app password**, not your login password,
  when 2FA is on. See the table above.
- `Missing required env var(s): USERNAME` on Windows → set `USERNAME` in `.env` or
  the Settings UI (see the Windows note above).
- `--category primary` empty on Gmail → tabs disabled; CLI auto-retries without filter; try `--category all`, or use `search` (which always covers all mail).
- Expected mail missing from `list` → it is probably archived or in another tab; `search --query "..."` covers the whole mailbox.
- HTML-only email noisy → use `get` for raw HTML (`body_html` in JSON).
- No events arriving → confirm the listener is running (`cremind skill-events
  listener-start imap-email`) and that the heartbeat file is fresh.
