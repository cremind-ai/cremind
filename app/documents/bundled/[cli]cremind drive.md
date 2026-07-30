---
description: "Grant Cremind access to individual **Google Drive files** and list the Drive files it can reach. Cremind holds **per-file** Drive access (the `drive.file` OAuth scope), so it can only open files the user explicitly picked through Google's file picker plus files Cremind created — knowing a Drive link is never enough, and there is no whole-Drive search. Use this when a Drive file returns a 403/404, when the user pastes a Drive URL Cremind cannot read, or to review which files have been granted."
---

# `cremind drive` — per-file Google Drive access

`cremind drive` grants and inspects **per-file** Google Drive access.

Cremind requests only the `https://www.googleapis.com/auth/drive.file` scope. It
therefore reaches **only**:

1. files the user explicitly picked through Google's file picker, and
2. files Cremind itself created.

**Knowing a file's id or URL is never enough.** A file that was never granted
returns 403/404 from every Drive call, and Google does not distinguish "not
granted" from "does not exist". The fix is a grant, never a retry.

Linking the Google account itself belongs to the **gdrive skill** (it owns the
OAuth token). These commands grant files on an already-linked account. If
`cremind drive status` says the account is not linked, ask the agent to link the
gdrive skill first.

## Finding this in the web UI

> **Sidebar → Settings → Google Drive** — the same status, the **Grant access**
> button (`grant`), the granted-file table (`files`), and the paste-the-redirect
> fallback (`grant-complete`).

## Commands

| Command | Arguments | What it does |
|---|---|---|
| `status` | — | Link state, the granted account, and whether a re-link is needed |
| `files` | `--page-token`, `--page-size` (50) | Lists the Drive files Cremind can reach |
| `grant` | `--file`, `--single`, `--no-folders`, `--mime-type`, `--no-browser`, `--print-only`, `--timeout` (600) | Opens the file picker so the user grants files |
| `grant-complete` | `<redirect-url>` | Finishes a grant from the URL the browser landed on |

### `status`

```bash
cremind drive status
```

Reports the linked Google account and the access model. When the account was
linked before per-file access existed, `scopes_stale` is true and the output says
to re-link: ask the agent to run the gdrive skill's `link` verb, then grant files
again. Old grants are not lost by re-linking.

### `files`

```bash
cremind drive files
cremind drive files --page-size 100 --json
```

This is the authoritative list — it asks Google what the token can see, so it
reflects exactly what Cremind can open. An empty list means nothing has been
granted yet.

### `grant`

```bash
cremind drive grant                                  # user picks anything
cremind drive grant --file https://docs.google.com/document/d/FILE_ID/edit
cremind drive grant --file FILE_ID --single
cremind drive grant --mime-type application/vnd.google-apps.spreadsheet
cremind drive grant --print-only                     # just print the URL
```

Prints a Google URL and opens it in a browser, then waits for the user to pick
files and approve. `--file` pre-selects a specific file — use it when the user has
already named a file Cremind cannot read yet. Accepts a bare id or any Drive,
Docs, Sheets, or Slides URL.

The grant takes effect the moment the user approves, so the command detects the
result even on installs where the browser cannot reach this server. If the final
redirect fails and nothing is detected, the command offers to take the URL the
browser landed on; `--json` and non-interactive runs are told to use
`grant-complete` instead.

Grants are permanent until the user revokes Cremind at
<https://myaccount.google.com/connections>, which removes **all** of them —
Google offers no per-file revoke, so neither does this command.

### `grant-complete`

```bash
cremind drive grant-complete "http://localhost:1515/api/oauth/google-drive/callback?state=...&picked_file_ids=..."
```

For remote installs: the user completes the picker in their own browser, copies
the URL it landed on (even if the page showed a connection error), and passes it
here.

## Unattended runs

**Never start a grant from an event run, scheduled run, or file watcher.** It
waits for a browser consent nobody will complete. When an unattended run hits a
file Cremind cannot reach, notify the user (name the file and point them at
Settings → Google Drive or `cremind drive grant --file <id>`) and stop the run.

## What per-file access cannot do

- **No whole-Drive search.** `files` lists the granted set, not the user's Drive.
  To act on a file the user names, ask for the URL or run `grant`.
- **No whole-Drive monitoring.** The gdrive skill's `file_changed` events cover
  granted files only.
- **No finding a Sheet or Doc by name.** Ask for the URL or id — the **gsheets**
  and **gdocs** skills read and write any file the user owns from a URL alone,
  with no Drive grant needed at all.

## Troubleshooting

- `Google Drive is not linked` → ask the agent to link the gdrive skill.
- `scopes_stale: true` → re-link the gdrive skill, then re-grant.
- A picked file still unreadable → the approval may have used a different Google
  account than the linked one; `status` shows which email is linked.
- Grant seems to do nothing on a remote install → use `grant-complete`, or run
  `cremind drive grant` from the machine with the browser.
