---
description: "Complete reference for the `cremind config` CLI command — the terminal-side counterpart to the **Settings → Config** page in the Cremind web UI — covering how to inspect, override, and reset per-profile agent settings using the four subcommands `cremind config schema`, `cremind config get`, `cremind config set`, and `cremind config reset`. Lists every configurable key across the reasoning agent loop (max steps, retries, temperature, max tokens, steps history), conversation history token budgets, skill classifier, and trace summarizer, with dotted names, types, defaults, allowed ranges, and the matching card and field label on the Settings page so users can switch between CLI and UI without losing their place."
---

# `cremind config` — Per-Profile Settings Reference

`cremind config` is the CLI for inspecting and changing per-profile settings
that control the Cremind reasoning agent. Each subcommand maps one-to-one
to an action on the **Settings → Config** page in the web UI, so you can
freely switch between the two: anything you change in the CLI shows up
on that page, and anything you change there is visible to `cremind config
get`.

The settings are grouped into four areas:

- **Reasoning Agent** — iteration limits and per-call LLM parameters
  for the ReAct loop that drives every conversation turn.
- **Conversation History** — token budgets for the message window
  assembled before each LLM call.
- **Skill Classifier** — the lightweight LLM that decides whether an
  incoming request maps to a registered skill.
- **Trace Summarizer** — the LLM that compresses long reasoning traces
  back into the conversation history when they grow too large.

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

1. **Reasoning Agent** — the `agent.*` keys.
2. **Conversation History** — the `history.*` keys.
3. **Skill Classifier** — the `skill_classifier.*` keys.
4. **Trace Summarizer** — the `summarizer.*` keys.

Inside each card, every row shows the field's label, a one-line
description, the current default, and a type-appropriate input — a
number spinner that enforces the min/max, a toggle, or a dropdown.
When a value differs from its default, a **Reset** button appears next
to the row to revert just that key. Edits are batched: a **Save
changes** button at the top of the page commits all pending edits in
one go. The per-group tables below list the exact UI label for every
key so you can match it to the row you see in the card.

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
  Controls the ReAct loop's iteration limits and per-call LLM parameters.
  agent.max_llm_retries  type=number default=2
  agent.max_steps  type=number default=40
  agent.reasoning_max_tokens  type=number default=32768
  agent.reasoning_retry  type=number default=3
  agent.reasoning_temperature  type=number default=1
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
agent.max_llm_retries                    2
agent.max_steps                  80      40
agent.reasoning_max_tokens               32768
...

# A single key
$ cremind config get agent.max_steps
80

# JSON output (full structure including both maps)
$ cremind config get --json
{"values":{"agent.max_steps":80},"defaults":{"agent.max_steps":40, ...}}
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
- An integer literal (e.g. `40`, `-3`) → integer
- A decimal or exponential literal (e.g. `0.5`, `1e-3`) → float
- Anything else → string

If the coerced value violates the schema (wrong type, out of range,
unknown key) the override is **not** applied and an error is reported.

**Examples.**

```bash
# Integer
cremind config set agent.max_steps 80

# Float
cremind config set agent.reasoning_temperature 0.5

# Boolean (none of the current keys are boolean, but the coercion works)
cremind config set some.flag true
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

### Group `agent` — Reasoning Agent

Controls the ReAct loop's iteration limits and per-call LLM parameters.

**Settings → Config card:** **Reasoning Agent** (the first card on the
page).

| Key                            | UI label              | Type   | Default | Range          | Meaning                                                                             |
|--------------------------------|-----------------------|--------|---------|----------------|-------------------------------------------------------------------------------------|
| `agent.max_steps`              | Max steps             | number | `40`    | 1 – 200        | Maximum ReAct iterations before the agent stops a turn.                             |
| `agent.max_llm_retries`        | Max LLM retries       | number | `2`     | 0 – 10         | How many times the loop retries after an LLM error before giving up.                |
| `agent.reasoning_temperature`  | Reasoning temperature | number | `1.0`   | 0 – 2 (±0.1)   | Sampling temperature for the main reasoning LLM call.                               |
| `agent.reasoning_max_tokens`   | Reasoning max tokens  | number | `32768` | 256 – 131072   | Output token cap for the reasoning LLM call.                                        |
| `agent.reasoning_retry`        | Per-call retry count  | number | `3`     | 0 – 10         | How many times an individual reasoning LLM call retries on transient errors.        |
| `agent.steps_length`           | Steps history length  | number | `80`    | 5 – 500        | Maximum number of recent ReAct step entries kept in the prompt context. Older entries are dropped once this is exceeded. |

### Group `compaction` — Conversation Compaction

Keeps long conversations within budget by folding the oldest turns into a
running summary (via the low model group) while recent turns stay verbatim.
Replaces fixed token-window truncation and is prompt-cache friendly — the
summary at the front stays byte-stable between compactions.

**Settings → Config card:** **Conversation Compaction** (the second card on
the page).

| Key                                   | UI label                    | Type    | Default  | Range           | Meaning                                                                                  |
|---------------------------------------|-----------------------------|---------|----------|-----------------|------------------------------------------------------------------------------------------|
| `compaction.enabled`                  | Enabled                     | boolean | `true`   | —               | When off, full history is sent (bounded only by the model's context window).             |
| `compaction.compact_threshold_tokens` | Compaction threshold        | number  | `100000` | 1000 – 1000000  | Fold the oldest turns into the summary once the verbatim tail's token count crosses this. |
| `compaction.keep_recent_tokens`       | Keep-recent target          | number  | `40000`  | 500 – 500000    | After a compaction, keep about this many tokens of recent turns verbatim (hysteresis band). |
| `compaction.keep_recent_messages`     | Keep-recent messages floor  | number  | `4`      | 0 – 50          | Never fold below this many of the most recent messages.                                  |
| `compaction.temperature`              | Temperature                 | number  | `0.3`    | 0 – 2 (±0.1)    | Sampling temperature for the summarization call.                                         |
| `compaction.max_tokens`               | Max tokens                  | number  | `2048`   | 128 – 8192      | Output token cap for the running summary (also its hard size bound).                     |
| `compaction.retry`                    | Retry count                 | number  | `2`      | 0 – 10          | Retries on transient summarization LLM errors.                                           |

### Group `skill_classifier` — Skill Classifier

Lightweight LLM that decides whether a request maps to a registered
skill.

**Settings → Config card:** **Skill Classifier** (the third card on the
page).

| Key                            | UI label    | Type   | Default | Range          | Meaning                                                            |
|--------------------------------|-------------|--------|---------|----------------|--------------------------------------------------------------------|
| `skill_classifier.temperature` | Temperature | number | `0.0`   | 0 – 2 (±0.1)   | Sampling temperature; keep low for deterministic classification.   |
| `skill_classifier.max_tokens`  | Max tokens  | number | `64`    | 8 – 2048       | Output token cap for the classifier call.                          |
| `skill_classifier.retry`       | Retry count | number | `2`     | 0 – 10         | Retries on transient classifier LLM errors.                        |

### Group `summarizer` — Trace Summarizer

LLM that compresses long reasoning traces back into the conversation
history.

**Settings → Config card:** **Trace Summarizer** (the fourth card on
the page).

| Key                       | UI label    | Type   | Default | Range          | Meaning                                                  |
|---------------------------|-------------|--------|---------|----------------|----------------------------------------------------------|
| `summarizer.temperature`  | Temperature | number | `0.3`   | 0 – 2 (±0.1)   | Sampling temperature for the summarization call.         |
| `summarizer.max_tokens`   | Max tokens  | number | `1024`  | 128 – 8192     | Output token cap for the summary.                        |
| `summarizer.retry`        | Retry count | number | `2`     | 0 – 10         | Retries on transient summarizer LLM errors.              |

## Worked examples

### Inspect everything

```bash
$ cremind config get
```

### Raise the ReAct step ceiling for long tasks

```bash
$ cremind config set agent.max_steps 80
$ cremind config get agent.max_steps
80
```

### Undo the override

```bash
$ cremind config reset agent.max_steps
$ cremind config get agent.max_steps
40
```

### Compact sooner for a short-context model

```bash
$ cremind config set compaction.compact_threshold_tokens 40000
$ cremind config set compaction.keep_recent_tokens 15000
```

### Pipe the schema into `jq`

```bash
$ cremind config schema --json | jq '.groups.agent.fields | keys'
[
  "max_llm_retries",
  "max_steps",
  "reasoning_max_tokens",
  "reasoning_retry",
  "reasoning_temperature",
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

**Override "doesn't seem to apply"** — Overrides are stored per profile.
An override set under one profile is invisible to another; if you have
switched profiles, you are reading a different set of overrides.
