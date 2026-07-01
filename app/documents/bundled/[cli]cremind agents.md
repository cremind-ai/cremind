---
description: "Register and manage external **A2A agents and MCP servers** the agent can call: `add` a server by `--url` or `--json-config`, remove it, enable or disable it per profile, retry a stub connection, run the **OAuth** authorize flow (`auth-url`) or `unlink` a stored token, and set a per-agent LLM override. Use this to connect a new MCP or A2A tool server — distinct from `cremind tools` (configuring already-registered tools) and `cremind llm` (LLM providers)."
---

# `cremind agents` — A2A and MCP Server Registration

`cremind agents` (alias `cremind agent`) is the CLI for managing the external
agents the Cremind agent can delegate to: A2A peer agents and MCP
servers. Once registered, an agent shows up as a tool in
`cremind tools list` (with `tool_type` of `a2a` or `mcp`), which is why
some flags overlap between the two commands. The dividing line:
`cremind agents` handles **registration and OAuth**, while `cremind tools`
handles **per-tool variables and arguments**.

The group covers:

- **Lifecycle** — `list`, `add`, `delete`.
- **Per-profile activation** — `enable`, `disable`.
- **Connection control** — `reconnect` for retrying stub agents that
  failed to connect at startup.
- **OAuth** — `auth-url` to mint the authorize URL, `unlink` to drop
  the stored OAuth token for the active profile.
- **Per-profile config** — `config get`, `config set` for the LLM and
  meta fields specific to MCP and A2A wrappers (system prompt,
  description, LLM provider/model/reasoning-effort, full-reasoning).

## Two ways to register an agent

`cremind agents add` supports two distinct registration styles, picked via
the `--type` flag and the choice between `--url` and `--json-config`:

| Style                              | Command                                                          | When to use                                                                                |
|------------------------------------|------------------------------------------------------------------|--------------------------------------------------------------------------------------------|
| **A2A (URL)**                      | `--type a2a --url <url>`                                         | The agent speaks the A2A protocol over HTTP. Provide its public URL.                       |
| **MCP (HTTP/SSE)**                 | `--type mcp --url <url>`                                         | The agent is an MCP server reachable over HTTP/SSE.                                        |
| **MCP (VS Code-style JSON)**       | `--type mcp --json-config '{<json>}'`                            | The agent is a stdio MCP server, or you have an existing VS Code `mcp.json` snippet to reuse. |

The JSON-config form mirrors the schema VS Code's MCP support uses, so
you can paste in a `command` / `args` / `env` block for stdio servers.

## Finding this in the web UI

Every operation in this group has a control on the **Agents** page of
the Cremind web UI:

> **Sidebar → Agents**

The page lists each registered agent with its type, status, URL, and
auth state. The **+ Add agent** button opens a dialog with a type
toggle (A2A / MCP) and either a URL field or a JSON editor — these
match `--url` and `--json-config` respectively. Selecting an agent row
opens a drawer with **OAuth** (matching `auth-url` / `unlink`) and
**Config** (matching `config get` / `config set`) tabs.

## Global flags

All `cremind agents` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind agents list`

**Purpose.** Show every registered A2A and MCP agent with its
type, profile-level enabled flag, status, and URL.

**Syntax.**

```bash
cremind agents list
```

**Behavior.** Renders a five-column table:

| Column     | Source        | Meaning                                                          |
|------------|---------------|------------------------------------------------------------------|
| `TOOL_ID`  | `tool_id`     | Stable id (e.g. `a2a.team-bot`, `mcp.linear`).                   |
| `TYPE`     | `agent_type`  | `a2a` or `mcp`.                                                  |
| `ENABLED`  | `enabled`     | `yes`/`no` for the active profile.                               |
| `STATUS`   | `status_text` | Server's status string (e.g. `connected`, `auth required`).      |
| `URL`      | `url`         | The agent's URL when applicable (blank for stdio MCP servers).   |

With `--json`, the underlying array is returned.

**Example.**

```bash
$ cremind agents list
TOOL_ID         TYPE  ENABLED  STATUS         URL
a2a.team-bot    a2a   yes      connected      https://team-bot.internal/a2a
mcp.linear      mcp   yes      auth required  https://mcp.linear.app
mcp.shell       mcp   yes      connected
```

### `cremind agents add`

**Purpose.** Register a new A2A or MCP server.

**Syntax.**

```bash
cremind agents add --type a2a --url <url> [meta/llm flags]
cremind agents add --type mcp --url <url> [meta/llm flags]
cremind agents add --type mcp --json-config '<json>' [meta/llm flags]
```

**Required flags.**

- `--type a2a|mcp`.
- Exactly one of `--url <url>` or `--json-config '<json>'`.

**Optional flags** (all string, default `""`):

| Flag                  | Meaning                                                                                  |
|-----------------------|------------------------------------------------------------------------------------------|
| `--system-prompt`     | Prompt prepended to LLM calls made on this agent's behalf (MCP wrappers).                |
| `--description`       | Human-friendly description shown in the UI and surfaced to the agent.                    |
| `--llm-provider`      | LLM provider override for this agent.                                                    |
| `--llm-model`         | LLM model override for this agent.                                                       |
| `--reasoning-effort`  | `low` / `medium` / `high`.                                                               |

**Behavior.** On success, prints a key-value table with `tool_id`,
`name`, `agent_type`, `url`, and `status_text`. With `--json`, returns
the full agent record (including any auth metadata).

**Examples.**

```bash
# A2A peer
$ cremind agents add --type a2a --url https://team-bot.internal/a2a

# HTTP MCP server with a per-server prompt
$ cremind agents add --type mcp --url https://mcp.linear.app \
    --system-prompt "Always cite issue ids" \
    --description "Linear issue tracker"

# stdio MCP server via VS Code-style JSON
$ cremind agents add --type mcp --json-config '{
    "command": "/usr/bin/mcp-shell",
    "args": ["--root", "/srv"],
    "env": {"SHELL_TIMEOUT": "120"}
  }'
```

### `cremind agents delete`

**Purpose.** Unregister an A2A or MCP agent. The matching tool
disappears from `cremind tools list`.

**Syntax.**

```bash
cremind agents delete <tool_id>
```

**Behavior.** Silent on success. Cascades: any per-profile config,
OAuth tokens, and live connections for the agent are dropped.

**Example.**

```bash
$ cremind agents delete mcp.experimental
```

### `cremind agents enable` / `cremind agents disable`

**Purpose.** Per-profile enable / disable toggle. Equivalent to the
toggle in the agent row of the web UI.

**Syntax.**

```bash
cremind agents enable <tool_id>
cremind agents disable <tool_id>
```

**Behavior.** Silent on success. Profile-scoped — disabling an agent
under one profile does not affect another. Note that this is the same
state read by `cremind tools list`'s `ENABLED` column.

**Example.**

```bash
$ cremind agents disable mcp.linear
```

### `cremind agents reconnect`

**Purpose.** Retry the connection for a stub agent (one that failed at
startup or returned a transient error). Useful after rotating
credentials or fixing a network issue.

**Syntax.**

```bash
cremind agents reconnect <tool_id>
```

**Behavior.** Silent on success. The next `cremind agents list` reflects
the new status.

**Example.**

```bash
$ cremind agents reconnect mcp.linear
$ cremind agents list | grep mcp.linear
mcp.linear  mcp  yes  connected  https://mcp.linear.app
```

### `cremind agents auth-url`

**Purpose.** Print the OAuth authorize URL for an agent. The user
opens it in a browser to grant access; the resulting token is stored
server-side, scoped to the active profile.

**Syntax.**

```bash
cremind agents auth-url <tool_id> [--return-url <url>]
```

**Flags.**

| Flag           | Type   | Default | Meaning                                                                       |
|----------------|--------|---------|-------------------------------------------------------------------------------|
| `--return-url` | string | `""`    | URL the OAuth callback redirects to after success. Server uses a default if omitted. |

**Behavior.** Prints the URL on a single line (suitable for `xdg-open`
or `start`). With `--json`, wraps it as `{"auth_url": "..."}`.

**Examples.**

```bash
# Just print the URL
$ cremind agents auth-url mcp.linear
https://mcp.linear.app/oauth/authorize?...

# Open the URL automatically (Linux example)
$ xdg-open "$(cremind agents auth-url mcp.linear)"
```

### `cremind agents unlink`

**Purpose.** Drop the active profile's OAuth token for an agent,
forcing the next call to re-authorize.

**Syntax.**

```bash
cremind agents unlink <tool_id>
```

**Behavior.** Silent on success. Other profiles' tokens are
unaffected.

**Example.**

```bash
$ cremind agents unlink mcp.linear
$ cremind agents list | grep mcp.linear
mcp.linear  mcp  yes  auth required  https://mcp.linear.app
```

### `cremind agents config get`

**Purpose.** Read the per-profile LLM and meta config for an agent —
the same fields shown in the agent's **Config** tab in the UI.

**Syntax.**

```bash
cremind agents config get <tool_id>
```

**Behavior.** Pretty-prints the JSON config object. With `--json`, the
same JSON is emitted unindented.

**Example.**

```bash
$ cremind agents config get mcp.linear
{
  "llm_provider": "anthropic",
  "llm_model": "claude-sonnet-4-6",
  "reasoning_effort": "medium",
  "system_prompt": "Always cite issue ids",
  "description": "Linear issue tracker"
}
```

### `cremind agents config set`

**Purpose.** Patch one or more agent config fields. Only the supplied
flags change; everything else is left untouched.

**Syntax.**

```bash
cremind agents config set <tool_id> [--llm-provider P] [--llm-model M] [--reasoning-effort E] [--full-reasoning true|false] [--system-prompt S] [--description D]
```

**Flags** (at least one required):

| Flag                 | Type   | Default | Meaning                                                       |
|----------------------|--------|---------|---------------------------------------------------------------|
| `--llm-provider`     | string | `""`    | LLM provider id.                                              |
| `--llm-model`        | string | `""`    | Model id.                                                     |
| `--reasoning-effort` | string | `""`    | `low` / `medium` / `high`.                                    |
| `--full-reasoning`   | string | `""`    | `true` / `false`. Any other value is rejected client-side.    |
| `--system-prompt`    | string | `""`    | Replace the system prompt (use `""`-like inputs to clear).    |
| `--description`      | string | `""`    | Replace the description.                                      |

**Behavior.** Silent on success.

**Note.** This is the right command for MCP/A2A wrapper config
(`system_prompt`, `description`, plus the LLM fields). For built-in or
intrinsic tools, use `cremind tools set-llm` and `cremind tools set-var`.

**Examples.**

```bash
# Switch the per-server model and bump reasoning effort
$ cremind agents config set mcp.linear --llm-model claude-opus-4-7 --reasoning-effort high

# Clear the description (current implementation requires a non-empty value;
# to truly clear, use the JSON API directly)
$ cremind agents config set mcp.linear --description "Linear issue tracker (read-only)"
```

## Worked examples

### Register an MCP server, OAuth-link it, and verify

```bash
$ cremind agents add --type mcp --url https://mcp.linear.app \
    --description "Linear" --llm-model claude-sonnet-4-6
$ xdg-open "$(cremind agents auth-url mcp.linear)"
# ... finish browser flow ...
$ cremind agents list | grep mcp.linear
mcp.linear  mcp  yes  connected  https://mcp.linear.app
```

### Add a stdio MCP server from VS Code-style JSON

```bash
$ cremind agents add --type mcp --json-config "$(cat <<'EOF'
{
  "command": "/usr/bin/mcp-shell",
  "args": ["--root", "/srv"],
  "env": { "SHELL_TIMEOUT": "120" }
}
EOF
)"
```

### Re-authorize an agent after rotating its credentials

```bash
$ cremind agents unlink mcp.linear
$ xdg-open "$(cremind agents auth-url mcp.linear)"
```

### Use a heavier model for one specific agent only

```bash
$ cremind agents config set mcp.linear --llm-model claude-opus-4-7 --reasoning-effort high
$ cremind agents config get mcp.linear
```

### Disable every MCP agent for the active profile in one shot

```bash
$ for id in $(cremind agents list --json | jq -r '.[] | select(.agent_type=="mcp") | .tool_id'); do
    cremind agents disable "$id"
  done
```

## Troubleshooting

**`--type is required`** — `add` always needs the type explicitly.
Pick `a2a` or `mcp`.

**`either --url or --json-config is required`** — `add` rejects an
empty registration. Provide one of the two.

**`STATUS = auth required`** — The agent needs OAuth. Run
`cremind agents auth-url <tool_id>`, open the URL, complete the flow, then
re-check with `cremind agents list`. If the status persists, check the
server logs — the OAuth callback may have failed.

**`STATUS = connection error` after a network change** — Run
`cremind agents reconnect <tool_id>`; if it still fails, the agent
configuration itself is bad — re-add it with the correct URL.

**`config set` rejected** — At least one config flag must be supplied.
The CLI does not allow an empty patch.

**`unlink` doesn't seem to do anything** — `unlink` is profile-scoped.
If you are seeing the same agent's token under another profile, switch
tokens (`CREMIND_TOKEN`) and unlink there as well.

**Agent appears in `cremind tools list` but not `cremind agents list`** — Only
`a2a` and `mcp` types are surfaced under `cremind agents`. Built-in,
intrinsic, and skill tools are managed by `cremind tools` instead.
