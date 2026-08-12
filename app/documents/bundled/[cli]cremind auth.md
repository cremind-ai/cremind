---
description: "Regenerate, rotate, and revoke a Cremind profile's login token — mint a fresh JWT when a `CREMIND_TOKEN` leaked, expired, or was deleted, instantly invalidating every token issued to that profile before it. Use this when you are locked out of the CLI, need to kill a stolen or compromised token, or want to check whether the current token is still valid and when it expires; the `--local` flag rotates directly against the database and the `tokens/<profile>.token` file on the server host, so recovery works with no working credential at all. Subcommands: `status`, `regenerate`, `show`. Distinct from `cremind me` (read-only identity, cannot detect a revoked token) and `cremind setup` (mints the very first admin token during install)."
---

# `cremind auth` — Token Rotation & Revocation

`cremind auth` manages the JWT a profile uses to authenticate against the
Cremind server — the token stored at
`<CREMIND_SYSTEM_DIR>/tokens/<profile>.token` and injected into every
`exec_shell` subprocess as `CREMIND_TOKEN`.

**Rotation is revocation.** Each profile row carries a `token_serial`
counter that is stamped into every token it issues (the `tsr` claim) and
compared on every decode. `cremind auth regenerate` increments that
counter, so every token previously issued to the profile stops
authenticating the moment the new one is minted. This is the *only* way
to kill an exposed token: simply overwriting the token file mints a
second valid credential and leaves the leaked one working for its full
30-day lifetime.

The blast radius is exactly one profile. Other profiles' tokens are
untouched — unlike rotating the installation's JWT secret, which logs
everyone out at once.

Two execution paths:

- **Authenticated (default)** — a `POST /api/auth/regenerate` call using
  your current token. Use this whenever you still have a working
  credential.
- **`--local`** — talks straight to the database and token file on the
  server host, with no authentication at all. This is the recovery path:
  it works when your token has expired, was deleted, or was already
  revoked, and even when the server is not running.

## Finding this in the web UI

There is no web-UI equivalent, by design: every UI page needs a valid
token to load, so a locked-out user could never reach a "regenerate"
button. Rotation is a shell operation on the server host.

The closest thing is the login screen — after a rotation, every open
browser session for that profile is logged out and returns to
**Profile selector**, where the new token can be pasted in.

## Global flags

`--json` is a **root** flag, so it goes before the group name:

```bash
cremind --json auth status      # correct
cremind auth status --json      # WRONG — "No such option: --json"
```

There are two different `--profile` flags, and they mean different things:

| Flag | Position | Meaning |
| --- | --- | --- |
| `--profile` / `-p` | root, before `auth` | The **actor** — which profile's token authenticates the call. |
| `--profile` | after the subcommand | The **target** — which profile is inspected or rotated. |

So `cremind -p admin auth regenerate --profile bob` means "acting as
admin, rotate bob's token".

`auth show`, and any invocation with `--local`, need no token at all and
never prompt for a profile. Every other invocation resolves a token the
usual way (root `--profile`, this terminal's remembered profile, or the
interactive picker).

## Subcommands

### `cremind auth status`

**Purpose.** Report whether the current token is still valid, what
generation it belongs to, and when it expires. `cremind me` cannot answer
this — it decodes the token's own claims without comparing them to the
server's current serial, so a revoked token still looks fine there.

```bash
cremind auth status [--local] [--profile NAME]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--local` | off | Read the serial from the database instead of calling the server. |
| `--profile NAME` | your own | Profile to report on. Another profile requires admin on the server path. |

**Exit codes.** `0` valid · `1` revoked, expired, or the call failed.
That makes it usable as a scripted liveness gate.

### `cremind auth regenerate`

**Purpose.** Mint a new token and revoke every token issued to the
profile before it. Reach for this the moment a token is exposed.

```bash
cremind auth regenerate [--local] [--profile NAME] [--expires-hours N]
                        [--yes] [--show-token] [--no-write-file]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--local` | off | Rotate directly against the database and token file on this host. Works with no valid token. |
| `--profile NAME` | your own | Profile to rotate. Rotating another profile requires admin on the server path; `--local` has no such gate. |
| `--expires-hours N` | server setting (720) | Token lifetime, 1–8760 hours. |
| `--yes` / `-y` | off | Skip the confirmation prompt. **Required** when stdin is not a terminal. |
| `--show-token` | off | Print the JWT itself. By default only the token-file path is shown. |
| `--no-write-file` | off | Don't update this host's `tokens/<profile>.token`. |

**What gets invalidated.** Every token issued to the profile before this
call: other terminals, open web-UI sessions, running agent shells, A2A
and MCP clients. Under a multi-replica deployment the change propagates
within about five seconds.

**What keeps working.** Newly spawned `exec_shell` subprocesses, which
read the token file at spawn time and therefore pick up the new token
automatically. Other profiles are entirely unaffected.

**Why the token isn't printed by default.** Agents run `cremind` through
`exec_shell`, so anything on stdout lands in shell scrollback, CI logs,
and conversation history. The token file is updated for you; pass
`--show-token` when you genuinely need the string.

### `cremind auth show`

**Purpose.** Print this host's stored token for a profile. Never contacts
the server and never needs a token, so it still works after a rotation
has invalidated the current shell's `CREMIND_TOKEN`.

```bash
cremind auth show [--profile NAME] [--path]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--profile NAME` | your own | Profile whose token file to read. |
| `--path` | off | Print the file's path instead of its contents. |

## The `--local` recovery path

Reach for `--local` when:

- your token has expired (default lifetime is 30 days),
- the token file was deleted or corrupted,
- the token was revoked and you have no replacement,
- the server isn't running.

It needs a shell on the server host and a readable database — nothing
else. It **has no authorization gate**: anyone who can run it can rotate
any profile, including `admin`. That is inherent rather than an
oversight, since the same shell access already allows reading every token
file directly. Do not treat `--local` as an authenticated path, and do
not expose the host shell to users who shouldn't hold every profile's
credentials.

Safe to run while the Cremind server is up, on both SQLite (WAL mode; the
write is a single row) and PostgreSQL. It deliberately does **not** run
migrations — a recovery command shouldn't quietly alter the schema of a
live install.

## Driving this non-interactively (agents / scripts)

`typer.confirm` aborts immediately when stdin is closed, which it is
under `exec_shell`. **Always pass `--yes`** from an agent or a script:

```bash
cremind auth regenerate --yes
```

After rotating, the `CREMIND_TOKEN` already exported in the current shell
is a *revoked* token, and it takes precedence over the token file. Every
later `cremind` call in that same shell will fail with 401 even though
the file on disk is perfectly good. Re-export it:

```bash
export CREMIND_TOKEN=$(cremind auth show)
```

New subprocesses are unaffected — they read the file fresh.

## Worked examples

### Check, then rotate

```bash
$ cremind auth status
profile          admin
valid            yes
token_serial     0
current_serial   0
expires_at       2026-09-11T04:22:07+00:00
token_file       /home/lee/.cremind/tokens/admin.token

$ cremind auth regenerate --yes
profile      admin
serial       1
expires_at   2026-09-11T18:40:12.559123+00:00
token_file   /home/lee/.cremind/tokens/admin.token

Every token issued to this profile before now is revoked.
```

### Locked out — recover on the host

```bash
$ cremind auth status
your token is expired or already revoked, so it can't authorize its own
rotation — run `cremind auth regenerate --local` on the server host instead.

$ cremind auth regenerate --local --yes
profile      admin
serial       2
expires_at   2026-09-11T18:41:55.204418+00:00
token_file   /home/lee/.cremind/tokens/admin.token
```

### Rotate a teammate's leaked token (admin)

```bash
$ cremind -p admin auth regenerate --profile bob --yes
profile      bob
serial       1
expires_at   2026-09-11T18:43:02.881204+00:00
token_file   /home/lee/.cremind/tokens/bob.token
```

### A short-lived token for CI

```bash
$ cremind --json auth regenerate --expires-hours 1 --show-token --yes
{
  "profile": "admin",
  "expires_at": "2026-08-12T19:41:03.118266+00:00",
  "serial": 3,
  "token_file": "/home/lee/.cremind/tokens/admin.token",
  "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
  "local_token_file": "/home/lee/.cremind/tokens/admin.token"
}
```

## Troubleshooting

**`401` on `regenerate`** — your token is already expired or revoked, so
it can't authorize its own rotation. Run
`cremind auth regenerate --local` on the server host.

**`403 admin_required`** — only the `admin` profile may rotate another
profile's token over the API. Use `--local` on the host, or rotate as
admin.

**`no token file for profile 'x'`** — nothing has been minted on this
host for that profile. `cremind auth regenerate --local --profile x`.

**`database is locked`** — SQLite, and the server is mid-write. Retry in
a moment. (A SQLite database on an NFS/SMB share is not safe for
concurrent writers at all, independent of this command.)

**`no such column: token_serial`** — the database predates the
revocation column. Start the Cremind server once so it migrates on boot,
or run `cremind db upgrade` with the service stopped.

**`bootstrap.toml is missing`** — no database is configured on this host.
Run `cremind setup` first.

**`--local` fails to import the storage layer** — on a PostgreSQL
install the driver extra may be missing: `pip install 'cremind[postgres]'`.

**The web UI logged me out after rotating** — expected. The browser holds
a token from the previous generation. Log in again with the new one
(`cremind auth show`).

**Everything 401s in this shell after rotating** — `CREMIND_TOKEN` is
exported and holds the old token. See *Driving this non-interactively*
above.

## Related

- `cremind me` — read-only identity for the current token. Cannot detect
  revocation; use `cremind auth status` for that.
- `cremind profile` — profile lifecycle and choosing which profile the
  CLI acts as.
- `cremind setup` — mints the very first token for a profile during
  install.
- `app/api/auth.py` — the API these commands wrap;
  `app/auth/` — the serial and verification logic behind it.
