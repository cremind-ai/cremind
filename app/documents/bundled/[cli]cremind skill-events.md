---
description: "Subscribe to and manage **skill events and notifications**: `list`, `edit`, `pause`, `resume`, or `delete` a skill's event subscriptions for the active profile, `simulate` an event by dropping a markdown file in the watched folder, `stream` events and `notifications` over SSE, browse the events a skill declares (`events <skill>`), and check or start its listener daemon (`listener-status`, `listener-start`). Given an **event id / subscription id** copied from the web UI's Events page, use it here with `edit`, `pause`, `resume`, `delete`, or `simulate` to answer questions about that event or change it. `pause` keeps a subscription but stops it firing (`resume` re-enables it) without touching the skill's shared listener. Use this for events emitted by installed skills — distinct from filesystem (`cremind file-watchers`) and time (`cremind calendar`) events."
---

# `cremind skill-events` — Skill Event Subscriptions and Notifications

`cremind skill-events` is the CLI for managing the
event-driven side of Cremind skills. A *skill* (declared by a
`SKILL.md`) can declare *events* it watches for; when an event fires,
it can spawn a conversation, run a script, or notify the user. This
group lets you inspect those subscriptions, tail live events, and
control the listener daemons that emit them.

Each time a subscribed skill event *fires*, that single firing now runs in its
own isolated, hidden per-run conversation, tracked with a status and token
usage. Browse that run history — and reply to a firing that paused to ask you
something — with `cremind event-runs` (`cremind event-runs list --kind skill`).

The group covers four orthogonal concerns:

- **Subscriptions** — `list`, `edit`, `pause`, `resume`, `delete`. Each
  subscription binds an event type to a skill (and optionally to a
  conversation); `edit` changes the trigger (validated against the skill's
  declared events) and/or the action. `pause <id>` retains the subscription
  but stops it firing runs (it's skipped at dispatch); `resume <id>` re-enables
  it. Pausing one subscription never stops the skill's shared listener, so its
  sibling subscriptions keep firing.
- **Live streaming** — `stream` (admin-wide snapshot) and
  `notifications` (per-profile notifications) emit Server-Sent Events
  until interrupted with Ctrl-C.
- **Skill metadata** — `events <skill>` lists what events a skill
  declares.
- **Listener daemons** — `listener-status <skill>`,
  `listener-start <skill>`. The listener daemon watches the
  filesystem (or other source) and posts events back to the server.
- **Manual testing** — `simulate <id>` writes a markdown file into a
  subscription's watched folder so you can fire an event without
  whatever real-world trigger normally produces it.

## Finding this in the web UI

Every operation in this group has a control on the **Skill Events**
page of the Cremind web UI:

> **Sidebar → Skill Events**

The page lists subscriptions in the left rail (matching `cremind skill-events list`),
shows recent notifications in the main area (matching
`cremind skill-events notifications`), and exposes an **Edit** button (a
dialog whose Trigger is a dropdown of the skill's declared events, plus an
Action editor — the same fields as `cremind skill-events edit`) and a
**Simulate** button on each subscription row that opens a small editor for
the markdown body (matching `cremind skill-events simulate`).

Every subscription row (and every rule card in the Tasks board view) displays
its id — the first 8 characters, labeled **"Event"**, with a copy icon that
copies the full id. That copied id is exactly the `<id>` that `edit`, `delete`,
and `simulate` accept, so when a user pastes one ("what is event id
`3f9c2a10-…`?" or "edit event `3f9c2a10-…`") use it directly. The Events page
also shows ids labeled **"Run"** — those are *executions* of an event, not the
event itself; they belong to `cremind event-runs` (`show`/`reply`/`cancel`),
not to this group. If an id labeled "Event" doesn't match a skill-event
subscription, it may be a **file-watcher** (`cremind file-watchers list`) or a
**schedule** event (`cremind calendar events`) — the Events page shows all of
those.

## Streaming output format

`cremind skill-events stream` and `cremind skill-events notifications` both
maintain a long-lived SSE connection. They print one line per event
in one of two formats:

- **Default (table mode):** `[<event_type>] <raw JSON payload>`.
- **`--json` mode:** the raw JSON payload only (one event per line).
  This is what you want for piping into `jq`.

Press Ctrl-C to exit cleanly; the server connection is dropped.

## Global flags

All `cremind skill-events` subcommands accept the root-level `--json`
flag. `CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind skill-events list`

**Purpose.** List skill event subscriptions belonging to the active
profile.

**Syntax.**

```bash
cremind skill-events list
```

**Behavior.** Renders a five-column table:

| Column         | Source                | Meaning                                                |
|----------------|-----------------------|--------------------------------------------------------|
| `ID`           | `id`                  | Subscription id (used by `delete` and `simulate`).     |
| `SKILL`        | `skill_name`          | The skill that owns the subscription.                  |
| `EVENT_TYPE`   | `event_type`          | The skill-declared event type the subscription matches.|
| `CONVERSATION` | `conversation_id`     | Conversation the subscription routes events into. Blank if none. |
| `CONV_TITLE`   | `conversation_title`  | Title of that conversation, for readability.           |

With `--json`, returns the underlying array.

**Example.**

```bash
$ cremind skill-events list
ID         SKILL          EVENT_TYPE  CONVERSATION  CONV_TITLE
sub_19a8   daily-brief    morning     c_82bc        Daily Brief
sub_4f02   review-pr      pr-opened
```

### `cremind skill-events delete`

**Purpose.** Remove a subscription. Future events of that type for
that skill stop being routed.

**Syntax.**

```bash
cremind skill-events delete <id>
```

**Behavior.** Silent on success.

**Example.**

```bash
$ cremind skill-events delete sub_19a8
```

### `cremind skill-events edit`

**Purpose.** Change an existing subscription's trigger and/or action.
Only the flags you pass are updated. A skill-event subscription is one
row per trigger, so `--trigger` re-points *this* row to a different event
the skill declares; to watch an additional event, create a separate
subscription rather than editing this one.

**Syntax.**

```bash
cremind skill-events edit <id> [--trigger <event_type>] [--action "<instruction>"]
```

**Flags.**

| Flag        | Type   | Default | Meaning                                                                                     |
|-------------|--------|---------|---------------------------------------------------------------------------------------------|
| `--trigger` | string | —       | New event type. Must be one the skill declares (see `cremind skill-events events <skill>`); an undeclared value is rejected. |
| `--action`  | string | —       | New natural-language instruction the assistant runs when the event fires. Cannot be empty.  |

Pass at least one flag, or the command exits with "nothing to update".

**Behavior.** PATCHes `/api/skill-events/{id}`. On success prints a
key-value table with the updated `id`, `skill_name`, `event_type`,
`action`, and `conversation_id`. With `--json`, returns the updated row.
No listener restart is needed — the blanket per-profile watch resolves
the new trigger on the next firing.

**Examples.**

```bash
# Re-point a subscription to a different declared event
$ cremind skill-events edit sub_19a8 --trigger evening

# Change just the action
$ cremind skill-events edit sub_4f02 --action "summarize the PR and post it to #eng"
```

### `cremind skill-events simulate`

**Purpose.** Drop a markdown file under the subscription's watched
events folder, simulating a real event firing. Useful for development
and dry-running event handlers.

**Syntax.**

```bash
cremind skill-events simulate <id> [--filename <name>]      # body read from stdin
```

**Arguments** (required):

- `<id>` — Subscription to simulate against.

**Flags.**

| Flag         | Type   | Default | Meaning                                                                |
|--------------|--------|---------|------------------------------------------------------------------------|
| `--filename` | string | `""`    | Name of the file dropped in the folder. If omitted, a unique `simulate-*.md` name is chosen. |

**Behavior.** Reads the markdown body from **stdin** until EOF and
posts it to the server, which writes it under the subscription's
watched folder. The skill's listener daemon then picks it up and
routes it through the normal event pipeline. Silent on success.

**Examples.**

```bash
# From a file
$ cremind skill-events simulate sub_19a8 --filename morning-brief.md < morning.md

# From a heredoc
$ cremind skill-events simulate sub_4f02 <<'EOF'
# PR opened
- Author: li
- Repo:  cremind
- URL:   https://github.com/.../pull/42
EOF
```

### `cremind skill-events stream`

**Purpose.** Tail the server-wide skill-events admin snapshot stream.
The stream emits one event per change to *any* subscription, listener
status, or routed event.

**Syntax.**

```bash
cremind skill-events stream
```

**Behavior.** Long-lived SSE connection. See [Streaming output
format](#streaming-output-format) for the per-line format. Ctrl-C to
exit.

**Example.**

```bash
$ cremind skill-events stream
[subscription_added] {"id":"sub_19a8","skill_name":"daily-brief",...}
[event_routed]       {"subscription_id":"sub_19a8","conversation_id":"c_82bc",...}
```

### `cremind skill-events notifications`

**Purpose.** Tail just the active profile's per-skill notifications —
the same notification badges that appear on the web UI's sidebar.

**Syntax.**

```bash
cremind skill-events notifications [--since <millis>]
```

**Flags.**

| Flag       | Type  | Default | Meaning                                                  |
|------------|-------|---------|----------------------------------------------------------|
| `--since`  | int64 | `0`     | Resume cursor (Unix milliseconds). Replays everything from this timestamp forward, then continues live. |

**Behavior.** Long-lived SSE connection. With `--since`, the server
backfills any notifications recorded at or after the given timestamp
before going live, so you can resume after a disconnect without
losing events.

**Example.**

```bash
$ cremind skill-events notifications --since 1746201600000
[notification] {"skill":"daily-brief","summary":"Daily brief ready","ts":1746205200000}
```

### `cremind skill-events events`

**Purpose.** List the events a particular skill declares (read from
its `SKILL.md`). This is what the **Subscribe** dialog in the UI
populates from.

**Syntax.**

```bash
cremind skill-events events <skill>
```

**Arguments** (required):

- `<skill>` — Skill name (matches `skill_name` from `list`).

**Behavior.** Pretty-prints the JSON document the server returns,
typically an array of `{type, description}` objects. With `--json`,
the same JSON is emitted unindented.

**Example.**

```bash
$ cremind skill-events events daily-brief
{
  "skill_name": "daily-brief",
  "events": [
    {"type": "morning", "description": "Fired every weekday at 09:00"},
    {"type": "evening", "description": "Fired every weekday at 18:00"}
  ]
}
```

### `cremind skill-events listener-status`

**Purpose.** Check whether a skill's listener daemon is alive, by
asking the server for the daemon's most recent heartbeat.

**Syntax.**

```bash
cremind skill-events listener-status <skill>
```

**Behavior.** Prints a key-value table:

| Row              | Meaning                                                          |
|------------------|------------------------------------------------------------------|
| `skill_name`     | The skill being checked.                                         |
| `running`        | `yes` if a heartbeat was received recently, `no` otherwise.      |
| `last_heartbeat` | Server's view of the most recent heartbeat (timestamp or `null`).|
| `autostart_id`   | The autostart-process row backing this listener, if any.         |
| `command`        | The exact command line the listener runs as.                     |

With `--json`, returns the underlying object.

**Example.**

```bash
$ cremind skill-events listener-status daily-brief
skill_name      daily-brief
running         yes
last_heartbeat  2026-05-02T14:00:00Z
autostart_id    a_8c14
command         /usr/bin/python /skills/daily-brief/listen.py
```

### `cremind skill-events listener-start`

**Purpose.** Start (or resume) a skill's listener daemon as an
autostart process — so it relaunches at server boot.

**Syntax.**

```bash
cremind skill-events listener-start <skill>
```

**Behavior.** Spawns the daemon if it is not already running and
records an autostart row. Idempotent: a second call against an
already-running listener returns the existing process and autostart
ids without duplicating.

Prints a key-value table:

| Row             | Meaning                                                    |
|-----------------|------------------------------------------------------------|
| `process_id`    | Live process id (use with `cremind proc attach`).              |
| `autostart_id`  | Autostart registration id (use with `cremind proc autostart delete` to undo). |

**Example.**

```bash
$ cremind skill-events listener-start daily-brief
process_id    p_9d72
autostart_id  a_8c14
```

## Worked examples

### Inspect what a skill exposes and subscribe

```bash
# What events does the skill declare?
$ cremind skill-events events daily-brief

# Subscribe via the UI (the CLI doesn't have a `subscribe` subcommand —
# subscriptions are created when a skill is bound to a conversation in
# the web UI).

# Confirm the subscription appears
$ cremind skill-events list
```

### Tail notifications and pretty-print as they arrive

```bash
$ cremind skill-events notifications --json | jq '.summary'
"Daily brief ready"
"PR opened: cremind#42"
```

### Resume notifications after a disconnect

```bash
# Note the timestamp before disconnecting (or pull it from the last
# event you saw)
$ since=$(date -d '5 minutes ago' +%s%3N)
$ cremind skill-events notifications --since "$since"
```

### Manually fire a "morning" event for development

```bash
$ cremind skill-events simulate sub_19a8 <<'EOF'
# Morning brief
- Top issue: cremind#42
- Calendar:  09:30 standup
EOF
```

### Bring a stale listener back to life

```bash
$ cremind skill-events listener-status daily-brief
skill_name      daily-brief
running         no
...
$ cremind skill-events listener-start daily-brief
process_id    p_9d72
autostart_id  a_8c14
$ cremind skill-events listener-status daily-brief
running         yes
```

## Troubleshooting

**`stream` / `notifications` exit immediately** — Almost always an
auth issue. Confirm `cremind me` works first.

**`simulate` is silent and nothing happens** — The listener for that
skill must be running for the dropped file to be picked up. Check
`cremind skill-events listener-status <skill>` and start the listener if
needed.

**No notifications even though the skill should have fired** — Three
common causes: (1) the listener is down (`listener-status`), (2) the
subscription is bound to a conversation that has been deleted (the
event is dropped), (3) the `event_type` declared by the skill changed
and the existing subscription no longer matches — re-create it from
the UI. (Note: when the **jira** skill split its old `issue_changed`
event into `issue_created`/`issue_updated`/`issue_transitioned`/
`issue_commented`, a boot-time migration auto-maps existing
`issue_changed` subscriptions, so re-subscribing is only needed if a
row was somehow missed.)

**`--since` returns nothing** — Notifications are not retained
indefinitely. If `--since` reaches further back than the server's
retention window, no backfill is produced (the live tail still works).

**Listener won't start** — `listener-start` defers to the autostart
process machinery. Use `cremind proc autostart list` to see whether a
duplicate or a stale entry is blocking the new one. If yes, delete the
stale row with `cremind proc autostart delete <id>` first.
