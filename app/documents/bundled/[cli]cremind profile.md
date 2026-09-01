---
description: "Create, list, inspect, and delete Cremind **profiles**, and **choose which profile the CLI acts as without setting `CREMIND_TOKEN`** — pick a profile interactively on first use in a terminal (a type-to-filter list), or select one directly with the root `--profile` flag or `cremind profile use`, remembered per terminal. Covers making/adding/registering a new profile, removing one, reading or editing a profile's **persona** text (who the agent is), its **standing instructions** (task directives the agent must follow in every conversation, e.g. registering new channel users in a sheet), and the assistant's **agent name** (display name), plus switching the active profile. Subcommands: `use`, `which`, `clear`, `create`, `list`, `get`, `delete`, `persona get/set`, `instructions get/set`, `agent-name get/set` — there is **no rename**: create the new profile, copy persona/instructions/agent name across, then delete the old one. Each profile isolates its own conversations, tool overrides, and agent registrations. Creating a profile is **admin-only**; deleting one is **self-only**, except the **admin** profile, which may delete any other profile and can itself never be deleted; a non-admin's `list` returns only its own profile. The root `--profile` flag picks an identity **only when no `CREMIND_TOKEN` / `--token` is set**, so inside `exec_shell` it cannot change who you act as — pass the other profile's token with `--token` instead."
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
`exec_shell` subprocesses) still takes precedence when set — and it wins
*over* `--profile`, which is then ignored, so in an agent shell that flag
cannot change who you are acting as. See *Acting as another profile*
below for what does.

The command groups together five concerns:

- **Profile selection** — `use`, `which`, `clear`, plus the root
  `--profile` flag. Chooses which profile subsequent commands act as, per
  terminal.
- **Profile lifecycle** — `list`, `get`, `create`, `delete`. Every one of
  these is scoped by the profile your token grants: `create` is
  **admin-only**, `delete` reaches only your own profile (the `admin`
  profile excepted — it may delete any other), and `list` shows a
  non-admin only its own profile. See *Who may do what* below.
- **Persona text** — `persona get`, `persona set`. The persona is a
  free-form Markdown blob prepended to the agent's system prompt for
  that profile. It describes **who the agent is**: personality, tone,
  background, what it can do.
- **Standing instructions** — `instructions get`, `instructions set`. A
  second free-form Markdown blob, injected into the same system prompt as
  its own `STANDING INSTRUCTIONS` section. It describes **what the agent
  must do**: task directives to follow in every conversation. Keeping
  these out of the persona is deliberate — persona is identity,
  instructions are standing work. Empty by default.
- **Agent name** — `agent-name get`, `agent-name set`. The display name
  the assistant goes by for that profile — shown in the chat header and
  in the `@`-mention menu when more than one profile is reachable.

Deleting a profile cascades: its conversations, tool overrides, and
skill registrations are removed in the same transaction. There is no
confirmation prompt, so be careful.

## Who may do what

The server authorizes every lifecycle call against the profile your token
grants (`cremind me` prints it), not against the name you typed:

- **Create** is **admin-only**. Under any other token `create` is refused
  with `403 Only the admin profile can create profiles.` Ask the `admin`
  profile to make the profile for you.
- **Delete** is **self-only**, with one exception: the `admin` profile may
  delete *any* other profile. Naming someone else's profile under a
  non-admin token is refused with `403 You can only modify your own
  profile ('<your profile>')`.
- **The `admin` profile can never be deleted**, not even by itself —
  the server refuses with `403 The admin profile cannot be deleted.` It is
  the profile that administers the others, so removing an installation
  means removing its data directory, not deleting `admin`.
- **List** is scoped the same way: `admin` sees every profile, everyone
  else sees only their own. See `cremind profile list` below — this is the
  one that quietly changes the output of existing scripts.
- The persona/instructions/agent-name subcommands are **self-only for
  everyone**. The `admin` exception is about administering profiles, not
  about reading them: `admin` cannot get or set another profile's persona,
  instructions, or agent name either.

## Finding this in the web UI

Every operation in this group has a control on the **Profiles** page of
the Cremind web UI:

> **Sidebar → Profiles**

The page shows one row per profile you may act on, each with a delete
button — signed in as `admin` that is *every* profile; signed in as anyone
else it is your own row alone, under a **Your Profile** heading (the same
scoping as `cremind profile list`, not a failed load). Signed in as
`admin` you will find its own row's delete button greyed out, matching the
server's refusal to delete `admin`. The **Create New Profile** form is
shown only to `admin`: it opens the setup wizard in a second tab, and the
wizard finishes through its own admin-gated endpoint (`POST
/api/config/setup`), which answers a non-admin with `403 Admin profile
required` — a different string from the CLI's, the same rule.

The same page carries the editor sections matching this command group —
**Agent name** (a single-line input matching `cremind profile agent-name
set`), **PERSONA.md** (a Markdown editor matching `cremind profile persona
set`), and **INSTRUCTIONS.md** directly below it (matching `cremind
profile instructions set`). Anything you change here is immediately
visible to `cremind profile get`.

Deleting the profile you are signed in as logs you out on the spot — its
token dies with it, so the UI drops you back on the profile selector.

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
acts as **when no `CREMIND_TOKEN` / `--token` is present**, and remembers
it for this terminal — see *Selecting the active profile* below.

The lifecycle/persona/agent-name subcommands need a resolved profile (via
the picker, `--profile`, a remembered selection, or `CREMIND_TOKEN`); the
selection subcommands `use`/`which`/`clear` work with no token — they only
read and write the local per-terminal selection.

## Selecting the active profile

Most commands act as "the current profile". The CLI resolves it in this
order, stopping at the first that applies:

1. `CREMIND_TOKEN` (or the root `--token`) — used verbatim if set, and
   then it is the *only* thing consulted: steps 2–4 never run, so
   `--profile` is accepted and silently ignored. This is the path
   `exec_shell` uses.
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

### Acting as another profile — `--token`, not `--profile`

`--profile` only ever applies at step 2 above: when there is **no**
`CREMIND_TOKEN` in the environment and no `--token` on the command line.
With a token present the CLI uses it verbatim and never consults the flag
— the command runs as whoever the token names, and passing `--profile`
changes nothing at all.

That is exactly the situation inside `exec_shell`: every agent shell is
spawned with `CREMIND_TOKEN` already set to the **acting profile's** token
(alongside `CREMIND_SYSTEM_DIR` and `CREMIND_SERVER` — `cremind
system-vars` prints the block). So re-running a refused command with
`--profile admin` there escalates nothing; it is the same command as the
same profile, and it returns the same `403`.

To act as another profile, hand the CLI **that profile's token**. Each
profile's JWT sits on disk beside the server at
`<CREMIND_SYSTEM_DIR>/tokens/<profile>.token`, and `cremind auth show`
reads one out without needing a token of its own:

```bash
$ cremind --token "$(cremind auth show --profile admin)" profile create bob
bob
```

PowerShell — what `exec_shell` spawns on Windows — takes the token from a
parenthesised sub-expression instead, which sidesteps quoting entirely:

```powershell
PS> cremind --token (cremind auth show --profile admin) profile create bob
```

The token file *is* the credential, so this is not a way around the
server's rules: if you cannot read another profile's `.token` file you
cannot act as it, and the answer is to ask whoever holds `admin`. Unlike
`--profile`, `--token` is not sticky — it authorizes the one command and
leaves this terminal's remembered profile alone.

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

**Purpose.** Print the profiles the current token may manage.

**Syntax.**

```bash
cremind profile list
```

**Behavior.** Renders a single-column table of profile names. With
`--json`, returns the JSON array exactly as the server emitted it
(typically a list of strings).

**The output is scoped to your token.** Run as `admin` it lists *every*
profile on the server; run as any other profile it lists **only that one
profile**. This is a roster of profiles you can act on, not a directory of
the server, so a one-line result under a non-admin token is the correct
answer, not a truncated one.

> **Scripts that fanned out over `profile list` need an `admin` token.**
> A loop like `for p in $(cremind --json profile list | jq -r '.[]')`
> used to walk every profile under any token; under a non-admin token it
> now walks exactly one — itself. It will not error, it will just do less,
> so re-check any automation that assumed the full roster. To keep the old
> behavior, give it `admin`'s **token**:
>
> ```bash
> cremind --token "$(cremind auth show --profile admin)" --json profile list
> ```
>
> `--profile admin` will not do it wherever `CREMIND_TOKEN` is already set,
> which includes every agent shell.

**Example.**

```bash
# As admin — the whole server
$ cremind profile list
PROFILE
admin
li
guest

# As 'li' — only its own profile
$ cremind --token "$(cremind auth show --profile li)" profile list
PROFILE
li
```

### `cremind profile get`

**Purpose.** Show a profile's persona text, standing instructions, and
agent name together. This is the equivalent of opening the profile's
detail panel in the UI.

**Syntax.**

```bash
cremind profile get <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile to inspect.

**Behavior.** Prints a header with `name` and `agent_name`, a blank
line, and the literal `--- persona ---` separator followed by the full
persona Markdown. When the profile has standing instructions, an
`--- instructions ---` section follows; it is omitted entirely when they
are empty. With `--json`, emits a single object with keys `name`,
`persona`, `instructions`, and `agent_name` (`instructions` is `""` when
unset).

**Example.**

```bash
$ cremind profile get admin
name        admin
agent_name  Ada

--- persona ---
You are an Cremind admin assistant. Prefer crisp, direct replies.

--- instructions ---
When a new user messages a channel, check the 'Active-User' sheet and
add a row for them if they are missing.
```

### `cremind profile create`

**Purpose.** Create a new profile. Newly created profiles start with
the server-default persona and agent name.

**Admin only.** Only the `admin` profile may create profiles. Under any
other token the server refuses with:

```
403 Only the admin profile can create profiles.
```

The web UI's **Create New Profile** form reaches the same rule by another
road — it opens the setup wizard, which completes through `POST
/api/config/setup` — and that gate has its own wording:

```
403 Admin profile required
```

**Syntax.**

```bash
cremind profile create <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile name. Must not already exist.

**Behavior.** Calls the server's create endpoint and, on success, prints
the new profile name on stdout (so the command is pipe-friendly). The
admin check runs *before* the name is validated, so a non-admin caller
gets the same 403 whatever name it passes.

`create` makes the profile, not its credential: no
`<CREMIND_SYSTEM_DIR>/tokens/<name>.token` file appears until the setup
wizard completes for that profile, or you mint one on the server host with
`cremind auth regenerate --local --profile <name> --yes`. Until then
nothing can act *as* the new profile.

**Example.**

```bash
$ cremind --token "$(cremind auth show --profile admin)" profile create alice
alice
```

### `cremind profile delete`

**Purpose.** Permanently delete a profile and everything scoped to it.

**Who may delete what.** You may always delete **your own** profile. The
`admin` profile may additionally delete **any other** profile. Two
refusals to expect:

```
403 You can only modify your own profile ('<your profile>').
403 The admin profile cannot be deleted.
```

The ownership check runs first, so which refusal you get depends on who
you are, not only on the name. A non-admin naming any profile but its own
gets the first — `admin` included: it never gets past ownership to hear
anything more specific. Only the `admin` token reaches the second, and it
reaches it by naming `admin` itself, which the server never allows.

**Syntax.**

```bash
cremind profile delete <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile to remove. Your own, or any non-`admin`
  profile when you are `admin`.

**Behavior.** Cascades to the profile's conversations, tool overrides,
agent OAuth tokens, and skill registrations. **There is no confirmation
prompt** — pair with a manual `cremind profile list` first if you need a
sanity check. Silent on success.

Deleting **your own** profile also invalidates the token you just used:
subsequent commands as that profile fail, and its
`<CREMIND_SYSTEM_DIR>/tokens/<profile>.token` no longer names anything
real. Switch to another profile (`cremind profile use <other>`) before
carrying on.

**Example.**

```bash
# admin tearing down someone else's profile
$ cremind --token "$(cremind auth show --profile admin)" profile delete alice

# a profile removing itself — the last command it can run
$ cremind --token "$(cremind auth show --profile alice)" profile delete alice
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

### `cremind profile instructions get`

**Purpose.** Print just the standing-instructions text — useful for
piping into a file, an editor, or a diff.

**Syntax.**

```bash
cremind profile instructions get <profile name>
```

**Arguments** (required):

- `<profile name>` — Profile whose instructions should be printed.

**Behavior.** Writes the instructions to stdout verbatim. A profile that
has never set any prints nothing (empty output is normal, not an error).
With `--json`, wraps it as `{"content": "..."}`.

**Example.**

```bash
$ cremind profile instructions get admin > admin.instructions.md
```

### `cremind profile instructions set`

**Purpose.** Replace the standing instructions for a profile in one
shot. Same inline-or-stdin shape as `persona set`.

**Syntax.**

```bash
cremind profile instructions set <profile name> <content>   # inline text
cremind profile instructions set <profile name>             # reads from stdin
```

**Arguments.** Order matters — the profile **name comes first**:

- `<profile name>` (required) — Profile whose instructions are being
  overwritten.
- `<content>` (optional) — The instructions text. If given, it is used
  verbatim (quote multi-line text). If omitted, the text is read from
  stdin until EOF. Providing no text — an interactive terminal, an empty
  pipe, or `< /dev/null` — is an error: the command prints a usage hint
  and exits non-zero rather than silently wiping the instructions.

**Behavior.** Replaces the previous instructions wholesale (no
patch/append mode). Silent on success. To deliberately clear them, pass
an explicit empty argument: `cremind profile instructions set <profile
name> ""`. The text becomes a `STANDING INSTRUCTIONS` section in the
agent's system prompt for that profile, alongside — but separate from —
the persona; it takes effect on the next run, with no restart needed.

**Driving this non-interactively (agents / scripts).** Identical to
`persona set`: prefer the inline argument for short text, or the stdin
form with an explicit EOF (`exec_shell_input` with `close_stdin=true`)
for multi-line content with `$`, backticks, or quotes.

**Examples.**

```bash
# Inline
$ cremind profile instructions set admin "Always reply in the user's language."

# From a heredoc — the typical multi-directive case
$ cremind profile instructions set admin <<'EOF'
When a new user messages one of this profile's channels, look them up in
the 'Active-User' Google Sheet. If they are not there yet, append a row
with their channel, sender id, display name, and today's date.
EOF

# Clear them
$ cremind profile instructions set admin ""
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

Two identities are in play: only `admin` can `create`, and only `alice`
can write her own persona and agent name. Each step therefore carries the
token of the profile it acts as. The `auth regenerate` line is what mints
`alice`'s token file in the first place — `create` alone does not.

```bash
$ cremind --token "$(cremind auth show --profile admin)" profile create alice
alice
$ cremind auth regenerate --local --profile alice --yes
$ ALICE="$(cremind auth show --profile alice)"
$ cremind --token "$ALICE" profile persona set alice < templates/alice.persona.md
$ cremind --token "$ALICE" profile agent-name set alice "Alice"
$ cremind --token "$ALICE" profile get alice
name        alice
agent_name  Alice

--- persona ---
You are Alice's research assistant ...
```

### Roll out a persona update across all profiles

Same two authorizations, one per stage: only `admin` can *enumerate* every
profile, and only a profile itself can *write* its own persona. Both
tokens are readable on the server host, where they live under
`<CREMIND_SYSTEM_DIR>/tokens/`:

```bash
$ ADMIN="$(cremind auth show --profile admin)"
$ for p in $(cremind --token "$ADMIN" --json profile list | jq -r '.[]'); do
    cremind --token "$(cremind auth show --profile "$p")" \
      profile persona set "$p" < templates/shared.persona.md
  done
```

Dropping either `--token` breaks it: without the first the list is just
whichever profile the ambient token names, and without the second every
write but that profile's own is refused with `403 You can only modify your
own profile`. `--profile` is not a substitute for either — wherever
`CREMIND_TOKEN` is set (every agent shell) it is ignored, and where it
does apply it is sticky, leaving the last profile remembered for the
terminal. `--token` leaves that selection untouched.

### Compare a profile's persona against a checked-in template

```bash
$ diff <(cremind profile persona get admin) templates/admin.persona.md
```

### Tear down a test profile

Run it with `admin`'s token so the delete is allowed and the closing
`list` shows the whole server:

```bash
$ ADMIN="$(cremind auth show --profile admin)"
$ cremind --token "$ADMIN" profile delete alice
$ cremind --token "$ADMIN" profile list
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
spelling — but remember it only lists profiles you may manage, so a
non-admin sees just its own name there. To check a name you did not
create, run it with `admin`'s token:

```bash
cremind --token "$(cremind auth show --profile admin)" profile list
```

**`create` returns `403` / `Only the admin profile can create
profiles`** — Profile creation is admin-only and the token you presented
is not `admin`'s. Re-run with `admin`'s token, or ask whoever holds the
`admin` profile to create it for you:

```bash
cremind --token "$(cremind auth show --profile admin)" profile create <name>
```

**Re-running with `--profile admin` will not help**: wherever
`CREMIND_TOKEN` is set (every agent shell) the flag is ignored and you get
the identical 403, so retrying it never converges. The check runs before
the name is validated, so this 403 says nothing about whether the name was
valid or already taken.

**`delete` returns `403` / `You can only modify your own profile`** —
You named someone else's profile under a non-admin token. Delete is
self-only for everyone but `admin`: either delete your own profile, or
re-run with `admin`'s token (again, `--profile admin` is ignored when a
token is already in the environment):

```bash
cremind --token "$(cremind auth show --profile admin)" profile delete <name>
```

Run `cremind me` to see which profile your token actually grants.

**`delete` returns `403` / `The admin profile cannot be deleted`** — By
design, and it applies to `admin` itself too: the `admin` profile is what
administers the others, so the server never lets it go. There is no flag
that overrides this — retiring an installation means removing its data
directory, not deleting `admin`.

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
