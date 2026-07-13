---
description: "Inspect and control the vector-embedding subsystem with `cremind embedding` (admin): read live `status`, `get` or `set` the persisted embedding config (provider + vector store), kick off an `initialize`/rebuild, and `--follow` the load progress over SSE. Setting a provider whose optional extras aren't installed returns a FeatureNotInstalled error listing the missing keys — install them with `cremind features install` first."
---

# `cremind embedding` — Vector Embedding Subsystem

`cremind embedding` inspects and controls Cremind's vector-embedding subsystem
— the semantic-search backend behind `documentation_search` and memory recall.
It mirrors the admin-only **Embedding** settings page.

The `get` and `set` operations are admin-only. `status` and `initialize` back
the Setup Wizard's pre-token polling, so they don't require a token; `get`/`set`
do.

## Finding this in the web UI

> **Sidebar → Settings → Embedding** (admin only)

The status panel, the provider/vector-store form, and the "Initialize / Rebuild"
button map to `status`, `get`/`set`, and `initialize` respectively.

## Streaming output format

`status --follow` and `initialize --follow` tail the embedding state stream
(SSE). Each frame prints as `[<type>] <raw JSON>` (or the raw JSON with
`--json`); press Ctrl-C to stop.

## Global flags

All subcommands accept the root-level `--json` flag.

## Subcommands

### `cremind embedding status`

**Purpose.** Show the subsystem's current state (`enabled`, `status`, `ready`,
`busy`, `error`, …).

**Syntax.**

```bash
cremind embedding status [--follow/-f]
```

**Behavior.** One-shot by default. With `--follow`, tails the live state stream
until Ctrl-C. No token required.

**Example.**

```bash
$ cremind embedding status
busy:     False
enabled:  True
error:    None
ready:    True
status:   ready
```

### `cremind embedding get`

**Purpose.** Print the persisted embedding config (admin).

**Syntax.**

```bash
cremind embedding get
```

**Behavior.** Pretty-prints the stored config plus current state. Requires an
admin token.

### `cremind embedding set`

**Purpose.** Persist a new embedding config and trigger a reload/rebuild.

**Syntax.**

```bash
cremind embedding set --json '<config>'
cremind embedding set --file <path>
```

**Flags.**

| Flag       | Type   | Default | Meaning                                                          |
|------------|--------|---------|------------------------------------------------------------------|
| `--json`   | string | (none)  | Embedding config as a JSON object.                               |
| `--file`   | string | (none)  | Path to a JSON file with the config (avoids shell-quoting pain). |

`--json` and `--file` are mutually exclusive; exactly one is required. The body
mirrors the wizard's `embedding_config`, e.g.
`{"enabled": true, "provider": "me5", "vectorstore": {...}}`.

**Behavior.** On success the new state is printed and a rebuild runs in the
background (watch it with `cremind embedding status --follow`). If the chosen
provider's extras aren't installed, the server returns **FeatureNotInstalled**
and the command prints the missing feature keys plus the exact
`cremind features install …` command to run first. Requires an admin token.

**Example.**

```bash
$ cremind embedding set --file embedding.json
# → FeatureNotInstalled path:
Vector Embedding requires the following optional dependencies... : embedding.me5
Install them first: cremind features install embedding.me5
```

### `cremind embedding initialize`

**Purpose.** Trigger an asynchronous load + rebuild of the subsystem.

**Syntax.**

```bash
cremind embedding initialize [--follow/-f]
```

**Behavior.** Kicks off the rebuild (a no-op if embedding is disabled or already
busy/ready) and prints the resulting state. With `--follow`, then tails progress
until Ctrl-C.

## Troubleshooting

**`FeatureNotInstalled` on `set`** — The provider needs optional extras. Run the
printed `cremind features install <key>`, then (if it reported `RESTART_AFTER`)
`cremind server restart`, then re-run `embedding set`.

**`set` returns 409 "currently … please wait"** — A rebuild is in progress.
Watch `cremind embedding status --follow` and retry once it's `ready`.

**`get`/`set` return 403 but `status` works** — `get`/`set` are admin-only;
`status` and `initialize` are not. Use an admin `CREMIND_TOKEN`.
