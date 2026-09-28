---
description: "Set up your own Cremind Tag hardware with Cremind Connect: connect a USB gateway, add a bridge or an e-paper tag from the setup code on its label, send a test card, pause, move, remove, or recover everything on a replacement computer. No terminal needed — Settings → Tags does the same; this is the CLI."
---

# `cremind tags devices` — set up your own gateways, bridges and tags

With **Cremind Connect** installed on the computer your gateway is plugged into,
a profile sets up its own Cremind Tag hardware: plug the gateway in, approve it
in the small Cremind Connect window, then add bridges and tags from the QR or
setup code printed on their labels. Everything you set up here belongs to your
profile only; no other profile (and no admin page) sees it. The web page does
all of this too — the CLI is optional.

Name a device by its id, its name, its short id (`1A2B3C4D`) or the start of
its device id (8+ characters); a connection (one gateway with its bridges and
tags) by its id or name.

## Finding this in the web UI

> **Settings → Tags** — the **Hardware** section: *Connect gateway*, *Add
> bridge*, *Add tag*, and each device's *Send test*, *Rename*, *Pause*,
> *Remove* and *Recover on this computer*.

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
`reconciling`), and bridge capacity or pending cards.

### `cremind tags devices connect`

Connect a USB gateway plugged into **this** computer. Cremind opens a
`cremind-connect://` link; the Cremind Connect window shows the server, your
profile, the computer and four words, and asks you to pick and approve the
gateway. The command then shows the same four words — confirm they match
(`--yes` skips the question when you already checked). No Cremind Connect
window? Install it first (`cremind tags devices installers`).

| Flag | Meaning |
|---|---|
| `--server` | the address Cremind Connect should use to reach this server (default: the CLI's server) |
| `--open` / `--no-open` | open the link on this computer (default) or print it to open elsewhere |
| `--yes` | do not ask whether the words match |
| `--timeout` | seconds to wait (a setup expires after 5 minutes) |

### `cremind tags devices add bridge|tag`

Add a bridge (it joins a gateway) or a tag (a ready bridge hears it) from its
label. Power the device first. A tag checks in about every 30 seconds, so
"waiting for the tag to wake" is normal.

| Flag | Meaning |
|---|---|
| `--code-file` | the file holding the setup code (`-` = stdin) |
| `--code-prompt` | type the code at a hidden prompt instead |
| `--gateway` | bridges: which connection to join (needed when you have several) |
| `--candidate` | tags: which search result to pair with when several bridges hear it |
| `--name` | a name for the new device |
| `--timeout` | seconds to wait |

### `cremind tags devices status <id>`

One setup session, device search, pairing or recovery, as JSON.

### `cremind tags devices cancel <id>`

Cancel an unfinished setup session or pairing (a half-configured bridge is
removed again; a tag is checked and left as it really is).

### `cremind tags devices test <tag>`

Show a test card on a tag.

### `cremind tags devices pause <device>` / `cremind tags devices resume <device>`

Stop (and restart) new updates to one tag, or — for a gateway — its whole
connection. The pairing is kept.

### `cremind tags devices move <tag>`

Serve a tag through another ready bridge of the same gateway, e.g. after its
bridge was removed.

| Flag | Meaning |
|---|---|
| `--bridge` | the bridge to use |

### `cremind tags devices remove <device>`

Remove a tag, a bridge or a gateway. Cremind stops sending to it at once; the
screen is cleared and the device's keys are removed when it is next reachable
(**Removal pending** until then). A removed tag shows a fresh QR code for its
next owner. Removing a bridge lists the tags it served; move them. Removing a
gateway removes its bridges and tags too.

| Flag | Meaning |
|---|---|
| `--force` | forget it now even though the device cannot be cleaned up (lost or broken) |
| `--yes` | do not ask for confirmation |

### `cremind tags devices recover <connection>`

Move a connection to this (replacement) computer: install Cremind Connect,
plug in the existing gateway, run this and approve. The old computer loses
access at once; bridges and tags move over as they wake (**Recovery pending**
until each one does). Same flags as `connect`: `--server`, `--open`/`--no-open`,
`--yes`, `--timeout`.

### `cremind tags devices installers`

Where to download Cremind Connect for Windows, macOS (Apple silicon, Intel) and
Linux (`.deb`, `.tar.gz`).

## Troubleshooting

| Code | Meaning |
|---|---|
| `simple_setup_disabled` | hardware setup is not enabled on this server yet |
| `no_gateway` | connect a gateway first |
| `no_ready_bridge` | add a bridge first — tags connect through a bridge |
| `gateway_required` | you have several gateways: pass `--gateway` |
| `setup_code_invalid` / `setup_code_wrong_role` | a typo, or a bridge label used for a tag (or the reverse) |
| `already_paired` | that device is already yours |
| `device_owned` | the device is set up elsewhere; reset it to set it up again |
| `not_approved` / `session_expired` | approve in the Cremind Connect window; a setup lasts 5 minutes |
| `bridge_full` | the bridge serves all the tags it can |
| `authority_unavailable` | this server lost the keys its devices trust — restore them from an encrypted backup made with `cremind backup create --include-tag-keys` |
