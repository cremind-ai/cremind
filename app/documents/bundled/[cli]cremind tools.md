---
description: "Configure the tools the Cremind agent can call, from the `cremind tools` CLI: enable or disable a tool, set its Tool Variables (env-style key=value — API keys, limits, modes), list the live option values of a tool's dynamic variables (`options` — e.g. the Claude models available to the logged-in account), get or set its JSON Tool Arguments, and toggle a grouped tool's sub-tools (\"leaves\"). Answers the coding-delegate questions with `coding-agents`: is Claude Code installed, is Codex signed in or logged in, which credential each one resolves to, and how to install the Claude Code or Codex CLI on this server without shell access. Signs a coding delegate in and out from the terminal: `cremind tools coding-agents login claude_code` runs the vendor CLI's own sign-in (`claude auth login`, `codex login --device-auth`) on the server host, and `cremind tools coding-agents logout codex` signs out from anywhere; `--shared` targets the server-wide login that profiles without one of their own inherit, instead of this profile's. Signing Claude Code in headless, where no browser can reach the server: `claude auth login` has no headless flag, so run `claude setup-token` on a machine that has a browser and paste the long-lived token in — `cremind tools set-var claude_code CLAUDE_CODE_OAUTH_TOKEN=<token>`, the `tool_variable_oauth_token` credential source. Explains how to change any tool's settings and how the agent configures tools itself by running these commands in its shell. Distinct from each tool's own `[tool] …` reference doc (which lists that one tool's full variables and allowed values, e.g. Claude Code's permission modes) and from `cremind agents` (registering MCP/A2A servers)."
---

# `cremind tools` — Tool & Skill Configuration

`cremind tools` (alias `cremind tool`) is the CLI for inspecting and configuring
the tools the Cremind agent can call. A tool here is anything the agent
can invoke during a turn: built-in functions baked into the server,
intrinsic agent operations, A2A peer agents, MCP servers, and locally
registered skills.

The group's surface area is small but covers every angle of tool config:

- **Inspection** — `list`, `get`, `options` (live option values for a tool's
  dynamic variables, e.g. Claude Code's available models).
- **Coding delegates** — `coding-agents`, a one-screen readiness report for
  Claude Code and Codex: installed, enabled, which credential, and (with
  `--probe`) whether the account is actually signed in. Its `login` and
  `logout` subcommands sign this profile in and out of the delegate's own CLI.
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
| `built-in`  | Compiled into the server (filesystem, shell, etc.).                       | Optional ones only    |
| `intrinsic` | Agent-control verbs the loop emits (e.g. `final_answer`, `think`).        | No                    |
| `mcp`       | MCP server registered via `cremind agents add --url <url>` (or `--json-config '<json>'` for a stdio server). | Yes |
| `a2a`       | Peer A2A agent. `cremind agents add` registers MCP servers only, so an `a2a` row comes from an existing registration (a restored blueprint, an older install) rather than from a command you can run. | Yes |
| `skill`     | Local skill discovered from a SKILL.md directory.                         | Yes                   |

Use `--type` on `cremind tools list` to filter by these labels.

## Finding this in the web UI

Every operation in this group has a control on the **Tools & Skills** page of
the Cremind web UI:

> **Settings → Tools & Skills**

The page shows one card per tool with its type, an enabled toggle, and a
configuration panel. The panel renders the tool's **Tool Variables** (the same
key/values as `cremind tools set-var`) — for example, Claude Code's *Permission
mode* dropdown, and its *Model* dropdown, which is populated live from the
account's available models (the same list as `cremind tools options`). Tool
**Arguments** are managed from the CLI / API
(`cremind tools set-args` / `get-args`), not from the built-in tool cards. Skill
rows additionally expose a **Register long-running app** action that maps to
`cremind tools register-long-running`.

A **Coding Agents** *section* sits at the top of the same page — its own section
above *Built-in Tools*, which no longer lists Claude Code or Codex — and is the
UI counterpart of `cremind tools coding-agents`. Each agent gets a card showing
its installed / enabled / credential state (and whether the credential is this
profile's own login or the shared server one), installs a missing SDK in place,
runs the same sign-in probe behind **Check sign-in**, and does the sign-in
itself: **Sign in** opens a device code for Codex, or a built-in terminal
running `claude auth login` for Claude Code — whose dialog also takes a pasted
`claude setup-token` token, for a server no browser can reach. **Sign out** is
the counterpart of `cremind tools coding-agents logout`. Nothing here links to
Settings → LLM Providers — those credentials are Cremind's own reasoning
models', and the coding CLIs never read them.

## The agent can configure tools itself

The Cremind assistant can run any `cremind tools` command through its Shell
Executor tool — the shell it spawns already has `CREMIND_SERVER` and
`CREMIND_TOKEN` set for the active profile, so no flags are needed. That is how
the agent answers "what permission modes can Claude Code use?" (via
`cremind --json tools options claude_code`, or by searching its documentation)
and applies "set Claude Code's permission mode to plan" (via
`cremind tools set-var claude_code CLAUDE_CODE_PERMISSION_MODE=plan`). It is also
how the agent handles "use Opus for Claude Code": discover the account's models
with `cremind --json tools options claude_code`, match the requested name against
the returned ids/labels, then apply it with `cremind tools set-var claude_code
CLAUDE_CODE_MODEL=<id>`. For the full list of a tool's variables and their
allowed values, see the per-tool reference docs below.

## Per-tool reference

The variables and arguments differ per tool. Each configurable built-in tool
has its own reference document — search the documentation for the tool's name to
get its full variable list, allowed values, defaults, and CLI recipes:

| Tool | `tool_id` | Reference doc | Notable variables |
|------|-----------|---------------|-------------------|
| Claude Code | `claude_code` | *Claude Code Tool* | `CLAUDE_CODE_PERMISSION_MODE` and `CLAUDE_CODE_MODEL` (both dynamic lists via `options`), budget, API key |
| Codex | `codex` | *Codex Tool* | `CODEX_SANDBOX` and `CODEX_MODEL` (both dynamic lists via `options`), reasoning effort, API key |
| Shell Executor | `exec_shell` | *Shell Executor Tool* | large-output mode, timeouts, RTK; `os` argument |
| System File | `system_file` | *System File Tool* | read/list/search/grep caps |
| Browser | `browser` | *Browser Tool* | headless, channel (enum), CDP URL |
| Web Search | `web_search` | *Web Search Tool* | provider (enum), safe-search (enum), Parallel API key |
| Web Fetch | `web_fetch` | *Web Fetch Tool* | `WEB_FETCH_MAX_CHARS` |
| Image Understanding | `image_understanding` | *Image Understanding Tool* | max image bytes / dimension |
| Audio Understanding | `audio_understanding` | *Audio Understanding Tool* | max audio bytes |
| Google Places | `google_places` | *Google Places Tool* | Maps API key; lat/long arguments |
| AccuWeather Weather | `accuweather_weather` | *AccuWeather Weather Tool* | AccuWeather API key |
| Documentation Search | `documentation_search` | *Documentation Search Tool* | `DEFAULT_TOP_K` |

For any tool not listed, `cremind --json tools get <tool_id>` prints its live
variable schema (including any `enum` of allowed values) and current per-profile
values.

## Global flags

`--json` forces JSON output instead of the default human-readable table. It is a
**root** flag, so it goes *before* the subcommand — `cremind tools list --json`
is rejected with `No such option: --json`:

```bash
cremind --json tools list
```

The one `--json` that belongs *after* a subcommand is `set-args --json '<obj>'`,
which is that command's own option: it takes a JSON value rather than switching
the output format.

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

**Purpose.** Enable or disable a tool for the active profile — A2A, MCP,
skill, or an **optional built-in** (a built-in that ships disabled by default,
e.g. `weather`, `browser`, `claude_code`). Core built-ins (filesystem, shell,
…) and intrinsic tools are always on and cannot be toggled this way.

Some optional built-ins depend on an installable feature (extra Python
packages). Enabling one whose feature is not installed is rejected with HTTP
409 `FeatureNotInstalled`; install the feature first with
`cremind features install <feature>` (e.g. `cremind features install
claude_code`), then re-run `enable`.

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
cremind tools set-var <tool_id> KEY=VALUE [KEY=VALUE...] [--force]
```

**Arguments** (at least one required):

- `<tool_id>` — Target tool.
- `KEY=VALUE` — Repeatable. Splits on the first `=`; subsequent `=`
  characters are part of the value.

**Flags.**

| Flag           | Type | Default | Meaning                                                                 |
|----------------|------|---------|-------------------------------------------------------------------------|
| `--force`, `-f`| bool | `false` | Set even if a value isn't a recognized option for a variable with a live option list (e.g. a custom/unverified `CLAUDE_CODE_MODEL` id). |

**Behavior.** Writes the variables to the server in one call; any
existing variables not mentioned are left untouched (this is a *patch*,
not a *replace*). Silent on success.

**Examples.**

```bash
$ cremind tools set-var skill.daily-brief INBOX=/var/mail/li REPORT_TIME=09:00
$ cremind tools set-var mcp.linear LINEAR_API_KEY=lin_api_...
# Built-in tools take variables too — e.g. Claude Code's permission mode:
$ cremind tools set-var claude_code CLAUDE_CODE_PERMISSION_MODE=plan
```

For built-in tools whose variables declare a static `enum` (such as the
Browser tool's `channel` or Web Search's `provider`), the server validates the
value and rejects anything outside the allowed set with HTTP 400 — so a typo
fails loudly instead of silently persisting. A variable with a **dynamic**
option list (Claude Code's `CLAUDE_CODE_MODEL` and `CLAUDE_CODE_PERMISSION_MODE`,
whose values come from `cremind tools options`) is validated the same way
**when the list can be fetched**: an unrecognized value is rejected with the
valid values listed (model aliases like `opus`/`sonnet` always pass). Pass
`--force` to set a custom or unverified value anyway; if the list can't be
fetched (no credential / offline for models, SDK not installed for modes) any
value is accepted. See the per-tool reference docs (below) for each tool's
variables and their allowed values.

### `cremind tools options`

**Purpose.** List the live option values for a tool's **dynamic** variables —
values fetched at request time rather than baked into a static `enum`. The
primary use is Claude Code: it prints the Claude models available to the account
the tool's credential resolves to (`CLAUDE_CODE_MODEL`) and the permission modes
the installed Claude Agent SDK accepts (`CLAUDE_CODE_PERMISSION_MODE`).

**Syntax.**

```bash
cremind tools options <tool_id> [--refresh]
```

**Arguments** (required):

- `<tool_id>` — Target tool.

**Flags.**

| Flag        | Type | Default | Meaning                                                     |
|-------------|------|---------|-------------------------------------------------------------|
| `--refresh` | bool | `false` | Bypass the 5-minute server-side cache and refetch the list. |

**Behavior.** Renders a `VARIABLE / VALUE / LABEL` table, one row per option. A
tool with no dynamic variables prints `(tool has no dynamic variables)` to
stderr. If a variable's list can't be fetched (e.g. no Anthropic credential, or
the API rejected it), a `(<VARIABLE>: <error>)` note is written to stderr and
that variable contributes no rows. With `--json`, returns
`{"tool_id": ..., "variables": {"<VAR>": {"options": [{"id", "label"}...],
"error": <str|null>, "source": <str|null>}}}`.

When a variable's list resolves, `set-var` **enforces** it: a value that isn't
listed is rejected unless you pass `--force` (see `set-var` above). When the
list can't be fetched, any value is accepted.

**Example.**

```bash
$ cremind tools options claude_code
VARIABLE                       VALUE                LABEL
CLAUDE_CODE_MODEL              claude-opus-4-5      Claude Opus 4.5
CLAUDE_CODE_MODEL              claude-sonnet-4-5    Claude Sonnet 4.5
CLAUDE_CODE_MODEL              sonnet               sonnet (alias)
CLAUDE_CODE_PERMISSION_MODE    bypassPermissions    bypassPermissions (fully autonomous)
CLAUDE_CODE_PERMISSION_MODE    acceptEdits          acceptEdits (auto-approve file edits)
CLAUDE_CODE_PERMISSION_MODE    plan                 plan (read-only planning, no changes)

# Pick one and apply it
$ cremind tools set-var claude_code CLAUDE_CODE_MODEL=claude-sonnet-4-5
```

### `cremind tools coding-agents`

**Purpose.** Answer "is Claude Code installed?", "is Codex signed in?" and
"which credential is each coding agent using?" in one command. Three separate
things have to line up before the assistant can delegate coding work, and this
is the only place that shows all three side by side:

1. the **SDK is installed** on the server (the `claude_code` / `codex` feature —
   the SDK wheel bundles the CLI binary, so installing the feature *is*
   installing the Claude Code / Codex CLI; there is no separate `npm install`);
2. the **tool is enabled** for the active profile;
3. a **credential resolves** for the active profile.

**Syntax.** `coding-agents` is a group that is also a command: run it bare for
the table, or with a subcommand to change the sign-in.

```bash
cremind tools coding-agents [--probe]
cremind tools coding-agents login  <claude_code|codex> [--shared]
cremind tools coding-agents logout <claude_code|codex> [--shared]
```

**Flags** (on the bare form):

| Flag       | Type | Default | Meaning                                                                                     |
|------------|------|---------|---------------------------------------------------------------------------------------------|
| `--probe`  | bool | `false` | Also run each installed agent's live sign-in check (one cheap request per agent — no coding task, no file changes, no token spend for Codex). Bypasses the server's 15-second probe cache, so it is also the right thing to run straight after a sign-in. |

**Behavior.** Renders a five-column table, one row per agent, always in the
order `claude_code`, `codex` — an agent whose feature is not installed still
gets a row, because that is exactly the case worth reporting:

| Column       | Meaning                                                                                     |
|--------------|-----------------------------------------------------------------------------------------------|
| `AGENT`      | `claude_code` or `codex` (the `tool_id`).                                                    |
| `INSTALLED`  | `yes` if the SDK (and its bundled CLI binary) is importable on the server.                   |
| `ENABLED`    | `yes` if the tool is switched on **for the active profile**.                                 |
| `CREDENTIAL` | The resolved credential source, or `none`. See the table below.                              |
| `LOGGED_IN`  | `-` without `--probe` (not checked); otherwise `yes` / `no` / `unknown`.                      |

A one-line human summary per agent — naming the single next step — is written to
stderr under the table, so it stays out of a piped table but is still visible.
With `--json`, returns the raw agent objects (including `feature_key`, `extras`,
`requires_restart_after_install`, `sign_in`, `credential_scope`, `cli_home`,
`account_hint`, `cli_available`, and, with `--probe`, the full probe payload
under `probe`).

`LOGGED_IN unknown` is **not** "logged out": the check ran but could not decide
(a timeout, or the CLI binary was not found). Only `no` means the credential was
rejected.

**Credential sources.** A key set *for the tool* beats a CLI login, and a
profile's own login beats the server's shared one. Claude Code's pasted
long-lived token leads even the key, because the `claude` CLI itself prefers
`CLAUDE_CODE_OAUTH_TOKEN` over `ANTHROPIC_API_KEY` when both are set:

| Value                       | What it means                                                                       |
|-----------------------------|-------------------------------------------------------------------------------------|
| `tool_variable_oauth_token` | Claude Code: the tool's own `CLAUDE_CODE_OAUTH_TOKEN` variable (this profile's) — a long-lived `claude setup-token` token. |
| `tool_variable_api_key`     | The tool's own `CLAUDE_CODE_API_KEY` / `CODEX_API_KEY` variable (this profile's).    |
| `env_anthropic_api_key`     | Claude Code: `ANTHROPIC_API_KEY` in the server's environment.                        |
| `env_oauth_token`           | Claude Code: `CLAUDE_CODE_OAUTH_TOKEN` in the server's environment.                  |
| `env_codex_api_key`         | Codex: `CODEX_API_KEY` in the server's environment.                                  |
| `env_openai_api_key`        | Codex: `OPENAI_API_KEY` in the server's environment.                                 |
| `profile_claude_login`      | Claude Code: **this profile's own** `claude auth login` (`credential_scope: profile`).|
| `profile_codex_login`       | Codex: **this profile's own** `codex login` (`credential_scope: profile`).           |
| `host_claude_login`         | Claude Code: the **shared** server login, inherited (`credential_scope: shared`).     |
| `host_codex_login`          | Codex: the **shared** server login, inherited (`credential_scope: shared`).           |
| `none`                      | Nothing is visible — sign in (see below), or set a key.                              |

Any `env_*` source is a server-wide key shared by every profile; the two
`profile_*` and two `host_*` sources are CLI logins, and only those carry a
`credential_scope`, an `account_hint` (the signed-in email / plan / org) and
something to sign out of.

The profile's credentials under **Settings → LLM Providers** are deliberately
**not** in this list. They are Cremind's own reasoning models' credentials; the
`claude` and `codex` CLIs never read them, and the earlier bridge that reused an
OpenAI "Sign in with ChatGPT" login for Codex (`profile_chatgpt_login`) has been
removed — a profile that relied on it must sign in to Codex itself once.

**Signing in.** The sign-in is the vendor CLI's own (`claude auth login`,
`codex login --device-auth`); Cremind only says which binary to run and which
home the credential goes into. Four doors:

- **No shell access** — **Settings → Tools & Skills → Coding Agents →**
  the agent's card **→ Sign in**. Codex shows a device code and a link to
  confirm in any browser; Claude Code opens a built-in terminal running the real
  `claude auth login` under a PTY. In a container Cremind removes `DISPLAY` from
  that terminal's environment, so the CLI cannot try to open a browser on the
  VNC desktop and prints the URL for you to open elsewhere instead.
- **No browser can reach the server at all** (Claude Code) — `claude auth login`
  has no headless flag, so run `claude setup-token` on any machine that *does*
  have a browser (it needs a Claude subscription) and paste the token it prints
  into the same Sign-in dialog, or set it directly:
  `cremind tools set-var claude_code CLAUDE_CODE_OAUTH_TOKEN=<token>`. That is
  the `tool_variable_oauth_token` source above.
- **A shell on the server** — `cremind tools coding-agents login claude_code`
  (or `codex`). It runs the login in *this* terminal (inheriting stdin/stdout,
  so the printed URL and the pasted code behave exactly as they would outside
  Cremind), then re-probes and prints `Signed in as …`. Inside a container it
  drops the same variables the built-in terminal does — the server names them —
  so a shell sign-in in a pod prints the URL rather than opening a browser on
  the VNC desktop.
- **Signing out** — `cremind tools coding-agents logout claude_code`. A pasted
  token is not a login, so it is cleared by emptying the variable
  (`cremind tools set-var claude_code CLAUDE_CODE_OAUTH_TOKEN=`), not by
  `logout`.

`login` only works **where the CLI actually lives**. `cremind` pointed at a
remote server refuses rather than writing a credential into the wrong machine's
home directory:

```text
The codex CLI lives on the Cremind server (cremind-7c9f4), not on this machine,
so the sign-in has to happen there. Run this same command on that host, or sign
in without a shell: Settings -> Tools & Skills -> Coding Agents -> Sign in does
it from a browser.
```

`logout` has no such limit — the server runs the CLI's own logout against the
home and removes the credential file — so it works from a remote `cremind`.

**Login scope.** A login is **per profile**, written to
`<CREMIND_SYSTEM_DIR>/<profile>/coding-cli/{claude,codex}`. A profile that never
signed in inherits the **server's shared login** (`$CLAUDE_CONFIG_DIR` /
`$CODEX_HOME`, else `~/.claude` / `~/.codex` on a native install, else
`<CREMIND_SYSTEM_DIR>/coding-cli/…` in a container, where it survives an image
upgrade or a pod replacement).

| Flag (on `login` / `logout`) | Type | Default | Meaning |
|------------------------------|------|---------|---------|
| `--shared`                   | bool | `false` | Act on the **server-wide** login every profile without one of its own inherits, instead of this profile's. |

`logout --shared` is **admin-only** (the server refuses it otherwise) and signs
out every profile that was inheriting it. `login --shared` needs no such check
because it needs something stronger: a shell on the server host, where the
operator can write that home anyway. Signing in from the *browser* with the
shared scope is admin-only too.

Signing out a profile that has no login of its own is refused (HTTP 409) rather
than silently signing the whole server out:

```text
This profile has no Codex login of its own - it uses the shared server login in
/root/.cremind/coding-cli/codex. Only an admin can sign that out (scope=shared).
```

Credentials and the enabled flag are **per profile**, so this command reports
the active profile's answer, never the server's.

**Installing a missing agent.** `INSTALLED no` is fixed with
`cremind features install claude_code` (or `codex`) — that pulls the SDK wheel,
CLI binary included, with no restart needed. Then `cremind tools enable
claude_code` to switch it on for the profile.

**Examples.**

```bash
$ cremind tools coding-agents
AGENT        INSTALLED  ENABLED  CREDENTIAL           LOGGED_IN
claude_code  yes        yes      host_claude_login    -
codex        no         no       none                 -
(claude_code: Claude Code is installed, enabled, and using host_claude_login, the shared server login.)
(codex: Codex is not installed on this server. Install the 'codex' feature to add it - no shell access needed.)

# Install the missing one, enable it, sign this profile in, then confirm
$ cremind features install codex
$ cremind tools enable codex
$ cremind tools coding-agents login codex
Running: /opt/venv/bin/codex login --device-auth
...
Signed in as dev@example.com (pro, chatgpt).

$ cremind tools coding-agents --probe
AGENT        INSTALLED  ENABLED  CREDENTIAL           LOGGED_IN
claude_code  yes        yes      host_claude_login    yes
codex        yes        yes      profile_codex_login  yes

# Sign this profile out again (works from a remote cremind too)
$ cremind tools coding-agents logout codex
Signed out of codex (profile).
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

### `cremind tools get-args`

**Purpose.** Show a tool's **arguments schema** and its **current saved
argument values** — the read counterpart to `set-args`.

**Syntax.**

```bash
cremind tools get-args <tool_id>
```

**Behavior.** Prints two JSON blocks: `--- arguments_schema ---` (the shape the
tool accepts) and `--- arguments ---` (the values currently saved for the
profile). With `--json`, returns `{"arguments_schema": ..., "arguments": ...}`.

This is derived from the tool detail (`cremind tools get`) — there is no
dedicated GET-arguments endpoint — so it reflects exactly what `tools get`
reports under `config.arguments`. Built-in tools that declare arguments (e.g.
`exec_shell`, `google_places`) report their real `arguments_schema` here.

**Example.**

```bash
$ cremind tools get-args mcp.shell
--- arguments_schema ---
{ "type": "object", "properties": { "shells": {...}, "timeout_s": {...} } }

--- arguments ---
{ "shells": ["bash", "pwsh"], "timeout_s": 120 }
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
$ cremind --json tools list --type mcp | jq -r '.[] | select(.enabled==false) | .tool_id'
```

### Spin up a skill's daemon and immediately attach to its console

```bash
$ pid=$(cremind --json tools register-long-running skill.daily-brief | jq -r .process_id)
$ cremind proc attach "$pid"
```

## Troubleshooting

**`enable` / `disable` rejected** — Core built-in and intrinsic tools are
always on and cannot be toggled — the server rejects the call. Optional
built-ins (e.g. `weather`, `browser`, `claude_code`) *can* be toggled; if
`enable` returns 409 `FeatureNotInstalled`, install the backing feature first
(`cremind features install <feature>`). Use `cremind tools list` to confirm the
type.

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
