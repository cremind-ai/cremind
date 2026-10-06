---
name: claude-usage-monitor
description: "Track Claude subscription usage across every Claude account used with Claude Code on this computer — how much of each account's 5-hour and weekly limits is used and left, the live burn rate, when the running account hits a limit, and which account to switch to. Raises Cremind events when an account nears or hits a limit, and when a used-up account is available again. Serves a live dashboard: when the user asks to open it, run `dashboard --open`, which opens it in the browser of the computer running Cremind and returns the link. Reads only usage figures and token counts from Claude Code's own files, never login tokens or conversation content."
metadata:
  environment_variables:
    - name: DASHBOARD_PORT
      description: Port of the local dashboard, served on 127.0.0.1 only. When it is taken, the next free port (up to 10 higher) is used.
      required: false
      type: number
      default: '7337'
  events:
    event_type:
      - name: limit_warning
        description: The account in use crossed the heads-up threshold of its 5-hour or weekly limit (default 80% / 90%)
      - name: switch_now
        description: The account in use crossed the switch-now threshold of its 5-hour or weekly limit (default 90% / 95%)
      - name: limit_soon
        description: At the current pace the account in use runs out of its 5-hour or weekly limit within minutes (default 15)
      - name: limit_reached
        description: An account hit its 5-hour or weekly limit; Claude Code stops for it until the limit resets
      - name: account_available
        description: A used-up account's limit has reset, so it can take over again
  long_running_app:
    command: uv run scripts/event_listener.py
    description: The usage monitor. Follows Claude Code's usage figures and session transcripts every 2 seconds, raises the alert events, and serves the dashboard on 127.0.0.1.
---

# claude-usage-monitor

**Purpose:** keep a long Claude Code job from stalling on a usage limit. For every Claude
account used on this computer it shows how much of the 5-hour and weekly limits is gone and
what is left, how fast the account in use burns through both, **when the first of them runs
out**, and **which account to switch to**. Alerts arrive as Cremind events; a live dashboard
shows everything at a glance.

## How it works

A background **monitor** (this skill's listener) on the computer running Cremind reads Claude
Code's own files every 2 seconds: official usage from each profile's `.claude.json` and the
optional status line, and in between estimates (marked **≈**) from the token counts in session
transcripts. It tracks **your Claude Code** (`~/.claude`, the login Cremind's Claude Code tool
falls back to), **this Cremind profile's own Claude Code login** if any, and **extra
profiles**, one per additional account. Method and limits: `cat references/how-it-works.md`.

## Open the dashboard (the usual request)

When the user asks to open, show or see the usage dashboard:

```bash
uv run scripts/__main__.py dashboard --open
```

- It starts the monitor first when it isn't running (a few seconds), then prints the URL.
- `--open` opens it in a new tab of the default browser **on the computer running Cremind**.
  Use it when the user is in the Cremind app or a browser on that computer. **Leave it out**
  when they write from a messaging channel (Telegram, Zalo, WhatsApp …) or say they are on
  another device — nothing should pop up on a screen nobody is looking at.
- Always reply with the link from the output's `link` field (a Markdown link), e.g. "Opened
  it: [Claude Usage Monitor](http://127.0.0.1:7337/)". If `opened_in_browser` is false, say
  it couldn't open by itself and they can click the link. The link works only in a browser on
  the computer running Cremind (it is served on 127.0.0.1).
- On `error`, report it with its `fix`.

## Answer usage questions

"How much Claude usage is left?", "Which account should I switch to?", "When do I hit the
limit?":

```bash
uv run scripts/__main__.py status
```

Answer from `running_account` (`forecast`, `pace_per_hour`), `recommendation`, `accounts[]`
(`five_hour` / `weekly`: `used`, `left_pct`, `resets`) and `weekly_headroom`. When
`monitor_running` is false the figures are the last known ones; run `dashboard` (without
`--open`) to start the monitor.

## Alerts as Cremind events

| Event | Raised when |
|---|---|
| `limit_warning` | the account in use crosses the heads-up threshold of either limit |
| `switch_now` | it crosses the switch-now threshold — switch accounts now |
| `limit_soon` | at the current pace either limit runs out within N minutes |
| `limit_reached` | an account hits a limit; Claude Code stops for it until the reset |
| `account_available` | a used-up account's limit has reset |

Each alert fires once per limit window. **An alert reaches the user only through a
subscription** — with none, Cremind drops the event (it still appears in the dashboard's
Activity list). When the user wants to be told about their Claude usage, subscribe this
conversation with the skill's `subscribe` object: every trigger they asked for (all five by
default), and a short action such as:

> Report this Claude usage alert in one or two lines: the account, the limit (5-hour or
> weekly), how much is used, when it resets or runs out, and which account to switch to.

Every firing costs one agent run, so keep the action brief. `test-alert` fires every
subscription to `limit_warning` (or `--type <event>`). An event carries `account`, `limit`,
`used_pct`, `resets_at`, forecasts (`runs_out_at`, `minutes_left`), `switch_to` and the
`dashboard` URL, plus the alert text and every account's figures.

## Commands

Run `uv run scripts/__main__.py <command>`. Every command prints JSON.

| Command | What it does |
|---|---|
| `status` | Usage of every account, the forecast, and which account to switch to |
| `dashboard [--open] [--no-start]` | The dashboard link; starts the monitor if needed; `--open` opens it on the Cremind computer |
| `accounts` | Claude Code profiles and the account signed in to each |
| `add-account <name> [--no-open]` | Create a profile for another account and open Claude Code in it |
| `open-account <name>` | Open Claude Code in a profile (to sign in again or run `/usage`) |
| `remove-account <name> [--delete-files]` | Stop tracking an extra profile (optionally delete its folder and login) |
| `settings [--warn-pct N] [--crit-pct N] [--weekly-warn-pct N] [--weekly-crit-pct N] [--eta-minutes N] [--alerts on\|off]` | Show or change the alert thresholds |
| `label <account> <name>` | Give an account (email, current name or id) a short name |
| `test-alert [--type <event>]` | Send a test alert event |
| `statusline install\|remove\|status [--force]` | The optional Claude Code status line |

## Tracking more accounts

Claude only reveals an account's usage to a Claude Code session signed in to it, so every
account to track needs a **profile**: a separate Claude Code config folder with its own login,
which never changes the account your main sessions use.

1. `add-account spare1` creates the profile. On Windows it opens a Claude Code window on the
   computer running Cremind; elsewhere it prints the command to run there.
2. In that window: `/login` with the account to track, `/usage`, then `/exit`. The monitor
   picks it up within seconds.
3. To refresh it later, `open-account spare1` and run `/usage` again (it uses none of the
   account's quota). Idle accounts stay accurate on their own.

To move work to another account, the user runs `/login` in Claude Code as usual; the monitor
notices within 2 seconds. Every account the user signs in to that way is tracked from then on.

## Settings

`DASHBOARD_PORT` (default 7337) is in Settings → claude-usage-monitor. Alert thresholds are on
the dashboard (Alert settings) or `settings`: heads-up 80% (5-hour) / 90% (weekly), switch-now
90% / 95% — accounts past switch-now aren't recommended until they reset — and "either limit
is N minutes away" (default 15; 0 turns it off).

## Status line (optional)

`statusline install` makes Claude Code's terminal sessions report exact figures after every
reply and show a one-line summary. It edits Claude Code's `settings.json` (backed up first)
and won't replace another status line without `--force`. VS Code sessions don't need it.

## Troubleshooting

- **No alerts arrive** → is there a subscription (`cremind skill-events list`) and are alerts
  on (`settings`)? `test-alert` checks the whole path.
- **"not running"** → `dashboard` starts it. Its log is on Cremind's Processes page.
- **A figure has ≈** → estimated since the last official reading; `/usage` in that session or
  profile gives an exact one. A sudden drop is Claude resetting a limit early (logged).
- **Docker or a remote Cremind** → the dashboard is reachable only on the computer running
  Cremind; use `status` instead.

## Privacy

From Claude Code's files the monitor reads the signed-in account's non-secret identity
(uuid, email, name, plan), Claude Code's cached usage percentages, and from transcripts only
timestamps, model ids, token counts and limit notices. It never reads `.credentials.json` or
any token, never stores conversation content, makes no network requests, and serves only
`127.0.0.1`. Its data lives in `scripts/.monitor/` (safe to delete).

## Module layout

```
claude-usage-monitor/
├── SKILL.md
├── events/<event_type>/              # alert drop-zones (one folder per event)
├── references/how-it-works.md        # measurement method, calibration, limits
└── scripts/
    ├── __main__.py                   # CLI
    ├── event_listener.py             # the monitor + dashboard (long-running app)
    ├── statusline.py                 # Claude Code status line bridge
    ├── dashboard/                    # the dashboard page
    └── usage_monitor/                # monitor, usage maths, transcripts, events, server
```
