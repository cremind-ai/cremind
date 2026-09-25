---
description: "List and install Cremind's optional feature extras with `cremind features`: `list` shows each feature's install state and whether an installed one is outdated (UPDATE required/restart); `install` installs one or more — or updates an outdated one, such as a Codex SDK too old to read the account's model list — streaming the live pip output over SSE. Covers the vector-embedding models, vector-store backends, coding-agent SDKs and other heavier dependencies kept off the slim `pip install cremind`. An update, or a feature marked requires_restart_after_install, only activates after `cremind server restart`."
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

Both subcommands accept the root-level `--json` flag. It goes right after `cremind`, before the command group (`cremind --json <group> <command>`); a trailing `--json` is rejected as an unknown option. `CREMIND_TOKEN` is
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
| `INSTALLED`     | Whether the extras are present (importable).                   |
| `UPDATE`        | `-` up to date; `required` installed but outside the version range this Cremind needs; `restart` updated, but the server still runs the old copy. |
| `RESTART_AFTER` | Whether activating a first install needs a `cremind server restart`. |
| `EXTRAS`        | The pip extras the feature maps to.                            |

Each `required` feature also gets a note on stderr naming both versions and the
fix. With `--json`, returns the raw feature-id → state map; each entry also
carries `outdated` (bool), `required` (the version specifiers, e.g.
`["openai-codex>=0.154.0,<0.155"]`), `installed_versions` (`{dist: version}`)
and `restart_pending` (bool).

**Example.**

```bash
$ cremind features list
FEATURE          INSTALLED  UPDATE    RESTART_AFTER  EXTRAS
claude_code      false      -         false          claude-code
codex            true       required  false          codex
embedding.me5    false      -         true           embeddings-me5
(codex: openai-codex 0.1.0b3 installed, needs openai-codex>=0.154.0,<0.155 - run `cremind features install codex`, then `cremind server restart`)
```

### `cremind features install`

**Purpose.** Install one or more features, streaming the pip output.

**Syntax.**

```bash
cremind features install <name>...
```

**Behavior.** Streams the live pip log, then a summary of `installed`,
`updated`, `already present`, and `failed` features. Exits **non-zero** if any
feature fails. A feature whose `RESTART_AFTER` is true (e.g. `embedding.me5`,
which pulls torch) only takes effect after `cremind server restart`; the command
says so when finished.

An installed feature whose `UPDATE` is `required` is **updated** instead of
skipped (JSON `done` frame: `upgraded`). The running server keeps the old
package loaded, so an update **always** needs `cremind server restart`; until
then `UPDATE` reads `restart` (and Codex refuses to run rather than drive the
new binary with the old SDK).

**Example.**

```bash
$ cremind features install embedding.me5
Collecting sentence-transformers ...
...
installed: embedding.me5
restart required — run `cremind server restart`

$ cremind features install codex
...
updated: codex
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

**`UPDATE required`** — The feature imports, but its package is older than this
Cremind needs (a runtime venv is never resynced on upgrade). Run
`cremind features install <feature>`, then `cremind server restart`.

**`Codex is in use on this server`, or `Access is denied` (Windows)** — Windows
cannot replace a binary that is running. The update is refused while a Codex
task or sign-in is active, and pip fails with `Access is denied` / "being used
by another process" if the old binary is still held. Wait for them to finish or
restart the server, then update **before** starting any Codex task, and restart
once more.
