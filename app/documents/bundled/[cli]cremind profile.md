---
description: "Create, list, inspect, rename, and delete Cremind **profiles** — how to make, add, or register a new profile, remove one, and read or edit a profile's **persona** text and the assistant's **agent name** (display name). Subcommands: `create`, `list`, `get`, `delete`, `persona get/set`, `agent-name get/set`. Each profile isolates its own conversations, tool overrides, and agent registrations."
---

# `cremind profile` — Profile Management

`cremind profile` is the CLI for managing Cremind profiles. A *profile*
isolates a user's conversations, tool overrides, agent registrations,
persona text, and agent name. The active profile is resolved
server-side from the JWT in `CREMIND_TOKEN`, so most other commands implicitly
act on that profile; `cremind profile` is the way to manage profiles
themselves.

The command groups together three concerns:

- **Profile lifecycle** — `list`, `get`, `create`, `delete`.
- **Persona text** — `persona get`, `persona set`. The persona is a
  free-form Markdown blob prepended to the agent's system prompt for
  that profile.
- **Agent name** — `agent-name get`, `agent-name set`. The display name
  the assistant goes by for that profile — shown in the chat header and
  in the `@`-mention menu when more than one profile is reachable.

Deleting a profile cascades: its conversations, tool overrides, and
skill registrations are removed in the same transaction. There is no
confirmation prompt, so be careful.

## Finding this in the web UI

Every operation in this group has a control on the **Profiles** page of
the Cremind web UI:

> **Sidebar → Profiles**

The page shows one row per profile with edit/delete buttons. Selecting
a profile opens a detail panel with two fields — **Persona** (a Markdown
editor matching `cremind profile persona set`) and **Agent name** (a
single-line input matching `cremind profile agent-name set`). Anything you
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

**Purpose.** Show a profile's persona text and agent name together.
This is the equivalent of opening the profile's detail panel in the UI.

**Syntax.**

```bash
cremind profile get <name>
```

**Arguments** (required):

- `<name>` — Profile to inspect.

**Behavior.** Prints a header with `name` and `agent_name`, a blank
line, and the literal `--- persona ---` separator followed by the full
persona Markdown. With `--json`, emits a single object with keys
`name`, `persona`, and `agent_name`.

**Example.**

```bash
$ cremind profile get admin
name        admin
agent_name  Ada

--- persona ---
You are an Cremind admin assistant. Prefer crisp, direct replies.
```

### `cremind profile create`

**Purpose.** Create a new profile. Newly created profiles start with
the server-default persona and agent name.

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

### `cremind profile agent-name get`

**Purpose.** Read the profile's agent name.

**Syntax.**

```bash
cremind profile agent-name get <name>
```

**Behavior.** Prints just the agent name on a single line (empty if the
profile is using the server default). With `--json`, wraps as
`{"name": "..."}`.

**Example.**

```bash
$ cremind profile agent-name get admin
Ada
```

### `cremind profile agent-name set`

**Purpose.** Set the display name the assistant goes by for a profile.

**Syntax.**

```bash
cremind profile agent-name set <name> <agent-name>
```

**Arguments** (both required):

- `<name>` — Profile to update.
- `<agent-name>` — The new agent name (at most 128 characters). Quote it
  if it contains spaces.

**Behavior.** Updates the agent name shown in the chat header and the
`@`-mention menu. Silent on success. The server rejects an empty name or
one longer than 128 characters.

**Example.**

```bash
$ cremind profile agent-name set admin "Ada"
```

## Worked examples

### Bootstrap a fresh profile, seed its persona, and name the agent

```bash
$ cremind profile create alice
alice
$ cremind profile persona set alice < templates/alice.persona.md
$ cremind profile agent-name set alice "Alice"
$ cremind profile get alice
name        alice
agent_name  Alice

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

**`profile not found`** — `get`, `delete`, `persona`, and `agent-name`
all require the profile to exist. Run `cremind profile list` to confirm
spelling.

**`persona set` does nothing / persona is empty** — `persona set`
reads from stdin. If you ran it interactively without redirection, it
is waiting for input — terminate with Ctrl-D after typing, or pipe a
file in with `<`.

**`agent-name set` rejected** — The name must be non-empty and at most
128 characters. Trim it (or quote a name with spaces) and retry.

**Override of "the" profile vs the current profile** — Every subcommand
takes an explicit `<name>`; nothing in `cremind profile` implicitly targets
the active profile. To find out which profile the current token grants,
run `cremind me`.
