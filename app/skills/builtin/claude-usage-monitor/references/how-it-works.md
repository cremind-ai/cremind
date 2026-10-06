# How the Claude Usage Monitor measures usage

Read this when the user asks how a figure was worked out, why it is marked ≈, or why it
differs from what claude.ai shows.

## The two limits

Claude subscriptions have a **5-hour** limit and a **weekly** limit, and running out of
either interrupts Claude Code. The weekly one can run out while the 5-hour window still has
plenty left: 1% of the weekly limit is about 5× as much usage as 1% of the 5-hour one (on
Max 20x under heavy use, the last 5% of a week lasts around half an hour). That is why both
limits get the same alerts, and why the weekly thresholds default higher (90% / 95% against
80% / 90%).

## Official readings

Claude Code itself reports exact figures in three ways, and each one replaces the estimate:

- its usage cache in each profile's `.claude.json`, refreshed when you sign in or run `/usage`
  (Claude Code's own config backups carry earlier readings, which are imported too);
- the status line after each terminal reply (`statusline install`);
- "limit reached" notices, which Claude Code records in the session transcripts — an official
  100% until the reset.

## Estimates between readings

Between readings — and for VS Code sessions, which never run a status line — the monitor
reads the session transcripts (`<profile>/projects/**/*.jsonl`) for each reply's token counts,
prices them at API list rates, and credits them to the account that was signed in to that
profile at the time. A calibration converts dollars to percent: on 2026-10-06 two Max 20x
accounts measured $590 and $550 of list-price usage per full 5-hour window, so 1% ≈ $5.70
(weekly ≈ $31). Other plans start from their advertised multiple of Pro (Max 5x ¼, Pro 1/20).
Each new official reading refines the calibration of that plan; implausible samples (usage
made somewhere the monitor can't see) are ignored. Estimated figures are marked **≈**.

A 5-hour window opens with the first reply after the previous one ended (Claude aligns it to
10 minutes); a weekly window likewise, aligned to the hour. Once a window's reset time has
passed, its usage is back to 0 — so idle accounts stay accurate without any new reading.

## Burn rate and forecast

The burn rate is the % of each limit used per hour over the last 30 minutes (or since the
account became active, if that was more recent; for terminal-only accounts, from status line
readings). The forecast says which limit runs out first at that pace and when, and when to
switch (that limit's switch-now threshold). Below about 1.2% of the 5-hour limit per hour the
pace counts as idle. If both windows reset before they run out, the forecast says so.

## Which account to switch to

The recommendation is the account that is below both switch-now thresholds with the most
5-hour headroom, avoiding accounts past the weekly heads-up; the least recently used breaks a
tie. When nothing is usable it says when the first account frees up.

## Limits of the method

- Usage made outside this computer (claude.ai, the mobile apps, another machine) only appears
  at the next official reading.
- If Anthropic weighs models differently from list prices, a mixed-model session drifts a
  little until the next official reading corrects it.
- Claude occasionally resets a limit early (the same window's usage drops, or a new window
  starts while the old one still had hours left). The monitor notices it at the next official
  reading, logs it, and re-arms that window's alerts.

## Where the data lives

`scripts/.monitor/` in the skill folder of each Cremind profile: `state.json` (accounts,
readings, settings, alert history), `ledger.json` (per-minute spend and how far each
transcript was read — saved together, so a restart never counts a reply twice),
`profiles.json` (extra profiles), `summary.json` (for the status line), `inbox/` (status line
snapshots) and `runtime.json` (the running monitor's dashboard URL). Deleting the folder
resets the monitor; nothing in Claude Code is affected.

Extra profiles live in this Cremind profile's own `coding-cli/claude-accounts/<name>` folder,
next to its other command-line logins, and are deleted with the Cremind profile.
