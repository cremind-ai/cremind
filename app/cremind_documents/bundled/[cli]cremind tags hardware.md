---
description: "Set up **Cremind Tag hardware** as admin: add a companion PC and rotate its credential, scan for and provision bridges, claim an e-paper tag for a profile, move, release or forget it, follow hardware commands, and set the Tags defaults every profile inherits. Admin only; a profile's own tags, notes and routing are `cremind tags`."
---

# `cremind tags hardware` — Cremind Tag hardware (admin)

The hardware behind Cremind Tag is shared by every profile, so only the
**admin** profile manages it; anyone else gets `403` (`cremind -p admin tags
hardware …` runs it as admin). A **companion** is the program on a PC that
drives a **gateway** (USB), which reaches **bridges** over a mesh; each bridge
serves nearby e-paper **tags**. Cremind never talks to the hardware directly:
it queues **commands** that the companion picks up and runs, so most changes
here finish a moment later — follow them with `status`.

A device is named by its id, a unique id prefix (4+ characters), its hardware
id (the HW ID column, as in `1A2B3C4D`) or its name; a companion by its id,
prefix or name. Timestamps in `--json` output are epoch **milliseconds**.

## Finding this in the web UI

> **Settings → Tags → Hardware** (admin profile) — companions, the device
> inventory, hardware commands, who owns each tag, and the defaults profiles
> inherit.

## Global flags

All `cremind tags hardware` subcommands accept the root-level `--json` flag. It goes right after `cremind`, before the command group (`cremind --json tags hardware list`); a trailing `--json` is rejected as an unknown option.
`CREMIND_TOKEN` is required, and it must be the admin profile's.

## Inventory and commands

### `cremind tags hardware list`

```bash
cremind tags hardware list
```

Three tables: companions (`ONLINE` = heard from in the last 2 minutes, active
credentials), devices (`KIND` gateway / bridge / tag, `HW ID`, `OWNER`,
`STATUS`, `PENDING` cards on their way to a tag, battery), and the pending plus
recent commands. A tag with STATUS `clear_failed` could not be blanked after a
change of owner (see Troubleshooting).

### `cremind tags hardware status`

```bash
cremind tags hardware status <command-id>
```

One command: `queued`, `claimed` (the companion is running it), `succeeded`,
`failed`, `expired` or `cancelled`, with its arguments, result or error.

### `cremind tags hardware run`

Queue a hardware operation.

```bash
cremind tags hardware run <kind> [--companion <companion>] [kind's flags]
```

| Kind | Flags | Does |
|---|---|---|
| `scan_unprovisioned` | `--duration` (5–600 s, default 60) | List bridges waiting to be provisioned (their UUIDs are in the result). |
| `provision_bridge` | `--uuid`, optional `--name` | Add a scanned bridge to the mesh. |
| `configure_bridge` | `--hw-id` | Re-send a bridge's configuration. |
| `remove_bridge` | `--hw-id` | Take a bridge out of the mesh. |
| `identify` | `--hw-id` | Make a gateway, bridge or tag identify itself. |
| `refresh_tag` | `--tag-id` | Redraw a tag. |
| `install_fontpack` | `--bridge-hw-id` | Install the font pack on a bridge (needs the operator). |
| `collect_diagnostics` | none | Gather the companion's diagnostics. |

`--companion` defaults to the only registered companion. Dashes work in the
kind (`scan-unprovisioned`). Tag ownership commands (`assign_tag`,
`clear_tag`) come only from `claim`, `assign` and `release`.

## Companions

### `cremind tags hardware register`

```bash
cremind tags hardware register <name>
```

Creates the companion and its **hardware credential**, and prints the
`Authorization` value `CremindTag tagc_….<secret>` **once** — Cremind keeps
only a hash. Give it to the companion with `cremind-tag connect`, from your own
terminal rather than through the assistant (whatever captures the output keeps
the secret). Each profile that wants cards on these tags also creates a
content credential: `cremind tags credentials create`.

### `cremind tags hardware rotate`

```bash
cremind tags hardware rotate <companion>
```

A new hardware credential, shown once; the old one stops working at once, so
the companion is locked out until it has the new value.

### `cremind tags hardware remove`

```bash
cremind tags hardware remove <companion> --yes
```

Revokes the companion's credentials and forgets its devices with their
delivery history. Without `--yes` it only says what would go and exits 2.

## Tag ownership

### `cremind tags hardware claim`

Give a tag to a profile.

```bash
cremind tags hardware claim <tag> --owner <profile> [--bridge <bridge>] [--name <name>]
```

| Flag | Meaning |
|---|---|
| `--owner` | The profile that will own it (required). |
| `--bridge` | The bridge to serve it. Needed when its companion has several and the tag has none yet (`409 bridge_required`). |
| `--name` | Rename the tag at the same time. |

The previous owner's pending cards are cancelled and the screen is blanked
before the new owner's cards appear (`clear_required` until then; what arrives
meanwhile is delivered once the clear succeeds). The tag starts clean: its name
is `--name` or empty, and the previous owner's screen previews are deleted.
Queues `assign_tag` and `clear_tag`. A clear that fails or expires is retried,
three attempts in all.

### `cremind tags hardware assign`

```bash
cremind tags hardware assign <tag> --bridge <bridge>
```

Moves the tag to another bridge on the same companion (`--bridge` is
required); its owner and pending cards stay.

### `cremind tags hardware release`

```bash
cremind tags hardware release <tag>
```

The tag belongs to nobody; its cards are cancelled and its screen is blanked.

### `cremind tags hardware rename` and `forget`

```bash
cremind tags hardware rename <device> <name>
cremind tags hardware forget <device> --yes
```

A name is 1–128 characters; a blank one is refused (`422 invalid_name`).

`forget` deletes the device's record and delivery history (without `--yes` it
only explains and exits 2) and prints the tag's last epoch. A tag a profile
owns is refused (`409 tag_owned`): release it first (`cremind tags hardware
release <tag>`), which blanks its screen and moves its epoch on. A device the
companion still reports comes back as a new, unclaimed one.

## Defaults for every profile

### `cremind tags hardware defaults`

```bash
cremind tags hardware defaults
```

Each setting and card route: the admin default next to the built-in value.

### `cremind tags hardware set-defaults`

Only what you pass changes: just those settings are sent and merged into the
current defaults. A profile's own value (`cremind tags set`) still wins over
these.

```bash
cremind tags hardware set-defaults [--layout <name>] [--excerpts|--no-excerpts]
    [--qr-links|--no-qr-links] [--progress-cadence <seconds>] [--language <tag>]
    [--timezone <tz>] [--route <KIND>=<value>]... [--inherit <setting>]...
```

The flags mean what they mean for `cremind tags set`: `--progress-cadence`
60–3600 seconds, `--timezone own` = each profile's Cremind timezone, `--route`
takes `KIND=all`, `KIND=none`, `KIND=<tag>[,<tag>...]` or `KIND=inherit`, and
`--inherit <setting>` drops the admin default so the built-in applies.

## Worked examples

### Connect a new companion and give it a tag

```bash
cremind tags hardware register "Desk PC"
cremind tags hardware list
cremind tags hardware claim 1A2B3C4D --owner alice --name Desk
```

### Provision a new bridge

```bash
cremind tags hardware run scan_unprovisioned --duration 120
cremind tags hardware status 5d0c9a7e-0000-4000-8000-000000000001
cremind tags hardware run provision_bridge --uuid 8f3e1c0a9b7d4e2f8a6c5b4d3e2f1a0b --name Hall
```

### Vietnamese text on every profile's tags

```bash
cremind tags hardware set-defaults --language vi --timezone Asia/Ho_Chi_Minh
```

### Script: tags nobody owns

```bash
cremind --json tags hardware list | jq '.devices[] | select(.kind == "tag" and .owner_profile == null)'
```

## Troubleshooting

- **`403`** — not the admin profile. Run it as admin.
- **`409 bridge_required`** — pass `--bridge`; the bridges are the `bridge`
  rows of `list` on the tag's companion.
- **`422 bridge_not_found`** — that bridge is not on the tag's companion.
- **`422 unknown_profile`** — no such profile (`cremind profile list`).
- **`422 use_tag_endpoint`** — use `claim`, `assign` or `release`.
- **`409 tag_owned`** on `forget` — a profile owns the tag; `release` it first.
- **STATUS `clear_failed`** — the tag's clear failed three times (asleep, out
  of range, flat battery), so no content reaches it. Check the tag and its
  companion, then retry: `cremind tags hardware claim <tag> --owner <profile>`
  (same owner is fine) or `cremind tags hardware release <tag>`; either queues a
  fresh clear.
- **`422 invalid_name`** — a name must be 1–128 characters.
- **`422 unknown_command` / `invalid_args`** — see the kinds table under `run`.
- **A command stays `queued`** — the companion is offline or not connected;
  check `ONLINE` in `list`. Unclaimed commands expire (tag ownership after 7
  days, a scan after 15 minutes, others after an hour).

## Related

- `cremind tags` — a profile's own tags: notes, routing, history, content
  credentials.
- `app/api/tags_hardware.py` — the endpoints these commands wrap.
