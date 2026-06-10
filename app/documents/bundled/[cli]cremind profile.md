---
description: "Complete reference for the `cremind profile` CLI command — the terminal-side counterpart to the **Profiles** page in the Cremind web UI — covering how to list, create, inspect, and delete profiles, plus how to read and edit each profile's persona text and skill mode (`manual` vs `automatic`). Documents every subcommand (`list`, `get`, `create`, `delete`, `persona get/set`, `skill-mode get/set`), their arguments, and the exact stdin contract for `persona set`, so admins can script profile bootstrap and persona rollouts without leaving the shell."
---

# `cremind profile` — Profile Management

`cremind profile` is the CLI for managing Cremind profiles. A *profile*
isolates a user's conversations, tool overrides, agent registrations,
persona text, and skill mode. The active profile is resolved
server-side from the JWT in `CREMIND_TOKEN`, so most other commands implicitly
act on that profile; `cremind profile` is the way to manage profiles
themselves.

The command groups together three concerns:

- **Profile lifecycle** — `list`, `get`, `create`, `delete`.
- **Persona text** — `persona get`, `persona set`. The persona is a
  free-form Markdown blob prepended to the agent's system prompt for
  that profile.
- **Skill mode** — `skill-mode get`, `skill-mode set`. Selects
  whether registered skills are dispatched automatically by the skill
  classifier (`automatic`) or only when the user calls them by name
  (`manual`).

Deleting a profile cascades: its conversations, tool overrides, and
skill registrations are removed in the same transaction. There is no
confirmation prompt, so be careful.

## Finding this in the web UI

Every operation in this group has a control on the **Profiles** page of
the Cremind web UI:

> **Sidebar → Profiles**

The page shows one row per profile with edit/delete buttons. Selecting
a profile opens a detail panel with two tabs — **Persona** (a Markdown
editor matching `cremind profile persona set`) and **Skill mode** (a
two-option radio matching `cremind profile skill-mode set`). Anything you
change here is immediately visible to `cremind profile get`.

## Global flags

All `cremind profile` subcommands accept the root-level `--json` flag to
force JSON output instead of the default tables/key-value view:

```bash
cremind profile list --json
```

`CREMIND_TOKEN` is required for every subcommand in this group.

## Subcommands

`cremind profile` has six subcommand groups. Each is documented below.

### `cremind profile list`

**Purpose.** Print every profile registered on the server.

**Syntax.**

```bash
cremind profile list
```

**Behavior.** Renders a single-column table of profile names. With
`--json`, returns the JSON array exactly as the server emitted it
(typically a list of strings).

**Example.**

```bash
$ cremind profile list
PROFILE
admin
li
guest
```

### `cremind profile get`

**Purpose.** Show a profile's persona text and skill mode together.
This is the equivalent of opening the profile's detail panel in the UI.

**Syntax.**

```bash
cremind profile get <name>
```

**Arguments** (required):

- `<name>` — Profile to inspect.

**Behavior.** Prints a header with `name` and `skill_mode`, a blank
line, and the literal `--- persona ---` separator followed by the full
persona Markdown. With `--json`, emits a single object with keys
`name`, `persona`, and `skill_mode`.

**Example.**

```bash
$ cremind profile get admin
name        admin
skill_mode  automatic

--- persona ---
You are an Cremind admin assistant. Prefer crisp, direct replies.
```

### `cremind profile create`

**Purpose.** Create a new profile. Newly created profiles start with
the server-default persona and skill mode.

**Syntax.**

```bash
cremind profile create <name>
```

**Arguments** (required):

- `<name>` — Profile name. Must not already exist.

**Behavior.** Calls the server's create endpoint and, on success, prints
the new profile name on stdout (so the command is pipe-friendly).

**Example.**

```bash
$ cremind profile create alice
alice
```

### `cremind profile delete`

**Purpose.** Permanently delete a profile and everything scoped to it.

**Syntax.**

```bash
cremind profile delete <name>
```

**Arguments** (required):

- `<name>` — Profile to remove.

**Behavior.** Cascades to the profile's conversations, tool overrides,
agent OAuth tokens, and skill registrations. **There is no confirmation
prompt** — pair with a manual `cremind profile list` first if you need a
sanity check. Silent on success.

**Example.**

```bash
$ cremind profile delete alice
```

### `cremind profile persona get`

**Purpose.** Print just the persona text — useful for piping into a
file, an editor, or a diff.

**Syntax.**

```bash
cremind profile persona get <name>
```

**Arguments** (required):

- `<name>` — Profile whose persona should be printed.

**Behavior.** Writes the persona to stdout with no trailing newline
beyond what the persona itself contains. With `--json`, wraps it as
`{"content": "..."}`.

**Example.**

```bash
$ cremind profile persona get admin > admin.persona.md
$ wc -l admin.persona.md
12 admin.persona.md
```

### `cremind profile persona set`

**Purpose.** Replace the persona text for a profile in one shot. The
new persona is read from **standard input**, so this command composes
naturally with `cat`, redirection, and editor pipelines.

**Syntax.**

```bash
cremind profile persona set <name>      # reads persona from stdin
```

**Arguments** (required):

- `<name>` — Profile whose persona is being overwritten.

**Behavior.** Reads everything on stdin until EOF and posts it as the
new persona. The previous persona is replaced wholesale (there is no
patch/append mode). Silent on success.

**Examples.**

```bash
# From a file
$ cremind profile persona set admin < admin.persona.md

# From a heredoc
$ cremind profile persona set admin <<'EOF'
You are an Cremind admin assistant. Be concise.
Always show file paths as clickable links.
EOF

# Edit-then-replace round-trip
$ cremind profile persona get admin > /tmp/persona.md
$ $EDITOR /tmp/persona.md
$ cremind profile persona set admin < /tmp/persona.md
```

### `cremind profile skill-mode get`

**Purpose.** Read the profile's skill-dispatch mode.

**Syntax.**

```bash
cremind profile skill-mode get <name>
```

**Behavior.** Prints just the literal mode string (`manual` or
`automatic`) on a single line. With `--json`, wraps as `{"mode": "..."}`.

**Example.**

```bash
$ cremind profile skill-mode get admin
automatic
```

### `cremind profile skill-mode set`

**Purpose.** Switch a profile between manual and automatic skill
dispatch.

**Syntax.**

```bash
cremind profile skill-mode set <name> <mode>
```

**Arguments** (both required):

- `<name>` — Profile to update.
- `<mode>` — Either `manual` or `automatic`.

**Behavior.** The two valid values map to the two **Skill mode** radio
options in the web UI. Silent on success. Server-side validation
rejects any other value.

**Example.**

```bash
$ cremind profile skill-mode set admin manual
```

## Skill mode quick reference

| Value       | UI label    | What it changes                                                                                                                  |
|-------------|-------------|----------------------------------------------------------------------------------------------------------------------------------|
| `automatic` | Automatic   | The skill classifier runs on every user turn and may dispatch a registered skill if it matches.                                  |
| `manual`    | Manual only | Skills are only invoked when the user explicitly names one (e.g. by a leading `/skillname`). The classifier is bypassed entirely.|

## Worked examples

### Bootstrap a fresh profile and seed its persona from a file

```bash
$ cremind profile create alice
alice
$ cremind profile persona set alice < templates/alice.persona.md
$ cremind profile skill-mode set alice manual
$ cremind profile get alice
name        alice
skill_mode  manual

--- persona ---
You are Alice's research assistant ...
```

### Roll out a persona update across all profiles

```bash
$ for p in $(cremind profile list --json | jq -r '.[]'); do
    cremind profile persona set "$p" < templates/shared.persona.md
  done
```

### Compare a profile's persona against a checked-in template

```bash
$ diff <(cremind profile persona get admin) templates/admin.persona.md
```

### Tear down a test profile

```bash
$ cremind profile delete alice
$ cremind profile list
PROFILE
admin
li
```

## Troubleshooting

**`profile already exists`** — `create` is rejected when the name
collides with an existing profile. Pick a different name, or
`delete` first.

**`profile not found`** — `get`, `delete`, `persona`, and `skill-mode`
all require the profile to exist. Run `cremind profile list` to confirm
spelling.

**`persona set` does nothing / persona is empty** — `persona set`
reads from stdin. If you ran it interactively without redirection, it
is waiting for input — terminate with Ctrl-D after typing, or pipe a
file in with `<`.

**`skill-mode set` rejected** — Only `manual` and `automatic` are
accepted; any other string returns a validation error.

**Override of "the" profile vs the current profile** — Every subcommand
takes an explicit `<name>`; nothing in `cremind profile` implicitly targets
the active profile. To find out which profile the current token grants,
run `cremind me`.
