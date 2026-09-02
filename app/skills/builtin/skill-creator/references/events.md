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
   `(profile, skill, event_type)`. Each match becomes its own run.
5. Each run executes in its **own hidden `event_run` conversation** — never in
   the subscribed chat itself. Runs of a single subscription are sequential (one
   FIFO per subscription); different subscriptions run concurrently, up to the
   `[event_runs] max_parallel_runs` budget.
6. Before spending a full turn, a cheap **relevance gate** classifies whether the
   event content satisfies the subscription's `action` condition. It **fails
   open** — if it can't decide, it runs. (So `action` can carry a fine-grained
   condition like "only when the sender is my manager".)
7. The agent runs with, essentially,
   `action + "\n\n" + <the event file's content>`. The trigger is recorded as a
   structured bubble in that hidden run conversation.
8. When the run finishes, **its result is reported back into the conversation
   that registered the subscription**, as a new turn there (or folded into a
   turn already running there, with a one-line heads-up that it arrived). This
   happens for *every* firing of a standing subscription, not just for one-shot
   tasks. Only a subscription with nowhere to report — one bound to a reserved
   host conversation, or to a conversation since deleted — produces no turn; its
   runs surface as notifications only.

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
  bug — it floods the user's conversations. Built-ins (imap-email, gdrive, jira, …)
  all baseline.
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
  action: "Extract the item's title, owner and link, and report them"
  task: true                   # optional — ONE-SHOT (next occurrence only)
  timeout_minutes: 1440        # optional — only valid together with `task`
```

- One row is written per trigger; triggers are validated against the skill's
  declared events.
- **Every subscription reports its runs back.** When a run finishes, its result
  lands in the conversation that registered the subscription as a new turn. So
  `action` must say what to **extract and report** — "extract the sender and
  what they are asking for, and report them", never "notify the user". The
  reporting is automatic; an action phrased as a notification produces a turn
  whose whole content is "I notified them", which is worth nothing to read.
- The action must be **self-contained**: the run happens in a fresh hidden
  conversation that sees only `action` plus the event content, so a gate can
  reject one that leans on context the run will not have ("the file we discussed
  earlier", "continue what I asked for").
- **`task: true` makes the subscription ONE-SHOT**: it waits for the *next*
  occurrence only, runs once, reports, then terminates. It requires exactly one
  `trigger`. Without it the subscription is **standing** — it fires on every
  occurrence, indefinitely, reporting each one.
- **`timeout_minutes`** (1–43200, default 10080 = 7 days) is valid only
  alongside `task`. When the deadline passes with no event, the task gives up and
  reports "the event never fired", so the waiting conversation is not left
  hanging.
- Anti-recursion is two-tier. **Inside an event run, nothing can be registered
  at all.** On a turn started by a *reported result*, only **one-shot tasks**
  may be registered — a standing rule registered there would re-register itself
  on every result it produced.
- Subscriptions are per conversation and per profile. An event only fires the
  subscriptions in the same profile that declared them.

Manage subscriptions from the CLI:

- `cremind skill-events list|edit|pause|resume|delete` — list subscriptions with
  their ids, re-point a trigger or rewrite an action, pause one without losing
  it (and resume it later), or remove it.

---

## 6. Testing and operations

| Command | What it does |
|---|---|
| `cremind skill-events events <skill>` | List the events a skill declares (reads its `SKILL.md`). Succeeds only if the skill is **registered** — a good post-write registration check. |
| `cremind skill-events list` | List subscriptions and their ids. |
| `cremind skill-events simulate <sub_id>` | Inject a synthetic event (body from stdin; optional `--filename`). The end-to-end test — but **not a dry run**: it writes a real event file, so every subscription for that skill + event type in the profile fires, and each one reports its result into its own conversation (a platform group chat, or a Cremind room, if that is where it was registered). A one-shot `task` spends its single firing on it. |
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
   `cremind skill-events simulate <sub_id>` and confirm the registering
   conversation receives an `[Event result]` turn carrying what the action was
   asked to report.
4. **Listener (if any):** `cremind skill-events listener-start <name>` then
   `listener-status <name>`.

---

## 7. Volume and back-pressure

One run per subscription per event file — so a chatty listener means a chatty
chat. How many turns the user ends up reading is decided by what the listener
emits, not by how the action is worded.

- **Filter at the listener.** Dropping an uninteresting item before writing the
  file is free. Every file written costs a run, a relevance-gate call, and
  usually a turn in someone's conversation.
- **Prefer digest-style actions.** "Report the one-line summary and the link" is
  a fine thing to receive twenty times a day; "walk through the item in detail"
  is not.
- **Results coalesce.** A result landing while that conversation is mid-turn is
  folded into the running turn rather than stacking up behind it.
- **Bounded on purpose.** Only the newest `[event_runs] max_results_per_delivery`
  (default 5) *standing* results per conversation are reported, and standing
  results older than `undelivered_max_age_hours` (default 72h) are dropped as
  stale. Dropped results show as `skipped` in `cremind event-runs list`. One-shot
  task results are never dropped by either bound — one more reason to use
  `task: true` when a conversation genuinely depends on one specific outcome.
