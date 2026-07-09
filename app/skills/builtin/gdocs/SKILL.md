---
name: gdocs
description: Read Google Docs as markdown or plain text, create documents, append text, and find-and-replace across a document via OAuth2. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. Accepts document URLs or ids. Execution-only — to search/list documents or watch for changes, use the gdrive skill.
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
> `mime_type`, so you can filter to Docs). To list/search documents, use gdrive
> (`list --mime-type application/vnd.google-apps.document`).

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
| `create` | `--title` | `--text` / `--file` (initial content; else stdin) |
| `read` | `--id` | `--format markdown\|text\|json` (default markdown) |
| `info` | `--id` | — |
| `append` | `--id` + text via `--text`/`--file`/stdin | — |
| `replace` | `--id`, `--find`, `--replace-with` | `--match-case` |

`--id` accepts a bare document id or a full document URL.

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
