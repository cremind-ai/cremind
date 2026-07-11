---
description: "List and configure the **tools** the agent can call (built-in, MCP, A2A, skill, intrinsic): inspect a tool's config, enable or disable A2A/MCP tools, set tool variables (env-style key/value) or arguments (JSON), toggle a tool's sub-tools (\"leaves\"), and register a skill's long-running app as an autostart process. Use this to turn tools on or off and configure them — distinct from `cremind agents` (registering MCP/A2A servers)."
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
  `set-args` (a structured JSON arguments object).
- **Sub-tools ("leaves")** — `leaves` to list a grouped tool's
  individual sub-tools and their enabled state, `set-leaf` to turn
  specific ones on or off.
- **Skill-specific** — `register-long-running` to spin up a skill's
  long-running daemon and persist it as an autostart entry.

Most subcommands act on the **active profile**, which is resolved
server-side from your `CREMIND_TOKEN`. Built-in and intrinsic tools cannot
be deleted, but their variable overrides are profile-scoped, so
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
**Variables** and **Arguments** that map directly to
`cremind tools set-var` and `set-args`. Skill rows additionally
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
formatted JSON config blob.

**Syntax.**

```bash
cremind tools get <tool_id>
```

**Arguments** (required):

- `<tool_id>` — The id from `cremind tools list`.

**Behavior.** Prints a key-value header followed by a `--- config ---`
section containing the indented JSON config object.

**Header rows:**

| Row           | Meaning                                                       |
|---------------|---------------------------------------------------------------|
| `tool_id`     | The id you passed in.                                         |
| `name`        | Display name.                                                 |
| `tool_type`   | `built-in` / `mcp` / `a2a` / `skill` / `intrinsic`.            |
| `description` | Long-form description (may be multi-line).                    |
| `configured`  | `yes`/`no` — whether overrides exist for the active profile.  |

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
  "arguments": {},
  "variables": {},
  "meta": {"description": "Read and update Linear issues."}
}
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

### `cremind tools leaves`

**Purpose.** List a tool's sub-tools ("leaves") with their per-profile
enabled state. Grouped tools — built-in groups and connected MCP servers —
expose several callable sub-tools under one `tool_id`; this shows them
individually so you can toggle just the ones you want.

**Syntax.**

```bash
cremind tools leaves <tool_id>
```

**Behavior.** Renders a `LEAF / NAME / ENABLED / DESCRIPTION` table. If the
tool is a disconnected MCP server, a `(tool is disconnected — live sub-tool
list unavailable)` note is printed to stderr and the list may be empty. With
`--json`, returns `{supports_leaf_toggle, disconnected, leaves: [{leaf_name,
name, description, enabled}]}`.

**Example.**

```bash
$ cremind tools leaves mcp.linear
LEAF              NAME            ENABLED  DESCRIPTION
list_issues       List issues     yes      List issues in a team.
create_issue      Create issue    no       Create a new issue.
```

### `cremind tools set-leaf`

**Purpose.** Enable or disable specific sub-tools of a grouped tool. A single
pair is a per-leaf toggle; pass several to drive an "enable all" / "disable
all".

**Syntax.**

```bash
cremind tools set-leaf <tool_id> NAME=true|false [NAME=true|false...]
```

**Arguments** (at least one pair required):

- `<tool_id>` — Target tool.
- `NAME=BOOL` — Repeatable. `NAME` is the `leaf_name` from
  `cremind tools leaves`; the value accepts `true/false`, `1/0`, `yes/no`,
  `on/off`.

**Behavior.** Unknown leaf names are rejected when the tool exposes a live
sub-tool list; when that list is empty (e.g. a disconnected MCP server) the
write is accepted so the choice survives a reconnect. Silent on success.

**Example.**

```bash
# Keep only the read-only Linear sub-tools
$ cremind tools set-leaf mcp.linear list_issues=true create_issue=false update_issue=false
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

### Configure a skill's environment

```bash
$ cremind tools set-var skill.review-pr GITHUB_TOKEN=ghp_...
$ cremind tools get skill.review-pr
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
