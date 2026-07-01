---
description: "Bootstrap a fresh Cremind server headlessly — the CLI equivalent of the first-run wizard: check setup `status`, mint the very first admin JWT (`setup complete`), `reset-orphaned` a half-finished install, `reconfigure` to re-run the wizard, and read or write server-wide non-secret config (`server-config get/set`). Use this for initial install, obtaining the first admin token, and server-level configuration — distinct from `cremind profile` (creating profiles afterward)."
---

# `cremind setup` — First-Run Setup and Server Configuration

`cremind setup` is the CLI entry point for bootstrapping a fresh Cremind
server, recovering a half-finished setup, and editing server-wide
configuration after the fact. It exposes the same actions that the
**Setup Wizard** in the web UI walks an admin through, but in a form
that can be scripted into infrastructure automation.

The group has two halves with different auth requirements:

- **Unauthenticated bootstrap** — `status`, `complete`, `reset-orphaned`.
  These do **not** require `CREMIND_TOKEN` because they are how you obtain
  one in the first place.
- **Admin-authenticated maintenance** — `reconfigure`,
  `server-config get`, `server-config set`. These need a valid
  `CREMIND_TOKEN` from an admin profile.

A typical first-run flow is: `cremind setup status` → `cremind setup complete`
(which prints a JWT) → `export CREMIND_TOKEN=...` → use the rest of the CLI.

## Finding this in the web UI

The unauthenticated subcommands (`status`, `complete`,
`reset-orphaned`) correspond to the **Setup Wizard** that the web UI
shows on first load when no profiles exist:

> **Web UI → Setup Wizard** (auto-displayed before login)

The admin-authenticated subcommands map to the admin settings page:

> **Sidebar → Settings → Admin → Server config**

That page shows non-secret server settings as a key/value editor, and
exposes a "Reset setup" action that mirrors `cremind setup reconfigure`.

## Global flags and auth

All `cremind setup` subcommands respect the root-level `--json` flag.

Unlike most CLI commands, `status`, `complete`, and `reset-orphaned`
are deliberately **unauthenticated** — they ignore `CREMIND_TOKEN`. The
remaining subcommands (`reconfigure`, `server-config *`) **require**
`CREMIND_TOKEN` to belong to an admin-capable profile.

`CREMIND_SERVER` (default `http://localhost:1112`) controls which server
the CLI talks to.

## Subcommands

### `cremind setup status`

**Purpose.** Check whether the server has finished its first-run setup,
and optionally whether a particular profile already exists.

**Syntax.**

```bash
cremind setup status [--profile <name>]
```

**Flags.**

| Flag        | Type   | Default | Meaning                                                |
|-------------|--------|---------|--------------------------------------------------------|
| `--profile` | string | `""`    | Also check whether this specific profile is registered.|

**Behavior.** Calls the unauthenticated status endpoint. Prints a
key-value table:

| Row              | Meaning                                                                     |
|------------------|-----------------------------------------------------------------------------|
| `setup_complete` | Whether the server has been bootstrapped at least once.                     |
| `profile_exists` | (Only when `--profile` was passed.) Whether that named profile exists.      |
| `has_profiles`   | (Only when present in the response.) Whether *any* profile exists.          |

With `--json`, returns the raw object exactly as the server emitted it.

**Example.**

```bash
$ cremind setup status --profile admin
setup_complete  true
profile_exists  true
```

### `cremind setup complete`

**Purpose.** POST a setup payload — the JSON the wizard would have
collected — and receive a freshly minted admin JWT. This is the **only
unauthenticated way to mint a token**; every other path requires an
existing token.

**Syntax.**

```bash
cremind setup complete [--profile <name>] (--json '<inline>' | --json-file <path|->)
```

**Flags.**

| Flag           | Type   | Default | Meaning                                                                      |
|----------------|--------|---------|------------------------------------------------------------------------------|
| `--profile`    | string | `""`    | Overrides the `profile` field inside the JSON payload.                       |
| `--json`       | string | `""`    | Setup payload as an inline JSON object.                                      |
| `--json-file`  | string | `""`    | Path to a JSON file containing the payload. Use `-` to read stdin.           |

If neither `--json` nor `--json-file` is given, the payload is read
from **stdin**. `--json` and `--json-file` are mutually exclusive.

**Payload schema.** The body matches the setup wizard one-for-one:

```json
{
  "profile": "admin",
  "server_config": {
    "jwt_secret": "...",
    "user_working_dir": "..."
  },
  "llm_config": {
    "anthropic.api_key": "sk-...",
    "auth_method": "anthropic"
  },
  "tool_configs": {
    "<tool_id>": { "_enabled": "true", "VAR_NAME": "value" }
  },
  "agent_configs": {
    "<tool_id>": {
      "llm_provider": "anthropic",
      "llm_model": "claude-..."
    }
  }
}
```

The first profile must be named `admin`. A `profile` field is required
either inside the JSON or via `--profile`.

**Behavior.** On success, prints a key-value table to stdout containing
`profile`, `expires_at`, and `token`, then writes the recommended
`export CREMIND_TOKEN=...` line to **stderr** so users can copy/paste it.
With `--json`, the full response object is emitted to stdout instead.

**Examples.**

```bash
# Minimal: just the profile
$ echo '{"profile":"admin"}' | cremind setup complete
profile     admin
expires_at  2026-06-01T14:00:00Z
token       eyJhbGciOi...

Export the token to use the CLI:
  export CREMIND_TOKEN=eyJhbGciOi...

# From a file, with a profile override
$ cremind setup complete --profile admin --json-file ./bootstrap.json

# Capture the token directly
$ export CREMIND_TOKEN=$(cremind setup complete --json-file ./bootstrap.json --json | jq -r .token)
```

### `cremind setup reset-orphaned`

**Purpose.** Recover from a partially-completed setup where
`setup_complete` is true but no profiles exist (for example, after
manually wiping the profile table). Clears the flag so `setup complete`
can run again.

**Syntax.**

```bash
cremind setup reset-orphaned
```

**Behavior.** Unauthenticated. Silent on success. The server itself
verifies that no profiles exist — the call is rejected if the database
is in any state other than "orphaned".

**Example.**

```bash
$ cremind setup reset-orphaned
$ cremind setup status
setup_complete  false
```

### `cremind setup reconfigure`

**Purpose.** Reset `setup_complete` from a healthy server so the wizard
runs again on next boot. Used to onboard the server through a fresh
admin payload, e.g. after rotating the JWT secret.

**Syntax.**

```bash
cremind setup reconfigure
```

**Behavior.** **Requires admin auth.** Silent on success. The next
startup of the UI will display the Setup Wizard, and `cremind setup
status` will report `setup_complete=false` until a new `setup complete`
call lands.

**Example.**

```bash
$ cremind setup reconfigure
$ cremind setup status
setup_complete  false
```

### `cremind setup server-config get`

**Purpose.** Read non-secret server-wide settings — the same key/value
pairs accepted by `server_config` in the setup payload.

**Syntax.**

```bash
cremind setup server-config get [<key>]
cremind setup server server-config get [<key>]   # alias
```

**Arguments** (optional):

- `<key>` — When given, prints just that key's value. When omitted,
  prints every key as a key/value table.

**Behavior.** **Requires admin auth.** With `--json`, output is the
full JSON object (or `{<key>: <value>}` for the single-key form).

**Examples.**

```bash
# Everything
$ cremind setup server-config get
user_working_dir   /home/li/work
jwt_issuer         cremind
log_level          info

# A single key (handy for scripts)
$ cremind setup server-config get user_working_dir
/home/li/work
```

### `cremind setup server-config set`

**Purpose.** Write one or more server-wide config keys.

**Syntax.**

```bash
cremind setup server-config set KEY=VALUE [KEY=VALUE...]
```

**Arguments** (at least one required):

- `KEY=VALUE` — Repeatable. Each pair is split on the first `=`. The
  value side may contain further `=` characters.

**Behavior.** **Requires admin auth.** All updates are sent in a single
PATCH; if any one is rejected, none are applied. Silent on success.

**Examples.**

```bash
$ cremind setup server-config set log_level=debug user_working_dir=/srv/cremind
$ cremind setup server-config get log_level
debug
```

## Worked examples

### Headless first-run bootstrap

```bash
# 1. Confirm the server is fresh
$ cremind setup status
setup_complete  false

# 2. POST the wizard payload, capture the token
$ cat > bootstrap.json <<'EOF'
{
  "profile": "admin",
  "server_config": { "user_working_dir": "/srv/cremind" },
  "llm_config":    { "anthropic.api_key": "sk-...", "auth_method": "anthropic" }
}
EOF
$ export CREMIND_TOKEN=$(cremind setup complete --json-file bootstrap.json --json | jq -r .token)

# 3. Verify identity is now usable
$ cremind me
profile  admin
...
```

### Re-run the wizard from an existing admin

```bash
$ cremind setup reconfigure        # invalidates setup_complete
$ unset CREMIND_TOKEN              # no longer needed
$ cremind setup complete --json-file bootstrap.json
```

### Rotate the configured working directory

```bash
$ cremind setup server-config set user_working_dir=/mnt/cremind
$ cremind setup server-config get user_working_dir
/mnt/cremind
```

## Troubleshooting

**`a 'profile' field is required`** — `setup complete` saw neither a
`profile` key in the JSON nor a `--profile` flag. Add one or the
other.

**`--json and --json-file are mutually exclusive`** — Pick one source
for the payload. To read from stdin, omit both flags or pass
`--json-file -`.

**`401 Unauthorized` on `reconfigure` / `server-config`** — These
subcommands require admin auth. Make sure `CREMIND_TOKEN` belongs to an
admin profile (`cremind me` to confirm).

**`setup complete` reports `setup already complete`** — The server has
been bootstrapped before. Either use `cremind profile create` for new
profiles (with an existing admin token), or run `cremind setup reconfigure`
first to allow the wizard to run again.

**`reset-orphaned` rejected** — The recovery path only fires when the
database is genuinely orphaned (`setup_complete=true` but no profiles).
Run `cremind setup status` first to confirm.

**Token printed to stdout but not exported** — `setup complete` cannot
mutate your shell environment. Either copy the printed export line
(emitted to stderr), or capture the token in a subshell:
`export CREMIND_TOKEN=$(cremind setup complete --json-file b.json --json | jq -r .token)`.
