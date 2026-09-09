---
description: "The Codex built-in tool and its sandbox modes: the filesystem sandbox levels (read-only, workspace-write, full-access) come live from the installed OpenAI Codex SDK, so what each allows and how to change one, listed with cremind tools options codex. Codex runs headless so it never pauses for approval. Plus how to choose the model from the account's live model list (cremind tools options codex, or the status sub-tool's models field) and its other Tool Variables (model, sandbox, reasoning effort, OpenAI API key, codex binary path, config overrides, max concurrent tasks). Also covers how to install Codex on this server without shell access — the codex feature / OpenAI Codex SDK wheel that bundles the codex CLI binary, one click from the Coding Agents section of Settings → Tools & Skills or `cremind features install codex` — plus how to sign in / log in to Codex and which credential it uses, checked with `cremind tools coding-agents --probe`. Signing in is the `codex` CLI's own login, NOT Settings → LLM Providers: Settings → Tools & Skills → Coding Agents → Codex → Sign in with ChatGPT shows a device code and a link you confirm in any browser, `codex login --device-auth` does the same in a terminal, and `cremind tools coding-agents login codex` runs it on the server host; sign out with Sign out or `cremind tools coding-agents logout codex`. The login is per profile, in that profile's own CODEX_HOME under the Cremind System Directory (auth.json), and a profile that never signs in inherits the server's shared login; the credential order is the CODEX_API_KEY tool variable, then CODEX_API_KEY / OPENAI_API_KEY in the server environment, then this profile's login, then the shared one (sources tool_variable_api_key, env_codex_api_key, env_openai_api_key, profile_codex_login, host_codex_login). The old bridge that reused a profile's Sign in with ChatGPT LLM login for the tool (credential source profile_chatgpt_login) has been REMOVED — anyone who relied on it must sign in to Codex itself once. Adds that Codex is disabled by default and how to write a new Cremind skill with Codex (delegating skill authoring to it against the skill-creator contract). Troubleshooting a blocked run: if Codex made no changes because CODEX_SANDBOX is read-only (or the task needed to write outside the working directory under workspace-write), the fix is `cremind tools set-var codex CODEX_SANDBOX=full-access` (or workspace-write), not any UI toggle or `codex` CLI command; also covers a resumed session that returns no output (start a fresh task without session_id) and a session_id that no longer resumes because threads belong to the CODEX_HOME that created them. Distinct from the general `cremind tools` CLI reference and from the Claude Code tool."
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
(including Windows `win_amd64`), so installing the feature *is* installing the
Codex CLI; no separate npm/node install is needed.

The shortest path is the UI: **Settings → Tools & Skills**, whose first section
is **Coding Agents** — Codex and Claude Code have their own section there and are
no longer listed under *Built-in Tools*. The Codex card detects whether the SDK
is present, installs it with one click (streaming the pip log), switches the tool
on for the profile, and runs the sign-in itself — none of which needs shell
access to the server.

The same thing from a terminal:

```bash
# 1. Install the backing feature (Python extras: codex)
cremind features install codex

# 2. Enable the tool for the active profile
cremind tools enable codex
```

If you try to enable it before installing the feature, the server rejects the
call with HTTP 409 `FeatureNotInstalled`. The per-tool switch on the
**Settings → Tools & Skills → Coding Agents → Codex** card toggles the same flag.

## Credentials: the `codex` CLI's own login

Codex authenticates the way the `codex` CLI does — the app-server reads
`$CODEX_HOME/auth.json` — so a credential is either an **API key set for this
tool** or a **`codex login` you made in the CLI**. The profile's OpenAI settings
under Settings → LLM Providers are deliberately **not** a tier: configuring an
OpenAI model for chatting must not silently hand the Codex tool a paid coding
agent on that key.

The tiers, in the order they resolve (the winner is what
`cremind tools coding-agents` prints under `CREDENTIAL` and what the status
sub-tool returns as `credential_source`):

| Order | `credential_source` | Where it comes from |
|-------|---------------------|---------------------|
| 1 | `tool_variable_api_key` | The `CODEX_API_KEY` Tool Variable below (this profile's). |
| 2 | `env_codex_api_key` | `CODEX_API_KEY` in the **server's** environment (shared by every profile). |
| 3 | `env_openai_api_key` | `OPENAI_API_KEY` in the server's environment. |
| 4 | `profile_codex_login` | This profile's **own** `codex login` (`credential_scope: profile`). |
| 5 | `host_codex_login` | The server's **shared** login, inherited by any profile without one (`credential_scope: shared`). |
| — | `none` | Nothing is visible — sign in, or set a key. |

> **`profile_chatgpt_login` is gone.** An earlier release reused a profile's
> **Settings → LLM Providers → OpenAI → Sign in with ChatGPT** login for this
> tool. That bridge has been removed: it made one single-use refresh-token chain
> shared state between the LLM transport and a subprocess that rotates it behind
> Cremind's back, so a task killed at the wrong moment left the *provider*
> holding a spent token. If Codex used to authenticate that way, **sign in to
> Codex itself once** (below); the OpenAI provider keeps working untouched.

### Signing in

The login belongs to the CLI, so Cremind runs the CLI for you. Three doors, all
equivalent:

- **UI (no shell access needed)** — **Settings → Tools & Skills → Coding Agents
  → Codex → Sign in with ChatGPT**. Cremind starts the device-code flow and the
  dialog shows a URL and a short code; open the link on any device, enter the
  code, and the dialog reports the account it signed in as. The code is valid
  for **15 minutes**, and the flow needs the Cremind server to stay up for that
  window — a restart mid-flow drops the pending sign-in ("Sign-in interrupted
  — start again"). **Check sign-in** re-runs the live probe and **Sign out**
  reverses it.
- **A terminal on the server** — `codex login --device-auth` (the device-code
  variant on purpose: plain `codex login` waits on a browser and a loopback
  redirect, which is nothing on a headless server).
  `cremind tools coding-agents login codex` runs exactly that for you with the
  right home already set, and refuses when `cremind` is pointed at a *remote*
  server, because the credential would land on the wrong machine.
- **Sign out from anywhere** — `cremind tools coding-agents logout codex` (no
  binary needed locally; the server runs `codex logout` and removes `auth.json`).

### Where the login lives, and who inherits it

Logins are **per profile**: a sign-in writes `auth.json` under
`<CREMIND_SYSTEM_DIR>/<profile>/coding-cli/codex`, which is also what the
`CODEX_HOME` system variable points every Cremind shell at, so a member profile
running `codex login` in a Cremind terminal can never overwrite the operator's
account.

A profile with no login of its own falls back to the **server's shared login**:
`$CODEX_HOME` if the operator set one, else `~/.codex` (`%USERPROFILE%\.codex`
on Windows) on a native install, else `<CREMIND_SYSTEM_DIR>/coding-cli/codex` in
a container (the Docker image and the Helm chart both set it there, so a login
survives `docker compose down`, an image upgrade and a pod replacement — `/root`
is not a volume, `~/.cremind` is). Signing out the shared login is admin-only
(`cremind tools coding-agents logout codex --shared`, or the card's *Sign out
(shared login)*) because every inheriting profile loses it.

When the credential is an **API key** instead — from `CODEX_API_KEY` or the
server environment — Cremind installs it into a **managed** `CODEX_HOME`
(`<CREMIND_SYSTEM_DIR>/codex-home/<credential fingerprint>`) rather than into any
login home, so a key never overwrites a `codex login`. One directory per
credential also means concurrent tasks under different keys never share an
`auth.json`.

Either way, **a thread belongs to the home that created it**: a `session_id`
resumes only under the same `CODEX_HOME`, so switching credentials — or a profile
that was on the shared login signing in for itself — starts a new thread rather
than resuming an old one. `cli_home` in the status payload always names the home
a run authenticated from.

To check all three things that must line up — installed, enabled for this
profile, and which credential resolves — for both coding agents at once:

```bash
cremind tools coding-agents          # claude_code and codex side by side
cremind tools coding-agents --probe  # also run the live sign-in check
```

The `status` sub-tool (with `probe=true`) answers the same question from inside
a conversation, reports `credential_scope`, `cli_home` and the signed-in account
alongside the source, and also returns the account's model list.

## Writing Cremind skills

Authoring a new Cremind **skill** is a coding task, so it can be delegated here.
When the user asks for a new skill and the `skill-creator` skill is enabled too,
the assistant asks **once** which path the user wants — build it with
`skill-creator` in the conversation, or hand it to Codex — and starts only after
they answer.

When it delegates, the brief hands Codex the `skill-creator` directory as the
contract: read its `SKILL.md` and `references/spec.md`, `events.md` and
`templates.md` first, write the new skill into the profile skills root
(`<skills root>/<name>`, a sibling of `skill-creator`), run `uv run
skill-creator/scripts/validate.py <name>` until it reports `PASS`, and touch
nothing outside that root. This writes files, so the sandbox must allow it —
`full-access`, or `workspace-write` with the working directory set to the skills
root (see *Sandbox modes*). The skill hot-loads within ~1s — no restart — and
`cremind skill-events events <name>` confirms it registered.

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

- **UI** — Settings → Tools & Skills → Coding Agents → Codex → set *Sandbox*
  (a dropdown
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
One specific cause: a thread belongs to the `CODEX_HOME` that created it, so a
session started under the shared server login, or under a different API key, is
not visible after the credential changes (see *Where the login lives*).

**"No Codex credential is available for this profile."** Nothing in the tier list
resolved. Sign in — Settings → Tools & Skills → Coding Agents → Codex → Sign in,
or `cremind tools coding-agents login codex` on the server host — or set the
`CODEX_API_KEY` tool variable, or `CODEX_API_KEY` / `OPENAI_API_KEY` in the
server environment. Settings → LLM Providers → OpenAI does **not** feed this
tool any more; a profile that used to authenticate through the removed ChatGPT
bridge has to sign in to Codex once.

## Choosing a model

The `CODEX_MODEL` variable selects which Codex model coding tasks run on. Like
the sandbox, its list of values is **dynamic** — but it is fetched live from the
OpenAI account the tool's credential resolves to (see *Credentials* for the
chain), via the Codex SDK's model listing, so it reflects exactly the
models that account can use. When that list is available, the server **rejects**
a `CODEX_MODEL` value it doesn't recognize and returns the valid ids, so a
guessed or mistyped id fails loudly instead of silently persisting. If the
account list can't be fetched (no credential / offline), any value is accepted.
Empty = Codex's default model.

Three equivalent ways, all profile-scoped:

- **UI** — Settings → Tools & Skills → Coding Agents → Codex → the *Model* field
  is a dropdown
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

- **Agent** — **always run `cremind --json tools options codex` first** (through
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
| `CODEX_API_KEY` | string (secret) | `""` | OpenAI API key for Codex. Empty = fall back to `CODEX_API_KEY` / `OPENAI_API_KEY` in the server environment, then this profile's own `codex login`, then the server's shared login. Never the profile's OpenAI LLM-provider credentials — see *Credentials*. A key set here is installed into a Cremind-managed `CODEX_HOME`, never your own `~/.codex`. |
| `CODEX_BIN` | string | `""` | Absolute path to an external codex binary. Empty = the SDK's bundled binary. |
| `CODEX_CONFIG_OVERRIDES` | string | `""` | Comma-separated Codex `--config` overrides (e.g. `model_reasoning_effort=high, sandbox_mode=workspace-write`). Empty = none. |
| `CODEX_MAX_CONCURRENT_TASKS` | number | `2` | Maximum Codex tasks running at once across all conversations. |

`CODEX_API_KEY` is a secret: its value is masked everywhere it is read back
(shown as set/not set, never the value).

To view the live schema and the current per-profile values:

```bash
cremind --json tools get codex      # schema + current values (no static mode list)
cremind tools options codex         # the live model AND sandbox-mode lists
```

`CODEX_SANDBOX` and `CODEX_MODEL` are dynamic-list variables, so their allowed
values come from `cremind tools options` rather than a static `enum` in the
`tools get` schema.

See `cremind tools` for the full tool-configuration CLI reference.
