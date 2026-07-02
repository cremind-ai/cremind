# Cremind Skill Specification

The authoritative contract for a Cremind skill. A skill written to this spec
runs correctly with no access to Cremind's source. Read this before writing or
editing any `SKILL.md` frontmatter. For the event system (the `events/` folder,
listeners, subscriptions) read `events.md`. For copy-adapt starting points read
`templates.md`.

Read these files with `cat references/<file>.md` via **Exec Shell** — when a
skill is loaded, the skill directory must not be read with the System File tool
(the load header says so). Exec Shell runs with the skill directory as its
working directory.

---

## 1. What a skill is

A skill is a directory containing a `SKILL.md` file. That's the only hard
requirement. Cremind scans skill directories, and each valid one becomes a
**native tool** the agent can call. When the agent calls it, the entire
`SKILL.md` body is injected into the conversation as a tool result — so the body
is an operating manual the future agent reads and follows.

```
<skill-name>/
├── SKILL.md          # required — frontmatter + markdown body
├── scripts/          # optional — uv-run Python (CLI + listener) + .env
├── events/<type>/    # optional — event drop-zones (Cremind's differentiator)
├── references/       # optional — docs the agent reads on demand
├── assets/           # optional — portable, but INERT in Cremind (see §7)
└── agents/           # optional — portable, but INERT in Cremind (see §7)
```

### Discovery and lifecycle

- **Where skills live at runtime:** each profile has its own skills directory at
  `~/.cremind/<profile>/skills/`. A skill is installed by having its directory
  present there. Built-in skills are copied in on boot; user skills are authored
  directly into that directory (or imported).
- **Hot reload:** a filesystem watcher re-scans the profile skills directory
  about **1 second** after any change. Creating a new skill directory, or
  editing a `SKILL.md`, takes effect within ~1s — no restart, no code change, no
  registration step.
- **Built-in names are reserved.** Built-in skills are re-copied into every
  profile on each boot, overwriting any directory with the same name. A user
  skill that reuses a built-in name (`caldav-calendar`, `confluence`,
  `gcalendar`, `gmail`, `homeassistant`, `imap-email`, `jira`, `skill-creator`)
  will be clobbered. Never reuse one. `scripts/validate.py` catches this because
  the built-ins are siblings in the same directory.
- **Duplicate names shadow silently.** If two directories declare the same
  frontmatter `name`, the first one scanned wins and the other never appears —
  with no error. Keep names unique.

---

## 2. SKILL.md frontmatter contract

`SKILL.md` starts with a YAML frontmatter block delimited by `---` fences,
followed by a markdown body:

```markdown
---
name: my-skill
description: One or two sentences describing what the skill does and when to use it.
metadata: { ... optional ... }
---

# my-skill

...markdown body the agent follows when the skill is loaded...
```

### Parsing rules (exactly how Cremind reads it)

- The frontmatter is the text between the leading `---` and the **first** `---`
  that follows it. **Gotcha:** because Cremind finds the closing fence by
  searching for the next `---`, a literal `---` line *inside* your frontmatter (or
  a Markdown horizontal-rule `---` placed before you meant to close the block)
  ends the frontmatter early — everything after it silently becomes body. Don't
  put `---` inside frontmatter values.
- The block is parsed with a standard YAML loader. If it fails to parse, or does
  not parse to a mapping, **the whole skill is silently skipped** (a warning is
  logged to the server, but nothing surfaces in the conversation). This is the
  single most common failure mode — run the validator.
- **`name`** (required): must be a non-empty string. **By convention and rule
  here, it must equal the directory name.** (Event dispatch falls back to a
  slugified directory name, so a mismatch causes subtle event bugs.)
- **`description`** (required): must be a non-empty string. This is the text the
  model sees *before* loading the skill — it is what makes the model decide to
  load it. Front-load capabilities and trigger conditions.
- **`metadata`** (optional): must be a mapping if present. Preserved verbatim.
  **Only three keys are consumed** (§3–§5); every other key under `metadata`,
  and every top-level frontmatter key other than `name`/`description`/`metadata`
  (e.g. `variables`, `arguments`, `license`, `version`), is silently ignored.
  Don't invent frontmatter keys expecting behavior.
- Only `description` from the frontmatter is re-injected with the body; `name` is
  shown separately in the load header and `metadata` is stripped from the
  injected text (it drives behavior, it isn't instructions).

---

## 3. metadata.environment_variables

Declares the skill's configuration. Cremind renders these as a form in
**Settings → the skill**, and writes the user's values into `scripts/.env`
automatically. **Scripts must read configuration only from `scripts/.env`** —
never ask the user to export environment variables in chat, and never rely on a
committed `.env` (it is overwritten from the database on every save and on every
boot).

```yaml
metadata:
  environment_variables:
    - name: API_BASE_URL          # required: the variable name
      description: Base URL of the service   # shown in the Settings form
      required: false             # bool; default false
      secret: false               # bool; true => masked input, not logged
      type: string                # string | boolean | number | enum
      default: "https://api.example.com"
      enum: []                    # list[str]; used when type: enum
```

Field semantics:

| Field | Meaning |
|---|---|
| `name` | Required. The env var name. An entry with no string `name` is dropped. |
| `description` | Shown in the Settings form. Defaults to `name` if omitted. |
| `required` | Boolean; default `false`. Required+unset variables block functionality. |
| `secret` | Boolean. When omitted, Cremind guesses from the name (e.g. names containing `TOKEN`/`SECRET`/`KEY`/`PASSWORD` are treated as secret). Set it explicitly when unsure. |
| `type` | `string` (default), `boolean`, `number`, or `enum`. |
| `default` | Default value (stringified). |
| `enum` | List of allowed string values; pair with `type: enum`. |

A plain string entry (`- API_BASE_URL`) is also accepted and treated as an
optional string variable, but prefer the object form.

---

## 4. metadata.events

Declares the event types the skill can emit. This is Cremind's differentiator;
the full mechanism is in `events.md`. In frontmatter it looks like:

```yaml
metadata:
  events:
    event_type:
      - name: new_item
        description: A new item appeared upstream
      - name: item_changed
        description: An existing item was modified
```

Each `name`:

- Becomes a value in the skill tool's `subscribe.trigger` enum (how the agent
  subscribes a conversation to that event).
- Is validated when a subscription is created — an unknown trigger is rejected.
- Should match `^[a-z0-9_]+$` (lowercase snake_case) because **it doubles as a
  folder name**: the skill emits an event by writing a markdown file into
  `events/<name>/`. Ship each declared event's folder with a `.gitkeep` so it
  exists right after install and survives git.

On load, Cremind appends an "Automatic actions on events" hint to the skill's
tool result listing these events and telling the agent it can subscribe.

---

## 5. metadata.long_running_app

Declares a background process (typically the event listener) that Cremind
manages for the skill.

```yaml
metadata:
  long_running_app:
    command: uv run scripts/event_listener.py
    description: Persistent listener that emits <skill> events.
```

- `command` (required string): run with the skill directory as its working
  directory. Convention is `uv run scripts/event_listener.py`.
- On first install, Cremind raises a notification prompting the user to register
  the listener; once started it is respawned automatically on every boot.
- Controlled via the CLI: `cremind skill-events listener-start <skill>` and
  `cremind skill-events listener-status <skill>` (see `events.md`).

Declaring `long_running_app` without declaring any events is usually a mistake —
a listener with nothing to emit.

---

## 6. Runtime behavior when a skill is loaded

- The skill is exposed to the model as a native function. Calling it with a
  `request` argument **loads** the skill: Cremind injects a load header, a
  generated tree of the skill directory, the full `SKILL.md` body (untruncated),
  and the events hint (if any) as a single tool result. No sub-agent runs — the
  same agent continues, now following the body.
- **The conversation's working directory is anchored to the skill directory.**
  Subsequent Exec Shell / System File calls run there, so `uv run
  scripts/__main__.py …` resolves against the skill. The `change_working_directory`
  tool can move between the user working dir, the skills root, documents, a custom
  path, or any loaded skill's directory.
- **Budget:** because the whole body is injected on every load, keep it tight
  (roughly ≤ 200 lines / ~10 KB) and push depth into `references/`. The validator
  warns past ~10 KB.
- Calling the skill with a `subscribe` object (instead of `request`) creates an
  event subscription rather than loading — see `events.md`.

---

## 7. Directory conventions — what Cremind parses vs. ignores

| Path | Status |
|---|---|
| `SKILL.md` | **Parsed.** Required. |
| `scripts/` | **Used.** Working directory for the skill; `scripts/.env` is auto-materialized here from Settings. Run entry points as `uv run scripts/<file>.py`. |
| `events/<type>/` | **Watched.** Markdown dropped here fires the event pipeline (see `events.md`). |
| `references/` | **Convention only.** Nothing parses it; the agent reads files on demand via Exec Shell `cat`. |
| `assets/` | **Inert in Cremind.** Part of the standard Agent Skills layout (output resources/templates). Copied along and shown in the on-load tree, but Cremind never consumes it. Portable if you also target other Agent-Skill runtimes. |
| `agents/` | **Inert in Cremind.** Standard Agent Skills UI metadata. Same as `assets/`: copied, listed, never consumed. |

**Schedules are not a skill concept.** Time-based / recurring behavior is set up
at runtime as calendar/scheduler rows via Cremind's scheduler tools, bound to a
conversation — never declared in `SKILL.md` frontmatter. Do not emit any schedule
declaration in a generated skill; if the user wants a recurring action, tell them
to ask for it in conversation (the scheduler handles it).

---

## 8. Authoring a built-in skill (Cremind contributors)

End users author skills into their profile skills directory (above). If you are
developing Cremind itself and want to ship a skill with the product, the contract
is identical, with two differences:

- The source lives at `app/skills/builtin/<name>/` in the Cremind repo. On boot it
  is copied into every profile's skills directory, and its name becomes reserved
  for all profiles.
- It must be **git-tracked** to ship: the wheel packages git-tracked files under
  `app/`. Empty directories need a `.gitkeep`, and `scripts/.gitignore` should
  exclude runtime artifacts (`.env`, `*token*.json`, listener state, `__pycache__/`).

No registration list, manifest, or code change is needed — discovery is directory
iteration. No database migration is involved (a skill adds no schema).
