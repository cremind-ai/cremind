---
description: "Create, list, inspect, rename, and delete Cremind **profiles**, and **choose which profile the CLI acts as without setting `CREMIND_TOKEN`** — pick a profile interactively on first use in a terminal (a type-to-filter list), or select one directly with the root `--profile` flag or `cremind profile use`, remembered per terminal. Covers making/adding/registering a new profile, removing one, reading or editing a profile's **persona** text and the assistant's **agent name** (display name), and switching the active profile. Subcommands: `use`, `which`, `clear`, `create`, `list`, `get`, `delete`, `persona get/set`, `agent-name get/set`. Each profile isolates its own conversations, tool overrides, and agent registrations."
---

# `cremind profile` — Profile Management & Selection

`cremind profile` is the CLI for managing Cremind profiles and for
choosing which profile the CLI acts as. A *profile* isolates a user's
conversations, tool overrides, agent registrations, persona text, and
agent name.

**You do not need to export `CREMIND_TOKEN` to use the CLI.** The active
profile is resolved from a per-profile JWT, and because the CLI runs on
the server host it reads that JWT straight from
`<CREMIND_SYSTEM_DIR>/tokens/<profile>.token`. On the **first** command in
a terminal, the CLI prompts you to pick a profile from an interactive,
type-to-filter list and remembers that choice **for that terminal**, so
later commands don't ask again. You can also select a profile directly —
without the prompt — via the root `--profile` flag or `cremind profile
use`. An explicit `CREMIND_TOKEN` in the environment (as injected into
`exec_shell` subprocesses) still takes precedence when set.

The command groups together four concerns:

- **Profile selection** — `use`, `which`, `clear`, plus the root
  `--profile` flag. Chooses which profile subsequent commands act as, per
  terminal.
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
force JSON output instead of the default tables/key-value view. Because
it is a **root** flag it must come *before* the subcommand path, not
after it:

```bash
cremind --json profile list          # correct
cremind profile list --json          # WRONG — "No such option: --json"
```

The root **`--profile <name>`** / **`-p <name>`** flag (also a root flag,
so it comes before the subcommand path) selects which profile the command
acts as and remembers it for this terminal — see *Selecting the active
profile* below.

The lifecycle/persona/agent-name subcommands need a resolved profile (via
the picker, `--profile`, a remembered selection, or `CREMIND_TOKEN`); the
selection subcommands `use`/`which`/`clear` work with no token — they only
read and write the local per-terminal selection.

## Selecting the active profile

Most commands act as "the current profile". The CLI resolves it in this
order, stopping at the first that applies:

1. `CREMIND_TOKEN` (or the root `--token`) — used verbatim if set. This is
   the path `exec_shell` uses, so agent shells are unaffected.
2. The root `--profile <name>` / `-p <name>` flag (or the
   `CREMIND_PROFILE` env var). Sticky: it is also saved as this terminal's
   active profile.
3. The profile remembered for this terminal (from a previous `--profile`,
   `profile use`, or picker choice).
4. On an interactive terminal with several profiles: a type-to-filter
   picker. With exactly one profile on disk it is chosen automatically.

If nothing resolves (e.g. a non-interactive shell with several profiles
and no selection), the command exits with a message pointing at
`--profile` / `cremind setup`.

### `cremind profile use`

**Purpose.** Set the active profile for **this terminal**, remembered
across later commands (no token needed).

**Syntax.**

```bash
cremind profile use <profile name>
```

**Behavior.** Validates that `<profile name>` has a token file under
`<CREMIND_SYSTEM_DIR>/tokens/`, records it as this terminal's active
profile, and confirms on stdout. Rejected (with the available names) if
that profile has no token file.

**Example.**

```bash
$ cremind profile use admin
active profile for this terminal: admin
```

### `cremind profile which`

**Purpose.** Print the profile remembered for this terminal.

**Syntax.**

```bash
cremind profile which
```

**Behavior.** Prints the active profile name, or exits non-zero with
`no profile selected for this terminal` if none is remembered.

**Example.**

```bash
$ cremind profile which
admin
```

### `cremind profile clear`

**Purpose.** Forget this terminal's remembered profile so the next
command re-prompts (or falls back to `--profile`/`CREMIND_TOKEN`).

**Syntax.**

```bash
cremind profile clear
```

**Behavior.** Removes this terminal's entry from the local selection
state. Silent-safe; always confirms on stdout.

**Example.**

```bash
$ cremind profile clear
cleared active profile for this terminal
```

## Subcommands

The lifecycle, persona, and agent-name subcommands follow.

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
cremind profile get <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile to inspect.

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
cremind profile create <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile name. Must not already exist.

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
cremind profile delete <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile to remove.

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
cremind profile persona get <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile whose persona should be printed.

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
new persona can be passed **inline as an argument** or read from
**standard input**, so this command composes naturally with `cat`,
redirection, and editor pipelines.

**Syntax.**

```bash
cremind profile persona set <profile name> <content>   # inline persona text
cremind profile persona set <profile name>             # reads persona from stdin
```

**Arguments.** Order matters — the profile **name comes first**, the
persona text second:

- `<profile name>` (required) — Profile whose persona is being overwritten. It
  must be an existing profile name (lowercase letters, numbers, `-`,
  `_`). A common mistake is passing the persona text here and forgetting
  the name; the server then rejects it (you can only edit your own
  profile).
- `<content>` (optional) — The persona text. If given, it is used
  verbatim (quote multi-line text). If omitted, the persona is read
  from stdin until EOF. Providing no text — an interactive terminal, an
  empty pipe, or `< /dev/null` — is an error: the command prints a usage
  hint and exits non-zero rather than storing an empty persona.

**Behavior.** Uses the `<content>` argument when present; otherwise
reads everything on stdin until EOF. Empty/blank text is rejected (to
deliberately clear a persona, pass an explicit empty argument:
`cremind profile persona set <profile name> ""`). The text is posted as the new
persona, replacing the previous one wholesale (there is no patch/append
mode). Silent on success.

**Driving this non-interactively (agents / scripts).** Two robust
paths: (1) pass the persona as the inline `<content>` argument — best
for short, simple text; or (2) use the stdin form and feed the text
through a mechanism that **closes stdin (sends EOF)** when done — this
avoids shell-quoting hazards for content with `$`, backticks, or
newlines, and is the safest path for large multi-line personas. When
run through the process tools, send the content and then close stdin
(e.g. `exec_shell_input` with `close_stdin=true`, or `cremind proc
stdin <pid> --close-stdin`); if stdin is closed with no text sent, the
command exits with the usage hint instead of hanging or storing an
empty persona.

**Examples.**

```bash
# Inline (quote multi-line text)
$ cremind profile persona set admin "You are an Cremind admin assistant. Be concise."

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
cremind profile agent-name get <profile name>
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
cremind profile agent-name set <profile name> <agent-name>
```

**Arguments** (both required):

- `<profile name>` — Profile to update.
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
$ for p in $(cremind --json profile list | jq -r '.[]'); do
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

**`persona set` errors asking for the persona text** — With no
`<content>` argument, `persona set` reads stdin; if that yields nothing
(interactive terminal, empty pipe, or `< /dev/null`) it exits with a
usage hint instead of storing an empty persona. Pass the persona as a
quoted argument, or pipe/redirect a file in with `<`.

**`persona set` returns `403` / `You can only modify your own profile`**
— The `<profile name>` argument doesn't match the profile your token grants (a
frequent cause is passing the persona *text* in the name slot and
omitting the name). Put the profile name first:
`cremind profile persona set <profile name> <text>`. Run `cremind me` to see
your profile, and `cremind profile list` for valid names. A name with
spaces/newlines/invalid characters is rejected with `400 Invalid
profile name`.

**`agent-name set` rejected** — The name must be non-empty and at most
128 characters. Trim it (or quote a name with spaces) and retry.

**Override of "the" profile vs the current profile** — Every subcommand
takes an explicit `<profile name>`; nothing in `cremind profile` implicitly targets
the active profile. To find out which profile the current token grants,
run `cremind me`.
