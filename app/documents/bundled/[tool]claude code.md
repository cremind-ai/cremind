---
description: "The Claude Code built-in tool and its permission mode: the permission modes (bypassPermissions, acceptEdits, default, plan, dontAsk, auto) come live from the installed Claude Agent SDK — the same modes the Claude Code CLI cycles through with Shift+Tab — so what each allows and how to change one, listed with cremind tools options claude_code. Plus how to choose the model from the account's live model list (cremind tools options claude_code, or the status sub-tool's models field) and its other Tool Variables (model, permission mode, max turns, max budget USD, Anthropic API key, CLI path, allowed/disallowed tools, max concurrent tasks). Also covers whether Claude Code is logged in / which credential it uses (including a host `claude login`), that Claude Code is disabled by default, and how to enable it (the claude_code feature / Claude Agent SDK). Distinct from the general `cremind tools` CLI reference."
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
`CLAUDE_CODE_PERMISSION_MODE` Tool Variable. Its list of values is **dynamic** —
it comes live from the installed Claude Agent SDK (the same modes the Claude
Code CLI cycles through with Shift+Tab), so a newer SDK that adds modes exposes
them automatically. List the current set with `cremind tools options
claude_code`. The modes the SDK ships today:

| Mode                | What it allows |
|---------------------|----------------|
| `bypassPermissions` | **The default.** Runs fully autonomously with no approval prompts — the same trust level as the Shell Executor tool. Recommended for headless/server use where no human is present to approve steps. |
| `acceptEdits`       | Auto-approves file edits only. Other actions (e.g. shell commands) may be denied because no human is present to approve them. |
| `default`           | Interactive-style prompting: actions that need approval pause for a human. In a headless Cremind server there is no one to approve, so such actions fail. |
| `plan`              | Plan-only. Claude Code can read, search, and reason, but does not edit files or run mutating commands. Use it to get a recommendation without changes. |
| `dontAsk`           | Never prompts: anything not pre-approved (via the allowlist / settings) is **denied** rather than surfaced. The most restrictive non-interactive mode — the mirror image of `bypassPermissions`. |
| `auto`              | No routine prompts, but a background safety classifier reviews each action and blocks destructive ones (force push, production deploys, exfiltration). Availability depends on the model/plan/provider. |

### Changing the permission mode

Three equivalent ways, all profile-scoped:

- **UI** — Settings → Tools & Skills → Claude Code → set *Permission mode*
  (a dropdown populated from the live mode list; you may also type a value).
- **CLI** — `cremind tools set-var claude_code CLAUDE_CODE_PERMISSION_MODE=plan`.
  When the SDK is installed the server rejects a value outside its mode list
  with HTTP 400 and lists the valid ones; pass `--force` to set an unlisted
  value anyway. If the SDK isn't installed the list can't be resolved and any
  value is accepted (the tool can't run without the SDK regardless).
- **Agent** — the assistant can run that same `cremind tools set-var` command
  through its Shell Executor tool (the shell already has `CREMIND_SERVER` and
  `CREMIND_TOKEN` set, so no flags are needed).

A change takes effect on the **next** Claude Code task for that profile — no
server restart is needed (the value is re-read per task).

## Choosing a model

The `CLAUDE_CODE_MODEL` variable selects which Claude model coding tasks run on.
Like the permission mode, its list of values is **dynamic** — but it is fetched
live from the Anthropic account the tool's credential resolves to (see *Enabling
the tool* for the credential chain), so it reflects exactly the models that
account can use. When that list is available, the server **rejects** a
`CLAUDE_CODE_MODEL` value it doesn't recognize and returns the valid ids, so a
guessed or mistyped id fails loudly instead of silently persisting. The aliases
`sonnet`, `opus`, `haiku`, `opusplan` always pass. If the account list can't be
fetched (no credential / offline), any value is accepted. Empty = Claude Code's
default model.

Three equivalent ways, all profile-scoped:

- **UI** — Settings → Tools & Skills → Claude Code → the *Model* field is a
  dropdown populated from the account's live model list; you can also type a
  custom id or alias (the UI intentionally allows unverified values).
- **CLI** — list, then set:

  ```bash
  cremind tools options claude_code            # the account's live model list
  cremind tools options claude_code --refresh  # bypass the 5-minute cache
  cremind tools set-var claude_code CLAUDE_CODE_MODEL=claude-sonnet-4-5
  ```

  Setting an id that isn't in the list is rejected with the valid ids; pass
  `--force` to set a custom/unverified id anyway.

- **Agent** — **always run `cremind tools options claude_code --json` first**
  (through the Shell Executor tool) and copy an exact `id` from the output; then
  apply it with `cremind tools set-var claude_code CLAUDE_CODE_MODEL=<id>`.
  **Do not guess a model id or use one from memory** — ids like
  `claude-3-opus-…` are wrong; the account uses ids such as `claude-opus-4-8`.
  If you pass an unrecognized id, `set-var` fails and lists the valid ids — read
  them and retry with a real one. The `claude_code__status` sub-tool also returns
  the same account model list in its `models` field, so "which models can Claude
  Code use?" can be answered directly — but `cremind tools options claude_code`
  is the canonical list-and-set flow.

If no Anthropic credential is available, the list comes back empty with an
`error` note and the model stays a free-form text field (no rejection). A change
takes effect on the next Claude Code task.

## All Tool Variables

Every variable is optional; the table gives its exact name and default.

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `CLAUDE_CODE_MODEL` | string | `""` | Claude model for coding tasks — pick from the account's live model list (see *Choosing a model*) or type an id/alias (e.g. `claude-sonnet-4-5`, `opus`). Empty = Claude Code's default model. |
| `CLAUDE_CODE_PERMISSION_MODE` | string (dynamic list) | `bypassPermissions` | See the permission modes above; list the live values with `cremind tools options claude_code`. |
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
cremind tools get claude_code --json      # schema + current values (no static mode list)
cremind tools options claude_code         # the live model AND permission-mode lists
```

`CLAUDE_CODE_PERMISSION_MODE` and `CLAUDE_CODE_MODEL` are dynamic-list variables,
so their allowed values come from `cremind tools options` rather than a static
`enum` in the `tools get` schema.

See `cremind tools` for the full tool-configuration CLI reference.
