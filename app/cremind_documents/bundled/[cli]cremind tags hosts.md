---
description: "Gateway computers for Cremind Tag: the computers whose USB ports Cremind drives your gateways from. List them and see if each is ready, search one's USB ports for a gateway, prepare the gateway components on the server (admin), and let other profiles use the server's USB ports (admin)."
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
drives. A computer that cannot search says why below the table.

### `cremind tags hosts show <computer>`

One computer in detail: each component, USB access, the gateways it sees on
its ports, and who may use it.

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
