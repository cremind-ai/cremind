---
description: "Package a single profile's **design** into a portable `.cremind-blueprint` file and import it into a profile in another Cremind install — for sharing a purpose-built agent (e.g. a customer-service assistant) with others. A blueprint carries the agent persona, per-tool enable/disable and configuration, the chosen LLM provider/model selection, any settings changed from defaults (e.g. max steps 200→300), bundled user skills, registered events (schedules, file watchers, skill events), and skill event-listener processes. It contains **NO secrets** — API keys, tokens, passwords, OAuth token files, and `scripts/.env` are never included; the importer is prompted to supply the required secrets (or skip and provide them later). Export shows a checklist of only the components you actually customized. Import runs a step-by-step wizard that applies the design to the current profile (create a fresh profile first if you don't want to change an existing one) and reports what needs attention. It can also publish a blueprint to the Cremind Hub marketplace with `publish` (a browser device-code approval — no hub credentials are stored locally) and install one from the hub by link or name with `install`. Blueprints carry version metadata and stay compatible across Cremind versions in both directions. Distinct from `cremind backup`, which snapshots the whole system including secrets."
---

# `cremind blueprint` — Package & share a profile's design

`cremind blueprint` turns the *design* of one profile into a portable
`.cremind-blueprint` file that someone else can import into a brand-new profile
in their own install. It is how you share a purpose-built agent — say a
customer-service assistant with a tuned persona, the email + calendar skills, a
tested LLM provider, and a couple of scheduled tasks — so others get the same
setup without rebuilding it by hand.

It is **not** a backup. `cremind backup` copies the whole system *including
secrets* to move or recover an install; a blueprint copies only the design and
public settings of ONE profile, and never copies a secret.

## What a blueprint contains

A `.cremind-blueprint` is a gzipped tar of semantic JSON documents plus bundled
skill files:

- **`manifest.json`** — name, description, author, the app version it was made
  with, per-component versions, and a precomputed `requirements` list (which
  secrets and paths the importer must supply).
- **`components/*.json`** — one document per component you chose to export:
  - `persona` — the agent persona (system prompt) + agent name
  - `tools` — per-tool enable/disable, arguments, LLM overrides, disabled
    sub-tools, and the *names* of any secret variables (never their values)
  - `llm` — the selected provider(s), model groups (high/vision/low),
    reasoning effort, and custom-provider definitions (API keys excluded)
  - `settings` — only the settings you changed from their defaults
  - `skills` — an index of bundled/built-in skills + their configuration
  - `events` — schedules, file watchers, and skill-event subscriptions
  - `listeners` — skill event-listener processes (referenced by skill, so the
    command is rebuilt safely on the target)
- **`skills/<dir>/**`** — the files of each bundled *user* skill (built-in
  skills are referenced by name, not copied — they ship with every install).

## What a blueprint NEVER contains

Secrets are stripped by construction and a fail-closed audit blocks the export
if anything slips through:

- API keys, tokens, passwords — any `is_secret` configuration value
- skill `scripts/.env` files and OAuth token stores
  (`.google_token.json`, `.atlassian_token.json`, `.ha_token.json`,
  `.listener_state.json`, and any `*token*`/`*secret*`/`*credential*` file)
- conversations, messages, memories, usage, channels, and the JWT secret

Only design and public settings travel. On import you are prompted for the
secrets the design needs.

## Export

Export runs against the server and packages the **current profile** (your token's
profile). Only components you actually customized appear.

```
cremind blueprint exportable                 # show what can be exported
cremind blueprint export --all               # export every available component
cremind blueprint export --components persona,llm,skills --name cs-agent -o ./cs-agent.cremind-blueprint
cremind blueprint export --skills imap-email,my-skill   # bundle only these skills
```

Options:

| Option | Meaning |
| --- | --- |
| `--all` | Include every available component (default if `--components` omitted). |
| `--components a,b,c` | Only these component keys (`persona,tools,llm,settings,skills,events,listeners`). |
| `--skills a,b` | Bundle only these skill slugs (default: all customized skills). |
| `--name` / `--display-name` / `--description` | Blueprint metadata. |
| `-o, --out PATH` | Also download the archive to a local file. |

Built-in skills are exported **settings-only** (their files ship with every
install and are re-synced on boot, so a modified built-in isn't portable). Note
that persona and event/tool action text ship verbatim — review them for any
secret you may have pasted in.

## Inspect (offline)

Read a local blueprint's manifest without a server and without extracting any
skill files:

```
cremind blueprint inspect ./cs-agent.cremind-blueprint
```

It prints the components included and the secrets you'll be asked for on import.

## Import

Import applies the design to the **current profile** (your token's profile) —
it does not create a profile. Create a fresh profile first (and use its token)
if you don't want to change an existing one; that keeps the blueprint from
overwriting matching settings in a profile you already use.

```
# See what it needs first (targets your token's profile):
cremind blueprint import ./cs-agent.cremind-blueprint --dry-run

# Apply, providing the secrets it asked for:
cremind blueprint import ./cs-agent.cremind-blueprint \
  --set openai.api_key=sk-... \
  --set skill:imap-email.IMAP_PASSWORD=... \
  --start-listeners
```

Options:

| Option | Meaning |
| --- | --- |
| `--profile NAME` | Optional. Assert the import targets this profile; it must match your token's profile, else the command errors. |
| `--set KEY=VALUE` | Provide a required secret/path (repeatable). See the grammar below. |
| `--skip-all` | Apply the design even if some secrets are missing (they error at run time). |
| `--start-listeners` | Start skill listeners now (otherwise they start on the next server restart). |
| `--dry-run` | Upload + validate, print the plan and required secrets, then stop. |
| `--replace` | Abort any in-progress import you started first. |

In the UI, open a profile's **Settings → Blueprints** page and use *Import a
blueprint* — the wizard applies the design to that profile.

`--set` grammar:

- `openai.api_key=sk-...` — an LLM provider secret (prefix `llm:` is optional)
- `tool:<tool_id>.<VAR>=value` — a built-in tool's secret variable
- `skill:<slug>.<VAR>=value` — a bundled skill's secret env variable
- `watcher:<name>=/new/path` — a file watcher's root path on this machine

### The skip contract

Skipping never skips the *design* — it only omits a *secret value*. The
component still applies; the missing secret surfaces as a runtime error later
(e.g. the LLM returns "missing API key"), telling you exactly what to add in
Settings. Only one import runs at a time; a new upload with `--replace` stops
any in-progress import you started. Stopping an import does not undo steps
already applied and never deletes the profile — delete the profile yourself if
you want a clean slate.

## Install from the Cremind Hub

Instead of a local file, you can install a blueprint straight from the Cremind
Hub marketplace (`hub.cremind.io`) by its page link or bare name:

```
cremind blueprint install https://hub.cremind.io/blueprints/cs-agent
cremind blueprint install cs-agent --dry-run
cremind blueprint install cs-agent --set openai.api_key=sk-... --start-listeners
```

Cremind downloads the `.cremind-blueprint` from the hub, stages it server-side,
and runs the **same non-interactive wizard as `import`** — so every `import`
option applies (`--profile`, `--set`, `--skip-all`, `--start-listeners`,
`--dry-run`, `--replace`, and the `--set` grammar above). Set `CREMIND_HUB_URL`
to target a non-default hub (e.g. `http://localhost:8788`).

## Publish to the Cremind Hub

Share a blueprint by publishing it to the Cremind Hub marketplace. First export
it (so the server has the archive), then publish by its archive name:

```
cremind blueprint export --all --name cs-agent
cremind blueprint publish cs-agent
```

`publish` downloads the archive from your local server, then runs a **browser
device-code approval** against the hub: it prints a verification URL + short
code and opens the URL (unless `--no-browser`). Sign in / approve on the hub —
if you're not logged in the hub asks you to first — and the CLI uploads on your
behalf and prints the new hub page URL. **No hub credentials are stored
locally**; the one-time, short-lived publish token is used only for the upload.

| Option | Meaning |
| --- | --- |
| `--display-name TEXT` | Human-readable name for the hub listing (defaults to the blueprint name). |
| `--no-browser` | Don't auto-open the approval URL (print it instead) and don't open the result. |

`CREMIND_HUB_URL` overrides the hub base (default `https://hub.cremind.io`). The
blueprint publishes immediately; until a hub moderator verifies it, its hub name
carries a short suffix (e.g. `cs-agent-k3m9p2qr`). You can also publish in one
click from the app's **Settings → Blueprints** page (no file handling), or
upload the exported file on the hub website.

## Version compatibility

A blueprint records the app version it was made with and a per-component
version. Importing checks both directions:

- A blueprint from a **newer** Cremind imports what this build understands and
  skips (with a note) any component or setting it doesn't — it never fails
  wholesale.
- A blueprint whose *format* is newer than this build can read is refused with
  the exact version to upgrade to.

There is no database-schema gate: the design is applied through the current
build's storage, so a blueprint from any past version imports.

## Troubleshooting

- **"needs …" on import** — a required secret wasn't supplied via `--set`; add
  it, or pass `--skip-all` and set it in Settings afterwards.
- **A listener didn't start** — check `cremind skill-events` / the listener's
  `last_error`; a missing `uv` or missing skill env var is the usual cause.
- **A file watcher isn't armed** — its `root_path` doesn't exist on this
  machine; re-run with `--set watcher:<name>=/existing/path` or fix it in
  Events.
- **An MCP/A2A tool's enable state wasn't applied** — that tool isn't installed
  in this environment; blueprints don't create MCP/A2A tools, only record their
  intended state.

## Related commands

- `cremind backup` — full-system backup & restore (includes secrets).
- `cremind profile` — manage profiles.
- `cremind skill-events`, `cremind calendar`, `cremind file-watchers` — inspect
  the events a blueprint registered.
