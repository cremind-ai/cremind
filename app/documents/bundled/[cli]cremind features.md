---
description: "List and install Cremind's optional feature extras with `cremind features`: `list` shows each feature's install state; `install` installs one or more, streaming the live pip output over SSE. Covers the vector-embedding models, vector-store backends, and other heavier dependencies kept off the slim `pip install cremind`. A feature marked requires_restart_after_install only activates after `cremind server restart`."
---

# `cremind features` — Optional Feature Extras

Cremind keeps heavier, opt-in dependencies (vector-embedding models,
vector-store backends, some LLM SDKs) out of the slim `pip install cremind` and
exposes them as installable **features**. `cremind features` is the CLI for the
same flow the Setup Wizard and Settings page drive.

## Finding this in the web UI

Feature installs surface wherever an optional dependency is needed — most
visibly the **Embedding** settings page and the "install this to enable…"
dialogs on **Tools & Skills**. Each streams the same pip log this command shows.

## Streaming output format

`features install` keeps an SSE connection open while pip runs. It prints each
pip output line as it arrives, then a final summary. With `--json`, every frame
is emitted as one JSON object per line (`{"event": "log|done|error", "data":
{…}}`) for `jq`.

## Global flags

Both subcommands accept the root-level `--json` flag. `CREMIND_TOKEN` is
required.

## Subcommands

### `cremind features list`

**Purpose.** Show every optional feature and whether it's installed.

**Syntax.**

```bash
cremind features list
```

**Behavior.** Prints a table:

| Column          | Meaning                                                        |
|-----------------|----------------------------------------------------------------|
| `FEATURE`       | Feature id (pass to `features install`).                       |
| `INSTALLED`     | Whether the extras are present.                                |
| `RESTART_AFTER` | Whether activating it needs a `cremind server restart`.        |
| `EXTRAS`        | The pip extras the feature maps to.                            |

With `--json`, returns the raw feature-id → state map.

**Example.**

```bash
$ cremind features list
FEATURE          INSTALLED  RESTART_AFTER  EXTRAS
embedding.me5    false      true           sentence-transformers, torch
vectorstore.qdrant  true    false          qdrant-client
```

### `cremind features install`

**Purpose.** Install one or more features, streaming the pip output.

**Syntax.**

```bash
cremind features install <name>...
```

**Behavior.** Streams the live pip log, then a summary of `installed`,
`already present`, and `failed` features. Exits **non-zero** if any feature
fails. A feature whose `RESTART_AFTER` is true (e.g. `embedding.me5`, which
pulls torch) only takes effect after `cremind server restart`; the command says
so when finished.

**Example.**

```bash
$ cremind features install embedding.me5
Collecting sentence-transformers ...
...
installed: embedding.me5
restart required — run `cremind server restart`
```

## Troubleshooting

**`server returned 403`** — Feature install is admin-only once setup is
complete; use an admin `CREMIND_TOKEN`.

**Install succeeded but the feature still isn't active** — It needs a restart
(`RESTART_AFTER = true`). Run `cremind server restart`.

**A feature failed to install** — The pip error is in the streamed log. Common
causes are a missing system toolchain (for packages that compile) or no network
access on the server host.
