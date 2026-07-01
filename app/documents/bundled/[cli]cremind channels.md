---
description: "Connect and manage external **messaging channels** such as Telegram: `list` connected channels, `add` one from a JSON config, run the interactive `pair` flow (QR code in the terminal, or a Telegram verification code and 2FA password), `delete` a channel and cascade-remove its conversations, and dump the `catalog` of supported platforms. Use this to link a Telegram bot or other chat platform to Cremind; the auto-created `*main*` channel cannot be removed."
---

# `cremind channels` — External Messaging Channel Management

`cremind channels` is the CLI for managing Cremind's external messaging
channels — Telegram, WhatsApp, Discord, Messenger, and Slack — that
let users on those platforms talk to your Cremind agent. Each channel
is a row in the per-profile `channels` table; conversations created
from inbound messages on that channel are linked back to it via a
foreign key, and the Cremind web UI shows them filtered into the
sidebar's channel selector.

The group covers four operations:

- **`list`** — Show every configured channel for the active profile,
  with live runtime status pulled from the in-process registry.
- **`add`** — Register a new channel by picking a `--type`, a `--mode`
  (bot vs user account), and a JSON blob of platform-specific config
  (e.g. `{"bot_token":"…"}` for Telegram).
- **`delete`** — Tear down the adapter and remove the row. **Cascades
  delete to every conversation that belonged to that channel and
  every per-sender authentication state.**
- **`catalog`** — Dump the TOML-driven catalog (one entry per
  supported channel type, each describing which modes, which auth
  modes, and which config fields the channel needs). The web UI's
  "Add Channel" form is built from the same data.

The `main` channel — the implicit channel that web UI and CLI
conversations belong to — is always present, auto-created on profile
creation, and is **not** listed by `cremind channels list`. You can't
register a second `main`, and you can't delete the existing one.

## Channel data model

Each row returned by `list` looks like:

| Field              | Meaning                                                                                                                  |
|--------------------|--------------------------------------------------------------------------------------------------------------------------|
| `id`               | UUID of the channel row. Used by `delete`, the API's `PATCH /api/channels/{id}`, and the conversation FK.                |
| `channel_type`     | `telegram` \| `whatsapp` \| `discord` \| `messenger` \| `slack`. Unique per profile (you can't register two Telegrams).  |
| `mode`             | `bot` (a separate bot account replies — Telegram bot, Discord bot, Messenger Page bot, Slack bot) or `userbot` (your own user account auto-replies on your behalf — WhatsApp today, Telegram/Discord/Messenger/Slack scaffolded as "coming soon" in the catalog). |
| `auth_mode`        | `none` \| `otp` \| `password`. Which gate to apply per inbound sender before the agent runs.                              |
| `response_mode`    | `normal` (final answer only) or `detail` (also stream Thinking-Process step bubbles).                                    |
| `enabled`          | `true`/`false`. Disabling stops the in-process adapter without deleting the row.                                          |
| `status`           | `running` / `stopped` — derived live from the registry, not stored.                                                       |
| `config`           | Platform-specific. Secret fields (`bot_token`, `password`, etc.) are redacted in `list`/`get` responses.                  |
| `state`            | Adapter scratch — last polled update id, last error message, etc.                                                         |
| `created_at`, `updated_at` | Unix-ms timestamps.                                                                                              |

The list of which fields are *secret* per channel type comes from the
TOML catalog (`cremind channels catalog`); the API redacts those keys to
`***` in any list/get response.

## Read-only contract

External channels are **inbound-only from the platform's user**:

- A user on Telegram messages your bot → the inbound message becomes a
  user message on a per-sender conversation under that channel.
- The Cremind agent runs and the response is sent back through the
  channel adapter to the platform user.
- You **cannot** post messages from the web UI or CLI into a non-`main`
  conversation — `POST /api/conversations/{id}/messages` returns 403
  `Read-only channel` for any conversation whose `channel_id` resolves
  to a channel with `channel_type != "main"`.

Use `cremind conv get <id>` and `cremind conv attach <id>` to inspect channel
conversations; use the corresponding platform (Telegram, etc.) to
talk to the agent.

## Finding this in the web UI

Every operation in this group has a control on the **Channels** page
of the Cremind web UI:

> **Sidebar → Channels** (live management view) — or
> **Settings → Channels** (registration form).

The Settings page exposes the **Add Channel** flow (mirroring `cremind
channels add`); the sidebar Channels page lists channels with their
runtime status and per-sender authentication state (mirroring `cremind
channels list` plus the `senders` API the CLI doesn't expose). The
sidebar's conversation-list channel selector mirrors `cremind conv list
--channel <type>`.

## Global flags

All `cremind channels` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind channels list`

**Purpose.** Show every external channel registered for the active
profile, with live runtime status.

**Syntax.**

```bash
cremind channels list
```

**Behavior.** Renders a seven-column table:

| Column     | Source           | Meaning                                                |
|------------|------------------|--------------------------------------------------------|
| `ID`       | `id`             | Channel row UUID (used by `delete`).                   |
| `TYPE`     | `channel_type`   | `telegram` / `whatsapp` / etc.                         |
| `MODE`     | `mode`           | `bot` or `normal`.                                     |
| `AUTH`     | `auth_mode`      | `none` / `otp` / `password`.                           |
| `REPLY`    | `response_mode`  | `normal` (final answer) / `detail` (with thinking).    |
| `ENABLED`  | `enabled`        | `true`/`false`.                                        |
| `STATUS`   | (live)           | `running` / `stopped` — derived from the registry.     |

Secret config fields are not shown in this table; use `--json` if you
need the full row (with secrets still redacted to `***`).

The `main` channel is intentionally hidden from this command — it is
not user-manageable.

**Example.**

```bash
$ cremind channels list
ID                                     TYPE      MODE  AUTH  REPLY    ENABLED  STATUS
e2e8...d4f1                            telegram  bot   none  detail   true     running
```

### `cremind channels add`

**Purpose.** Register a new external messaging channel and optionally
start its adapter.

**Syntax.**

```bash
cremind channels add --type <kind>
                 [--mode bot|userbot]
                 [--auth-mode none|otp|password]
                 [--response-mode normal|detail]
                 [--enabled true|false]
                 [--no-pair]
                 [--json '<config-object>' | --config key=value ...]
```

**Behavior.** POSTs to `/api/channels`. The server validates that
`channel_type` is unique for the profile, that `mode` is one of the
catalog's declared modes, that `auth_mode` is one of the catalog's
declared auth modes, that `response_mode` is `detail` or `normal`,
and that all `required` fields for the chosen mode are present in
the supplied config (whether passed as `--json` or `--config`). On
success, the adapter is started in-process (long-poll loop for
Telegram, etc.) and the new row is printed.

When successful, prints a key/value summary of the row (id, type,
mode, auth_mode, response_mode, enabled, status).

**Auto-pairing.** When the chosen mode declares an interactive setup
(`setup_kind` set in the catalog — e.g. WhatsApp QR scan, Telegram
userbot code + 2FA), `add` drops directly into the same flow `cremind
channels pair <id>` runs. The QR is rendered in the terminal, or the
prompt waits for the verification code. Pass `--no-pair` to skip; the
root `--json` flag also suppresses auto-pairing because it implies a
non-interactive caller. The auto-pair step is also skipped when
`--enabled=false` (no live adapter to pair with yet — re-enable from
the web UI or run `cremind channels pair <id>` after enabling).

**Flags.**

| Flag              | Type    | Default   | Meaning                                                                            |
|-------------------|---------|-----------|------------------------------------------------------------------------------------|
| `--type`          | string  | (required)| Channel type. Must match an entry in `cremind channels catalog`.                       |
| `--mode`          | string  | `bot`     | Adapter mode (`bot` or `userbot`). Catalog-declared modes only; modes flagged `implemented = false` are rejected. |
| `--auth-mode`     | string  | `none`    | Per-sender gate.                                                                   |
| `--response-mode` | string  | `normal`  | Reply detail (`normal` or `detail`).                                               |
| `--enabled`       | bool    | `true`    | Start the adapter immediately.                                                     |
| `--json`          | string  | `""`      | Channel-specific config as a JSON object. Mutually exclusive with `--config`. **PowerShell caveat:** Windows PowerShell strips inner double quotes when passing arguments to native binaries, so `--json '{"k":"v"}'` arrives as `--json {k:v}` and fails to parse — prefer `--config k=v` on PS, or escape with backticks / the `--%` stop-parsing token. |
| `--config`        | string (repeatable) | (none) | Channel-specific config as `key=value`, repeatable for multiple fields. Values are passed to the server as strings. Mutually exclusive with `--json`. Quoting-safe across PowerShell, cmd.exe, bash, and zsh. |
| `--no-pair`       | bool    | `false`   | Skip the auto-launched pairing flow even when the chosen mode would warrant one.   |

**Examples.**

```bash
# Register a Telegram bot
$ cremind channels add --type telegram --mode bot \
                   --response-mode detail \
                   --json '{"bot_token":"123:abc..."}'

# Register a Telegram userbot (your own account auto-replies)
# Prereq: get api_id + api_hash from https://my.telegram.org/auth.
# After `add`, open the web UI's Channels page; the pairing dialog will
# prompt for the verification code Telegram sent through the Telegram
# app itself, plus the cloud password if 2FA is enabled.
$ cremind channels add --type telegram --mode userbot \
                   --auth-mode otp \
                   --json '{"api_id":"12345","api_hash":"abcdef","phone":"+14155551212"}'
id              e2e8...d4f1
channel_type    telegram
mode            bot
auth_mode       none
response_mode   detail
enabled         true
status          running

# Register a Telegram bot but don't start it yet
$ cremind channels add --type telegram --mode bot \
                   --enabled=false \
                   --json '{"bot_token":"123:abc..."}'

# WhatsApp with a password gate (mode is `userbot` — the agent auto-replies
# as your own WhatsApp account). The QR is rendered straight to the terminal
# via `mdp/qrterminal`; scan it with WhatsApp → Linked Devices.
# Prereq: Node 18+ on PATH and `npm install` already run inside
# `app/channels/sidecars/whatsapp/`.
$ cremind channels add --type whatsapp --mode userbot \
                   --auth-mode password \
                   --json '{"phone":"+14155551212","password":"hunter2"}'
id              <whatsapp-id>
...
This channel needs interactive pairing — starting the pairing flow.
(re-run later with `cremind channels pair <whatsapp-id>`, or pass --no-pair to skip)

Open WhatsApp → Settings → Linked Devices → Link a Device, then scan:

  ▄▄▄▄▄▄▄ ▄▄▄ ▄ ▄ ▄▄▄▄▄▄▄
  █ ▄▄▄ █ ▀█ ██▀██ █ ▄▄▄ █
  …  (rest of the QR)
✓ Paired successfully.

# Same flow but skip auto-pair (e.g. you'll scan from another machine)
$ cremind channels add --type whatsapp --mode userbot \
                   --no-pair \
                   --json '{"phone":"+14155551212"}'

# WhatsApp under Windows PowerShell — use --config to dodge PS's quote stripping
PS> cremind channels add --type whatsapp --mode userbot --auth-mode otp `
                     --config phone=+84986664411
```

### `cremind channels pair`

**Purpose.** Run the interactive pairing flow for a channel directly
in the terminal — render WhatsApp's linked-device QR (as Unicode block
characters), or prompt for Telegram userbot's verification code and
2FA cloud password.

**Syntax.**

```bash
cremind channels pair <id>
```

**Behavior.** Subscribes to the channel's auth-events SSE stream
(`/api/channels/{id}/auth-events`) and dispatches per event kind:

| Event              | Terminal behaviour                                                                                                                                       |
|--------------------|----------------------------------------------------------------------------------------------------------------------------------------------------------|
| `qr`               | Clears the screen and re-renders the QR via `mdp/qrterminal` (half-block style). Each new QR replaces the previous one — Baileys rotates them every ~20s.|
| `code_required`    | Prints the phone hint and reads a single line from stdin, POSTed back as `{code: ...}`.                                                                  |
| `password_required`| Reads from stdin **without echo** (via `golang.org/x/term`), POSTed back as `{password: ...}`.                                                            |
| `ready`            | Prints `✓ Paired successfully.` and exits cleanly.                                                                                                       |
| `disconnected`     | Logs the disconnect; if `logged_out=true`, exits (the session was unlinked remotely and needs a fresh pair). Otherwise waits for reconnect.              |
| `error`            | Prints the error to stderr; the loop continues.                                                                                                          |

With root-level `--json`, every SSE frame is printed verbatim instead
of the interactive UI — useful for scripting against the same stream
without re-implementing the parser. The command still exits on `ready`
in JSON mode.

**Prerequisites.** Same as the channel itself — for WhatsApp, Node 18+
and `npm install` already run inside `app/channels/sidecars/whatsapp/`.
For Telegram userbot, the `api_id` / `api_hash` / `phone` config fields
must be set on the channel before `pair` is run.

**Examples.**

```bash
# Stand up a WhatsApp channel and pair it from the terminal in one go
$ cremind channels add --type whatsapp --mode userbot --auth-mode otp \
                   --json '{}' --enabled false
id              <whatsapp-id>
...
$ cremind channels pair <whatsapp-id>
Open WhatsApp → Settings → Linked Devices → Link a Device, then scan:

  ▄▄▄▄▄▄▄ ▄▄▄ ▄ ▄ ▄▄▄▄▄▄▄
  █ ▄▄▄ █ ▀█ ██▀██ █ ▄▄▄ █
  …  (rest of the QR)
✓ Paired successfully.

# Telegram userbot from the CLI
$ cremind channels pair <telegram-userbot-id>
Telegram sent a verification code to +14155551212.
Code: 12345
Password:           # echoed if 2FA, hidden as you type
✓ Paired successfully.
```

**Aborting.** Ctrl-C closes the SSE connection and exits with a
non-zero status. The adapter on the server keeps running — re-invoke
`cremind channels pair <id>` (or open the web UI dialog) to resume the
flow from wherever it stalled.

### `cremind channels delete`

**Purpose.** Stop a channel's adapter and delete the row. **Cascade
deletes every conversation that lived on that channel and every
per-sender row.**

**Syntax.**

```bash
cremind channels delete <id>
```

**Arguments** (required):

- `<id>` — Channel UUID (from `cremind channels list`).

**Behavior.** Refuses to delete the `main` channel. Otherwise:

1. Stops the adapter (drains long-poll, closes platform connections).
2. Deletes the `channels` row, which cascades to:
   - `conversations` rows whose `channel_id` matched (and their `messages`).
   - `channel_senders` rows for that channel (auth state is gone).

Silent on success.

**Example.**

```bash
$ cremind channels delete e2e8...d4f1
$ cremind channels list      # gone
```

### `cremind channels catalog`

**Purpose.** Dump the dynamic, TOML-driven channel catalog. This is
what the web UI's "Add Channel" form is built from.

**Syntax.**

```bash
cremind channels catalog
```

**Behavior.** Returns the merged catalog object — one entry per
channel type, each describing the platform's display name, supported
modes, supported auth modes, default response mode, and the field
schema for each mode (with which fields are secret and which are
required).

`--json` emits the same JSON unindented; the default mode prints it
pretty-printed.

**Example.**

```bash
$ cremind channels catalog
{
  "telegram": {
    "channel": {
      "type": "telegram",
      "display_name": "Telegram",
      "icon": "mdi:telegram",
      "supports_bot": true,
      "supports_normal": false,
      "auth_modes": ["none"],
      "default_response_mode": "normal",
      "modes": [
        {
          "id": "bot",
          "label": "Bot",
          "instructions": "Open Telegram → @BotFather → /newbot ...",
          "fields": {
            "bot_token": {
              "description": "Bot API Token",
              "type": "string",
              "secret": true,
              "required": true
            }
          }
        }
      ]
    }
  },
  ...
}
```

The catalog source is `app/config/channels/*.toml`. To add a new
channel type or change its registration form, drop a new TOML file
there — no code change needed for the catalog itself; only the
adapter implementation has to land.

## Filtering conversations by channel

`cremind conv list --channel <type>` filters the conversation list by
channel type. The most common case is rebuilding the sidebar's view
by-channel from the terminal:

```bash
$ cremind conv list --channel main         # web/CLI conversations
$ cremind conv list --channel telegram     # only Telegram-sourced ones
```

The `CHANNEL` column on `cremind conv list` shows the channel id (use
`cremind channels list` to map id → type).

## Worked examples

### Stand up a Telegram bot end-to-end

```bash
# 1. Talk to @BotFather on Telegram → /newbot → copy the API token.
$ TOKEN="123456:abc..."

# 2. Register and start the adapter.
$ cremind channels add --type telegram --mode bot \
                   --response-mode detail \
                   --json "{\"bot_token\":\"$TOKEN\"}"

# 3. Confirm it's running.
$ cremind channels list
ID         TYPE      MODE  AUTH  REPLY    ENABLED  STATUS
e2e8...    telegram  bot   none  detail   true     running

# 4. Send a DM to the bot from your phone, then watch the new
#    conversation appear under the Telegram channel filter.
$ cremind conv list --channel telegram
ID         TITLE                CHANNEL          CREATED_AT  TASK_ID
c_92bc     Lee Nguyen           e2e8...d4f1      ...

# 5. Replay the agent's reasoning trace for that conversation.
$ cremind conv get c_92bc --detail
```

### Pause a channel without losing it

```bash
$ cremind channels list
ID                                     TYPE      MODE  AUTH  REPLY    ENABLED  STATUS
e2e8...d4f1                            telegram  bot   none  detail   true     running

# Stop the adapter but keep the registration. (PATCH via the API —
# the CLI doesn't have a `disable` subcommand yet; use the web UI
# Channels page → toggle "Enabled".)
```

### Move from a stuck Telegram bot back to a clean state

```bash
# Drop the channel — this cascades all its conversations away.
$ cremind channels delete e2e8...d4f1

# Re-register with the same token.
$ cremind channels add --type telegram --mode bot \
                   --json '{"bot_token":"123:abc..."}'
```

### Dump the catalog through `jq`

```bash
$ cremind channels catalog --json | jq '.telegram.channel.modes[].fields'
{
  "bot_token": {
    "description": "Bot API Token",
    "type": "string",
    "secret": true,
    "required": true
  }
}
```

## Troubleshooting

**`add` returns `Channel 'telegram' is already registered for this profile`** —
There's already a row of that type. `cremind channels list` to find it,
then either reuse it (PATCH the config from the web UI) or
`cremind channels delete <id>` first. Each profile is hard-capped to one
channel per type.

**`add` succeeds but `STATUS` is `stopped`** — The adapter raised
during startup. Check `state.last_error` via `cremind channels list
--json`; common causes are an invalid `bot_token`, a Telegram userbot
waiting for the verification code in the pairing dialog (status flips
to `running` once `ready` fires), a missing `node_modules/` directory
under `app/channels/sidecars/whatsapp/` (run `npm install` once),
Node not on PATH, or a platform adapter that's still a stub (`discord`,
`messenger`, and `slack` raise `ChannelNotImplemented` on first
start in v1).

**Telegram userbot keeps prompting for the code** — Either the code
expired (Telegram codes are short-lived; the dialog will say "Code
expired; a new one was sent") or the digits typed don't match. Pull
the latest code straight from the Telegram app on the phone you're
pairing with. If 2FA is enabled, after the code succeeds the dialog
asks for the cloud password (the password you set under
``Telegram → Settings → Privacy and Security → Two-Step Verification``).

**Trying to send a message into a channel conversation from `cremind conv send`** —
Returns `403 Read-only channel`. External channels are inbound-only
from the platform side; the agent's reply is forwarded automatically
through the adapter. Use the corresponding platform (Telegram, etc.)
to talk to the agent.

**Telegram replies are blank or truncated after a long agent run** —
The adapter chunks long replies on paragraph boundaries to stay under
Telegram's 4096-char cap, retries each chunk on transient
`NetworkError`, and falls back to plain text when Markdown parsing
fails. If a single chunk still drops, the cause is logged at
`telegram: send failed (attempt N/4); resetting connection pool` —
copy that line into a bug.

**`cremind channels delete` deleted my conversations too** — That is the
documented behaviour: a channel deletion cascades to its conversations
and per-sender rows. If you only want to pause a channel, toggle
`enabled=false` from the web UI's Channels page instead.
