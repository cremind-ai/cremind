---
description: "The Claude Code built-in tool and its permission mode: what the four Claude Code permission modes (bypassPermissions, acceptEdits, default, plan) each allow and how to change one, plus its other Tool Variables (model, max turns, max budget USD, Anthropic API key, CLI path, allowed/disallowed tools, max concurrent tasks). Also covers that Claude Code is disabled by default and how to enable it (the claude_code feature / Claude Agent SDK). Distinct from the general `cremind tools` CLI reference."
---

# Claude Code Tool

**Claude Code** is a built-in tool that delegates software-engineering work to
Anthropic's autonomous coding agent through the Claude Agent SDK. When it is
enabled, Cremind hands coding-expertise tasks (creating projects, writing,
refactoring, debugging, reviewing, and explaining code, running and fixing
tests) to Claude Code instead of editing files with its own shell/file tools.

Its `tool_id` is `claude_code`. Coding sessions run as background tasks; the
model starts one with `run`, polls with `wait`, aborts with `stop`, and checks
setup with `status`. Only Claude Code's final result and stats are returned to
the agent — its intermediate reasoning streams only to the user's Agent
Activity panel.

## Enabling the tool

Claude Code is **disabled by default**. Enabling it requires the `claude_code`
feature — the Claude Agent SDK, whose wheel bundles the Claude Code CLI binary.

```bash
# 1. Install the backing feature (Python extras: claude-code)
cremind features install claude_code

# 2. Enable the tool for the active profile
cremind tools enable claude_code
```

If you try to enable it before installing the feature, the server rejects the
call with HTTP 409 `FeatureNotInstalled`. You can also toggle it from
**Settings → Tools & Skills → Claude Code**.

For the tool to actually run coding tasks it needs an Anthropic credential —
either the `CLAUDE_CODE_API_KEY` variable below, the profile's Anthropic LLM
credentials (Settings → LLM), the server environment, or a host-level
`claude login`. Run the `status` sub-tool (with `probe=true`) to confirm.

## Permission modes

The permission mode controls how autonomously Claude Code acts. It is the
`CLAUDE_CODE_PERMISSION_MODE` Tool Variable, and it accepts exactly four
values:

| Mode                | What it allows |
|---------------------|----------------|
| `bypassPermissions` | **The default.** Runs fully autonomously with no approval prompts — the same trust level as the Shell Executor tool. Recommended for headless/server use where no human is present to approve steps. |
| `acceptEdits`       | Auto-approves file edits only. Other actions (e.g. shell commands) may be denied because no human is present to approve them. |
| `default`           | Interactive-style prompting: actions that need approval pause for a human. In a headless Cremind server there is no one to approve, so such actions fail. |
| `plan`              | Plan-only. Claude Code can read, search, and reason, but does not edit files or run mutating commands. Use it to get a recommendation without changes. |

### Changing the permission mode

Three equivalent ways, all profile-scoped:

- **UI** — Settings → Tools & Skills → Claude Code → set *Permission mode*.
- **CLI** — `cremind tools set-var claude_code CLAUDE_CODE_PERMISSION_MODE=plan`
  (the server rejects any value outside the four above with HTTP 400).
- **Agent** — the assistant can run that same `cremind tools set-var` command
  through its Shell Executor tool (the shell already has `CREMIND_SERVER` and
  `CREMIND_TOKEN` set, so no flags are needed).

A change takes effect on the **next** Claude Code task for that profile — no
server restart is needed (the value is re-read per task).

## All Tool Variables

Every variable is optional; the table gives its exact name and default.

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `CLAUDE_CODE_MODEL` | string | `""` | Claude model override for coding tasks (e.g. `claude-sonnet-4-5`). Empty = Claude Code's default model. |
| `CLAUDE_CODE_PERMISSION_MODE` | enum | `bypassPermissions` | See the four permission modes above. |
| `CLAUDE_CODE_MAX_TURNS` | number | `0` | Maximum agent turns per task. `0` = unlimited. |
| `CLAUDE_CODE_MAX_BUDGET_USD` | number | `0` | Maximum API spend (USD) per task. `0` = unlimited. |
| `CLAUDE_CODE_API_KEY` | string (secret) | `""` | Anthropic API key for Claude Code. Empty = fall back to the profile's Anthropic LLM credentials, then the server environment or `claude login`. |
| `CLAUDE_CODE_CLI_PATH` | string | `""` | Absolute path to an external Claude Code CLI binary. Empty = the SDK's bundled CLI. |
| `CLAUDE_CODE_ALLOWED_TOOLS` | string | `""` | Comma-separated allowlist of Claude Code tools (e.g. `Read,Edit,Bash`). Empty = all standard tools. |
| `CLAUDE_CODE_DISALLOWED_TOOLS` | string | `""` | Comma-separated denylist of Claude Code tools. Empty = none denied. |
| `CLAUDE_CODE_MAX_CONCURRENT_TASKS` | number | `2` | Maximum Claude Code tasks running at once across all conversations. |

`CLAUDE_CODE_API_KEY` is a secret: its value is masked everywhere it is read
back (shown as set/not set, never the value).

To view the live schema and the current per-profile values:

```bash
cremind tools get claude_code --json   # includes the schema with the mode enum
```

See `cremind tools` for the full tool-configuration CLI reference.
