# CLAUDE.md

Guidance for Claude Code and contributors working in this repo. See
[CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and [RELEASING.md](RELEASING.md)
for the release pipeline.

## Branching

Do not create new branches without permission. Before creating a branch for a
new feature, consult the user first and get their explicit go-ahead.

## Debugging on the local dev environment

To debug errors in Cremind, read the log file `logs/app.log`.

## When you add, modify, or remove a feature

Two checks are mandatory for every feature change. Both are easy to forget and
either leaves users broken on upgrade or leaves the CLI and its docs out of
sync with the app.

### 1. Does it need CLI support? Keep the CLI and its bundled doc in sync.

The `cremind` CLI is a first-class client alongside the UI, so most
user-facing features need CLI coverage. When a feature is added, changed, or
removed, update the CLI to match — and its bundled documentation with it.

A CLI feature is three files kept in lockstep (see `calendar`, `files`,
`usage` for examples):

- **Client wrapper** — `app/cli/client/<feature>.py`: thin async functions over
  the REST endpoints, e.g. `await client.get_json("/api/<feature>/...")`.
- **Command module** — `app/cli/commands/<feature>.py`: a `typer.Typer`
  sub-app with one command per action, registered in
  [app/cli/main.py](app/cli/main.py) via
  `app.add_typer(<feature>_app, name="<feature>")`.
- **Bundled doc** — `app/documents/bundled/[cli]cremind <feature>.md`: YAML
  frontmatter whose `description` is the **only** text embedded into the
  `documentation_search` vector store, plus a Markdown body documenting every
  subcommand and flag. Follow the shape in
  [app/documents/bundled/document.md](app/documents/bundled/document.md).

- **Added** feature → add all three (plus a REST endpoint if the command talks
  to the server).
- **Modified** feature → update the command **and** the bundled doc — both the
  body (subcommands/flags) and the `description` if the feature's purpose moved.
- **Removed** feature → delete the client, the command module, its `add_typer`
  line in `main.py`, **and** the bundled doc.

Notes:
- The bundle is authoritative: `app/documents/sync.py` overwrites
  `~/.cremind/documents/` from `app/documents/bundled/` on every boot, so always
  edit the bundled copy, never the working copy.
- CLI import discipline: modules under `app/cli/` must not import from
  `app.server` / `app.api` / `app.tools` / `app.storage` / etc. at top level
  (see the docstring in [app/cli/main.py](app/cli/main.py)) — this keeps the
  slim `pip install cremind` free of server dependencies.

### 2. Does it change the DB schema? Ship a migration so older installs upgrade.

If the feature adds, changes, or removes a column, table, index, or model in
[app/storage/models.py](app/storage/models.py), it needs an Alembic migration.
The app runs `upgrade head` automatically on boot (`ensure_at_head()` in
[app/storage/migrations.py](app/storage/migrations.py), called from
`ConversationStorage.initialize()`), so a model change with no matching
migration will break existing installs on upgrade.

- Migrations live in `app/alembic/versions/` (current head:
  `20260627_llm_messages`).
- Generate and **hand-review** the migration following the checklist in
  [RELEASING.md](RELEASING.md) → **Schema change**: prefer additive changes,
  split destructive changes across two releases, backfill **inside** the
  migration, and test the upgrade from a real older install (not just a fresh
  DB).
- Migrations must work on **both SQLite and PostgreSQL** — Cremind supports
  both backends. Avoid backend-specific SQL and DDL that one engine can't run
  (e.g. SQLite can't `ALTER COLUMN`; use batch operations / `op.batch_alter_table`),
  and test the upgrade against each.
- Only bump `MIN_SUPPORTED_UPGRADE_FROM` in
  [app/__version__.py](app/__version__.py) when explicitly dropping support for
  older versions.
