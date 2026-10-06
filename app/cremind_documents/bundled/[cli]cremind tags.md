---
description: "Pin a note on, clear, preview and list your **Cremind Tag e-paper displays** (desk tags): battery, what each shows, delivery history, which cards (questions for you, finished tasks, notifications) reach which tag, and a companion's content credential. Your own tags only; the admin's hardware setup is `cremind tags hardware`."
---

# `cremind tags` — Cremind Tag displays

Cremind Tag puts small battery-powered e-paper tags on your desk. A
**companion** program on a PC drives them over a USB gateway — directly, when
the gateway reaches the tags near it itself, or through bridges, which carry
updates farther. While
Tags is on for a profile, Cremind sends **cards** to the tags that profile
owns (a question waiting for your answer, a finished or failed task, a
notification, progress) and the companion draws them. `cremind tags` manages
the calling profile's own tags. The admin decides which profile owns which
tag, with `cremind tags hardware`.

Name a tag by its id, a unique id prefix (4+ characters), its hardware id (as
in `1A2B3C4D`) or its name. Another profile's tags are never visible: their ids
answer `404 device_not_found`. Timestamps in `--json` output are epoch
**milliseconds**.

## Finding this in the web UI

> **Sidebar → Tags** — your tags, their screens and delivery history (`list`,
> `show`, `display`, `clear`, `refresh`, `identify`, `preview`, `deliveries`).
>
> **Settings → Tags** — the on/off switch, card routing and content
> credentials (`settings`, `set`, `credentials`, `companions`).

## Global flags

All `cremind tags` subcommands accept the root-level `--json` flag. It goes right after `cremind`, before the command group (`cremind --json tags list`); a trailing `--json` is rejected as an unknown option.
`CREMIND_TOKEN` is required for every subcommand.

## Your tags

### `cremind tags list`

```bash
cremind tags list
```

Prints whether Tags is `enabled`, the counts (`active deliveries`, `needs
input`, `failed (24 h)`), then `ID / NAME / STATUS / PENDING / BATTERY / LAST
CONTACT / SCREEN / COMPANION`. PENDING is how many cards are still on their way
to that tag. SCREEN `rev 17 (18 pending)` means the tag shows screen revision
17 and 18 is on its way; `clearing` means it is being blanked after a change of
owner. STATUS `clear_failed` means that blanking failed three times, and
`assign_failed` that the bridge (or gateway) serving the tag could not take it
(see Troubleshooting).

### `cremind tags show`

```bash
cremind tags show <tag>
```

Every field of one tag (battery, signal, pending cards, panel size, firmware,
bridge, preview revisions) and its 20 latest deliveries.

### `cremind tags rename`

```bash
cremind tags rename <tag> <name>
```

The name is 1–128 characters; a blank one is refused (`422 invalid_name`).

### `cremind tags display`

Pin a note on a tag.

```bash
cremind tags display <tag> <title> [--body <text> | --body-file <path>] [--icon <name>] [--ttl <duration>] [--replace]
```

| Flag | Default | Meaning |
|---|---|---|
| `--body` | none | Body text, at most 400 characters. |
| `--body-file`, `-f` | none | Read the body from a file; `-` reads stdin. Prefer it on PowerShell. |
| `--icon` | `push_pin` | An icon name; `cremind tags settings` lists them. |
| `--ttl` | 1 day | How long the note stays: seconds, or `30m`, `2h`, `7d` (1 minute to 7 days). |
| `--replace` | off | Replace the previous note that was also sent with `--replace`, instead of adding a card. |

Each note is its own card, so several can show at once. A note sent with
`--replace` takes the tag's one replaceable slot: use it for a status line you
keep updating ("In a meeting" → "Back at 3").

The title is at most 120 characters. Text that looks like a one-time code (4–8
digits near a word such as "code", "OTP", "PIN", "passcode" or "verification")
is refused with `422 otp_refused` and nothing is sent: codes are never shown on
a tag. The title is one line; the body keeps its line breaks (a run of blank
lines becomes one). Tokens and passwords in the text are masked.

### `cremind tags clear`, `refresh` and `identify`

```bash
cremind tags clear <tag>      # blank the screen; its pending cards are cancelled
cremind tags refresh <tag>    # redraw the screen
cremind tags identify <tag>   # make the physical tag identify itself
```

`refresh` and `identify` queue a hardware command; the tag acts at its next
wake-up. After a Cremind upgrade that changes how screens look, each tag
redraws once by itself (one refresh) — no `refresh` needed.

### `cremind tags preview`

Save the tag's screen as a PNG.

```bash
cremind tags preview <tag> [--kind displayed|desired] [--out <file.png>]
```

| Flag | Default | Meaning |
|---|---|---|
| `--kind` | `displayed` | `displayed` = on the tag now; `desired` = the screen being sent. |
| `--out`, `-o` | `tag-<id>-<kind>.png` | The file to write. |

### `cremind tags companions`

```bash
cremind tags companions
```

`ID / NAME / ONLINE / LAST SEEN / VERSION` of every companion — which one to
name in `credentials create`.

## Settings and card routing

### `cremind tags settings`

```bash
cremind tags settings
```

Shows `enabled`, the timezone tags use, each setting with its SOURCE
(`profile`, `admin default` or `built-in`), the route of every card kind, and
the icon names.

### `cremind tags set`

Only what you pass changes: the command sends just those settings and the
server merges them into this profile's own, so a change made elsewhere at the
same time is kept. A setting cannot be both given and inherited in one command.

```bash
cremind tags set [--enable|--disable] [--layout <name>] [--excerpts|--no-excerpts]
                 [--qr-links|--no-qr-links] [--progress-cadence <seconds>] [--language <tag>]
                 [--timezone <tz>] [--route <KIND>=<value>]... [--inherit <setting>]...
```

| Flag | Meaning |
|---|---|
| `--enable` / `--disable` | Turn Tags on or off for this profile. Off sends no cards. |
| `--layout` | Screen layout (`status`). |
| `--excerpts` / `--no-excerpts` | Show a short excerpt of the assistant's answer. |
| `--qr-links` / `--no-qr-links` | Draw a QR code linking to the source. |
| `--progress-cadence` | Seconds between progress screens, 60–3600 (built-in 300). |
| `--language` | Language of tag text, e.g. `en`, `vi`. |
| `--timezone` | A timezone name or UTC offset; `own` = the profile's Cremind timezone. |
| `--route` | `KIND=all` (every tag you own), `KIND=none`, `KIND=<tag>[,<tag>...]`, or `KIND=inherit` (drop your route for that kind). Repeatable. |
| `--inherit` | Drop your own value for `layout`, `excerpts`, `qr-links`, `progress-cadence`, `language`, `timezone` or `routes`: the admin default applies, else the built-in. Repeatable. |

Card kinds: `notification`, `task_outcome`, `needs_input`, `excerpt`,
`progress`, `health`, `indexing_problem`, `calendar`, `automation`, `usage`,
`tag_diagnostics`. Built in, every kind goes to all your tags except `usage`.

## Delivery history

### `cremind tags deliveries list`

```bash
cremind tags deliveries list [--device <tag>] [--state <state>] [--limit <n>] [--before <id>]
```

| Flag | Default | Meaning |
|---|---|---|
| `--device` | every tag | Only this tag. |
| `--state` | all | `active` (not on the screen yet), `terminal` (finished), or one stage. |
| `--limit` | 50 | At most 200. |
| `--before` | newest | Older pages: the command prints the next `--before` to use. |

Stages run `queued`, `companion_accepted`, `gateway_received`,
`bridge_received`, `transferring`, `refreshing`, `displayed`; a delivery can
instead end `superseded`, `expired`, `cancelled`, `failed` or `uncertain`.

### `cremind tags deliveries show`

```bash
cremind tags deliveries show <id>
```

The card's title and body, stage, outcome, detail, and when each stage was
reached.

### `cremind tags deliveries cancel`

```bash
cremind tags deliveries cancel <id>
```

Works on any card that is not finished yet — also one the companion has
already fetched: the cancel is sent on to the companion as a `resolved` job
(the command prints its id), so the tag drops the card if it has it. Only a
finished card (displayed, expired, cancelled …) answers `409
already_terminal`. When the tag has changed hands meanwhile, nothing is sent.

## Content credentials

A companion fetches a profile's cards with that profile's **content
credential**. The companion itself is registered by the admin (`cremind tags
hardware register`).

### `cremind tags credentials list`

```bash
cremind tags credentials list
```

`ID / COMPANION / LABEL / CREATED / LAST USED / STATE`. Secrets are never shown
here.

### `cremind tags credentials create`

```bash
cremind tags credentials create [--companion <id or name>] [--label <text>]
```

| Flag | Default | Meaning |
|---|---|---|
| `--companion` | the only companion | The companion that may fetch this profile's cards. |
| `--label` | `Content credential` | A note to recognise it by. |

Prints the `Authorization` value, `CremindTag tagc_….<secret>`, **once**:
Cremind keeps only a hash. Give it to the companion with `cremind-tag connect`.
Run this in your own terminal rather than through the assistant, since
whatever captures the output keeps the secret.

### `cremind tags credentials revoke`

```bash
cremind tags credentials revoke <credential-id>
```

Immediate: the companion's next request with it fails.

## Worked examples

### Pin a note for two hours

```bash
cremind tags display Desk "Back at 3 pm" --body "Ring the bell" --ttl 2h
```

### A long body from a file

```bash
cremind tags display Desk "Standup notes" --body-file notes.txt
```

### Only questions go to the kitchen tag

```bash
cremind tags set --enable --route needs_input=Kitchen --route notification=none
```

### Script: what failed

```bash
cremind --json tags deliveries list --state failed | jq '.deliveries[] | {id, detail}'
```

## Troubleshooting

- **`404 device_not_found`** — not one of your tags. `cremind tags list` shows
  yours; only the admin can give you another.
- **`422 otp_refused`** — the text looks like a one-time code. Remove it.
- **`409 clear_pending`** — the tag is still being blanked after a change of
  owner. Try again in a minute.
- **STATUS `clear_failed`** — blanking the tag after a change of owner failed
  three times (it was asleep or out of range), so nothing is shown on it. Ask
  the admin to claim it for you again (`cremind tags hardware claim`) or to
  release it; either starts a fresh clear.
- **STATUS `assign_failed`** — the bridge (or gateway) serving the tag could
  not take it (usually `bridge_full`: every slot of its table is taken), so
  nothing reaches it. Ask the admin to assign it to another bridge (`cremind
  tags hardware assign`); a tag you set up yourself moves with `cremind tags
  devices move <tag> --to <gateway or bridge>`.
- **`422 invalid_title`, `invalid_body`, `invalid_icon`, `invalid_ttl`** — see
  the limits under `display`.
- **`422 invalid_settings`** — one line per rejected field follows (an
  unknown card kind shows as `routes.<kind>`).
- **`422 invalid_name`** — a name must be 1–128 characters.
- **`404 no_preview`** — the companion has not uploaded that screen yet.
- **Nothing reaches a tag** — check `cremind tags settings` (enabled, the
  kind's route), that the companion is online (`cremind tags companions`), and
  that it holds an active content credential of this profile.

## Related

- `cremind tags hardware` — the admin side: companions, tag ownership, the
  defaults every profile inherits.
- `app/api/tags.py` — the endpoints these commands wrap.
