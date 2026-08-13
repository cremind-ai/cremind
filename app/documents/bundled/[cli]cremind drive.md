---
description: "Grant Cremind access to individual **Google Drive files**, list the Drive files it can reach, and check which Drive access model this account uses. By default Cremind holds **per-file** access (the `drive.file` OAuth scope), so it can only open files the user explicitly picked through Google's file picker plus files Cremind created — knowing a Drive link is never enough and there is no whole-Drive search. An account linked with bring-your-own Google credentials holds **whole-Drive** access instead, where every file is reachable and grants are unnecessary; `cremind drive status` reports which applies, so run it before concluding a file is out of reach. Use this when a Drive file returns a 403/404, when the user pastes a Drive URL Cremind cannot read, when asked what Drive files Cremind can see, or to review which files have been granted."
---

# `cremind drive` — Google Drive access

`cremind drive` grants and inspects Google Drive access.

**Run `cremind drive status` first** — there are two access models and they lead
to opposite conclusions:

**Per-file (default).** Cremind requests only the
`https://www.googleapis.com/auth/drive.file` scope, so it reaches **only**:

1. files the user explicitly picked through Google's file picker, and
2. files Cremind itself created.

**Knowing a file's id or URL is never enough.** A file that was never granted
returns 403/404 from every Drive call, and Google does not distinguish "not
granted" from "does not exist". The fix is a grant, never a retry.

**Whole-Drive.** The token holds the wider `.../auth/drive` scope. Every file is
reachable, `files` is a real whole-Drive listing, and `grant` is unnecessary — do
not run it. A 403/404 here means the file genuinely is missing, not ungranted.

Two different situations produce this, and `status` names which one in its
`Access:` line — do not assume the user configured anything:

- *"the shared Cremind client still requests it"* — the default for an account
  linked before Cremind narrowed its scopes. Nothing was configured by the user.
- *"your own Google credentials"* — the user really did supply their own OAuth
  client.

Linking the Google account itself belongs to the **gdrive skill** (it owns the
OAuth token). These commands grant files on an already-linked account. If
`cremind drive status` says the account is not linked, ask the agent to link the
gdrive skill first.

## Finding this in the web UI

> **Sidebar → Settings → GSuite**, the **Google Drive file access** group — the
> same status, the **Grant access** button (`grant`), the granted-file table
> (`files`), and the paste-the-redirect fallback (`grant-complete`). The
> **Accounts** group above it is where the Google account itself is unlinked
> (`cremind google unlink gdrive`).

## Commands

| Command | Arguments | What it does |
|---|---|---|
| `status` | — | Link state, the granted account, **which access model applies**, and whether a re-link is needed |
| `files` | `--page-token`, `--page-size` (50) | Lists the Drive files Cremind can reach |
| `grant` | `--file`, `--single`, `--no-folders`, `--mime-type`, `--no-browser`, `--print-only`, `--timeout` (600) | Opens the file picker so the user grants files |
| `grant-complete` | `<redirect-url>` | Finishes a grant from the URL the browser landed on |

### `status`

```bash
cremind drive status
```

Reports the linked Google account and the access model — `Access: per-file ...`
or `Access: whole-Drive (...)` with the reason in parentheses. **Check this before
answering "what can Cremind reach?" or deciding a file needs a grant**, and quote
the reason as given rather than inferring one.

When the account was linked before per-file access existed, `scopes_stale` is
true and the output says to re-link: ask the agent to run the gdrive skill's
`link` verb, then grant files again. **Re-linking is one-way on the shared
Cremind client** — it trades whole-Drive access for per-file access permanently,
and every file that was reachable before must then be granted individually. To
keep whole-Drive access, bring your own Google OAuth client (set `GOOGLE_SCOPES`
in the gdrive skill settings) *before* re-linking.

`expected_resolved: false` means cremind-connect could not be reached, so what
the next link would request is unknown. `scopes_stale` is never set in that
state — do not tell the user to re-link on the strength of a guess.

### `files`

```bash
cremind drive files
cremind drive files --page-size 100 --json
```

This is the authoritative list — it asks Google what the token can see, so it
reflects exactly what Cremind can open. Under per-file access an empty list means
nothing has been granted yet; under whole-Drive access this is a page of the
user's Drive, so present the first page and mention that more exist rather than
paging through everything.

Results are paginated: the output ends with a `--page-token` hint when more
files are available. A page that looks short is not an error — pass the token
only if the user asked for more.

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

Grants are permanent until Cremind's Drive access is revoked, which removes
**all** of them at once — Google offers no per-file revoke, so neither does this
command. Use **`cremind google unlink gdrive`** to do that (it revokes at Google
*and* clears the local credential); <https://myaccount.google.com/connections> is
the manual fallback. Either way the grants are gone for good: re-linking does not
restore them, so the user has to pick the files again.

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
Settings → GSuite or `cremind drive grant --file <id>`) and stop the run.

## What per-file access cannot do

These limits apply when `status` reports **per-file** access; under whole-Drive
access none of them do.

- **No whole-Drive search.** `files` lists the granted set, not the user's Drive.
  To act on a file the user names, ask for the URL or run `grant`.
- **No whole-Drive monitoring.** The gdrive skill's `file_changed` events cover
  granted files only.
- **No finding a Sheet or Doc by name.** Ask for the URL or id — the **gsheets**
  and **gdocs** skills read and write any file the user owns from a URL alone,
  with no Drive grant needed at all (under either access model).

## Troubleshooting

- `Google Drive is not linked` → ask the agent to link the gdrive skill.
- `scopes_stale: true` → re-link the gdrive skill, then re-grant. One-way on the
  shared client: whole-Drive cannot be restored after the re-link (bring your own
  client with `GOOGLE_SCOPES` first to keep it).
- A picked file still unreadable → the approval may have used a different Google
  account than the linked one; `status` shows which email is linked.
- Grant seems to do nothing on a remote install → use `grant-complete`, or run
  `cremind drive grant` from the machine with the browser.
- On a remote install the redirect always targets `http://localhost:<port>` —
  Google's Desktop client accepts no other kind of address. If a port-forward or
  tunnel already makes that address reach this server from the user's machine
  (e.g. `kubectl port-forward`), keeping it running while they approve captures
  the redirect automatically, with no `grant-complete` paste. `grant` prints the
  exact address it will use.
