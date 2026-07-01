---
description: "Browse and manage **files in the Cremind workspace**: `list`, `download`, `upload`, `mkdir`, `move`, and `delete` files, read or set a conversation's working directory (`cwd`, `set-cwd`), and `watch` filesystem-change events. Use this to move files into or out of the workspace and manage the agent's working directory — file operations, distinct from `cremind file-watchers` (which subscribes to change events)."
---

# `cremind files` — Workspace File Management

`cremind files` is the CLI for browsing and managing files that the Cremind
agent can see. It talks to the file-serving API (`/api/files/*`), which is
**sandboxed**: every path must resolve inside one of the allowed roots — the
Cremind system directory or the user working directory — or the server returns
`403 Access denied`. A conversation that has switched into a custom directory
(via the `change_working_directory` tool) widens its own allowlist; pass
`--conversation <id>` to reach those paths.

All paths are **absolute server-side paths**. Start from `cremind files cwd`
to learn the workspace root, then `cremind files list <path>` to walk down.

## Finding this in the web UI

These operations back the file-tree panel on the right side of the chat view:

> **Chat view → file tree (right panel)**

The tree renders directory listings (`list`), opens files (`download`),
accepts drag-and-drop uploads (`upload`), and offers right-click new-folder /
rename / delete (`mkdir` / `move` / `delete`). The live refresh as files change
on disk is driven by the same watch stream as `cremind files watch`.

## The sandbox & `--conversation`

- Reads and writes are confined to the Cremind system dir and the user working
  dir. A path outside both is rejected with `403 Access denied`.
- `delete` and `move` additionally refuse to touch an allowed *base* root
  itself (you can't delete the workspace root).
- `--conversation <id>` widens the allowlist to include the directory that
  conversation was switched into with `change_working_directory` — needed only
  when the file lives outside the static roots.

## Global flags

All `cremind files` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind files cwd`

**Purpose.** Print the workspace working directory — the seed path the file
tree opens at.

```bash
cremind files cwd
```

Prints the absolute path on a single line (or `{"cwd": "..."}` with `--json`).

### `cremind files set-cwd`

**Purpose.** Set a conversation's working-directory override (the same write
the `change_working_directory` tool performs), so the agent's effective cwd
moves without a tool round-trip.

```bash
cremind files set-cwd <conversation_id> <path>
```

- `<conversation_id>` — Conversation to repoint.
- `<path>` — An existing absolute directory.

Prints the resolved working directory. The override persists across restarts.

### `cremind files list`

**Purpose.** List the entries in a directory.

```bash
cremind files list <path> [--show-hidden] [--conversation <id>]
```

**Behavior.** Renders a `NAME / DIR / SIZE / MODIFIED` table, directories
first then files, each sorted case-insensitively. `--show-hidden` includes
dotfiles and Windows-hidden entries (SYSTEM-flagged entries are always
omitted). Listings are capped at 2000 entries; when more exist, a
`(listing truncated)` note is printed to stderr. With `--json`, returns
`{path, entries: [{name, path, is_dir, size, modified}], truncated}`.

**Example.**

```bash
$ cremind files list "$(cremind files cwd)"
NAME          DIR   SIZE   MODIFIED
documents     yes          2026-06-28 11:02:14
notes.md      no    1841   2026-06-30 09:15:48
```

### `cremind files download`

**Purpose.** Download a file's bytes.

```bash
cremind files download <path> [--out <local_file>] [--conversation <id>]
```

**Behavior.** Streams the file to `--out` if given, otherwise to stdout
(binary-safe — redirect it: `cremind files download <path> > local`). On
success with `--out`, prints `saved <file>` to stderr.

**Example.**

```bash
$ cremind files download "C:\Users\me\workspace\report.pdf" --out report.pdf
saved report.pdf
```

### `cremind files upload`

**Purpose.** Upload one or more local files into a server directory.

```bash
cremind files upload <server_dir> <local_file>... [--conversation <id>]
```

**Behavior.** Sends a multipart request. The server strips any path components
from each filename and writes into `<server_dir>`, picking a collision-free
name (`foo.txt` → `foo (1).txt`) when one already exists. Prints a
`NAME / SAVED_AS / STATUS / ERROR` table (`status` is `ok`, `renamed`, or
`error`); `--json` returns the raw `results` array.

**Example.**

```bash
$ cremind files upload "C:\Users\me\workspace\inbox" ./a.csv ./b.csv
NAME    SAVED_AS    STATUS   ERROR
a.csv   a.csv       ok
b.csv   b (1).csv   renamed
```

### `cremind files mkdir`

**Purpose.** Create a new directory.

```bash
cremind files mkdir <path> [--conversation <id>]
```

Fails with `409` if the directory already exists. Prints the created path.

### `cremind files move`

**Purpose.** Move or rename a file or directory.

```bash
cremind files move <src> <dest> [--conversation <id>]
```

`<dest>` is the full target path **including the new basename** — to move
*into* a folder, compose the destination yourself. Fails with `409` if the
destination already exists, and refuses to move a path into itself or a
descendant. Prints the resolved destination.

### `cremind files delete`

**Purpose.** Delete a file, or recursively delete a directory.

```bash
cremind files delete <path> [--conversation <id>]
```

**There is no confirmation prompt.** Refuses to delete an allowed base root.
Silent on success.

### `cremind files watch`

**Purpose.** Stream filesystem-change events for a directory (recursively) as
Server-Sent Events. This is the ad-hoc, tail-it-now counterpart to a persistent
`cremind file-watchers` subscription — it does **not** run an agent action,
it just prints events.

```bash
cremind files watch <path> [--conversation <id>]
```

**Behavior.** Long-lived SSE connection. Prints a `ready` handshake frame, then
one line per event (`created`/`deleted`/`modified`/`moved`), each carrying the
`path` (and `dest_path` for moves). Default format is `[<type>] <raw JSON>`;
`--json` prints the raw JSON only. Ctrl-C to exit.

**Example.**

```bash
$ cremind files watch "C:\Users\me\workspace\Lee"
[ready] {"type":"ready","path":"C:\\Users\\me\\workspace\\Lee"}
[created] {"type":"created","path":"C:\\Users\\me\\workspace\\Lee\\new.py","is_dir":false}
```

## Worked examples

### Round-trip a file: download, edit, re-upload

```bash
$ root="$(cremind files cwd)"
$ cremind files download "$root/notes.md" --out notes.md
$ $EDITOR notes.md
$ cremind files upload "$root" notes.md     # lands as notes (1).md unless the original is gone
```

### Watch a directory in one window while working in another

```bash
# Window 1
$ cremind files watch "C:\Users\me\workspace"
# Window 2: any create/modify/delete shows up live in Window 1
```

## Troubleshooting

**`403 Access denied`** — The path is outside the Cremind system dir and the
user working dir. If the path belongs to a conversation's custom cwd, pass
`--conversation <id>`. Otherwise pick a path under the workspace
(`cremind files cwd`).

**`404 Not a directory` / `File not found`** — `list`/`watch` need an existing
directory; `download` needs an existing file. Re-check the path with
`cremind files list` on the parent.

**`409` on `mkdir` / `move`** — The target already exists. Pick a new name or
delete the existing entry first.

**`watch` exits immediately** — Almost always an auth issue; confirm
`cremind me` works first.

## Related

- `cremind file-watchers` — persistent watches that run an agent action on each
  matching event (vs. `files watch`, which only prints events).
- `app/api/files.py` — the file-serving API these commands wrap.
