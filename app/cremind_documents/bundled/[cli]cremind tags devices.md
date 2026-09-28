---
description: "Set up your own Cremind Tag hardware: connect a USB gateway plugged into a gateway computer, add an e-paper tag (it connects through the gateway itself or a bridge) or a bridge from the setup code on its label, send a test card, pause, move a tag to the gateway or another bridge, remove, or move everything to another computer. Settings → Tags does the same; this is the CLI."
---

# `cremind tags devices` — set up your own gateways, bridges and tags

Cremind drives your gateway itself, over USB, from a **gateway computer**: the
computer the Cremind server runs on, or one of your computers running the
Cremind desktop app (see `cremind tags hosts`). Plug the gateway in there,
connect it, then add tags and bridges from the QR or setup code printed on
their labels. Everything you set up here belongs to your profile only; no other
profile (and no admin page) sees it. The web page does all of this too — the
CLI is optional.

A newer gateway (nRF52840) reaches the tags near it on its own radio, so a
gateway alone is enough for them; **bridges only carry updates farther**. An
older gateway cannot reach tags itself: every tag it serves needs a bridge.
`cremind tags devices list` shows which one yours is.

These commands never act on the USB ports of the computer the CLI runs on
unless it is the gateway computer you name (or the only one there is): the
search always runs on the gateway computer, and its output says which one.

Name a device by its id, its name, its short id (`1A2B3C4D`) or the start of
its device id (8+ characters); a connection (one gateway with its bridges and
tags) by its id or name; a gateway computer by its id or name.

## Finding this in the web UI

> **Settings → Tags** — the **Your hardware** section: *Gateway computers*,
> *Connect gateway*, *Add bridge*, *Add tag*, and each device's *Send test*,
> *Rename*, *Pause*, *Remove* and *Move to another computer*.

## Global flags

All subcommands accept the root-level `--json` flag right after `cremind`:

```bash
cremind --json tags devices list
```

## Setup codes are secrets

A bridge's or tag's setup code (25 characters, e.g. `4D6KR-ART00-0G40R-40M30-E2097`,
or the QR text `CTAG:…`) proves you hold the device. It is never taken as a
command-line argument: pass it with `--code-file <file>` (`--code-file -`
reads one line from stdin) or type it at a hidden prompt with `--code-prompt`.
It is never printed back or logged. `0`/`O` and `1`/`I`/`L` are the same
character; a typo is caught before anything is sent (`setup_code_invalid`).

## Subcommands

### `cremind tags devices list`

Your connections (gateways) with their bridges and tags: kind, name, short id,
state (`pairing`, `ready`, `offline`, `recovery_pending`, `removal_pending`,
`reconciling`), and in DETAIL: a bridge's capacity (`3/20 tags`); for a gateway
`3/20 tags directly` when it reaches tags itself, or `tags need a bridge`; for a
tag its pending cards and the gateway or bridge it connects through
(`via Hall`).

### `cremind tags devices connect`

Connect a USB gateway plugged into a gateway computer. Cremind searches that
computer's USB ports, shows the gateway it found (`Connect gateway …3C4D on
Office PC?`), checks it again, sets up its worker there and claims it. Each
step is printed; the connection keeps going on the server if you stop waiting.
Once connected it prints the next step: `add tag` when the gateway reaches tags
itself (a bridge is then only needed to reach farther), else `add bridge`.

```bash
cremind tags devices connect --host "Office PC" --name "Hall gateway"
```

| Flag | Meaning |
|---|---|
| `--host` | the gateway computer it is plugged into (id or name; optional when you may use only one) |
| `--candidate` | a gateway `cremind tags hosts scan` found — needed when the search finds several |
| `--name` | a name for the gateway |
| `--yes` | connect the gateway found without asking |
| `--timeout` | seconds to wait for the search and for the connection |

When the search finds nothing it says why, with the same words as the web
page: **No gateway detected**, **USB access denied**, **Gateway busy in another
application**, **Unsupported firmware**, **Required components unavailable**,
**Computer offline**, **Gateway owned elsewhere** or **Recovery required** (a
gateway of yours driven from another computer: use `recover`).

### `cremind tags devices add bridge|tag`

Add a bridge (it joins a gateway) or a tag from its label. A tag is heard by
your gateway itself, when it reaches tags, and by every ready bridge; it
connects through the one that heard it (the strongest signal is recommended),
and the output says which (`Tag ready. It connects through gateway 'Office
gateway'.`). Power the device first. A tag checks in about every 30 seconds, so
"waiting for the tag to wake" is normal.

| Flag | Meaning |
|---|---|
| `--code-file` | the file holding the setup code (`-` = stdin) |
| `--code-prompt` | type the code at a hidden prompt instead |
| `--gateway` | bridges: which connection to join (needed when you have several) |
| `--candidate` | tags: which search result to pair with when several devices (your gateway, bridges) hear it — the output lists each one by name, e.g. `c1  gateway 'Office gateway'  rssi -55` |
| `--name` | a name for the new device |
| `--timeout` | seconds to wait |

### `cremind tags devices status <id>`

One gateway connection or computer search, device search, pairing, recovery or
setup session, as JSON.

### `cremind tags devices cancel <id>`

Cancel an unfinished gateway connection (before the gateway is claimed),
search, pairing or setup session. A half-configured bridge is removed again; a
tag is checked and left as it really is.

### `cremind tags devices test <tag>`

Show a test card on a tag.

### `cremind tags devices pause <device>` / `cremind tags devices resume <device>`

Stop (and restart) new updates to one tag, or — for a gateway — its whole
connection. The pairing is kept.

### `cremind tags devices move <tag>`

Serve a tag through another place of the same gateway: the gateway itself (when
it reaches tags on its own radio) or another ready bridge — e.g. after its
bridge was removed, or to move it from the gateway to a bridge and back.

```bash
cremind tags devices move Kitchen --to Hall
cremind tags devices move Kitchen --to gateway
```

| Flag | Meaning |
|---|---|
| `--bridge` / `--to` | where it connects from now on: `gateway` (the tag's own gateway), or a ready bridge of that gateway by id, name or short id |

### `cremind tags devices remove <device>`

Remove a tag, a bridge or a gateway. Cremind stops sending to it at once; the
screen is cleared and the device's keys are removed when it is next reachable
(**Removal pending** until then). A removed tag shows a fresh QR code for its
next owner. Removing a bridge lists the tags it served; move them to your
gateway (when it reaches tags itself) or another bridge. Removing a gateway
removes its bridges and tags too, the ones it served itself included.

| Flag | Meaning |
|---|---|
| `--force` | forget it now even though the device cannot be cleaned up (lost or broken) |
| `--yes` | do not ask for confirmation |

### `cremind tags devices recover <connection>`

Move a connection to another gateway computer — its old computer is gone or
replaced, or it was set up with the older Cremind Connect. Plug the gateway
into the new computer and run this: Cremind finds it there, the old computer
loses access at once, and bridges and tags move over as they wake (**Recovery
pending** until each one does).

| Flag | Meaning |
|---|---|
| `--host` | the gateway computer it is plugged into now |
| `--yes` | move it without asking |
| `--timeout` | seconds to wait for the search and for the move |

## Troubleshooting

| Code | Meaning |
|---|---|
| `simple_setup_disabled` | hardware setup is not enabled on this server yet |
| `host_access_denied` | the admin has not allowed your profile on the server's USB ports (`cremind -p admin tags hosts access <computer> <profile> --allow`) |
| `host_offline` / `host_not_running` | the gateway computer is off, or Cremind is not running there |
| `components_unavailable` | the gateway computer lacks the gateway components (`cremind -p admin tags hosts prepare <computer>`) |
| `candidate_expired` / `candidate_not_found` | the search result is too old or unknown: search again |
| `owned_elsewhere` | the gateway belongs to another Cremind or profile; remove it there, or reset it |
| `recovery_required` | the gateway is yours but driven from another computer: `cremind tags devices recover` |
| `already_claiming` | the gateway is being claimed and can no longer be cancelled; remove it afterwards |
| `no_gateway` | connect a gateway first |
| `no_ready_bridge` | nothing can take a tag yet: your gateway cannot reach tags itself (an older gateway) and no bridge is ready — add a bridge first; or the gateway is paused — resume it |
| `candidate_not_eligible` | that gateway or bridge cannot take the tag now (it stopped reaching tags, its bridge is not ready, or it belongs to another gateway): choose the tag's gateway or a ready bridge of it |
| `gateway_required` | you have several gateways: pass `--gateway` |
| `setup_code_invalid` / `setup_code_wrong_role` | a typo, or a bridge label used for a tag (or the reverse) |
| `already_paired` | that device is already yours |
| `device_owned` | the device is set up elsewhere; reset it to set it up again |
| `session_expired` | a setup session lasts a few minutes; start again |
| `bridge_full` | that gateway or bridge serves all the tags it can: pick another with `--candidate`, or move a tag off it (`move <tag> --to <gateway or bridge>`) |
| `authority_unavailable` | this server lost the keys its devices trust — restore them from an encrypted backup made with `cremind backup create --include-tag-keys` |
