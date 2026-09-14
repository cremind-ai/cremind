---
description: "Create and set up a NEW Cremind profile step by step, from the terminal or from chat — make, add, register a profile (for example 'javis') with an LLM provider and model, tools, memory and channels, then mint its login token and download its configuration file: `cremind profile wizard start | status | set | skip | finish | cancel`. Use `--adopt` to configure a profile that already exists but was left bare by `cremind profile create`. This is the terminal form of the web Setup Wizard's per-profile flow. Distinct from `cremind setup` (the server's own first-run bootstrap of the admin profile) and from the rest of `cremind profile` (list, delete, persona, instructions, agent name)."
---

# `cremind profile wizard` — Set Up a Profile Step by Step

`cremind profile wizard` creates a profile the way the web UI does: it asks for
an LLM provider and model, which tools to enable, whether to turn on memory and
which channels to connect, then creates the profile, mints its login token and
writes its configuration file.

This matters because `cremind profile create` does **not** do any of that. It
registers a name and nothing else — no model (so the profile answers nothing),
no token (so nobody can sign in as it), no configuration file. A profile created
that way is a shell; `--adopt` below is how it becomes usable.

Every subcommand runs as the **admin** profile. Creating a profile is an admin
action server-side, so the wizard checks up front rather than letting you answer
four steps and hit a `403` at the end:

```bash
cremind --token "$(cremind auth show --profile admin)" profile wizard start javis
```

## How the wizard works

Answers accumulate in a **draft file** — one per profile being set up, under the
admin profile's own directory in the Cremind System Directory — and nothing
reaches the server until `finish`. That is what makes the flow survivable across
turns: you can answer the LLM step now, the channels step tomorrow, and skip
whatever you do not want to decide yet.

- **One step at a time.** `start` prints the first step's questions; each `set`
  prints the next step's.
- **Skipping is a real answer.** `skip` marks a step deliberately unanswered so
  `finish` can run. Each skip says what you give up.
- **Secrets stay out of the output.** `status` masks anything that looks like a
  key, token or password. The draft file itself is written `0600` into a
  directory the file API refuses to serve.
- **Drafts expire.** A draft untouched for seven days is treated as absent and
  deleted, because it may hold an API key.
- **One POST at the end.** `finish` sends every answer as a single
  `POST /api/config/setup`, exactly what the web wizard's last step sends.

## Driving the wizard from chat

This command family is the **exception** to the usual rule about running
state-changing commands as soon as they are asked for. Every step needs values
only the user has — which provider, whose API key, which channels — so after
`start` and after each `set`, **ask the user the questions the command printed
and end your turn**. Never invent a provider, a model or a key. The commands
print a `STOP:` line saying exactly this; `set` with no answers refuses rather
than storing nothing.

A worked run, one turn per step:

```text
user:  create a profile called javis
agent: $ cremind profile wizard start javis
       → prints the LLM step's questions, then STOP
       "Which LLM provider should javis use, and which model? If it needs an
        API key, paste it here. You can also say 'skip' and set it up later."
user:  anthropic, claude-sonnet-5, key sk-ant-...
agent: $ cremind profile wizard set javis llm provider=anthropic \
           model=claude-sonnet-5 api_key=sk-ant-...
       → prints the tools step's questions, then STOP
       "Which built-in tools should javis have?"
user:  defaults are fine
agent: $ cremind profile wizard skip javis tools
...
agent: $ cremind profile wizard finish javis
```

`finish` can run for minutes when a step enabled a tool whose packages have to
be installed. It prints a heartbeat while it waits, but a long run may still
come back as a *running process* rather than finished output: read the rest with
`exec_shell_output`, or re-print everything afterwards with

```bash
cremind --json profile wizard status javis
```

When it is done, hand the result over:

1. Give the user the **configuration file as a download**: call the
   `system_file` tool's `read_file` on the `config_file` path `finish` printed.
   Its result carries the file and the chat renders a Download chip. A Markdown
   link to that path will not open — file links carry no authorization header.
   Do not restate the file's contents.
2. Give the user the **login URL** and the **token**, verbatim. They open the
   URL in another tab and paste the token into the sign-in box. The server also
   keeps a copy at the `token_file` path.
3. Pass on anything still outstanding: channels awaiting pairing, and which
   steps were skipped.

## `cremind profile wizard start`

**Purpose.** Begin setting up a profile and print the first step's questions.

**Syntax.**

```bash
cremind profile wizard start <profile name> [--adopt]
```

**Flags.**

| Flag      | Meaning                                                                    |
|-----------|----------------------------------------------------------------------------|
| `--adopt` | Configure a profile that already exists instead of refusing.                |

**Behavior.** Refuses if the server has not finished its own first-run setup
(use `cremind setup complete` for that), if the profile exists and `--adopt` was
not given, if `--adopt` was given and the profile does not exist, or if a draft
for that profile is already open. `admin` is never accepted — it is created by
first-run setup and reconfigured with `cremind setup reconfigure`.

Adoption applies the configuration on top of the existing profile and mints a
new token; **tokens issued to it earlier stay valid** (revoking those is
`cremind auth regenerate`).

**Example.**

```bash
$ cremind profile wizard start javis --adopt
profile    javis
adopt      true
draft      /home/li/.cremind/admin/cli-wizards/javis.json
llm        pending
...
```

## `cremind profile wizard status`

**Purpose.** Show a draft's progress and what the next step needs. The one
read-only command in the group.

**Syntax.**

```bash
cremind profile wizard status <profile name> [--step llm|tools|memory|channels]
```

**Behavior.** Prints each step's state (`pending`, `set`, `skipped`), the next
step, whether the draft is ready to finish, and that step's questions. Secrets
are masked. After a `finish`, prints the finished result instead — the token,
login URL and file paths — which is how to recover them if the original output
was cut short.

## `cremind profile wizard set`

**Purpose.** Record one step's answers.

**Syntax.**

```bash
cremind profile wizard set <profile name> <step> KEY=VALUE [KEY=VALUE...]
cremind profile wizard set <profile name> <step> --json-file <path>
```

`KEY=VALUE` pairs **merge** into what the step already holds, so answers can
arrive over several turns. `--json-file` **replaces** the step outright. Reading
the payload from stdin is not supported — an idle stdin is auto-closed in an
agent shell, which would silently store nothing. `--no-validate` stores the
answers without checking them against the live catalogs.

### `set ... llm` — provider and models

| Key | Meaning |
|-----|---------|
| `provider=<name>` | Which provider. List: `cremind llm providers list` |
| `model=<id>` or `<provider>/<id>` | The main model. List: `cremind llm providers models <provider>` |
| `api_key=<key>` | The provider's credential. Other methods use their own field name (`setup_token=`, …) |
| `auth_method=<id>` | Optional; defaults to the provider's own default. Browser sign-ins are refused — finish those in Settings → LLM Providers |
| `plan_model=` `low_model=` `vision_model=` `audio_model=` | Optional extra roles |
| `reasoning_effort=<low\|medium\|high>` | Optional |

**Skipping this step** creates a profile that cannot answer anything: web chat
reports the missing model, direct messages error, and group chats are ignored
with no reply at all — until a model is chosen.

### `set ... tools` — built-in tools and skills

| Key | Meaning |
|-----|---------|
| `<tool_id>.enabled=true\|false` | Turn a tool on or off. List: `cremind tools list` |
| `<tool_id>.<VAR>=<value>` | A tool variable (API keys, endpoints) |
| `<tool_id>._arg.<name>=<json>` | A tool argument |
| `<tool_id>._description=<text>` | Override what the agent is told the tool is for |

Skills appear in the admin catalogue under an `admin__` prefix; they are
remapped to the new profile's own copy at `finish`. Skipping leaves the server's
defaults.

### `set ... memory` — long-term memory

| Key | Meaning |
|-----|---------|
| `enabled=true\|false` | Whether the agent remembers facts across conversations |
| `<group>.<key>=<value>` | Any other per-profile setting. List: `cremind config schema` |

Skipping leaves memory off.

### `set ... channels` — messaging channels

One channel per `set` call; naming the same platform twice replaces the first.

| Key | Meaning |
|-----|---------|
| `channel_type=<type>` | The platform. List: `cremind channels catalog` |
| `mode=<mode>` | `bot`, `userbot`, `notification`… Defaults to the platform's first |
| `<field>=<value>` | The mode's own fields (`bot_token`, `api_id`, …) |
| `response_mode=normal\|detail` | Optional |
| `group_chats=true\|false` | Optional |
| `subscribe_auth=open\|passcode\|otp\|approval\|allowlist` | Optional; defaults to `open` |

Modes that need a QR scan or a verification code cannot be completed here;
`finish` prints the `cremind channels pair` command to run afterwards.

## `cremind profile wizard skip`

**Purpose.** Mark a step deliberately unanswered so `finish` can run.

**Syntax.**

```bash
cremind profile wizard skip <profile name> <step>
```

**Behavior.** Clears anything already stored for that step and prints what
skipping it costs (see each step above).

## `cremind profile wizard finish`

**Purpose.** Create the profile, mint its token, and write its configuration
file.

**Syntax.**

```bash
cremind profile wizard finish <profile name> [--format md|json|env] [--out <path>]
                              [--no-file] [--agent-url <url>] [--pending-https]
```

**Flags.**

| Flag              | Meaning                                                                 |
|-------------------|--------------------------------------------------------------------------|
| `--format`, `-f`  | Configuration file format: `md` (default), `json`, `env`.                |
| `--out`           | Where to write it. Default: the admin profile's `exports` directory.     |
| `--no-file`       | Do not write the configuration file.                                     |
| `--agent-url`     | The address the login URL should use, when `APP_URL` is not what a browser reaches. |
| `--pending-https` | Mark that address as the HTTPS origin that answers after a restart.      |

**Behavior.** Refuses while any step is still `pending`, and refuses a `set` LLM
step that has no main model (skip it deliberately instead). Then it posts every
answer at once, saves the new profile's token file on this host, writes the
configuration file, and deletes the draft.

Prints `profile`, `expires_at`, `token`, `login_url`, `token_file`,
`config_file`, the steps that were skipped, and any channels created. Warnings —
a channel that failed to register, a feature that needs a restart, a missing
model — go to stderr, followed by the hand-off steps for an agent.

**Example.**

```bash
$ cremind profile wizard finish javis
profile      javis
expires_at   2026-10-14T09:12:33Z
token        eyJhbGciOi...
login_url    http://localhost:1515/#/login/javis
token_file   /home/li/.cremind/tokens/javis.token
config_file  /home/li/.cremind/admin/exports/cremind-javis-config.md
skipped      tools, channels
```

## `cremind profile wizard cancel`

**Purpose.** Discard a draft. Creates nothing and changes no profile.

**Syntax.**

```bash
cremind profile wizard cancel <profile name>
```

## Filling in a skipped step later

A skipped step is filled in as the **new profile**, not as admin — the per-setting
endpoints are all scoped to the token that calls them:

```bash
NEW="$(cremind auth show --profile javis)"
cremind --token "$NEW" llm providers configure anthropic --api-key sk-ant-...
cremind --token "$NEW" llm model-groups set --high anthropic/claude-sonnet-5
cremind --token "$NEW" tools enable web_search
cremind --token "$NEW" config set memory.enabled true
cremind --token "$NEW" channels add --type telegram --mode bot --config bot_token=...
```

## Finding this in the web UI

The same flow is **Sidebar → Settings → Profiles → Create New Profile**, which
opens the Setup Wizard for the new profile in a second tab. Its last step offers
the same configuration file, and **Sidebar → Settings → Profiles → Configuration
File** re-downloads it later.

## Troubleshooting

**`profile 'x' already exists`** — it was created bare (`cremind profile
create`, or `POST /api/profiles`). Re-run `start` with `--adopt`.

**`the profile wizard runs as admin`** — the token in the environment belongs to
another profile. Inside an agent shell `--profile` cannot change that; pass
admin's token with `--token "$(cremind auth show --profile admin)"`.

**`nothing to set for step ...`** — `set` was called with no `KEY=VALUE` pairs.
That is deliberate: it means the questions have not been put to the user yet.

**The login URL does not open** — the server's `APP_URL` is not the address a
browser uses. Common on a split-origin development setup, where the SPA is
served by Vite on another port. Re-run `finish` (or `cremind config export`)
with `--agent-url <the address you actually open>`.

**`finish` came back as a running process** — a tool needed packages installed.
Read the rest with `exec_shell_output`, or `cremind --json profile wizard status
<name>` once it is done.

**The profile cannot answer anything** — the LLM step was skipped, or no main
model was chosen. Set one under Settings → LLM Providers, or with
`cremind --token "$(cremind auth show --profile <name>)" llm model-groups set`.
