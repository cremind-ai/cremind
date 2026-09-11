---
name: gdocs
description: Read Google Docs as markdown or plain text, create documents, append text, and find-and-replace across a document via OAuth2. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. Accepts document URLs or ids and reaches any document the user owns from a URL alone, with no Drive access needed. Execution-only — for file-level change events on a document, use the gdrive skill. There is no search-by-name; ask the user for the URL or id.
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
---

# gdocs

**Purpose:** Python CLI for **Google** Docs over OAuth2. Authorization goes
through the **Cremind Connect** service (`connect.cremind.io`) so you never touch
GCP. The OAuth code→token exchange happens locally (loopback PKCE); **tokens are
stored only on this machine** (`scripts/.google_token.json`). Runs via `uv`
(PEP 723 inline metadata).

> **Execution-only skill.** Google offers no push API for document content, so
> this skill has no event listener. To be notified when a document changes,
> subscribe to the **gdrive** skill's `file_changed` event (it carries the file's
> `mime_type`, so you can filter to Docs). Drive events only cover files the user
> granted to Cremind, so grant the document first: `gdrive grant --file <url>`,
> **Settings → GSuite**, or `cremind drive grant --file <url>`.

> **Finding a document.** There is no search-by-name anywhere in Cremind — that
> needed whole-Drive access, which Cremind no longer requests. Ask the user to paste
> the document **URL or id**; this skill reaches any document they own from that
> alone, with no Drive grant involved.

## How it works

All verbs call the Docs API v1 directly with your local token. Scope is
least-privilege `https://www.googleapis.com/auth/documents` (fetched from Cremind
Connect, with a built-in fallback).

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
```

Then link the account:
```bash
uv run scripts/__main__.py link
```
`link` prints a Google consent URL, then waits (in the background) for consent
to complete. **Surface that URL to the user and ask them to open it and approve
access.** Google then sends the browser to a loopback callback such as
`http://localhost:1515/api/oauth/callback` — always `http://`, because Google
accepts only an http loopback redirect here; on an HTTPS install Cremind forwards
it to its HTTPS handler. The always-running Cremind backend receives it, so
linking completes even though the command keeps running in the background. The
browser then shows Cremind's **response received** page (it may close itself or
return to the Cremind page the user started from). That only means the redirect
arrived — `link` itself confirms the account once it has exchanged the code.
Once the user says they've approved, confirm:
```bash
uv run scripts/__main__.py status
```
(`--no-browser` only affects standalone runs outside Cremind, which open their
own temporary loopback listener; under the app the URL is always printed for the
user, never opened.)

## CLI Commands
Run `uv run scripts/__main__.py <subcommand>`. Output is JSON.

| Subcommand | Required | Optional |
|---|---|---|
| `link` | — | `--no-browser` |
| `complete-link` | `--response` | — |
| `status` | — | — |
| `unlink` | — | `--no-revoke`, `--yes`, `--keep-siblings`, `--dry-run` |
| `create` | `--title` | `--text` / `--file` (initial content; else stdin) |
| `read` | `--id` | `--format markdown\|text\|json` (default markdown) |
| `info` | `--id` | — |
| `append` | `--id` + text via `--text`/`--file`/stdin | — |
| `replace` | `--id`, `--find`, `--replace-with` | `--match-case` |

`--id` accepts a bare document id or a full document URL.


## Unlinking a Google account

`unlink` is the inverse of `link`: it revokes Cremind's access at Google, then
deletes the local credentials.

```bash
uv run scripts/__main__.py unlink              # revoke at Google, then wipe locally
uv run scripts/__main__.py unlink --dry-run    # report what it would do, change nothing
uv run scripts/__main__.py unlink --no-revoke  # wipe locally, leave the grant live
```

- If the revoke fails, the local credentials are **still** deleted — Cremind can no
  longer use the account either way — and the result carries `revoked: false` with
  an `action_required` pointing at <https://myaccount.google.com/connections>.
  Re-running does **not** help: the refresh token is the only thing that can revoke
  a grant, and it has just been deleted.
- **Google revokes per app, not per skill.** Every Cremind Google skill shares one
  OAuth client, so a sibling skill linked to the same address loses access too.
  Those are listed under `siblings` and their now-dead tokens are cleaned up as
  well; `--keep-siblings` leaves them in place.
- Unlinking when nothing is linked succeeds (`unlinked: false`,
  `reason: "not_linked"`), so it is safe to repeat.
- `scripts/.env` is preserved — it holds configuration, not credentials.
- Operators can do the same across every Google skill at once with
  `cremind google unlink --all`.

## Markdown extraction

`read --format markdown` converts the document structure to markdown:
- **Headings** — TITLE/HEADING_1…HEADING_6 → `#`…`######` (SUBTITLE → `##`).
- **Lists** — ordered → `1.`, unordered → `-`, with 2-space indentation per
  nesting level.
- **Links** — `[text](url)`.
- **Tables** — GitHub pipe tables (cell newlines flattened to spaces).
- **Horizontal rules** — `---`; inline images → `[image]` placeholder.

**Not converted (v1):** bold/italic and other character styles; headers,
footers, and footnotes. Use `--format json` for the raw document resource if you
need everything.

## Examples
```bash
uv run scripts/__main__.py create --title "Meeting notes" --text "Attendees:\n- Ada\n- Lin"
uv run scripts/__main__.py read --id https://docs.google.com/document/d/ABC123/edit
uv run scripts/__main__.py read --id ABC123 --format text
echo "## Action items" | uv run scripts/__main__.py append --id ABC123
uv run scripts/__main__.py replace --id ABC123 --find "{{name}}" --replace-with "Ada" --match-case
```

## Troubleshooting
- `Account not linked` → run `uv run scripts/__main__.py link`.
- The browser shows a connection error after the user approves (a remote or
  Ingress install, or a `kubectl port-forward` that is not running) → while `link`
  is still waiting, have the user copy the **full** address from the browser's
  address bar, then run
  `uv run scripts/__main__.py complete-link --response "<url>"` (keep the double
  quotes — the URL contains `&`). It hands Google's response to the waiting
  `link`, which finishes the exchange; then run `status`. On an HTTPS install the
  callback is still an `http://localhost:<port>/api/oauth/callback` address
  (Google requires an http loopback redirect) that Cremind forwards to its HTTPS
  handler; if the browser warns about the certificate at that step, trust the
  Cremind CA on that device (Settings → HTTPS & Certificate) and reload, or use
  `complete-link` with the address shown.
- `No GOOGLE_CLIENT_SECRET available` → cremind-connect must be reachable (it
  serves the secret), or set it in `scripts/.env` to override.

## Module layout
```
gdocs/
├── SKILL.md
└── scripts/
    ├── .env
    ├── __main__.py                  # CLI entry
    ├── tests/test_account_key.py    # cross-repo routing-key parity test
    └── app/
        ├── config.py
        ├── docs_api.py              # Docs API v1 wrapper (get/create/append/replace)
        ├── extract.py               # document resource → markdown / plain text
        ├── cli.py                   # argparse + dispatch
        └── google/                  # shared: account_key, discovery, auth (PKCE), relay_client
```
