---
description: "List the **environment variables** the Cremind server injects into every `exec_shell` subprocess (and every built-in terminal and autostart command) — `CREMIND_SYSTEM_DIR`, `CREMIND_INSTALL_DIR`, `CREMIND_USER_WORKING_DIR`, `CREMIND_SKILL_DIR`, `CREMIND_SERVER`, `CREMIND_PROFILE`, `CREMIND_AGENT_NAME`, `CREMIND_TOKEN`, `CREMIND_OAUTH_REDIRECT_URI`, plus `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, which point the `claude` and `codex` coding CLIs at this profile's own login home under the Cremind System Directory so a sign-in in a Cremind shell never overwrites the server's shared login — with each one's resolved value and description. Use this to discover what env vars an agent-spawned shell will see; a read-only query, distinct from `cremind me` (which decodes the token identity)."
---

# `cremind system-vars` — List Env Vars Injected Into Shells

`cremind system-vars` is a thin client over the server's system-variables
registry. Every command the Cremind agent runs through the built-in
`exec_shell` tool inherits a small block of env vars set by the server
(loopback URL, profile token, working-directory sentinels). This
command lists what is in that block today, so you can write skills and
shell scripts that reference those names without guessing.

This command takes no arguments and has no subcommands.

## Finding this in the web UI

There is no dedicated page for the system-variables registry in the
Cremind web UI. The list is intentionally small and grows only when the
server-side registry at `app/config/system_vars.py` is extended, so the
CLI is the canonical place to browse it.

## Global flags

`cremind system-vars` accepts the root-level `--json` flag to emit the raw
list as JSON instead of a human-readable table. Because it is a **root** flag it
goes *before* the command name — `cremind system-vars --json` is rejected with
`No such option: --json`:

```bash
cremind --json system-vars
```

It also obeys the standard CLI environment variables — most importantly
`CREMIND_TOKEN` (required) and `CREMIND_SERVER` (default
`http://localhost:1112`).

## Behavior

`cremind system-vars` performs a single authenticated `GET /api/system-vars`
and prints the response. The server resolves each variable's value for
the caller's profile (taken from the JWT), so the output is exactly
what an `exec_shell` invocation would see.

`CREMIND_TOKEN`'s value is the same JWT the caller used to authenticate,
so echoing it back is not a privacy escalation. Variables whose
resolver returns nothing (e.g. `CREMIND_SKILL_DIR` when the profile has
no skills directory yet) appear with an empty value cell.

In the default (table) view the output has three columns:

| Column        | Meaning                                                                       |
|---------------|-------------------------------------------------------------------------------|
| `NAME`        | The exact env-var name as it appears inside an `exec_shell` subprocess.       |
| `VALUE`       | The resolved value for the caller's profile, or empty when omitted.           |
| `DESCRIPTION` | A short, server-supplied description of what the variable holds.              |

With `--json`, the output is the raw JSON array emitted by the
endpoint, suitable for piping into `jq`.

## The variables

What the registry ships today. The list is authoritative on the server, not
here — run the command for the values your profile actually gets.

| Name                        | Holds                                                                                                   |
|-----------------------------|-----------------------------------------------------------------------------------------------------------|
| `CREMIND_SYSTEM_DIR`        | The Cremind System Directory (`~/.cremind`) — runtime state and user-content root.                        |
| `CREMIND_INSTALL_DIR`       | The Install Directory — install-time scratch (compose bundle, `install.log`, caches).                     |
| `CREMIND_USER_WORKING_DIR`  | The user-facing default working directory.                                                                |
| `CREMIND_SKILL_DIR`         | This profile's skills directory; omitted when there is no profile.                                        |
| `CREMIND_SERVER`            | Loopback URL of this server, so a `cremind` CLI call in the shell needs no `--server`.                    |
| `CREMIND_PROFILE`           | The active profile name; omitted when none is set.                                                        |
| `CREMIND_AGENT_NAME`        | The agent's display name for this profile; omitted when there is no profile.                              |
| `CREMIND_TOKEN`             | This profile's Cremind token, so a `cremind` call needs no `--token`; omitted when missing.               |
| `CREMIND_OAUTH_REDIRECT_URI`| Browser-facing Google OAuth redirect for the Google skills; omitted for a non-loopback `APP_URL`.         |
| `CLAUDE_CONFIG_DIR`         | This profile's **own** Claude Code CLI home (see below); omitted when there is no profile.                |
| `CODEX_HOME`                | This profile's **own** Codex CLI home (see below); omitted when there is no profile.                      |

### `CLAUDE_CONFIG_DIR` and `CODEX_HOME` — the coding CLIs' per-profile homes

`claude` and `codex` keep their login in a directory on disk, named by these two
variables. Cremind points every shell it spawns — `exec_shell`, the built-in
terminals, autostart commands — at **this profile's own** home:

```text
<CREMIND_SYSTEM_DIR>/<profile>/coding-cli/claude   # CLAUDE_CONFIG_DIR
<CREMIND_SYSTEM_DIR>/<profile>/coding-cli/codex    # CODEX_HOME
```

Deliberately the profile's own directory and never the *resolved* one: running
`claude auth login` or `codex login` in a Cremind shell must sign **that
profile** in, not overwrite the server operator's account for everyone. The
fallback still exists where it belongs — the Claude Code and Codex *tools* read
the server's shared login when the profile has none of its own — so a fresh
profile can use the coding delegates before it ever signs in. See
`cremind tools coding-agents`, and the `[tool]claude code` / `[tool]codex`
references, for the full credential order and how to sign in or out.

Both are omitted when the call has no profile, and both are set from the server's
registry rather than inherited, so their value in a Cremind shell may differ from
the same variable in the operator's own login shell (in a container the Docker
image and the Helm chart set the *shared* homes to
`<CREMIND_SYSTEM_DIR>/coding-cli/...`, which survives an image upgrade or a pod
replacement).

## Examples

### List all system variables

```bash
$ cremind system-vars
┌──────────────────────────┬───────────────────────────────────────────┬────────────────────────────────────────────────────────────────────────────┐
│ NAME                     │ VALUE                                     │ DESCRIPTION                                                                │
├──────────────────────────┼───────────────────────────────────────────┼────────────────────────────────────────────────────────────────────────────┤
│ CREMIND_SYSTEM_DIR       │ /home/li/.cremind                         │ Cremind System Directory (~/.cremind) - runtime state + user content root. │
│ CREMIND_USER_WORKING_DIR │ /home/li/Documents                        │ User-facing default working directory.                                     │
│ CREMIND_SKILL_DIR        │ /home/li/.cremind/admin/skills            │ Per-profile skills directory; omitted when no profile.                     │
│ CREMIND_SERVER           │ http://127.0.0.1:1112                     │ Loopback URL of this server for the `cremind` CLI.                         │
│ CREMIND_PROFILE          │ admin                                     │ Active profile name; omitted when no profile is set.                       │
│ CREMIND_TOKEN            │ eyJhbGciOi...                             │ Per-profile Cremind token; omitted when missing.                           │
│ CLAUDE_CONFIG_DIR        │ /home/li/.cremind/admin/coding-cli/claude │ Per-profile Claude Code CLI home.                                          │
│ CODEX_HOME               │ /home/li/.cremind/admin/coding-cli/codex  │ Per-profile Codex CLI home.                                                │
└──────────────────────────┴───────────────────────────────────────────┴────────────────────────────────────────────────────────────────────────────┘
```

### Read just the value of one variable

```bash
$ cremind --json system-vars | jq -r '.[] | select(.name == "CREMIND_SKILL_DIR") | .value'
/home/li/.cremind/admin/skills
```

### Confirm a specific variable is registered

```bash
$ cremind --json system-vars | jq -e '.[] | select(.name == "CREMIND_SKILL_DIR")'
```

Exits 0 when the variable is registered, 1 otherwise — handy in CI
checks that depend on the registry shape.

## Troubleshooting

**`no Cremind profile selected and no token available`** — The command is
authenticated but no token resolved. Pick a profile interactively, pass
`--profile <name>`, or `export CREMIND_TOKEN=<jwt>` before running it (see
`cremind profile`).

**`401 Unauthorized`** — The token has expired or does not match the
running server. Re-mint via `cremind setup complete` or ask your admin.

**Variable I expected is missing** — The registry is the file
`app/config/system_vars.py` on the server. If something is missing,
either it was never added, or the server was started before it was
added — restart the server.
