---
description: "Manage **conversations** and stream agent replies from the terminal: create a new conversation, list, fetch history, rename or change its id (`rename`, `set-id`), delete or delete-all, and `send` a message to stream the response, plus attach or cancel a run, inspect memory and running summary, force compaction, and per-conversation token usage. Use this to script one-shot messages and manage threads — distinct from `cremind chat` (the interactive REPL)."
---

# `cremind conv` — Conversation Management and Streaming

`cremind conv` (alias `cremind conversation`) is the CLI for managing
conversations — the persistent units of agent dialogue — and for
streaming agent runs inside them. Each conversation is owned by the
active profile (resolved server-side from `CREMIND_TOKEN`).

The group splits into three concerns:

- **Conversation lifecycle** — `list`, `new`, `get`, `history`,
  `rename`, `set-id`, `delete`, `delete-all`. CRUD plus paginated
  history. `rename` changes the title; `set-id` changes the
  conversation id itself (with cascade across messages and skill-event
  subscriptions).
- **Agent runs** — `send` (queue a user message and stream the agent's
  response), `attach` (subscribe to an already-running run without
  sending anything), `cancel` (stop an in-flight run by its `run_id`).
- **Inspection** — `get --detail` opens a TUI that replays the full
  thinking-process trace (Thought / Action / Input / Observation /
  Response) for every agent turn. `memory` shows the conversation's
  running summary and long-term memory, `compact` forces a compaction
  fold now, and `usage` reports per-request token usage and cost.

For an interactive REPL that wraps `new` + `send` into a chat loop,
see `cremind chat`. `cremind conv send` is the right command when you want to
script a one-shot question or pipe the answer somewhere.

## Streaming modes

Both `cremind conv send` and `cremind conv attach` produce streamed output.
The renderer is picked by flag precedence:

| Mode      | Trigger                        | What you see                                                                                  |
|-----------|--------------------------------|-----------------------------------------------------------------------------------------------|
| **TUI**   | Default (when stdout is a TTY).| Full-screen rendering of thinking, text, terminal output, and tool calls as they stream.       |
| **Raw**   | `--raw`.                       | Plain text — only the assistant's final text tokens, suitable for piping into another command. |
| **JSON**  | Root-level `--json`.           | One SSE event per line as raw JSON. Perfect for `jq` and structured pipelines.                |

`--json` overrides `--raw`. In TUI mode, Ctrl-C cancels the in-flight
run cleanly.

## Finding this in the web UI

Every operation in this group has a control on the **Conversations**
view of the Cremind web UI:

> **Sidebar → Conversations**

The list pane on the left mirrors `cremind conv list`. Selecting a
conversation opens the message thread (mirroring `cremind conv get`).
Sending a message in the composer mirrors `cremind conv send`; the
"viewer" mode that opens when you click into a conversation that is
mid-run mirrors `cremind conv attach`. Each conversation row exposes a
**pencil** icon that opens an inline edit dialog with both the **id**
and **title** fields (mirroring `cremind conv set-id` and `cremind conv
rename` together) and an **×** delete button, and a "Delete all"
action lives in the list pane's overflow menu.

## Global flags

All `cremind conv` subcommands accept the root-level `--json` flag, which
both forces JSON output for non-streaming subcommands *and* selects the
JSON streaming renderer for `send` / `attach`.

`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind conv list`

**Purpose.** List conversations belonging to the active profile, with
pagination and optional per-channel filtering.

**Syntax.**

```bash
cremind conv list [--limit N] [--offset N] [--channel <type>]
```

**Flags.**

| Flag        | Type   | Default | Meaning                                                                                       |
|-------------|--------|---------|-----------------------------------------------------------------------------------------------|
| `--limit`   | int    | `50`    | Page size.                                                                                    |
| `--offset`  | int    | `0`     | Offset for paging.                                                                            |
| `--channel` | string | `""`    | Filter by `channel_type` (e.g. `main`, `telegram`). Empty (default) returns every channel.    |

**Behavior.** Prints a five-column table:

| Column        | Source         | Meaning                                                                                                              |
|---------------|----------------|----------------------------------------------------------------------------------------------------------------------|
| `ID`          | `id`           | Conversation id (used by every other subcommand).                                                                    |
| `TITLE`       | `title`        | Title (may be blank).                                                                                                |
| `CHANNEL`     | `channel_id`   | UUID of the channel this conversation belongs to. `main` for web/CLI conversations; non-`main` for external platforms. Use `cremind channels list` to map id → type. |
| `CREATED_AT`  | `created_at`   | RFC 3339 creation timestamp.                                                                                         |
| `TASK_ID`     | `task_id`      | Active run id, if a run is in progress. Blank when idle.                                                             |

With `--json`, returns the full array (including the raw
`channel_id`).

**Examples.**

```bash
# All conversations across every channel
$ cremind conv list --limit 5
ID      TITLE                    CHANNEL                                 CREATED_AT            TASK_ID
c_82bc  Daily Brief              c1...main                               2026-05-02T08:00:00Z  t_19a8
c_4d10  PR review #42            c1...main                               2026-05-01T15:30:00Z
c_92bc  Lee Nguyen               e2e8...d4f1                             2026-05-03T00:30:00Z

# Only web/CLI conversations
$ cremind conv list --channel main

# Only conversations sourced from Telegram
$ cremind conv list --channel telegram
```

**Note.** Conversations on non-`main` channels are read-only from
the Cremind side: `cremind conv send` and the web UI's composer both
return `403 Read-only channel` for those ids. Inbound messages flow
through the platform's user; replies are forwarded automatically by
the channel adapter. See [`cremind channels`](./%5Bcli%5Dopa%20channels.md)
for the model.

### `cremind conv new`

**Purpose.** Create a new conversation. Useful as a building block
for scripts that want to send messages programmatically.

**Syntax.**

```bash
cremind conv new [-t <title>]
```

**Flags.**

| Flag           | Type   | Default | Meaning                  |
|----------------|--------|---------|--------------------------|
| `--title`, `-t`| string | `""`    | Conversation title.      |

**Behavior.** When stdout is a TTY, prints a key-value table with
`id`, `title`, `created_at`. When stdout is **not** a TTY (i.e. you
are piping it), prints just the id on its own line so you can capture
it directly. With `--json`, returns the full conversation object
either way.

**The new conversation is always created under the profile's `main`
channel.** External channels (Telegram, Discord, etc.) only ever spawn
conversations from inbound platform messages — there is no CLI flag
to attach a new conversation to one, and the server rejects any such
attempt over the API with `403 Read-only channel`. See
[`cremind channels`](./%5Bcli%5Dopa%20channels.md) for how those channels
work.

**Examples.**

```bash
# Interactive (TTY) — see the metadata
$ cremind conv new -t "Daily Brief"
id          c_82bc
title       Daily Brief
created_at  2026-05-02T14:00:00Z

# In a pipeline — just the id
$ id=$(cremind conv new -t "Auto")
$ echo "$id"
c_82bc
```

### `cremind conv get`

**Purpose.** Fetch a conversation with its full message history, or
open a TUI that replays the entire thinking-process trace.

**Syntax.**

```bash
cremind conv get <id> [--detail]
```

**Flags.**

| Flag        | Type | Default | Meaning                                                                |
|-------------|------|---------|------------------------------------------------------------------------|
| `--detail`  | bool | `false` | Open a TUI that replays Thought / Action / Input / Observation / Response for every agent turn. ESC to exit. |

**Behavior.** Without `--detail`, prints a key-value header (`id`,
`title`, `task_id`, `created_at`) followed by `--- messages ---` and
one line per message in the form `[<role>] <content>`. With `--detail`,
opens a full-screen TUI that reconstructs the live stream view from
the persisted messages, so you can scroll through the agent's reasoning
exactly as it appeared the first time.

With root `--json`, returns the full server response (a `conversation`
object plus a `messages` array).

**Example.**

```bash
$ cremind conv get c_82bc
id          c_82bc
title       Daily Brief
task_id
created_at  2026-05-02T08:00:00Z

--- messages ---
[user]      Give me today's brief
[assistant] Here's your brief for May 2: ...
```

### `cremind conv history`

**Purpose.** Print paginated message history for one conversation —
the lighter-weight cousin of `get`, useful for long threads.

**Syntax.**

```bash
cremind conv history <id> [--limit N] [--offset N]
```

**Flags.**

| Flag        | Type | Default | Meaning            |
|-------------|------|---------|--------------------|
| `--limit`   | int  | `100`   | Page size.         |
| `--offset`  | int  | `0`     | Offset for paging. |

**Behavior.** Prints `[<role>] <content>` per message, in order. With
`--json`, returns the underlying array.

**Example.**

```bash
$ cremind conv history c_82bc --limit 3 --offset 0
[user]      Give me today's brief
[assistant] Here's your brief for May 2: ...
[user]      Anything urgent?
```

### `cremind conv send`

**Purpose.** Send a user message and stream the agent's response.

**Syntax.**

```bash
cremind conv send <id> <message> [--raw] [--no-reasoning]
```

**Arguments** (both required):

- `<id>` — Target conversation.
- `<message>` — Message text. Quote it if it contains spaces.

**Flags.**

| Flag              | Type | Default | Meaning                                                          |
|-------------------|------|---------|------------------------------------------------------------------|
| `--raw`           | bool | `false` | Plain-text streaming (no TUI). Pipe-friendly.                    |
| `--no-reasoning`  | bool | `false` | Disable reasoning mode for this message — the agent answers without an explicit ReAct loop. |

The root `--json` flag overrides `--raw` and selects the JSON-per-line
renderer.

**Behavior.** Default mode opens a TUI that renders thinking, text,
terminal output, and other events as they stream from the agent.
Ctrl-C cancels the in-flight run cleanly. The conversation's id is
returned to its idle state once the run completes.

**Examples.**

```bash
# Interactive TUI (default)
$ cremind conv send c_82bc "Anything urgent?"

# Pipe-friendly raw stream — just the assistant text
$ cremind conv send c_82bc "Anything urgent?" --raw | tee answer.txt

# Structured event stream
$ cremind conv send c_82bc "Anything urgent?" --json | jq -r 'select(.type=="text").data.token'

# One-shot answer, no reasoning steps
$ cremind conv send c_82bc "Summarize in one sentence" --raw --no-reasoning
```

### `cremind conv attach`

**Purpose.** Subscribe to an in-flight conversation run without
sending a new message. Use this to peek at an agent run started
elsewhere (e.g. by the web UI or a skill event).

**Syntax.**

```bash
cremind conv attach <id> [--raw]
```

**Flags.**

| Flag      | Type | Default | Meaning                                  |
|-----------|------|---------|------------------------------------------|
| `--raw`   | bool | `false` | Plain-text streaming (no TUI).           |

The root `--json` flag overrides `--raw` and selects the JSON-per-line
renderer.

**Behavior.** Same renderer choices as `send`. If the conversation is
idle, the stream stays open and starts emitting as soon as a run
begins.

**Example.**

```bash
$ cremind conv attach c_82bc --json | jq .type
"thinking"
"text"
"complete"
```

### `cremind conv rename`

**Purpose.** Set a conversation's title.

**Syntax.**

```bash
cremind conv rename <id> <title>
```

**Behavior.** Silent on success.

**Example.**

```bash
$ cremind conv rename c_82bc "Daily Brief – May 2"
```

### `cremind conv set-id`

**Purpose.** Change a conversation's id. Useful for turning a
server-allocated UUID into a memorable slug like `mkt_1` so scripts
and skill-event subscriptions can reference the conversation by name.

**Syntax.**

```bash
cremind conv set-id <old_id> <new_id>
```

**Format.** The new id must match the regex
`^[a-z0-9][a-z0-9_-]{0,127}$` — that is:

- Length 1..128.
- Lowercase letters `a–z`, digits `0–9`, hyphen `-`, underscore `_`.
- The first character must be a letter or digit (no leading separator).

| Valid               | Invalid                                |
|---------------------|----------------------------------------|
| `mkt_1`             | `MKT_1`           (uppercase)          |
| `my-conv`           | `-mkt`            (leading separator)  |
| `1mkt`              | `lý-1`            (non-ASCII)          |
| `ai1`               | `tài-liệu`        (non-ASCII)          |
|                     | `'thư viện'`      (quotes / spaces)    |

**Behavior.** Silent on success. The rename cascades atomically to all
referencing tables (`messages.conversation_id`, skill-event
subscriptions). The conversation's **title is reset to the new id** —
if you want a different title, run `cremind conv rename <new_id> <title>`
right after.

**Restrictions.** The rename is rejected with an error message in
these cases:

- The new id is malformed (`400 Invalid id format`).
- A different conversation already uses that id (`409 Id already in use`).
- The conversation has an active streaming run (`409 Conversation is
  streaming`). Wait for the run to finish (or `cremind conv cancel` it)
  before retrying.

**Example.**

```bash
# Rename a UUID-style id to a friendly slug
$ cremind conv set-id c_82bc mkt_1
$ cremind conv get mkt_1
id          mkt_1
title       mkt_1
task_id
created_at  2026-05-02T08:00:00Z

# Optional: give it a human-readable title afterward
$ cremind conv rename mkt_1 "Marketing thread"
```

### `cremind conv cancel`

**Purpose.** Cancel an in-flight agent run by its **run id** (also
called `task_id`). The run id is the value shown in the `TASK_ID`
column of `cremind conv list` while a conversation is running.

**Syntax.**

```bash
cremind conv cancel <run_id>
```

**Behavior.** Prints `cancelled` if a run was active and was
cancelled, or `no active run for that id` if no run was active. With
`--json`, emits `{"cancelled": true|false}`.

**Note.** This takes a run id, not a conversation id. Use
`cremind conv list --json` and select the `task_id` of the conversation
you want to cancel.

**Examples.**

```bash
# Cancel by explicit run id
$ cremind conv cancel t_19a8
cancelled

# Cancel whatever is running in conversation c_82bc (one-liner)
$ run=$(cremind conv list --json | jq -r '.[] | select(.id=="c_82bc") | .task_id')
$ cremind conv cancel "$run"
```

### `cremind conv memory`

**Purpose.** Show a conversation's **running summary** (short-term memory)
and **long-term memory**, plus progress toward the next compaction fold.

**Syntax.**

```bash
cremind conv memory <id>
```

**Behavior.** Prints `enabled` (whether long-term memory is on),
`last_compacted_at`, a `context: <current> / <threshold> (window <N>)`
line showing how close the latest turn is to triggering a fold, the
`--- running summary ---` block, and a `--- long-term memory ---` list.
With `--json`, returns the full object (`summary`, `long_term`,
`token_progress`, `enabled`, `last_compacted_at`).

**Example.**

```bash
$ cremind conv memory c_82bc
enabled: True
last_compacted_at: 2026-06-29T22:10:04Z
context: 48210 / 95000 (window 200000)

--- running summary ---
The user (Lee) is preparing a Q3 marketing plan ...

--- long-term memory ---
- Prefers concise, bulleted replies.
```

### `cremind conv compact`

**Purpose.** Force a compaction **now** — fold the oldest turns into the
running summary instead of waiting for the context window to fill. Runs a
synthetic, non-persisted "please compact" turn through the agent.

**Syntax.**

```bash
cremind conv compact <id>
```

**Behavior.** Prints `compacted` if the running summary changed, or
`no change` if nothing needed folding. With `--json`, emits
`{"compacted": true|false}`. Requires `compaction.enabled` (see
`cremind config`).

**Example.**

```bash
$ cremind conv compact c_82bc
compacted
```

### `cremind conv usage`

**Purpose.** Per-request and cumulative token usage & estimated cost for one
conversation — the drill-down behind a single conversation on the
**Usage & Cost** dashboard.

**Syntax.**

```bash
cremind conv usage <id>
```

**Behavior.** Prints a headline (conversation id, request count, cache-hit
rate, and the conversation-wide totals), then a `--- requests ---` table of
`CREATED_AT / MODEL / PROVIDER / TOKENS / COST_USD`, one row per assistant
turn. With `--json`, returns the full object (`totals`, `cache_hit_rate`,
`request_count`, `by_source`, `requests`). For the cross-conversation
aggregate, use `cremind usage`.

**Example.**

```bash
$ cremind conv usage c_82bc
cache_hit_rate    0.58
conversation_id   c_82bc
request_count     6
total_tokens      184320
...

--- requests ---
CREATED_AT            MODEL            PROVIDER    TOKENS   COST_USD
2026-05-02T08:00:01Z  claude-opus-4-8  anthropic   31002    0.21
```

### `cremind conv delete`

**Purpose.** Delete a single conversation and all its messages.

**Syntax.**

```bash
cremind conv delete <id>
```

**Behavior.** Silent on success. **No confirmation prompt.**

**Example.**

```bash
$ cremind conv delete c_3a02
```

### `cremind conv delete-all`

**Purpose.** Delete every conversation belonging to the active
profile. Useful for clearing a noisy test profile.

**Syntax.**

```bash
cremind conv delete-all
```

**Behavior.** Prints `deleted N conversation(s)`. With `--json`,
emits `{"deleted_count": N}`. **No confirmation prompt** — be
careful, this is profile-wide.

**Example.**

```bash
$ cremind conv delete-all
deleted 17 conversation(s)
```

## Worked examples

### One-shot question, capture only the assistant text

```bash
$ id=$(cremind conv new -t "Quick" )
$ cremind conv send "$id" "What is the capital of Australia?" --raw
Canberra.
```

### Replay a finished conversation in detail mode

```bash
$ cremind conv get c_82bc --detail
# (full-screen TUI — ESC to exit)
```

### Watch a long-running agent turn from another shell

```bash
# Shell A: kick off a slow run via the UI or a skill event.
# Shell B:
$ cremind conv attach c_82bc
```

### Tail every event into a structured log

```bash
$ cremind conv attach c_82bc --json >> events.jsonl
```

### Cancel any conversation that has been running for >5 minutes (rough sketch)

```bash
$ now=$(date +%s)
$ cremind conv list --json | jq -c '.[] | select(.task_id != "")' | while read row; do
    started=$(jq -r .created_at <<<"$row" | xargs -I{} date -d "{}" +%s)
    if (( now - started > 300 )); then
      run=$(jq -r .task_id <<<"$row")
      echo "cancelling $run"
      cremind conv cancel "$run"
    fi
  done
```

## Troubleshooting

**`cremind conv send` opens a TUI when I want raw text** — Pass `--raw`
(plain text) or `--json` (structured events). The TUI is the default
only when stdout is a TTY.

**`Ctrl-C` killed the CLI but the run kept going** — Almost always
caused by exiting before the TUI handed Ctrl-C to the cancel hook.
Reattach with `cremind conv attach <id>` and Ctrl-C from there, or use
`cremind conv cancel <task_id>` directly.

**`cremind conv cancel` says `no active run for that id`** — You passed
a conversation id, not a run id. Run ids live in the `task_id` column
of `cremind conv list`.

**TUI rendering looks broken on Windows** — The CLI uses ANSI
escapes. Set `OPA_NO_COLOR=1` to strip color (the layout still works);
on legacy `cmd.exe` use Windows Terminal or PowerShell 7+ for proper
rendering.

**`get --detail` opens with empty content** — The TUI replay rebuilds
the trace from `thinking_steps` persisted on each assistant message.
Conversations created on older server versions may not have those
fields and will appear empty in detail mode; the non-`--detail` view
still shows the message text.

**`delete-all` removed too much** — Conversations are scoped to the
active profile, but they cannot be recovered. If you regularly need
this, work under a dedicated test profile so production data is
isolated.
