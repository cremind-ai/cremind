---
description: "Reference for the `cremind system-vars` CLI command — a one-shot read-only query that asks the Cremind server which environment variables it injects into every `exec_shell` subprocess (`CREMIND_SERVER`, `CREMIND_TOKEN`, `CREMIND_SYSTEM_WORKING_DIR`, `CREMIND_USER_WORKING_DIR`, `CREMIND_SKILL_DIR`, plus any extensions registered later) and prints each one's name, currently-resolved value for the caller's profile, and description. Useful for discovering which env vars are available inside agent-spawned shells, confirming what an `exec_shell` run will actually see, and verifying that the server-side registry is in sync with what the CLI expects."
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
list as JSON instead of a human-readable table:

```bash
cremind system-vars --json
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

## Examples

### List all system variables

```bash
$ cremind system-vars
┌───────────────────────────┬───────────────────────────────────┬────────────────────────────────────────────────────────┐
│ NAME                      │ VALUE                             │ DESCRIPTION                                            │
├───────────────────────────┼───────────────────────────────────┼────────────────────────────────────────────────────────┤
│ CREMIND_SYSTEM_WORKING_DIR │ /home/li/.cremind                  │ Cremind internal working directory (~/.cremind).         │
│ CREMIND_USER_WORKING_DIR   │ /home/li/Documents                │ User-facing default working directory.                 │
│ CREMIND_SKILL_DIR          │ /home/li/.cremind/admin/skills     │ Per-profile skills directory; omitted when no profile. │
│ CREMIND_SERVER             │ http://127.0.0.1:1112             │ Loopback URL of this server for the `cremind` CLI.         │
│ CREMIND_TOKEN              │ eyJhbGciOi…                       │ Per-profile OPA token; omitted when missing.           │
└───────────────────────────┴───────────────────────────────────┴────────────────────────────────────────────────────────┘
```

### Read just the value of one variable

```bash
$ cremind system-vars --json | jq -r '.[] | select(.name == "CREMIND_SKILL_DIR") | .value'
/home/li/.cremind/admin/skills
```

### Confirm a specific variable is registered

```bash
$ cremind system-vars --json | jq -e '.[] | select(.name == "CREMIND_SKILL_DIR")'
```

Exits 0 when the variable is registered, 1 otherwise — handy in CI
checks that depend on the registry shape.

## Troubleshooting

**`CREMIND_TOKEN is required`** — The command is authenticated. Export a
JWT (`export CREMIND_TOKEN=<jwt>`) before running it.

**`401 Unauthorized`** — The token has expired or does not match the
running server. Re-mint via `cremind setup complete` or ask your admin.

**Variable I expected is missing** — The registry is the file
`app/config/system_vars.py` on the server. If something is missing,
either it was never added, or the server was started before it was
added — restart the server.
