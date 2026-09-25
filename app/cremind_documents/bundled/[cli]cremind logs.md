---
description: "Tail the live **server log stream** over SSE with `cremind logs tail`: follow the backend's log records in real time, or print a one-shot backfill of the most recent lines with `--no-follow`, filtering by `--level` and `--grep`. Admin-only. This is the whole-server log feed behind the Developer page — distinct from `cremind proc stream`, which streams per-process snapshots."
---

# `cremind logs` — Server Log Tail

`cremind logs` streams the Cremind backend's own log records — the same feed
the web UI renders on the **Developer** page's *Server Logs* panel. It is
**admin-only**: the token in `CREMIND_TOKEN` must belong to the `admin`
profile.

This is the *whole-server* log. It is not the per-process output shown by
`cremind proc stream` (that streams process snapshots) nor a conversation's
agent events (`cremind conv attach`).

## Finding this in the web UI

> **Sidebar → Developer → Server Logs**

The page shows a ring-buffer backfill of recent records followed by a live
tail, with level chips and a text filter — exactly what `cremind logs tail`
exposes on the command line.

## Streaming output format

On connect, the server replays the most recent records it is holding (the
**backfill**), emits a `ready` marker, then forwards every subsequent record
live. Filtering happens client-side — the server always sends everything.

- **Default (table mode):** one line per record,
  `HH:MM:SS.mmm  LEVEL    source  message`.
- **`--json` mode:** the inner log record as one JSON object per line
  (`{ts, level, source, message}`) — pipe-friendly for `jq`.

Press Ctrl-C to exit cleanly (exit code 130).

## Global flags

`cremind logs` accepts the root-level `--json` flag. `CREMIND_TOKEN` (an admin
token) is required.

## Subcommands

### `cremind logs tail`

**Purpose.** Replay the backfill, then follow new records live.

**Syntax.**

```bash
cremind logs tail [--level LEVEL] [--grep TEXT] [--no-follow] [-n/--lines N]
```

**Flags.**

| Flag           | Type   | Default | Meaning                                                                 |
|----------------|--------|---------|-------------------------------------------------------------------------|
| `--level`      | string | (all)   | Minimum level to show: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. Client-side filter. |
| `--grep`       | string | (none)  | Only show records whose `source` or `message` contains this text (case-insensitive). |
| `--no-follow`  | bool   | `false` | Print the backfill then exit at the `ready` marker instead of tailing.  |
| `-n`, `--lines`| int    | `0`     | Keep only the last N matching backfill records (0 = all). Applies to the backfill; live records always print in full. |

**Behavior.** See [Streaming output format](#streaming-output-format).

**Examples.**

```bash
# Follow the log live, warnings and worse only
$ cremind logs tail --level warning

# One-shot: the last 50 lines mentioning "oauth", then exit
$ cremind logs tail --no-follow -n 50 --grep oauth

# Pipe live errors into jq
$ cremind --json logs tail --level error | jq -r '.message'
```

## Troubleshooting

**`server returned 403`** — The token isn't an admin token. Server logs are
admin-only; obtain an admin `CREMIND_TOKEN`.

**No output with `--no-follow`** — The ring buffer had no records matching your
`--level`/`--grep` filters. Loosen the filters or drop `--no-follow` to watch
for new records.

**It never exits** — Without `--no-follow`, `tail` follows forever by design;
press Ctrl-C.
