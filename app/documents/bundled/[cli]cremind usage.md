---
description: "Report aggregate **token usage and estimated cost** for a profile over a date window: grand totals, cache-hit rate, and breakdowns by model, provider, and source (reasoning agent vs. each sub-agent or tool), plus the top conversations. Use this to see how many tokens or how much money a profile has spent — profile-wide totals, unlike the per-conversation `cremind conv usage` breakdown."
---

# `cremind usage` — Token Usage & Cost

`cremind usage` prints the aggregate behind the **Usage & Cost** dashboard:
how many tokens you've spent, the estimated dollar cost, the prompt-cache hit
rate, and breakdowns by model, provider, and source (the reasoning agent vs.
each sub-agent/tool), plus your highest-spend conversations.

Cost is estimated from per-model pricing at the time each request was recorded.
Historical rows whose price couldn't be resolved are flagged by a
`has_unpriced` field — when it's `true`, the cost figures are a lower bound.

Results are scoped to the **caller's own profile**. The `admin` profile may
pass `--profile <name>` to inspect another profile, or omit it to span all
profiles.

## Finding this in the web UI

> **Sidebar → Usage & Cost**

The dashboard shows the same totals, the daily series chart, the
by-model/provider/source breakdowns, and the top-conversations list. The
per-conversation drill-down on that page corresponds to
`cremind conv usage <id>`.

## Syntax

```bash
cremind usage [--start <ms>] [--end <ms>] [--tz-offset <minutes>] [--profile <name>]
```

**Options.**

| Flag          | Default          | Meaning                                                                       |
|---------------|------------------|-------------------------------------------------------------------------------|
| `--start`     | all time         | Window start as epoch **milliseconds**.                                       |
| `--end`       | now              | Window end as epoch **milliseconds**.                                         |
| `--tz-offset` | `0` (UTC)        | Minutes east of UTC, used only to bucket the daily series into local days.    |
| `--profile`   | the caller       | Inspect another profile. **Honored only for the `admin` profile**; ignored otherwise. Omit as admin to span all profiles. |

`CREMIND_TOKEN` is required.

## Output

**Default (table) view.** A key/value headline (request count, conversation
count, cache-hit rate, cache read/write cost, `has_unpriced`, plus the totals)
followed by `By model`, `By provider`, `By source`, and `Top conversations`
tables. Empty sections are omitted.

**`--json`.** The full summary object verbatim: `totals`, `cache_hit_rate`,
`cache_read_usd`, `cache_write_usd`, `request_count`, `conversation_count`,
`series` (daily, ISO-dated), `by_model`, `by_provider`, `by_source`,
`top_conversations`, and `has_unpriced`. Use this for scripting.

## Worked examples

### This profile, all time

```bash
$ cremind usage
request_count        128
conversation_count   14
cache_hit_rate       0.62
has_unpriced         false
...

By model
MODEL              REQUESTS  TOTAL_TOKENS  COST_USD
claude-opus-4-8    96        1843201       4.12
...
```

### A bounded window, scripted with `jq`

```bash
# Spend for a single UTC day, total cost only
$ start=$(date -u -d 2026-06-29 +%s)000
$ end=$(date -u -d 2026-06-30 +%s)000
$ cremind usage --start "$start" --end "$end" --json | jq '.totals'
```

### Admin: inspect another profile, or span all

```bash
$ cremind usage --profile li          # one profile (admin only)
$ cremind usage                       # as admin, omitting --profile spans all profiles
```

## Troubleshooting

**`--profile` seems ignored** — It is honored only for the `admin` profile.
A non-admin caller is always pinned to its own profile regardless of the flag.

**Cost looks low / `has_unpriced` is true** — Some historical rows predate the
pricing data or use a model with no price entry, so their cost couldn't be
estimated. Treat the totals as a lower bound when `has_unpriced` is `true`.

**Nothing but zeros** — No usage has been recorded for the window/profile.
Widen the window (drop `--start`/`--end`) or confirm the profile with
`cremind me`.

## Related

- `cremind conv usage <id>` — the per-conversation, per-request breakdown
  (the drill-down behind a single conversation on the dashboard).
- `app/api/usage.py` — the `/api/usage/summary` endpoint this command wraps.
