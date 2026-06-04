---
description: "Complete reference for the `cremind tools` CLI command (alias `cremind tool`) — the terminal-side counterpart to the **Tools** page in the Cremind web UI — covering how to list every tool the agent can call (built-in, mcp, a2a, skill, intrinsic), inspect a tool's current configuration, enable or disable A2A/MCP tools, set tool variables (env-style key/value pairs), set tool arguments from a JSON object, override the LLM the tool uses, reset those overrides, and register a skill's `long_running_app` as an autostart process. Lists the five tool types, the locked-LLM-fields concept, and the rules for partial vs full updates."
---

# `cremind tools` — Tool & Skill Configuration

`cremind tools` (alias `cremind tool`) is the CLI for inspecting and configuring
the tools the Cremind agent can call. A tool here is anything the agent
can invoke during a turn: built-in functions baked into the server,
intrinsic agent operations, A2A peer agents, MCP servers, and locally
registered skills.

The group's surface area is small but covers every angle of tool config:

- **Inspection** — `list`, `get`.
- **Lifecycle for A2A / MCP tools** — `enable`, `disable`.
- **Per-tool configuration** — `set-var` (env-style variables),
  `set-args` (a structured JSON arguments object), `set-llm`
  (per-tool LLM overrides), `reset-llm` (drop those overrides).
- **Skill-specific** — `register-long-running` to spin up a skill's
  long-running daemon and persist it as an autostart entry.

Most subcommands act on the **active profile**, which is resolved
server-side from your `CREMIND_TOKEN`. Built-in and intrinsic tools cannot
be deleted, but their LLM and variable overrides are profile-scoped, so
each profile can carry its own configuration.

## Tool types

| Type        | Where it comes from                                                       | Can `enable/disable`? |
|-------------|---------------------------------------------------------------------------|-----------------------|
| `built-in`  | Compiled into the server (filesystem, shell, etc.).                       | No                    |
| `intrinsic` | Agent-control verbs the loop emits (e.g. `final_answer`, `think`).        | No                    |
| `mcp`       | MCP server registered via `cremind agents add --type mcp`.                    | Yes                   |
| `a2a`       | Peer A2A agent registered via `cremind agents add --type a2a`.                | Yes                   |
| `skill`     | Local skill discovered from a SKILL.md directory.                         | Yes                   |

Use `--type` on `cremind tools list` to filter by these labels.

## Finding this in the web UI

Every operation in this group has a control on the **Tools** page of
the Cremind web UI:

> **Sidebar → Tools**

The page shows one row per tool with type, enabled toggle, and a "..."
menu opening a configuration drawer. The drawer has tabs for
**Variables**, **Arguments**, and **LLM Overrides** that map directly to
`cremind tools set-var`, `set-args`, and `set-llm`. Skill rows additionally
expose a **Register long-running app** action that maps to
`cremind tools register-long-running`.

## Global flags

All `cremind tools` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind tools list`

**Purpose.** Print every tool registered for the active profile, with
type and enabled/configured flags.

**Syntax.**

```bash
cremind tools list [--type <type>]
```

**Flags.**

| Flag      | Type   | Default | Meaning                                                       |
|-----------|--------|---------|---------------------------------------------------------------|
| `--type`  | string | `""`    | Filter by tool type: `built-in`, `mcp`, `a2a`, `skill`, `intrinsic`. |

**Behavior.** Renders a five-column table:

| Column       | Source       | Meaning                                                                |
|--------------|--------------|------------------------------------------------------------------------|
| `TOOL_ID`    | `tool_id`    | Stable identifier (e.g. `built_in.filesystem`, `mcp.linear`).          |
| `TYPE`       | `tool_type`  | One of the five types above.                                           |
| `ENABLED`    | `enabled`    | `yes`/`no`. Defaults to `yes` for built-in/intrinsic types.            |
| `CONFIGURED` | `configured` | `yes` if any per-profile config has been written for this tool.        |
| `NAME`       | `name`       | Display name.                                                          |

With `--json`, returns the underlying array unchanged.

**Examples.**

```bash
# Everything
$ cremind tools list

# Only the locally-registered skills
$ cremind tools list --type skill
TOOL_ID            TYPE   ENABLED  CONFIGURED  NAME
skill.review-pr    skill  yes      yes         Review PR
skill.daily-brief  skill  yes      no          Daily Brief
```

### `cremind tools get`

**Purpose.** Show one tool's full configuration, including the
formatted JSON config blob and any locked LLM fields.

**Syntax.**

```bash
cremind tools get <tool_id>
```

**Arguments** (required):

- `<tool_id>` — The id from `cremind tools list`.

**Behavior.** Prints a key-value header followed by a `--- config ---`
section containing the indented JSON config object, then a
`locked_llm_fields:` line if any LLM fields are locked.

**Header rows:**

| Row           | Meaning                                                       |
|---------------|---------------------------------------------------------------|
| `tool_id`     | The id you passed in.                                         |
| `name`        | Display name.                                                 |
| `tool_type`   | `built-in` / `mcp` / `a2a` / `skill` / `intrinsic`.            |
| `description` | Long-form description (may be multi-line).                    |
| `configured`  | `yes`/`no` — whether overrides exist for the active profile.  |

`locked_llm_fields` lists keys (e.g. `llm_provider`, `reasoning_effort`)
that the tool definition disallows overriding. `set-llm` calls touching
those fields will fail server-side.

**Example.**

```bash
$ cremind tools get mcp.linear
tool_id      mcp.linear
name         Linear
tool_type    mcp
description  Read and update Linear issues.
configured   yes

--- config ---
{
  "llm_provider": "anthropic",
  "llm_model": "claude-sonnet-4-6",
  "system_prompt": "..."
}

locked_llm_fields: llm_provider
```

### `cremind tools enable` / `cremind tools disable`

**Purpose.** Enable or disable an A2A or MCP tool for the active
profile. Built-in / intrinsic tools cannot be disabled this way.

**Syntax.**

```bash
cremind tools enable <tool_id>
cremind tools disable <tool_id>
```

**Behavior.** Silent on success. The change is profile-scoped: another
profile's enabled state is unaffected.

**Example.**

```bash
$ cremind tools disable mcp.linear
$ cremind tools list --type mcp
TOOL_ID     TYPE  ENABLED  CONFIGURED  NAME
mcp.linear  mcp   no       yes         Linear
```

### `cremind tools set-var`

**Purpose.** Set environment-style variables for a tool — useful for
MCP servers and skills whose runners read configuration from
environment variables.

**Syntax.**

```bash
cremind tools set-var <tool_id> KEY=VALUE [KEY=VALUE...]
```

**Arguments** (at least one required):

- `<tool_id>` — Target tool.
- `KEY=VALUE` — Repeatable. Splits on the first `=`; subsequent `=`
  characters are part of the value.

**Behavior.** Writes the variables to the server in one call; any
existing variables not mentioned are left untouched (this is a *patch*,
not a *replace*). Silent on success.

**Examples.**

```bash
$ cremind tools set-var skill.daily-brief INBOX=/var/mail/li REPORT_TIME=09:00
$ cremind tools set-var mcp.linear LINEAR_API_KEY=lin_api_...
```

### `cremind tools set-args`

**Purpose.** Replace the tool's structured arguments object — for tools
whose configuration is best expressed as JSON rather than flat
variables.

**Syntax.**

```bash
cremind tools set-args <tool_id> --json '<JSON object>'
```

**Arguments** (required):

- `<tool_id>` — Target tool.

**Flags.**

| Flag     | Type   | Default | Meaning                                                        |
|----------|--------|---------|----------------------------------------------------------------|
| `--json` | string | `""`    | Tool arguments as a JSON object. **Required.**                 |

**Behavior.** The JSON object replaces the previous arguments
wholesale. Silent on success.

**Example.**

```bash
$ cremind tools set-args mcp.shell --json '{"shells":["bash","pwsh"],"timeout_s":120}'
```

### `cremind tools set-llm`

**Purpose.** Override the LLM parameters a tool uses — provider,
model, reasoning effort, and the full-reasoning toggle. Useful for
making one tool always use a heavier model than the global default, or
for forcing a specific provider.

**Syntax.**

```bash
cremind tools set-llm <tool_id> [--provider P] [--model M] [--reasoning-effort low|medium|high] [--full-reasoning true|false]
```

**Arguments** (required):

- `<tool_id>` — Target tool.

**Flags** (at least one required):

| Flag                 | Type   | Default | Meaning                                                  |
|----------------------|--------|---------|----------------------------------------------------------|
| `--provider`         | string | `""`    | LLM provider id (e.g. `anthropic`, `openai`).            |
| `--model`            | string | `""`    | Model id (e.g. `claude-sonnet-4-6`).                     |
| `--reasoning-effort` | string | `""`    | `low` / `medium` / `high`.                               |
| `--full-reasoning`   | string | `""`    | `true` / `false`. Any other value is rejected client-side.|

**Behavior.** Each supplied flag updates exactly its corresponding
field; omitted flags are unchanged. Fields listed in the tool's
`locked_llm_fields` cannot be set and the server rejects such updates.
Silent on success.

**Examples.**

```bash
$ cremind tools set-llm skill.review-pr --provider anthropic --model claude-opus-4-7 --reasoning-effort high
$ cremind tools set-llm mcp.shell --full-reasoning false
```

### `cremind tools reset-llm`

**Purpose.** Remove specific LLM-parameter overrides so the tool falls
back to the code-defined default.

**Syntax.**

```bash
cremind tools reset-llm <tool_id> <key> [key...]
```

**Arguments** (at least two required):

- `<tool_id>` — Target tool.
- `<key>` — One or more LLM-parameter field names
  (`llm_provider`, `llm_model`, `reasoning_effort`, `full_reasoning`).

**Behavior.** Silent on success.

**Example.**

```bash
$ cremind tools reset-llm skill.review-pr llm_provider llm_model reasoning_effort
```

### `cremind tools register-long-running`

**Purpose.** Spawn a skill's `long_running_app` (declared in its
`SKILL.md`) and persist it as an autostart entry so it relaunches at
boot.

**Syntax.**

```bash
cremind tools register-long-running <tool_id> [--force]
```

**Arguments** (required):

- `<tool_id>` — Skill tool id (must be of type `skill`).

**Flags.**

| Flag       | Type | Default | Meaning                                                                |
|------------|------|---------|------------------------------------------------------------------------|
| `--force`  | bool | `false` | Bypass the duplicate-command check and register a second autostart.    |

**Behavior.** Spawns the skill's `long_running_app` immediately and
writes a row to the autostart table. Returns a key-value table:

| Row             | Meaning                                                          |
|-----------------|------------------------------------------------------------------|
| `process_id`    | The id of the live process (use with `cremind proc attach`).         |
| `autostart_id`  | The id of the autostart registration (use with `cremind proc autostart delete` if you want to undo). |
| `command`       | The exact command line that was launched.                        |
| `working_dir`   | Working directory for the process.                               |

With `--json`, returns the raw response.

**Example.**

```bash
$ cremind tools register-long-running skill.daily-brief
process_id    p_e21f
autostart_id  a_8c14
command       /usr/bin/python /skills/daily-brief/run.py
working_dir   /home/li/work
```

## Worked examples

### Disable an MCP tool for the active profile only

```bash
$ cremind tools disable mcp.linear
```

### Configure a skill's environment and force it to use Opus

```bash
$ cremind tools set-var skill.review-pr GITHUB_TOKEN=ghp_...
$ cremind tools set-llm skill.review-pr --provider anthropic --model claude-opus-4-7 --reasoning-effort high
$ cremind tools get skill.review-pr
```

### Drop all per-tool LLM overrides in one shot

```bash
$ cremind tools reset-llm skill.review-pr llm_provider llm_model reasoning_effort full_reasoning
```

### Find the tool ids of every disabled MCP server

```bash
$ cremind tools list --type mcp --json | jq -r '.[] | select(.enabled==false) | .tool_id'
```

### Spin up a skill's daemon and immediately attach to its console

```bash
$ pid=$(cremind tools register-long-running skill.daily-brief --json | jq -r .process_id)
$ cremind proc attach "$pid"
```

## Troubleshooting

**`enable` / `disable` rejected** — Built-in and intrinsic tools cannot
be enabled or disabled — the server rejects the call. Use `cremind tools list`
to confirm the type.

**`set-llm` rejected for a locked field** — Some tool definitions lock
specific LLM fields (visible in `cremind tools get` under
`locked_llm_fields`). The locked field cannot be changed via
`set-llm`; pick a different model or relax the lock at the tool
definition level.

**`set-args` requires `--json`** — Even for an empty object, you must
pass `--json '{}'`. The empty default is a deliberate forcing function.

**Variables seem stale** — `set-var` is a patch, not a replace; old keys
hang around until you set them to an empty string or restart from
scratch via the Tools UI. Use `cremind tools get` to inspect the live
state.

**`register-long-running` says "duplicate command"** — The tool already
has an autostart with the same command line. Pass `--force` if you
genuinely want a second copy, otherwise inspect existing autostarts
with `cremind proc autostart list`.
