---
description: "Gateway computers for Cremind Tag: the computers whose USB ports Cremind drives your gateways from. List them and see if each is ready (or has a font update available), search one's USB ports for a gateway, prepare the gateway components on the server — again after an update brings new fonts (admin) — and let other profiles use the server's USB ports (admin)."
---

# `cremind tags hosts` — the computers Cremind drives gateways from

A **gateway computer** is a computer whose USB ports Cremind drives your
gateways from, so no separate app is needed:

- the computer the **Cremind server** runs on — the admin profile may use it,
  and other profiles once the admin allows them;
- a computer running the **Cremind desktop app** set up for your profile —
  only your profile uses it.

Each computer reports for itself whether it is ready: its gateway components,
its USB access (a Cremind in a container sees no USB ports unless the gateway's
device is mapped into it), and the gateways it sees. A profile allowed on a
computer only ever claims unclaimed gateways; what it connects stays its own.

Name a computer by its id or its name. Connect a gateway with
`cremind tags devices connect` (it searches first); `scan` here only looks.

## Finding this in the web UI

> **Settings → Tags** — **Your hardware** → **Gateway computers**: each
> computer's status, *Connect a gateway here*, *Prepare components* and (admin)
> *Other profiles on this computer's USB ports*.

## Global flags

All subcommands accept the root-level `--json` flag right after `cremind`:

```bash
cremind --json tags hosts list
```

## Subcommands

### `cremind tags hosts list`

The computers this profile may use: id, name, kind (`server`, `desktop`),
state (`running`, `offline`, `unavailable` — components missing,
`busy_elsewhere`, `stalled`, `failed`), components (`ready`, `partial` —
gateways work, tag screens wait for fonts), USB access, whether you may use it
(`manage` for the admin on the server), and how many of your gateways it
drives. Below the table, a computer that cannot search says why, and one with
a **font update available** says how to install it.

### `cremind tags hosts show <computer>`

One computer in detail: each component, USB access, the gateways it sees on
its ports, and who may use it. A component is `ready`, `missing`,
`unsupported` or `broken`; the fonts can also be `outdated` — an update of
Cremind pinned a newer font pack than the one the computer draws with. Tag
screens keep working with the older pack, so the computer stays `ready` (in
`--json`, `readiness.fonts_update` is `true`) until its components are
prepared again: `cremind -p admin tags hosts prepare` for the server's own
computer, `cremind tags host prepare` on a desktop gateway computer.

### `cremind tags hosts scan [computer]`

Search a computer's USB ports and list the gateways found, as opaque
**candidates** (valid for 10 minutes, bound to your profile and that computer)
with what each means: `usable`, `already_connected`, `recovery_required`,
`owned_elsewhere`, `unsupported_firmware`, `busy`, `not_a_gateway`,
`device_rejected`. Nothing is claimed. Connect one with
`cremind tags devices connect --host <computer> --candidate <candidate>`.

| Flag | Meaning |
|---|---|
| `--timeout` | seconds to wait for the search |

A search that finds nothing says why: **No gateway detected** (plug it in with
a data cable), **USB access denied** (on Linux, add the account Cremind runs as
to the `dialout` group; in a container, map the device), **Gateway busy in
another application** (close the serial monitor, firmware tool or older
Cremind Connect that holds it) or **Unsupported firmware**.

### `cremind tags hosts prepare [computer]`

Admin: install the gateway components on the Cremind server's computer — the
Python packages through the feature installer, and the verified font bundle
tag screens need — then start gateway support. Progress is printed line by
line; it keeps going on the server if you stop waiting. Run it again to retry
after a failure.

Run it again after an update that brings new fonts (`hosts list` says *a font
update is available*): it installs the font pack this Cremind pins, restarts
gateway support on it, then removes the older packs nothing uses any more —
never one a bridge behind this computer's gateways still shows. A pack that
cannot be removed yet (Windows keeps a file in use) stays until the next
preparation; that never fails it. Tags behind a bridge draw with the new
fonts once the bridge has the same pack (`cremind tags tools bridge
fonts-install`, over the bridge's USB port).

```bash
cremind -p admin tags hosts prepare
```

| Flag | Meaning |
|---|---|
| `--timeout` | seconds to wait (downloads can take a few minutes) |

### `cremind tags hosts access <computer> <profile>`

Admin: let another profile connect gateways plugged into the Cremind server's
computer (`--allow`), or stop it (`--deny`). Stopping it keeps what that
profile already connected; it only stops new connections.

```bash
cremind -p admin tags hosts access "Office PC" bob --allow
```

| Flag | Meaning |
|---|---|
| `--allow` / `--deny` | allow the profile, or stop it |

## Moving from Cremind Connect

A gateway set up with the older Cremind Connect moves into Cremind by itself:
when a gateway computer starts (the Cremind server's own, or the Cremind app
set up as one), it takes over every Cremind Connect worker of this server on
that computer — its keys, credentials and waiting updates, nothing paired
again — and tells Cremind Connect to stop that worker. A desktop gateway
computer serves one profile, so only that profile's workers move to it.
Workers of other servers (and there, of other profiles) stay with Cremind
Connect, which keeps running them; once none is left, Cremind Connect's
startup entry is removed. A worker that cannot move (a damaged key, Cremind
refusing it) stays with Cremind Connect, which keeps running it.
`cremind tags hosts show` tells how many moved.

## Gateways and containers

A Cremind in a container sees no USB ports unless the gateway's device is
mapped into it (`cremind tags hosts list` shows `USB container`):

- **Docker Desktop on Windows or macOS** cannot pass USB devices to a
  container. Set up the computer the gateway plugs into as a gateway computer
  instead: Settings → Tags → *Set up a gateway computer* (the Cremind app does
  the rest), or `cremind tags host enroll` on it.
- **Docker on Linux** can: add the gateway's device (its stable name is under
  `/dev/serial/by-id/`) to the container with `devices:` and the device's group
  with `group_add:` in a `docker-compose.override.yml`, then recreate the
  container. The installer's README shows the exact lines.
- **Kubernetes:** use a gateway computer; a pod seldom runs where the gateway
  plugs in.

## Troubleshooting

| Code | Meaning |
|---|---|
| `host_not_found` | no such computer for your profile (another profile's desktop computer is not listed) |
| `host_access_denied` | the admin has not allowed your profile on the server's USB ports |
| `host_offline` / `host_not_running` | the computer is off, or Cremind is not running there |
| `host_busy` | another Cremind on that computer drives its gateways |
| `components_unavailable` | the gateway components are missing: `cremind -p admin tags hosts prepare` |
| `admin_required` | only the admin prepares the server or decides who may use it |
| `host_not_shareable` | a desktop computer stays the profile's that set it up |
| `profile_not_found` | no such profile (`cremind profile list`) |
