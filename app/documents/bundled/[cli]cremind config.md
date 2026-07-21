---
description: "Inspect, override, and reset **per-profile agent settings** with `cremind config schema`, `get`, `set`, and `reset`: set the system timezone used by the scheduler and clock, and tune the reasoning-agent loop (max steps, retries, temperature, max tokens, steps history, prompt caching, reasoning-trace replay), conversation compaction, tool-result truncation, and long-term memory. Use this to change how the agent behaves for a profile — including which timezone schedules fire in — distinct from `cremind llm` (which models/providers) and `cremind tools` (per-tool config)."
---

# `cremind config` — Per-Profile Settings Reference

`cremind config` is the CLI for inspecting and changing per-profile settings
that control the Cremind reasoning agent. Each subcommand maps one-to-one
to an action on the **Settings → Config** page in the web UI, so you can
freely switch between the two: anything you change in the CLI shows up
on that page, and anything you change there is visible to `cremind config
get`.

The settings are grouped into five areas:

- **System** — install-level preferences. The **timezone** sets the
  wall-clock zone the scheduler fires time-based events in and the agent
  reports for "what time is it". Leave it as `auto` to inherit the admin
  profile's zone (for profiles that never set their own), then the
  `CREMIND_TIMEZONE` environment variable, then the server's OS zone.
- **Reasoning Agent** — iteration limits and per-call LLM parameters
  for the agent loop that drives every conversation turn, plus the
  prompt-cache and reasoning-trace-replay switches.
- **Conversation Compaction** — folds the oldest turns into a running
  summary so long conversations stay within the model's context window.
- **Tool Result Truncation** — shortens older tool observations before
  they are re-sent to the reasoning LLM (the full result is always kept
  in the database and shown in the web UI).
- **Memory** — long-term, cross-conversation facts about the user.

Every setting has a built-in **default**. When you change a setting
with `cremind config set`, your value becomes an **override** that takes
priority over the default for your profile only; the override persists
across restarts. `cremind config reset` removes the override and the
setting goes back to its default. Overrides are scoped to one profile,
so different profiles can carry different values for the same key
without interfering with each other.

## Finding these settings in the web UI

Every key documented here also has a control on the **Settings** page
of the Cremind web UI. The path is:

> **Sidebar → Settings → Config**

The Config page is a vertical stack of cards, one card per group, in
this order:

1. **System** — the `system.*` keys (timezone).
2. **Reasoning Agent** — the `agent.*` keys.
3. **Conversation Compaction** — the `compaction.*` keys.
4. **Tool Result Truncation** — the `tool_result.*` keys.
5. **Memory** — the `memory.*` keys.

Inside each card, every row shows the field's label, a one-line
description, the current default, and a type-appropriate input — a
number spinner that enforces the min/max, or a toggle. When a value
differs from its default, a **Reset** button appears next to the row to
revert just that key. Edits are batched: a **Save changes** button at
the top of the page commits all pending edits in one go. The per-group
tables below list the exact UI label for every key so you can match it
to the row you see in the card.

## Global flags

All `cremind config` subcommands accept the root-level `--json` flag, which
forces JSON output instead of the default human-readable table:

```bash
cremind config get --json
```

## Subcommands

`cremind config` has four subcommands. Each is documented below with its
purpose, syntax, and worked examples.

### `cremind config schema`

**Purpose.** Print every configurable group and key, with each key's
type, default value, and allowed range. This is the answer to "what can
I configure?".

**Syntax.**

```bash
cremind config schema [--json]
```

**Behavior.** In the default (table) view, output is grouped by config
group, with the group's label and description, followed by one line per
key showing its dotted name, type, and default. With `--json`, the
schema is emitted as a machine-readable JSON document that also
includes each key's `min`, `max`, `step`, `label`, and `description`.

**Example (default output, abbreviated).**

```bash
$ cremind config schema
[agent] Reasoning Agent
  Controls the agent loop's iteration limits and per-call LLM parameters.
  agent.enable_prompt_cache  type=boolean default=True
  agent.max_llm_retries  type=number default=2
  agent.max_steps  type=number default=200
  agent.reasoning_max_tokens  type=number default=32768
  agent.reasoning_retry  type=number default=3
  agent.reasoning_temperature  type=number default=1.0
  agent.replay_reasoning_steps  type=boolean default=True
  agent.steps_length  type=number default=20
  ...
```

### `cremind config get`

**Purpose.** Show the *current* value of one or all settings for the
active profile, alongside the declared default so you can see at a
glance which keys you have overridden.

**Syntax.**

```bash
cremind config get [<group.key>]
```

**Arguments.**

- `<group.key>` *(optional)* — A dotted key path such as
  `agent.max_steps`. If omitted, every key is displayed.

**Behavior.** Without an argument, the CLI renders a three-column
table sorted by key, with an empty `VALUE` column whenever no override
exists. With a single argument, the CLI prints just the resolved value —
the override if one is set, otherwise the default.

**Examples.**

```bash
# All keys with their override and default values
$ cremind config get
KEY                              VALUE   DEFAULT
agent.enable_prompt_cache                true
agent.max_llm_retries                    2
agent.max_steps                  300     200
...

# A single key
$ cremind config get agent.max_steps
300

# JSON output (full structure including both maps)
$ cremind config get --json
{"values":{"agent.max_steps":300},"defaults":{"agent.max_steps":200, ...}}
```

### `cremind config set`

**Purpose.** Override a single config key for the active profile. The
override persists across restarts until you change it again or reset it.

**Syntax.**

```bash
cremind config set <group.key> <value>
```

**Arguments** (both required):

- `<group.key>` — A dotted key path such as `agent.max_steps`.
- `<value>` — The new value as a string. The CLI converts the string
  to the appropriate type before submitting it, and the value is then
  validated against the key's declared type and allowed range.

**Type coercion.** The string-to-type rules are:

- `"true"` or `"false"` (case-insensitive) → boolean
- An integer literal (e.g. `200`, `-3`) → integer
- A decimal or exponential literal (e.g. `0.5`, `1e-3`) → float
- Anything else → string

If the coerced value violates the schema (wrong type, out of range,
unknown key) the override is **not** applied and an error is reported.

**Examples.**

```bash
# Integer
cremind config set agent.max_steps 300

# Float
cremind config set agent.reasoning_temperature 0.5

# Boolean
cremind config set memory.enabled true
```

### `cremind config reset`

**Purpose.** Drop a single override and revert that key to its declared
default.

**Syntax.**

```bash
cremind config reset <group.key>
```

**Arguments** (required):

- `<group.key>` — A dotted key path such as `agent.max_steps`.

**Behavior.** Removes the override for the given key on the active
profile. The next read returns the key's declared default.

**Example.**

```bash
cremind config reset agent.max_steps
```

## Available config keys

The defaults shown below are the values that apply when no override
has been set. Run `cremind config schema --json` to confirm the live
values for your installation.

### Group `system` — System

Install-level preferences. The timezone sets the wall-clock zone the scheduler
uses to fire time-based events and the agent uses to answer "what time is it".

**Settings → Config card:** **System** (the first card on the page).

| Key               | UI label | Type   | Default | Range | Meaning                                                                                  |
|-------------------|----------|--------|---------|-------|------------------------------------------------------------------------------------------|
| `system.timezone` | Timezone | string | `auto`  | IANA name, UTC offset, or `auto` | Wall-clock zone for schedules and clock reads, given as **either** an IANA name (e.g. `Asia/Tokyo`, `America/New_York`, `UTC`) **or** a whole-hour UTC offset (e.g. `+07:00`, `-05:00`) — pick one format. `auto` means: inherit the **admin** profile's zone if you have never set your own; else the `CREMIND_TIMEZONE` env var; else the server's OS zone. Setting your own value stops the admin default from applying to you. |

**Two formats (IANA name or UTC offset).** On the Config page the Timezone field
has a format toggle: choose **IANA name** (a searchable zone list, with DST
handled automatically) or **UTC offset** (a fixed offset like `+07:00`, no DST).
The two are mutually exclusive — the stored value is one or the other. From the
CLI, just pass whichever form you want: `cremind config set system.timezone
Asia/Tokyo` or `cremind config set system.timezone +07:00`. Offsets are
**whole hours only** — `+HH:00` / `-HH:00` (also written `UTC+07:00`, `+0800`,
`Z`), range-checked to `[-12:00, +14:00]`; a partial-hour offset like `+05:30`
is rejected.

**Timezone resolution (why local ≠ VPS).** With `auto`, a profile that has
never set its own zone follows the admin profile's `system.timezone`; if the
admin also leaves it `auto`, Cremind falls back to the `CREMIND_TIMEZONE`
environment variable, and finally to the server's OS timezone. A Docker/VPS
install runs in UTC by default, which is why schedules there fire in UTC until
you set this — set the admin profile's zone (or `CREMIND_TIMEZONE`) to your
local zone. An invalid IANA name or offset is rejected by `cremind config set`.

### Group `agent` — Reasoning Agent

Controls the agent loop's iteration limits and per-call LLM parameters.

**Settings → Config card:** **Reasoning Agent** (the second card on the
page).

| Key                            | UI label              | Type    | Default | Range          | Meaning                                                                             |
|--------------------------------|-----------------------|---------|---------|----------------|-------------------------------------------------------------------------------------|
| `agent.max_steps`              | Max steps             | number  | `200`   | 1 – 500        | Maximum tool-calling iterations before the agent stops a turn.                      |
| `agent.max_llm_retries`        | Max LLM retries       | number  | `2`     | 0 – 10         | How many times the loop retries after an LLM error before giving up.                |
| `agent.reasoning_temperature`  | Reasoning temperature | number  | `1.0`   | 0 – 2 (±0.1)   | Sampling temperature for the main reasoning LLM call.                               |
| `agent.reasoning_max_tokens`   | Reasoning max tokens  | number  | `32768` | 256 – 131072   | Output token cap for the reasoning LLM call.                                        |
| `agent.reasoning_retry`        | Per-call retry count  | number  | `3`     | 0 – 10         | How many times an individual reasoning LLM call retries on transient errors.        |
| `agent.steps_length`           | Steps history length  | number  | `20`    | 5 – 500        | Maximum number of recent step entries kept in the prompt context. Older entries are dropped once this is exceeded. |
| `agent.enable_prompt_cache`    | Prompt caching        | boolean | `true`  | —              | Reuse the cached system+tools prefix across reasoning steps to cut input tokens. Anthropic uses explicit cache markers; OpenAI-family providers cache automatically. Harmless on providers without cache support. |
| `agent.replay_reasoning_steps` | Replay reasoning steps| boolean | `true`  | —              | Send each prior turn's full tool-call/tool-result trace back into history (not just the final answer), so the model resumes the real transcript and the cached prefix covers the reasoning. Larger prompts — cheap on Anthropic (cached), but extra input tokens on providers without caching. |

### Group `compaction` — Conversation Compaction

Keeps long conversations within budget by folding the oldest turns into a
running summary (via the main model, from its warm cached prefix) while recent
turns stay verbatim. Replaces fixed token-window truncation and is prompt-cache
friendly — the summary at the front stays byte-stable between compactions. By
default it is **suggest-only** (the UI proposes compacting and the user clicks);
turn on `auto_compact_enabled` to fold without a click — useful for the CLI,
headless, and event runs that have no popup. A deterministic floor always clamps
the assembled prompt to the model's window, so it can **never overflow**, even
when compaction is disabled.

**Settings → Config card:** **Conversation Compaction** (the third card on
the page).

| Key                                   | UI label                    | Type    | Default  | Range           | Meaning                                                                                  |
|---------------------------------------|-----------------------------|---------|----------|-----------------|------------------------------------------------------------------------------------------|
| `compaction.enabled`                  | Enabled                     | boolean | `true`   | —               | When off, summarization is skipped; the deterministic floor still clamps the prompt to the model's window so it can never overflow. |
| `compaction.auto_compact_enabled`     | Automatic compaction        | boolean | `false`  | —               | Fold automatically (no click) once context crosses a high band above the suggestion threshold. Off = today's suggest-only behavior for UI clients; useful for CLI/headless/event runs. Safety is guaranteed by the floor regardless. |
| `compaction.compact_threshold_percent` | Compaction threshold (% of context window) | number | `85` | 10 – 100 (±5) | Suggest folding the oldest turns once context reaches this percentage of the model's context window. Lower it to compact earlier. |
| `compaction.keep_recent_tokens`       | Keep-recent target (tokens) | number  | `12000`  | 500 – 500000    | After a compaction, keep about this many tokens of recent turns verbatim (the hysteresis band that keeps the cached summary stable across turns). Clamped down at fold time when needed to hit the fold target. |
| `compaction.keep_recent_messages`     | Keep-recent messages (floor) | number | `4`      | 0 – 50          | Enforced floor — never fold below this many of the most recent messages, even if the tail is over the keep-recent target. |
| `compaction.fold_target_percent`      | Fold target (% of context window) | number | `60`  | 20 – 90 (±5)    | A fold aims to land the prompt at/below this fraction of the window (summary + kept tail + reply reserve), so folds stay rare and the floor never fires the turn after one. Must be below the threshold. |
| `compaction.temperature`              | Temperature                 | number  | `0.3`    | 0 – 2 (±0.1)    | Sampling temperature for the summarization call.                                         |
| `compaction.max_tokens`               | Max tokens                  | number  | `2048`   | 128 – 8192      | Output token cap for the running summary (also its hard size bound).                     |
| `compaction.retry`                    | Retry count                 | number  | `2`      | 0 – 10          | Retries on transient summarization LLM errors.                                           |

### Group `tool_result` — Tool Result Truncation

Limits applied to tool observations when they are re-sent to the reasoning
LLM. The full result is always stored in the database and shown in the web UI;
only the copy fed back into the next reasoning prompt is shortened.

**Settings → Config card:** **Tool Result Truncation** (the fourth card on
the page).

| Key                          | UI label                       | Type    | Default | Range           | Meaning                                                                                  |
|------------------------------|--------------------------------|---------|---------|-----------------|------------------------------------------------------------------------------------------|
| `tool_result.enabled`        | Enabled                        | boolean | `true`  | —               | When on, older tool observations are shortened to a head/tail excerpt before being included in the next reasoning prompt. |
| `tool_result.max_tokens`     | Per-observation token threshold | number | `1000`  | 100 – 200000    | An older observation longer than this many tokens is replaced with a head excerpt + truncation marker + tail excerpt. |
| `tool_result.preserve_recent`| Recent observations kept full  | number  | `1`     | 0 – 10          | The N most recent observations always pass through at full length, regardless of size.   |
| `tool_result.head_tokens`    | Head excerpt tokens            | number  | `200`   | 0 – 10000       | Tokens kept from the beginning of a truncated observation.                               |
| `tool_result.tail_tokens`    | Tail excerpt tokens            | number  | `200`   | 0 – 10000       | Tokens kept from the end of a truncated observation.                                      |

### Group `memory` — Memory

Lets the agent recall durable, long-term facts about the user across
conversations. Long-term memory is extracted together with the conversation
summary at the compaction fold (so it **requires Compaction enabled**). When
Vector Embedding is on, facts are stored in the vector store and retrieved by
relevance; otherwise they live in a small size-capped queue. Off by default.

**Settings → Config card:** **Memory** (the fifth card on the page).

| Key                              | UI label                   | Type    | Default | Range    | Meaning                                                                                  |
|----------------------------------|----------------------------|---------|---------|----------|------------------------------------------------------------------------------------------|
| `memory.enabled`                 | Enabled                    | boolean | `false` | —        | Master switch for long-term memory. Requires Compaction enabled (memory is generated at the compaction fold). When off, the agent reads and writes no long-term memory. |
| `memory.long_term_queue_size`    | Long-term queue size       | number  | `20`    | 1 – 100  | Max long-term facts kept per profile in the DB queue (Vector-Embedding-OFF mode). Oldest is dropped on overflow. Ignored in vector mode (unlimited). |
| `memory.long_term_max_tokens`    | Long-term entry max tokens | number  | `50`    | 10 – 500 | Each long-term fact is clipped to at most this many tokens.                              |
| `memory.long_term_retrieve_limit`| Long-term retrieval limit  | number  | `10`    | 1 – 50   | Top-K long-term facts retrieved from the vector store for the prompt (Vector-Embedding-ON mode). |

## Worked examples

### Inspect everything

```bash
$ cremind config get
```

### Set the timezone schedules fire in (fixes UTC on a VPS)

```bash
# Set the admin profile's zone — other profiles that never set their own
# inherit it. Do this on the admin profile after a Docker/VPS install.
$ cremind config set system.timezone Asia/Ho_Chi_Minh
$ cremind config get system.timezone
Asia/Ho_Chi_Minh

# Or express it as a fixed UTC offset instead of an IANA name:
$ cremind config set system.timezone +07:00

# Revert to auto (inherit admin / CREMIND_TIMEZONE / OS zone)
$ cremind config reset system.timezone
```

### Raise the step ceiling for long tasks

```bash
$ cremind config set agent.max_steps 300
$ cremind config get agent.max_steps
300
```

### Undo the override

```bash
$ cremind config reset agent.max_steps
$ cremind config get agent.max_steps
200
```

### Compact sooner (fold before the context window fills)

```bash
$ cremind config set compaction.compact_threshold_percent 70
$ cremind config set compaction.keep_recent_tokens 8000
```

### Turn on long-term memory

```bash
# Memory needs compaction enabled (facts are written at the compaction fold).
$ cremind config set compaction.enabled true
$ cremind config set memory.enabled true
```

### Pipe the schema into `jq`

```bash
$ cremind config schema --json | jq '.groups.agent.fields | keys'
[
  "enable_prompt_cache",
  "max_llm_retries",
  "max_steps",
  "reasoning_max_tokens",
  "reasoning_retry",
  "reasoning_temperature",
  "replay_reasoning_steps",
  "steps_length"
]
```

## Troubleshooting

**`cremind config set` is rejected as a bad request** — The value failed
validation. Common causes:

- The value is out of the key's declared `min`/`max` range (see the
  keys table above).
- The dotted key is misspelled or refers to a non-existent group/key.
- The coerced type does not match the key's declared type (e.g.
  setting a `number` key to a non-numeric string).

Run `cremind config schema` to confirm the exact key name and allowed
range, then retry.

**`memory.enabled` is on but nothing is remembered** — Long-term memory is
generated at the compaction fold, so it requires `compaction.enabled` to be
`true`. Turn compaction on (and have a conversation long enough to trigger a
fold) for memory to start accumulating.

**Override "doesn't seem to apply"** — Overrides are stored per profile.
An override set under one profile is invisible to another; if you have
switched profiles, you are reading a different set of overrides.
