---
description: "The Codex built-in tool and its sandbox modes: the filesystem sandbox levels (read-only, workspace-write, full-access) come live from the installed OpenAI Codex SDK, so what each allows and how to change one, listed with cremind tools options codex. Codex runs headless so it never pauses for approval. Plus how to choose the model from the account's live model list (cremind tools options codex, or the status sub-tool's models field) and its other Tool Variables (model, sandbox, reasoning effort, OpenAI API key, codex binary path, config overrides, max concurrent tasks). Also covers whether Codex is logged in / which credential it uses (including a host `codex login` and the Cremind-managed CODEX_HOME on Windows %USERPROFILE%\\.codex), that Codex is disabled by default, and how to enable it (the codex feature / OpenAI Codex SDK). Troubleshooting a blocked run: if Codex made no changes because CODEX_SANDBOX is read-only (or the task needed to write outside the working directory under workspace-write), the fix is `cremind tools set-var codex CODEX_SANDBOX=full-access` (or workspace-write), not any UI toggle or `codex` CLI command; also covers a resumed session that returns no output (start a fresh task without session_id). Distinct from the general `cremind tools` CLI reference and from the Claude Code tool."
---

# Codex Tool

**Codex** is a built-in tool that delegates software-engineering work to OpenAI's
autonomous coding agent through the OpenAI Codex SDK. When it is enabled, Cremind
delegates ALL software-engineering work to Codex instead of using its own
shell/file tools — not only creating, writing, refactoring, debugging, and
testing code, but also reading, understanding, exploring, explaining, and
reviewing an existing codebase. Understanding or explaining source code counts as
a coding task, so Cremind hands it to Codex rather than opening the files itself.

Its `tool_id` is `codex`. Coding sessions run as background tasks; the model
starts one with `run`, polls with `wait`, aborts with `stop`, and checks setup
with `status`. Only Codex's final result and stats are returned to the agent —
its intermediate reasoning streams only to the user's Agent Activity panel.

Codex and **Claude Code** are peer coding delegates. If both are enabled, either
can handle any coding task; the assistant picks per task and honours the user
naming one ("use Codex"). A `session_id` only resumes with the agent that
produced it.

## Enabling the tool

Codex is **disabled by default**. Enabling it requires the `codex` feature — the
OpenAI Codex SDK, whose `openai-codex-cli-bin` wheel bundles the codex binary
(including Windows `win_amd64`), so no separate npm/node install is needed.

```bash
# 1. Install the backing feature (Python extras: codex)
cremind features install codex

# 2. Enable the tool for the active profile
cremind tools enable codex
```

If you try to enable it before installing the feature, the server rejects the
call with HTTP 409 `FeatureNotInstalled`. You can also toggle it from
**Settings → Tools & Skills → Codex**.

For the tool to actually run coding tasks it needs an OpenAI credential — either
the `CODEX_API_KEY` variable below, the profile's OpenAI LLM credentials
(Settings → LLM), the server environment (`CODEX_API_KEY` / `OPENAI_API_KEY`), or
a host-level `codex login`. Run the `status` sub-tool (with `probe=true`) to
confirm the active account credential.

The app-server authenticates from `$CODEX_HOME/auth.json` (`~/.codex`, or
`%USERPROFILE%\.codex` on Windows). A host `codex login` works out of the box.
When you supply a key via `CODEX_API_KEY` or the profile/server environment,
Cremind installs it into a **managed** `CODEX_HOME`
(`<CREMIND_SYSTEM_DIR>/codex-home`) so it never overwrites your own `~/.codex`
login. Because of this, a thread started under a managed key cannot be resumed
under a host login and vice versa — resume a `session_id` with the same
credential it was created under.

## Sandbox modes

The sandbox controls how much of the filesystem Codex may touch. It is the
`CODEX_SANDBOX` Tool Variable. Its list of values is **dynamic** — it comes live
from the installed Codex SDK's `Sandbox` enum, so a newer SDK that adds levels
exposes them automatically. List the current set with `cremind tools options
codex`. The levels the SDK ships today:

| Mode              | What it allows |
|-------------------|----------------|
| `full-access`     | **The default.** No filesystem restrictions — fully autonomous, the same trust level as the Shell Executor tool. Recommended for headless/server use, matching Claude Code's `bypassPermissions` default. |
| `workspace-write` | Codex may read anywhere but only write files and run commands inside the working directory (and configured writable roots). A safer default at the cost of occasional failures when a task must touch files outside the workspace. |
| `read-only`       | Codex may read and reason but makes no changes. Good for explain/review tasks; any edit task needs the sandbox raised first. |

Codex runs **headless**, so approvals are pinned off internally (approval mode
`deny_all` — it never pauses for a human). The sandbox is therefore the safety
knob, not an approval prompt. (On native Windows the OS-level sandbox is newer
than on macOS/Linux; if a `workspace-write` task behaves unexpectedly, try
`full-access` or run Codex under WSL2.)

Two notes on how the effective sandbox is resolved: a `sandbox_mode=<value>`
entry in `CODEX_CONFIG_OVERRIDES` also sets the sandbox and takes precedence over
`CODEX_SANDBOX`; and an **unrecognized** `CODEX_SANDBOX` value falls back to
`full-access` (never fails the run). Either way the value Codex actually used is
reported back as `effective_sandbox`, and a fall-back from an unrecognized value
is flagged with a `sandbox_coercion_note`, so what is reported never contradicts
what ran.

### Changing the sandbox mode

Three equivalent ways, all profile-scoped:

- **UI** — Settings → Tools & Skills → Codex → set *Sandbox* (a dropdown
  populated from the live mode list; you may also type a value).
- **CLI** — `cremind tools set-var codex CODEX_SANDBOX=workspace-write`. When the
  SDK is installed the server rejects a value outside its mode list with HTTP 400
  and lists the valid ones; pass `--force` to set an unlisted value anyway. If
  the SDK isn't installed the list can't be resolved and any value is accepted
  (the tool can't run without the SDK regardless).
- **Agent** — the assistant can run that same `cremind tools set-var` command
  through its Shell Executor tool (the shell already has `CREMIND_SERVER` and
  `CREMIND_TOKEN` set, so no flags are needed).

A change takes effect on the **next** Codex task for that profile — no server
restart is needed (the value is re-read per task).

## Symptoms & troubleshooting

**Codex explored but did not change anything.** If a coding task made no file
changes, `CODEX_SANDBOX` is likely `read-only` (explore/answer only). Under
`workspace-write` a task that needed to touch files *outside* the working
directory can also be blocked. The sandbox is set by Cremind's `CODEX_SANDBOX`
tool variable — there is no UI sandbox toggle for the user to flip and no `codex`
CLI command that changes it here.

Every `codex` result carries `effective_sandbox`, and when the sandbox is not
fully autonomous it also carries a `sandbox_advisory` object with the exact fix.
The playbook is **confirm once, then fix**: tell the user the current sandbox
blocks changes, ask once whether to switch it, and only on their OK run — through
the Shell Executor tool:

```bash
cremind tools set-var codex CODEX_SANDBOX=full-access
```

(or `workspace-write` to confine changes to the working directory), then re-run
the task (reuse the `session_id` to continue).

**A resumed session returned no output.** The thread may have expired or is no
longer resumable. Do not report an empty result or ask the user what to do —
start a **fresh** task with `run` *without* a `session_id`, repeating the full
task brief. (The result carries `resume_produced_no_work: true` in this case.)

## Choosing a model

The `CODEX_MODEL` variable selects which Codex model coding tasks run on. Like
the sandbox, its list of values is **dynamic** — but it is fetched live from the
OpenAI account the tool's credential resolves to (see *Enabling the tool* for the
credential chain), via the Codex SDK's model listing, so it reflects exactly the
models that account can use. When that list is available, the server **rejects**
a `CODEX_MODEL` value it doesn't recognize and returns the valid ids, so a
guessed or mistyped id fails loudly instead of silently persisting. If the
account list can't be fetched (no credential / offline), any value is accepted.
Empty = Codex's default model.

Three equivalent ways, all profile-scoped:

- **UI** — Settings → Tools & Skills → Codex → the *Model* field is a dropdown
  populated from the account's live model list; you can also type a custom id
  (the UI intentionally allows unverified values).
- **CLI** — list, then set:

  ```bash
  cremind tools options codex            # the account's live model list
  cremind tools options codex --refresh  # bypass the 5-minute cache
  cremind tools set-var codex CODEX_MODEL=gpt-5.1-codex
  ```

  Setting an id that isn't in the list is rejected with the valid ids; pass
  `--force` to set a custom/unverified id anyway.

- **Agent** — **always run `cremind tools options codex --json` first** (through
  the Shell Executor tool) and copy an exact `id` from the output; then apply it
  with `cremind tools set-var codex CODEX_MODEL=<id>`. **Do not guess a model id
  or use one from memory.** If you pass an unrecognized id, `set-var` fails and
  lists the valid ids — read them and retry with a real one. The `codex__status`
  sub-tool also returns the same account model list in its `models` field, so
  "which models can Codex use?" can be answered directly — but `cremind tools
  options codex` is the canonical list-and-set flow.

If no OpenAI credential is available, the list comes back empty with an `error`
note and the model stays a free-form text field (no rejection). A change takes
effect on the next Codex task.

## All Tool Variables

Every variable is optional; the table gives its exact name and default.

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `CODEX_MODEL` | string (dynamic list) | `""` | Codex model for coding tasks — pick from the account's live model list (see *Choosing a model*) or type an id (e.g. `gpt-5.1-codex`). Empty = Codex's default model. |
| `CODEX_SANDBOX` | string (dynamic list) | `full-access` | Filesystem sandbox; see the sandbox modes above. List the live values with `cremind tools options codex`. |
| `CODEX_REASONING_EFFORT` | string | `""` | Reasoning effort per task (`none`, `minimal`, `low`, `medium`, `high`, `xhigh`). Empty = the model's default. Higher effort is slower and costs more tokens. |
| `CODEX_API_KEY` | string (secret) | `""` | OpenAI API key for Codex. Empty = fall back to the profile's OpenAI LLM credentials, then the server environment (`CODEX_API_KEY` / `OPENAI_API_KEY`) or `codex login`. A key set here is installed into a Cremind-managed `CODEX_HOME`, never your own `~/.codex`. |
| `CODEX_BIN` | string | `""` | Absolute path to an external codex binary. Empty = the SDK's bundled binary. |
| `CODEX_CONFIG_OVERRIDES` | string | `""` | Comma-separated Codex `--config` overrides (e.g. `model_reasoning_effort=high, sandbox_mode=workspace-write`). Empty = none. |
| `CODEX_MAX_CONCURRENT_TASKS` | number | `2` | Maximum Codex tasks running at once across all conversations. |

`CODEX_API_KEY` is a secret: its value is masked everywhere it is read back
(shown as set/not set, never the value).

To view the live schema and the current per-profile values:

```bash
cremind tools get codex --json      # schema + current values (no static mode list)
cremind tools options codex         # the live model AND sandbox-mode lists
```

`CODEX_SANDBOX` and `CODEX_MODEL` are dynamic-list variables, so their allowed
values come from `cremind tools options` rather than a static `enum` in the
`tools get` schema.

See `cremind tools` for the full tool-configuration CLI reference.
