---
description: "Is this Cremind running in Docker, native, or Kubernetes — on the dev, test, or production release channel, with the VNC desktop enabled or not? `cremind server environment` and `server capabilities` answer that, plus deployment (local/server/custom), supervisor, backend version, system paths, and the timezone its schedules actually fire in; `server health`, `version`, and `restart` operate the running backend. On Kubernetes `server environment` also answers which namespace this pod is in, which Helm release installed it, and which Deployment and Service it is, and prints the ready-to-run `kubectl port-forward` command that reconnects to it (`kubernetes.namespace`, `kubernetes.release`, `kubernetes.workload`, `kubernetes.service`, `kubernetes.service_port`, `kubernetes.source`, `kubernetes.port_forward`; `source: inferred` means an older chart states no release name — `helm list --all-namespaces` shows it). It also says how to reach the VNC desktop / noVNC / remote desktop: a published Docker port, a path on the same origin behind the Kubernetes nginx proxy, or a `kubectl port-forward` you have to run first, with the URL to open afterwards (`vnc.access`, `vnc.novnc_url`, `vnc.port_forward.1`, `vnc.open_url`, `vnc.note`; `server capabilities` carries the public `vnc_access` word without a token). Distinct from `cremind version`, which prints the locally installed CLI package."
---

# `cremind server` — Server Operations

`cremind server` controls and inspects the *running* Cremind backend — the
operational surface of the web UI's **Developer** page. It complements
`cremind serve` (which starts a server in-process) and the root
`cremind version` (which prints the *locally installed* package version).

How that server *listens* — bind address, port, and whether the public origin
serves HTTPS with HTTP/2 — is configured on `cremind serve` itself (see
`cremind serve --help`) or, for a setting that must survive a restart, in
`~/.cremind/.env`. A restart re-execs the server without any command-line
flags, so the `.env` is the durable place for them.

Three read commands (`health`, `version`, `capabilities`) hit unauthenticated
endpoints, so they work without a token — handy for probing a server before
login or against a remote `--server`. `environment` and `restart` are
**admin-only**: the full description names the server's paths and bind host,
which is not public the way the tray-gating list is.

## Finding this in the web UI

> **Sidebar → Developer → Environment / VNC Desktop / Restart Server**

The Environment card, the restart control, its install-mode caveats, and the
health/version probes that back the update banner all live on that page. On
Kubernetes the Environment card also shows the namespace, Helm release,
Deployment/Service and the port-forward command — the same rows
`server environment` prints as `kubernetes.*`.

The **VNC Desktop** card appears only where the desktop exists (the
`cremind-desktop` image; never on native). It has an **Open desktop** button —
a new browser tab, or a dedicated window in the Electron app — the noVNC URL,
the VNC password, and, on Kubernetes, the `kubectl port-forward` command that
has to be running first. Those are the `vnc.*` rows of `server environment`.

## Global flags

All subcommands accept the root-level `--json` flag. Because it is a **root**
flag it goes before the group name, not after the subcommand:

```bash
cremind --json server environment      # correct
cremind server environment --json      # WRONG — "No such option: --json"
```

## Subcommands

### `cremind server health`

**Purpose.** Probe `/health` and report each subsystem's state.

**Syntax.**

```bash
cremind server health
```

**Behavior.** Prints `status`, `db`, and `vectorstore`. A `disabled` vector
store is healthy, not an error. Exits **non-zero** when the server reports a
degraded subsystem (HTTP 503), so it's usable as a scripted liveness gate. No
token required.

**Example.**

```bash
$ cremind server health
status:       ok
db:           ok
vectorstore:  disabled
```

### `cremind server version`

**Purpose.** Show the *connected server's* build version and release channel.

**Syntax.**

```bash
cremind server version
```

**Behavior.** Prints `backend` (SemVer), `schema` (Alembic head), `channel`
(`production`/`test`/`dev`), and `min_supported_upgrade_from`. No token
required.

> **Not the same as `cremind version`.** The root command prints the version of
> the CLI package installed locally; `server version` reports what the server
> you're talking to is actually running. They can differ.

**Example.**

```bash
$ cremind server version
backend:                     0.3.1
schema:                      20260627_llm_messages
channel:                     production
min_supported_upgrade_from:  0.1.0
```

### `cremind server capabilities`

**Purpose.** Show the server's install mode, deployment, and the UI features it
exposes — the non-secret half of `server environment`, without a token.

**Syntax.**

```bash
cremind server capabilities
```

**Behavior.** Reads the public tray-capabilities endpoint:

| Field             | Meaning                                                                                                                                                 |
|-------------------|---------------------------------------------------------------------------------------------------------------------------------------------------------|
| `install_mode`    | `docker` / `native` / `kubernetes` (`electron` on older backends). The same value `server environment` prints — both read one shared description, so they cannot disagree about the same machine. |
| `supervised`      | Whether something restarts the backend when it exits — true whenever `install_mode` above is `docker` or `kubernetes` — including an older install where `INSTALL_MODE` is absent and only the container signal says so — under Electron, and on a native install with a boot service (`cremind boot enable`). Also the same value `server environment` prints. |
| `ui_features`     | Which SPA pages this build ships (drives the Electron tray entries).                                                                                      |
| `deployment`      | `local` / `server` / `custom` / `kubernetes` — how the install is reached.                                                                                 |
| `release_channel` | `production` / `test` / `dev`.                                                                                                                            |
| `vnc_enabled`     | Whether this install has the VNC desktop (the desktop Docker image; never on native).                                                                     |
| `vnc_access`      | *How* a browser reaches that desktop: `direct` (a port Docker publishes), `same_origin` (a path on the origin you already reach Cremind on, behind the Kubernetes nginx proxy), `port_forward` (its own Service port, which nothing forwards until you run a tunnel), or blank when there is no desktop. The namespaces and ready-to-run commands stay behind `environment`; this endpoint is unauthenticated, so it carries only the word. |
| `container`       | Whether the backend runs inside a container.                                                                                                              |

No token required. (The richer admin `/api/services/capabilities` used by the
Setup Wizard is intentionally not wrapped here.)

**Example.**

```bash
$ cremind server capabilities
install_mode:     docker
supervised:       true
ui_features:      processes, events, channels
deployment:       local
release_channel:  production
vnc_enabled:      true
vnc_access:       direct
container:        true
```

### `cremind server environment`

**Purpose.** Describe the install the server is running in — Docker vs native
vs Kubernetes, which release channel, whether the VNC desktop is enabled, and
where the install lives on disk.

**Syntax.**

```bash
cremind server environment
cremind --json server environment
```

**Behavior.** Requires an **admin token**. Prints the same facts the Developer
page's Environment card shows, in one aligned block:

- `release_channel` — `production`, `test`, or `dev` (set at install time in
  `CREMIND_UPGRADE_CHANNEL`; switching channels is a reinstall, not a toggle).
- `install_mode` / `container` / `image_flavor` / `vnc_enabled` — `docker`,
  `native`, or `kubernetes`; the image flavor is `desktop` (cremind-desktop,
  ships the VNC desktop) or `basic` (headless). A Docker image predating the
  flavor split reports no flavor and still has the desktop.
- `deployment` — `local` (only this machine), `server` (reachable from other
  devices), `custom`, or `kubernetes`. It is the answer given at install time:
  a native install reads it from `ENV` in `~/.cremind/.env`, a container from
  `SETUP_WIZARD_ENV` (the compose file pins `ENV=production` for every
  container, so it says nothing about how the operator chose to reach it). On a
  `custom` deployment the operator's own answers are appended as
  `custom.listen_host`, `custom.public_url`, `custom.allowed_origins`, and
  `custom.wizard_preset`.
- `supervised` / `electron` — whether something restarts the backend when it
  exits, and whether it was launched by the Electron desktop app. `supervised`
  follows the `install_mode` above rather than a variable of its own, so a
  container never reports itself as a container nothing restarts.
- `backend_version`, `python_version`, `os`, `os_release`.
- `app_url`, `host`, `system_dir`, `install_dir` — the public URL, the bind
  address, `~/.cremind` (or wherever it was relocated), and the install root.
- `effective_timezone` — the zone schedules actually fire in, resolved rather
  than read off a variable: the profile's own **Config → System timezone**,
  else the `admin` profile's (which every profile inherits until it sets its
  own), else `CREMIND_TIMEZONE`, else the server's OS zone. Because the command
  is admin-only this is `admin`'s answer, i.e. the inherited default for the
  whole install. Never blank.
- `boot_timezone` — the `CREMIND_TIMEZONE` environment variable on its own,
  which is what a Docker/VPS install sets as the default for every profile.
  Blank on most installs, and blank is not "no timezone": it means nothing
  overrode the OS zone at boot, and `effective_timezone` above is still the
  answer.

**On Kubernetes only**, seven more rows say *where in the cluster this pod is*.
A pod cannot infer any of this — Kubernetes injects no release or Deployment
name — so the chart states it (`CREMIND_K8S_NAMESPACE`, `_RELEASE`,
`_WORKLOAD`, `_SERVICE_PORT`) and the app falls back to what it can read off
itself. Off Kubernetes the block is absent entirely.

- `kubernetes.namespace` — the namespace this pod runs in.
- `kubernetes.release` — the Helm release that installed it. **Blank on an older
  chart**: nothing in a pod records the release name, so it is the one value
  with no fallback. `helm list --all-namespaces` shows it.
- `kubernetes.workload` / `kubernetes.service` — the Deployment and the Service,
  which the chart names alike. Falls back to the pod's own hostname with the
  ReplicaSet hash and pod suffix stripped.
- `kubernetes.service_port` — the Service port (`80` unless the chart says
  otherwise).
- `kubernetes.source` — `chart` when all three names were stated, `inferred`
  when any fallback fired (so confirm the release with `helm list`), blank when
  the pod could name nothing at all.
- `kubernetes.port_forward` — the ready-to-run
  `kubectl --namespace <ns> port-forward svc/<service> 1515:<port>` line that
  reconnects to this install from your own machine. Present only when the
  namespace and the Service are both known — a command with a placeholder in it
  belongs to the HTTPS runbook (`cremind tls status`), not to a row meant to be
  copied and run.

**When the VNC desktop is enabled**, further rows say how a browser reaches it.
`vnc.access` is `direct`, `same_origin` or `port_forward` (see
`server capabilities` above); `vnc.novnc_url` is where noVNC answers (composed
from the server URL you are talking to on a `direct` install, because only the
client knows which address it reached Docker on); `vnc.port_forward.1`,
`vnc.port_forward.2`, … are the `kubectl port-forward` commands that must be
running **first** on Kubernetes; `vnc.open_url` is the page to open once one of
them is up; and `vnc.note` explains the scheme (noVNC on its own Docker port is
plain http even when Cremind serves HTTPS; behind the Kubernetes proxy it shares
Cremind's scheme; with in-pod TLS the sidecar is a plain TCP relay, so noVNC
answers on its own Service port over http and has to be tunnelled).

`cremind --json server environment` prints the raw object, including
`deployment_custom_fields` whatever the deployment is, and the `kubernetes` and
`vnc` blocks unflattened (`kubernetes` is `null` off Kubernetes).

A one-line summary is also in the agent's system prompt, so asking the assistant
"am I on Docker?" or "is VNC on?" in chat is answered directly — this command is
what to run when the answer must be scriptable or complete. The cluster identity
and the VNC commands are deliberately **not** in that prompt line (it has to stay
byte-stable for prompt caching), so "which namespace am I in?" and "how do I open
the desktop?" are answered by the assistant running this command in its shell.

**Example (Docker desktop image).**

```bash
$ cremind server environment
release_channel:     production
install_mode:        docker
deployment:          local
container:           true
image_flavor:        desktop
vnc_enabled:         true
supervised:          true
electron:            false
backend_version:     0.3.1
python_version:      3.12.7
os:                  Linux
os_release:          6.6.32-linuxkit
app_url:             http://localhost:1515
host:                0.0.0.0
system_dir:          /root/.cremind
install_dir:         /app
effective_timezone:  Asia/Ho_Chi_Minh
boot_timezone:
vnc.access:          direct
vnc.novnc_url:       http://localhost:6080/vnc.html
vnc.note:            noVNC listens on its own port over plain http; Cremind's own HTTPS does not cover it.
```

**Example (Kubernetes, chart-stated identity, desktop behind the nginx proxy).**
Only the rows the install adds are shown:

```bash
$ cremind server environment
install_mode:            kubernetes
deployment:              kubernetes
container:               true
...
kubernetes.namespace:    lee-cremind
kubernetes.release:      cremind
kubernetes.workload:     cremind
kubernetes.service:      cremind
kubernetes.service_port: 80
kubernetes.source:       chart
kubernetes.port_forward: kubectl --namespace lee-cremind port-forward svc/cremind 1515:80
vnc.access:              same_origin
vnc.novnc_url:           http://localhost:1515/vnc/vnc.html
vnc.port_forward.1:      kubectl --namespace lee-cremind port-forward svc/cremind 1515:80
vnc.open_url:            http://localhost:1515/vnc/vnc.html
vnc.note:                The desktop is served by the nginx sidecar on the app origin, so it uses the same scheme you reach Cremind with.
```

An **older chart** that states none of the `CREMIND_K8S_*` variables still fills
in what the pod can read off itself, and says so:

```bash
kubernetes.namespace:    lee-cremind
kubernetes.release:
kubernetes.workload:     cremind
kubernetes.service:      cremind
kubernetes.service_port: 80
kubernetes.source:       inferred
kubernetes.port_forward: kubectl --namespace lee-cremind port-forward svc/cremind 1515:80
```

`source: inferred` with a blank `release` is the answer to "why does Settings →
HTTPS & Certificate still say `<release>`?" — `helm upgrade` needs a name only
Helm knows, so run `helm list --all-namespaces` for it.

With in-pod TLS (`cremind.ssl`) the sidecar becomes a plain TCP relay and noVNC
moves to its own Service port, so `vnc.access` is `port_forward` and there are
two commands: one tunnelling `6080:6080` alone, one carrying Cremind and the
desktop together (`… 1515:80 6080:6080`).

### `cremind server restart`

**Purpose.** Restart the backend process (admin).

**Syntax.**

```bash
cremind server restart [--yes/-y]
```

**Flags.**

| Flag           | Type | Default | Meaning                        |
|----------------|------|---------|--------------------------------|
| `--yes`, `-y`  | bool | `false` | Skip the confirmation prompt.  |

**Behavior.** Active HTTP, SSE, and chat connections drop while the server is
unavailable. The stop itself is graceful: the backend finishes its shutdown
hooks — channel adapters and their sidecars, processes started by agents, open
terminals — before exiting, and a detached watchdog force-stops it only if that
hangs (~25s). Whether it comes back on its own depends on the install mode,
which the command reads first and warns about before confirming:

- **docker** — the container restarts automatically (usually 5–15s).
- **electron** — Cremind relaunches the backend automatically.
- **native / unknown** — **no supervisor**: the backend stays DOWN and you must
  relaunch `cremind serve` manually.

Unless `--yes` is given, the caveat prints to stderr and the command asks for
confirmation. On success it prints `restarting (pid <pid>)`. Requires an admin
token.

**Example.**

```bash
$ cremind server restart
Docker install — the container will restart automatically (usually 5-15 seconds).
Restart the Cremind server now? [y/N]: y
restarting (pid 12841)
```

## Troubleshooting

**`server restart` says the backend will stay DOWN** — You're on a `native`
install with no supervisor, so nothing brings the backend back. Register one
with `cremind boot enable` (a systemd user unit, a launchd LaunchAgent, or a
logon Scheduled Task) and restarts come back on their own; Docker and Electron
installs are supervised already. Without it, run `cremind serve` again after
each restart.

**`server health` exits non-zero** — A subsystem is degraded (HTTP 503). Run it
again or check `cremind logs tail --level error` for the cause. A `disabled`
vector store is *not* a failure.

**A read command works without a token but `environment` or `restart` fails
with 403** — That's expected: `health`, `version`, and `capabilities` are
public; `environment` (paths, bind host, cluster object names, ready-to-run
`kubectl` lines) and `restart` need an admin token. To answer "Docker or
native?", "which release channel?", "is VNC on?" or "how is the desktop
reached?" without one, `server capabilities` carries `install_mode`,
`release_channel`, `vnc_enabled` and `vnc_access` already — but not the
namespace, Service name or port-forward commands, which stay admin-only.
