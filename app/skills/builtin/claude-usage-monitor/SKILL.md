---
name: claude-usage-monitor
description: "Track Claude subscription usage across every Claude account used with Claude Code on this computer — each account's 5-hour and weekly limits used and left, the burn rate, when the running account hits a limit, which account to switch to — and switch Claude Code (VS Code, terminals) to another account in one step with the login saved here: no /login, no browser. When the user asks to switch their Claude account, run `switch <account>`. Automatic switching (`settings --switching auto`) moves it to the best account by itself at 98% of the 5-hour or 99% of the weekly limit; `plan` says what it would do and why. The figures are Anthropic's own, as claude.ai shows them. Raises Cremind events when an account nears or hits a limit, is available again, or was switched automatically. Serves a live dashboard with one-click switching: when the user asks to open it, run `dashboard --open` (it opens in the browser of the computer running Cremind and returns the link). Never reads conversation content."
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
      - name: auto_switched
        description: Automatic switching moved Claude Code to another account (the one in use reached its 5-hour or weekly threshold, or a week about to reset was used up early)
      - name: no_account_available
        description: Automatic switching found no account to take over from the one in use, which reached its threshold; says when the first account frees up
      - name: auto_switch_failed
        description: Automatic switching tried three times and could not switch Claude Code; says why and how to fix it
  long_running_app:
    command: uv run scripts/event_listener.py
    description: The usage monitor. Asks Anthropic for every account's official usage (every 2 minutes while an account is in use, every 5-10 minutes otherwise), follows Claude Code's files and session transcripts every 2 seconds, raises the alert events, switches Claude Code by itself when automatic switching is on, and serves the dashboard on 127.0.0.1.
---

# claude-usage-monitor

**Purpose:** keep a long Claude Code job from stalling on a usage limit. For every Claude
account used on this computer it shows how much of the 5-hour and weekly limits is gone and
what is left, how fast the account in use burns through both, **when the first of them runs
out**, and **which account to switch to** — and **switches in one step**, with the login saved
here, or **by itself** in automatic mode. Alerts arrive as Cremind events; a live dashboard
shows everything at a glance.

## How it works

A background **monitor** (this skill's listener) on the computer running Cremind asks
Anthropic's usage API for every account's **official figures** — the same numbers claude.ai
shows under Settings → Usage, including usage on claude.ai, the apps and other computers —
with each profile's Claude login, as Claude Code's `/usage` does: every 2 minutes while an
account is in use, every 5-10 minutes otherwise (less often after Anthropic asks it to slow
down), and at once when the dashboard opens or `status` runs; in between, the figures move with
what every reply costs. When an idle profile's login has expired it renews it the way Claude Code does. Only
when no recent official figure is at hand (no usable login, no network, live figures turned
off) does it estimate from the token counts in session transcripts; those figures are marked
**≈**. It tracks **your Claude Code** (`~/.claude`, the login Cremind's Claude Code tool falls
back to), **this Cremind profile's own Claude Code login** if any, and **extra profiles**, one
per additional account. Method and limits: `cat references/how-it-works.md`.

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

It asks Anthropic for fresh figures first (a few seconds). Answer from `running_account`
(`forecast`, `pace_per_hour`), `recommendation`, `accounts[]` (`five_hour` / `weekly`: `used`,
`left_pct`, `resets`; `figures`; `figures_as_of`; `your_claude_code_uses_it`; `switch_with`) and
`weekly_headroom`. Report the numbers as given: `figures` starting with "official" means
Anthropic's own figures, identical to claude.ai's; an `estimated` one comes with a
`live_figures_problem` saying why (e.g. the profile must sign in again — `open-account <name>`,
then `/login`). When `monitor_running` is false no alerts are raised; run `dashboard` (without
`--open`) to start the monitor.

## Switch accounts

"Switch to my work account", "use another Claude account", "change the account Claude Code uses":

```bash
uv run scripts/__main__.py switch <account>
```

- `<account>`: the email, a short name, the profile name, or a part of the email only it has
  (`admin`). Each account `status` lists has its `switch_with` command when it can switch.
- It moves the account's saved login into the user's Claude Code (`~/.claude`, which VS Code and
  terminals use) and keeps the replaced login in a profile of its own — **no sign-in**. Report
  `your_claude_code_now_uses` and `previous_login_kept_in`: VS Code and terminal sessions use the
  new account from their next message, and Cremind's Claude Code tool too unless it has a login
  of its own. Switching back is the same command.
- **Never tell the user to `/login` to switch**, and don't use `open-account` for it: `/login`
  drops the account it replaces from this computer, which is why it costs a new sign-in every
  time; `open-account` only opens a separate profile and changes nothing.
- On `error`, act on its `reason`: `not_here` — no login of that account is saved here: run
  `add-account <name>` (a window opens: `/login` with that account, `/exit`), then `switch`;
  `in_use` — the account runs in its own Claude Code window: ask the user to close it, or add
  `--force` if they say so; `signin` — its saved login was refused: `open-account <profile>`,
  `/login`, `/exit`, then switch; `busy` — retry in a few seconds; `auto_blocked` — automatic
  switching is on and would move away from that account at once (its limit is nearly full):
  say so, and offer `settings --switching manual` if they still want it. Otherwise report it
  with its `fix`.

## Automatic switching

"Switch my Claude account automatically", "turn on auto switch", "stop switching by itself":

```bash
uv run scripts/__main__.py settings --switching auto     # or: --switching manual
```

- **Automatic**: the monitor switches your Claude Code itself, within seconds, when the account
  in use reaches **98%** of its 5-hour limit or **99%** of its weekly one
  (`--auto-session-pct N`, `--auto-weekly-pct N`), or hits a limit. An account whose week is
  full is **set aside until its week resets** (sooner if Anthropic resets it early). Running
  sessions carry on from their next message. It needs the monitor running (Cremind running).
- **Manual** (the default): alerts only; the user, or you when they ask, switch.
- **Which account it picks**: the one with the most room to work — its 5-hour room, capped by
  what its week still allows, in coarse steps (plenty / some / little); among similar ones, the
  one whose unused week would be lost soonest at its reset (weekly left ÷ days to its reset).
  So weeks get used before they expire, and no account's week runs out early while the others
  sit full. Only accounts whose login is saved here and that are in rotation
  (`rotation <account> off` leaves one out).
- `--early on` also switches before the thresholds, to an account whose leftover week would
  otherwise go unused within 24 hours. Off by default.
- "Which account is next?", "why that one?", "what will it do?" → `plan`: `do` (switch / wait /
  stay), `what`, `switching.next_in_line`, and every account in `ranking` with `why`.
- When automatic switching goes on, offer to subscribe the conversation to `auto_switched`,
  `no_account_available` and `auto_switch_failed` if they want to hear about it (Telegram …):
  in automatic mode the heads-up alerts for the account in use are not sent.

## Alerts as Cremind events

| Event | Raised when |
|---|---|
| `limit_warning` | the account in use crosses the heads-up threshold of either limit |
| `switch_now` | it crosses the switch-now threshold — switch accounts now |
| `limit_soon` | at the current pace either limit runs out within N minutes |
| `limit_reached` | an account hits a limit; Claude Code stops for it until the reset |
| `account_available` | a used-up account's limit has reset |
| `auto_switched` | automatic switching moved Claude Code to another account (`from`, `to`, `reason`, `next_in_line`) |
| `no_account_available` | automatic switching found no account to take over (`first_free`, `first_free_at`) |
| `auto_switch_failed` | automatic switching tried three times and could not switch (`error`, `fix`) |

Each alert fires once per limit window. With automatic switching on, the account your Claude
Code uses raises only `limit_reached` of the first five (it is switched before the others
matter), and `account_available` isn't sent. **An alert reaches the user only through a
subscription** — with none, Cremind drops the event (it still appears in the dashboard's
Activity list). When the user wants to be told about their Claude usage, subscribe this
conversation with the skill's `subscribe` object: every trigger they asked for (the first five
by default; the three `auto_…` ones with automatic switching), and a short action such as:

> Report this Claude usage alert in one or two lines: the account, the limit (5-hour or
> weekly), how much is used, when it resets or runs out, and which account to switch to.

Every firing costs one agent run, so keep the action brief. `test-alert` fires every
subscription to `limit_warning` (or `--type <event>`). An event carries `account`, `limit`,
`used_pct`, `resets_at`, forecasts (`runs_out_at`, `minutes_left`), `switch_to` (plus
`switch_command` when its login is saved here, and `switch_why`) and the `dashboard` URL, plus
the alert text and every account's figures.

**Manual mode, switching for the user:** when the user asks you to decide and switch yourself
when an alert comes, make the subscription's action say so, e.g. "Run `plan`. If `do` is
`switch`, run its `switch_command`, then report in one or two lines what was switched and why
(`what`, `switching.next_in_line`); otherwise report the alert and why no switch (`what`)."
Switch only when the user asked for it. Mention that automatic mode does the same within
seconds and without an agent run per alert, if they'd rather have that.

## Commands

Run `uv run scripts/__main__.py <command>`. Every command prints JSON.

| Command | What it does |
|---|---|
| `status` | Usage of every account, the forecast, and which account to switch to |
| `switch <account> [--force]` | Make another account the one your Claude Code uses, with its saved login — no sign-in |
| `plan` | What should happen to the account your Claude Code uses now (`do`: switch / wait / stay, with `switch_command`), every other account ranked with `why` |
| `rotation <account> on\|off` | Whether an account may be suggested and switched to (on by default) |
| `dashboard [--open] [--no-start]` | The dashboard link; starts the monitor if needed; `--open` opens it on the Cremind computer |
| `accounts` | Claude Code profiles and the account signed in to each |
| `add-account <name> [--no-open]` | Create a profile for another account and open Claude Code in it to sign in once |
| `open-account <name>` | Open Claude Code in a profile (to sign in again); it does not switch accounts |
| `remove-account <name> [--delete-files]` | Stop tracking an extra profile (optionally delete its folder and login) |
| `settings [--switching auto\|manual] [--auto-session-pct N] [--auto-weekly-pct N] [--early on\|off] [--warn-pct N] [--crit-pct N] [--weekly-warn-pct N] [--weekly-crit-pct N] [--eta-minutes N] [--alerts on\|off] [--live on\|off]` | Show or change switching and alert settings; `--live` turns the official figures from Anthropic on or off |
| `label <account> <name>` | Give an account (email, current name or id) a short name |
| `test-alert [--type <event>]` | Send a test alert event |
| `statusline install\|remove\|status [--force]` | The optional Claude Code status line |

## Tracking more accounts

Anthropic reveals an account's usage only to that account's login, so every other account
needs a **profile**: a separate Claude Code config folder with its own login. Adding one never
changes the account your Claude Code uses; `switch` does, in one step.

1. `add-account spare1` creates the profile. On Windows it opens a Claude Code window on the
   computer running Cremind; elsewhere it prints the command to run there.
2. In that window: `/login` with the account to add, then `/exit`. The monitor fetches its
   official figures within seconds, and keeps them current from then on — the profile's login
   is renewed by the monitor while it is idle, so it never needs opening again.
3. From then on `switch <its email>` (or the dashboard's switcher) makes it your Claude Code's
   account at once. The account it replaces keeps its login in a profile of its own (created
   on the first switch, named after its email), so it can be switched back just as fast.
4. Only if Anthropic stops accepting a login (a password change, a revoked session) does the
   account show `live_figures_problem` asking to sign in again: `open-account <profile>`,
   `/login`, `/exit`.

A profile emptied by a switch keeps that account's place (`accounts` shows `keeps_place_of`):
the login returns there when your Claude Code switches away from it.

## Settings

`DASHBOARD_PORT` (default 7337) is in Settings → claude-usage-monitor. Switching is Manual or
Automatic (the switch on the dashboard's "Your Claude Code uses" card, or `settings --switching`);
automatic switching's thresholds (98% 5-hour, 99% weekly), its early switch and which accounts are
in rotation are on the dashboard (Settings: switching and alerts) or `settings` / `rotation`.
Alert thresholds are there too: heads-up 80% (5-hour) / 90% (weekly), switch-now
90% / 95% — accounts past switch-now aren't recommended until they reset — and "either limit
is N minutes away" (default 15; 0 turns it off). Official figures from Anthropic are on by
default; `settings --live off` turns them off (every figure is then an estimate between Claude
Code's own readings).

## Status line (optional)

`statusline install` makes Claude Code's terminal sessions report exact figures after every
reply and show a one-line summary. It edits Claude Code's `settings.json` (backed up first)
and won't replace another status line without `--force`. VS Code sessions don't need it.

## Troubleshooting

- **No alerts arrive** → is there a subscription (`cremind skill-events list`) and are alerts
  on (`settings`)? `test-alert` checks the whole path.
- **"not running"** → `dashboard` starts it. Its log is on Cremind's Processes page.
- **Automatic switching didn't switch** → `plan` says why: `do: wait` (no account can take over;
  `switching.now` names the first one free again), another Cremind profile runs automatic
  switching for this computer (`switching.note`), the monitor isn't running, or each candidate
  was refused (the Activity list and `auto_switch_failed` give the reason and fix).
- **Signed in to another account but VS Code still uses the old one** → a `/login` in a
  profile's window (`add-account`, `open-account`) adds that account to the computer without
  switching anything; `switch <its email>` makes it your Claude Code's account.
- **A figure has ≈** → no recent official figure for that account; its
  `live_figures_problem` (dashboard: "No live figures") says why — sign in again
  (`open-account`, `/login`), Anthropic asking to slow down (it waits as asked), no network,
  or no readable login (a Mac whose login is in the Keychain and has expired: it resumes when
  Claude Code next runs there). A sudden drop is Claude resetting a limit early (logged).
- **Docker or a remote Cremind** → the dashboard is reachable only on the computer running
  Cremind; use `status` instead.

## Privacy

To get the official figures, the monitor reads each tracked profile's Claude login
(`.credentials.json`, or the macOS Keychain) and sends it **only to Anthropic**: to its usage
API, and to Claude's sign-in service to renew a login that expired — the same requests, lock
and save Claude Code uses, so running Claude Code sessions keep working. The login is never
stored elsewhere, logged, or shown on the dashboard or in events. A `switch` moves logins
between Claude Code's own folders on this computer, under Claude Code's locks: never copied,
never signed out, and checked with Anthropic's usage API before it moves. From Claude Code's other
files the monitor reads the signed-in account's identity (uuid, email, name, plan) and from
transcripts only timestamps, model ids, token counts and limit notices — never conversation
content. The dashboard is served only on `127.0.0.1`. `settings --live off` stops all network
requests. Its data lives in `scripts/.monitor/` (safe to delete).

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
    └── usage_monitor/                # monitor, live figures, switching (planner: which account, when), usage maths, transcripts, events, server
```
