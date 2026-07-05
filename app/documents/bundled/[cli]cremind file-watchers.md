---
description: "Watch the **filesystem** and trigger the agent when files change: register a directory watch (relative to `CREMIND_USER_WORKING_DIR` or absolute), filtered by event type (created / modified / deleted / moved), target kind (file / folder), and file extensions; `list` and `delete` watcher subscriptions for the active profile; and tail admin snapshots over SSE. Use this to run the agent when a file is added, edited, or removed — filesystem events, unlike time-based `cremind calendar` or skill events."
---

# `cremind file-watchers` — Filesystem Watch Subscriptions

`cremind file-watchers` (aliases `cremind file-watcher`, `cremind fw`) is the CLI for
managing the **File Watcher** subsystem of Cremind. A *file watcher
subscription* binds a directory on disk to a natural-language *action*
that the Reasoning Agent should execute whenever a matching filesystem
event fires. This is the parallel system to skill events, but the trigger
comes from `watchdog` watching real files instead of a skill's listener
daemon dropping `.md` files under `events/<event_type>/`.

Each time a watcher *fires*, that single filesystem event now runs in its own
isolated, hidden per-run conversation, tracked with a status and token usage.
Browse that run history — and reply to a firing that paused to ask you
something — with `cremind event-runs` (`cremind event-runs list --kind file-watcher`).

The group covers three orthogonal concerns:

- **Subscriptions** — `list`, `delete`, `register`. Each subscription
  binds a directory + filter set to a conversation-scoped action.
- **Live streaming** — `stream` emits a Server-Sent Events feed of the
  current admin snapshot whenever any subscription is created, deleted,
  or its arm-state changes. Runs until interrupted with Ctrl-C.
- **Notifications** — file watcher runs publish to the same
  per-profile notifications bus as skill events, so use
  `cremind skill-events notifications` to tail both kinds together.

## Finding this in the web UI

Every operation in this group has a control on the **Events** page of
the Cremind web UI:

> **Sidebar → Events → File Watcher Events**

The Events page renders two sections: **Skill Events** at the top and
**File Watcher Events** below. The lower table mirrors
`cremind file-watchers list`; deletes propagate live through the same
admin SSE stream that powers `cremind file-watchers stream`. There is no
"register" button in the UI — subscriptions are created either by the
assistant via the `register_file_watcher` builtin tool (e.g.
"when a python file changes in the 'Lee' directory, notify me") or
from the CLI with `cremind file-watchers register`.

## How a watcher fires

After registration, the server mounts a single `watchdog.Observer` per
unique `(profile, root_path, recursive)` triplet — multiple
subscriptions on the same root share one Observer to keep file-handle
cost flat. Each filesystem event is funneled through this pipeline:

1. **Ignore patterns** — events matching `*.swp`, `*.swx`, `*.swo`,
   `~$*` (Office lock files), `*.tmp`, `*.temp`, `.DS_Store`,
   `*.crdownload`, or `*.part` are dropped before any further work.
   This avoids agent-run spam from IDE saves and OS housekeeping.
2. **Debounce** — identical `(path, event_type)` events arriving
   within 500 ms of the previous one are coalesced. Watchdog on
   Windows often emits multiple `modified` events per single save;
   this prevents that storm from triggering N agent runs.
3. **Per-subscription filter** — each subscription on the root is
   evaluated independently:
   - `event_types` — must contain the event's type
     (`created`/`modified`/`deleted`/`moved`).
   - `target_kind` — `any`, `file`, or `folder`. Determined by
     `event.is_directory`.
   - `extensions` — only applied to file events. Empty list = match
     all extensions; otherwise the event's path suffix (lowercased)
     must be in the list.
4. **Enqueue** — matching subscriptions enqueue an item on the
   per-conversation queue (the same queue user messages use), so a
   user message and a watcher-triggered run for the same conversation
   never interleave.
5. **Agent run** — the runner builds a synthetic trigger message,
   calls `stream_runner.run_agent_to_bus(...)`, and the agent's reply
   streams back through the conversation's SSE bus and shows up in the
   web UI like a normal turn.

The synthetic trigger message handed to the agent looks like this:

```
Trigger: <event_type>
Action: <action>
Content:
---
event_type: created|modified|deleted|moved
target_kind: file|folder
path: <abs path>
src_path: <abs path>          # only for moved
dest_path: <abs path>         # only for moved
watch_name: <name>
extension: <.ext or empty>
detected_at: <ISO 8601 with TZ>
---
```

## Streaming output format

`cremind file-watchers stream` maintains a long-lived SSE connection. It
prints one line per snapshot frame in one of two formats:

- **Default (table mode):** `[<event_type>] <raw JSON payload>`.
- **`--json` mode:** the raw JSON payload only (one snapshot per line).
  Pipe-friendly for `jq` and similar tools.

Press Ctrl-C to exit cleanly; the server connection is dropped.

## Lifecycle and persistence

Subscriptions live in the `file_watcher_subscriptions` SQLite table at
`<CREMIND_WORKING_DIR>/storage/cremind.db`. They survive server
restarts: on boot, the `FileWatcherManager` re-arms a watchdog Observer
for every existing row. If a row's `root_path` is missing or
unreadable at boot, that row is marked `armed=false` in memory but
**not deleted** — fix the path on disk and restart the server, or
delete the subscription and re-register it with `cremind file-watchers
register`.

`armed=false` shows up in `list` and `stream` so you can spot stale
rows without diffing logs.

## Global flags

All `cremind file-watchers` subcommands accept the root-level `--json`
flag. `CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind file-watchers list`

**Purpose.** List file watcher subscriptions belonging to the active
profile.

**Syntax.**

```bash
cremind file-watchers list
```

**Behavior.** Renders an eight-column table:

| Column        | Source              | Meaning                                                                    |
|---------------|---------------------|----------------------------------------------------------------------------|
| `ID`          | `id`                | Subscription id (used by `delete`).                                        |
| `NAME`        | `name`              | Display label (auto-generated from path + extensions if not provided).     |
| `PATH`        | `root_path`         | Absolute, fully-resolved directory being watched.                          |
| `TRIGGERS`    | `event_types`       | Comma-joined subset of `created,modified,deleted,moved`.                   |
| `TARGET`      | `target_kind`       | `file`, `folder`, or `any`.                                                |
| `EXTENSIONS`  | `extensions`        | Comma-joined like `.py,.md`. Empty = match all extensions.                 |
| `ARMED`       | `armed`             | `yes` if a live watchdog Observer covers this row, `no` otherwise.         |
| `CONV_TITLE`  | `conversation_title`| Title of the conversation the subscription routes events into.             |

With `--json`, returns the underlying array (which also includes
`recursive`, `action`, `conversation_id`, `profile`, and `created_at`).

**Example.**

```bash
$ cremind file-watchers list
ID         NAME      PATH                              TRIGGERS                          TARGET  EXTENSIONS  ARMED  CONV_TITLE
fw_a3f1    py-only   C:\Users\me\Documents\Lee         created,modified                  file    .py         yes    Lee Watcher
fw_8c20    inbox     C:\Users\me\Documents\inbox       created                           file    .pdf        yes    PDF Inbox
```

### `cremind file-watchers delete`

**Purpose.** Remove a subscription. The corresponding watchdog
Observer is torn down if no other subscription on the same
`(root_path, recursive)` pair remains.

**Syntax.**

```bash
cremind file-watchers delete <id>
```

**Behavior.** Silent on success.

**Example.**

```bash
$ cremind file-watchers delete fw_a3f1
```

### `cremind file-watchers register`

**Purpose.** Create a new file watcher subscription. The server-side
behavior is identical to invoking the `register_file_watcher` builtin
tool from a conversation — same path resolution, same validation, same
arming.

**Syntax.**

```bash
cremind file-watchers register --action "<instruction>"
                           [--path <directory>]
                           [--name <label>]
                           [--triggers <csv>]
                           [--target file|folder|any]
                           [--ext <csv>]
                           [--recursive=true|false]
                           [--conversation <id>]
```

**Flags.**

| Flag             | Type   | Default                              | Meaning                                                                                                                          |
|------------------|--------|--------------------------------------|----------------------------------------------------------------------------------------------------------------------------------|
| `--action`       | string | — (**required**)                     | Natural-language instruction the assistant runs on each matching event. Don't include event metadata — the runtime appends it.   |
| `--path`         | string | `CREMIND_USER_WORKING_DIR`            | Directory to watch. Relative paths join the user working dir; absolute paths are used as-is. Must exist and be a directory.      |
| `--name`         | string | auto from `path` + `extensions`      | Short display label.                                                                                                             |
| `--triggers`     | string | `created,modified,deleted,moved`     | Comma-separated subset of the four event types.                                                                                  |
| `--target`       | string | `any`                                | Restrict to `file`, `folder`, or both (`any`).                                                                                   |
| `--ext`          | string | `""` (match all)                     | Comma-separated extensions (`.py,.md` or `py,md` — leading dot added automatically). File events only; folder events bypass this.|
| `--recursive`    | bool   | `true`                               | Watch subdirectories recursively.                                                                                                |
| `--conversation` | string | new conversation                     | Bind to an existing conversation id. If omitted, a fresh conversation is created with title `File Watcher: <name>`.              |

**Behavior.** POSTs the registration to the server. On success, prints
a key-value table with the new subscription's `id`, `name`, resolved
`root_path`, `event_types`, `target_kind`, `extensions`, `recursive`,
`armed` flag, and `conversation_id`. With `--json`, returns the full
JSON payload.

If `armed` comes back `no`, the subscription was saved but no live
Observer is running for it (typically a transient I/O error). Restart
the server and the boot-time re-arm will retry.

**Path resolution rules.**

- `--path Lee` (relative) → `<CREMIND_USER_WORKING_DIR>/Lee`. A
  `..`-traversal that escapes the user working directory is rejected.
- `--path C:\Users\me\Lee` (absolute) → used verbatim. The traversal
  guard does not apply to absolute paths — that's an explicit user
  opt-in.
- `--path` omitted or empty → watches `CREMIND_USER_WORKING_DIR` itself.

**Examples.**

```bash
# Watch every change to .py files in <USER_WORKING_DIR>/Lee
$ cremind file-watchers register \
    --path Lee \
    --triggers modified,created \
    --target file \
    --ext .py \
    --action "notify the user about the change"
id               fw_a3f1
name             Lee-.py
root_path        C:\Users\me\Documents\Lee
event_types      modified,created
target_kind      file
extensions       .py
recursive        yes
armed            yes
conversation_id  c_19a8

# Watch an absolute path for new PDFs and summarize them
$ cremind file-watchers register \
    --path C:\Users\me\Documents\inbox \
    --triggers created \
    --target file \
    --ext .pdf \
    --action "summarize the new pdf for me"

# Folder-only top-level watch (non-recursive), bind to existing conversation
$ cremind file-watchers register \
    --path Projects \
    --triggers created,deleted \
    --target folder \
    --recursive=false \
    --conversation c_19a8 \
    --action "log when a new project folder appears or vanishes"
```

### `cremind file-watchers stream`

**Purpose.** Tail the server-wide File Watcher admin snapshot stream.
Each frame is the **complete** subscription list for the active
profile — clients replace their in-memory copy on every push. The
server emits a frame on every create, delete, or arm-state change.

**Syntax.**

```bash
cremind file-watchers stream
```

**Behavior.** Long-lived SSE connection. See [Streaming output
format](#streaming-output-format) for the per-line format. Ctrl-C to
exit.

**Example.**

```bash
$ cremind file-watchers stream
[snapshot] {"subscriptions":[{"id":"fw_a3f1","name":"py-only",...,"armed":true},...]}
[snapshot] {"subscriptions":[]}                         # last subscription deleted
```

## Worked examples

### "When something changes in the 'Lee' directory, let me know"

The most common case — pretty much exactly the user request that
prompted the feature:

```bash
$ cremind file-watchers register \
    --path Lee \
    --action "notify the user that something changed in the 'Lee' directory"
id               fw_a3f1
name             Lee-all
root_path        C:\Users\me\Documents\Lee
event_types      created,modified,deleted,moved
target_kind      any
extensions
recursive        yes
armed            yes
```

Now drop a file into `<USER_WORKING_DIR>/Lee` from any tool —
Explorer, `touch`, an editor — and the agent receives a trigger
within ~500 ms (after the debounce window), runs the action, and
streams the reply into the conversation that was created for this
watcher.

### Tail the snapshot stream while creating a watcher in another window

```bash
# Window 1
$ cremind file-watchers stream
[snapshot] {"subscriptions":[]}

# Window 2
$ cremind file-watchers register --path Lee --action "notify me"

# Window 1 immediately receives the new state
[snapshot] {"subscriptions":[{"id":"fw_a3f1","name":"Lee-all",...,"armed":true}]}
```

### Pipe the snapshot stream into `jq`

```bash
$ cremind file-watchers stream --json | jq '.data.subscriptions[] | {name, path: .root_path, armed}'
{"name":"py-only","path":"C:\\Users\\me\\Documents\\Lee","armed":true}
```

### Tail combined notifications (skill events + file watcher events)

```bash
$ cremind skill-events notifications --since 0
```

File watcher runs publish into the same per-profile notifications
buffer that skill events do, so the existing notifications endpoint
covers both. There is no separate `cremind file-watchers notifications`
because it would just be the same SSE stream.

### Manually fire a watcher for development

There is no `cremind file-watchers simulate` (intentionally). To exercise
a watcher end-to-end during development, just touch a real file at
the watched path:

```bash
# Linux/macOS
$ touch "$$CREMIND_USER_WORKING_DIR/Lee/test.py"

# Windows PowerShell
> ni $env:USERPROFILE\Documents\Lee\test.py -Force | Out-Null
```

This exercises the full pipeline: watchdog → ignore filter →
debounce → per-subscription filter → enqueue → agent run.

## Troubleshooting

**`stream` exits immediately** — Almost always an auth issue. Confirm
`cremind me` works first.

**`register` returns `armed: no`** — The subscription row was saved
but no live watchdog Observer was created. Most common cause: the
path is on a drive that became unavailable, or watchdog couldn't open
the directory handle. Check the server logs for
`FileWatcherManager: failed to start observer for ...` near the
registration timestamp; restart the server to retry the boot-time
re-arm.

**Watcher fires for some events but not others** — Check the filter
columns from `list`. The most common cause is `target_kind: file`
silently excluding all directory events (e.g. on Windows, copying a
folder fires a `DirCreatedEvent` for the folder followed by file
events inside; with `target=file` only the inner file events match).

**Editor saves trigger many runs** — The 500 ms debounce coalesces
identical `(path, event_type)` bursts but does not collapse different
event types. A single save in some editors emits
`created → deleted → modified → modified` over ~50 ms; the debounce
suppresses the duplicate `modified` events but the others get
through. If this is a problem, narrow `--triggers` to just `modified`.

**No `armed` row for a subscription that exists in `list`** —
The watcher couldn't be set up at server boot (typically because the
path was missing). The row persists so you can fix the path on disk
and just restart the server — no need to re-register.

**Antivirus or VSS-protected paths** — On Windows, watching certain
system directories or VSS-protected locations can silently swallow
events even though `armed` shows `yes`. Pick a path under the user
profile (e.g. under `%USERPROFILE%\Documents`) when in doubt.

**File written, no agent reaction, no logs** — Two checks: (1)
Confirm `armed: yes` in `list`. (2) Tail the server log while
touching the file; you should see `FileWatcherManager: dispatched
N/M subs for <event> '<path>'`. If you see watchdog events at DEBUG
level but `dispatched 0` at INFO, your filters are excluding the
event — re-check `target_kind`, `extensions`, and the path actually
written.

## Related

- `cremind skill-events` — the parallel system for events declared by a
  skill's `SKILL.md` rather than raw filesystem changes.
- `cremind event-runs` — the per-firing run history: each matching filesystem
  event runs in its own isolated conversation with a status and token usage,
  viewable (and replyable, when pending) here.
- `documents/document.md` — how to write Cremind docs (this file's
  conventions).
- `app/tools/builtin/register_file_watcher.py` — the in-process
  builtin tool the assistant calls when a user asks for a watch.
