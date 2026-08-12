---
description: "Create, edit, and delete **scheduled and recurring events, reminders, and timers** (one-off or RRULE-recurring) that fire an agent action at a set time; enable or disable the per-profile **Calendar & Schedule** feature, connect or disconnect **Google Calendar**, and pause/resume/cancel schedule subscriptions. Use this to set a reminder, schedule a recurring task, or link a Google calendar — time-based triggers, unlike filesystem or skill events. Also reports **which Google account the calendar is using and where it came from** (`google_source`: the gcalendar skill's link, which takes precedence, or one connected here) — consult this when asked why the calendar shows the wrong account or why connecting Google here is refused. `schedule list` also shows which one-time events are **event tasks** (created by the assistant to wait for a moment, delivering their result back into the conversation that is waiting); tasks cannot be paused or made recurring."
---

# `cremind calendar` — Calendar & Schedule

`cremind calendar` is the CLI for the **Calendar & Schedule** feature: a
per-profile system of time-based events that fire an agent action (or a plain
reminder) on a one-off or recurring schedule. It also drives the optional
Google Calendar connection.

A *schedule event* binds a start time (and optional RRULE recurrence) to an
**action** — a natural-language instruction the agent runs when the event
fires. Manual events created here run in the profile's dedicated
`__schedule__` conversation.

The feature is **off by default per profile** and must be enabled
(`cremind calendar enable`) before events can be created.

Each time a schedule event *fires*, that single firing now runs in its own
isolated, hidden per-run conversation (not one shared `__schedule__` thread),
tracked with a status and token usage. Browse that run history — and reply to a
firing that paused to ask you something — with `cremind event-runs`
(`cremind event-runs list --kind schedule`).

## Finding this in the web UI

Two surfaces back this command group:

> **Sidebar → Calendar & Schedule** — the month grid (`events`), the
> create/edit dialog (`add` / `edit` / `delete`), the feature switch
> (`enable` / `disable`), and the Google Calendar connect button (`google`).
>
> **Sidebar → Events → Schedule Events** — the raw subscription checklist
> (`schedule list`) with its pause/resume/cancel controls (`schedule status`).

Each schedule event on the Events page displays its id (first 8 characters)
labeled **"Event"**, with a copy icon that copies the full id — the exact
`<id>` that `cremind calendar edit` and `cremind calendar delete` accept, and
that `cremind event-runs list --subscription <id>` filters run history by. When
a user pastes such an id ("edit event `…`") use it directly. Ids labeled
**"Run"** on the same page are individual firings — those go to `cremind
event-runs`, not here.

Changes made from the CLI show up live on both pages (they share the same
admin SSE stream).

## Global flags

All `cremind calendar` subcommands accept the root-level `--json` flag.
`CREMIND_TOKEN` is required for every subcommand.

## Subcommands

### `cremind calendar settings`

**Purpose.** Show the per-profile feature switch and Google connection status.

```bash
cremind calendar settings
```

Prints `enabled`, `google_connected`, `google_email`, `google_source`, and
`provider`.

`google_source` says **which** Google credential is in play:

| Value    | Meaning                                                                     |
|----------|-----------------------------------------------------------------------------|
| `skill`  | Linked through the **gcalendar skill**. That link drives the calendar, and `google_email` is the linked address. |
| `app`    | Connected with `google connect` (or the page's button). No address — this flow requests no email scope. |
| empty    | Not connected; the built-in system calendar is in use.                      |

### `cremind calendar enable` / `cremind calendar disable`

**Purpose.** Turn the Calendar & Schedule feature on or off for the active
profile. Disabling also disarms the profile's existing schedule events.

```bash
cremind calendar enable
cremind calendar disable
```

### `cremind calendar events`

**Purpose.** List calendar occurrences in a date window (recurrences are
expanded on demand for the view).

```bash
cremind calendar events [--from <date>] [--to <date>]
```

- `--from` / `--to` — Window bounds, as `YYYY-MM-DD` or an ISO datetime.
  Defaults to roughly the current month (a week before the 1st through ~45
  days out) when omitted.

Renders an `ID / TITLE / START / ALL_DAY / KIND` table; `--json` returns
`{events, from, to}`.

### `cremind calendar add`

**Purpose.** Create a manual schedule event. **Requires the feature enabled**
(otherwise the server returns `409 feature_disabled`).

```bash
cremind calendar add --title <title> --at <dtstart>
                 [--action <instruction>]
                 [--duration <minutes>]
                 [--all-day]
                 [--rrule <RRULE>]
                 [--schedule-kind instant|recurrence]
                 [--recurrence-end-type <type>] [--recurrence-end-value <value>]
```

**Flags.**

| Flag                      | Default                                   | Meaning                                                                 |
|---------------------------|-------------------------------------------|-------------------------------------------------------------------------|
| `--title`                 | — (**required**)                          | Event title.                                                            |
| `--at`                    | — (**required**)                          | Start (`dtstart`): `YYYY-MM-DD` or an ISO datetime.                     |
| `--action`                | the title                                 | Instruction the agent runs when the event fires.                        |
| `--duration`              | `30`                                      | Duration in minutes.                                                    |
| `--all-day`               | off                                       | Mark as an all-day event.                                               |
| `--rrule`                 | none                                      | iCalendar RRULE (e.g. `FREQ=WEEKLY;BYDAY=MO`). Implies a recurrence.    |
| `--schedule-kind`         | `recurrence` if `--rrule` else `instant`  | Force the kind explicitly.                                              |
| `--recurrence-end-type`   | none                                      | How the recurrence ends (e.g. `count` or `until`).                      |
| `--recurrence-end-value`  | none                                      | The count or end date that goes with `--recurrence-end-type`.           |

Prints the created event (key/value, or full JSON with `--json`).

**Examples.**

```bash
# One-off reminder at a specific time
$ cremind calendar add --title "Pay invoice" --at 2026-07-01T09:00:00 \
    --action "remind me to pay the AWS invoice"

# Weekly recurring standup prep
$ cremind calendar add --title "Standup prep" --at 2026-07-06T08:45:00 \
    --rrule "FREQ=WEEKLY;BYDAY=MO" \
    --action "summarize yesterday's commits across my repos"
```

### `cremind calendar edit`

**Purpose.** Edit a schedule event — only the flags you pass are changed.

```bash
cremind calendar edit <id> [--title ...] [--at ...] [--action ...]
                   [--duration ...] [--all-day/--no-all-day] [--rrule ...]
                   [--schedule-kind ...] [--recurrence-end-type ...]
                   [--recurrence-end-value ...]
```

Passing no field flag is an error. Prints the updated event.

### `cremind calendar delete`

**Purpose.** Delete a schedule event.

```bash
cremind calendar delete <id>
```

Silent on success. (Equivalent to `DELETE /api/schedule-events/{id}`.)

### `cremind calendar google connect` / `disconnect`

**Purpose.** Connect or disconnect Google Calendar for the active profile.

```bash
cremind calendar google connect      # prints an authorize URL to open
cremind calendar google disconnect
```

`connect` prints a Google **authorize URL** — open it in a browser and grant
access; the OAuth callback completes the link server-side (same pattern as
`cremind agents auth-url`). If the server can't build the URL (its public URL
or the Google client isn't configured) it returns `409 unavailable`.
`disconnect` drops the stored token.

**The gcalendar skill's link wins.** Linking a Google account through the
`gcalendar` skill (in chat: `uv run scripts/__main__.py link`) also gives the
Calendar & Schedule feature that account — one link, both places, no second
consent screen. While that link is in force (`google_source: skill`):

- `google connect` returns **`409 skill_managed`** rather than mint a credential
  the skill's link would immediately shadow. To change accounts, re-link the
  skill or disable it.
- `google disconnect` clears only the account connected *here*; the skill's link
  keeps driving the calendar, and the command says so.

Disabling the gcalendar skill (or resetting it, which deletes its token) hands
the calendar back to whatever `google connect` connected, or to the built-in
system calendar if nothing was.

Only one account is ever in effect. When the effective account changes, Cremind
forgets where it had mirrored this profile's events — their Google copies live in
the old account and can no longer be reached — so those events show as
Cremind-only until their next edit, which re-creates them in the new account.

While connected, new events are mirrored to Google Calendar, and the `events`
view merges your Google events with Cremind-managed ones. **Sub-daily
recurrences** (hourly / every-few-minutes, e.g. `FREQ=HOURLY`) are the one
exception: Google Calendar only stores `DAILY`/`WEEKLY`/`MONTHLY`/`YEARLY`
recurrences, so a sub-daily reminder stays **Cremind-only** — it still fires and
still shows in `cremind calendar events`, but it will not appear on Google
Calendar. (Creating one through the agent's `schedule_create` tool warns and
asks you to confirm; `cremind calendar add` just keeps it local.)

### `cremind calendar schedule list`

**Purpose.** List the raw schedule-event subscriptions for the active profile
(the Events-page checklist).

```bash
cremind calendar schedule list
```

Renders an `ID / TITLE / KIND / START / STATUS / TASK / CONV_TITLE` table;
`--json` returns the `subscriptions` array.

`TASK` is `yes` for a **one-time event task**: an event the assistant created
while working, for a moment the conversation is waiting on ("check the build at
16:00 and tell me"). When it fires, the result is delivered back into that
conversation as a new turn so the assistant can continue, and the row flips to
`completed`. Recurring events and events you add yourself (from the calendar UI
or `cremind calendar add`) are never tasks — their runs surface as
notifications and on the Events page instead.

### `cremind calendar schedule status`

**Purpose.** Pause, resume (set `active`), or cancel a schedule event.

```bash
cremind calendar schedule status <id> active|paused|cancelled
```

Prints the updated event. Any value other than `active`/`paused`/`cancelled`
is rejected.

**Tasks cannot be paused** (`400 task_pause_unsupported`). Resuming re-seeds an
event's fire time from *now*, so a one-time task whose moment passed while
paused would silently never fire and the conversation waiting for it would
never hear back. Cancel or delete it instead. For the same reason,
`cremind calendar edit` refuses to add an `--rrule` to a task
(`400 task_recurrence_conflict`): a recurring rule never reports back. Create a
separate recurring event if that is what you want.

## Worked examples

### Turn the feature on and add a daily check

```bash
$ cremind calendar enable
enabled: True
$ cremind calendar add --title "Inbox triage" --at 2026-07-01T08:00:00 \
    --rrule "FREQ=DAILY" --action "summarize my unread email and flag anything urgent"
```

### Pause a noisy recurring event, then resume it

```bash
$ cremind calendar schedule list
ID        TITLE          KIND        START                STATUS   TASK  CONV_TITLE
se_4a1f   Inbox triage   recurrence  2026-07-01T08:00:00  active         Schedule
se_9b02   Check CI       instant     2026-08-10T16:00:00  active   yes   Release 2.4
$ cremind calendar schedule status se_4a1f paused
$ cremind calendar schedule status se_4a1f active
```

## Troubleshooting

**`409 feature_disabled` on `add`** — The Calendar & Schedule feature is off
for this profile. Run `cremind calendar enable` first.

**`409 unavailable` on `google connect`** — The server couldn't resolve its
public URL or the Google client. Google Calendar connect needs the
cremind-connect Google client and a reachable public server URL; see the
Calendar & Schedule setup docs.

**`409 skill_managed` on `google connect`** — This profile is already linked to
Google through the **gcalendar skill**, and that link is what the calendar uses.
Connecting a second account here would have no effect. Re-link the skill to
switch accounts, or disable the gcalendar skill to connect one here instead.
`cremind calendar settings` shows which account is in force.

**The Calendar & Schedule page shows a different account than expected** — Check
`google_source`. `skill` means the gcalendar skill's link is in force and takes
precedence over anything connected on the page.

**Created an event but it never fires** — Confirm `cremind calendar settings`
shows `enabled: true` (disabling the feature disarms events) and that
`cremind calendar schedule status` is `active`, not `paused`/`cancelled`.

**Added a recurring reminder but it's not on Google Calendar** — If it's a
sub-daily recurrence (e.g. "every 2 hours" → `FREQ=HOURLY`), Google Calendar
can't store it; it stays Cremind-only. It still fires and still shows in
`cremind calendar events`. Use a daily-or-coarser cadence to have it appear on
Google Calendar.

## Related

- `cremind skill-events` / `cremind file-watchers` — the other two event
  sources that trigger agent runs (skill listeners and filesystem changes).
- `cremind event-runs` — the per-firing run history: each time a schedule event
  fires it runs in its own isolated conversation with a status and token usage,
  viewable (and replyable, when pending) here.
- `app/api/calendar.py` — the Calendar & Schedule API these commands wrap.
