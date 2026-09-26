---
description: "Show the **identity** of the current CLI session: decode the active `CREMIND_TOKEN` JWT and print the profile, subject, issued-at and expires-at times, the server's system directory, and this profile's own working directory (each profile has its own). Use this to confirm which profile you are acting as, check whether the token has expired, or find the working directory your files and tools use — a read-only probe that changes nothing."
---

# `cremind me` — Identity Info for the Current Token

`cremind me` is the simplest authenticated command in the CLI. It asks the
server to decode the token in `CREMIND_TOKEN` and report what that token
authorizes: which profile it grants, when it expires, and which working
directory the agent operates from. Because the rest of the CLI silently
acts on the active profile resolved from this token, `cremind me` is the
fastest way to confirm "who am I to the server right now?".

This command takes no arguments and has no subcommands.

## Finding this in the web UI

There is no dedicated page for the `me` payload, but the same identity
information is shown in the **header / profile badge** of the Cremind web
UI: the active profile name appears top-right, and hovering it surfaces
the subject and expiry. If you only need to confirm the active profile,
the badge is faster; if you need machine-readable claims (timestamps,
working directories), use `cremind --json me`.

## Global flags

`cremind me` accepts the root-level `--json` flag to emit the raw token
payload as JSON instead of a human-readable key/value table:

```bash
cremind --json me
```

It also obeys the standard CLI environment variables — most importantly
`CREMIND_TOKEN` (required) and `CREMIND_SERVER` (default
`http://localhost:1112`).

## Behavior

`cremind me` calls the server's identity endpoint with the bearer token and
prints back the decoded claims. It does **not** mutate any state and is
safe to run as often as needed.

In the default (table) view the output is a fixed two-column key/value
list with these rows:

| Row                | Meaning                                                                                       |
|--------------------|-----------------------------------------------------------------------------------------------|
| `profile`          | Profile name the token grants. All other CLI commands act on this profile.                    |
| `subject`          | JWT `sub` claim — the principal id (typically the same as the profile, but server-controlled).|
| `issued_at`        | RFC 3339 timestamp + Unix seconds, e.g. `2026-05-02T14:00:00Z (1746201600)`.                  |
| `expires_at`       | RFC 3339 timestamp + Unix seconds. After this moment, every authenticated command will fail.  |
| `system_dir`       | Cremind's system directory on the server (`CREMIND_SYSTEM_DIR`): database, tokens, per-profile state. |
| `user_working_dir` | **This profile's own working directory** — its file panel, the default directory of its tools and terminals, `$CREMIND_USER_WORKING_DIR` in its shells, what its Documentation search indexes. Every profile has its own; no other profile can reach it. |
| `user_working_dir_default` | Whether that folder is the profile's default, `<workspaces>/<profile>` (`yes`/`no`). Omitted by a server that does not report it. |

With `--json`, the output is the same rows as a JSON object (keys
`subject`, `profile`, `issued_at`, `expires_at`, `system_dir`,
`user_working_dir`, and `user_working_dir_default` when reported),
suitable for piping into `jq`. Only `admin` can change a profile's working
directory — see `cremind profile working-dir`.

## Examples

### Confirm the active profile

```bash
$ cremind me
profile:                   li
subject:                   li
issued_at:                 2026-05-02T14:00:00+00:00 (1746201600)
expires_at:                2026-06-01T14:00:00+00:00 (1748793600)
system_dir:                /home/li/.cremind
user_working_dir:          /home/li/.cremind/workspaces/li
user_working_dir_default:  yes
```

### Find your working directory in a script

```bash
$ cremind --json me | jq -r .user_working_dir
/home/li/.cremind/workspaces/li
```

### Read just the profile name in a script

```bash
$ cremind --json me | jq -r .profile
admin
```

### Confirm the token is still valid

```bash
$ cremind --json me | jq -r '.expires_at | todate'
2026-06-01T14:00:00Z
```

## Troubleshooting

**`no Cremind profile selected and no token available`** — Nothing resolved a
token. On an interactive terminal the CLI normally prompts you to pick a
profile; in a non-interactive shell with several profiles, pass
`--profile <name>` (or `export CREMIND_TOKEN=<jwt>`). If no profiles exist
yet, run `cremind setup complete` to mint the first one. See
`cremind profile` for how selection works.

**`401 Unauthorized` / `token expired`** — The token has passed its
`expires_at` timestamp. Obtain a fresh token (re-run setup, or ask your
admin) and re-export it.

**Wrong profile shown** — Profiles are baked into the token. If `cremind me`
reports a profile you did not expect, you are using the wrong token.
List available profiles with `cremind profile list` (under a token that has
permission), then export the token for the profile you want.
