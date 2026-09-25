---
description: "Search the user's OWN files with User Document Search via `cremind userdocs`: turn indexing on or off for this profile (`enable`, `disable --delete-index`), choose the indexed folder (`set-root`, default the working directory), manage exclude rules (`excludes list|add|remove`), follow sync progress live (`status --follow`), describe photos and scanned PDFs with the Specialized Vision Model (`caption --consent-vision`, daily cap), say who \"me\" is for \"docs I wrote\" / \"photos I took\" (`identity`), choose where the agent may use them (`allow-in` web/CLI, channels, rooms), and, as admin, allow the feature and set storage budgets (`admin get|set --allow`). Needs Vector Embedding. Deep research across many files (compile a folder, legal or financial analysis) is `cremind userdocs research`. Not for Cremind's own documentation (that is documentation_search)."
---

# `cremind userdocs` — User Document Search

`cremind userdocs` controls **User Document Search** for the current profile:
an index of your own files (a folder you choose, and optionally Google Drive)
that the agent can search by meaning, by keyword, by date and by folder, and
cite back to the exact page or lines. It mirrors **Settings → My Documents**.

It is separate from `documentation_search`, which only covers Cremind's own
manual. Searching and reading the index from the terminal (`search`, `find`,
`read`, `cite`) is documented in **`cremind userdocs search`**; deep research
jobs over many files (`research run|status|continue|cancel|list` — compile a
folder into one table, or a legal or financial analysis with verified quotes)
in **`cremind userdocs research`**.

Two levels of switch:

1. **The admin gate** — the admin allows the feature for the server
   (`cremind userdocs admin set --allow`). It needs **Vector Embedding** to be
   on, because the embedding model and vector store are shared server-wide.
2. **Each profile opts in** — `cremind userdocs enable`. Every profile has its
   own folder, its own index and its own settings; no profile can see another's.

## Finding this in the web UI

> **Sidebar → Settings → My Documents** (every profile)
> **Sidebar → Settings → Embedding → User Document Search** (admin: the gate)

## Changes that remove indexed content

Moving the folder, adding exclude rules that drop indexed files, and
`disable --delete-index` would remove content from the index. The server
refuses them at first and prints what would go:

```text
This change would:
  - remove files that are no longer in scope from the index (412 files)
Nothing was changed. Re-run with --yes to apply.
```

Re-run with `--yes` to apply (exit code 2 means "not applied, confirmation
needed"). Scripts and the agent's own shell must pass `--yes` explicitly —
nothing is ever deleted by default. `disable` without `--delete-index` keeps
the index so re-enabling is fast.

## Global flags

All subcommands accept the root-level `--json` flag, right after `cremind`
(`cremind --json userdocs status`).

## Subcommands

### `cremind userdocs status`

**Purpose.** Show what User Document Search is doing for this profile.

```bash
cremind userdocs status [--follow/-f]
```

**Behavior.** Prints one line: the state (`disabled`, `idle`, `scanning`,
`indexing`, `paused(...)`, `suspended(...)`, …), progress, the file being
processed, and how search behaves right now (`search: lexical_only` when
Vector Embedding is off). With `--follow`, prints one line per update until
Ctrl-C; with `--json`, one JSON snapshot per line.

```bash
$ cremind userdocs status
indexing · 3120/12840 files · 12 failed · ~30 min left · now: MKT-report/q3.xlsx (embed)
```

`suspended(admin_gate)` means the admin has not allowed the feature;
`suspended(embedding_off)` means Vector Embedding is off — the index is kept
and still searchable by keyword, but nothing syncs.

### `cremind userdocs settings`

**Purpose.** Print this profile's folder, Google Drive and option settings,
plus what the server allows (working directory, whether non-admin folders must
sit inside it).

```bash
cremind userdocs settings
```

### `cremind userdocs enable`

**Purpose.** Turn on User Document Search for this profile.

```bash
cremind userdocs enable [--root PATH] [--yes]
```

| Flag     | Default                 | Meaning                                                   |
|----------|-------------------------|-----------------------------------------------------------|
| `--root` | the working directory   | Folder to index. Non-admin profiles: must be inside the working directory. |
| `--yes`  | off                     | Apply even if the change removes indexed content.         |

**Errors.** `FeatureDisabledByAdmin` (ask the admin to run `admin set --allow`),
`EmbeddingDisabled` (Vector Embedding is off), `root_path: …` (the folder was
refused — inside Cremind's system folder, an OS location, or outside the
working directory).

### `cremind userdocs disable`

**Purpose.** Turn it off for this profile.

```bash
cremind userdocs disable [--delete-index] [--yes]
```

| Flag             | Default | Meaning                                                  |
|------------------|---------|----------------------------------------------------------|
| `--delete-index` | off     | Also delete the index (needs `--yes`). Without it the index is kept. |
| `--yes`          | off     | Confirm deleting the index.                              |

### `cremind userdocs set-root`

**Purpose.** Change the indexed folder.

```bash
cremind userdocs set-root PATH [--yes]
cremind userdocs set-root --inherit [--yes]
```

`--inherit` goes back to the working directory. Files that stay in scope keep
their index entries; files outside the new folder are removed (after `--yes`).

### `cremind userdocs excludes`

**Purpose.** Manage the folder's exclude rules.

```bash
cremind userdocs excludes list
cremind userdocs excludes add PATTERN [--type glob|dir|ext] [--metadata-only] [--yes]
cremind userdocs excludes remove PATTERN
```

| Flag              | Default | Meaning                                                     |
|-------------------|---------|-------------------------------------------------------------|
| `--type`          | `glob`  | `glob` (e.g. `Archive/**`), `dir` (a folder name anywhere), `ext` (e.g. `iso`). |
| `--metadata-only` | off     | Keep name/date/size in the index but never read the content. |
| `--yes`           | off     | Apply even if indexed files would be removed.               |

Credential folders (`.ssh`, `.aws`, `.kube`, `.gnupg`, Cremind's system folder)
are always excluded, and secret-looking files (`.env`, `*.pem`, `id_rsa`, …)
are never read — these cannot be overridden.

### Sync control

```bash
cremind userdocs start                 # begin a large first sync after `estimate`
cremind userdocs pause                 # stop syncing; the index stays searchable
cremind userdocs resume                # continue; changes made meanwhile are picked up
cremind userdocs rescan                # walk the whole folder now
cremind userdocs reindex PATH|FID ...  # re-read these files/folders first
cremind userdocs retry [--file-id FID] # retry failed files (all, or one per --file-id)
cremind userdocs rebuild [--reextract] [--yes]
cremind userdocs deletions confirm     # remove held vanished files from the index
cremind userdocs deletions reject      # keep them (hidden, re-checked for 14 days)
```

`rebuild` re-embeds everything from the stored text (`--reextract` also
re-reads every file) — rarely needed. When many files vanish at once (an
unplugged drive, a renamed folder) nothing is deleted: they are hidden from
search until you run `deletions confirm` (remove them) or `deletions reject`
(keep them; they are re-checked on every scan for 14 days).

### `cremind userdocs caption`

**Purpose.** Captions make photos findable by what they show ("two puppies in
a park") and scanned PDF pages readable. They come **only** from the
Specialized Vision Model chosen in **Settings → LLM Providers** — never the
main model — and only after you consent to sending images to it.

```bash
cremind userdocs caption [--enable|--disable] [--daily-cap N] [--min-px N] [--min-kb N]
                         [--consent-vision | --revoke-consent]
```

| Flag | Meaning |
|------|---------|
| `--enable/--disable` | Caption images at all (default on). |
| `--daily-cap` | Images + scanned pages per day for this profile (default: the admin's, 1000). |
| `--min-px` / `--min-kb` | Skip icons and tiny images (defaults 256 px, 20 KB). |
| `--consent-vision` | Agree to send images to the vision model `settings` shows. Consent is per model: switching the model asks again. |
| `--revoke-consent` | Stop sending images; existing captions stay. |

Until then images are indexed by name, folder, date and camera (EXIF), shown
as `awaiting_vision` / `awaiting_consent`; over the daily cap they are
`over_cap` and captioned on the following days, newest first. A search with
`verify_images` (see **`cremind userdocs search`**) re-checks its top photos
with the same model and uses the same daily cap.

**Errors.** `VisionNotConfigured` — no usable Specialized Vision Model; choose
one in Settings → LLM Providers. `VisionModelChanged` — the model changed after
`settings` was read; run `settings` again, check the model, and repeat.

### `cremind userdocs identity`

**Purpose.** Say who "me" is, so "the report I wrote" and "photos I took"
rank your own files first. A match is a boost, never a filter: files with no
author or no EXIF are still found.

```bash
cremind userdocs identity [--name NAME]... [--email EMAIL]... [--camera DEVICE]...
```

| Flag | Meaning |
|------|---------|
| `--name` | A name you write as (the author recorded in Word, PDF, … files). |
| `--email` | An email address of yours. |
| `--camera` | A camera or phone you shoot with, as its EXIF make/model shows it (e.g. `iPhone 14`). |

Each flag repeats or takes comma-separated values, and **replaces** that list;
`--name ""` clears it. Without flags, prints the current identity.

```bash
cremind userdocs identity --name "Ann Nguyen, Nguyễn Thị An" --camera "iPhone 14"
```

### `cremind userdocs allow-in`

**Purpose.** Choose where the agent may use your documents. Takes effect from
the next message.

```bash
cremind userdocs allow-in [--web-cli/--no-web-cli] [--channels/--no-channels] [--rooms/--no-rooms]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--web-cli` | on | The web app, the CLI and your own automations. |
| `--channels` | off | Messaging channels (Telegram, Zalo, …) — whoever the channel answers could ask about your files. |
| `--rooms` | off | Group rooms — everyone in the room reads the answers; the sources list there leaves out paths and links. |

Without flags, prints the current setting.

### Inspecting the index

```bash
cremind userdocs files [--status S] [--kind K] [--query TEXT] [--limit N] [--all]
cremind userdocs activity [--limit N]
cremind userdocs estimate [--wait]
cremind userdocs storage
```

`files --status error` lists files that failed and why (`encrypted`,
`timeout`, `too_large`, `permission_denied`, …); `metadata_only` files are
indexed by name, type, size and dates only (executables, archives, media,
secret-looking files). `activity` shows what changed, e.g. "report.docx — 3 of
128 chunks re-embedded": only the edited parts of a document are re-embedded.
`estimate` counts what a full sync would do (files by type, images to caption,
index size, time). `storage` shows index size against the budget and free disk
space; syncing pauses before the disk fills (deletions and search keep working).

### `cremind userdocs admin get`

**Purpose.** Show the server-wide gate, budgets, and which profiles use the
feature (flags only — never other profiles' paths). Admin token required.

```bash
cremind userdocs admin get
```

### `cremind userdocs admin set`

**Purpose.** Allow or disallow the feature and set limits. Admin token required.

```bash
cremind userdocs admin set [--allow|--disallow] [--budget-mb N] [--profile-budget-mb N]
                           [--vision-cap N] [--max-file-mb N] [--workers N]
                           [--vector-capacity-mb N] [--db-capacity-mb N]
```

| Flag                   | Meaning                                                              |
|------------------------|----------------------------------------------------------------------|
| `--allow/--disallow`   | Let profiles turn the feature on. Needs Vector Embedding on.          |
| `--budget-mb`          | Storage budget for all profiles' indexes together (default 10240).   |
| `--profile-budget-mb`  | Per-profile budget; 0 means only the global budget applies.         |
| `--vision-cap`         | Default photos/scans captioned per profile per day (default 1000).   |
| `--max-file-mb`        | Files larger than this are indexed by metadata only (default 100).   |
| `--workers`            | Extraction subprocesses shared by all profiles (1–8, default 2).    |
| `--vector-capacity-mb` | Size of an external/Kubernetes vector-store volume; 0 = measure it.  |
| `--db-capacity-mb`     | Size of an external/Kubernetes database volume; 0 = measure it.     |

**Errors.** `FeatureNotInstalled` on `--allow` prints the command to run
first: `cremind features install userdocs`. `EmbeddingDisabled` means turn on
Vector Embedding first (`cremind embedding set`).

## Troubleshooting

**`suspended(admin_gate)`** — The admin has not allowed the feature:
`cremind userdocs admin set --allow` (admin).

**`root_path: That folder is inside Cremind's system folder`** — The working
directory is set to Cremind's own system folder (`~/.cremind`). Pick another
folder with `set-root PATH`, or ask the admin to change the working directory.

**Exit code 2** — The change needs confirmation; the printed plan says what
would be removed. Re-run with `--yes`.
