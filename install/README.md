# Cremind installers

One-line install scripts for Linux, macOS, and Windows. Both end with
the setup wizard open in your browser; the rest of this file is the
reference for what they do, the flags they accept, and how to manage
the install once it's running.

## Quickstart

**Linux / macOS:**

```bash
curl -fsSL https://cremind.io/install.sh | bash
curl -fsSL https://raw.githubusercontent.com/cremind-ai/cremind/main/install/install.sh | bash
```

**Windows (PowerShell):**

```powershell
iwr -useb https://cremind.io/install.ps1 | iex
iwr -useb https://raw.githubusercontent.com/cremind-ai/cremind/main/install/install.ps1 | iex
```

## HTTP and HTTPS

Fresh installs use **HTTP** by default. Select **Enable HTTPS (SSL)** in the
installer to have the Setup Wizard guide you through trusting Cremind's local
certificate before switching to HTTPS. The same choice is available in the
terminal TUI, text prompts, and Electron installer.

For scripts, pass `--ssl after-setup` (PowerShell: `-Ssl after-setup`).
`--ssl auto` / `-Ssl auto` serves HTTPS from the first boot; trust the local
CA before opening its HTTPS page. `none` explicitly selects HTTP. Unattended
installs use HTTP unless a flag or inherited TLS configuration enables HTTPS.
A re-install preserves the existing transport configuration unless you pass
an explicit SSL flag. An inherited `CREMIND_SSL` or custom certificate pair is
also treated as an explicit override and written to the canonical install
environment. Custom URLs remain yours unless you enable in-app TLS.

To enable HTTPS after an HTTP installation, open **Settings > Security**. It
explains the benefits, certificate trust, activation, and restart steps for
your installation. For Kubernetes, use the [Helm HTTPS instructions](../helm/cremind/README.md#switch-an-existing-http-install-to-https):
`cremind.ssl=true` selects the same trust-first setup flow, while `auto`
enables HTTPS immediately. The chart defaults to HTTP.

## Three install modes

The scripts probe what this machine can run and offer only those modes:

| Mode | What you get | When to pick it |
|---|---|---|
| **docker** *(recommended)* | The agent runs inside a sandboxed container. A sub-question asks whether to include the VNC Desktop UI: **yes** (default) pulls `cremind/cremind-desktop` so the agent has an XFCE desktop you can observe at `http://<host>:6080/vnc.html`; **no** (`--no-desktop`) pulls the smaller headless `cremind/cremind`. Either way the Setup Wizard activates Postgres, Qdrant, or ChromaDB as sibling containers on demand. | Anytime Docker is available — the agent is isolated from your host, and per-service deployment choices are made later in the wizard. |
| **native** | A Python venv at `~/.cremind/venv` with `cremind` and SQLite. The agent shares your desktop and home directory. | Docker isn't available, or you want a minimal install without containers. |
| **kubernetes** | The [Helm chart](../helm/cremind/README.md) installed into a kubeconfig context **you pick**, with the bundled PostgreSQL. The installer waits for the rollout, opens a `kubectl port-forward`, and hands you the Setup Wizard at `http://localhost:1515`. See [Kubernetes mode](#kubernetes-mode). | You already run a cluster. Needs `kubectl` with at least one kubeconfig context and `helm` 3.8+. |

A mode is offered only when the machine meets its requirements (`requires` in
[`catalog.toml`](catalog.toml)): docker needs a reachable daemon, kubernetes
needs kubectl and helm, native needs nothing. When exactly one mode qualifies
it is selected without asking, and the list names what is missing for the
rest. `--mode` / `-Mode` for a mode this machine cannot run is an error, not a
silent fallback.

The desktop app's installer stage offers docker and native only: it probes
Docker and Python, not kubectl and helm, so a Kubernetes install runs from
`install.sh` / `install.ps1` in a terminal.

The installer no longer asks which database or vector store to use —
those choices are now per-service, made in the Setup Wizard. Each
service that supports multiple deployments (Postgres, Qdrant, ChromaDB)
gets a Docker / Native / External radio in its wizard step. SQLite
shows no radio (it's local-only).

## What docker mode does

1. Detect Docker, ask deployment type (local / server / custom) and host or advanced fields, then ask whether to include the VNC Desktop UI (default yes; `--desktop` / `--no-desktop` skip the prompt). A re-install defaults to the previously-chosen flavor.
2. Ask for the VNC password *(desktop image only)* — entered twice, 6–8 characters from `[A-Za-z0-9@%_+=:,.-]` (VNC ignores anything past the 8th). Precedence: `--vnc-password` → what you type → the previous install's password → a generated one. Non-interactive runs (`--unattended`, no TTY, the Electron installer) never prompt: they take the flag if given, otherwise keep the previous password, otherwise generate one and print it at the end.
3. Render [`docker-compose.yml`](templates/docker-compose.yml.tmpl) and an `.env` file (image flavor, ports, app URL, CORS, and — for the desktop image — VNC password) into `~/.cremind/docker/`. For a basic (`--no-desktop`) install the noVNC/VNC port maps and VNC env are stripped. The Setup Wizard later appends to `COMPOSE_PROFILES` as the user activates Docker-mode services; the bundle starts with only the `cremind` container running. The rendered compose file pins `INSTALL_MODE: docker` as a literal rather than interpolating it: Compose resolves `${VAR}` from the shell that runs `docker compose up` *before* the sibling `.env`, so a stray `INSTALL_MODE` in that shell used to be baked into the container.
4. Per-channel image strategy:
   - `production` / `test`: resolve the latest version from PyPI / Test PyPI, then `docker compose pull` + `docker compose up -d` (no local build; a missing tag fails hard).
   - `dev`: `docker compose pull --ignore-pull-failures` for sidecars, then `docker compose up -d --build` against the local checkout via `docker-compose.override.yml`.
5. Wait for `http://<host>:1515/health` to return 200.
6. Offer to trust the Cremind local CA on this machine (skipped with
   `--unattended`, or when TLS is off). The CA lives inside the container,
   where the Setup Wizard's one-click trust can't reach your trust store —
   but the installer runs on your machine, so it downloads `/ca.pem` from
   the container and adds it for you (Windows: the current user's Trusted
   Root store, after Windows' own confirmation dialog; macOS/Linux:
   system-wide, via `sudo`). Declining is fine — the wizard's "Secure this
   install" step shows the manual command, and any *other* device you browse
   from needs that path anyway. A re-install detects the CA is already
   trusted and skips the prompt.
7. Open `http://<host>:1515/#/setup` in your browser. *(Desktop image only:* also print the noVNC URL + VNC password.*)* When HTTPS was selected (`after-setup`), this stays `http://` — finishing the wizard restarts the container into `https://`.

The bundle defines four services. Only `cremind` is started at install
time; the others are activated by the wizard:

- **cremind** — Python 3.13 + the Cremind agent + the SPA static server, plus (on the desktop image) XFCE + TigerVNC + noVNC. Always started. Production / test installs pull a pre-built image from Docker Hub — `cremind/cremind-desktop:<version>` (desktop) or `cremind/cremind:<version>` (basic); dev installs build the matching [`Dockerfile`](../Dockerfile) target (`desktop` / `basic`). Mounts `/var/run/docker.sock` so the in-container wizard can `docker compose up -d` the sibling services on demand.
- **postgres** — Application DB (`postgres:16`). Activated when the wizard's Database step picks **Postgres + Docker**.
- **qdrant** — Vector store (`qdrant/qdrant:latest`). Activated when the wizard's Embedding step picks **Qdrant + Docker**.
- **chroma** — Vector store (`chromadb/chroma:latest`). Activated when the wizard's Embedding step picks **ChromaDB + Docker**.

For each of those sidecars you can instead pick **Native** (run a local
binary or in-process library) or **External** (point at a host/port you
already operate) from the wizard. SQLite is always local-only.

Stored at `~/.cremind/docker/`. Manage with:

```
cd ~/.cremind/docker
docker compose ps                  # status
docker compose logs -f cremind      # follow logs
docker compose restart cremind      # restart just the agent
docker compose down                # stop everything
docker compose down -v             # stop and delete all data
```

## What native mode does

1. Detect Python 3.13+ on PATH.
2. Ask deployment type (local / server / custom) and host or advanced fields.
3. Create venv at `~/.cremind/venv` and `pip install cremind`. The
   wheel ships the prebuilt SPA inside it (see
   [`scripts/build_ui.sh`](../scripts/build_ui.sh)), so no separate UI
   install is needed.
4. Generate `~/.cremind/.env` from [`templates/local.env`](templates/local.env), [`templates/server.env.tmpl`](templates/server.env.tmpl) (with `__APP_HOST__` substituted), or [`templates/custom.env.tmpl`](templates/custom.env.tmpl) (with the four advanced-field placeholders substituted). The installer also appends `INSTALL_MODE=docker|native` so the backend's Setup Wizard can filter per-service deployment modes, and the resolved `CREMIND_SSL` (see `--ssl`). That value drives the wizard's filter only: inside a container the backend treats itself as a Docker install whatever `INSTALL_MODE` says (only `kubernetes` is taken at its word, and a pod is never Docker), and says so in the boot log when the container, not the variable, decided.
5. Generate `~/.cremind/bootstrap.toml` selecting SQLite, and run
   `cremind db upgrade` to apply Alembic migrations — **unless** the install
   explicitly selected `after-setup` TLS, in which case both are left to the
   Setup Wizard. `bootstrap.toml` existing is what tells the server setup is
   finished and TLS should be bound, so writing one here would put the wizard
   itself behind a certificate nothing trusts yet. The server boots in
   deferred-storage mode until the wizard completes, exactly as it does under
   Docker and Kubernetes.
6. Write a `cremind` wrapper into `~/.cremind/bin/` and put it on `PATH`. It
   loads `~/.cremind/.env` (anything already in your environment wins) before
   handing over to the venv binary, so a later `cremind serve` from any
   directory keeps the install's settings — including `CREMIND_SSL`, which the
   wizard's restart step depends on.
7. Register a boot service (`cremind boot enable` — a systemd user unit, a
   launchd LaunchAgent, or a logon Scheduled Task) and let it start
   `cremind serve`. It serves the single public origin on `:1515` (UI + API)
   plus an internal loopback API on `:1112`. Wait for `/health`. The service
   is what makes Cremind come back after a reboot **and** what makes the
   wizard's restart work at all — the server stops cleanly rather than
   re-executing itself, so on an unsupervised install a restart leaves it
   down. `--no-boot-service` / `-NoBootService` opts out, and anywhere a
   service cannot be registered (WSL without systemd, SSH to a Mac) the
   installer falls back to the old background process for this session only.
   Upgrading an install that predates the service, the installer stops the
   background server it started last time so the service can own the port —
   otherwise that older, unsupervised process would go on serving the wizard,
   and the restart in step 8 would still have to be done by hand. A server
   the installer did not start (a dev `cremind serve`) is left alone; there
   the service is registered without being started and takes over once that
   one stops.
8. Open `http://<host>:1515/#/setup` in your browser. (Still `http://` on the
   opt-in `after-setup` mode — the switch to `https://` happens when the wizard
   finishes.) On a native install the wizard's "Secure this install" step
   trusts the CA in **one click**: the server runs on this machine, so it
   hands the CA to your OS trust store itself (`POST /api/tls/trust`) —
   no download, no terminal. The OS still asks for its own confirmation.

## Kubernetes mode

Installs the Cremind Helm chart into a cluster you already run. The chart owns
the pod's configuration, so this mode writes no host `.env`, registers no boot
service, and runs no migrations — the Setup Wizard does the first one.

### Which cluster

The question the mode exists to answer. The installer lists every kubeconfig
context it can find — the ones kubectl reads on its own (`$KUBECONFIG`, else
`~/.kube/config`) **plus** those in every other file under `~/.kube`, since
one file per cluster is a common layout — and shows each with its API server
and, when several files are in play, the file it came from:

```
  1) prod-eu — https://eks-prod.example:443 (cremind)  default kubeconfig  [current]
  2) default — https://161.248.199.106:6443 (frp)      ~/.kube/cremind_config
  3) default — https://103.153.69.159:6443 (default)   ~/.kube/ssp_config
```

Whatever you pick is passed as `--kube-context` — and, for a context from a
sibling file, `--kubeconfig` — on **every** helm and kubectl call the installer
makes. The ambient `current-context` never decides where a release lands, so a
context you switched away from an hour ago cannot redirect the install. With
exactly one context it is used without asking; with several, an unattended run
**requires** `--kube-context` rather than guessing. Sibling files routinely
reuse a context name (`default` above), so a name that exists in more than one
file needs `--kubeconfig FILE` as well; `--kubeconfig` on its own restricts the
list to that file.

### What it does

1. Probe `kubectl` (needs ≥ 1 context) and `helm` (needs 3.8+ for OCI charts).
2. Ask for the context, the namespace (default `cremind`, created if missing),
   the desktop image and its VNC password, HTTPS, and — behind one
   "customize?" question — the Helm options below.
3. Reach the cluster once (`kubectl cluster-info`) before anything is changed.
4. Resolve the version: the image tag is PEP 440 (`0.0.17rc13.dev1`), the
   chart version is its SemVer2 spelling (`0.0.17-rc.13.dev.1`). Both come
   from one release; `app/upgrade/channel.py chart-version` translates.
5. Write `~/.local/share/cremind/k8s/values.yaml` and run
   `helm upgrade --install` with it. Secrets ride the file rather than the
   command line, and the file is yours to reuse by hand afterwards.
6. Wait for PostgreSQL, then for the Cremind pod (`kubectl rollout status`).
7. Start a background `kubectl port-forward`, wait for `/health`, offer to
   trust the local CA when HTTPS is on, and open the Setup Wizard.
8. Record the release in `k8s/release.env` and write `credentials.toml`.

The manual `port-forward` command, the wizard URL, the noVNC URL and the VNC
password are printed either way.

### The values it sends

| Answer | Chart value | Default |
|---|---|---|
| desktop UI | `desktop.enabled` | `true` |
| VNC password | `cremind.vncPassword` | kept from the previous release, else generated |
| `--ssl` | `cremind.ssl` | `none` |
| `--k8s-app-url` | `cremind.appUrl` | **unset** — the chart derives `http(s)://localhost:1515` |
| `--k8s-legacy-postgres-image` | `postgresql.image.registry` + `.repository` | `yes` → `docker.io/bitnamilegacy/postgresql` |
| `--k8s-delete-postgres-data` | `postgresql.primary.persistentVolumeClaimRetentionPolicy` | `no` (the chart keeps the volume) |
| *(always)* | `postgresql.auth.password` | pinned, see below |
| `--k8s-extra-set` | appended as one trailing `--set` | — |

`--k8s-extra-set` is applied **last**, so it overrides anything above it.

Two defaults are worth knowing. The **legacy Bitnami image** is on because
Bitnami froze its free images into the `bitnamilegacy` namespace and the
chart's own default no longer pulls. The **app URL is deliberately not sent**:
the chart derives exactly the value a port-forward needs, and an explicit
`http://` value is what makes a later `--ssl` run fail the chart's own
validation.

### The Postgres password

The installer pins `postgresql.auth.password` instead of letting the subchart
generate one, and records it in `k8s/release.env`. Without that pin, an
uninstall-and-reinstall generates a new password while the retained data
volume keeps the old one, and setup fails with `password authentication failed
for user "cremind"` long after the cause.

Two consequences:

- Installing over a release the installer did not create **adopts** the live
  Secret's password rather than overwriting it.
- A retained volume from an earlier release whose password this machine never
  saw is a hard error, with the `kubectl delete pvc` line and the
  `--k8s-postgres-password` escape hatch both printed.

### Re-running and uninstalling

Re-running upgrades the release in place and re-sends every value, so nothing
silently carries over. The Postgres and VNC passwords are reused. Pointing a
re-run at a different context, namespace or release name asks first, and
refuses outright when unattended — the previously tracked release would
otherwise keep running with nothing recording where it is.

`--reinstall` uninstalls the release and installs it again (the pinned
password keeps a retained volume usable).

`--uninstall --keep` removes the Helm release and keeps both the PostgreSQL
volume and `k8s/release.env`, so a later install picks the data back up.
`--uninstall --purge` additionally deletes the bundled PostgreSQL / Qdrant /
ChromaDB volumes and — only if the installer created it — the namespace.

The chart's own PVCs (`system`, `venv`, `work`) are removed by
`helm uninstall` itself, in both modes; only the StatefulSet subcharts' data
volumes survive it.

### The production channel needs the desktop image

`--channel production --no-desktop` is refused. On the production channel the
Helm chart is pushed to `cremind/cremind:<version>` on Docker Hub, the same
tag the basic image uses, and the chart lands there second — so a headless
production install would ask Kubernetes to run a chart artifact as a container
image. Release candidates do not collide, so `--channel test --no-desktop`
works.

On `--channel dev` the local `helm/cremind` chart is installed (after
`helm dependency build`) against the newest published **test** image, because
no dev image or dev chart exists. The pod therefore reports the *test* channel
on its Updates page.

---

All three modes are idempotent: re-running upgrades in place and keeps your
existing config + database. Use `--reinstall` (sh) or `-Reinstall` (ps1)
to wipe and start fresh.

## Deployment types

Both installers ask **how** Cremind will be reached:

| Deployment | What it sets | When to pick it |
|---|---|---|
| **local**  | `HOST=127.0.0.1`. Only this machine can reach the backend. | Single-user desktop install. |
| **server** | `HOST=0.0.0.0`, `APP_URL=http://<your-host>:1515`, CORS allows your host. | Multi-machine setup; pass `--host`/`-AppHost` with your public IP or domain. |
| **custom** *(advanced)* | You pick `HOST`, `APP_URL`, `CORS_ALLOWED_ORIGINS`, and the Setup Wizard preset yourself. | Inside containers, behind reverse proxies, or anywhere the local/server presets don't fit. |

`container` is accepted as a deprecated alias for `custom` (with
container-friendly defaults) for one release; new scripts should use
`custom` directly.

## Flags (sh)

| Flag | Description |
|---|---|
| `--deployment local\|server\|custom` | Skip the deployment-type prompt. |
| `--host HOST`                        | Public IP/domain (server only). |
| `--listen-host HOST`                 | (custom) Override `HOST` in the rendered `.env`. |
| `--public-url URL`                   | (custom) Override `APP_URL`. |
| `--allowed-origins LIST`             | (custom) Override `CORS_ALLOWED_ORIGINS`. |
| `--wizard-preset ID`                 | (custom) Override `SETUP_WIZARD_ENV`. |
| `--mode docker\|native\|kubernetes`  | Skip the mode prompt. `--docker`, `--native` and `--kubernetes` are aliases. A mode this machine cannot run is an error. |
| `--kube-context CTX`                 | (kubernetes) The kubeconfig context to install into, passed to every helm and kubectl call. Required unattended when more than one context exists. |
| `--kubeconfig FILE`                  | (kubernetes) The kubeconfig file holding that context. Without it, every file under `~/.kube` is listed alongside kubectl's own config, and a `--kube-context` that exists in several files is refused until you add this. Rides every helm and kubectl call as `--kubeconfig`. |
| `--kube-namespace NS`                | (kubernetes) Namespace for the release, created if missing. Default `cremind`. |
| `--k8s-release-name NAME`            | (kubernetes) Helm release name. Default `cremind`. |
| `--k8s-app-url URL`                  | (kubernetes) `cremind.appUrl`. Leave unset to let the chart derive `http(s)://localhost:1515`. |
| `--k8s-legacy-postgres-image yes\|no` | (kubernetes) Use `docker.io/bitnamilegacy/postgresql`. Default `yes`. |
| `--k8s-delete-postgres-data yes\|no` | (kubernetes) Delete the Postgres volume on uninstall. Default `no`. |
| `--k8s-extra-set K=V,K2=V2`          | (kubernetes) The value of one extra helm `--set`, applied last. |
| `--k8s-postgres-password PW`         | (kubernetes) Adopt a retained Postgres volume whose password this installer never saw. |
| `--helm-chart REF`                   | (kubernetes) Chart to install: an OCI reference, a `.tgz`, or a directory. |
| `--no-port-forward`                  | (kubernetes) Don't start the background port-forward; just print the command. |
| `--desktop` / `--no-desktop`         | (docker) Include or skip the VNC Desktop UI. Default: desktop, incl. `--unattended`; a re-install keeps the previous choice. `--no-desktop` pulls the headless `cremind/cremind`. |
| `--vnc-password PW`                  | (docker + desktop) Password for the VNC Desktop. 6–8 chars from `[A-Za-z0-9@%_+=:,.-]`. Interactive installs ask for it (twice) instead; unattended runs fall back to the previous install's password, else a generated one. An invalid value is a hard error in every mode. |
| `--ssl none\|auto\|after-setup`      | TLS on the public origin. Default `none` (HTTP). Select Enable HTTPS or pass `after-setup` for certificate trust during the wizard followed by HTTPS. `auto` is HTTPS from boot one. A re-install preserves its previous choice unless this flag is supplied. Works with native, Docker, custom, and Electron installs. |
| `--boot-service` / `--no-boot-service` | (native) Register a login/boot service that starts `cremind serve` and restarts it if it stops. Default: on — it is also what makes the in-app restart and the after-setup HTTPS switch work. A re-install keeps a previous opt-out. Ignored for docker (the daemon supervises the container) and for Electron-driven installs. Manage it later with `cremind boot`. |
| `--no-launch`                        | Don't open the wizard at the end. |
| `--unattended`                       | Use defaults; never prompt. Implies `--mode docker` if Docker is present. |
| `--reinstall`                        | Wipe the existing venv (native) or regenerate compose+.env (docker). |

## Flags (ps1)

| Flag | Description |
|---|---|
| `-Deployment local\|server\|custom` | Skip the deployment-type prompt. |
| `-AppHost HOST`                     | Public IP/domain (server only). |
| `-ListenHost HOST`                  | (custom) Override `HOST`. |
| `-PublicUrl URL`                    | (custom) Override `APP_URL`. |
| `-AllowedOrigins LIST`              | (custom) Override `CORS_ALLOWED_ORIGINS`. |
| `-WizardPreset ID`                  | (custom) Override `SETUP_WIZARD_ENV`. |
| `-Mode docker\|native\|kubernetes`  | Skip the mode prompt. A mode this machine cannot run is an error. |
| `-KubeContext CTX`                  | (kubernetes) The kubeconfig context to install into, passed to every helm and kubectl call. Required unattended when more than one context exists. |
| `-KubeConfig FILE`                  | (kubernetes) The kubeconfig file holding that context. Without it, every file under `~\.kube` is listed alongside kubectl's own config, and a `-KubeContext` that exists in several files is refused until you add this. Rides every helm and kubectl call as `--kubeconfig`. |
| `-KubeNamespace NS`                 | (kubernetes) Namespace for the release, created if missing. Default `cremind`. |
| `-K8sReleaseName NAME`              | (kubernetes) Helm release name. Default `cremind`. |
| `-K8sAppUrl URL`                    | (kubernetes) `cremind.appUrl`. Leave unset to let the chart derive `http(s)://localhost:1515`. |
| `-K8sLegacyPostgresImage yes\|no`   | (kubernetes) Use `docker.io/bitnamilegacy/postgresql`. Default `yes`. |
| `-K8sDeletePostgresData yes\|no`    | (kubernetes) Delete the Postgres volume on uninstall. Default `no`. |
| `-K8sExtraSet K=V,K2=V2`            | (kubernetes) The value of one extra helm `--set`, applied last. |
| `-K8sPostgresPassword PW`           | (kubernetes) Adopt a retained Postgres volume whose password this installer never saw. |
| `-HelmChart REF`                    | (kubernetes) Chart to install: an OCI reference, a `.tgz`, or a directory. |
| `-NoPortForward`                    | (kubernetes) Don't start the background port-forward; just print the command. |
| `-Desktop` / `-NoDesktop`           | (docker) Include or skip the VNC Desktop UI. Default: desktop, incl. `-Unattended`; a re-install keeps the previous choice. `-NoDesktop` pulls the headless `cremind/cremind`. |
| `-VncPassword PW`                   | (docker + desktop) Password for the VNC Desktop. 6–8 chars from `[A-Za-z0-9@%_+=:,.-]`. Interactive installs ask for it (twice) instead; unattended runs fall back to the previous install's password, else a generated one. An invalid value is a hard error in every mode. |
| `-Ssl none\|auto\|after-setup`      | TLS on the public origin. Default `none` (HTTP). Select Enable HTTPS or pass `after-setup` for certificate trust during the wizard followed by HTTPS. `auto` is HTTPS from boot one. A re-install preserves its previous choice unless this flag is supplied. Works with native, Docker, custom, and Electron installs. |
| `-BootService` / `-NoBootService`   | (native) Register a logon Scheduled Task that starts `cremind serve` and restarts it if it stops. Default: on — it is also what makes the in-app restart and the after-setup HTTPS switch work. A re-install keeps a previous opt-out. Ignored for docker and for Electron-driven installs. Manage it later with `cremind boot`. |
| `-NoLaunch`                         | Don't open the wizard at the end. |
| `-Unattended`                       | Use defaults; never prompt. |
| `-Reinstall`                        | Wipe the existing venv or regenerate compose+.env. |

## Shared catalog

Deployment labels, install-mode descriptions, and the per-install-mode
service-mode visibility rules live in [`catalog.toml`](catalog.toml).
Both install scripts and the Setup Wizard read from it:

- `install.sh` and `install.ps1` source the generated bash and
  PowerShell includes ([`_catalog.sh`](_catalog.sh) and
  [`_catalog.ps1`](_catalog.ps1)).
- The backend ships a copy at `app/config/install_catalog.toml` and
  serves it to the wizard via `GET /api/config/install-catalog`.

Re-generate the includes after editing `catalog.toml`:

```bash
python install/scripts/build_catalog.py        # write the includes
python install/scripts/build_catalog.py --check  # CI: exit 1 on drift
```

When re-running on an existing Docker install (without `--reinstall` /
`-Reinstall`), the installer reuses the existing
`~/.cremind/docker/.env`. Service-deployment choices are persisted by
the wizard, not by the installer, so they survive re-runs as well.

## Files written

### Docker mode

| Path | Purpose |
|---|---|
| `~/.cremind/docker/docker-compose.yml` | Compose orchestration for cremind, postgres, qdrant. |
| `~/.cremind/docker/.env`               | Secrets and config that Compose substitutes (chmod 600). |
| `~/.cremind/install.log`               | Output of `compose pull` / `compose up`. |
| Docker named volumes                  | `cremind-data`, `pg-data`, `qdrant-data`. Persisted across restarts. |

### Native mode

| Path | Purpose |
|---|---|
| `~/.cremind/venv/`                     | Python virtualenv with `cremind` (SPA included). |
| `~/.cremind/.env`                      | Backend env vars. |
| `~/.cremind/bootstrap.toml`            | DB-provider selection. |
| `~/.cremind/install.log`               | Output of `pip install` and `cremind db upgrade`. |
| `~/.cremind/install.pid`               | PID of the install-session server (fallback path only — never a service-run one). |
| `~/.cremind/server.pid`                | PID of the server, written by the server itself when a boot service supervises it. |
| `~/.cremind/server.log`                | `cremind serve` stdout/stderr. |

### Kubernetes mode

Nothing is written to the System Dir: the pod owns its own state, on the
chart's PVCs.

| Path | Purpose |
|---|---|
| `<install dir>/k8s/release.env`      | Which context/namespace/release this machine installed, and the pinned Postgres + VNC passwords. Read line by line, never sourced. Mode 600. |
| `<install dir>/k8s/values.yaml`      | The values handed to helm. Regenerated every run; reusable by hand with `helm upgrade -f`. Mode 600. |
| `<install dir>/k8s/port-forward.pid` | The background `kubectl port-forward`. Stop it with `kill $(cat …)`. |
| `<install dir>/k8s/port-forward.log` | That process's output. |
| `<install dir>/credentials.toml`     | Connection info + passwords, `install_mode = "kubernetes"`. |
| `<install dir>/install.log`          | helm and kubectl output. |

Plus, when a boot service is registered — all owned by `cremind boot`, and
removed by `cremind boot disable` or by uninstalling:

| Path | Purpose |
|---|---|
| `~/.config/systemd/user/cremind.service` | (Linux) The systemd user unit. |
| `~/Library/LaunchAgents/io.cremind.server.plist` | (macOS) The LaunchAgent. |
| Scheduled Task `Cremind Server`         | (Windows) The logon trigger. |
| `~/.cremind/bin/cremind-task.xml`, `cremind-boot.vbs`, `cremind-boot-loop.ps1` | (Windows) The task definition and its respawn loop. |
| `~/.cremind/supervisor.pid`            | (Windows) PID of the respawn loop. |

`CREMIND_WORKING_DIR` overrides `~/.cremind` for side-by-side staging
installs. `CREMIND_TEMPLATE_BASE` overrides the URL the scripts fetch
templates from (useful for offline installs and CI).

## Stopping things

**Docker:**

```
cd ~/.cremind/docker
docker compose down
```

**Native (boot service — the default):**

```bash
systemctl --user stop cremind                          # Linux, this session
launchctl bootout gui/$(id -u)/io.cremind.server       # macOS, this session
```

```powershell
Stop-ScheduledTask -TaskName 'Cremind Server'          # Windows, this session
```

Those stop it until the next login. To unregister it altogether:

```
cremind boot disable
```

`cremind boot status` reports whether the service is registered, running, and
surviving logout. Uninstalling removes it too — you do not need to disable it
first.

**Kubernetes:** the port-forward is a local process; the release is not.

```bash
kill $(cat ~/.local/share/cremind/k8s/port-forward.pid)   # close the tunnel
helm uninstall cremind --kube-context <ctx> [--kubeconfig <file>] -n <namespace>  # stop the release
```

```powershell
Stop-Process -Id (Get-Content $env:LOCALAPPDATA\Cremind\k8s\port-forward.pid)
```

Reopen the tunnel with the command the installer printed (it is also in
`credentials.toml`). Prefer `--uninstall` over a bare `helm uninstall`: it
stops the forward, applies the keep/purge volume rules, and clears the local
record.

**Native (fallback, `--no-boot-service` or where no service could be
registered):** the server runs only until you log out or stop it explicitly.

```bash
kill $(cat ~/.cremind/install.pid)
```

```powershell
Stop-Process -Id (Get-Content ~/.cremind/install.pid)
```

## Building the SPA into the wheel

The `cremind` wheel ships the prebuilt SPA at `app/static/ui/` so
`cremind serve` can serve it on `:1515` without a separate UI install.
CI runs [`scripts/build_ui.sh`](../scripts/build_ui.sh) before
`hatch build`; you can run it locally too:

```bash
./scripts/build_ui.sh
```

On Windows (no Bash), use the PowerShell port [`scripts/build_ui.ps1`](../scripts/build_ui.ps1):

```powershell
.\scripts\build_ui.ps1
```

The script builds the in-tree `ui/` source and writes to
`app/static/ui/` (gitignored). When `cremind serve` boots it auto-detects
this directory and starts the SPA listener. `CREMIND_UI_PORT=0` disables
the listener (useful when an external nginx is already serving the SPA);
`CREMIND_UI_DIR=/elsewhere` points at a different built location, which
is exactly how the Docker image serves the stage-1 SPA build.
