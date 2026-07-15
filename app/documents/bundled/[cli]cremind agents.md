---
description: "Register and manage **MCP servers** the agent can call: `add` a server by `--url` (HTTP/SSE) or `--json-config` (VS Code-style stdio config), remove it, enable or disable it per profile, `reconnect` a stub connection, run the **OAuth** authorize flow (`auth-url`) or `unlink` a stored token, and read/set its per-profile `description` (`config get`/`config set`). Use this to connect a new MCP tool server — distinct from `cremind tools` (configuring already-registered tools) and `cremind llm` (LLM providers)."
---

# `cremind agents` — MCP Server Registration

`cremind agents` (alias `cremind agent`) is the CLI for managing the external
MCP servers the Cremind agent can delegate to. Once registered, an MCP server
shows up as a tool in `cremind tools list` (with a `tool_type` of `mcp`), which
is why some flags overlap between the two commands. The dividing line:
`cremind agents` handles **registration and OAuth**, while `cremind tools`
handles **per-tool variables and arguments**.

> **Note.** Earlier versions of this command also registered A2A peer agents
> and accepted per-agent LLM overrides (`--llm-provider`, `--llm-model`,
> `--reasoning-effort`) and a `--system-prompt`. A2A support and those
> overrides have been removed — MCP dispatch uses the reasoning model's native
> function calling, so a per-server LLM never actually ran. Only the
> `description` remains as an editable per-server field.

The group covers:

- **Lifecycle** — `list`, `add`, `delete`.
- **Per-profile activation** — `enable`, `disable`.
- **Connection control** — `reconnect` for retrying stub servers that
  failed to connect at startup.
- **OAuth** — `auth-url` to mint the authorize URL, `unlink` to drop
  the stored OAuth token for the active profile.
- **Per-profile config** — `config get`, `config set` for the per-server
  `description`.

## Two ways to register an MCP server

`cremind agents add` supports two registration styles, picked by the choice
between `--url` and `--json-config`:

| Style                        | Command                            | When to use                                                                                   |
|------------------------------|------------------------------------|-----------------------------------------------------------------------------------------------|
| **HTTP / SSE**               | `--url <url>`                      | The MCP server is reachable over HTTP/SSE. Provide its URL.                                    |
| **stdio (VS Code-style JSON)** | `--json-config '{<json>}'`       | The MCP server is a stdio subprocess, or you have an existing VS Code `mcp.json` snippet to reuse. |

The JSON-config form mirrors the schema VS Code's MCP support uses, so
you can paste in a `command` / `args` / `env` block for stdio servers.

## Finding this in the web UI

Every operation in this group has a control on the **Tools & Skills** page of
the Cremind web UI:

> **Sidebar → Settings → Tools & Skills**

Registered MCP servers are listed under the page's **"MCP Server Remote"**
section (alongside the "Built-in Tools" and "Skills" sections). The
**+ Add MCP Server** button opens a dialog with a **URL** / **JSON Config**
input-mode toggle — these match `--url` and `--json-config` respectively — plus
a **Description** field.

Each server renders as a card. Its header shows, as applicable, **Auth**
(matching `auth-url`), **Unlink** (`unlink`), **Reconnect** (`reconnect`), and
**Remove** (`delete`) buttons plus an enable/disable switch (`enable` /
`disable`). Expanding a card reveals an inline **Description** field with
**Save** / **Reset to Default** (matching `config get` / `config set`) and a
sub-tools enable/disable list. There is no separate "Agents" page and no
OAuth/Config tabs.

## Global flags

All `cremind agents` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind agents list`

**Purpose.** Show every registered MCP server with its type, profile-level
enabled flag, status, and URL.

**Syntax.**

```bash
cremind agents list
```

**Behavior.** Renders a five-column table:

| Column     | Source        | Meaning                                                          |
|------------|---------------|------------------------------------------------------------------|
| `TOOL_ID`  | `tool_id`     | Stable id (e.g. `mcp.linear`, `mcp.shell`).                      |
| `TYPE`     | `agent_type`  | `mcp`.                                                            |
| `ENABLED`  | `enabled`     | `yes`/`no` for the active profile.                               |
| `STATUS`   | `status_text` | Server's status string (e.g. `connected`, `auth required`).      |
| `URL`      | `url`         | The server's URL when applicable (blank for stdio servers).      |

With `--json`, the underlying array is returned.

**Example.**

```bash
$ cremind agents list
TOOL_ID         TYPE  ENABLED  STATUS         URL
mcp.linear      mcp   yes      auth required  https://mcp.linear.app
mcp.shell       mcp   yes      connected
```

### `cremind agents add`

**Purpose.** Register a new MCP server.

**Syntax.**

```bash
cremind agents add --url <url> [--description <text>]
cremind agents add --json-config '<json>' [--description <text>]
```

**Required flags.**

- Exactly one of `--url <url>` or `--json-config '<json>'`.

**Optional flags.**

| Flag            | Meaning                                                              |
|-----------------|----------------------------------------------------------------------|
| `--description` | Human-friendly description shown in the UI and surfaced to the agent. |

**Behavior.** On success, prints a key-value table with `tool_id`,
`name`, `agent_type`, `url`, and `status_text`. With `--json`, returns
the full server record (including any auth metadata).

**Examples.**

```bash
# HTTP MCP server with a description
$ cremind agents add --url https://mcp.linear.app \
    --description "Linear issue tracker"

# stdio MCP server via VS Code-style JSON
$ cremind agents add --json-config '{
    "command": "/usr/bin/mcp-shell",
    "args": ["--root", "/srv"],
    "env": {"SHELL_TIMEOUT": "120"}
  }'
```

### `cremind agents delete`

**Purpose.** Unregister an MCP server. The matching tool disappears from
`cremind tools list`.

**Syntax.**

```bash
cremind agents delete <tool_id>
```

**Behavior.** Silent on success. Cascades: any per-profile config,
OAuth tokens, and live connections for the server are dropped.

**Example.**

```bash
$ cremind agents delete mcp.experimental
```

### `cremind agents enable` / `cremind agents disable`

**Purpose.** Per-profile enable / disable toggle. Equivalent to the
switch on the server's card in the web UI.

**Syntax.**

```bash
cremind agents enable <tool_id>
cremind agents disable <tool_id>
```

**Behavior.** Silent on success. Profile-scoped — disabling a server
under one profile does not affect another. Note that this is the same
state read by `cremind tools list`'s `ENABLED` column.

**Example.**

```bash
$ cremind agents disable mcp.linear
```

### `cremind agents reconnect`

**Purpose.** Retry the connection for a stub server (one that failed at
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

**Purpose.** Print the OAuth authorize URL for a server. The user
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

**Purpose.** Drop the active profile's OAuth token for a server,
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

**Purpose.** Read the per-profile config for a server — the same
`description` shown on the server's card in the UI.

**Syntax.**

```bash
cremind agents config get <tool_id>
```

**Behavior.** Pretty-prints the JSON config object (its `url` and
`description`). With `--json`, the same JSON is emitted unindented.

**Example.**

```bash
$ cremind agents config get mcp.linear
{
  "url": "https://mcp.linear.app",
  "description": "Linear issue tracker"
}
```

### `cremind agents config set`

**Purpose.** Patch a server's `description`. Only the supplied flag
changes; everything else is left untouched.

**Syntax.**

```bash
cremind agents config set <tool_id> --description <text>
```

**Flags** (at least one required):

| Flag            | Type   | Default | Meaning                     |
|-----------------|--------|---------|-----------------------------|
| `--description` | string | `""`    | Replace the description.    |

**Behavior.** Silent on success.

**Note.** This sets the MCP server's `description`. To configure a
built-in or intrinsic tool's variables, use `cremind tools set-var` instead.

**Example.**

```bash
$ cremind agents config set mcp.linear --description "Linear issue tracker (read-only)"
```

## Worked examples

### Register an MCP server, OAuth-link it, and verify

```bash
$ cremind agents add --url https://mcp.linear.app --description "Linear"
$ xdg-open "$(cremind agents auth-url mcp.linear)"
# ... finish browser flow ...
$ cremind agents list | grep mcp.linear
mcp.linear  mcp  yes  connected  https://mcp.linear.app
```

### Add a stdio MCP server from VS Code-style JSON

```bash
$ cremind agents add --json-config "$(cat <<'EOF'
{
  "command": "/usr/bin/mcp-shell",
  "args": ["--root", "/srv"],
  "env": { "SHELL_TIMEOUT": "120" }
}
EOF
)"
```

### Re-authorize a server after rotating its credentials

```bash
$ cremind agents unlink mcp.linear
$ xdg-open "$(cremind agents auth-url mcp.linear)"
```

### Disable every MCP server for the active profile in one shot

```bash
$ for id in $(cremind agents list --json | jq -r '.[].tool_id'); do
    cremind agents disable "$id"
  done
```

## Troubleshooting

**`either --url or --json-config is required`** — `add` rejects an
empty registration. Provide one of the two.

**`STATUS = auth required`** — The server needs OAuth. Run
`cremind agents auth-url <tool_id>`, open the URL, complete the flow, then
re-check with `cremind agents list`. If the status persists, check the
server logs — the OAuth callback may have failed.

**`STATUS = connection error` after a network change** — Run
`cremind agents reconnect <tool_id>`; if it still fails, the server
configuration itself is bad — re-add it with the correct URL.

**`config set` rejected** — At least one config flag must be supplied.
The CLI does not allow an empty patch.

**`unlink` doesn't seem to do anything** — `unlink` is profile-scoped.
If you are seeing the same server's token under another profile, switch
tokens (`CREMIND_TOKEN`) and unlink there as well.

**Server appears in `cremind tools list` but not `cremind agents list`** — Only
`mcp` tools are surfaced under `cremind agents`. Built-in, intrinsic, and
skill tools are managed by `cremind tools` instead.
