# Cremind Event System Guide

Read this before designing or building any skill that reacts to things happening
elsewhere. Events are what set Cremind skills apart from plain Agent Skills: a
skill can run an action *automatically* when something occurs, without the user
asking each time.

The frontmatter side (`metadata.events`, `metadata.long_running_app`) is in
`spec.md`. This file covers how events actually flow, the on-disk contract, how
to build a listener, and how to test.

---

## 1. The event pipeline (and why it behaves the way it does)

1. **One recursive watch per profile** is mounted on the whole profile skills
   directory (`~/.cremind/<profile>/skills/`). It sees every file created under
   any skill.
2. A file is treated as an event **only** if its path is exactly
   `<skill>/events/<event_type>/<file>.md` — a `.md` file, two levels below the
   skill's `events/` directory. Files at the wrong depth, or non-`.md` files
   (e.g. `scripts/.listener.lock`), are ignored (strays at the wrong depth are
   deleted as junk).
3. When a matching file appears, Cremind **reads it** (with a few short retries
   in case it is still being written), then **deletes it immediately**. Events
   are single-use: they are never replayed. If nobody is subscribed, the file is
   still consumed and dropped — that's by design.
4. The content is **fanned out** to every subscription matching
   `(profile, skill, event_type)`. Each match enqueues a run on that
   subscription's conversation.
5. Runs for one conversation are **sequential**; different conversations run
   concurrently.
6. Before spending a full turn, a cheap **relevance gate** classifies whether the
   event content satisfies the subscription's `action` condition. It **fails
   open** — if it can't decide, it runs. (So `action` can carry a fine-grained
   condition like "only when the sender is my manager".)
7. The agent runs on the subscribed conversation with, essentially,
   `action + "\n\n" + <the event file's content>`. The trigger is recorded as a
   structured bubble in that conversation.

Consequences you must design around:

- **Single-use, no replay, wiped on boot.** All `events/**/*.md` are cleared at
  startup before listeners spawn. Never treat `events/` as storage or a queue you
  can read back — it is a fire-and-forget drop-zone.
- **The folder is the API.** Any process that can write a well-formed file into
  `events/<type>/` triggers the pipeline — the skill's own listener, a cron job,
  another tool, even a human dropping a file for a test. A listener is the usual
  producer, but it is not the only way.
- **Emit only declared event types.** The `<event_type>` in the path must be one
  of the skill's declared `metadata.events.event_type[].name`, and the folder name
  must match exactly.

---

## 2. Event file contract (normative)

An event file is Markdown with a YAML frontmatter block. The content lands
verbatim in a conversation, so write the body for a human/LLM reader.

Required frontmatter keys:

- **`event_type`** — must equal the folder name it's written into.
- **`received_at`** — ISO 8601 timestamp (e.g. `2026-07-02T09:00:05+00:00`).

Everything else is domain-specific. Example (`events/new_item/…md`):

```markdown
---
id: "abc-123"
title: "Quarterly report is ready"
source: "reports-service"
url: "https://example.com/items/abc-123"
event_type: "new_item"
received_at: "2026-07-02T09:00:05+07:00"
---

The quarterly report finished generating and is ready for review.
Owner: Alice. Size: 2.3 MB.
```

### Filename convention

`<YYYY-MM-DDTHH-MM-SS> <short-label>.md`, e.g.
`2026-07-02T09-00-05 Quarterly report is ready.md`.

The label must be filesystem-safe. Sanitize it: replace `<>:"/\|?*` and control
characters, collapse whitespace, trim, cap at ~100 characters, guard against
Windows reserved names (`con`, `prn`, `aux`, `nul`, `com1`–`com9`, `lpt1`–`lpt9`),
and add a ` (2)`, ` (3)`… suffix on collision.

### Write it atomically

Create the file with `os.O_CREAT | os.O_EXCL` and write UTF-8 with `\n`
newlines. Writing atomically (and picking a fresh name on `EEXIST`) prevents the
watcher from reading a half-written file and prevents two producers from
clobbering each other. The `write_event()` helper in `templates.md` implements
all of this correctly — copy it rather than re-deriving it.

---

## 3. Do you even need a listener?

You need a `long_running_app` listener only if **something must run continuously**
to notice events:

- **Push source** (webhooks, a message relay, a socket): a daemon that stays
  connected and writes an event file when notified.
- **Polling source** (an API with no push): a daemon that wakes on an interval,
  diffs against a stored cursor, and writes files for what's new.

You do **not** need a listener if events are produced some other way — e.g. another
tool or an external system writes into `events/<type>/` directly. In that case
declare `metadata.events` (so subscriptions and folders exist) and skip
`long_running_app`.

Keep the smallest design that works. Don't add a listener speculatively.

---

## 4. Listener contract

A listener is a long-running Python program (run as `uv run
scripts/event_listener.py` from the skill directory). It must:

- **Read config only from `scripts/.env`** (materialized from Settings). No chat
  prompts, no other config source.
- **Baseline on first run.** On the very first start, record the current cursor
  (latest id / timestamp / history marker) and emit **nothing** for pre-existing
  items. Emitting the entire backlog as "new" events on first run is the classic
  bug — it floods the user's conversations. Built-ins (gmail, jira, …) all
  baseline.
- **Bounded catch-up.** On later starts, emit what genuinely changed while
  offline, but cap it — never replay an unbounded backlog.
- **Deduplicate.** At-least-once sources deliver duplicates; track emitted ids so
  each real event yields exactly one file.
- **Single instance.** Guard with a lock file so two copies don't double-emit.
- **Persist state** in `scripts/.listener_state.json` (write to a temp file then
  `os.replace` for atomicity). Gitignore it.
- **Shut down cleanly** on SIGINT/SIGTERM.
- **Emit only declared event types**, into their matching folders.

The template in `templates.md` implements the lock, state, signal handling,
sanitizer, and atomic writer; you customize only the "how do I learn about new
items" part (poll vs. push).

Note: the built-in mail/calendar skills use a hosted **Cremind Connect relay** to
receive push nudges without exposing credentials. That relay is built-in-only
infrastructure — for a user skill, use the provider's own webhook/API or polling.

---

## 5. Subscriptions

A subscription binds one conversation to `(skill, event_type, action)`. It is
created by the agent calling the skill's own tool with a `subscribe` object:

```
subscribe:
  trigger: [new_item]          # one or more declared event names
  action: "Summarize the item and post it to the team channel"
```

- One row is written per trigger; triggers are validated against the skill's
  declared events.
- **Subscribing is refused while the agent is itself reacting to an event**
  (anti-recursion), so an event handler can't spawn more subscriptions.
- Subscriptions are per conversation and per profile. An event only fires the
  subscriptions in the same profile that declared them.

Manage subscriptions from the CLI:

- `cremind skill-events list` — list subscriptions (with their ids).
- `cremind skill-events delete <sub_id>` — remove one.

---

## 6. Testing and operations

| Command | What it does |
|---|---|
| `cremind skill-events events <skill>` | List the events a skill declares (reads its `SKILL.md`). Succeeds only if the skill is **registered** — a good post-write registration check. |
| `cremind skill-events list` | List subscriptions and their ids. |
| `cremind skill-events simulate <sub_id>` | Inject a synthetic event for that subscription (body from stdin; optional `--filename`) and watch the conversation react. The end-to-end test. |
| `cremind skill-events delete <sub_id>` | Delete a subscription. |
| `cremind skill-events listener-status <skill>` | Listener heartbeat/status. |
| `cremind skill-events listener-start <skill>` | Start the declared `long_running_app` listener now (also respawned on boot). |
| `cremind skill-events stream` | Stream the admin snapshot (SSE). |
| `cremind skill-events notifications` | Tail per-profile skill-event notifications (SSE). |

### Recommended test sequence

1. **Registration:** after writing the skill, wait ~2s (watcher debounce), then
   `cremind skill-events events <name>`. A listing (even empty) proves it parsed
   and registered. An "unknown skill" error means the frontmatter failed to parse
   — run `scripts/validate.py` and fix.
2. **Pipeline armed (no subscription needed):** hand-write a spec-conformant file
   into `events/<type>/` (correct frontmatter, sane filename). If the watch is
   armed it **disappears within ~1s** (consumed; with no subscribers it fans out
   to nobody). If it lingers, the path/format is wrong.
3. **End-to-end:** load the skill in a conversation and ask for an automation so
   the agent subscribes; `cremind skill-events list` to get the `sub_id`; then
   `cremind skill-events simulate <sub_id>` and confirm the conversation reacts.
4. **Listener (if any):** `cremind skill-events listener-start <name>` then
   `listener-status <name>`.
