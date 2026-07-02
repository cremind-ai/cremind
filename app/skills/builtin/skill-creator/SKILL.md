---
name: skill-creator
description: Create new Cremind skills. Guides you through designing, scaffolding, validating, and verifying a fully Cremind-compatible skill in the user's skills directory — the SKILL.md frontmatter contract, uv-run Python scripts, environment-variable config, and Cremind's event system (events/ folders, background listeners, subscriptions). Load whenever the user asks to create, build, scaffold, fix, or extend a skill.
---

# skill-creator

**Purpose:** Author a new Cremind skill on the user's behalf. You produce a skill
directory that Cremind picks up automatically. Build the **smallest tier** that
meets the need:

- **Tier A — instructions only:** a `SKILL.md` whose body tells a future agent
  what to do. No scripts, no events.
- **Tier B — + scripts:** add a `scripts/` CLI (`uv run scripts/__main__.py …`)
  for deterministic actions.
- **Tier C — + events:** add `events/<type>/` folders and (usually) a background
  listener so the skill reacts to things automatically. This is Cremind's
  differentiator.

This skill itself is a Tier-A example: `SKILL.md` + `references/` + one helper
script, no `metadata`.

## How Cremind skills work (essentials)

- A skill is a **directory with a `SKILL.md`**. It becomes a native tool. When
  loaded, the **entire `SKILL.md` body is injected into the conversation** (no
  sub-agent) and the working directory is anchored to the skill dir — so write
  the body as a self-sufficient manual and keep it tight (push depth to
  `references/`).
- **Frontmatter contract:** `name` (string, required, **must equal the directory
  name**) and `description` (string, required) — plus an optional `metadata`
  mapping. **Every other frontmatter key is silently ignored** — don't invent
  keys. If the YAML fails to parse, the skill is **silently skipped** (no error
  in the conversation); that's the #1 failure mode, so always validate.
- **`metadata` has exactly three consumed keys:** `environment_variables`
  (renders a Settings form and is written to `scripts/.env`), `events.event_type`
  (declares event names → subscription enum + folders), and `long_running_app`
  (a background listener Cremind manages).
- **Config reaches scripts only via `scripts/.env`** (auto-written from Settings;
  overwritten on save and boot). Never ask users to export env vars in chat, and
  never rely on a committed `.env`.
- **Events (the differentiator):** any process that drops a markdown file at
  `events/<event_type>/<file>.md` triggers Cremind — the file is read, **deleted
  immediately** (single-use, wiped on boot), and every conversation subscribed to
  that event runs its action with the file's content.
- **Honesty:** `references/` is convention-only (read on demand). `assets/` and
  `agents/` from the standard Agent-Skills layout are copied but **inert** in
  Cremind (never parsed) — portable, not functional here. **Schedules are not
  frontmatter** — recurring behavior is set up at runtime via Cremind's scheduler
  tools; never put a schedule in a `SKILL.md`.

Full detail lives in the references — read them with **Exec Shell `cat`** (the
skill directory must not be read with System File):
- `cat references/spec.md` — the complete frontmatter/metadata/lifecycle contract.
- `cat references/events.md` — the event pipeline, event-file format, and listener
  contract. **Read before designing any event support.**
- `cat references/templates.md` — copy-adapt SKILL.md / listener / CLI templates.

## Where new skills go, and naming

- Create the skill as a **sibling of this directory**: `../<new-name>/`. This
  skill lives in the profile skills root, so `..` *is* the skills root. (You can
  also `change_working_directory` to the `skills` target.)
- The **directory name must equal the frontmatter `name`** — lowercase,
  hyphen-separated, filesystem-safe.
- **Check for collisions first:** run `ls ..`. A sibling with that name means it's
  taken. Built-in skill names (`caldav-calendar`, `confluence`, `gcalendar`,
  `gmail`, `homeassistant`, `imap-email`, `jira`, `skill-creator`) are **reserved**
  — Cremind re-copies built-ins over any same-named dir on every boot. Never
  overwrite an existing directory.

## Authoring workflow

1. **Interview the user.** What actions should it perform? What external
   service/credentials does it need (→ environment variables; secrets are never
   asked for in chat)? Must it *react automatically* to things happening
   elsewhere (→ events + maybe a listener), or only act when asked (→ no events)?
   Steer toward the smallest tier that satisfies them.
2. **Design.** Pick the tier. List env vars (`name` / `description` / `required` /
   `secret` / `type` / `default`). If it has events: name each event type
   (lowercase snake_case — these double as folder names) and decide whether a
   listener is needed (a push or polling source) or events arrive some other way.
   **If any events are involved, `cat references/events.md` now.**
3. **Check the name.** `ls ..`; rename with the user on any collision.
4. **Scaffold.** With Exec Shell: `mkdir -p ../<name>/scripts` and one
   `../<name>/events/<type>` per declared event; add a `.gitkeep` in each events
   folder; create `scripts/.gitignore` (template E in `references/templates.md`).
5. **Write `SKILL.md`.** Start from a skeleton in `references/templates.md`
   (template A for Tier A, template B for Tier B/C). Make the `description`
   trigger-worthy, and write a body a future agent can follow with no other
   context (Purpose, Setup, CLI table, Examples, Event listener + event schema,
   Troubleshooting, Module layout — the shape of the shipped built-ins).
6. **Write scripts** (Tier B/C). Adapt templates C/D: self-contained PEP-723
   files run as `uv run scripts/__main__.py` / `uv run scripts/event_listener.py`;
   config only from `scripts/.env`; JSON output for the CLI. For a listener,
   follow the contract in `references/events.md` (baseline on first run,
   single-instance lock, atomic writes) — copy the writer, customize only the
   source.
7. **Validate.** `uv run scripts/validate.py ../<name>` (run from *this* skill's
   directory, the default working dir). Fix every `ERROR`; address `WARN`s.
8. **Verify it registered.** Wait ~2s (watcher debounce ~1s), then
   `cremind skill-events events <name>`. A listing (even empty) proves it parsed
   and registered; an "unknown skill" error means the frontmatter failed to parse
   — re-validate.
9. **Verify events** (Tier C). Drop a hand-written, spec-conformant `.md` into
   `../<name>/events/<type>/`; it should vanish within ~1s (pipeline armed;
   with no subscribers it's consumed silently). Full end-to-end: have the user
   load the new skill and ask for an automation (creates a subscription), then
   `cremind skill-events list` → `cremind skill-events simulate <sub_id>` and
   confirm the conversation reacts. Listener: `cremind skill-events
   listener-start <name>` / `listener-status <name>`.
10. **Hand off to the user.** Tell them concretely: (a) open **Settings → the new
    skill** to fill the declared variables (Cremind writes them to `scripts/.env`);
    (b) if it has a listener, approve the registration notification or run
    `cremind skill-events listener-start <name>` (it respawns on boot); (c) to
    automate, load the skill in the conversation that should react and ask for it
    (that creates a subscription — note subscriptions can't be created while
    reacting to an event); (d) editing `SKILL.md` hot-reloads within ~1s.

## Hard rules

- Directory name **==** frontmatter `name`.
- One `events/<type>/` folder per declared event, names matching **exactly**
  (`^[a-z0-9_]+$`); ship each with `.gitkeep`.
- Only `environment_variables`, `events`, `long_running_app` exist under
  `metadata` — don't invent frontmatter.
- No schedule declarations in frontmatter (schedules are runtime scheduler rows).
- `assets/` and `agents/` are inert in Cremind — don't rely on them.
- Secrets go through declared env vars + Settings, never chat.
- Event files are single-use and wiped on boot — never use `events/` as storage.
- Never overwrite an existing directory or a reserved built-in name.
- Write the generated `SKILL.md` for a **future agent with no other context**.

## Validator

`uv run scripts/validate.py <path-to-skill-dir>` parses the frontmatter exactly
as Cremind's scanner does and checks: valid YAML; `name`/`description` present;
`name` == directory; no sibling/built-in name collision; `metadata` shapes; each
declared event has a matching folder; `long_running_app` has a command. A `PASS`
means Cremind will load the skill.

## Module layout

```
skill-creator/
├── SKILL.md
├── references/
│   ├── spec.md            # full skill contract
│   ├── events.md          # event system + listener contract
│   └── templates.md       # copy-adapt SKILL.md / listener / CLI templates
└── scripts/
    ├── .gitignore
    └── validate.py        # pre-flight validator (run before verifying live)
```
