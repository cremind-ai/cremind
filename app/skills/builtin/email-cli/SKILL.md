---
name: email-cli
description: Send, receive, search, list, reply, and trash email messages from the CLI via IMAP/SMTP. Works with any IMAP/SMTP provider (Gmail, Outlook, Yahoo, iCloud, Fastmail, custom servers). A persistent event listener drops incoming messages as markdown.
metadata: {
  environment_variables: ["USERNAME", "PASSWORD", "IMAP_HOST", "SMTP_HOST"],
  events: {"event_type":[{"name":"new_email","description":"Event of receiving a new email"}]},
  long_running_app: {
    command: "uv run scripts/event_listener.py",
    description: "Persistent listener for new emails. Drops incoming messages as markdown.",
  }
}
---

# email-cli

**Purpose:** Python CLI over IMAP/SMTP (stdlib only). Auto-detects Gmail for extensions; falls back to standard IMAP. Runs via `uv` (PEP 723 inline metadata).

## Setup
Credentials in `scripts/.env`:
```
USERNAME, PASSWORD, IMAP_HOST, SMTP_HOST
# Optional: IMAP_PORT (993), SMTP_PORT (587)
```
Most providers require an App Password when 2FA is on. Common hosts: `imap.gmail.com`/`smtp.gmail.com`, `outlook.office365.com`/`smtp.office365.com`, `imap.mail.yahoo.com`, `imap.mail.me.com`, `imap.fastmail.com`, `imap.zoho.com`.

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (or human-readable on TTY; force with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `list` | — | `--max-results` (10), `--query`, `--detail title_only\|summary\|full`, `--category primary\|promotions\|social\|updates\|forums\|spam\|all`, `--since/--before YYYY-MM-DD` |
| `list-sent` | — | `--max-results`, `--since`, `--before` |
| `send` | `--to` (repeatable), `--subject` | `--cc`, `--bcc` (repeatable), `--body`/`--body-file`/stdin |
| `reply` | `--message-id`, body | `--cc`, `--bcc` |
| `trash` | `--message-id` | — |
| `get` | `--message-id` | — |

`--message-id` = RFC822 Message-ID (angle brackets optional). Resolved to UID at op time.

## Troubleshooting highlights
- `AUTHENTICATIONFAILED` → need App Password with 2FA.
- `--category primary` empty on Gmail → tabs disabled; CLI auto-retries without filter; try `--category all`.
- HTML-only email noisy → use `get` for raw HTML (`body_html` in JSON).
