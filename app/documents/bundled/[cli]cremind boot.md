---
description: "Make Cremind start automatically at login or boot on a native install, and restart itself if it stops — for when the server is not running after a reboot, stopped after logout, or stays down after a restart or upgrade. `cremind boot enable` registers an OS service (a systemd user unit on Linux, a launchd LaunchAgent on macOS, a logon Scheduled Task on Windows) that runs `cremind serve` and supervises it; `cremind boot disable` removes it; `cremind boot status` reports whether it is registered, running, and surviving logout (loginctl enable-linger). This is also what makes the in-app Restart Server button and the CREMIND_SSL=after-setup switch to HTTPS work on a native install, because both stop the server and rely on a supervisor to bring it back. Runs entirely locally — no server call, no token, no admin rights. Distinct from `cremind proc autostart`, which relaunches the processes an agent started, and from Docker or Kubernetes installs, where the container runtime already restarts Cremind."
---

# `cremind boot` — Start Cremind at login and keep it running

A native install has no supervisor. The installer starts `cremind serve` once,
for the install session, and nothing brings it back — so after a logout or a
reboot the server is simply gone. The same gap has a second, less obvious
symptom: Cremind's own restart is a *stop*. The backend drains its connections,
stops channel sidecars and the processes agents started, releases their lock
files and exits cleanly — but it cannot re-exec itself while serving a
response. So on an unsupervised install:

- the Developer page's **Restart Server** button stops the backend and it stays
  down;
- an in-app **upgrade** applies and then leaves nothing running;
- `CREMIND_SSL=after-setup` — the installer default — cannot make its switch
  from HTTP to HTTPS, because that switch *is* a restart.

`cremind boot enable` fixes all of it by handing the job to the operating
system. The service it registers restarts Cremind whenever it exits, which is
what turns those flows from "stops working" into "back in a couple of seconds".

The installers register this for you on native installs; `--no-boot-service`
(`-NoBootService` on Windows) opts out, and the choice is remembered across
re-installs.

**One service per user, not per profile.** A single `cremind serve` process
serves every Cremind profile, so there is exactly one unit — it lives in your
OS user's service registry, not in any profile's state.

## Global flags

`cremind boot` never contacts the server, so it needs **no token and no
profile** — it exists precisely for the state where nothing is running yet.
The root `--json` flag applies to all three subcommands; on `enable` and
`disable` it must be paired with `--yes`, since a confirmation prompt has no
meaning in JSON mode.

## Subcommands

### `cremind boot enable`

```bash
cremind boot enable [--start | --no-start] [--exec PATH] [--print-only] [--yes]
```

| Flag | Default | Meaning |
|---|---|---|
| `--start` / `--no-start` | `--start` | Also start Cremind now. Use `--no-start` when a server is already running on the port — the service takes over at the next login. |
| `--exec PATH` | `<CREMIND_SYSTEM_DIR>/bin/cremind` | The launcher the service runs. |
| `--print-only` | off | Print the unit file and every command, run nothing. |
| `--yes` / `-y` | off | Skip the confirmation prompt. |

The service always launches the **shim** at `<CREMIND_SYSTEM_DIR>/bin/cremind`,
never the virtualenv binary directly. Only the shim loads your `~/.cremind/.env`
(Cremind's settings loader resolves `.env` relative to the working directory, so
a bare `cremind serve` from an arbitrary folder sees none of your install's
settings). A service pointed at the venv binary would come up on the wrong host
and port with TLS disabled.

What gets registered, per platform:

| OS | What it creates | Restart policy |
|---|---|---|
| Linux | systemd user unit `~/.config/systemd/user/cremind.service`, then `systemctl --user enable` | `Restart=always`, `RestartSec=2` |
| macOS | LaunchAgent `~/Library/LaunchAgents/io.cremind.server.plist`, loaded with `launchctl bootstrap gui/$UID` | `KeepAlive` |
| Windows | Scheduled Task `Cremind Server` (at logon, unelevated) plus a respawn loop under `<CREMIND_SYSTEM_DIR>\bin\` | the loop restarts the server in ~2s |

On Windows the task does not run the server directly. Task Scheduler can only
retry a failed task once a minute, which is far too slow for the restart flows
above, so the task starts a small hidden loop that runs the server and restarts
it when it exits.

On Linux, `enable` also runs `loginctl enable-linger` so the service survives
logout. That can be refused by your system's policy — it is reported as a
warning, and `cremind boot status` shows the result.

### `cremind boot disable`

```bash
cremind boot disable [--print-only] [--yes]
```

Stops Cremind, removes the unit, and unregisters it. Safe to run when nothing
is registered. Every teardown step is best-effort — stopping a service that
is not running is an error to the tool but a success here — so the command
confirms the end state with the OS rather than trusting exit codes.

Uninstalling Cremind removes the service too; you do not need to run this first.

To stop the server *once* without unregistering:

| OS | Command |
|---|---|
| Linux | `systemctl --user stop cremind` |
| macOS | `launchctl bootout gui/$(id -u)/io.cremind.server` |
| Windows | `Stop-ScheduledTask -TaskName 'Cremind Server'` |

### `cremind boot status`

```bash
cremind boot status [--json]
```

Reports whether the service is **registered**, whether it is **running**,
whether it **survives logout** (Linux only), and the PID of the server it is
supervising. A value can come back as `unknown` where the OS cannot say —
that is not the same as `no`.

## Logs

| OS | Where |
|---|---|
| Linux | `journalctl --user -u cremind -f`, and `~/.cremind/server.log` |
| macOS | `~/.cremind/server.log` |
| Windows | `~/.cremind/server.log` (rotated at 10 MB by the loop) |

## Troubleshooting

- **`systemd is not the init system here` (WSL).** Default WSL does not run
  systemd. Add `[boot]` / `systemd=true` to `/etc/wsl.conf`, run
  `wsl --shutdown`, reopen the shell, and retry.
- **`The systemd user manager is not reachable from this session`.** Typical
  over SSH without lingering. Run `sudo loginctl enable-linger $USER`,
  reconnect, and try again.
- **Cremind stops when I log out (Linux).** `cremind boot status` will show
  `Survives logout: no`. Fix with `sudo loginctl enable-linger $USER`. This
  matters most on a headless server installed with `--deployment server`.
- **macOS over SSH.** There is no GUI session to load a LaunchAgent into, so
  the plist is written but not started; it loads at your next login on the Mac
  itself.
- **`This install runs in a container` / `The Cremind desktop app starts and
  stops the backend itself`.** Both already have a supervisor, so there is
  nothing to register. Docker restarts the container; the desktop app spawns
  and stops the backend with the app.
- **`CREMIND_SYSTEM_DIR is …, not the default`.** There is one service per
  user, so a side-by-side install is not offered one — run `cremind serve`
  yourself for that install.
- **The Windows task exists but nothing starts.** Check
  `~/.cremind/server.log`. If a `cremind serve` is already listening the loop
  waits rather than fighting it for the port, which is intended.
- **I want this off but keep Cremind installed.** `cremind boot disable`, then
  start it by hand with `cremind serve` when you need it. To keep it off across
  re-installs, pass `--no-boot-service` (`-NoBootService`).
