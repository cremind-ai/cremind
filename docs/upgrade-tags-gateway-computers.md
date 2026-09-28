# Upgrading: Cremind Tag gateways without Cremind Connect

Applies to the first release after **v0.0.19rc8.dev1** that ships gateway
computers, and to anyone who set up Cremind Tag gateways with the separate
**Cremind Connect** program.

## In one paragraph

Cremind now drives Cremind Tag USB gateways itself. A gateway is plugged into a
**gateway computer**: the computer the Cremind server runs on, or — for a
Cremind in a container, on a NAS or elsewhere — a computer with the Cremind
desktop app set up as a gateway computer for that server (outbound HTTPS only).
Cremind Connect is retired: nothing asks for it any more and no new version of
it is released. A gateway Connect already runs is **not** paired again: when a
gateway computer starts on the computer and OS user Connect ran as, it takes
over every Connect worker of that server as it is — controller key,
credentials, waiting deliveries, pairings — and tells Connect to let go of it.
Once Connect has nothing left to run, it is stopped and its startup entry is
removed.

## What an administrator does

Usually nothing beyond upgrading:

- **Cremind runs natively on the computer the gateway is plugged into.** The
  server's own gateway computer starts with Cremind and moves the workers in.
  If Settings → Tags shows the gateway computer as not ready, prepare its
  components there (or `cremind tags hosts prepare`); the move happens at the
  next start.
- **Cremind runs somewhere without USB access** (Docker, a NAS, another
  machine). On the computer Connect ran on, install the Cremind desktop app
  and use **Settings → Tags → Set up a gateway computer** (or
  `cremind tags host enroll`) for the profile the gateways belong to. The
  workers move in when it starts.

Until then Connect keeps running its workers: the two coexist, and a worker
is only ever driven by one of them (gateway locks, and Connect is told to stop
a worker before Cremind starts it).

## What moves, what stays

- A worker moves when its recorded authority id is this server's and, on a
  desktop gateway computer, when it belongs to the profile the computer is set
  up for. Workers of other servers — and, on a desktop computer, of other
  profiles — are left alone and keep running in Connect; Connect is then not
  retired.
- Each worker moves through a journal
  (`<system dir>/.tag-runtime/migration/<worker id>.json`, or the desktop app's
  runtime folder): validate, stop Connect's copy, copy (the delivery queue's
  database through SQLite's backup API), verify, record the worker on the
  gateway computer, publish, start.
- A failure **before** Cremind records the worker puts Connect back exactly as
  it was (its copy enabled again) and is retried at the next start. A failure
  **after** it is resumed from the journal at the next start.
- Connect's own copy stays in its folder, disabled and marked
  `migrated_to: cremind`; Cremind never deletes it. After checking that the
  gateways work, Cremind Connect can be uninstalled and its folder removed.

## Seeing the result

- Settings → Tags: the gateway computer says how many gateways moved in from
  Cremind Connect, and how many are still to move.
- `cremind tags hosts show <computer>`: the `Moved in:` line.
- On the gateway computer itself: `cremind tags host status` (`migration` in
  `--json`).

## Removed

| What | Before | Now |
|---|---|---|
| Connect installers | `GET /api/tags/connect` (download links per OS) | removed; `server_config.tags_connect_downloads` is ignored |
| Connect builds | released from the cremind-tag repository | retired; cremind-tag releases firmware and the protocol contract only |
| Where a worker runs | Cremind Connect only | `tag_companions.execution_kind`: `legacy_external` (Connect, or a manual runtime), `server`, `desktop`; the migration `20261002_tag_hosts` marks every existing worker `legacy_external` |

The wire protocol and the firmware are unchanged: devices keep their owners,
keys and pairings.
