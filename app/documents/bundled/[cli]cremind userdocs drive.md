---
description: "Index the user's OWN Google Drive files for User Document Search with `cremind userdocs drive`, so the agent can search, read and cite Drive documents next to local files: turn Drive indexing on or off (`drive enable`, `drive disable --yes`, which deletes the Drive index), choose which Drive folders to index (`drive folders list`, `drive folders set`, required for a whole-Drive account), check it (`drive status`: account, access model, hold such as auth_revoked or drive_unreachable, counts, last sync) and sync now (`drive sync --full`). Explains what gets indexed under per-file vs whole-Drive access, Google Docs/Sheets/Slides exports, metadata-only files, and why Drive results disappear after unlinking or revoking Google. Linking the Google account itself is the gdrive skill (see `cremind drive`). Not for Cremind's own documentation (that is documentation_search)."
---

# `cremind userdocs drive` — indexing your Google Drive

`cremind userdocs drive` adds **Google Drive** as a second source of User
Document Search: the agent then searches, reads and cites your Drive files the
same way as the files in your indexed folder (`cremind userdocs`). Drive can
be on while the local folder is off.

It indexes what the Google account linked to the **gdrive skill** lets
Cremind see — it never links an account itself. `cremind drive status` shows
the link and its access model; to link, ask the agent to link the gdrive
skill in chat.

- **Per-file access** (the default): the files you granted Cremind (`cremind
  drive grant`) plus the files Cremind created. `--folder` optionally narrows
  that to some folders.
- **Whole-Drive access** (the token holds the `drive` or `drive.readonly`
  scope): you must choose at least one folder first, or `enable` fails with
  `DriveFoldersRequired` — nobody wants their entire Drive indexed by flipping
  one switch.

## Finding this in the web UI

> **Sidebar → Settings → My Documents → Google Drive** — the switch, the
> folder picker, the state and **Sync now**.

## What is indexed, and how it stays in sync

- Google's change feed is read every 5 minutes (`drive sync` reads it now),
  and everything in scope is re-listed every 6 hours and after a folder
  change (`drive sync --full` re-lists now).
- Renaming or moving a file only updates its name and path. Editing it
  re-embeds only the changed parts.
- Google Docs, Sheets and Slides are indexed from an export (Markdown, xlsx,
  pptx), so headings, sheets and slide numbers still work as locators.
- Indexed by name, type and dates only (`metadata_only`): files whose owner
  disabled downloads (`not_downloadable`), files over the admin's size limit
  (`too_large`), video, audio, archives, forms and sites. Shortcuts become a
  card naming their target.
- Drive paths read `Drive/<folders>/<name>`. `cremind userdocs files --source
  drive` lists them; `search`/`find --source drive` searches only Drive.
- When at least half (and at least 200) of the Drive files vanish at once,
  nothing is deleted: `cremind userdocs deletions confirm --source drive`
  removes them, `deletions reject --source drive` keeps them.

## When Google access changes

`drive status` (and `cremind userdocs status`) show a Drive hold:

| State | Meaning | Drive results | Drive index |
|---|---|---|---|
| `hold(auth_revoked)` | Google rejected the link (e.g. access removed at myaccount.google.com) | hidden | removed 7 days after the first failure, unless Google is re-linked |
| `hold(drive_unlinked)` | The gdrive token is gone (unlinked from chat, skill removed) | hidden | same 7-day timer |
| `hold(drive_unreachable)` | Network down or Google failing | shown, possibly stale | kept; the hold clears itself |
| `hold(drive_misconfigured)` | Google refused the OAuth client | shown, possibly stale | kept; re-link the gdrive skill |

Unlinking Google Drive through Cremind (`cremind google unlink gdrive`, or
Settings → GSuite) removes the Drive index **immediately**. Linking a
different Google account re-indexes Drive from scratch automatically.

## Subcommands

All accept the root-level `--json` flag right after `cremind`
(`cremind --json userdocs drive status`).

### `cremind userdocs drive status`

**Purpose.** One line: on/off, the engine's state, the linked account and
access model, the folders, how many files are indexed, and the last sync.

```bash
$ cremind userdocs drive status
on · live · linked as u@example.com (whole-Drive) · folders: 1AbC…, 7xYz… · 1204 files indexed · last sync 2026-09-25 10:12
```

### `cremind userdocs drive enable`

**Purpose.** Turn Drive indexing on.

```bash
cremind userdocs drive enable [--folder ID]... [--yes]
```

| Flag | Meaning |
|---|---|
| `--folder` | A folder to index: its id or its `drive.google.com/drive/folders/…` link. Repeatable; replaces the list. Required for whole-Drive access. |
| `--yes` | Apply even if the new folder list removes indexed Drive files. |

### `cremind userdocs drive disable`

**Purpose.** Turn Drive indexing off. **This deletes the Drive index**, so it
always needs `--yes`; without it the command prints what would go and exits 2.

```bash
cremind userdocs drive disable --yes
```

### `cremind userdocs drive sync`

**Purpose.** Read Google's changes now instead of at the next poll.

```bash
cremind userdocs drive sync [--full]
```

`--full` re-lists everything in scope (slower; use it when Drive and the
index disagree).

### `cremind userdocs drive folders list`

**Purpose.** Browse Drive folders one level at a time, to find the ids
`folders set` takes. `*` marks the folders being indexed.

```bash
cremind userdocs drive folders list [--parent ID]
```

Without `--parent` it lists the top level: My Drive for whole-Drive access,
the folders you granted for per-file access.

### `cremind userdocs drive folders set`

**Purpose.** Choose the folders to index (replaces the list; subfolders are
included).

```bash
cremind userdocs drive folders set ID [ID]... [--yes]
cremind userdocs drive folders set --clear
```

| Flag | Meaning |
|---|---|
| `--clear` | Drop the folder list: index every file the link can reach (per-file access only). |
| `--yes` | Apply even if files outside the new folders leave the index (exit code 2 without it). |

## Troubleshooting

**`DriveNotLinked`** — no Google account is linked to the gdrive skill. Ask
the agent to link it, then run `enable` again.

**`DriveFoldersRequired`** — whole-Drive access: pick folders with `drive
folders list`, then `drive enable --folder ID`.

**Drive files missing from search** — check `drive status` for a hold, and
under per-file access that the file was granted (`cremind drive files`).
