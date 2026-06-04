---
description: "Reference for the `cremind chat` CLI command — an interactive full-screen chat REPL that opens a TUI against a new or existing conversation and streams the agent's thinking, text, and tool output as it arrives. Documents how `chat` differs from `cremind conv send` (REPL vs one-shot scripting), the keyboard shortcuts (Enter to send, Ctrl+C to cancel an in-flight run or quit when idle, Ctrl+D to quit, PgUp/PgDn to scroll), the optional conversation-id argument that resumes an existing thread, the `-t/--title` flag for new conversations, how to rename a conversation's id or title from outside the REPL via `cremind conv set-id` and `cremind conv rename`, and what to reach for when you want to script a single message instead of sit at a prompt."
---

# `cremind chat` — Interactive Chat REPL

`cremind chat` opens a full-screen TUI chat session against an Cremind
conversation. Every keystroke goes into the composer; Enter dispatches
the message; the agent's thinking, text tokens, and tool output stream
in real-time. With no argument, `chat` creates a new conversation and
starts a fresh session; with a conversation id, it resumes the thread.

This is the right command when you want to *talk to* the agent. For
scripted, one-shot interactions (pipe answer to a file, gate it on
exit code, etc.) reach for [`cremind conv send`](%5Bcli%5Dopa%20conv.md)
instead — its `--raw` and `--json` modes are designed for pipelines,
while `chat` always runs as an interactive TUI.

`chat` is the rough CLI equivalent of opening a conversation in the
**Conversations** view of the Cremind web UI and typing into its
composer.

## Finding this in the web UI

`cremind chat` mirrors the conversation view in the web UI:

> **Sidebar → Conversations → (open or create a conversation)**

Selecting a conversation in the sidebar opens the same streaming
message pane the TUI renders, with a composer at the bottom for new
messages. Creating a new conversation from the **+** button corresponds
to running `cremind chat` with no argument; clicking into an existing
conversation corresponds to running `cremind chat <id>`.

## Global flags

`cremind chat` accepts the root-level `--json` flag, but only as a no-op:
the TUI always renders interactively and ignores `--json`. For
machine-readable streams use `cremind conv send --json` or
`cremind conv attach --json`.

`CREMIND_TOKEN` is required.

## Syntax

```bash
cremind chat [<conversation_id>] [-t <title>]
```

**Arguments.**

- `<conversation_id>` *(optional)* — Resume an existing conversation.
  When omitted, `chat` creates a new conversation first, then enters
  the TUI on it.

**Flags.**

| Flag             | Type   | Default | Meaning                                                            |
|------------------|--------|---------|--------------------------------------------------------------------|
| `--title`, `-t`  | string | `""`    | Title to apply when creating a new conversation. Ignored when an id is supplied. |

## Keyboard shortcuts

| Key             | Action                                                                            |
|-----------------|-----------------------------------------------------------------------------------|
| **Enter**       | Send the composer's contents as a user message.                                   |
| **Ctrl+C**      | Cancel the current in-flight run. If no run is in flight, quit the TUI.            |
| **Ctrl+D**      | Quit the TUI immediately. Any in-flight run is left running on the server.         |
| **PgUp / PgDn** | Scroll the message history.                                                       |

Reasoning mode is always **on** in `chat` — the agent runs its full
ReAct loop (Thought / Action / Input / Observation / Response) for
every turn, and the TUI renders each step as it streams. To send a
message without reasoning, use `cremind conv send --no-reasoning` instead.

## Behavior

When called with no argument, `chat`:

1. Creates a new conversation (using `--title` if provided), capturing
   its id and title server-side.
2. Opens the TUI in **interactive** mode against that conversation,
   with reasoning enabled.

When called with a conversation id, `chat`:

1. Looks up the conversation to read its current title for display.
2. Opens the TUI in interactive mode against that conversation. Any
   prior messages are shown as history when you scroll up.

The TUI exits on Ctrl+D, on Ctrl+C while idle, or when the local
terminal closes. Conversations and runs persist on the server in all
cases.

## Examples

### Start a new chat

```bash
$ cremind chat -t "Quick question"
# (full-screen TUI opens — type, press Enter to send, Ctrl+D to quit)
```

### Resume an existing conversation

```bash
$ cremind chat c_82bc
# (TUI opens with the conversation's title; previous messages
# are scrollable via PgUp/PgDn)
```

### Pipe an answer instead of sitting at a TUI

```bash
# `chat` is interactive only — for one-shot scripting use `conv send`:
$ id=$(cremind conv new -t "One-shot")
$ cremind conv send "$id" "Define entropy in one sentence." --raw
```

### Watch an existing run from another shell

```bash
# `chat` will *send* a new message and lock you into the composer.
# To passively follow a run started elsewhere, use `conv attach`:
$ cremind conv attach c_82bc
```

### Resume after Ctrl+D

```bash
# Ctrl+D quits the TUI but does not delete or finalize the conversation.
$ cremind conv list | head
$ cremind chat c_82bc            # pick up where you left off
```

### Give the conversation a friendlier id or title

`cremind chat` itself does not rename conversations; do it from the
sister command group before resuming. Both edits are non-destructive
— the message history follows the rename atomically.

```bash
# Promote a server-allocated UUID to a memorable slug. The title is
# reset to match the new id; rename it afterward if you want a label.
$ cremind chat -t "scratch"        # creates e.g. c_82bc, then drops you in
# (Ctrl+D to leave the TUI)
$ cremind conv set-id c_82bc mkt_1
$ cremind conv rename mkt_1 "Marketing thread"
$ cremind chat mkt_1               # resume under the new id
```

Format rules for `set-id` (lowercase a-z, digits, `-`, `_`, must
start with an alphanumeric, max 128 chars) and the full subcommand
reference live in [`cremind conv`](%5Bcli%5Dopa%20conv.md). The web UI's
sidebar exposes the same edits via the pencil icon on each
conversation row.

## Cancelling vs quitting

`chat` distinguishes the two cases by whether a run is in flight:

| State              | Ctrl+C                                                          | Ctrl+D                                          |
|--------------------|-----------------------------------------------------------------|-------------------------------------------------|
| Run in flight      | Cancels the run (the agent's stream stops, the conversation goes idle). | Quits the TUI; the run keeps going on the server. |
| Idle (no run)      | Quits the TUI.                                                   | Quits the TUI.                                   |

If you Ctrl+D out during a run and want to stop it later, use
`cremind conv cancel <run_id>` (the run id is the `TASK_ID` column of
`cremind conv list`).

## Troubleshooting

**TUI rendering looks broken** — `chat` uses ANSI escapes throughout.
Set `OPA_NO_COLOR=1` to strip color (the layout still works); on
Windows use Windows Terminal or PowerShell 7+ for proper rendering.

**`Ctrl+C` quit the CLI but my run is still running** — That happens
when Ctrl+C is delivered before the TUI has wired up its run-cancel
hook (typically only at the very start of a session). Use
`cremind conv cancel <run_id>` to stop the run from another shell.

**`chat` exits immediately with an auth error** — `CREMIND_TOKEN` is
either missing or expired. Run `cremind me` to confirm.

**Cannot create a new conversation** — Title-only failures are rare;
the most common cause is a missing or invalid token. Try
`cremind conv new -t "Test"` to isolate the create step from the TUI.

**Wrong conversation opens** — `chat <id>` does not validate the id
matches the current profile beyond what the server enforces. If a
conversation id you remember does not appear in `cremind conv list`, it
likely belongs to a different profile — switch tokens (`CREMIND_TOKEN`)
and try again.
