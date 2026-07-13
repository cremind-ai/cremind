---
description: "Operate the running Cremind server with `cremind server`: `restart` the backend process (with an install-mode-aware confirmation), probe `health`, read the server's build `version` and release channel, and show tray/install `capabilities`. Distinct from the local `cremind version` command — `server version` reports what the connected server is running, not the installed CLI. The reads need no token; `restart` is admin-only."
---

# `cremind server` — Server Operations

`cremind server` controls and inspects the *running* Cremind backend — the
operational surface of the web UI's **Developer** page. It complements
`cremind serve` (which starts a server in-process) and the root
`cremind version` (which prints the *locally installed* package version).

The three read commands (`health`, `version`, `capabilities`) hit
unauthenticated endpoints, so they work without a token — handy for probing a
server before login or against a remote `--server`. `restart` is **admin-only**.

## Finding this in the web UI

> **Sidebar → Developer → Restart Server**

The restart control, its install-mode caveats, and the health/version probes
that back the update banner all live on that page.

## Global flags

All subcommands accept the root-level `--json` flag.

## Subcommands

### `cremind server health`

**Purpose.** Probe `/health` and report each subsystem's state.

**Syntax.**

```bash
cremind server health
```

**Behavior.** Prints `status`, `db`, and `vectorstore`. A `disabled` vector
store is healthy, not an error. Exits **non-zero** when the server reports a
degraded subsystem (HTTP 503), so it's usable as a scripted liveness gate. No
token required.

**Example.**

```bash
$ cremind server health
status:       ok
db:           ok
vectorstore:  disabled
```

### `cremind server version`

**Purpose.** Show the *connected server's* build version and release channel.

**Syntax.**

```bash
cremind server version
```

**Behavior.** Prints `backend` (SemVer), `schema` (Alembic head), `channel`
(`production`/`test`/`dev`), and `min_supported_upgrade_from`. No token
required.

> **Not the same as `cremind version`.** The root command prints the version of
> the CLI package installed locally; `server version` reports what the server
> you're talking to is actually running. They can differ.

**Example.**

```bash
$ cremind server version
backend:                     0.3.1
schema:                      20260627_llm_messages
channel:                     production
min_supported_upgrade_from:  0.1.0
```

### `cremind server capabilities`

**Purpose.** Show the server's install mode and the UI features it exposes.

**Syntax.**

```bash
cremind server capabilities
```

**Behavior.** Reads the public tray-capabilities endpoint: `install_mode`
(`docker`/`electron`/`native`) and `ui_features`. No token required. (The
richer admin `/api/services/capabilities` used by the Setup Wizard is
intentionally not wrapped here.)

**Example.**

```bash
$ cremind server capabilities
install_mode:  docker
ui_features:   processes, events, channels
```

### `cremind server restart`

**Purpose.** Restart the backend process (admin).

**Syntax.**

```bash
cremind server restart [--yes/-y]
```

**Flags.**

| Flag           | Type | Default | Meaning                        |
|----------------|------|---------|--------------------------------|
| `--yes`, `-y`  | bool | `false` | Skip the confirmation prompt.  |

**Behavior.** Active HTTP, SSE, and chat connections drop while the server is
unavailable. Whether it comes back on its own depends on the install mode,
which the command reads first and warns about before confirming:

- **docker** — the container restarts automatically (usually 5–15s).
- **electron** — Cremind relaunches the backend automatically.
- **native / unknown** — **no supervisor**: the backend stays DOWN and you must
  relaunch `cremind serve` manually.

Unless `--yes` is given, the caveat prints to stderr and the command asks for
confirmation. On success it prints `restarting (pid <pid>)`. Requires an admin
token.

**Example.**

```bash
$ cremind server restart
Docker install — the container will restart automatically (usually 5-15 seconds).
Restart the Cremind server now? [y/N]: y
restarting (pid 12841)
```

## Troubleshooting

**`server restart` says the backend will stay DOWN** — You're on a `native`
install with no supervisor. After restarting you must run `cremind serve`
again. Use Docker or Electron for auto-restart.

**`server health` exits non-zero** — A subsystem is degraded (HTTP 503). Run it
again or check `cremind logs tail --level error` for the cause. A `disabled`
vector store is *not* a failure.

**A read command works without a token but `restart` fails with 403** — That's
expected: the reads are public; `restart` needs an admin token.
