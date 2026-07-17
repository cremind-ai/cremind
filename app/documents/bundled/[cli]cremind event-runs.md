---
description: "View and manage **event runs** — the per-trigger execution history of automatic event rules (skill, file-watcher, schedule). Each fired trigger runs in its own isolated conversation with a status (running/pending/completed/failed/cancelled) and token usage; reply to runs pending your input, cancel a running run, and inspect or delete run history."
---

# `cremind event-runs` — Event Run History

`cremind event-runs` is the CLI for **event runs**: the per-trigger execution
history of Cremind's automatic event rules. Whenever a rule fires — a **skill
event**, a **file-watcher** filesystem change, or a **schedule / calendar**
event — that single firing runs in its own isolated, hidden conversation and is
recorded as an `event_runs` row. Each row carries a **status**, a natural-
language **label** and **action**, the originating **subscription id**, and a
per-run **token-usage** rollup.

This is the run-history counterpart to the three event-*source* command groups
(`cremind skill-events`, `cremind file-watchers`, `cremind calendar`), which
manage the *rules*; `event-runs` shows what those rules *did* each time they
fired.

A run moves through these statuses:

| Status      | Meaning                                                              |
|-------------|---------------------------------------------------------------------|
| `running`   | The agent is actively working the trigger.                          |
| `pending`   | The run paused to ask **you** a question, **or ended with an unfinished task list** — reply to resume/continue it. |
| `completed` | The run finished with every task in its todo list completed (a run that drove no todo list completes when its turn ends). |
| `failed`    | The run errored out (see the `error` field via `show`).             |
| `cancelled` | The run was cancelled (e.g. deleted while running).                 |

## Finding this in the web UI

> **Sidebar → Events**

Each event rule on the Events page has a run-history child table (matching
`event-runs list --subscription <id>`), and clicking a run opens the
run-detail drawer (matching `event-runs show`). The reply box on a pending
run's drawer corresponds to `event-runs reply`.

## Global flags

All `cremind event-runs` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand. Runs are scoped to the
caller's own profile.

## Subcommands

### `cremind event-runs list`

**Purpose.** List event runs for the active profile, newest first.

```bash
cremind event-runs list [--kind <source>] [--subscription <id>]
                        [--status <status>] [--limit <n>]
```

**Flags.**

| Flag             | Default | Meaning                                                                                               |
|------------------|---------|-------------------------------------------------------------------------------------------------------|
| `--kind`         | all     | Filter by event source: `skill_event`, `file_watcher`, or `schedule`. Friendly aliases are accepted and mapped: `skill` → `skill_event`, `file-watcher` (or `watcher`) → `file_watcher`, `calendar` → `schedule`. |
| `--subscription` | all     | Filter to a single originating subscription / event id.                                               |
| `--status`       | all     | Filter by status: `running`, `pending`, `completed`, `failed`, or `cancelled`.                        |
| `--limit`        | `50`    | Maximum runs to return (server caps at 200).                                                          |

Renders a `FIRED / STATUS / LABEL / TOKENS / COST / TURNS / RUN ID` table. The
`STATUS` column is color-coded (pending is highlighted). `FIRED` is the local
time the trigger fired. `COST` is the run's estimated dollar cost and `TOKENS`
its total token count. `RUN ID` is a **shortened** run id — pass `--json` to
get the full id you feed to `show` / `reply` / `delete`. A `shown / total`
footer follows the table; an empty result prints `no event runs match.`.

With `--json`, returns the raw `{runs: [...], total: N}` object (each run in the
full RunJSON shape, with full ids and the complete usage breakdown).

**Examples.**

```bash
# Everything, newest first
$ cremind event-runs list

# Only runs still waiting on my input
$ cremind event-runs list --status pending

# Recent schedule/calendar-triggered runs
$ cremind event-runs list --kind schedule --limit 20

# All runs from one file-watcher subscription (grab the full id with --json)
$ cremind event-runs list --subscription fw_a3f1 --json | jq '.runs[].status'
```

### `cremind event-runs show`

**Purpose.** Show one run in detail.

```bash
cremind event-runs show <run-id>
```

Prints a key/value panel: `id`, `status`, `source_kind`, `subscription_id`,
`label`, `action`, `conversation_id`, `run_id`, `turn_count`, the fired /
updated / finished timestamps, and — when present — the `pending_question` and
`error`. A `--- usage ---` block follows with the full token breakdown
(`input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`,
`output_tokens`, `total_tokens`, `total_usd`, `request_count`).

When the run has a `conversation_id`, the panel also prints hints to view the
transcript (`cremind conv get <conversation_id>`) and, for a pending run, to
reply (`cremind event-runs reply <run-id> "..."`).

With `--json`, returns the raw RunJSON object.

```bash
$ cremind event-runs show 3f9c2a10-...-b1
```

### `cremind event-runs reply`

**Purpose.** Reply to a run that is **pending** your input. This sends your
message into the run's hidden conversation, resuming it.

```bash
cremind event-runs reply <run-id> "<message>"
```

The command looks the run up, finds its `conversation_id`, and posts your
message to `POST /api/conversations/{conversation_id}/messages`. It prints a
confirmation on success.

- If the run has **no conversation yet**, there is nothing to reply to and the
  command says so.
- If the run is **not** `pending`, the command prints a note but still sends the
  message — the backend resumes the conversation if it can.

```bash
$ cremind event-runs reply 3f9c2a10-...-b1 "yes, go ahead and archive them"
sent to conversation c_82bc.
```

### `cremind event-runs delete`

**Purpose.** Delete a run and its hidden conversation. If the run is still
running it is cancelled first. The run's **usage rollup survives** the delete
(so profile/aggregate cost totals stay accurate).

```bash
cremind event-runs delete <run-id>
```

Silent on success. (Equivalent to `DELETE /api/event-runs/{id}`.)

```bash
$ cremind event-runs delete 3f9c2a10-...-b1
```

### `cremind event-runs cancel`

**Purpose.** Cancel a run that is currently **running**, without deleting it —
the run and its transcript stay, its status moves to `cancelled`.

```bash
cremind event-runs cancel <run-id>
```

Prints `cancelled` on success, or `run was not running` when the run had
already finished (or never started) — that case is a no-op, not an error. With
`--json`, prints `{"cancelled": <bool>}`. (Equivalent to
`POST /api/event-runs/{id}/cancel`.) Use `delete` instead if you also want to
remove the run and its conversation.

```bash
$ cremind event-runs cancel 3f9c2a10-...-b1
cancelled
```

## Worked examples

### Triage runs that are waiting on me, then answer one

```bash
$ cremind event-runs list --status pending
FIRED                STATUS   LABEL              TOKENS  COST     TURNS  RUN ID
2026-07-03 09:12:04  pending  Archive old PRs    18422   $0.0412  2      3f9c2a10
...
# Grab the full id and read the question
$ cremind event-runs list --status pending --json | jq -r '.runs[0].id, .runs[0].pending_question'
3f9c2a10-...-b1
"Archive all 7 stale PRs, or only the ones with no activity in 90 days?"
$ cremind event-runs reply 3f9c2a10-...-b1 "only the ones with no activity in 90 days"
```

### Audit what a schedule fired last week and read one transcript

```bash
$ cremind event-runs list --kind schedule --limit 10
$ cremind event-runs show 3f9c2a10-...-b1
$ cremind conv get c_82bc          # the run's transcript
```

## Troubleshooting

**`run not found` on `show` / `reply`** — The `RUN ID` shown by `list` is
**truncated**. Pass `--json` to `list` and copy the full `id`, then use that.
Runs are also profile-scoped: you can only see your own profile's runs.

**`reply` says "no conversation yet"** — The run hasn't started a conversation
(it may still be initializing or it failed before one was created). There's
nothing to reply to; re-check `event-runs show` for the `status` and `error`.

**A run is stuck `pending` and I don't remember the question** — Run
`cremind event-runs show <id>` (or read `pending_question` from `--json`); the
full text of what the run asked is stored there.

**Deleted a run but my usage totals didn't drop** — By design. Deleting a run
tears down its conversation but keeps the per-run usage rollup, so `cremind usage`
and the Usage & Cost dashboard stay accurate.

## Related

- `cremind skill-events` / `cremind file-watchers` / `cremind calendar` — the
  three event *sources* whose triggers produce these runs.
- `cremind conv get <id>` — view a run's full conversation transcript.
- `cremind usage` — profile-wide token & cost totals (per-run usage rolls up
  into these).
- `app/api/event_runs.py` — the `/api/event-runs` API these commands wrap.
