# email-cli

A Python CLI that talks directly to any IMAP/SMTP mail server using the Python standard library (`imaplib` + `smtplib` + `email`). No vendor SDKs, no OAuth flow — just IMAP/SMTP credentials. Gmail-specific extensions (`X-GM-RAW`, `X-GM-LABELS`, category tabs) are used automatically when the server advertises them, and standard IMAP behaviors are used otherwise.

Run via [`uv`](https://docs.astral.sh/uv/). Each entry script carries PEP 723 inline metadata so `uv` auto-provisions a venv; no `pyproject.toml` needed.

## Prerequisites

- [`uv`](https://docs.astral.sh/uv/) installed.
- An email account with IMAP and SMTP enabled.
- An **App Password** (required by most providers when 2-Factor Authentication is on) or a regular IMAP/SMTP password.
- Credentials in [scripts/.env](scripts/.env):
  ```
  USERNAME=you@example.com
  PASSWORD=your-app-password
  IMAP_HOST=imap.example.com
  SMTP_HOST=smtp.example.com
  # Optional overrides — defaults are 993 (IMAP SSL) and 587 (SMTP STARTTLS)
  # IMAP_PORT=993
  # SMTP_PORT=587
  ```

### Common provider settings

| Provider | `IMAP_HOST` | `SMTP_HOST` | Auth notes |
|---|---|---|---|
| Gmail / Google Workspace | `imap.gmail.com` | `smtp.gmail.com` | App Password at <https://myaccount.google.com/apppasswords> (2FA required) |
| Outlook.com / Hotmail / Office 365 | `outlook.office365.com` | `smtp.office365.com` | App Password at <https://account.microsoft.com/security>; some tenants require OAuth2 |
| Yahoo Mail | `imap.mail.yahoo.com` | `smtp.mail.yahoo.com` | App Password at Account Security settings |
| iCloud Mail | `imap.mail.me.com` | `smtp.mail.me.com` | App-specific password at <https://appleid.apple.com> |
| Fastmail | `imap.fastmail.com` | `smtp.fastmail.com` | App Password at <https://app.fastmail.com/settings/security> |
| Zoho Mail | `imap.zoho.com` | `smtp.zoho.com` | App Password in Zoho account security |
| Custom / self-hosted | your provider's hostnames | your provider's hostnames | whatever your admin configured |

Ports default to 993 (IMAP SSL) and 587 (SMTP STARTTLS). Override with `IMAP_PORT` / `SMTP_PORT` if your provider differs.

## One-shot CLI commands

Run from the skill root. Every command prints JSON to stdout (pipe-friendly); human-readable output only when attached to a TTY.

```bash
# List recent inbox messages (default detail=summary)
uv run scripts/__main__.py list --max-results 10

# Filtered list
uv run scripts/__main__.py list --query "alice invoice" --since 2026-04-01 --before 2026-04-30
uv run scripts/__main__.py list --detail title_only

# Full content of one email (body included)
uv run scripts/__main__.py get --message-id "<abc@mail.example.com>"

# Sent folder
uv run scripts/__main__.py list-sent --max-results 5

# Compose a new email
uv run scripts/__main__.py send \
    --to bob@example.com --to carol@example.com \
    --cc dave@example.com \
    --subject "Hello" --body "Plain-text body here."

# Compose with body piped in (useful for long messages)
echo "multi-line body" | uv run scripts/__main__.py send --to bob@example.com --subject "Hi"

# Reply (preserves threading via In-Reply-To / References)
uv run scripts/__main__.py reply --message-id "<abc@mail.example.com>" --body "Thanks!"

# Trash
uv run scripts/__main__.py trash --message-id "<abc@mail.example.com>"
```

### Flag reference

| Subcommand | Required | Optional |
|---|---|---|
| `list` | — | `--max-results N` (10), `--query STR`, `--detail title_only\|summary\|full` (summary), `--category primary\|promotions\|social\|updates\|forums\|spam\|all` (primary), `--since YYYY-MM-DD`, `--before YYYY-MM-DD` |
| `list-sent` | — | `--max-results N` (10), `--since`, `--before` |
| `send` | `--to` (repeatable), `--subject` | `--cc` (repeatable), `--bcc` (repeatable), `--body` / `--body-file` / stdin |
| `reply` | `--message-id`, body (via `--body`/`--body-file`/stdin) | `--cc`, `--bcc` |
| `trash` | `--message-id` | — |
| `get` | `--message-id` | — |

Global: `--json` to force JSON output even on a TTY.

### Provider-aware behavior

The CLI auto-detects Gmail at login (via the `X-GM-EXT-1` IMAP capability) and branches where Gmail differs from standard IMAP:

| Behavior | Gmail | Other IMAP providers |
|---|---|---|
| `--query` | passed through `X-GM-RAW` → full Gmail search grammar (`from:`, `has:attachment`, `before:`, etc.) | each whitespace-separated term matched with IMAP `TEXT` (headers + body) |
| `--category primary\|promotions\|...` | filters by Gmail category tab | ignored (always INBOX) |
| `--category spam` / `all` | selects `\Junk` / `\All` folders | selects `\Junk` / `\All` if the server advertises them, else INBOX |
| `trash` | atomic `+X-GM-LABELS \Trash` | COPY to `\Trash` folder + set `\Deleted` + EXPUNGE |
| `send` / `reply` | Gmail auto-saves SMTP-sent messages to `[Gmail]/Sent Mail` | the CLI APPENDs the sent message to the `\Sent` folder after SMTP send |

### About `--message-id`

`--message-id` is the RFC822 `Message-ID` header (e.g. `<abc@mail.example.com>`). Copy it from the output of `list` or `get`. Angle brackets are optional — both `<abc@host>` and `abc@host` work. The CLI resolves it to the current IMAP UID at operation time by searching INBOX first, then the All-Mail folder if one is advertised.

## Event listener

Run persistently to capture every new inbound message:

```bash
uv run scripts/event_listener.py
```

Behavior (same on all providers):
- **First run baseline**: records the current `UIDNEXT - 1` as `last_seen_uid` and only processes messages that arrive *after* that point. Historical mail is not retroactively dumped.
- **Polling**: checks INBOX every 30 seconds (override with `POLL_INTERVAL` env var).
- **Output**: each new email is written to [events/new_email/](events/new_email/) as `<subject>.md`. The subject is sanitized for the filesystem (Windows-invalid chars `<>:"/\|?*` and control chars become `_`, whitespace is collapsed, length capped at 100 chars, Windows reserved names like `CON`/`NUL` get an underscore prefix). Empty subjects fall back to `no-subject.md`. On collision the listener appends ` (2)`, ` (3)`, … to keep the create atomic.
- **State**: persisted to `scripts/.listener_state.json` (`{last_seen_uid, uidvalidity}`). Gitignored. Delete it to force a re-baseline.
- **Reconnect**: exponential backoff on network errors; proactive reconnect every ~25 minutes to stay under most servers' idle timeouts.
- **Shutdown**: SIGINT / SIGTERM stops cleanly.

### Event markdown schema

Each `events/new_email/<subject>.md` has YAML frontmatter followed by the decoded plain-text body (HTML is stripped if the message has no `text/plain` part):

```markdown
---
message_id: "<abc@mail.example.com>"
gm_msgid: "17891234567890123"
from: "Alice Example <alice@example.com>"
to: "you@example.com"
cc: ""
subject: "Meeting Thursday"
date: "2026-04-24T10:15:03+07:00"
received_at: "2026-04-24T10:15:07+07:00"
labels: ["\\Inbox", "CATEGORY_PERSONAL"]
---

Hi — let's meet Thursday at 2pm.
```

Notes:
- `gm_msgid` is Gmail-only; empty string (`""`) on other providers.
- `labels` holds Gmail labels on Gmail; on other providers it holds standard IMAP flags (e.g. `\Seen`, `\Flagged`, `\Answered`).
- `message_id` is suitable for feeding back into `reply --message-id ...` / `trash --message-id ...` / `get --message-id ...`.

## Troubleshooting

- **`Missing required env var(s): IMAP_HOST, SMTP_HOST`** — add them to `scripts/.env` using the Common provider settings table above.
- **`IMAP login failed ... AUTHENTICATIONFAILED`** — most providers require an App Password (not the account's web login password) when 2FA is enabled. Generate one from your provider's security settings.
- **Listener processes nothing** — baseline is working as designed; send yourself a new email and wait up to `POLL_INTERVAL` seconds. Inspect `scripts/.listener_state.json` to see the current `last_seen_uid`.
- **Listener logs `UIDVALIDITY changed`** — the server rebuilt the mailbox index (rare). Listener rebaselines automatically; a few in-flight messages may be missed during the switchover.
- **`list --category primary` returns nothing** (Gmail only) — the account may have category tabs disabled. The CLI automatically retries without the category filter; if you still see nothing, try `--category all`.
- **`No Sent folder advertised by this IMAP server`** — the server didn't return a `\Sent` special-use mailbox in its LIST response. Some older/self-hosted servers need this enabled. Your sent messages will still send, but `list-sent` and the auto-APPEND-to-Sent behavior need a Sent folder to target.
- **`No Trash folder advertised by this IMAP server`** — same root cause, but affects `trash`. Enable `SPECIAL-USE` on your server, or contact your admin.
- **HTML-only marketing emails look noisy** — the stdlib HTML-to-text stripper is intentionally simple. Use `get --message-id ...` if you need the raw HTML (`body_html` field in JSON output).

## Module layout

```
email-cli/
├── SKILL.md                         # this file
├── events/
│   └── new_email/                   # markdown drop-zone for the listener (one <subject>.md per inbound email)
└── scripts/
    ├── .env                         # USERNAME, PASSWORD, IMAP_HOST, SMTP_HOST
    ├── __main__.py                  # CLI entry point (uv run scripts/__main__.py ...)
    ├── event_listener.py            # listener entry point (uv run scripts/event_listener.py)
    └── app/
        ├── config.py                # env + paths + logging
        ├── imap_client.py           # IMAP connect, capability detection, folder discovery, fetch/search/trash/append
        ├── smtp_client.py           # SMTP send (STARTTLS)
        ├── search.py                # build IMAP SEARCH criteria (Gmail X-GM-RAW vs standard TEXT)
        ├── message.py               # RFC822 parse + outgoing message/reply builders
        ├── formatter.py             # list rows + event markdown rendering
        ├── operations.py            # verbs: list / list-sent / send / reply / trash / get
        ├── listener.py              # polling loop, state file, reconnect, event writer
        └── cli.py                   # argparse builder + dispatch
```