---
description: "Make this computer a gateway computer for a Cremind server running elsewhere (a container, a NAS, another machine) so it drives Cremind Tag USB gateways plugged in here: enroll it from the Cremind page's setup link, run it, check its status and components, or forget it. The Cremind desktop app does this by itself."
---

# `cremind tags host` — this computer as a gateway computer

A Cremind server in a container or on another machine cannot see the USB ports
of the computer your gateway plugs into. Set that computer up as a **gateway
computer** and it searches for, connects and drives your profile's gateways
for the server, reaching it over HTTPS only — no port is opened on this
computer. The **Cremind desktop app does all of this by itself** (Settings →
Tags → Gateway computers → *Set up a gateway computer* opens it); these
commands serve computers without the app, and show what the app does.

The computer uses its own credential, which works only for this computer and
the profile that set it up — no Cremind sign-in is needed here, and it never
sees anything of other profiles. Its keys and gateway workers live in
`~/.cremind/.tag-runtime/`, which file tools and backups never touch.

## Finding this in the web UI

> **Settings → Tags** — **Your hardware** → **Gateway computers** → *Set up a
> gateway computer* (shows the link and the four words), and each computer's
> *Remove*.

## Global flags

All subcommands accept the root-level `--json` flag right after `cremind`:

```bash
cremind --json tags host status
```

## Subcommands

### `cremind tags host enroll`

Complete the link the Cremind page shows under *Set up a gateway computer*
(`cremind://tags/setup?…`). The link holds a one-time secret, so it is read
from stdin or a file — never from the command line. This computer shows the
server, the profile and **four words**; approve only if the Cremind page
shows the same words, then confirm them on the page.

```bash
cremind tags host enroll --link-file link.txt
```

| Flag | Meaning |
|---|---|
| `--link-file` | a file holding the link (`-` reads one line from stdin; without it an interactive terminal asks) |
| `--yes` | approve without asking (you compared the words) |
| `--events` | JSON lines on stdin/stdout for the Cremind app: the link first, then `approve` or `decline` after the `bound` event; events `bound`, `approved`, `confirmed`, `enrolled`, `failed` |

Enrolling the same computer again for the same profile keeps its place in
Cremind and replaces its credential.

### `cremind tags host run`

Drive the gateways plugged into this computer until stopped (Ctrl-C): report
to Cremind, search its USB ports when asked, connect gateways, and run each
gateway's worker. Exit status `3` means Cremind no longer accepts this
computer (it was removed): enroll it again. `4` means it was never set up.

### `cremind tags host status`

The enrollment (server, profile, host id), whether the host runs, and the
gateway components (`platform`, `packages`, `fonts`). On a computer that ran
the older Cremind Connect, a `Moved in` line tells how many of its gateways
this computer took over (`migration` in `--json`): the host does that by
itself when it starts, keeping their keys and pairings; one that could not
move yet is retried at the next start.

### `cremind tags host prepare`

Install the gateway components here: checks the gateway packages (install
them with `pip install "cremind[tags]"` if they are missing) and installs the
verified font bundle tag screens are drawn with.

### `cremind tags host forget`

Stop being a gateway computer: Cremind forgets this computer (its credential
stops working) and this computer forgets its enrollment. The gateways it drove
stay your profile's, offline until you move them to another computer
(`cremind tags devices recover`).

| Flag | Meaning |
|---|---|
| `--yes` | do not ask for confirmation |
| `--offline` | forget it here even when Cremind cannot be reached (then remove it on the Cremind page too) |

## Troubleshooting

| Code | Meaning |
|---|---|
| `wrong_scheme` / `unknown_action` / `bad_token` | not a Cremind setup link, or a damaged one: copy it again from the Cremind page |
| `pin_mismatch` / `tls` | the server's certificate does not match the link, or is not trusted here |
| `declined` | you declined on this computer; start again from the Cremind page |
| `expired` / `cancelled` | the setup took longer than five minutes, or was cancelled on the page |
| `credential_revoked` | Cremind no longer accepts this computer (removed or set up again elsewhere): enroll it again |
| `components_unavailable` | run `cremind tags host prepare` |
