---
description: "Inspect and control the vector-embedding subsystem with `cremind embedding` (admin) — the semantic search behind Cremind documentation search, Documentation search (the user's own files) and memory recall: read live `status`, `get` or `set` the persisted embedding config (provider + vector store), kick off an `initialize`/rebuild, and `--follow` the load progress over SSE. Setting a provider whose optional extras aren't installed returns a FeatureNotInstalled error listing the missing keys — install them with `cremind features install` first. On an Intel Mac or Windows on ARM, Vector Embedding can't be turned on at all: PyTorch publishes no build there."
---

# `cremind embedding` — Vector Embedding Subsystem

`cremind embedding` inspects and controls Cremind's vector-embedding subsystem
— the semantic-search backend behind Cremind documentation search
(`cremind_documentation_search`, Cremind's own manuals), Documentation search
(`documentation_search`, the user's own files — `cremind docs`) and memory
recall. It mirrors the admin-only **Embedding** settings page. Turning it off
degrades those features rather than disabling them:
`cremind_documentation_search` hands its relevance judge the whole shared
library plus up to 50 of the profile's own documents instead of a
vector-ranked shortlist; Documentation search keeps its index and answers by
keyword only (`mode: lexical_only`) but stops syncing
(`suspended(embedding_off)`), and an admin cannot allow it until embedding is
back on (Settings → My Documents is hidden while it is off); long-term memory
search returns the stored facts unranked.

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

All subcommands accept the root-level `--json` flag. It goes right after `cremind`, before the command group (`cremind --json embedding status`). A trailing `--json` is rejected — except on `set`, where a `--json` after the subcommand is that command's own JSON-payload option, not the output switch.

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
`cremind features install …` command to run first — unless this computer can't
install them at all, in which case it returns an error saying why (see
Troubleshooting). Requires an admin token.

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

**`set` says "Vector Embedding runs on PyTorch, which publishes no builds for
…"** — Both embedding models need PyTorch, and PyTorch has no build for an
Intel Mac on Python 3.13+ (Cremind's Python) or for Windows on ARM, so pip has
nothing to install. Vector Embedding can't be enabled on that computer; the
Setup Wizard and Settings → Embedding grey the switch out for the same reason.
On an Intel Mac, a Docker install of Cremind (Linux x86_64 inside) can run it.

**`set` returns 409 "currently … please wait"** — A rebuild is in progress.
Watch `cremind embedding status --follow` and retry once it's `ready`.

**`get`/`set` return 403 but `status` works** — `get`/`set` are admin-only;
`status` and `initialize` are not. Use an admin `CREMIND_TOKEN`.

**Windows: `error` says PyTorch needs the Microsoft Visual C++ runtime (older
builds said `[WinError 1114] … torch\lib\c10.dll`)** — PyTorch needs the
Microsoft Visual C++ runtime 14.40 or newer. When the PC's runtime is older,
Cremind installs a private copy into its own venv (no admin needed) and loads
it before PyTorch, so this error means that wasn't possible (offline, for
example). Install the latest Microsoft Visual C++ Redistributable from
https://aka.ms/vc14/vc_redist.x64.exe, then `cremind server restart`.
`cremind embedding initialize` alone won't help: the running server keeps the
old runtime loaded.
