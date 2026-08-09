---
name: gmail
description: Send Gmail messages and in-thread replies via OAuth2, using the send-only gmail.send scope. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. Reading email, searching it, and receiving new-email events are NOT available here - use the imap-email skill for all of those. The list, search, and get verbs work only with bring-your-own Google credentials.
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
      description: Space-separated OAuth scopes to request at link. Only useful with your own OAuth client - it is how a bring-your-own-credentials user asks for a Gmail read scope.
      required: false
      type: string
      default: ''
---

# gmail

**Purpose:** Python CLI for **sending** Gmail over OAuth2. Authorization goes
through the **Cremind Connect** service (`connect.cremind.io`) so you never touch
GCP. The OAuth code→token exchange happens locally (loopback PKCE); **tokens are
stored only on this machine** (`scripts/.google_token.json`) and the relay never
sees them. Runs via `uv` (PEP 723 inline metadata).

## Send-only — read email with imap-email

This skill holds `https://www.googleapis.com/auth/gmail.send` and nothing more.
It **cannot** read, search, or list mail, and there are no new-email events.

That is not an oversight: Google classes *every* Gmail scope that can return
message content as "restricted" — including headers-only metadata — and the
watch/history APIs behind push notifications accept only those scopes. Restricted
scopes require a recurring paid third-party security assessment, so Cremind's
shared OAuth client does not request any.

**To read, search, or react to email, use the `imap-email` skill** (IMAP/SMTP with
an app password). It covers everything this skill dropped:

| You need to… | Use |
|---|---|
| List or search mail | `imap-email` `list` (one folder) or `search` (all mail) — on Gmail accounts both accept Gmail's own search grammar |
| Read one message | `imap-email` `get --message-id <id>` |
| React to new mail | `imap-email`'s `new_email` events |
| Send mail | **this skill** (`send`) or `imap-email` `send` |
| Reply in-thread | **this skill** (`reply`, see below) or `imap-email` `reply` |

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
GOOGLE_SCOPES=                                   # optional; only with your own OAuth client
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
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON (human-readable on a TTY; force JSON with `--json`).

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | `--no-browser` |
| `complete-link` | `--response` | — |
| `status` | — | — |
| `send` | `--to` (repeatable), `--subject` | `--cc`, `--bcc` (repeatable), `--body`/`--body-file`/stdin |
| `reply` | `--to`, `--subject`, `--in-reply-to` | `--references`, `--thread-id`, `--cc`, `--bcc`, body via `--body`/`--body-file`/stdin |
| `list` **(BYO only)** | — | `--query`, `--max-results` (10), `--detail summary\|full` |
| `search` **(BYO only)** | `--query` | `--max-results` (10), `--detail summary\|full` |
| `get` **(BYO only)** | `--id` | — |

**BYO only** = requires a Gmail read scope, which only a bring-your-own Google
OAuth client can request. Without one these exit with code 2 and a
`scope_not_granted` error pointing at imap-email.

### Replying in-thread

`reply` takes the threading headers from you, because it cannot look the original
message up:

```bash
uv run scripts/__main__.py reply \
  --to alice@example.com \
  --subject "Lunch?" \
  --in-reply-to "<CABc123@mail.gmail.com>" \
  --body "Sounds good."
```

- `--in-reply-to` is the original's **RFC822 Message-ID**. Get it from the
  imap-email skill: the `message_id` field of a `new_email` event, or of
  `imap-email get --message-id ...`.
- `--references` defaults to `--in-reply-to`; pass the full chain if you have it.
- `Re: ` is added to the subject automatically when missing.
- `--thread-id` is optional. Mail clients thread on the headers plus a matching
  subject, so replies group correctly without it; there is no way to discover a
  Gmail thread id under send-only anyway.

`--id` still works on a bring-your-own-credentials account with a read scope,
looking the original up the old way.

## Examples
```bash
uv run scripts/__main__.py status
uv run scripts/__main__.py send --to a@b.com --subject "Hi" --body "Hello there"
uv run scripts/__main__.py reply --to a@b.com --subject "Hi" \
  --in-reply-to "<CABc123@mail.gmail.com>" --body "Thanks!"
```

## Bring your own Google credentials

Creating your own Google Cloud project and OAuth client falls under Google's
personal-use exception (fewer than 100 users), which needs neither verification
nor a security assessment — so you *can* request read scopes there. Set
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and
`GOOGLE_SCOPES="openid email https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"`
in `scripts/.env`, then re-run `link`. `status` will then show `can_read: true`
and the read verbs work.

Two caveats: set your OAuth app's publishing status to **Production without
submitting for verification** ("Testing" expires refresh tokens after 7 days and
silently breaks automation), and note that new-email **events** still won't work —
the Cremind relay only accepts ID tokens issued to the shared client. See the
Cremind docs, *Setup → Bring your own Google credentials*.

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- `scope_not_granted` (exit 2) → that verb needs a read scope. Use the
  `imap-email` skill, or bring your own credentials (above).
- `No GOOGLE_CLIENT_SECRET available` → cremind-connect must be reachable (it
  serves the secret), or set it in `scripts/.env` to override.
- `Google did not return a refresh token` → revoke at <https://myaccount.google.com/permissions> and re-link.
- `stale_scopes: true` in `status` → the account was linked with scopes Cremind no
  longer requests. It keeps working until Google retires the grant; re-run `link`
  to move to the current set. Re-linking is **one-way on the shared client** —
  read scopes cannot be re-granted there, so bring your own client with
  `GOOGLE_SCOPES` (above) first if you want to keep them.
- `expected_unresolved: true` in `status` → cremind-connect was unreachable, so the
  stale-scope check was skipped. Not a problem to fix; just don't advise a re-link.
- A reply didn't thread → check `--in-reply-to` carries the full Message-ID
  including the angle brackets, and that the subject matches the original.

## Module layout
```
gmail/
├── SKILL.md
└── scripts/
    ├── .env                          # optional overrides (creds fetched from cremind-connect by default)
    ├── __main__.py                   # CLI entry
    ├── tests/test_account_key.py     # cross-repo routing-key parity test
    └── app/
        ├── config.py                 # env + paths + logging
        ├── gmail_api.py              # Gmail API wrapper (send/reply; read verbs for BYO)
        ├── formatter.py              # message parsing + markdown
        ├── cli.py                    # argparse + dispatch
        └── google/                   # shared: account_key, discovery, auth (PKCE)
```
