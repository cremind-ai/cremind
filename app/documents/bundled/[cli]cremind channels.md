---
description: "Connect and manage external **messaging channels** — Telegram, WhatsApp, Discord, Slack, Messenger, and Zalo: `list` connected channels, `add` one from a JSON config, `edit` a channel's settings, `enable`/`disable` it, list its `senders` with their token usage, wipe one subscriber's conversation history with `clear-history`, delete a client completely with `forget` (as if they had never messaged — conversation, messages, automations, contact details and access all removed), run the interactive `pair` flow (QR code in the terminal, or a Telegram verification code and 2FA password), set a channel's push-notification filter with `notify-filter`, push an ad-hoc message out to a notification channel with `send`, send a direct message to specific individual clients — one person or a bulk list, addressed by platform id or **phone number** — with `message`, record a contact's phone number with `set-phone`, decide per client whether the agent must ask before messaging them with `set-confirm` (the profile-wide default is `channels.confirm_before_send` in Settings → Config → Channels; turn it off so unattended automations can send without stopping to ask), `approve`/`revoke` who may subscribe to a notification channel, `delete` a channel and cascade-remove its conversations, and dump the `catalog` of supported platforms. Channels can run in conversational `bot`/`userbot` mode or a push-only `notification` mode that forwards Cremind's automation/event alerts to a chat with a configurable filter (importance, kind, source, specific automation/conversation, keyword, quiet hours). All channels gate access with the same per-channel **authentication** method — open, passcode, one-time code (`otp`), admin approval, or allowlist — controlling who may chat (bot/userbot) or subscribe (notification); `approve`/`revoke` authorize individual senders and work in every mode. A notification channel can also receive one-off messages you send with `cremind channels send` — the same delivery the agent's `send_notification` tool uses when you ask it to 'notify me on Telegram'. Separately, `cremind channels message` sends to **named individuals** rather than to subscribers: give it sender ids or phone numbers (one `--to`, or a JSON list for a bulk campaign such as thanking every customer in a spreadsheet), and each delivered message is saved into that client's own conversation so the agent has the context later; it previews by default and only sends with `--send`, and only WhatsApp can message someone who has never written first. Zalo offers both an official Bot API mode and a QR-paired personal-account mode; Messenger requires a publicly-reachable HTTPS host for its webhook. A channel can also take part in **group chats** — real Telegram, Discord, Slack, WhatsApp or Zalo groups full of real people that this profile's account has been added to: opt in per channel with `--group-chats`, then approve each group with `cremind channels groups approve` (new groups arrive `pending` with a high-priority notification and the agent reads nothing until you approve), and tune it with `channels groups list`/`members`/`policy`/`allow`/`deny`/`respond`/`refresh`/`block`/`forget`. In an approved group the agent replies when mentioned and otherwise only when a cheap relevance check says the message is for it, while everything else is still stored as context. This is **not** `cremind group`, which is Cremind's own rooms where several profiles' agents talk to each other; the two features share nothing. Use this to link a Telegram/Discord/Slack bot or other chat platform to Cremind; the auto-created `*main*` channel cannot be removed."
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
- **`message`** — Send a message to **specific individual clients** on a
  channel — one person or a bulk list, addressed by platform sender id or
  phone number. Previews by default; `--send` delivers. This is the manual
  counterpart to the agent's `send_channel_message` tool.
- **`set-phone`** — Record (or clear) a contact's phone number so `message`
  can reach them by it.
- **`set-confirm`** — Decide whether the agent must ask you before messaging
  one client: `never` (send directly — what automations need), `always`, or
  `default` to inherit the profile setting.
- **`clear-history`** — Wipe one client's messages, keeping the person and
  their automations.
- **`forget`** — Delete a client **completely**, as if they had never
  messaged: conversation, messages, automations, contact details and access
  approval all go. Irreversible.
- **`approve`** / **`revoke`** — Approve a pending subscriber (or revoke an
  existing one) on a `notification`-mode channel — the operator side of the
  `approval` subscription-auth method (see **Notification mode** below).
- **`delete`** — Tear down the adapter and remove the row. **Cascades
  delete to every conversation that belonged to that channel and
  every per-sender authentication state.**
- **`groups`** — Approve and manage the **platform group chats** this
  channel's account has been added to: `list`, `approve`/`block`/`forget`,
  `members`, `policy`, `allow`/`deny`, `respond` and `refresh` (see
  **Group chats on a channel** below).
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
| `response_mode`    | `normal` — the platform user receives ONLY the final answer. `detail` — they also receive, as separate bubbles while the run executes, what triggered it (for event-driven runs) and each Thinking-Process step. Everything beyond the answer is Cremind's own working, so `normal` sends none of it. Applies to **group chats too**, with one exception: a turn that answers `[silent]` (the agent judging the message was not for it) posts nothing at all, steps included. The setting is channel-wide — it cannot be `detail` in DMs and `normal` in that channel's rooms. Changing it restarts the adapter, so it takes effect on the next message with no `serve` restart. |
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
- A message that arrives **while the bot is still composing** is folded into the
  reply being written, so the agent takes it into account before answering. The
  bot no longer replies "I'm thinking…" and no longer ignores those messages: a
  burst gets one answer covering all of it, or — if the turn was already
  finishing — an immediate follow-up reply.
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

A **group** on the platform is a third path: the same inbound-only rule applies
(the agent answers into the group through the adapter, and you cannot post into
the group's conversation from the CLI), but the group has to be switched on and
approved first — see **Group chats on a channel** below.

Use `cremind conv get <id>` and `cremind conv attach <id>` to inspect channel
conversations; use the corresponding platform (Telegram, etc.) to
talk to the agent.

## Group chats on a channel

A channel is normally a set of one-to-one threads: one person messages the bot,
the agent answers them. **Group chats** are the other shape — this profile's
channel account (a Telegram bot or userbot, a Discord bot, a Slack app, the
paired WhatsApp account, a Zalo bot or paired account) sits in a real group on
that platform, alongside real people, and takes part in the conversation.

### Not the same as `cremind group`

Two features, similar names, nothing shared:

| What you want | What to use |
|---------------|-------------|
| **One** profile's agent in a real Telegram/Discord/Slack/WhatsApp/Zalo group, alongside real people | `cremind channels groups` (this page) |
| **Several** of your profiles' agents in one Cremind-owned room, talking to you and to each other | [`cremind group`](./%5Bcli%5Dcremind%20group.md) |

A platform group is never mirrored into a Cremind room, a Cremind room is never
bound to a platform chat, and neither one's settings reach the other.

### Off by default

Group chats are opt-in **per channel** (`config.group_chats_enabled`, off until
you turn it on with `--group-chats` on `add` or `edit`, or the switch on the
Channels page). While it is off the agent never sees group traffic at all:
nothing is stored, no notification is raised, and it does not matter who adds the
account to what. A bot can be added to any group by anybody, and an agent that
silently started recording those would be a surprise nobody asked for.

### Approve before the agent reads anything

With the flag on, every group the account is in shows up in
`cremind channels groups list` as **pending**, together with a **high-priority
notification**. A pending group is inert: its messages are not read, not stored
and not answered. It becomes live only when a human approves it — the Channels
page, or `cremind channels groups approve <channel_id> <group>`.

`block` is the opposite decision, kept on the record: the transcript so far
survives and being re-added does not ask you again. `forget` erases the group and
its conversation instead, so the next message from it asks afresh.

### When the agent speaks

Once a group is approved:

- **Mentioned → it answers.** An `@mention`, or a reply to one of the agent's own
  messages, is an immediate turn. Each platform spells a mention differently and
  each adapter detects its own.
- **Addressed by name → it answers.** The agent knows the name its account
  shows in the group ("Lý Nguyen", not the profile name), so `@Lý Nguyen …` or
  `Lý Nguyen, …` counts as being addressed even on platforms that report no
  structured mention — Zalo has none at all.
- **Not addressed → a cheap relevance check decides.** One call on the profile's
  **`low`-tier model** reads the last few messages plus the new one and answers
  "is this for *me*?". The agent is treated as a **member of the group**, not as
  an intruder in it: a message put to everybody — a greeting, "anyone…?", a
  question with no named addressee — is put to it too, and it answers like any
  other member would. It stays out of exchanges addressed to somebody else by
  name, side conversations it was never part of, and another assistant's reply
  to the same question. Set `respond mention_only` to skip the check entirely.
- **A broken check stays silent.** A timeout, a provider error, an unparseable
  answer or no LLM configured at all resolve to *not relevant* — ambiguity leans
  towards answering, but a provider outage must not turn a group into a
  chatterbox.
- **Either way the message is stored.** A message the agent does not answer still
  lands in the group's conversation as context, so a later turn reads the thread
  it is joining rather than one line out of nowhere.

### Interrupting an agent that is busy

A message sent while the agent is already working is **folded into the turn it
is already running** rather than queued behind it. It goes through the same
checks as any other message first, so an interruption meant for somebody else
still gets nothing.

One that **@mentions the agent** is answered during the pause: it posts one
short line to the group — "not yet, still installing" — and carries straight on
with the work. The line arrives while the job is still running, which is the
point of asking, and the final answer still comes when the job is done.

For a message that was *not* addressed to it by name but passed the relevance
check anyway, replying now is the agent's own call — it may cover the point in
its final message instead.

Either way the interruption reaches the running turn, so "stop what you are
doing" or "do X instead" changes course from that point rather than after the
superseded work finishes.

### The group's conversation

Each approved group gets an **ordinary conversation** bound to the channel and
titled after the group. It appears in the sidebar like any other, opens from the
Channels page, and carries the whole transcript — the answered messages and the
unanswered ones alike.

### Who the agent answers

Per group, with `groups policy`:

| Mode | Effect |
|------|--------|
| `everyone` *(default)* | Answers anybody in the group **except** the deny list. |
| `selected` | Answers **only** the allow list. |

`groups allow` and `groups deny` move member ids between the two lists (adding to
one removes from the other); both lists survive a mode switch, so flipping back
does not lose a list you curated. A **denied** member is stronger than "not
answered": their messages are dropped outright rather than stored, so somebody
you blocked cannot fill the agent's context either.

### Groups the account was already in

Everything above is about being *added* to a group. The other half is the groups
the account already belonged to when you turned the feature on — nobody added it
to those, so there is no join to notice, no notification, and nothing to
approve. They are **not** raised as pending, deliberately: switching the feature
on would otherwise open with a wall of decisions you never asked to make.

Reach them by picking instead:

- **Web UI** — Channels page → the channel's Group chats section → **Add
  existing groups…**, which lists what the platform says the account is in and
  enables the ones you tick.
- **CLI** — `cremind channels groups available <channel_id>` then
  `cremind channels groups add <channel_id> -- <chat_id>…` (the `--` matters:
  Telegram and Zalo ids start with a minus sign).

Picking **is** approving; there is no second step.

Alongside that, each listing-capable channel reconciles in the background every
15 minutes. The first pass records what the account is in as a silent baseline;
after that, a group that appears which is not in the baseline really was joined
while Cremind was down, and gets the normal pending row and notification.

### Loop brakes

A group can contain other automated accounts — including another Cremind
profile's agent, which is a supported way to use this — and two assistants being
endlessly helpful at each other is the failure mode. Two caps stop it:

- **20 agent posts per minute**, per group.
- **8 consecutive bot-authored messages** with no human in between, after which
  the agent goes quiet until a person posts. Only reachable on platforms that
  flag bot authorship: **Telegram, Discord and Slack**. WhatsApp and Zalo report
  no such flag, so only the rate cap applies there.

A braked agent is quiet, not blind: messages keep being stored throughout.

### Per-platform prerequisites

The middle column is the step that is otherwise invisible — skip it and the
account joins the group, looks perfectly healthy, and hears nothing.

| Platform | Put in the group | The setup step that is easy to miss | Reports a join? | Lists existing groups? | Member roster |
|----------|------------------|--------------------------------------|-----------------|------------------------|---------------|
| Telegram (bot) | add the bot to the group | @BotFather → `/setprivacy` → **Disable**, or make the bot a group admin — otherwise it only ever receives messages that mention or reply to it | yes | no — the Bot API cannot enumerate | **administrators only** (the Bot API cannot enumerate a group), plus whoever has posted |
| Telegram (userbot) | the paired personal account must be a member | none beyond pairing | yes | yes | full |
| Discord | invite the bot to the server and give it access to the channel | enable **MESSAGE CONTENT INTENT** (Developer Portal → Bot → Privileged Gateway Intents), or messages arrive with an empty body; add **SERVER MEMBERS** for a complete roster | no per channel — joining a *server* raises one notification pointing at the picker | yes — every readable text channel, as `Server / #channel` | full with the Server Members intent, partial without |
| Slack | `/invite` the app into the channel | add `channels:history`, `groups:history`, `channels:read` (plus `groups:read` for private channels), subscribe to `message.channels` / `message.groups` **and `member_joined_channel`**, then **reinstall** — a DM-only install does not cover channels | yes | yes | full |
| WhatsApp | the QR-paired personal account must itself be a member | none beyond pairing | yes | yes | full |
| Zalo (bot) | add the bot to the group and post once | none | no — discovered on the first message | no | **nobody**: the Bot API names no members, so the roster is whoever has posted |
| Zalo (userbot) | the QR-paired personal account must itself be a member | none beyond pairing | yes | yes | full |
| Messenger | — | **not supported.** A Messenger channel is a Facebook Page, and Pages have no group threads. | — | — | — |

"Reports a join" is what decides *when* a group first appears: where the answer
is no, the group shows up as pending on its **first message** instead of the
moment the account was added. "Lists existing groups" is what decides whether
**Add existing groups** / `channels groups available` can offer you the ones the
account already belonged to; where the answer is no, waiting for somebody to
post really is the only way in. `cremind channels groups refresh` re-asks the
platform for the roster on demand and reports `unsupported` rather than failing
where the platform names nobody.

## Finding this in the web UI

Every operation in this group has a control on the **Channels** page
of the Cremind web UI:

> **Sidebar → Channels** (live management view) — or
> **Settings → Channels** (registration form).

The Settings page exposes the **Add Channel** flow (mirroring `cremind
channels add`); the sidebar Channels page lists channels with their
runtime status and, when a channel is expanded, one row per subscriber
showing authentication state, that subscriber's **token usage and cost**,
and per-row **Approve**/**Revoke**, **Open**, and **Clear history**
actions (mirroring `cremind channels list`, `senders`,
`approve`/`revoke`, and `clear-history`). The sidebar's conversation-list
channel selector mirrors `cremind conv list --channel <type>`.

On a group-capable channel the same card carries a **Group chats** panel — a
badge counting the groups waiting on you, an on/off switch mirroring
`--group-chats`, and per group the Approve / Block / Forget buttons, the
**Everyone in the group** / **Only selected people** and **When mentioned or
relevant** / **Only when mentioned** dropdowns, and **Refresh members** —
mirroring `cremind channels groups` in full. A `channel_group_request`
notification deep-links straight to the group it is about.

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
| `REPLY`    | `response_mode`  | `normal` (final answer only) / `detail` (with steps).  |
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
                 [--group-chats | --no-group-chats]
                 [--enabled | --disabled]
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
`--disabled` (no live adapter to pair with yet — re-enable from
the web UI or run `cremind channels pair <id>` after enabling).

**Flags.**

| Flag              | Type    | Default   | Meaning                                                                            |
|-------------------|---------|-----------|------------------------------------------------------------------------------------|
| `--type`          | string  | (required)| Channel type. Must match an entry in `cremind channels catalog`.                       |
| `--mode`          | string  | `bot`     | Adapter mode (`bot`, `userbot`, or `notification`). Catalog-declared modes only; modes flagged `implemented = false` are rejected. |
| `--auth-mode`     | string  | `none`    | Per-sender gate.                                                                   |
| `--response-mode` | string  | `normal`  | Reply detail (`normal` or `detail`).                                               |
| `--group-chats/--no-group-chats` | bool | `--no-group-chats` | Let this channel's agent take part in **platform group chats**. New groups arrive `pending` and must be approved with `cremind channels groups approve` — see **Group chats on a channel**. |
| `--enabled/--disabled` | bool | `--enabled` | Start the adapter immediately, or register the row without starting it.       |
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
                   --disabled \
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

### `cremind channels message`

**Purpose.** Send a message to **specific individual clients** on a channel —
one person or a bulk list — addressed by platform sender id or phone number.

This is the targeted counterpart of `send`: `send` broadcasts to a notification
channel's subscribers, while `message` writes to named people (your customers,
not you). It is the manual counterpart to the agent's `send_channel_message`
tool.

**Syntax.**

```bash
cremind channels message <id> "<message>" --to <id-or-phone> [--to ...]
cremind channels message <id> --recipients-file <path.json> [--send]
cremind channels message <id> --message-file <path> --to <id-or-phone> --send
```

**Previews by default.** Without `--send` nothing is delivered: every recipient
is resolved and reported, so you can see who would be messaged, who has never
been contacted before, and which entries failed to resolve. Add `--send` to
deliver. This CLI behaviour is fixed — `--send` is your approval, so it is not
affected by any setting.

The agent's tool asks for approval too, but there it is **configurable**: the
profile-wide *Confirm before messaging clients* setting
(`channels.confirm_before_send`, default on) plus per-client overrides from
`cremind channels set-confirm`. Turn it off, or exempt individual clients, so an
unattended automation can send without stopping to ask — someone who has never
messaged the channel is always confirmed regardless.

**Addressing.** `--to` takes a platform sender id (Telegram numeric id,
WhatsApp JID, Slack `U…`, Discord id, …) or a phone number. Resolution is
exact, never fuzzy — an ambiguous recipient is reported rather than guessed:

1. An exact sender-id match among the channel's known contacts.
2. A stored phone number (`channels senders` PHONE column, set automatically
   for WhatsApp or by `channels set-phone`).
3. For WhatsApp, the number's own JID (`<digits>@s.whatsapp.net`).
4. A cold send — only on WhatsApp, and only after checking the number really
   has a WhatsApp account.

Phone numbers must be international (`+84901234567`). A national-format number
with a leading `0` is ambiguous without a country, so it needs
`--country-code 84`.

**Who can be reached.** Only WhatsApp can message someone who has never written
first. Telegram bots, Messenger and Zalo bots cannot start conversations (the
platforms forbid it) and those recipients come back as errors naming what would
work instead. A successful cold send registers the person as a contact with
their own conversation, but leaves them **unauthenticated** — on a channel with
subscription auth, their reply still has to pass your access gate.

**History.** Every delivered message is saved into that client's conversation as
an agent message, so later turns (and the web UI) show what was already sent to
them. A message that failed to send is not recorded.

**Bulk sends.** Max 100 recipients per call. Sends are sequential and paced per
platform (WhatsApp deliberately slowly — bursts of unsolicited messages are what
gets numbers banned), and the run aborts early if 5 consecutive sends fail.

**Recipient file.** `--recipients-file` reads a JSON list (use `-` for stdin) of
strings, or of objects for per-recipient personalisation:

```json
[
  {"to": "+84901234567", "name": "Lee",  "message": "Thanks for trying it, Lee!"},
  {"to": "+84907654321", "name": "Minh"},
  "84900000000"
]
```

Entries without their own `message` fall back to the shared message argument.

**Message input.** Provide the shared text as the positional argument, via
`--message-file <path>`, or on stdin. On Windows PowerShell prefer
`--message-file` / `--recipients-file` — PowerShell mangles inline quotes and
apostrophes when passing arguments to native binaries.

**Output.** A `TO / STATUS / CHANNEL / SENDER_ID / NEW / DETAIL` table, then a
summary line. `STATUS` is `would_send` (preview), `sent`, `failed`, or `skipped`
(after an early abort). Exit code 1 if any recipient failed. `--json` at the
root returns `{"dry_run", "sent", "failed", "results": [...]}`.

**Examples.**

```bash
# Preview first — who would actually get this?
$ cremind channels message e2e8...d4f1 "Thanks for trying our product!" \
    --to +84901234567 --to +84907654321
TO             STATUS      CHANNEL   SENDER_ID                  NEW  DETAIL
+84901234567   would_send  whatsapp  84901234567@s.whatsapp.net
+84907654321   would_send  whatsapp  84907654321@s.whatsapp.net  yes

Preview only — nothing sent. 2 of 2 recipient(s) resolved, 1 never contacted
before. Re-run with --send to deliver.

# Looks right — deliver it
$ cremind channels message e2e8...d4f1 "Thanks for trying our product!" \
    --to +84901234567 --to +84907654321 --send

# Personalised bulk campaign from a file
$ cremind channels message e2e8...d4f1 --recipients-file thankyou.json --send

# Vietnamese numbers written in national form
$ cremind channels message e2e8...d4f1 "Cảm ơn bạn!" --to 0901234567 --country-code 84
```

### `cremind channels set-confirm`

**Purpose.** Choose whether the agent must ask you before it messages **one**
client — overriding the profile-wide default for that person.

**Syntax.**

```bash
cremind channels set-confirm <channel_id> <sender_id> <default|always|never>
```

**Why this exists.** By default the agent previews a send and waits for your
approval, which is right when you are sitting there and wrong when you are not:
a scheduled automation has nobody to answer the prompt, so it parks itself
pending instead of sending. Marking the clients you have already decided about
as `never` lets those sends go straight out while everyone else still gets the
preview.

**Modes.**

| Mode | Effect |
|---|---|
| `never` | Send directly — no approval needed for this client. |
| `always` | Always ask about this client, even if the profile setting is off. |
| `default` | Clear the override and inherit the profile setting. |

**The profile-wide default** is *Confirm before messaging clients* under
Settings → Config → Channels, or:

```bash
cremind config set channels.confirm_before_send false   # send without asking
cremind config get channels.confirm_before_send
```

**Always confirmed regardless.** A recipient who has never messaged the channel
(e.g. a phone number from a spreadsheet that WhatsApp would cold-contact) is
always previewed, whatever the profile setting and whatever any override says —
they have no client record to exempt, and it is the send most likely to be going
to the wrong person.

**Output.** Prints the resulting stance for that client. The current value shows
in the `CONFIRM` column of `cremind channels senders` (blank = inherit).

**Example.**

```bash
$ cremind channels set-confirm e2e8...d4f1 84986664411 never
84986664411: send directly

$ cremind channels senders e2e8...d4f1
SENDER_ID    NAME        PHONE        AUTHED  CONFIRM  TOKENS   COST_USD  CONVERSATION_ID  PENDING_OTP
84986664411  Lee Nguyen  84986664411  yes     never    124,908  0.2841    c_92bc
```

The web UI's Channels page exposes the same choice as a **Confirm before send**
dropdown on each client row.

### `cremind channels set-phone`

**Purpose.** Record (or clear) a contact's phone number, so `cremind channels
message` — and the agent — can reach them by number instead of platform id.

**Syntax.**

```bash
cremind channels set-phone <id> <sender_id> <phone>
cremind channels set-phone <id> <sender_id> --clear
```

**Behavior.** PATCHes `/api/channels/{id}/senders/{sender_id}` with the
normalized number. WhatsApp contacts are filled in automatically (their sender
id *is* the number); everywhere else the mapping has to come from you, because
no platform tells Cremind a chat partner's phone number.

This is also the only way to **correct** a stored number: automatic derivation
only ever fills an empty one, so a mapping you fixed by hand always wins. The
number must be in international form (HTTP 400 otherwise). The sender must
already exist — find them with `cremind channels senders <id>`.

**Example.**

```bash
$ cremind channels set-phone e2e8...d4f1 123456789 +84901234567
123456789: phone set to 84901234567
```

### `cremind channels edit`

**Purpose.** Update a channel's settings — mode, auth mode, response mode,
and/or config — sending only the fields you pass.

**Syntax.**

```bash
cremind channels edit <id> [--mode M] [--auth-mode A] [--response-mode R]
                           [--group-chats | --no-group-chats]
                           [--json '<config>'] [--config KEY=VALUE ...]
```

**Flags.**

| Flag              | Meaning                                                              |
|-------------------|----------------------------------------------------------------------|
| `--mode`          | Channel mode (`bot`/`userbot`/`notification`).                       |
| `--auth-mode`     | Auth mode (`none`/`otp`/`password`).                                 |
| `--response-mode` | Reply detail (`normal`/`detail`).                                    |
| `--group-chats/--no-group-chats` | Whether this channel's agent takes part in **platform group chats** (see **Group chats on a channel**). Turning it off leaves the groups on record but stops the agent reading them; turning it on does *not* approve anything — each group still starts `pending`. |
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

# Let this channel's agent take part in platform groups (each one still needs
# approving with `cremind channels groups approve`)
$ cremind channels edit e2e8...d4f1 --group-chats
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

**Purpose.** List the senders (remote users) seen on a channel, with each
one's token usage.

**Syntax.**

```bash
cremind channels senders <id>
```

**Behavior.** Prints a
`SENDER_ID / NAME / PHONE / AUTHED / CONFIRM / TOKENS / COST_USD /
CONVERSATION_ID / PENDING_OTP` table (any active OTP code is redacted to `***`). `PHONE` is the
contact's number where Cremind knows it — derived automatically for WhatsApp,
otherwise set with `cremind channels set-phone` — and is what lets
`cremind channels message` address them from a list of numbers. `TOKENS` and `COST_USD`
are that sender's cumulative totals across their conversation — the same
numbers the conversation's usage panel shows, rolled up so you don't have
to open each conversation; both are blank for a sender with no recorded
usage yet. `--json` returns the raw sender rows, each with a `usage`
object (`input_tokens`, `cache_read_input_tokens`,
`cache_creation_input_tokens`, `output_tokens`, `total_tokens`,
`total_usd`, `request_count`) or `null`. Prints `no senders.` when the
channel hasn't seen any.

**Example.**

```bash
$ cremind channels senders e2e8...d4f1
SENDER_ID    NAME        PHONE        AUTHED  CONFIRM  TOKENS   COST_USD  CONVERSATION_ID  PENDING_OTP
84986664411  Lee Nguyen  84986664411  yes              124,908  0.2841    c_92bc
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

### `cremind channels clear-history`

**Purpose.** Wipe one subscriber's conversation history on a channel —
the per-user equivalent of clearing a chat, without touching anyone else
on that channel.

**Syntax.**

```bash
cremind channels clear-history <channel_id> <sender_id> [--yes]
```

**Behavior.** Deletes every message in that sender's conversation but
**keeps the conversation itself**. Three consequences worth knowing:

- The sender's next message continues in the same conversation (a fresh
  one is *not* created), so their conversation id and any automations
  homed on it stay valid.
- Their token/cost totals in `cremind channels senders` and on the web
  Channels page **survive** the wipe — usage is attributed to the
  conversation, not to the messages.
- Skill events, file watchers, and schedules the sender registered stay
  armed. Clearing chat history is not a way to disarm automations.

Queued-but-unstarted turns, the live replay buffer, and the wiped turns'
plan files are dropped along with the messages. If the subscriber has a
run in progress the command fails with a 409 — wait for it to finish.
Sender ids come from `cremind channels senders <channel_id>`; a sender
who has never spoken has no conversation and the command reports that
without failing.

**Confirmation.** Prompts before deleting. `--yes` / `-y` skips the
prompt; **non-interactively (scripts, `exec_shell`) `--yes` is required**
— without it the command explains what it would delete and exits 1
rather than guessing.

**Example.**

```bash
$ cremind channels clear-history e2e8...d4f1 84986664411 --yes
84986664411: cleared 42 message(s) from conversation c_92bc

# Usage totals are still there afterwards
$ cremind channels senders e2e8...d4f1
SENDER_ID    NAME        AUTHED  TOKENS   COST_USD  CONVERSATION_ID  PENDING_OTP
84986664411  Lee Nguyen  yes     124,908  0.2841    c_92bc
```

The web UI's Channels page exposes the same action as a **Clear history**
button on each subscriber row.

### `cremind channels forget`

**Purpose.** Delete a channel client **completely** — return Cremind to the
state it would be in if that person had never messaged. The full-erasure
counterpart of `clear-history`, which deliberately keeps the person and only
wipes their messages.

**Syntax.**

```bash
cremind channels forget <channel_id> <sender_id> [--yes]
```

**Behavior.** Removes everything the client left behind:

- their **conversation** and every message in it;
- the **automations homed on it** — skill events, file watchers and schedules —
  disarmed in the live managers, not merely dropped from the database, along
  with their run history, queued turns, replay buffer and plan files;
- files they **uploaded** into that conversation;
- long-term memory entries the agent learned **from that conversation** (facts
  the profile holds for other reasons are untouched);
- the **sender record** itself: display name, phone, WhatsApp alias, and their
  access state (`authenticated` plus any outstanding OTP);
- their entry in the channel's `target_chat_ids`, if they were also a static
  notification recipient — otherwise they would keep receiving pushes after
  being deleted;
- the running adapter's in-memory state for them, so nothing about them
  survives until the next restart.

Afterwards their next message is a genuine first contact: a new sender row, a
new conversation, and the channel's access check applied from scratch.

**What survives, by design.** Recorded token usage and cost stay in the account
totals — the tokens really were spent — but their conversation link is nulled,
so the spend is no longer attributed to anyone and nothing identifying remains.
Use `cremind usage` to see account-level totals.

Fails with a 409 while that client has a run in progress; wait for it to finish.

**Confirmation.** Prompts before deleting. `--yes` / `-y` skips the prompt;
**non-interactively (scripts, `exec_shell`) `--yes` is required** — without it
the command explains what it would delete and exits 1 rather than guessing.

**Example.**

```bash
$ cremind channels forget e2e8...d4f1 84986664411 --yes
84986664411: deleted from channel e2e8...d4f1
  removed 42 message(s)
  forgot 2 long-term memory entries

# They are gone from the subscriber list entirely
$ cremind channels senders e2e8...d4f1
no senders.
```

The web UI's Channels page exposes the same action as a **Delete** button on
each client row.

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
                   --json '{}' --disabled
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

### `cremind channels groups list`

**Purpose.** List the platform groups this channel's account has been added to,
with their approval status and member policy.

**Syntax.**

```bash
cremind channels groups list <channel_id> [--status pending|approved|blocked]
```

**Flags.**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--status` | string | (all) | Show only `pending`, `approved` or `blocked` groups. |

**Behavior.** Prints `GROUP_ID`, `CHAT_ID` (the id the platform uses), `TITLE`,
`STATUS`, `MEMBERS` (how many are on record), `POLICY` (`everyone`/`selected`)
and `LAST_MESSAGE`. A group appears the moment the account is added to it — or,
on a platform that reports no join, when somebody first speaks — and stays
`pending`, with the agent deaf to it, until you `approve` it. An empty list
usually means group chats are still off for the channel; the command says so and
names the `edit --group-chats` command that turns them on. With `--json`,
returns the full rows including `settings`.

**Example.**

```bash
$ cremind channels groups list e2e8...d4f1
GROUP_ID   CHAT_ID          TITLE      STATUS    MEMBERS  POLICY    LAST_MESSAGE
7c0f...e1  -1001234567890   Ops room   approved  6        everyone  2026-08-29 14:20
9b31...a4  -1009876543210   Family     pending   0        everyone

# Just the ones waiting on you
$ cremind channels groups list e2e8...d4f1 --status pending
```

### `cremind channels groups approve`

**Purpose.** Let the agent take part in one group.

**Syntax.**

```bash
cremind channels groups approve <channel_id> <group>
```

**Arguments.**

- `<channel_id>` — channel id, from `cremind channels list`.
- `<group>` — group id, the platform's own chat id, or a unique title. Two
  groups with the same title are refused rather than guessed at: approving the
  wrong room is not a recoverable mistake.

**Flags.** None beyond the root `--json`.

**Behavior.** Flips the group to `approved`. From then on the agent reads the
group's messages, replies immediately when mentioned (or replied to), and
otherwise asks the relevance judge — see **Group chats on a channel**. Who it may
answer is the group's member policy. Approval also asks the platform for a roster
straight away where the platform will answer. Prints `id`, `title`, `status` and
`conversation_id`.

**Example.**

```bash
$ cremind channels groups approve e2e8...d4f1 "Ops room"
id               7c0f...e1
title            Ops room
status           approved
conversation_id  c_92bc
```

### `cremind channels groups block`

**Purpose.** Keep the agent out of one group, and remember the decision.

**Syntax.**

```bash
cremind channels groups block <channel_id> <group>
```

**Flags.** None beyond the root `--json`.

**Behavior.** Flips the group to `blocked`. The transcript so far is kept, and
being added to the group again does not ask you a second time — this is a
decision on the record, not a dismissal. To erase the group instead, use
`groups forget`. Prints the same four fields as `approve`.

**Example.**

```bash
$ cremind channels groups block e2e8...d4f1 -1009876543210
id               9b31...a4
title            Family
status           blocked
conversation_id
```

### `cremind channels groups forget`

**Purpose.** Erase a group and its transcript, as if the account had never been
in it.

**Syntax.**

```bash
cremind channels groups forget <channel_id> <group> [--yes]
```

**Flags.**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--yes`, `-y` | bool | `false` | Skip the confirmation prompt. |

**Behavior.** Deletes the group row and its conversation with every message in
it. Unlike `block`, nothing is remembered: the next message from that group
arrives as a fresh `pending` request. Fails with a `409` while the group has a
run in progress — wait for it to finish. Irreversible.

**Confirmation.** Prompts before deleting. **Non-interactively (scripts,
`exec_shell`) `--yes` is required** — without it the command explains what it
would delete and exits 1 rather than guessing. With `--json` the confirmation is
`{"deleted": true, "group_id": "<id>"}`.

**Example.**

```bash
$ cremind channels groups forget e2e8...d4f1 "Family" --yes
9b31...a4: forgotten
```

### `cremind channels groups members`

**Purpose.** Show who is in a group, and whether the agent answers them.

**Syntax.**

```bash
cremind channels groups members <channel_id> <group>
```

**Flags.** None beyond the root `--json`.

**Behavior.** Prints `MEMBER_ID`, `NAME`, `SOURCE`, `BOT`, `RESPONDS` and
`LAST_SEEN`. `SOURCE` says where the row came from: `roster` is the platform's
own member list, `seen` is somebody who has posted here. `RESPONDS` is the
member policy's verdict for that account — what `allow`/`deny`/`policy` change.
`MEMBER_ID` is what `allow` and `deny` take.

Some platforms name nobody: a Telegram **bot** can only list administrators, and
a Zalo bot not even those, so a short list is not necessarily a wrong one — the
rest fill in as people post. `cremind channels groups refresh` re-asks the
platform.

**Example.**

```bash
$ cremind channels groups members e2e8...d4f1 "Ops room"
MEMBER_ID     NAME        SOURCE  BOT    RESPONDS  LAST_SEEN
1644772063    Alexa       roster  false  true      2026-08-29 14:19
216091010     Ops Bot     roster  true   true
84986664411   Lee Nguyen  seen    false  true      2026-08-29 14:20
```

### `cremind channels groups policy`

**Purpose.** Choose who the agent answers in a group: everyone, or only the
accounts you allow.

**Syntax.**

```bash
cremind channels groups policy <channel_id> <group> <everyone|selected>
```

**Flags.** None beyond the root `--json`.

**Behavior.** `everyone` answers anybody in the group except those on the deny
list; `selected` answers only the allow list. **Both lists are kept when you
switch**, so flipping back does not lose one you curated. Any other `MODE`
exits 1 before anything is sent. Prints `id`, `title`, `respond_mode`,
`policy_mode`, `allow` and `deny`.

**Example.**

```bash
$ cremind channels groups policy e2e8...d4f1 "Ops room" selected
id            7c0f...e1
title         Ops room
respond_mode  mention_or_relevant
policy_mode   selected
allow         1644772063
deny
```

### `cremind channels groups allow` / `cremind channels groups deny`

**Purpose.** Move member accounts on and off the group's allow/deny lists.

**Syntax.**

```bash
cremind channels groups allow <channel_id> <group> <member_id>...
cremind channels groups deny  <channel_id> <group> <member_id>...
```

**Flags.** None beyond the root `--json`.

**Behavior.** The two lists are exclusive: `allow` adds each id to the allow list
and takes it off the deny list, `deny` does the reverse. Repeats are collapsed,
so re-running is harmless. Member ids come from `cremind channels groups
members`.

Which list is consulted depends on `groups policy`: `everyone` reads the deny
list, `selected` reads the allow list. A **denied** account is stronger than one
that simply is not answered — their messages are dropped rather than stored, so
somebody you blocked cannot fill the agent's context either. Both commands print
the same six fields as `policy`.

**Examples.**

```bash
# Under `selected`: these are the only people the agent answers
$ cremind channels groups allow e2e8...d4f1 "Ops room" 1644772063 84986664411

# Under `everyone`: shut one noisy bot out entirely
$ cremind channels groups deny e2e8...d4f1 "Ops room" 216091010
```

### `cremind channels groups respond`

**Purpose.** Decide when the agent may speak in a group without being mentioned.

**Syntax.**

```bash
cremind channels groups respond <channel_id> <group> <mention_or_relevant|mention_only>
```

**Flags.** None beyond the root `--json`.

**Behavior.** `mention_or_relevant` (the default) runs the cheap relevance check
on messages that do not mention the agent and replies when the answer is yes.
`mention_only` skips that check entirely — cheaper, and the right setting for a
quiet assistant in a busy room. Either way an `@mention` or a reply to the agent
still gets an answer, and every message is still stored as context. Any other
`MODE` exits 1. Prints the same six fields as `policy`.

**Example.**

```bash
$ cremind channels groups respond e2e8...d4f1 "Ops room" mention_only
id            7c0f...e1
title         Ops room
respond_mode  mention_only
policy_mode   everyone
allow
deny
```

### `cremind channels groups refresh`

**Purpose.** Ask the platform who is in a group, now.

**Syntax.**

```bash
cremind channels groups refresh <channel_id> <group>
```

**Flags.** None beyond the root `--json`.

**Behavior.** The member list comes from the platform, not from Cremind, so the
channel's adapter has to be **running**. Platforms that name nobody report
`source: unsupported` rather than failing — a Zalo bot always, a Telegram bot
beyond the administrators. Prints `id`, `title`, `members` and `source`.

**Example.**

```bash
$ cremind channels groups refresh e2e8...d4f1 "Ops room"
id       7c0f...e1
title    Ops room
members  6
source   roster
```

### `cremind channels groups available`

**Purpose.** List the groups this channel's account is **already** in.

**Syntax.**

```bash
cremind channels groups available <channel_id>
```

**Flags.** None beyond the root `--json`.

**Behavior.** The route into a group nobody added the agent to. A join event
only fires while Cremind is running, so groups the account belonged to before
the feature was switched on are never announced — this asks the platform
directly. Needs the channel **running**. The `TRACKED` column shows `-` for a
group Cremind does not know about yet, or its status where it does. Platforms
that cannot enumerate groups (a Telegram bot, the Zalo bot) say so instead of
returning an empty list.

**Example.**

```bash
$ cremind channels groups available e2e8...d4f1
CHAT_ID              TITLE        MEMBERS  TRACKED
-1001987654321       Ops room     6        approved
-1001222333444       Lunch club   12       -
```

### `cremind channels groups add`

**Purpose.** Enable one or more groups the account is already in.

**Syntax.**

```bash
cremind channels groups add <channel_id> [--title TEXT] -- <chat_id>...
```

A Telegram or Zalo chat id starts with a minus sign, which every CLI parser
reads as the start of an option — hence the `--`.

**Flags.**

| Flag | Meaning |
|------|---------|
| `--title` | Title to store. Only meaningful when adding a single chat id; otherwise the platform's own name is used. |

**Behavior.** Approved on the spot — naming a specific group out of your own
list **is** the approval, so there is no second step and no notification. Each
group gets its conversation and a roster refresh immediately. A group Cremind
already knows is approved rather than duplicated, so this is also the quickest
way to accept one that is sitting pending.

**Example.**

```bash
$ cremind channels groups add e2e8...d4f1 -- -1001222333444
GROUP_ID   CHAT_ID           TITLE       STATUS
9a1b...c7  -1001222333444    Lunch club  approved
```

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

**The account is in the group but nothing happens** — Work down the list; each
step is silent by design, which is why nothing shows up in the group itself:

1. **Group chats are off for the channel.** The default. Nothing is stored and
   no notification is raised while the flag is off, so an empty
   `cremind channels groups list <id>` is the expected result. Turn it on with
   `cremind channels edit <id> --group-chats`.
2. **A platform prerequisite is missing.** Telegram privacy mode still enabled
   (@BotFather → `/setprivacy` → **Disable**, or make the bot an admin),
   Discord's MESSAGE CONTENT INTENT off (messages arrive with an empty body),
   the Slack app missing `channels:history` / `groups:history` /
   `channels:read` or never reinstalled after adding them, or — on WhatsApp and
   Zalo — the paired account not actually being a member. See the table in
   **Group chats on a channel**.
3. **The group is still `pending`.** The agent reads nothing until a human
   approves it: `cremind channels groups list <id> --status pending`, then
   `cremind channels groups approve <id> <group>`.
4. **The member is denied.** Under `everyone` a denied account's messages are
   dropped outright; under `selected` anybody off the allow list is. Check with
   `cremind channels groups members <id> <group>` — the `RESPONDS` column is the
   verdict.
5. **The judge decided it was not for the agent.** With `respond_mode`
   `mention_or_relevant`, a message that does not mention the agent goes to a
   relevance check that **fails closed** — a timeout, a provider error or no
   `low`-tier model configured all read as "not relevant". The message is still
   stored, just not answered. `@mention` the agent to be certain, or look for
   the decision in `logs/app.log` (`[channel_group]`).

A loop brake is the sixth possibility: 20 agent posts a minute, or 8 consecutive
bot-authored messages with no human, and the agent stays quiet until a person
posts.
