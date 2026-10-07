# How the Claude Usage Monitor measures usage

Read this when the user asks how a figure was worked out, why it is marked ≈, why it differs
from what claude.ai shows, what the monitor does with their Claude logins, or how switching
accounts works.

## The two limits

Claude subscriptions have a **5-hour** limit and a **weekly** limit, and running out of
either interrupts Claude Code. The weekly one can run out while the 5-hour window still has
plenty left: 1% of the weekly limit is about 5× as much usage as 1% of the 5-hour one (on
Max 20x under heavy use, the last 5% of a week lasts around half an hour). That is why both
limits get the same alerts, and why the weekly thresholds default higher (90% / 95% against
80% / 90%).

## Official figures from Anthropic

Claude Code's `/usage` and claude.ai's Settings → Usage both show what Anthropic's usage API
(`GET https://api.anthropic.com/api/oauth/usage`) answers for the signed-in account: the
5-hour session, the weekly limit, any per-model weekly limit (e.g. "Weekly · Fable") and
extra usage. The monitor asks the same API with each tracked profile's Claude login:

- every 2 minutes for an account in use;
- every 10 minutes for the other accounts (every 5 minutes while the dashboard is open);
- at once when the dashboard opens, its Refresh button is pressed, or `status` runs (no more
  than once per 45 seconds per account);
- after Anthropic answers "slow down", it waits as asked (5 minutes when it doesn't say), then
  asks that account half as often, and a quarter as often after a second one, stepping back
  every 30 minutes without one. Asking every 30-60 seconds drew 20 such answers in one morning
  (2026-10-07); between readings the figures move with what every reply costs anyway.

An account counts as in use when its sessions on this computer spent in the last 6 minutes,
or when Anthropic's figure rose since the previous answer — which also catches usage on
claude.ai, the apps or another computer. A figure fetched in the last 3 minutes is shown
exactly as Anthropic gave it, so it matches claude.ai to the percent. When Anthropic answers
429 (or 403), the monitor waits as long as it asks (5 minutes when it doesn't say), as Claude
Code does; network errors are retried after 30 s, 1, 2, 4 … up to 15 minutes.

### Logins

The login is the profile's `.credentials.json` (on a Mac, the Keychain item Claude Code keeps
it in). Its access token lasts 8 hours; Claude Code renews it while it runs. When an idle
profile's token has expired, the monitor renews it exactly the way Claude Code does, so that
running sessions keep working:

1. it takes Claude Code's refresh lock — the directories `<profile>/.oauth_refresh.lock` and
   `<profile>.lock` — touching it every 2 seconds while held, and waits while Claude Code
   holds it (a lock untouched for a minute is abandoned and taken over, as Claude Code does);
2. it re-reads the file: if someone renewed it meanwhile, it uses that token;
3. it sends Claude Code's refresh request (`https://platform.claude.com/v1/oauth/token`, Claude
   Code's client id, the login's scopes);
4. it saves with a compare-and-swap, under Claude Code's credential-store lock
   (`<profile>/.storage-write.lock`): only if the file still holds the refresh token it used,
   so a login saved meanwhile (another `/login`) wins. Claude Code notices the new file and
   adopts the token.

If Anthropic refuses the refresh token (`invalid_grant`: a revoked session, a password
change), the account shows "sign in again" and the monitor doesn't retry until the profile's
login changes. Keychain logins (Mac) are used while valid and not renewed by the monitor.

## Switching accounts

Claude Code keeps one login per config folder. `/login` replaces the folder's login and drops
the old one from the computer (it isn't revoked, just gone), so going back to an account costs
another sign-in. A switch moves logins between folders instead:

- the chosen account's login moves from its extra profile into your Claude Code (`~/.claude`:
  VS Code and terminals, and Cremind's Claude Code tool when it has no login of its own);
- the login it replaces moves to the extra profile that keeps that account's place — one an
  earlier switch emptied for it, or a new one named after its email.

It writes exactly what `/login` writes: the account's entries in the credential store
(`claudeAiOauth`, `organizationUuid` and the device token, in `.credentials.json` or the macOS
Keychain — anything else there, such as MCP servers' logins, stays with its folder) and
`oauthAccount` in `.claude.json`, clearing the per-account caches `/login` clears. It holds
Claude Code's refresh, credential-store and config locks of every folder involved, re-reads
everything under them, and writes in an order that keeps each login on disk at every step;
if a write fails, what was written is put back. Before it moves, the login is checked with
Anthropic's usage API (renewed first if it expired): a login Anthropic refuses is never moved
in. Logins are never copied — Anthropic replaces the refresh token at every renewal, so of
two copies only one would keep working.

Running Claude Code sessions check `.credentials.json` before every request and re-read it
when it changed, and watch `.claude.json` every second, so they continue with the new account
from their next message — the way they follow a `/login` made in another window. A profile
whose transcripts changed in the last 6 minutes has a Claude Code window open: switching its
account away is refused unless forced, since that window would continue with the other login.

## Claude Code's own readings

Claude Code reports exact figures too, and each one counts as an official reading:

- its usage cache in each profile's `.claude.json`, refreshed when you sign in or run `/usage`
  (Claude Code's own config backups carry earlier readings, which are imported too);
- the status line after each terminal reply (`statusline install`);
- "limit reached" notices, which Claude Code records in the session transcripts — an official
  100% until the reset.

## Estimates

Only when no recent official figure is at hand — live figures turned off, a profile without a
usable login, Anthropic unreachable or asking to wait — the monitor estimates: it reads the
session transcripts (`<profile>/projects/**/*.jsonl`) for each reply's token counts, prices
them at API list rates, and credits them to the account that was signed in to that profile at
the time. A calibration converts dollars to percent: on 2026-10-06 two Max 20x accounts
measured $590 and $550 of list-price usage per full 5-hour window, so 1% ≈ $5.70 (weekly ≈ $31).
Other plans start from their advertised multiple of Pro (Max 5x ¼, Pro 1/20). Official
readings refine the calibration of that plan — each sample spans a rise of at least 8 points
(5-hour) or 4 (weekly), since readings a minute apart are too close to measure anything;
implausible samples (usage made somewhere the monitor can't see) are ignored. List prices are
only a proxy for how Anthropic counts usage, so an estimate can be well off (on 2026-10-07 one
read 10% where Anthropic said 25%). Estimated figures are marked **≈**.

A 5-hour window opens with the first reply after the previous one ended (Claude aligns it to
10 minutes); a weekly window likewise, aligned to the hour. Once a window's reset time has
passed, its usage is back to 0 — so idle accounts stay accurate without any new reading.

## Burn rate and forecast

The burn rate of the 5-hour limit is the slope of Anthropic's own figures over the last 20
minutes; the weekly one follows from it (the same usage, through each limit's calibration).
Without live figures it is the % of each limit used per hour over the last 30 minutes of
estimated spend (or since the account became active, if that was more recent; for
terminal-only accounts, from status line readings). The forecast says which limit runs out first at that pace and when, and when to
switch (that limit's switch-now threshold). Below about 1.2% of the 5-hour limit per hour the
pace counts as idle. If both windows reset before they run out, the forecast says so.

## Which account to switch to

The week is the scarce part: on a Max 20x plan one full 5-hour window costs about a quarter of
a week (one account's 5-hour went 0 → 98% while its week went 71 → 97%; the calibration says
$6.21 / $25.46 per 1%), so an account's week holds about four full windows. The 5-hour window
is the throttle. Every view of "which account next" — the alerts, the dashboard, `status`,
`plan` and automatic switching — ranks the other accounts the same way (`planner.py`):

1. **Can it take over?** It has figures, it is in rotation, its week isn't set aside, it is below
   automatic switching's thresholds (98% / 99%), and it has room for at least 10% of a 5-hour
   window. Accounts whose login is saved here come before ones that would need a sign-in.
2. **Room**: the room left in its 5-hour window — a window that resets before it would fill at
   the current pace counts as empty — capped by what its week still allows (weekly room in
   5-hour terms, through each plan's calibration), compared in classes: plenty (60+ points),
   some (30-60), little.
3. **A week it would use up**: when carrying the work it has room for would leave its week
   used up (under 10 points of a window) and the week would stay out for longer than a 5-hour
   window lasts, the account counts as "some room" at best, and within its class comes after
   accounts whose week lasts. An account out of its week can't take over when another
   account's 5-hour window fills, and several accounts with weekly room are what keep work
   going — the last account's 5-hour window must not become the only one left. A week that
   comes back within 5 hours of running out is used like any other; when every account would
   use its week up, the one whose week comes back soonest goes first.
4. **The week that would be lost soonest**: weekly room left ÷ days until that account's week
   resets, in steps of 3 points a day. A week about to reset is used before it expires, and
   weeks that reset together drain together.
5. Then more 5-hour room, a window already running before a fresh one, and the least recently
   used.

Step 3 came from a switch on 2026-10-07 15:51: the account in use filled its 5-hour window; one
other account had 17% of its week left until 06:00 the next morning, another a whole week. Without
step 3 the first was picked — its leftover week would have been lost overnight — and it would
have been out of its week from about 18:30 until 06:00. Now the empty account takes over and the
nearly-full week stays as cover, used later in the evening if the work goes on.

A simulation of three Max 20x accounts over four weeks (workdays of 3-16 hours at 15-70% of a
window per hour, the accounts' real reset days and readings of 2026-10-07) compared rules:
using up one account's week before the next blocked about 6× longer than these rules when the
weeks reset together; "most 5-hour room first" (the earlier rule) left about half again more of
each week unused when they reset on different days; ranking 5-hour room finely before the week
did worse than these classes. Step 3, from that day's figures at 15-33% per hour: at 7 hours a
day no block instead of 0.9 h, and 521 instead of 924 account-hours with a week used up; at 8
hours, 15.6 instead of 21.6 h blocked. From random starting figures both orders block within
minutes of each other. Its cost: past what three accounts allow (10+ hours a day), a week kept
as cover sometimes resets partly unused — 6.5 h more blocked over four weeks at 10 hours a day.
Above about 7 hours of heavy work a day (5-6 at the heaviest paces) three such accounts run out
whatever the order.

When nothing can take over, it says when the first account frees up.

## Automatic switching

With switching set to Automatic, the monitor checks the account your Claude Code uses every
2 seconds (the figures roll forward with each reply's cost between readings) and switches when:

- its 5-hour figure reaches the threshold (98% by default), or a reply hits the limit;
- its weekly figure reaches the threshold (99%): the account is also **set aside** until its
  week resets, and comes back sooner only if Anthropic's figures show the week was reset early;
- optionally ("use up a week that resets soon", off by default), early: another account's week
  resets within 24 hours with room for at least an hour of work at the current pace, and 25%
  more weekly room per day left than the account in use. In the simulation this used about a
  third less of each week unused, for about one extra switch every few days.

It switches to the best account as ranked above, the same way the dashboard's switch does, in a
thread of its own. The switch re-reads that account's figures from Anthropic before anything
moves; if they now say it is near a limit, the next account is tried (up to three). At most one
switch every 2 minutes, unless a limit is hit or a week is full; an early switch waits 30
minutes after any switch. When every try fails it retries after a minute, and after three
failed rounds sends `auto_switch_failed` and retries every 10 minutes. When no account can take
over it sends `no_account_available` once per window, and switches as soon as one frees up.

Running sessions follow on their next request, mid-turn included — verified with Claude Code
2.1.291 against a local stand-in for Anthropic's API: the request after a switch, even a tool
result inside the same turn, carried the new login, and Claude Code's own config saves did not
undo the switch.

Only one monitor switches a computer's Claude Code: Cremind profiles are independent, but they
share `~/.claude`. The monitor that turns automatic switching on claims it in
`~/.claude/.claude-usage-monitor-auto.json` (renewed every 30 seconds, stale after 2 minutes,
removed when switched off or stopped); another one shows who has it and doesn't switch. In
automatic mode a manual switch to an account it would leave at once is refused
(`auto_blocked`), and the heads-up alerts for the account in use are not sent.

## Limits of the method

- Anthropic gives whole percents, so the figures move in steps of 1% — as on claude.ai.
- An account in use is at most a minute behind (30 seconds while the dashboard is open); the
  dashboard's Refresh button and `status` ask at once.
- Only accounts signed in to a tracked profile can be asked about. Accounts seen only through
  the status line, or a profile whose login can't be read or used, fall back to estimates,
  and usage made elsewhere only appears at their next official reading.
- Claude occasionally resets a limit early (the same window's usage drops, or a new window
  starts while the old one still had hours left). The monitor notices it at the next official
  reading, logs it, and re-arms that window's alerts.

## Where the data lives

`scripts/.monitor/` in the skill folder of each Cremind profile: `state.json` (accounts,
readings, settings, alert history), `ledger.json` (per-minute spend and how far each
transcript was read — saved together, so a restart never counts a reply twice),
`profiles.json` (extra profiles, and the account whose place each keeps after a switch),
`summary.json` (for the status line), `inbox/` (status line
snapshots) and `runtime.json` (the running monitor's dashboard URL). Deleting the folder
resets the monitor; nothing in Claude Code is affected.

Extra profiles live in this Cremind profile's own `coding-cli/claude-accounts/<name>` folder,
next to its other command-line logins, and are deleted with the Cremind profile.
