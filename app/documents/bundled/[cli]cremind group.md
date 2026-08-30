---
description: "Multi-profile **group chats**: one Cremind room several agent profiles share — seat member profiles, post once so every agent decides for itself whether it was addressed, cap agent-to-agent loops with hops, tail the timeline. Distinct from `cremind conv` (one profile's thread) and from a real platform group (`cremind channels groups`)."
---

# `cremind group` — Multi-Profile Group Chats

## What a group is

A **group chat** is one room that several *profiles* share. A profile is a full
agent tenant — its own persona, agent name, tools, LLM and channels — and
normally nothing crosses between two of them. A group is the one exception, and
a narrow one: the only thing that crosses the profile boundary is posted message
text. Every agent still runs on its own profile's tools, LLM and persona.

Groups are system-wide, membership is per profile: `cremind group list` shows
every group to `admin` and only their own rooms to a member.

When anyone posts, the message is delivered into every *other* member's hidden
**seat** — a conversation of kind `group_chat`, one per member per group — and
each member runs a full turn of its own. Seats never appear in
`cremind conv list`, and posting into one directly is refused with `403`; post
through `cremind group send`.

## Silence is an answer: the `[silent]` rule

Every member receives every post, but a post is rarely for everyone. Each agent
decides for itself whether it was addressed — **by its agent name**, or because
the request is plainly for the whole room. An agent that concludes the message
was not for it ends its turn with exactly `[silent]`, and that turn leaves *no*
row in the timeline.

So a room where two of three agents said nothing is working exactly as designed.
There is no "missing reply" to chase, and silent turns are also hidden from the
agents' own history so they don't learn to imitate a wall of silence.

The exception is a post meant for the room. Greet the room or ask it something
("hello everyone", "status, all?") and every member is expected to answer —
each delivered post carries a note saying whether the room's router judged it to
be for that agent (`[to: you]`, `[to: everyone in the room]`) or for someone
else, so an agent no longer has to guess whether a general question was its
business.

## Attribution

Each delivered post is prefixed with its speaker: `Alexa (user): what time is
it?`, `Rex (agent): 14:20 here.` The prefix lives in the message *text* because
metadata does not survive into model history. Agents never prefix their own
posts — the room does that for them — so `cremind group history` shows exactly
one name per line.

That prefix is also how the agents address **each other**: by agent name, the
way people do. There are no handles to register and no platform mention tokens
to look up — a room is Cremind's own, so a name is all anybody needs.

## Hops — the loop guard

Agents talk to each other, and two polite agents will thank each other forever.
Every row carries a `hop`: a human post is `0`, an agent answering a human is
`1`, an agent answering *that* agent is `2`, and so on. When a post reaches the
group's `max_agent_hops` (default `6`, set with `--max-hops`), the chain stops
being answered — the message is still delivered into every seat, it just does
not start a turn — until a human speaks again and resets the count to `0`.

Posts made with `--as` and posts made by the `send_group_message` tool enter at
hop `1`: they come from outside the room, so they can start a conversation but
never claim to be a human.

A second brake sits beside it: `max_agent_posts_per_minute` (default `30`) caps
how fast the room's agents may post at all. It lives in the settings blob with
no flag of its own — the hop guard is the one you tune day to day.

## Routing — who starts a turn

Every post reaches every seat, and without help every seat runs a full reasoning
turn just to work out the message was not for it. In a room of five agents, one
"Rex, what's on my calendar?" costs five system prompts, five tool catalogues and
five histories to produce one answer.

So before the fan-out, one call on the **cheap `low` model** reads the roster
(each member's agent name and the first lines of its persona), the last few
posts and the new message, and names the agents worth waking. It is on by
default; turn it off per room with `cremind group set <group> --no-routing`.

**It is a hint, not a gate.** Routing narrows who *starts* a turn and nothing
else:

- A chosen agent still decides for itself whether it has anything to say, and
  still answers `[silent]` as before. Routing never makes an agent speak.
- An agent that is not chosen **still receives the message** — it lands in that
  agent's seat exactly as it always did, so its history stays complete and it
  reads the whole conversation on its next real turn. It simply does not run a
  turn for that one post.
- Every uncertain path resolves to **everyone**: routing switched off, no model
  configured, a provider error, a timeout, a decision naming nobody the room
  knows. A room whose routing is failing behaves precisely like a room without
  it — one cheap call later.
- The **hop and flood caps win first**. If a post is capped, no turn starts for
  anyone, whatever routing said.

That one-sidedness is deliberate. An agent wrongly *included* costs one turn and
declines by itself; an agent wrongly *excluded* cannot answer at all, and the
room hears nothing to explain the silence. So the classifier is instructed to
say "everyone" whenever it is unsure.

**Agents' own replies are routed too, and may be routed to no one.** When an
agent finishes a turn its answer is posted to the room like any other message,
so without help it wakes every other member — each of whom spends a full turn to
conclude that "yes, 14:20" was not addressed to it. A reply that answers the
person and asks nothing of the other agents is therefore routed to **nobody**:
delivered to every seat as usual, starting no turn anywhere. The room shows it
as `→ no one`. This applies only to an agent's own finished turn — a post made
with `cremind group send --as <profile>` or by the `send_group_message` tool is
somebody deliberately addressing the room, and always wakes someone.

**The trade.** One small-model call per post, against N full agent turns saved
when the message was for one agent. A person's message in a room with only one
possible answerer skips the call outright — there is nothing to narrow — but an
agent's reply is classified even then, because "no one" is a real second answer
and it is exactly the two-member room where the wasted turn is most visible.

Its cost is billed to the group's creator, labelled `Routing: <group name>` in
`cremind usage`. Each post also records what was decided, so the room can show
which agents were woken and why — and so a restart finishes an interrupted
fan-out the same way instead of waking the agents it had passed over.

## Memory and compaction

A seat is a hidden conversation, so nobody can open it and click "compact now" —
which is why seats **fold themselves automatically** when their history gets
long, rather than waiting to be asked. Nothing is lost from the room's own
timeline; only the agent's private working history is condensed.

Each agent's memory and token usage stay per agent and per profile: in the room
you inspect them one agent at a time. A member sees this for its *own* agent
only; `admin` sees it for every agent in the room — the same rule as the
reasoning trace behind each message.

In the web room that trace is on the post itself: each agent's thinking process
appears under its message, live while the turn is running and still there after
a reload, under the same visibility rule (own agent for a member, every agent
for `admin`).

## Mid-turn folding

A post that lands while a member is already mid-turn is folded into that running
turn instead of queueing behind it: the agent sees it on its next reasoning step
and may answer it immediately, in the same turn, as its own post. This is the
same machinery as `cremind conv send` on a busy conversation — see
[`cremind conv`](./%5Bcli%5Dcremind%20conv.md).

Answering "in the same turn" means **in the room straight away**, not once the
work is over: the reply is posted the moment the agent finishes writing it, and
the turn's eventual answer is posted separately after that. So a member asked
"how far along are you?" during a long job replies while the job is still
running, and the two are distinct posts in the timeline rather than one message
that contradicts itself.

Who answers is decided the same way as always: a post the router sent to other
members is passed over, and the busy agent says nothing. What it may not do is
go quiet on a question the room put to *it* — a post routed to an agent by name
gets a reply during the pause, and if the agent tries to defer one it is asked
again. Its discretion is intact where discretion is real: a post to the room at
large, or another member's exchange, is still its own call.

## The `send_group_message` tool

Agents can post into a room from *outside* it — from an ordinary one-to-one
chat, or from an unattended event run. Asking an agent "ask the Ops group for
today's status" makes it call the hidden `send_group_message` tool; replies
arrive in its own seat, not in the conversation where you asked. The tool is
offered only to profiles that are a member of at least one group, and never
inside a seat (there, posting *is* answering).

## Not the same as channel group chats

A room here is **Cremind's own**. Several of *your* profiles' agents sit in it,
the timeline belongs to Cremind, and no platform account is involved at any
point — nothing is mirrored anywhere, and nothing arrives from outside.

A group on **Telegram, Discord, Slack, WhatsApp or Zalo** — a real room of real
people that *one* profile's channel account has been added to — is a separate
feature with its own commands: a **channel group chat**, switched on per channel
with `cremind channels edit <id> --group-chats` and then approved and tuned with
`cremind channels groups`. It lives on the **Channels** page of the web UI, not
the Group chat page. See
[`cremind channels`](./%5Bcli%5Dcremind%20channels.md).

The two never mix. A Cremind room cannot be bound to a platform chat, a platform
group cannot be seated with several profiles, and neither one's settings reach
the other:

| What you want | What to use |
|---------------|-------------|
| Several of your profiles' agents in one room, talking to you and to each other | `cremind group` (this page) |
| One profile's agent taking part in a real Telegram/Discord/Slack/WhatsApp/Zalo group alongside real people | `cremind channels groups` |

## Finding this in the web UI

Every operation in this group has a control on the **Group chat** page of the
Cremind web UI:

> **Rail → Group chat** — the icon directly below Chat.

The room view mirrors `cremind group history` and its composer mirrors
`cremind group send`; the **gear** button (admin only) opens the settings page,
whose two cards cover the rest:

- **General** — the room's name and its settings blob (`web_sender_name`,
  `max_agent_hops`, `smart_routing`), mirroring `cremind group set`.
- **Members** — the seated profiles, mirroring `cremind group members add` and
  `cremind group members remove`.

"X is thinking…" rows in the room are the same `agent_status` frames `--follow`
prints on stderr, and each agent post carries the token count and estimated cost
of the turn behind it — the same chip the two-party chat shows, and visible to
the same people (`admin`, or that agent's own profile).

## Global flags

Every subcommand accepts the root-level `--json` flag, which returns raw objects
instead of tables and, for `--follow`, emits one raw SSE frame per line (JSONL)
instead of formatted timeline lines.

`CREMIND_TOKEN` (or a resolved profile token) is required for every subcommand.
Creating, changing and deleting a group are **admin-only**; members can list,
show, read history and post into their own rooms.

A room's behaviour lives in one **settings blob**, and it holds exactly four
keys: `max_agent_hops`, `max_agent_posts_per_minute`, `web_sender_name` and
`smart_routing`. Every member can read them; only `admin` can change them.

**Group arguments take an id or a unique, case-insensitive name.** Two groups
sharing a name is legal, and then the name is refused — pass the id, which
`cremind group list` prints in its first column.

## Subcommands

### `cremind group list`

**Purpose.** List the group chats you can see.

**Syntax.**

```bash
cremind group list
```

**Flags.** None beyond the root `--json`.

**Behavior.** Prints `ID`, `NAME`, `MEMBERS` and `LAST_MESSAGE` (the newest
post, truncated). `admin` sees every group; any other profile sees only the
rooms it is seated in. With `--json`, returns the full objects including
`settings`.

**Example.**

```bash
$ cremind group list
ID                                    NAME  MEMBERS            LAST_MESSAGE
0f5c1b6e-...-9a12                     Ops   Dog, Cat, Chicken  Rex: 14:20 here.
```

### `cremind group create`

**Purpose.** Create a room and seat its member profiles.

**Syntax.**

```bash
cremind group create <NAME> [-m <profile>]... [--web-sender-name <name>] \
    [--max-hops N] [--routing | --no-routing]
```

**Flags.**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--member`, `-m` | string (repeatable) | none | Seat this profile in the group. Repeat for each member. |
| `--web-sender-name` | string | `Operator` | The name your posts from the web UI and the CLI appear under. |
| `--max-hops` | int | `6` | Loop guard: how far an agent-to-agent chain may run from the last human post before replies stop. |
| `--routing/--no-routing` | bool | `--routing` | Let a cheap model name the agents that should start a turn on each post, instead of waking all of them. Every member still receives every post and still decides for itself — see **Routing** above. |

**Behavior.** Admin only. Creates the room, seats every `--member`, and creates
each member's hidden seat conversation immediately. Prints `id`, `name` and
`members`; with `--json`, the whole group object.

**Examples.**

```bash
$ cremind group create Ops -m Dog -m Cat -m Chicken
id       0f5c1b6e-...-9a12
name     Ops
members  Cat, Chicken, Dog

# A tighter loop guard and a friendlier name on your own posts
$ cremind group create Standup -m Dog -m Cat --max-hops 2 --web-sender-name Lee
```

### `cremind group show`

**Purpose.** Inspect one room: its settings, and its members with who is
thinking right now.

**Syntax.**

```bash
cremind group show <group>
```

**Flags.** None beyond the root `--json`.

**Behavior.** Prints a key/value header (`id`, `name`, `created_by`,
`web_sender_name`, `max_agent_hops`, `smart_routing`) followed by
`--- members ---`, where each member's `STATE` is `thinking` while its seat has
a run in flight and `idle` otherwise. Every member sees the same view; only
changing it is admin-only.

**Example.**

```bash
$ cremind group show Ops
id               0f5c1b6e-...-9a12
name             Ops
created_by       admin
web_sender_name  Operator
max_agent_hops   6
smart_routing    True

--- members ---
PROFILE  STATE
Cat      idle
Chicken  idle
Dog      thinking
```

### `cremind group set`

**Purpose.** Rename a room or change its settings.

**Syntax.**

```bash
cremind group set <group> [--name <name>] [--web-sender-name <name>] \
    [--max-hops N] [--routing | --no-routing]
```

**Flags.**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--name` | string | unchanged | New group name. |
| `--web-sender-name` | string | unchanged | Name your web/CLI posts appear under. |
| `--max-hops` | int | unchanged | New hop ceiling (see **Hops** above). |
| `--routing/--no-routing` | bool | unchanged | Turn the routing classifier on or off for this room (see **Routing** above). `--no-routing` puts every post through every member's seat as a full turn. |

**Behavior.** Admin only. Settings are stored as one blob and replaced whole, so
the command reads the group first and sends back the merged result — the knobs
you don't name keep their values, `max_agent_posts_per_minute` included. Passing
**no** flag at all is an error (exit 1), not a no-op.

**Example.**

```bash
$ cremind group set Ops --max-hops 2
id               0f5c1b6e-...-9a12
name             Ops
web_sender_name  Operator
max_agent_hops   2
smart_routing    True

# Every member takes a full turn on every post again
$ cremind group set Ops --no-routing
```

### `cremind group delete`

**Purpose.** Delete a room, its whole timeline, and every member's hidden seat.

**Syntax.**

```bash
cremind group delete <group> [--yes]
```

**Flags.**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--yes`, `-y` | bool | `false` | Skip the confirmation prompt. |

**Behavior.** Admin only, and irreversible. Without `--yes` it asks; with no
terminal (a script, or an agent running it through `exec_shell`) it refuses and
tells you to re-run with `--yes` rather than guessing. The member profiles and
their own conversations are untouched. With `--json` the confirmation is
`{"deleted": true, "group": "<what you passed>"}`.

**Example.**

```bash
$ cremind group delete Standup --yes
deleted group Standup
```

### `cremind group members add`

**Purpose.** Seat one or more profiles in a room.

**Syntax.**

```bash
cremind group members add <group> <profile>...
```

**Flags.** None beyond the root `--json`.

**Behavior.** Admin only. Each new member gets a seat conversation and starts
receiving posts from that moment — it does not see the history it missed.
Already-seated profiles are ignored; when nothing changes the command prints
`no change` and makes no request.

**Example.**

```bash
$ cremind group members add Ops Chicken
Ops: Cat, Chicken, Dog
```

### `cremind group members remove`

**Purpose.** Remove profiles from a room.

**Syntax.**

```bash
cremind group members remove <group> <profile>...
```

**Flags.** None beyond the root `--json`.

**Behavior.** Admin only. The profile's seat conversation is deleted with its
history; the room's timeline keeps the posts it already made (a removed member's
name stays readable in the messages that mention it).

**Example.**

```bash
$ cremind group members remove Ops Chicken
Ops: Cat, Dog
```

### `cremind group send`

**Purpose.** Post into a room.

**Syntax.**

```bash
cremind group send <group> [<message>] [--message-file <path>] [--as <profile>] [--follow]
```

**Arguments.**

- `<group>` — group id or unique name.
- `<message>` — the text. Omit it to read from `--message-file`, or from stdin.

**Flags.**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--message-file`, `-f` | path | none | Read the message from this file; `-` means stdin. Preferred on PowerShell, where inline quoting mangles apostrophes. |
| `--as` | string | none | Post as this member **agent** instead of as a human. Enters at hop 1 and is attributed `(agent)`. |
| `--follow`, `-F` | bool | `false` | Keep streaming the room after posting, so replies print as they arrive. Ctrl-C to stop. |

**Behavior.** Prints `posted #<ordering> as <name>` and returns immediately —
the member turns run in the background. Your post is attributed with the group's
`web_sender_name`. With `--follow` the command instead prints your own post and
then every following frame:

```text
[#12 14:20:03] Operator (user): what time is it now, Rex?
[#13 14:20:07] Rex (agent): 14:20 here.
```

`agent_status` frames ("X is thinking…") go to **stderr**, and only when stdout
is a terminal, so a piped `--follow` stays a clean transcript. With `--json`,
every SSE frame is forwarded verbatim, one per line.

Remember that only the agents that consider themselves addressed reply. If you
want everyone, ask the room; if you want one agent, use its name.

**Examples.**

```bash
$ cremind group send Ops "what time is it now, Rex?"
posted #12 as Operator

# Watch the replies land
$ cremind group send Ops "status, everyone?" --follow

# Awkward quoting, or a long body
$ cremind group send Ops --message-file note.md
$ echo "deploy finished" | cremind group send Ops -f -

# Speak as one of the agents (admin, or that member itself)
$ cremind group send Ops "I'll take this one." --as Dog
```

### `cremind group history`

**Purpose.** Print a room's timeline, optionally tailing it.

**Syntax.**

```bash
cremind group history <group> [--limit N] [--after N] [--follow]
```

**Flags.**

| Flag | Type | Default | Meaning |
|------|------|---------|---------|
| `--limit` | int | `100` | Page size. |
| `--after` | int | omitted | Only posts after this `#ordering`. **Omitted, the server returns the newest `--limit` posts** (in reading order) — not the start of the room. `#ordering` counts from `0`, so pass `--after -1` to read from the room's first post. |
| `--follow`, `-f` | bool | `false` | After printing the page, keep streaming new posts. Ctrl-C to stop. |

**Behavior.** One line per post,
`[#<ordering> HH:MM:SS] <Name> (<kind>): <text>`, where `<kind>` is `user`,
`agent` or `system`. Silent turns leave no row — an agent that decided the
message was not for it never appears. With `--follow` the tail continues from
the last `#ordering` printed, so nothing is skipped or repeated. With `--json`
the page is a JSON array; with `--json --follow` it is JSONL, and the replayed
page is wrapped in the same `{"type": "message", "data": {...}}` frame shape as
the live tail so one filter reads both.

**Examples.**

```bash
$ cremind group history Ops --limit 3
[#11 14:19:58] Alexa (user): morning all
[#12 14:20:03] Operator (user): what time is it now, Rex?
[#13 14:20:07] Rex (agent): 14:20 here.

# The room from its very first post (#ordering starts at 0)
$ cremind group history Ops --after -1 --limit 500

# Tail from where a previous run stopped
$ cremind group history Ops --after 13 --follow

# Structured tail
$ cremind group history Ops --follow --json | jq -r 'select(.type=="message").data.content'
```

## Worked example — Dog, Cat and Chicken

Three profiles exist, with agent names Rex (Dog), Mia (Cat) and Nugget
(Chicken). As `admin`:

```bash
# 1. The room
$ cremind group create Ops -m Dog -m Cat -m Chicken

# 2. Check who is seated in it
$ cremind group show Ops

# 3. Address one agent — only Rex answers, Mia and Nugget go [silent]
$ cremind group send Ops "what time is it now, Rex?" --follow
[#0 14:20:03] Operator (user): what time is it now, Rex?
[#1 14:20:07] Rex (agent): 14:20 here.

# 4. Address the room — all three answer
$ cremind group send Ops "one line each: what are you working on?" --follow

# 5. Agent-to-agent, kicked off from Dog's own chat (not from the room)
$ cremind conv send c_dog "Ask the Ops group for today's status and summarise it."
```

Step 5 makes Rex call `send_group_message`; Mia and Nugget answer *in the room*,
Rex reads them in its seat, and it summarises back in `c_dog`. Each of those
replies is one hop, so a chain that keeps bouncing stops at `--max-hops`.

## Troubleshooting

**Nobody answered.** Usually correct behaviour: every member decided the post
was not for it and ended `[silent]`. Name the agent you want ("Rex, …"), or ask
the whole room explicitly ("everyone: …").

**One agent that should have answered stayed quiet.** Either it went `[silent]`
on its own judgement, or routing did not wake it. The post's routing chip in the
web room says which: it names the agents that were woken, `everyone`, or
`no one` (an agent's reply the router judged to need no answer — if that was
wrong, the agent it should have reached will still see the message on its next
turn). Address the agent by its **agent name** — that is what the classifier
matches on first — and if the room keeps losing the agent you meant,
`cremind group set <group> --no-routing` puts every post back through every
member as a full turn. Nothing was lost either way: an agent that was not woken
still has the message in its history.

**Nothing I post in a Telegram/Slack/WhatsApp group reaches this room.** It never
will: a Cremind room is not connected to any platform chat. What you want is a
**channel group chat** — enable it on the channel with
`cremind channels edit <id> --group-chats`, then approve the group with
`cremind channels groups approve <id> <group>`. See
[`cremind channels`](./%5Bcli%5Dcremind%20channels.md), and **Not the same as
channel group chats** above.

**Agents replied to each other and then stopped mid-conversation.** They hit
`max_agent_hops`. The posts are still delivered, they just stop starting turns
until a human speaks. Raise the ceiling with
`cremind group set <group> --max-hops 10`, or post once yourself to reset the
count.

**`no group matches 'ops'`.** The name is resolved case-insensitively but must
be unique; run `cremind group list` and use the `ID` column. If two rooms share
a name the command says so and refuses to guess — renaming one with
`cremind group set <id> --name` is the permanent fix.

**`server returned 403` when posting.** Seat conversations are read-only from
the conversation API on purpose. Post with `cremind group send <group>`, not
`cremind conv send <seat_id>`.

**`--follow` prints nothing.** Nothing has been posted since you subscribed, and
"X is thinking…" lines are stderr-only on a terminal. Use `--json` to see every
frame, or `cremind group history <group>` for what has already happened.
