---
description: "Create, list, download, upload, delete, and restore **full-system backups** of Cremind — one portable `.cremind-backup` archive holding the entire database (conversations, LLM providers and API keys, custom providers, channels, events/schedules, memories) plus the on-disk trees (skills, Google/OAuth token files, personas, per-profile documents, channel sessions, browser login state). The JWT sign-in secret and session tokens are kept local to each install (re-issued on restore), not carried in the archive. Restores across environments — Windows↔Docker/K8s, SQLite↔PostgreSQL, a new home directory — by relocating stored absolute paths automatically. Use this to move Cremind to another machine, reinstall without losing data, or recover from failure. Distinct from `cremind db backup`, which snapshots only the database. Optional passphrase encryption."
---

# `cremind backup` — Full-System Backup & Restore

`cremind backup` captures the **entire** Cremind system in a single portable
archive and restores it into this or a completely different environment. It is
what you use to reinstall Cremind, move from a laptop to a server, or recover
after a disk failure without losing anything.

Unlike `cremind db backup` (which snapshots only the relational database), a
full backup also includes everything Cremind keeps on disk: per-profile skills
(and their Google/OAuth token files), personas, per-profile documents, channel
session files, and browser login state.

The JWT sign-in secret and the per-profile session tokens are **not** backed
up — they are local to each installation. Carrying them across a restore would
invalidate the target's own sign-ins (tokens are verified against the secret
that signed them). A restore keeps the target's current secret and re-issues a
fresh token file for every restored profile, so you stay signed in and each
profile's `tokens/<profile>.token` stays valid.

## What a backup contains

A `.cremind-backup` archive is a gzipped tar with three parts:

- **`manifest.json`** — versions, the database revision, the source
  environment's paths (used to relocate absolute paths on restore), and the
  profile list.
- **A portable database dump** — a backend-neutral logical dump, so a backup
  taken on SQLite restores into PostgreSQL and vice-versa.
- **The file trees** under `CREMIND_SYSTEM_DIR` (skills, personas, per-profile
  documents, channel sessions, browser profiles).

Rebuildable/transient and installation-local content is intentionally excluded:
the raw database files (dumped logically instead), the embeddings vector store
(rebuilt on boot), the shared documents corpus (re-seeded from the bundle),
temporary chat uploads, derived skill `.env` files (regenerated from the
database), and the JWT sign-in secret and session tokens (kept per-install;
re-issued on restore).

## Environment independence

Absolute paths stored in the database (a conversation's working directory, an
autostart process's command and working directory, a skill's source directory,
a file-watcher root, the configured user working directory) are **relocated**
on restore: the source machine's `CREMIND_SYSTEM_DIR` and home directory are
rewritten to the target's, converting separators between Windows and POSIX.
Paths that live outside those roots (e.g. a `D:\projects\...` working
directory) are left unchanged and reported as warnings, since the process that
uses them may not run in the new environment.

After a restore, the normal boot re-arms everything from the restored data:
previously-activated events, schedules (fired forward from their next
occurrence), autostart processes, and channels. Any autostart process or
channel that cannot start in the new environment produces a **warning** (see
`cremind backup report`) rather than failing silently — restart or re-link it
manually.

## Finding this in the web UI

> **Settings → Backup & Restore**

The page lists backups, creates new ones (with an optional passphrase),
uploads/downloads archives, and drives a restore with live progress. The Setup
Wizard also offers "restore from a backup" as an alternative to configuring a
fresh install.

## Syntax

```bash
cremind backup create   [--offline] [--to <path>] [--passphrase <text> | --passphrase-prompt]
cremind backup list
cremind backup download <name> [--to <path>]
cremind backup upload   <path>
cremind backup delete   <name> [--yes]
cremind backup restore  <src>  [--offline] [--yes] [--passphrase <text> | --passphrase-prompt]
cremind backup status
cremind backup report   [--ack]
```

### Online vs. offline

- **Online (default)** — talks to a running server over the REST API
  (`--server` / `CREMIND_TOKEN`). `create` runs in the background and streams
  phases; `restore` uploads a local archive if needed, then restarts the server
  to apply the restore (the command keeps polling through the restart).
- **`--offline`** — talks to the local engine directly and must be run with the
  Cremind **service stopped**. This is the path for moving to a new machine or
  disaster recovery: point `CREMIND_SYSTEM_DIR` at the target, run
  `cremind backup restore <archive> --offline`, then start the service.

## Options

| Flag                   | Applies to        | Meaning                                                                 |
|------------------------|-------------------|-------------------------------------------------------------------------|
| `--offline`            | create, restore   | Operate on the local system directly (service stopped).                 |
| `--to <path>`          | create, download  | Output path. Offline create / any download.                            |
| `--passphrase <text>`  | create, restore   | Encrypt (create) or decrypt (restore) with this passphrase.             |
| `--passphrase-prompt`  | create, restore   | Prompt for the passphrase interactively (hidden input).                 |
| `--yes` / `-y`         | restore, delete   | Skip the confirmation prompt.                                           |
| `--ack`                | report            | Mark the restore report as acknowledged.                                |

The passphrase may also be supplied via the `CREMIND_BACKUP_PASSPHRASE`
environment variable. A backup contains the secrets Cremind holds (LLM API
keys, channel bot tokens, OAuth refresh tokens, database passwords) in the
clear unless you encrypt it — prefer a passphrase for archives that leave the
machine. (The JWT sign-in secret and session tokens are not among them — they
stay local to each install.)

## Where archives live

Backups are written to `<CREMIND_SYSTEM_DIR>/backups/` (e.g.
`~/.cremind/backups/cremind-<version>-<timestamp>.cremind-backup`). A restore
first takes a safety backup of the current system (named `pre-restore-*`) so a
failed restore rolls back automatically.

## Cross-environment notes

- **Windows → Docker/K8s**: paths relocate automatically; session-based channels
  (Telegram userbot, WhatsApp, Zalo) may not transfer and will need re-linking —
  they surface in `cremind backup report`.
- **SQLite → PostgreSQL**: supported. The target's configured database provider
  (from `bootstrap.toml` / `CREMIND_POSTGRES_*`) receives the imported data. On
  Kubernetes, configure PostgreSQL via the Setup Wizard **before** restoring.

## Worked examples

Create an encrypted backup and download it:

```bash
cremind backup create --passphrase-prompt
cremind backup list
cremind backup download cremind-0.0.8-20260708_120000.cremind-backup --to ./mybackup.cremind-backup
```

Move to a new machine (service stopped on the target):

```bash
export CREMIND_SYSTEM_DIR=~/.cremind
cremind backup restore ./mybackup.cremind-backup --offline --passphrase-prompt
cremind serve            # events re-arm, autostart relaunches
cremind backup report    # review any processes/channels that couldn't start
```

Restore into a running server (restarts it):

```bash
cremind backup restore ./mybackup.cremind-backup --passphrase-prompt
```

## Troubleshooting

- **"Wrong or missing passphrase"** — the archive is encrypted; pass
  `--passphrase` / `--passphrase-prompt` (or set `CREMIND_BACKUP_PASSPHRASE`).
- **"A restore or backup is already in progress"** — one operation runs at a
  time; check `cremind backup status`.
- **Autostart/channel warnings after restore** — expected when moving between
  environments; `cremind backup report` lists them, then restart the process
  from the Process Manager or re-link the channel under Settings → Channels.
- **Restore failed** — the system automatically rolls back to the safety backup
  taken at the start; the server boots on the pre-restore state.

## Related

- `cremind db backup` / `cremind db restore` — database-only snapshot (same
  backend), used by the upgrader.
- `cremind upgrade` — upgrade Cremind in place.
- `cremind proc` — the Process Manager, where restored autostart processes
  appear (including any that failed to start).
