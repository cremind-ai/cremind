---
description: "Cremind Tag hardware tools on this computer, for developers and factory stations: flash gateway/bridge/tag firmware, enroll a tag over SWD, install a font pack on a bridge by USB, provision a mesh by hand, build fonts, render previews, run the simulator, run a manual runtime daemon, collect diagnostics. Setting up your own hardware is `cremind tags devices` instead."
---

# `cremind tags tools` — Cremind Tag hardware tools

The host-side utilities of Cremind Tag: they talk to devices plugged into the
computer the command runs on (a gateway or a bridge by USB, a tag through a
J-Link probe) or to the local simulator. They do not go through the Cremind
server. People setting up their own gateway, bridges and tags never need them:
**Settings → Tags** or `cremind tags devices` does that.

Everything after `tools` is handed to the tools unchanged; `--help` works at
every level:

```bash
cremind tags tools --help
cremind tags tools firmware --help
```

Most tools need the Cremind Tag components (serial, ICU, HarfBuzz, FreeType):
`cremind features install tags`. Without them, `cremind tags tools` says so and
exits 1.

## Finding this in the web UI

> None: these run on the computer the hardware is attached to. The web UI's
> **Settings → Tags** covers setting hardware up.

## Global flags

The tools print tables; many take their own `--json`. The root-level
`--json` flag belongs to the rest of the CLI:

```bash
cremind --json tags devices list
```

## Subcommands

### `cremind tags tools firmware`

`list`, `verify` and `flash` released firmware (gateways and bridges over
USB DFU or J-Link), and read a device's identity. A tag's firmware is written
by `tag enroll`, never by `flash`.

### `cremind tags tools tag`

Enroll a tag over SWD (firmware, enrollment blob, read-back), list enrolled
tags, assign one to a bridge by hand.

### `cremind tags tools bridge`

A bridge's USB maintenance port: `info`, `fonts-install <pack>` (the bridge's
font pack must match the runtime's), flash test, reboot.

### `cremind tags tools gateway`, `mesh`

Talk to a gateway on a serial port (info, counters, reboot) and provision or
configure bridges by hand (manual, protocol v1 setups).

### `cremind tags tools fonts`

Pin (`lock`), `fetch` (≈ 56 MB, verified), `build`, `size`, `coverage`,
`image` and `list` the Noto-based font packs, from `fonts/manifest.yaml` of a
Cremind checkout.

### `cremind tags tools preview`

Render cards, screens or text to PNG exactly as a bridge draws them.

### `cremind tags tools sim`

Run a simulated gateway, bridges and tags (development).

### `cremind tags tools connect`, `daemon`, `queue`

A manual (legacy) runtime: store a server address and connector credentials
created under **Settings → Tags → Hardware** (admin), run the delivery daemon
against a gateway port, and inspect its local queue.

### `cremind tags tools doctor`, `diag`

Check this computer (server reachability, credentials, gateway, J-Link, font
pack) and collect a diagnostics archive.

## Troubleshooting

- **"cannot start … Install their components first"** — run
  `cremind features install tags` (on macOS 15 or newer).
- **A port is busy** — Cremind's own hardware runtime may hold the gateway;
  pause the gateway under **Settings → Tags** first.
