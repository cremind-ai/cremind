---
description: "Connect and manage external **messaging channels** — Telegram, WhatsApp, Discord, Slack, Messenger, and Zalo: `list` connected channels, `add` one from a JSON config, `edit` a channel's settings, `enable`/`disable` it, list its `senders`, run the interactive `pair` flow (QR code in the terminal, or a Telegram verification code and 2FA password), set a channel's push-notification filter with `notify-filter`, push an ad-hoc message out to a notification channel with `send`, `approve`/`revoke` who may subscribe to a notification channel, `delete` a channel and cascade-remove its conversations, and dump the `catalog` of supported platforms. Channels can run in conversational `bot`/`userbot` mode or a push-only `notification` mode that forwards Cremind's automation/event alerts to a chat with a configurable filter (importance, kind, source, specific automation/conversation, keyword, quiet hours). All channels gate access with the same per-channel **authentication** method — open, passcode, one-time code (`otp`), admin approval, or allowlist — controlling who may chat (bot/userbot) or subscribe (notification); `approve`/`revoke` authorize individual senders and work in every mode. A notification channel can also receive one-off messages you send with `cremind channels send` — the same delivery the agent's `send_notification` tool uses when you ask it to 'notify me on Telegram'. Zalo offers both an official Bot API mode and a QR-paired personal-account mode; Messenger requires a publicly-reachable HTTPS host for its webhook. Use this to link a Telegram/Discord/Slack bot or other chat platform to Cremind; the auto-created `*main*` channel cannot be removed."
---

# `cremind channels` — External Messaging Channel Management

`cremind channels` is the CLI for managing Cremind's external messaging
channels — Telegram, WhatsApp, Discord, Slack, Messenger, and Zalo — that
let users on those platforms talk to your Cremind agent. Each channel
is a row in the per-profile `channels` table; conversations created
from inbound messages on that channel are linked back to it via a
foreign key, and the Cremind web UI shows them filtered into the
sidebar's channel selector.

The group covers these operations:

- **`list`** — Show every configured channel for the active profile,
  with live runtime status pulled from the in-process registry.
- **`add`** — Register a new channel by picking a `--type`, a `--mode`
  (`bot`/`userbot` for conversation, or `notification` for push-only
  alerts), and a JSON blob of platform-specific config
  (e.g. `{"bot_token":"…"}` for Telegram).
- **`pair`** — Run the interactive pairing flow (WhatsApp QR, Telegram
  userbot code + 2FA) in the terminal.
- **`notify-filter`** — Show or set the notification filter of a
  `notification`-mode channel (see **Notification mode** below).
- **`send`** — Push a one-off, ad-hoc message out to a
  `notification`-mode channel's subscribers (see **Notification mode**
  below). This is the manual counterpart to the agent's
  `send_notification` tool.
- **`approve`** / **`revoke`** — Approve a pending subscriber (or revoke an
  existing one) on a `notification`-mode channel — the operator side of the
  `approval` subscription-auth method (see **Notification mode** below).
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
| `channel_type`     | `telegram` \| `whatsapp` \| `discord` \| `slack` \| `messenger` \| `zalo`. Unique per profile (you can't register two Telegrams).  |
| `mode`             | `bot` (a separate bot account replies — Telegram/Discord/Slack/Zalo bot, Messenger Page bot), `userbot` (your own account auto-replies — WhatsApp and Zalo personal via QR pairing), or `notification` (push-only: no conversation; forwards Cremind's automation/event notifications to the chat with a configurable filter). |
| `auth_mode`        | **Legacy** per-sender gate (`none` \| `otp` \| `password`), superseded by the unified `config.subscribe_auth` (see **Access authentication** below). Still read for back-compat on channels created before unification (`password`→`passcode`, `otp`→`otp`, `none`→`open`); new channels set `subscribe_auth` and leave this `none`. |
| `response_mode`    | `normal` (final answer only) or `detail` (also stream Thinking-Process step bubbles).                                    |
| `enabled`          | `true`/`false`. Disabling stops the in-process adapter without deleting the row.                                          |
| `status`           | `running` / `stopped` — derived live from the registry, not stored.                                                       |
| `config`           | Platform-specific. Secret fields (`bot_token`, `password`, etc.) are redacted in `list`/`get` responses.                  |
| `state`            | Adapter scratch — last polled update id, last error message, etc.                                                         |
| `created_at`, `updated_at` | Unix-ms timestamps.                                                                                              |

The list of which fields are *secret* per channel type comes from the
TOML catalog (`cremind channels catalog`); the API redacts those keys to
`***` in any list/get response.

## Access authentication (all modes)

Every channel — conversational (`bot`/`userbot`) and `notification` — gates who
may use it with the **same** per-channel setting, `config.subscribe_auth`, chosen
in the web UI's **Authentication** dropdown or via
`cremind channels edit <id> --config subscribe_auth=<method>`:

| method | conversational (bot/userbot) — who may chat | notification — who may subscribe |
|---|---|---|
| `open` *(default)* | anyone who messages | anyone who `/start`s |
| `passcode` | sender sends the passcode once to unlock (`config.subscribe_passcode`) | `/start <passcode>` |
| `otp` | server code shown in your web-UI bell; the sender echoes it | same |
| `approval` | first message is held; sender told "pending", you're notified; the agent replies only after you approve | `/start` creates a pending subscriber you approve |
| `allowlist` | only approved senders may chat; unknown senders get a flat refusal | no self-subscribe; only `config.target_chat_ids` receive |

For `approval`/`allowlist` you authorize a sender with **`cremind channels approve
<id> <sender>`** (or the Approve button on the Channels page); `revoke` reverses
it. Both work for any mode. Back-compat: the legacy conversational `auth_mode`
(`none`/`otp`/`password`) and `config.password` are read automatically when
`subscribe_auth` is unset, so channels created before unification keep their gate
(`password`→`passcode`, `otp`→`otp`, `none`→`open`).

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

This inbound-only rule is about *conversations*. A `notification`-mode
channel has no conversation at all — it is a push-only feed — so it is
the one case where an *operator-initiated* outbound push is allowed, via
`cremind channels send` (or the agent's `send_notification` tool). That
path delivers straight to subscribers and never creates or writes a
conversation.

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
| `MODE`     | `mode`           | `bot` / `userbot` / `notification`.                    |
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
                 [--mode bot|userbot|notification]
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

**On-demand SDK install.** Telegram, Discord, and Slack ship their Python
SDKs (`python-telegram-bot`, `telethon`, `discord.py`, `slack-bolt`) as
optional extras that stay off disk until needed. Enabling one of those
channels — via `add`, `enable`, or `edit` — installs its package at runtime
before the adapter starts, the same way built-in tools like `browser` do.
The adapters import lazily, so the channel comes up **without a server
restart**; the first connect just takes a little longer while pip runs. If
the install fails (offline host, etc.), the channel is left `enabled=false`
with the reason in `state.last_error`. Messenger and Zalo (bot) need no extra
(they use the core HTTP client); WhatsApp and the Zalo personal channel use a
Node.js sidecar (`npm`) instead.

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
| `--mode`          | string  | `bot`     | Adapter mode (`bot`, `userbot`, or `notification`). Catalog-declared modes only; modes flagged `implemented = false` are rejected. |
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

# Register a Telegram NOTIFICATION channel (push-only alerts, no conversation).
# The default filter forwards everything except the noisy "started" pings and
# OTP codes. After `add`, DM the bot /start to subscribe.
$ cremind channels add --type telegram --mode notification \
                   --json '{"bot_token":"123:abc...","notification_filter":{"min_priority":"all"}}'
```

### Notification mode

`--mode notification` turns a channel into a **push-only alert feed**: it holds
no conversation and never dispatches to the agent. Instead it subscribes to the
profile's notification stream — the same automation/event activity the web UI
shows (schedule / file-watcher / skill-event runs, run errors, pending prompts) —
and forwards entries that pass a **filter** to the chat.

**Transports.** Telegram notification runs over a normal bot (BotFather token —
no account login). WhatsApp notification runs over your linked WhatsApp account
(same QR pairing as its userbot mode).

**Subscribing (recipients).** A bot can't message someone who hasn't started it,
so recipients opt in:

- Send `/start` to the bot (or, on WhatsApp, message the linked account) to
  **subscribe**; `/stop` to unsubscribe. Subscriptions are stored as
  `channel_senders` rows and survive restarts.
- Or set `target_chat_ids` in config (comma-separated chat ids / JIDs) to push
  to a known group/channel without anyone having to `/start`.

**Subscription authentication.** Who may subscribe is controlled per-channel by
`config.subscribe_auth` (the web UI's **Subscription authentication** dropdown, or
`cremind channels edit <id> --config subscribe_auth=<method>`). Without it a
stranger who finds the bot can `/start` and receive your notifications, so pick a
method other than `open` for anything sensitive:

- `open` *(default)* — anyone who sends `/start` subscribes. Backward-compatible:
  leaving `subscribe_auth` unset behaves this way, except a channel that only set
  `subscribe_passcode` (before this setting existed) still behaves as `passcode`.
- `passcode` — the sender must send `/start <passcode>` matching
  `config.subscribe_passcode`.
- `otp` — `/start` makes Cremind generate a one-time code shown to you in the web
  UI's notification bell; you share it out-of-band and the sender replies with it
  to subscribe. Codes expire after 10 minutes.
- `approval` — `/start` creates a **pending** subscriber and notifies you; they
  receive nothing until you approve them (`cremind channels approve <id> <sender>`
  or the **Approve** button on the Channels page). `revoke` reverses it.
- `allowlist` — self-subscribe is refused; only `config.target_chat_ids` receive
  (any previously-approved self-subscribers are excluded too).

**One channel per platform.** A profile can register only one Telegram (and one
WhatsApp) channel, so choosing `notification` **replaces** conversational
`bot`/`userbot` on that platform. To have both conversation and alerts, use two
different platforms (e.g. chat on Telegram, alerts on WhatsApp).

**The filter** lives in `config.notification_filter` and is validated/normalized
server-side (invalid → HTTP 400). All fields optional; a notification is
delivered only if it matches **every** set dimension (empty list = no constraint
on that dimension):

| Field              | Meaning                                                                                     |
|--------------------|---------------------------------------------------------------------------------------------|
| `min_priority`     | `all` (default) or `high` — deliver only high-priority (errors, pending prompts).            |
| `kinds`            | Allowlist of notification kinds. Empty = all kinds (then `exclude_kinds` applies).           |
| `exclude_kinds`    | Denylist. Defaults to `["started","channel_otp"]` when omitted. `channel_otp` is **always** dropped regardless (never relay another channel's login code). |
| `source_kinds`     | Allowlist over `schedule` / `file_watcher` / `skill_event` (only applies to event runs).     |
| `subscription_ids` | Allowlist — only these specific automations.                                                 |
| `conversation_ids` | Allowlist — only these specific conversations.                                               |
| `keywords`         | Case-insensitive substrings matched against the title + preview.                             |
| `keywords_mode`    | `any` (default) or `all` — how many keywords must hit.                                        |
| `quiet_hours`      | `{enabled, start:"HH:MM", end:"HH:MM", tz:"<IANA>", allow_high}` — mute during a daily window (crossing midnight supported); `allow_high` still lets high-priority through. `tz` defaults to server local. |

### `cremind channels notify-filter`

**Purpose.** Show or set the notification filter of a `notification`-mode channel.

**Syntax.**

```bash
cremind channels notify-filter <id> [--json '<filter-object>']
```

**Behavior.** With `--json`, PATCHes `config.notification_filter` (merged,
validated, and the adapter restarted so it takes effect immediately). Without
`--json`, prints the channel's current filter. `--json` at the root prints the
filter as compact JSON.

**Examples.**

```bash
# Show the current filter
$ cremind channels notify-filter e2e8...d4f1

# Only high-priority alerts from scheduled automations, muted 22:00–07:00 local
$ cremind channels notify-filter e2e8...d4f1 --json \
    '{"min_priority":"high","source_kinds":["schedule"],"quiet_hours":{"enabled":true,"start":"22:00","end":"07:00","allow_high":true}}'
```

### `cremind channels send`

**Purpose.** Push a one-off, ad-hoc message OUT to a `notification`-mode
channel — straight to its recipients, right now.

**Syntax.**

```bash
cremind channels send <id> "<message>"
cremind channels send <id> --message-file <path>   # or -f -  for stdin
```

**Behavior.** POSTs to `/api/channels/{id}/notify`. The message is delivered to
the channel's recipients — the union of `config.target_chat_ids` and everyone
who has `/start`-subscribed — via the running adapter. Unlike automatic
notifications, this **bypasses the channel's `notification_filter`** (you asked
for it explicitly), so quiet hours / priority / kind rules do not apply.

Requirements: the channel must be in `notification` mode (HTTP 400 otherwise)
and its adapter must be running (HTTP 409 otherwise). If the channel has no
recipients yet, nothing is sent and the command says so — have subscribers
`/start` the bot, or set `target_chat_ids` in config.

This is the manual, operator-facing counterpart to the agent's
`send_notification` tool: when you tell the agent "calculate X and notify me on
Telegram", it computes the answer and calls that tool, which delivers through
the same path.

**Message input.** Provide the text as the positional argument, via
`--message-file <path>`, or on stdin (`-f -`, or simply pipe with no positional
argument). On Windows PowerShell, prefer `--message-file` / stdin — PowerShell
mangles inline quotes and apostrophes when passing arguments to native binaries.

**Output.** Prints `Delivered to N recipient(s).` (or a "no recipients" notice).
`--json` at the root returns `{"delivered": <bool>, "recipients": <int>}`.

**Examples.**

```bash
# Send straight from the command line
$ cremind channels send e2e8...d4f1 "Nightly backup finished OK"
Delivered to 2 recipient(s).

# PowerShell-safe: read the body from a file
PS> cremind channels send e2e8...d4f1 --message-file .\note.txt

# Pipe from stdin
$ echo "1 + 1 = 2" | cremind channels send e2e8...d4f1 -f -
```

### `cremind channels edit`

**Purpose.** Update a channel's settings — mode, auth mode, response mode,
and/or config — sending only the fields you pass.

**Syntax.**

```bash
cremind channels edit <id> [--mode M] [--auth-mode A] [--response-mode R]
                           [--json '<config>'] [--config KEY=VALUE ...]
```

**Flags.**

| Flag              | Meaning                                                              |
|-------------------|----------------------------------------------------------------------|
| `--mode`          | Channel mode (`bot`/`userbot`/`notification`).                       |
| `--auth-mode`     | Auth mode (`none`/`otp`/`password`).                                 |
| `--response-mode` | Reply detail (`normal`/`detail`).                                    |
| `--json`          | Config patch as a JSON object (on PowerShell prefer `--config`).     |
| `--config`        | Config patch as repeatable `KEY=VALUE` (alternative to `--json`).    |

**Behavior.** `config` is **merged** server-side, so you can patch one field
without resending the rest (redaction sentinels like `***` are dropped, never
overwriting a real secret). At least one flag is required. The adapter restarts
when anything runtime-affecting changes. Prints the updated channel; `--json`
returns the full object. The auto-created `main` channel cannot be edited.

**Example.**

```bash
$ cremind channels edit e2e8...d4f1 --response-mode detail --config bot_token=123:abc
```

### `cremind channels enable` / `cremind channels disable`

**Purpose.** Start or stop a channel's adapter.

**Syntax.**

```bash
cremind channels enable <id>
cremind channels disable <id>
```

**Behavior.** A thin shortcut for `edit --json '{"enabled": true|false}'`. Prints
`<id>: enabled=<bool> status=<status>`; `--json` returns the full channel.

**Example.**

```bash
$ cremind channels disable e2e8...d4f1
e2e8...d4f1: enabled=false status=stopped
```

### `cremind channels senders`

**Purpose.** List the senders (remote users) seen on a channel.

**Syntax.**

```bash
cremind channels senders <id>
```

**Behavior.** Prints a `SENDER_ID / NAME / AUTHED / CONVERSATION_ID / PENDING_OTP`
table (any active OTP code is redacted to `***`). `--json` returns the raw
sender rows. Prints `no senders.` when the channel hasn't seen any.

**Example.**

```bash
$ cremind channels senders e2e8...d4f1
SENDER_ID    NAME        AUTHED  CONVERSATION_ID  PENDING_OTP
84986664411  Lee Nguyen  yes     c_92bc
```

### `cremind channels approve` / `cremind channels revoke`

**Purpose.** Approve a pending sender (or revoke an existing one) on **any**
channel. This is the operator side of the `approval`/`allowlist` access methods:
on a `notification` channel a sender who `/start`s stays pending until approved;
on a `bot`/`userbot` channel a sender's first message is held (the agent won't
reply) until approved. Mode-agnostic — it just flips the sender's authorized flag.

**Syntax.**

```bash
cremind channels approve <channel_id> <sender_id>
cremind channels revoke  <channel_id> <sender_id>
```

**Behavior.** PATCHes `/api/channels/{id}/senders/{sender_id}` with
`{"authenticated": true|false}`. The sender must already exist — i.e. they've
contacted the channel (sent `/start`, or any message on a conversational
channel) — otherwise the server returns 404 (so a typo can't seed a junk row).
Find the `sender_id` with `cremind channels senders <channel_id>`. Approving
clears any outstanding one-time code. `revoke` works on any sender regardless of
the channel's auth method, so it's also how you cut off someone on an
`open`/`passcode`/`otp` channel. The web UI's Channels page exposes the same
**Approve** / **Revoke** buttons per sender.

**Example.**

```bash
# See who's waiting / subscribed
$ cremind channels senders e2e8...d4f1
SENDER_ID    NAME        AUTHED  CONVERSATION_ID  PENDING_OTP
84986664411  Lee Nguyen  no

# Approve them
$ cremind channels approve e2e8...d4f1 84986664411
84986664411: approved on channel e2e8...d4f1

# Later, revoke
$ cremind channels revoke e2e8...d4f1 84986664411
84986664411: revoked on channel e2e8...d4f1
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
      "supports_userbot": true,
      "auth_modes": ["none", "otp", "password"],
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
--json`; common causes are an invalid `bot_token`, a Telegram/Zalo
userbot waiting for the verification code or QR scan in the pairing
dialog (status flips to `running` once `ready` fires), a platform SDK
that couldn't be installed at connect time (Telegram/Discord/Slack
install their package automatically on enable — a failure here is
usually an offline host or a locked-down index; install it manually with
`cremind features install channel.discord.bot` / `.slack.bot` /
`.telegram.bot` / `.telegram.userbot`), a missing `node_modules/` under
`app/channels/sidecars/whatsapp/` or `app/channels/sidecars/zalo/` (run
`npm install` once, or restart to auto-install), Node not on PATH, or —
for Messenger — the Cremind host not being publicly reachable so Meta's
webhook can't deliver.

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
