---
description: "The Claude Code built-in tool (claude_code, disabled by default): delegate coding tasks to Anthropic's Claude Code CLI via the Claude Agent SDK. Tool Variables: model (live list via `cremind tools options claude_code`), permission mode (bypassPermissions, acceptEdits, default, plan, dontAsk, auto), max turns, max budget USD, API key, long-lived OAuth token, CLI path, allowed/disallowed tools, max concurrent tasks; installing it without shell access (`cremind features install claude_code`, or Settings → Tools & Skills → Coding Agents); signing in and out (the Sign in dialog runs `claude auth login`; headless servers use `claude setup-token` + CLAUDE_CODE_OAUTH_TOKEN; `cremind tools coding-agents login/logout claude_code`), per-profile vs shared login and the credential order; troubleshooting a run blocked by a read-only permission mode (plan/default/dontAsk — set CLAUDE_CODE_PERMISSION_MODE=bypassPermissions), a resumed session with no output, and a CLI hanging at 100% CPU on a qemu64 virtual CPU (cli_blocked; fix the hypervisor CPU model). Distinct from `cremind tools` and the Codex tool."
---

# Claude Code Tool

**Claude Code** is a built-in tool that delegates software-engineering work to
Anthropic's autonomous coding agent through the Claude Agent SDK. When it is
enabled, Cremind delegates ALL software-engineering work to Claude Code instead
of using its own shell/file tools — not only creating, writing, refactoring,
debugging, and testing code, but also reading, understanding, exploring,
explaining, and reviewing an existing codebase. Understanding or explaining
source code counts as a coding task, so Cremind hands it to Claude Code rather
than opening the files itself.

Its `tool_id` is `claude_code`. Coding sessions run as background tasks; the
model starts one with `run`, polls with `wait`, aborts with `stop`, and checks
setup with `status`. Only Claude Code's final result and stats are returned to
the agent — its intermediate reasoning streams only to the user's Agent
Activity panel.

## Enabling the tool

Claude Code is **disabled by default**. Enabling it requires the `claude_code`
feature — the Claude Agent SDK, whose wheel bundles the Claude Code CLI binary,
so installing the feature *is* installing the Claude Code CLI (there is no
separate npm/node step).

The shortest path is the UI: **Settings → Tools & Skills**, whose first section
is **Coding Agents** — Claude Code and Codex have their own section there and are
no longer listed under *Built-in Tools*. The Claude Code card detects whether the
SDK is present, installs it with one click (streaming the pip log), switches the
tool on for the profile, and runs the sign-in itself — none of which needs shell
access to the server.

The same thing from a terminal:

```bash
# 1. Install the backing feature (Python extras: claude-code)
cremind features install claude_code

# 2. Enable the tool for the active profile
cremind tools enable claude_code
```

If you try to enable it before installing the feature, the server rejects the
call with HTTP 409 `FeatureNotInstalled`. The per-tool switch on the
**Settings → Tools & Skills → Coding Agents → Claude Code** card toggles the
same flag.

## Credentials: the `claude` CLI's own login

Claude Code authenticates the way the `claude` CLI does — from a **CLI home**
(`CLAUDE_CONFIG_DIR`) that the CLI itself writes when you sign in — or from an
API key set for this tool. It deliberately does **not** read the profile's
Anthropic credentials under Settings → LLM Providers: that provider is Cremind's
own reasoning credential, and a user who signs in to Claude Code with a Claude
subscription while configuring a separate Anthropic API key for chat expects
coding tasks not to be billed to the second one.

The tiers, in the order they resolve (the winner is what
`cremind tools coding-agents` prints under `CREDENTIAL` and what the status
sub-tool returns as `credential_source`):

| Order | `credential_source` | Where it comes from |
|-------|---------------------|---------------------|
| 1 | `tool_variable_oauth_token` | The `CLAUDE_CODE_OAUTH_TOKEN` Tool Variable below (this profile's) — a long-lived token from `claude setup-token`. |
| 2 | `tool_variable_api_key` | The `CLAUDE_CODE_API_KEY` Tool Variable below (this profile's). |
| 3 | `env_anthropic_api_key` | `ANTHROPIC_API_KEY` in the **server's** environment (shared by every profile). |
| 4 | `env_oauth_token` | `CLAUDE_CODE_OAUTH_TOKEN` in the server's environment. |
| 5 | `profile_claude_login` | This profile's **own** `claude auth login` (`credential_scope: profile`). |
| 6 | `host_claude_login` | The server's **shared** login, inherited by any profile without one (`credential_scope: shared`). |
| — | `none` | Nothing is visible — sign in, or set a key. |

A key beats a login on purpose: setting `CLAUDE_CODE_API_KEY` is a deliberate
statement about which account pays for coding work. The pasted long-lived token
beats the key for a different reason — the `claude` CLI itself prefers
`CLAUDE_CODE_OAUTH_TOKEN` over `ANTHROPIC_API_KEY` when both are set, so
Cremind reports the one the run will actually use.

### Signing in

The login belongs to the CLI, so Cremind runs the CLI for you. Four doors, all
equivalent:

- **UI (no shell access needed)** — **Settings → Tools & Skills → Coding Agents
  → Claude Code → Sign in**. Cremind opens a built-in terminal running the real
  `claude auth login` under a PTY, against this profile's own CLI home, and you
  answer its prompts there. In a container Cremind removes `DISPLAY` from that
  terminal's environment, so the CLI cannot try to open a browser on the VNC
  desktop and prints the URL for you to open elsewhere instead. **Check
  sign-in** re-runs the live probe, and **Sign out** reverses it.
- **A pasted long-lived token (no browser on the server at all)** — if the
  terminal login still cannot get you in, run `claude setup-token` on any
  machine that *does* have a browser (it needs a Claude subscription) and paste
  the token it prints into the same Sign-in dialog, or set it as the
  `CLAUDE_CODE_OAUTH_TOKEN` Tool Variable. `claude auth login` has no headless
  flag, so on a server nothing can reach a browser from, this is the way in.
- **A shell on the server** — `cremind tools coding-agents login claude_code`
  runs the same `claude auth login` in your terminal with the right home
  already set, and inside a container it drops `DISPLAY` exactly as the built-in
  terminal does, so it prints the URL rather than opening a browser on the VNC
  desktop. It refuses when `cremind` is pointed at a *remote* server, because
  the credential would land on the wrong machine.
- **Sign out from anywhere** — `cremind tools coding-agents logout claude_code`
  (no binary needed locally; the server runs `claude auth logout` and removes
  the credential file). A pasted token is not a login, so it is cleared by
  emptying the Tool Variable, not by signing out.

### Where the login lives, and who inherits it

Logins are **per profile**: a sign-in writes
`<CREMIND_SYSTEM_DIR>/<profile>/coding-cli/claude`, which is also what the
`CLAUDE_CONFIG_DIR` system variable points every Cremind shell at, so a member
profile running `claude auth login` in a Cremind terminal can never overwrite
the operator's account.

A profile with no login of its own falls back to the **server's shared login**:
`$CLAUDE_CONFIG_DIR` if the operator set one, else `~/.claude` on a native
install, else `<CREMIND_SYSTEM_DIR>/coding-cli/claude` in a container (the
Docker image and the Helm chart both set it there, so a login survives
`docker compose down`, an image upgrade and a pod replacement — `/root` is not a
volume, `~/.cremind` is). Signing out the shared login is admin-only
(`cremind tools coding-agents logout claude_code --shared`, or the card's
*Sign out (shared login)*) because every inheriting profile loses it.

Two consequences worth knowing:

- **Sessions live under the home that ran them.** A `session_id` resumes only
  under the same `CLAUDE_CONFIG_DIR`, so a profile that was using the shared
  login and then signs in for itself cannot resume its older sessions — start a
  fresh task instead. `cli_home` in the status payload always names the home a
  run authenticated from.
- **On macOS the credential is in the login Keychain**, not in the home, so
  per-profile isolation there is best-effort: Cremind detects the login from
  `.claude.json` but the credential behind two different homes is the same
  Keychain entry for that OS user. The probe (`claude auth status`) is the
  authority on macOS.

To check all three things that must line up — installed, enabled for this
profile, and which credential resolves — for both coding agents at once:

```bash
cremind tools coding-agents          # claude_code and codex side by side
cremind tools coding-agents --probe  # also run the live sign-in check
```

The `status` sub-tool (with `probe=true`) answers the same question from inside
a conversation, and reports `credential_scope`, `cli_home` and the signed-in
account alongside the source.

## Writing Cremind skills

Authoring a new Cremind **skill** is a coding task, so it can be delegated here.
When the user asks for a new skill and the `skill-creator` skill is enabled too,
the assistant asks **once** which path the user wants — build it with
`skill-creator` in the conversation, or hand it to Claude Code — and starts only
after they answer.

When it delegates, the brief hands Claude Code the `skill-creator` directory as
the contract: read its `SKILL.md` and `references/spec.md`, `events.md` and
`templates.md` first, write the new skill into the profile skills root
(`<skills root>/<name>`, a sibling of `skill-creator`), run `uv run
skill-creator/scripts/validate.py <name>` until it reports `PASS`, and touch
nothing outside that root. The skill hot-loads within ~1s — no restart — and
`cremind skill-events events <name>` confirms it registered.

## Permission modes

The permission mode controls how autonomously Claude Code acts. It is the
`CLAUDE_CODE_PERMISSION_MODE` Tool Variable. Its list of values is **dynamic** —
it comes live from the installed Claude Agent SDK (the same modes the Claude Code
CLI cycles through with Shift+Tab), so a newer SDK that adds modes exposes them
automatically. List the current set with `cremind tools options
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

- **UI** — Settings → Tools & Skills → Coding Agents → Claude Code → set
  *Permission mode*
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

## Symptoms & troubleshooting

**Everything says "the Claude Code CLI cannot run on this server's CPU".** The
card, the status sub-tool and any coding task all refuse immediately with a CPU
model and a list of missing instructions; run `claude` by hand on that host and
it hangs at 100% CPU forever with nothing in its logs, while `claude --version`
answers instantly. On real (non-virtual) hardware the same gap kills it with
`Illegal instruction` instead.

The Claude Code CLI inside the Claude Agent SDK wheel is a **Bun single-file
executable built for the x86-64-v2 instruction level**. A guest CPU that
advertises none of `ssse3`, `sse4_1`, `sse4_2`, `popcnt` — which is what the
default `qemu64` model reports, as `QEMU Virtual CPU version 2.5+` — sends Bun's
CPUID-based dispatch down a fallback path that never terminates: under KVM the
instructions do execute, so nothing crashes, it just spins. Node.js and the rest
of Cremind are unaffected, which is why only Claude Code fails on such a node.

Cremind reports it in five places, all from the same check: the **Coding Agents**
card, `cremind tools coding-agents` (as `cli_blocked`), the `host_advisory`
object on the `claude_code` status and `run` results, and the CPU rows of
**Developer → Environment** / `cremind server environment`.

The fix is on the hypervisor, and the node must be shut down and started again
afterwards (a live reboot keeps the old CPU model):

- **Proxmox** — VM → Hardware → Processors → Type `host` (or `x86-64-v2-AES`
  if the cluster needs a portable model).
- **libvirt / virt-manager** — `<cpu mode='host-passthrough'/>`, the *Copy host
  CPU configuration* checkbox.
- **plain QEMU** — pass `-cpu host` (or `-cpu x86-64-v3`) instead of letting it
  default to `qemu64`.

Pointing `CLAUDE_CODE_CLI_PATH` at a different `claude` does **not** help: every
Claude Code build is the same kind of executable. If the host is real hardware
that predates x86-64-v2 (roughly 2009), there is nothing to configure — Cremind
has to run somewhere newer for this tool to work. **Codex** is a different
runtime (a Rust binary built for the x86-64 baseline) and this check does not
cover it, so a blocked host may still be able to delegate coding work there.

**Claude Code planned but did not change anything / says it cannot write.**
Messages such as `Cannot write … while in plan mode`, "approve / exit plan mode
on your side", "ExitPlanMode is not enabled in this context", or a completed run
that only produced a plan mean `CLAUDE_CODE_PERMISSION_MODE` is a read-only or
approval-gated mode (`plan`, `default`, or `dontAsk`). Cremind runs Claude Code
**headless** — there is no interactive approver and the `ExitPlanMode` tool is
not available — so the run cannot proceed to edits. This is **not** a "plan
mode" in any UI, and it is **not** changed by a `claude` CLI command or by the
user "exiting plan mode": the only lever is the Cremind tool variable above.

Every `claude_code` result carries `effective_permission_mode`, and when the
mode is not fully autonomous it also carries a `permission_advisory` object with
the exact fix. The playbook is **confirm once, then fix**: tell the user the
current mode blocks changes, ask once whether to switch it, and only on their OK
run — through the Shell Executor tool:

```bash
cremind tools set-var claude_code CLAUDE_CODE_PERMISSION_MODE=bypassPermissions
```

then re-run the coding task (reuse the `session_id` to continue the session).
`bypassPermissions` (the shipped default) runs fully autonomously; `acceptEdits`
is a narrower option when only file edits are needed.

**A resumed session returned no output (empty result, no turns).** The session
may have expired or is no longer resumable. Do not report an empty result or ask
the user what to do — start a **fresh** task with `run` *without* a `session_id`,
repeating the full task brief. (The result carries `resume_produced_no_work:
true` in this case.) One specific cause: transcripts live inside the CLI home,
so a session started under the shared server login is not visible after this
profile signs in with its own (see *Where the login lives*).

**"No Claude Code credential is available for this profile."** Nothing in the
tier list resolved. Sign in — Settings → Tools & Skills → Coding Agents →
Claude Code → Sign in, or `cremind tools coding-agents login claude_code` on the
server host — or, when no browser can reach the server, paste a
`claude setup-token` token as `CLAUDE_CODE_OAUTH_TOKEN`, or set
`CLAUDE_CODE_API_KEY` / `ANTHROPIC_API_KEY`. Pasting a key under Settings → LLM
Providers → Anthropic does **not** help; that credential is Cremind's own
reasoning model's, and this tool never reads it.

## Choosing a model

The `CLAUDE_CODE_MODEL` variable selects which Claude model coding tasks run on.
Like the permission mode, its list of values is **dynamic** — but it is fetched
live from the Anthropic account the tool's credential resolves to (see
*Credentials* for the chain), so it reflects exactly the models that account can
use. When that list is available, the server **rejects** a
`CLAUDE_CODE_MODEL` value it doesn't recognize and returns the valid ids, so a
guessed or mistyped id fails loudly instead of silently persisting. The aliases
`sonnet`, `opus`, `haiku`, `opusplan` always pass. If the account list can't be
fetched (no credential / offline), any value is accepted. Empty = Claude Code's
default model.

Three equivalent ways, all profile-scoped:

- **UI** — Settings → Tools & Skills → Coding Agents → Claude Code → the *Model*
  field is a dropdown populated from the account's live model list; you can also
  type a custom id or alias (the UI intentionally allows unverified values).
- **CLI** — list, then set:

  ```bash
  cremind tools options claude_code            # the account's live model list
  cremind tools options claude_code --refresh  # bypass the 5-minute cache
  cremind tools set-var claude_code CLAUDE_CODE_MODEL=claude-sonnet-4-5
  ```

  Setting an id that isn't in the list is rejected with the valid ids; pass
  `--force` to set a custom/unverified id anyway.

- **Agent** — **always run `cremind --json tools options claude_code` first**
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
| `CLAUDE_CODE_API_KEY` | string (secret) | `""` | Anthropic API key for Claude Code. Empty = fall back to `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` in the server environment, then this profile's own `claude auth login`, then the server's shared login. A `CLAUDE_CODE_OAUTH_TOKEN` Tool Variable wins over this key. Never the profile's Anthropic LLM-provider credentials — see *Credentials*. |
| `CLAUDE_CODE_OAUTH_TOKEN` | string (secret) | `""` | Long-lived Claude Code token from running `claude setup-token` on a machine that has a browser (needs a Claude subscription), pasted here — the way to sign in on a server where no browser can be opened. Outranks every other tier, because the CLI prefers it over an API key. Empty = fall back to the tiers below it in *Credentials*. |
| `CLAUDE_CODE_CLI_PATH` | string | `""` | Absolute path to an external Claude Code CLI binary. Empty = the SDK's bundled CLI. |
| `CLAUDE_CODE_ALLOWED_TOOLS` | string | `""` | Comma-separated allowlist of Claude Code tools (e.g. `Read,Edit,Bash`). Empty = all standard tools. |
| `CLAUDE_CODE_DISALLOWED_TOOLS` | string | `""` | Comma-separated denylist of Claude Code tools. Empty = none denied. |
| `CLAUDE_CODE_MAX_CONCURRENT_TASKS` | number | `2` | Maximum Claude Code tasks running at once across all conversations. |

`CLAUDE_CODE_API_KEY` and `CLAUDE_CODE_OAUTH_TOKEN` are secrets: their values are
masked everywhere they are read back (shown as set/not set, never the value).

To view the live schema and the current per-profile values:

```bash
cremind --json tools get claude_code      # schema + current values (no static mode list)
cremind tools options claude_code         # the live model AND permission-mode lists
```

`CLAUDE_CODE_PERMISSION_MODE` and `CLAUDE_CODE_MODEL` are dynamic-list variables,
so their allowed values come from `cremind tools options` rather than a static
`enum` in the `tools get` schema.

See `cremind tools` for the full tool-configuration CLI reference.
