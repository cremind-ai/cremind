---
description: "Complete reference for the `cremind proc` CLI command (alias `cremind process`) — the terminal-side counterpart to the **Processes** page in the Cremind web UI — covering how to list and inspect long-running processes, terminate them (`stop`), feed them stdin (literal text, named keys, or piped stdin), resize their PTY, attach a raw-mode interactive PTY session (`attach`, with detach via Ctrl-\\), tail a live SSE stream of process snapshots (`stream`), and manage autostart registrations (`autostart list/add/delete/run`) so processes relaunch at server boot. Documents the named-key vocabulary for `stdin --keys`, the line-ending modes, the WebSocket-backed `attach`, and how `register-long-running` from `cremind tools` ties into autostart."
---

# `cremind proc` — Long-Running Process Control

`cremind proc` (alias `cremind process`) is the CLI for managing the
long-running processes that Cremind spawns on a profile's behalf —
typically MCP servers, skill listener daemons, and skill
`long_running_app` processes. Every process is owned by a profile; the
active profile is resolved server-side from `CREMIND_TOKEN`.

The group covers four concerns:

- **Live processes** — `list`, `get`, `stop`.
- **I/O** — `stdin` (write text, named keys, or piped data), `resize`
  (change the PTY dimensions), `attach` (open a raw-mode interactive
  session over a WebSocket).
- **Streaming** — `stream` emits SSE events whenever any process's
  status changes.
- **Autostart** — `autostart list/add/delete/run` register processes
  so they relaunch at server boot. `cremind tools register-long-running`
  and `cremind skill-events listener-start` both end up writing autostart
  rows that are visible here.

## Finding this in the web UI

Every operation in this group has a control on the **Processes** page
of the Cremind web UI:

> **Sidebar → Processes**

The page lists active processes (matching `cremind proc list`), exposes a
**Stop** button per row (matching `cremind proc stop`), and opens a
PTY-style terminal panel when a process is selected (matching
`cremind proc attach`). The **Autostart** sub-tab on the same page lists
autostart rows (matching `cremind proc autostart list`).

## Streaming output format

`cremind proc stream` maintains a long-lived SSE connection. It prints one
line per snapshot in one of two formats:

- **Default (table mode):** `[<event_type>] <raw JSON payload>`.
- **`--json` mode:** the raw JSON payload only, one event per line —
  pipe-friendly for `jq`.

Press Ctrl-C to exit cleanly.

## Global flags

All `cremind proc` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind proc list`

**Purpose.** List running processes for the active profile.

**Syntax.**

```bash
cremind proc list
```

**Behavior.** Prints a four-column table:

| Column        | Source        | Meaning                                                   |
|---------------|---------------|-----------------------------------------------------------|
| `PID`         | `id`          | Server-assigned process id (used by all other subcommands).|
| `STATUS`      | `status`      | `running`, `exited`, `failed`, etc.                       |
| `COMMAND`     | `command`     | Command line being executed.                              |
| `WORKING_DIR` | `working_dir` | Working directory.                                        |

With `--json`, returns the full process records (including PTY flags,
exit codes, autostart-id back-references, etc.).

**Example.**

```bash
$ cremind proc list
PID     STATUS   COMMAND                                       WORKING_DIR
p_9d72  running  /usr/bin/python /skills/daily-brief/listen.py /home/li/work
p_e21f  running  /usr/bin/python /skills/daily-brief/run.py    /home/li/work
```

### `cremind proc get`

**Purpose.** Show the full record for a single process.

**Syntax.**

```bash
cremind proc get <pid>
```

**Behavior.** Pretty-prints the JSON object the server returns. With
`--json`, emits the same JSON unindented.

**Example.**

```bash
$ cremind proc get p_9d72
{
  "id": "p_9d72",
  "command": "/usr/bin/python /skills/daily-brief/listen.py",
  "working_dir": "/home/li/work",
  "status": "running",
  "is_pty": true,
  "started_at": "2026-05-02T14:00:00Z",
  "autostart_id": "a_8c14"
}
```

### `cremind proc stop`

**Purpose.** Terminate a process. The server signals the process and
records the exit; restart policy from autostart still applies.

**Syntax.**

```bash
cremind proc stop <pid>
```

**Behavior.** Silent on success. If the process is part of an
autostart registration, it will be relaunched on the next server
restart unless you also `cremind proc autostart delete <id>`.

**Example.**

```bash
$ cremind proc stop p_e21f
```

## I/O subcommands

### `cremind proc stdin`

**Purpose.** Send input to a running process. Three input modes are
supported, picked by the flag combination.

**Syntax.**

```bash
cremind proc stdin <pid> [--text "..."] [--keys K1,K2,...] [--line-ending none|lf|crlf]
cremind proc stdin <pid>                                # body read from stdin
```

**Flags.**

| Flag             | Type     | Default | Meaning                                                                |
|------------------|----------|---------|------------------------------------------------------------------------|
| `--text`         | string   | `""`    | Literal string. Combine with `--line-ending` to append `\n` / `\r\n`.   |
| `--keys`         | []string | `nil`   | Comma-separated list of named keys (see vocabulary below).             |
| `--line-ending`  | string   | `""`    | One of `none` / `lf` / `crlf`. Server uses its default if omitted.     |

If neither `--text` nor `--keys` is given, the CLI reads everything on
its own stdin until EOF and forwards it verbatim. `--keys` and
`--text` are mutually exclusive (the server picks `--keys` when both
are present).

**Named-key vocabulary.** The full list is server-side, but the
common ones are:

- `Enter`, `Tab`, `Esc`, `Backspace`, `Space`
- `Up`, `Down`, `Left`, `Right`
- `PgUp`, `PgDn`, `Home`, `End`
- `F1`–`F12`

**Behavior.** Silent on success (or with `--json`, prints the server's
response — useful for confirming bytes-written counts).

**Examples.**

```bash
# Type a literal command and press Enter
$ cremind proc stdin p_9d72 --text "ls /tmp" --line-ending lf

# Press the Enter key alone
$ cremind proc stdin p_9d72 --keys Enter

# Pipe a payload from a file
$ cremind proc stdin p_9d72 < commands.txt
```

### `cremind proc resize`

**Purpose.** Resize a process's pseudo-terminal. Necessary for
applications that pay attention to terminal size (editors, REPLs,
`top`).

**Syntax.**

```bash
cremind proc resize <pid> --cols <int> --rows <int>
```

**Flags** (both required, both must be > 0):

| Flag      | Type | Default | Meaning            |
|-----------|------|---------|--------------------|
| `--cols`  | int  | `0`     | Terminal columns.  |
| `--rows`  | int  | `0`     | Terminal rows.     |

**Behavior.** Silent on success. `cremind proc attach` already forwards
resize events automatically; this command is for non-interactive
contexts.

**Example.**

```bash
$ cremind proc resize p_9d72 --cols 200 --rows 60
```

### `cremind proc attach`

**Purpose.** Open an interactive PTY session against a process —
the closest you can get to running the program "as if" it were
local.

**Syntax.**

```bash
cremind proc attach <pid> [--no-resize]
```

**Flags.**

| Flag           | Type  | Default | Meaning                                                          |
|----------------|-------|---------|------------------------------------------------------------------|
| `--no-resize`  | bool  | `false` | Do not forward terminal resize events to the remote PTY.         |

**Behavior.** Opens a WebSocket to the process and pipes stdin /
stdout through your local terminal in **raw mode**. The CLI:

- Forwards every keystroke, including arrow keys and Ctrl combinations.
- Forwards `Ctrl-C` to the remote process.
- Forwards `SIGWINCH` (terminal resize) by default. Pass
  `--no-resize` to opt out.
- **Detaches** when you press `Ctrl-\` (the FS character, byte `0x1c`)
  — also reachable as `Ctrl-]` on some keyboards. Detaching does not
  kill the process.

The session ends when the remote process exits, the WebSocket closes,
or you press the detach key. The terminal is restored to cooked mode
on exit.

**Example.**

```bash
$ cremind proc attach p_9d72
# (raw-mode session — type as if locally; press Ctrl-\ to detach)
```

## `cremind proc stream`

**Purpose.** Tail live process snapshots (SSE). Useful for dashboards
and log tailing.

**Syntax.**

```bash
cremind proc stream
```

**Behavior.** See [Streaming output format](#streaming-output-format).
Ctrl-C exits.

**Example.**

```bash
$ cremind proc stream
[snapshot]    {"id":"p_9d72","status":"running","cpu":0.7,...}
[exited]      {"id":"p_e21f","exit_code":0,...}
```

## Autostart subgroup

Autostart registrations make a process relaunch at server boot.
Several CLI commands write autostart rows automatically — including
`cremind tools register-long-running` and `cremind skill-events listener-start`.
This subgroup gives you direct CRUD on those rows.

### `cremind proc autostart list`

**Purpose.** List autostart registrations.

**Syntax.**

```bash
cremind proc autostart list
```

**Behavior.** Prints a five-column table:

| Column         | Source        | Meaning                                                              |
|----------------|---------------|----------------------------------------------------------------------|
| `ID`           | `id`          | Autostart row id (used by `delete` and `run`).                       |
| `COMMAND`      | `command`     | Command line that will be launched.                                  |
| `WORKING_DIR`  | `working_dir` | Working directory.                                                   |
| `PTY`          | `is_pty`      | `yes`/`no` — whether the process should run under a PTY.             |
| `ERROR`        | `error`       | Latest error message from the supervisor (blank when healthy).       |

With `--json`, returns the underlying array.

**Example.**

```bash
$ cremind proc autostart list
ID      COMMAND                                       WORKING_DIR    PTY  ERROR
a_8c14  /usr/bin/python /skills/daily-brief/listen.py /home/li/work  yes
a_b1f0  /usr/bin/python /skills/daily-brief/run.py    /home/li/work  yes
```

### `cremind proc autostart add`

**Purpose.** Register an *already-running* process as autostart.

**Syntax.**

```bash
cremind proc autostart add --pid <pid> [--force]
```

**Flags.**

| Flag       | Type   | Default | Meaning                                                      |
|------------|--------|---------|--------------------------------------------------------------|
| `--pid`    | string | `""`    | Process id to register. **Required.**                         |
| `--force`  | bool   | `false` | Bypass the duplicate-command check (allows two rows with the same command). |

**Behavior.** Reads the process's command, working directory, and PTY
flag, and writes a new autostart row. Prints the new row's id on
success.

**Example.**

```bash
$ cremind proc autostart add --pid p_9d72
a_8c14
```

### `cremind proc autostart delete`

**Purpose.** Remove an autostart row.

**Syntax.**

```bash
cremind proc autostart delete <id>
```

**Behavior.** Silent on success. Does **not** stop a running process
spawned from that row — use `cremind proc stop <pid>` for that.

**Example.**

```bash
$ cremind proc autostart delete a_b1f0
```

### `cremind proc autostart run`

**Purpose.** Spawn the command from an autostart row immediately,
without waiting for the next server boot.

**Syntax.**

```bash
cremind proc autostart run <id>
```

**Behavior.** Prints the new process id on success (or the full
record with `--json`).

**Example.**

```bash
$ cremind proc autostart run a_8c14
p_9d72
```

## Worked examples

### Find every running listener and tail its output

```bash
$ pid=$(cremind proc list --json | jq -r '.[] | select(.command|contains("listen.py")) | .id' | head -n1)
$ cremind proc attach "$pid"
```

### Send Ctrl-C to a wedged process without stopping it

```bash
# Attach, press Ctrl-C (which is forwarded), then detach with Ctrl-\
$ cremind proc attach p_e21f
```

### Resize a TUI process running under a PTY

```bash
$ cremind proc resize p_9d72 --cols $(tput cols) --rows $(tput lines)
```

### Promote a one-off process to autostart

```bash
$ pid=$(cremind proc list --json | jq -r '.[] | select(.command|contains("daily-brief"))' | jq -r .id | head -n1)
$ cremind proc autostart add --pid "$pid"
```

### Re-run an autostart row after a config change

```bash
$ cremind proc autostart run a_8c14
```

### Tail process state changes into `jq`

```bash
$ cremind proc stream --json | jq -r 'select(.type=="exited") | "\(.data.id) exited code=\(.data.exit_code)"'
```

## Troubleshooting

**`stdin` reads from your terminal forever** — That is the default
when neither `--text` nor `--keys` is provided. Either pipe a file in
(`< file.txt`) or use `--text`/`--keys`.

**`attach` shows garbled output / no response** — The process is not
running under a PTY. Check `cremind proc list --json | jq '.[] | .is_pty'`.
Non-PTY processes can still receive `stdin`, but their stdout/stderr
streams are line-buffered and not interactive.

**`Ctrl-C` killed the local CLI instead of the remote process** —
That happens when `attach` is not yet in raw mode, e.g. during the
WebSocket handshake. Wait for the remote prompt before sending Ctrl-C.

**Detach key doesn't seem to work** — The default detach key is
`Ctrl-\` (byte `0x1c`). On some keyboard layouts, `Ctrl-]` produces
the same byte and works as well. If neither is reachable, simply close
the terminal — the remote process keeps running.

**`autostart add` fails with "duplicate command"** — There is already
an autostart row with the same command line. Either delete the
existing row first, or pass `--force` if you genuinely want a second
copy.

**Process exits immediately after `autostart run`** — Inspect the
process record (`cremind proc get <pid>`) for the exit code and stderr
preview. The most common cause is a missing variable or a stale
working directory; check `cremind tools set-var` for the relevant tool.
