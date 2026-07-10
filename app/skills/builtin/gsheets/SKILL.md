---
name: gsheets
description: Read, write, append to, and clear Google Sheets ranges, create spreadsheets, and inspect sheet tabs via OAuth2. Authorizes through the Cremind Connect service (no GCP setup); tokens stay on this machine. Accepts spreadsheet URLs or ids and A1-notation ranges; values are JSON 2D arrays. Execution-only — for file-level change events on a spreadsheet, use the gdrive skill.
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
    - name: SPREADSHEET_ID
      description: Default spreadsheet id (or URL) for single-workbook workflows; --spreadsheet overrides per command
      required: false
      type: string
      default: ''
---

# gsheets

**Purpose:** Python CLI for **Google** Sheets over OAuth2. Authorization goes
through the **Cremind Connect** service (`connect.cremind.io`) so you never touch
GCP. The OAuth code→token exchange happens locally (loopback PKCE); **tokens are
stored only on this machine** (`scripts/.google_token.json`). Runs via `uv`
(PEP 723 inline metadata).

> **Execution-only skill.** Google offers no push API for spreadsheet content, so
> this skill has no event listener. To be notified when a spreadsheet changes,
> subscribe to the **gdrive** skill's `file_changed` event (it carries the file's
> `mime_type`, so you can filter to spreadsheets).

## How it works

All verbs call the Sheets API v4 directly with your local token. Scope is
least-privilege `https://www.googleapis.com/auth/spreadsheets` (fetched from
Cremind Connect, with a built-in fallback).

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
SPREADSHEET_ID=                                  # optional default workbook (id or URL)
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
| `create` | `--title` | `--tab NAME` (repeatable initial tabs) |
| `info` | — | `--spreadsheet` (id or URL) |
| `read` | `--range` (repeatable) | `--spreadsheet`, `--render formatted\|unformatted\|formula` |
| `update` | `--range`, values | `--spreadsheet`, `--raw` |
| `append` | `--range`, values | `--spreadsheet`, `--raw` |
| `clear` | `--range` | `--spreadsheet` |

`--spreadsheet` accepts a bare id or a full spreadsheet URL. When omitted, the
`SPREADSHEET_ID` env var is used (else the command errors).

## Ranges & values

- **Ranges** use A1 notation: `Sheet1!A1:D10`, a whole tab `Sheet1`, or an
  open-ended `Sheet1!A2:B` (from row 2 down). `read` takes `--range` repeatably
  (batch read). `append`'s range is the table anchor — new rows go after the last
  populated row.
- **Values** for `update`/`append` are a **JSON 2D array** (list of rows). Provide
  them one of three ways:
  - **Prefer `--values-file PATH`** — write the JSON to a temp file and pass its
    path. The only thing on the command line is a plain path, so this is **quote-safe
    on every shell** and is the right default whenever the data may contain quotes,
    apostrophes, commas, or newlines.
  - ⚠️ **Avoid inline `--values '…'` for anything but tiny, quote-free payloads.**
    This machine's shell is **PowerShell**, where an apostrophe inside a single-quoted
    string (e.g. `That's`, `I'm`, `you're`) *terminates the string early* and the whole
    command fails with `The string is missing the terminator: "` — the script never
    runs. Inline is fine only for payloads like `"[[1,2,3]]"` with no quotes/apostrophes.
  - **stdin** also works when neither flag is given; feed it through the exec-shell
    `stdin` channel — **not** `echo '…' | …`, which re-introduces the same PowerShell
    quoting hazard.
  - Default input mode is `USER_ENTERED` (formulas/dates are parsed like typing in the
    UI); pass `--raw` to store strings verbatim.

## Examples
```bash
uv run scripts/__main__.py create --title "Q3 tracker" --tab Data --tab Summary
uv run scripts/__main__.py info --spreadsheet https://docs.google.com/spreadsheets/d/ABC123/edit
uv run scripts/__main__.py read --spreadsheet ABC123 --range 'Data!A1:C' --render unformatted
# write values: put the JSON 2D array in a file, then pass --values-file (quote-safe on any shell).
#   values.json contents: [["Ada","It's great!"],["Lin",87]]
uv run scripts/__main__.py update --spreadsheet ABC123 --range 'Data!A2' --values-file values.json
uv run scripts/__main__.py append --spreadsheet ABC123 --range Data --values-file values.json
# inline --values is OK only for tiny, quote-free payloads:
uv run scripts/__main__.py append --spreadsheet ABC123 --range Data --values "[[1,2,3]]"
uv run scripts/__main__.py clear --spreadsheet ABC123 --range 'Data!A2:C'
```

## Not in this skill (v1)
- No cell formatting, add/delete-tab, or chart operations (the `batchUpdate`
  surface) — read/write values and create workbooks only.
- No listing of spreadsheets — that is a Drive operation; use the **gdrive** skill
  (`list --mime-type application/vnd.google-apps.spreadsheet`).

## Troubleshooting
- `The string is missing the terminator: "` (PowerShell) or `--values must be a JSON
  2D array …` → your JSON was mangled by shell quoting before the script ran. Don't
  inline it with `--values '…'`; write the array to a file and pass `--values-file PATH`.
- `Account not linked` → run `uv run scripts/__main__.py link`.
- `No GOOGLE_CLIENT_SECRET available` → cremind-connect must be reachable (it
  serves the secret), or set it in `scripts/.env` to override.
- `No spreadsheet specified` → pass `--spreadsheet <id-or-url>` or set
  `SPREADSHEET_ID` in `scripts/.env`.

## Module layout
```
gsheets/
├── SKILL.md
└── scripts/
    ├── .env
    ├── __main__.py                  # CLI entry
    ├── tests/test_account_key.py    # cross-repo routing-key parity test
    └── app/
        ├── config.py
        ├── sheets_api.py            # Sheets API v4 wrapper (values + metadata)
        ├── cli.py                   # argparse + dispatch
        └── google/                  # shared: account_key, discovery, auth (PKCE), relay_client
```
