# Cremind Helm chart

Deploys [Cremind](https://github.com/cremind/cremind) on Kubernetes as a
**single-replica** Deployment that boots straight into the **Setup Wizard** —
no install script runs. Storage is constrained for a clustered environment:
**PostgreSQL only** (SQLite is rejected), **embeddings off by default**, and if
embeddings are enabled, **only external/in-cluster vector stores** (Qdrant or
ChromaDB over HTTP) — never pod-local persistent storage.

By default the pod runs the published `cremind/cremind-desktop` image. The chart sets
`INSTALL_MODE=kubernetes` and `SETUP_WIZARD_ENV=kubernetes`, mounts
`CREMIND_SYSTEM_DIR` and the runtime venv on PersistentVolumeClaims, and — by
design — **never sets `CREMIND_DB_PROVIDER`** (which would skip the wizard).

### Image flavor (`desktop.enabled`)

`desktop.enabled` (default `true`) picks which published image the pod runs:

- **`true`** → `cremind/cremind-desktop`: bundles XFCE + TigerVNC + noVNC +
  Chrome so the agent drives a real GUI, watchable at `<service>/vnc/`.
- **`false`** → `cremind/cremind`: the headless **basic** image (smaller, no
  GUI). The chart then drops the `/vnc/` + `/websockify` proxy routes, the
  novnc/vnc container ports, and the `RESOLUTION`/`VNC_PASSWORD` env — so
  `/vnc/vnc.html` 404s and any GUI-dependent agent feature is unavailable.

Both flavors ship under the same chart `appVersion`. Flipping the toggle on an
existing release recreates the pod on the other image; the PVCs
(system/venv/work) re-attach and the venv content is wheel-identical, so no
re-setup is needed. To override the repository directly (wins over the toggle),
set `image.repository`.

### Single entry point

An in-pod **nginx reverse-proxy sidecar** fronts the whole workflow on **one
user-facing port** so you never forward more than one: it routes `/api` +
`/health` to the backend, `/vnc/` to the agent's desktop (noVNC — desktop
flavor only), and everything else to the SPA. The SPA calls the backend
**same-origin**, so everyday use works behind a single port-forward or a single
Ingress hostname. An enabled Ingress targets a separate, private ClusterIP and
nginx listener. That trust boundary lets the sidecar accept the controller's
forwarded HTTP/HTTPS scheme while the public Service always discards a
client-supplied scheme. (The public Service declares one other port, `1455`,
which carries no application traffic — it exists so a port-forward can reach
the transient Codex OAuth callback listener; see
[Sign in with ChatGPT](#sign-in-with-chatgpt-codex-oauth).)

Under [`cremind.ssl`](#https-in-pod-tls) the same sidecar runs as a **layer-4
TCP passthrough relay** instead: no routes, no termination, bytes forwarded
untouched — so the app's TLS runs end-to-end *and* a `kubectl port-forward`
survives the app restarting underneath it.

## Install

```bash
# Dependencies (PostgreSQL + optional vector DBs) must be fetched first.
helm dependency build ./helm/cremind

# Install (recommended release name `cremind` so the bundled Postgres Service
# matches the wizard's pre-filled host):
helm install cremind ./helm/cremind --namespace cremind --create-namespace
```

Or from the published OCI registry:

```bash
helm install cremind oci://registry-1.docker.io/cremind/cremind \
  --version <X.Y.Z> --namespace cremind --create-namespace
```

Release candidates are published as pre-release chart versions
(`X.Y.Z-rc.N.dev.M`), which Helm ignores unless you pass `--devel` (or pin the
exact `--version`):

```bash
helm install cremind oci://registry-1.docker.io/cremind/cremind \
  --devel --namespace cremind --create-namespace
```

An RC install automatically reports the **test** channel in-app — the Updates
page offers the latest RC and a picker to switch between specific release
candidates. A stable install reports **production**. The channel is derived from
the image tag, so you never set it by hand (override only via
`cremind.extraEnv`).

Reach it with a single port-forward (or an Ingress hostname):

```bash
kubectl -n cremind port-forward svc/cremind 1515:80
# UI / wizard:   http://localhost:1515/#/setup
# agent desktop: http://localhost:1515/vnc/vnc.html   (desktop flavor only)
# (https://localhost:1515 with cremind.ssl=auto, or with after-setup once the
#  wizard has finished — see HTTPS below)
```

Follow the `NOTES` printed after install: open `/#/setup` and click through. With
the bundled PostgreSQL, the Database step is fully pre-filled — including the
password, which is auto-wired into the pod from the generated Secret (via
`secretKeyRef`) and used server-side, so you **leave the password blank and just
click Next** (the Docker-Compose-like "everything is wired" experience). You only
type credentials when using an external PostgreSQL without `cremind.postgresPasswordSecret`.

### Sign in with ChatGPT (Codex OAuth)

The OpenAI provider's **Sign in with ChatGPT** uses OpenAI's Codex OAuth client,
whose redirect URI is hard-coded to `http://localhost:1455/auth/callback` and
**cannot be changed** — not by this chart, not by an Ingress. The backend
therefore opens a short-lived listener on port `1455` inside the pod while a
sign-in is pending, and the browser has to be able to reach *that*.

`kubectl port-forward` dials the pod's loopback, so forwarding `1455` alongside
the UI port is all it takes:

```bash
kubectl -n cremind port-forward svc/cremind 1515:80 1455:1455
```

Then sign in from Settings → LLM Providers → OpenAI; the redirect is captured
automatically. Without the second port the sign-in page simply waits until it
times out.

**Reaching Cremind through an Ingress instead?** There is no port to forward, so
use either fallback — both complete the exchange server-side:

- Approve in the browser, copy the `http://localhost:1455/auth/callback?...` URL
  out of the address bar (the page itself won't load — only the URL matters), and
  paste it into the **"Having trouble? Paste the redirect URL"** box; or
- run `cremind llm codex-oauth login` on your own machine against the cluster —
  the CLI binds `1455` locally, catches the redirect there, and relays the code.

### Coding agents (Claude Code, Codex)

These are a separate sign-in from the LLM providers above: each one authenticates
through its own CLI, not through Cremind's provider credentials. Sign in from
**Settings → Tools & Skills → Coding Agents** — Codex shows a device code you
approve on any device (no port to forward), and Claude Code opens a terminal in
the browser that runs `claude auth login` inside the pod.

The chart points both CLIs at the system PVC (`CLAUDE_CONFIG_DIR` and
`CODEX_HOME` under `<cremind.systemDir>/coding-cli/`), so a sign-in survives pod
replacement and `helm upgrade`. Their own defaults would put it in the container
filesystem, where every rollout would silently sign the user out. Each profile
gets its own login under `<cremind.systemDir>/<profile>/coding-cli/`; a login
made in the pod's shell is the shared fallback for profiles that have none.

## HTTPS (in-pod TLS)

With a real domain, terminate TLS at the Ingress (`ingress.tls`) — that also
gets you HTTP/2, and a public CA means nobody sees a warning. This section is
for the other case: **no domain**, reached over `kubectl port-forward` or a
NodePort, where no public CA will ever issue a certificate.

```bash
helm install cremind oci://registry-1.docker.io/cremind/cremind \
  --version <X.Y.Z> --namespace cremind --create-namespace \
  --set cremind.ssl=true
```

HTTP is the default (`cremind.ssl=""`). Set `cremind.ssl=true` to opt into
the trust-first HTTPS flow (`after-setup`); `false` or the compatible string
`none` explicitly selects HTTP.
The empty default preserves legacy `CREMIND_SSL` entries in `extraEnv`; remove
a conflicting legacy entry before setting the boolean toggle.

Enabling `cremind.ssl` makes the server generate a local CA and sign its own certificate
under `/root/.cremind/tls/` at first boot, then serve HTTPS (and HTTP/2) on
1515 itself. `auto` does that from the very first byte; `after-setup` waits
until the Setup Wizard has finished (see [No-warning first
load](#no-warning-first-load) — it is the recommended opt-in for a new Kubernetes install). One
flag is enough either way — the chart adjusts everything that depends on it:

| | `cremind.ssl=""` (default) | `cremind.ssl=auto` | `cremind.ssl=after-setup` |
|---|---|---|---|
| Service port 80 targets | nginx sidecar (`http`) | nginx sidecar as L4 relay (`relay`), named `https` | nginx sidecar as L4 relay (`relay`), named `https` |
| nginx sidecar | runs as L7 proxy | runs as **L4 TCP relay** (passthrough) | runs as **L4 TCP relay**, in both phases |
| noVNC (desktop flavor) | `/vnc/vnc.html` on the same port | **Service port 6080**, `http://localhost:6080/vnc.html` | **Service port 6080**, `http://localhost:6080/vnc.html` |
| auto-derived `APP_URL` | `http://localhost:1515` | `https://localhost:1515` | `https://localhost:1515` (steady state) |
| probes (if enabled) | scheme HTTP | scheme HTTPS | **`tcpSocket`** (true in both phases) |
| browser scheme during setup | `http` | `https` (warns) | `http` |

The manifests describe the install `after-setup` *becomes*: `APP_URL`, the
Atlassian callback and the Service port name are all the https steady state
from the first render, because that is what an agent card, an OAuth redirect
and a CORS origin have to say for the whole life of the install bar the wizard.
Only the probes track the phase, because a probe that is wrong for five minutes
restarts the pod. The server logs which phase it booted in.

`cremind.ssl` and `ingress.enabled` are mutually exclusive and the chart
rejects the combination: an Ingress controller speaks plain HTTP to the backend
and cannot portably re-encrypt to a private CA. Pick one place to terminate.

### Switch an existing HTTP install to HTTPS

Open **Settings > Security** while still connected over HTTP. Prepare the
certificate, download the public CA, verify its displayed SHA-256 fingerprint,
and follow the trust instructions on every device that opens Cremind. Keep the
private CA key inside the system PVC. If you use a LAN hostname or IP, include
it in `cremind.sslAutoHosts` so the server certificate covers the address.

For an install reached through a port-forward or NodePort, update the Helm
release using its current chart version (substitute your release, namespace,
and version below). An already completed setup switches immediately with
`auto`, so finish certificate trust first:

```bash
helm list --namespace cremind
helm upgrade cremind oci://registry-1.docker.io/cremind/cremind \
  --version <current-chart-version> --namespace cremind --reuse-values \
  --set cremind.ssl=auto
kubectl --namespace cremind rollout status deployment/cremind --timeout=5m
kubectl --namespace cremind port-forward svc/cremind 1515:80 1455:1455 6080:6080
```

You should not have to substitute anything by hand. The chart tells the pod who
this release is — `CREMIND_K8S_NAMESPACE`, `CREMIND_K8S_RELEASE`,
`CREMIND_K8S_WORKLOAD` (the Deployment, the Service and the Ingress all carry
that one name) and `CREMIND_K8S_SERVICE_PORT`, all in the env ConfigMap — so
**Settings > Security** prints this runbook with your real names already filled
in, and the exported config file (Setup Wizard, or **Developer > Configuration
File** afterwards) carries them together with the `kubectl port-forward` command
to reconnect. A chart older than these keys still works: the pod reads its
namespace off the service-account mount and infers the workload from its pod
name, marks the answer as inferred, and leaves `<release>` for you to fill in
after `helm list --all-namespaces`.

Omit `6080:6080` for the basic image. If `cremind.appUrl` or
`cremind.atlassianRedirectUri` was explicitly set, pass its matching HTTPS URL
on the upgrade too. Remove any conflicting `CREMIND_SSL` entry in `extraEnv`.
Reopen the port-forward after rollout: a Helm upgrade replaces the pod, which
closes a tunnel to the old pod. Then open `https://localhost:1515` (or the same
LAN address/port with `https://`). Cremind tabs waiting for the switch resume
when the HTTPS address is reachable and trusted; a browser suspended during
the change may need to be brought to the foreground.

Use Helm for this change: it updates the HTTP proxy to the TCP relay, Service
routing, probes, URLs, and noVNC port together. Changing only `CREMIND_SSL`
inside the container leaves the HTTP proxy and probes speaking the wrong
protocol. Keep `proxy.enabled=true` so later app-only restarts preserve the
port-forward.

For **Ingress** deployments, leave in-pod TLS disabled. The chart marks this as
edge-managed TLS, so **Settings > Security** does not create or ask users to
trust a private Cremind CA. Create a certificate Secret whose SAN covers the
Ingress hostname, then update the hostname, Secret, public URL, proxy, Service,
and probes in one Helm release:

```bash
kubectl --namespace cremind create secret tls cremind-tls \
  --cert=/path/to/fullchain.pem --key=/path/to/privkey.pem
helm upgrade cremind oci://registry-1.docker.io/cremind/cremind \
  --version <current-chart-version> --namespace cremind --reuse-values \
  --set ingress.enabled=true \
  --set ingress.host=cremind.example.com \
  --set 'ingress.tls[0].hosts[0]=cremind.example.com' \
  --set ingress.tls[0].secretName=cremind-tls \
  --set-string ingress.trustedProxyCidrs=<controller-source-cidr> \
  --set cremind.appUrl=https://cremind.example.com
kubectl --namespace cremind rollout status deployment/cremind --timeout=5m
kubectl --namespace cremind get ingress cremind
curl --fail https://cremind.example.com/api/tls/status
```

Two distinct moments, easily confused. Recording the switch (the Settings page,
or `cremind tls enable`) changes nothing about how the pod serves: HTTP keeps
working, existing sessions keep working, and `cremind tls cancel` still calls the
whole thing off — which is what makes it safe to record the switch first and
apply the chart change afterwards, or never. Only once a listener actually
answers HTTPS does the boundary move.

After that, `http://cremind.example.com/<old-route>` should return the uncached
recovery document, and an HTTP API request should return status 426. If either
is redirected by the controller, disable its redirect/HSTS setting before
relying on old bookmark recovery.

Do not roll the Cremind image back while a switch is waiting: an older build
completes it without moving the token epoch, leaving HTTP-era credentials valid
on the secure origin. Cancel first, then downgrade.

Replace the release, namespace, host, Secret, chart source, and version with
the existing release's values. If cert-manager owns the Secret, keep its
annotations and issuer workflow instead of creating the Secret by hand. Remove
any legacy `CREMIND_SSL` entry from `cremind.extraEnv`; `cremind.ssl` must stay
empty or `false` because the Ingress and in-pod TLS remain mutually exclusive.

Do **not** enable a permanent HTTP-to-HTTPS redirect or HSTS while HTTP
recovery is enabled. After activation, Cremind uses HTTP document navigation
as a small, uncached recovery page for old bookmarks and suspended tabs, while
refusing plaintext API calls. The chart sets nginx-ingress's `ssl-redirect` and
`force-ssl-redirect` annotations to `"false"` when TLS is present unless you
explicitly supply either annotation. For another Ingress controller, configure
its equivalent policy so ports 80 and 443 both reach Cremind. Keep the HTTP
route available after activation; the recovery page moves the browser session
to HTTPS without carrying credentials in the redirect. The sidecar also sends
plaintext `/vnc/` navigation through that recovery path and refuses the
plaintext noVNC WebSocket, so the desktop cannot bypass the HTTPS boundary.

The default sidecar uses separate public and edge listeners. Direct Service,
NodePort, LoadBalancer, and port-forward clients cannot forge
`X-Forwarded-Proto: https`; nginx overwrites it with the actual plaintext
scheme. Only the private Ingress backend listener accepts the controller's
exact `http` or `https` value. It also preserves the full public Host header,
including a custom port such as `:8443`, so APP_URL, CORS and handoff origins
continue to match.

A ClusterIP is reachable by workloads inside the cluster, so Ingress TLS
requires `ingress.trustedProxyCidrs` even with the sidecar. Set it to the narrow
source IPs or CIDRs used by the Ingress controller. nginx rejects other peers
on the edge listener and restores the original client address only through that
trusted chain. Wildcards, the whole cluster pod range, and `/0` networks defeat
that boundary and must not be used.

If you set `proxy.enabled=false`, that listener boundary no longer exists.
Set `ingress.trustedProxyCidrs` to a narrow, comma-separated list of the Ingress
controller's source IPs/CIDRs so Uvicorn can accept its forwarding headers. The
chart rejects wildcard and `/0` trust. Prefer the default sidecar when controller
addresses are dynamic.

If the rollout or Ingress is unavailable, inspect it with:

```bash
kubectl --namespace cremind describe ingress cremind
kubectl --namespace cremind get pods
kubectl --namespace cremind logs deployment/cremind -c cremind --tail=200
```

If you were also using a port-forward for setup or recovery, a Helm upgrade can
replace its pod. Reopen it after the rollout with `kubectl --namespace cremind
port-forward svc/cremind 1515:80`, then use the public HTTPS hostname once the
Ingress is healthy. The Security page keeps its verification step visible until
the HTTPS status response matches this installation and transition.

### No-warning first load

With `auto`, the very first page anyone opens — the Setup Wizard — is already
behind a certificate no device trusts yet, so the first thing Cremind shows a
new user is `ERR_CERT_AUTHORITY_INVALID`. `after-setup` removes that entirely:

1. The pod comes up serving **plain HTTP** on 1515 (the CA and certificate are
   generated anyway, at that first boot). Port-forward and open the wizard —
   no warning, because there is no TLS yet.
2. The wizard hands you the CA and walks you through trusting it, while
   nothing is behind a certificate.
3. Its last step restarts the server. Kubelet brings the pod back serving
   https on the same port, to a browser that now trusts the chain.

Step 3 used to cost you the tunnel. `kubectl` ≥ 1.23 ends a port-forward the
first time a connection into the pod is **refused**, so the moment the browser
polled during the restart the whole tunnel died with `lost connection to pod`.
With the default `proxy.enabled=true` that no longer happens: the tunnel now
lands on the relay sidecar, which stays up while the app is down and answers
every dial (accepting and closing a connection is not refusing one). The wizard
polls, the pod comes back, and the page continues to `https://localhost:1515`
by itself — nothing to re-run. The same protection covers the restarts that
in-app upgrades perform later.

With `proxy.enabled=false` there is no relay and the old behaviour stands:
re-run the same port-forward command when the wizard says it is waiting, and it
continues on its own the moment the tunnel is back. Either way, a self-healing
forward is worth having, since `helm upgrade`, laptop sleep and network blips
all end tunnels too:

```bash
while true; do kubectl -n cremind port-forward svc/cremind 1515:80; sleep 1; done
```
```powershell
while ($true) { kubectl -n cremind port-forward svc/cremind 1515:80; Start-Sleep -Seconds 1 }
```

**Use `after-setup` on Kubernetes unless you have a reason not to.** `auto`
remains the right pick for a headless or API-only install, where no browser is
involved and TLS from the first byte matters more than the interstitial: a
client that talks to Cremind over the API can be handed the CA out of band and
should never see a plaintext window at all.

### Trust the CA (one-time per device)

Under `after-setup` the wizard walks you through this at the right moment and
you can skip ahead — the `kubectl` extraction below is still the authenticated
reference to verify its copy against. Under `auto` it is the first thing to do,
before the interstitial.

Until the generated CA is in the device's trust store, browsers show
`ERR_CERT_AUTHORITY_INVALID` / "Your connection is not private". No server can
avoid that on its own — a certificate is trusted because it chains to a root the
*device* already trusts. The CA exists precisely so this is a **one-off**: it
lives on the `system` PVC, so it survives pod restarts and reschedules, and
re-issued server certificates stay trusted under it.

Copy it out of the pod:

```bash
kubectl -n cremind exec deploy/cremind -c cremind -- \
  cat /root/.cremind/tls/ca.pem > cremind-ca.pem
```

In PowerShell use `| Out-File -Encoding ascii cremind-ca.pem` instead of `>` —
PowerShell's redirection writes UTF-16 with a BOM and `certutil` rejects it.
You can also download `https://localhost:1515/ca.pem` (under `auto`, by
clicking through the warning once) — then compare
`cremind tls fingerprint --file <download>` against the `kubectl` copy before
trusting it. The `kubectl` route is already authenticated, which is why it
comes first.

Then install it, either with Cremind's helper (`cremind tls trust --print-only`
shows the command it would run):

```bash
cremind tls trust --file cremind-ca.pem
```

or by hand:

| OS | Command |
|---|---|
| Windows | `certutil -addstore -user Root cremind-ca.pem` |
| macOS | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain cremind-ca.pem` |
| Debian/Ubuntu | `sudo cp cremind-ca.pem /usr/local/share/ca-certificates/cremind-local-ca.crt && sudo update-ca-certificates` |
| RHEL/Fedora | `sudo cp cremind-ca.pem /etc/pki/ca-trust/source/anchors/cremind-local-ca.crt && sudo update-ca-trust extract` |

If Firefox still warns after OS trust, import the same CA under Settings →
Privacy & Security → Certificates → View Certificates → Authorities. Some
Linux packages use a separate NSS store.

### Reaching the pod by another name

The certificate covers `localhost`, the pod's hostname and its IPs. If you
reach Cremind as something else (a LAN name in front of a NodePort, say), add
it or the browser reports a name mismatch even after trusting the CA:

```bash
helm upgrade cremind <chart> --reuse-values \
  --set-string 'cremind.sslAutoHosts=cremind.lan\,10.0.0.5'
```

Quote the whole argument: helm splits its own value on unescaped commas, and an
unquoted `\` is removed by the shell before helm ever sees it. (`--set-string`
additionally keeps an all-numeric name from being coerced to a number.)
Changing this reissues only the server certificate on next boot; the CA you
trusted is unaffected.

### Upgrading from a pre-`cremind.ssl` install

Releases that turned this on through `cremind.extraEnv` keep working — the
chart reads `CREMIND_SSL` from there when `cremind.ssl` is unset, and derives
the same scheme-correct URLs. That spelling accepts `after-setup` too, though
there is no reason to prefer it. Move to the first-class knob when convenient;
setting both to *different* values fails the render on purpose.

## Bundled dependencies

| Subchart   | Default  | Service name (pinned) | Purpose                    |
|------------|----------|-----------------------|----------------------------|
| postgresql | enabled  | `cremind-postgresql`  | Shared application state    |
| qdrant     | disabled | `cremind-qdrant`      | Vector store (embeddings)  |
| chromadb   | disabled | `cremind-chromadb`    | Vector store (embeddings)  |

`fullnameOverride` pins each Service name so it is independent of the Helm
release name and matches the wizard's pre-filled hosts. Enable a vector DB with
`--set qdrant.enabled=true` (or `chromadb.enabled=true`) when turning on
embeddings.

> **Dependency availability (important):** As of August 2025 Bitnami moved its
> free Docker Hub images to the frozen `bitnamilegacy` namespace, so the chart's
> default container image — e.g. `docker.io/bitnami/postgresql:17.6.0-debian-12-r4`
> — now fails to pull (`ImagePullBackOff: not found`), even though
> `helm dependency build` resolves the chart fine. You have three options:
>
> 1. **External / managed PostgreSQL (recommended for production):**
>    `--set postgresql.enabled=false` and enter the endpoint in the wizard.
> 2. **Frozen legacy image (quick, for evaluation):** point the bundled
>    PostgreSQL at the legacy namespace. Verified working on k8s 1.32:
>    ```bash
>    helm install cremind ./helm/cremind -n cremind --create-namespace \
>      --set postgresql.image.registry=docker.io \
>      --set postgresql.image.repository=bitnamilegacy/postgresql
>    ```
>    These two `--set` overrides are sufficient. You do **not** need
>    `--set global.security.allowInsecureImages=true`: the Bitnami image-
>    verification guard is only invoked from the PostgreSQL subchart's
>    `NOTES.txt`, which Helm does not render for a dependency, and the
>    `bitnamilegacy/*` namespace is special-cased to a warning rather than a
>    hard `fail` regardless. Passing `allowInsecureImages=true` is harmless
>    (it only silences that substitution warning if it ever surfaces) but is
>    not required — verified against the bundled `postgresql-16.7.27` subchart.
> 3. **Vendor offline:** commit a pinned `postgresql-*.tgz` (with a pullable
>    image) into `charts/` so resolution and pulls are self-contained.

> **Reinstalling? Delete the Postgres data PVC first.** The bundled PostgreSQL
> is a StatefulSet, so its data volume (`data-<release>-postgresql-0`) is
> **retained** across `helm uninstall` — but a fresh `helm install` generates a
> **new** random password into the Secret. PostgreSQL only applies a password on
> *first* init, so the reused volume keeps the *old* password and setup fails
> with `password authentication failed for user "cremind"`. Before reinstalling,
> either delete the stale PVC for a clean database
> (`kubectl -n <ns> delete pvc data-<release>-postgresql-0`) or pin a stable
> password you reuse every time (`--set postgresql.auth.password=…`). The same
> applies to managed/external PostgreSQL: the wizard password must match the DB.
> To instead have `helm uninstall` delete this volume automatically (so a
> reinstall always starts clean), see [Uninstalling and removing data](#uninstalling-and-removing-data).

## Key values

| Key | Default | Notes |
|-----|---------|-------|
| `desktop.enabled` | `true` | `true` → `cremind/cremind-desktop` (VNC desktop); `false` → `cremind/cremind` (headless basic image, drops the noVNC routes/ports/env). |
| `replicaCount` | `1` | **Fixed at 1.** The chart rejects any other value (single-instance state; VNC = single desktop). |
| `resources.requests` | `2` CPU, `2Gi` | Minimum guaranteed for the cremind container; the node must have it free. The basic flavor needs less. |
| `image.repository` | `""` → auto | Auto-selected from `desktop.enabled`. Set to override with a specific repo. |
| `image.tag` | `""` → `appVersion` | The matching image tag (both flavors share it). |
| _(release channel)_ | auto from `image.tag` | Not a knob. `test` when the effective tag is an RC (`…rcN.devM`, i.e. the `--devel` chart), else `production`; the in-app **Updates** page reports this. Force it via `cremind.extraEnv` (`CREMIND_UPGRADE_CHANNEL`). |
| `cremind.installMode` | `kubernetes` | Drives external-only service modes. |
| `cremind.setupWizardEnv` | `kubernetes` | Pre-fills the wizard. |
| `cremind.appUrl` | `""` → auto | A2A card URL; auto-derives the Ingress URL or `http(s)://localhost:1515`. |
| `cremind.ssl` | `""` | HTTP by default; boolean `false` or string `none` explicitly disables in-pod TLS, while boolean `true` selects `after-setup`. `auto` = in-pod HTTPS with a generated local CA from the first boot; `after-setup` = the same, but plain HTTP until the Setup Wizard finishes so the CA is trusted before any https page loads (recommended when a browser is involved). Both switch the sidecar to an L4 passthrough relay and reject `ingress.enabled`. See [HTTPS](#https-in-pod-tls). |
| `cremind.sslAutoHosts` | `""` | Extra SANs (CSV) for the generated certificate, for names beyond localhost/pod. |
| `persistence.system.*` | `5Gi`, RWO | `bootstrap.toml`, tokens, profiles. |
| `persistence.venv.*` | `8Gi`, RWO | Wizard-installed Python deps (LLM SDKs, embeddings). |
| `persistence.work.*` | `10Gi`, RWO | Agent working dir (files it creates); `mountPath` must match the wizard's User Working Directory. |
| `extraVolumes` / `extraVolumeMounts` | `[]` | Persist any additional paths (raw volume specs). |
| `postgresql.enabled` | `true` | Bundled Bitnami PostgreSQL. |
| `qdrant.enabled` / `chromadb.enabled` | `false` | Enable when turning on embeddings. |
| `proxy.enabled` | `true` | nginx sidecar. Without `cremind.ssl` it is the single-entry L7 proxy (UI + API + noVNC on one port; noVNC routes only on the desktop flavor). With `cremind.ssl` it is an L4 TCP passthrough relay that keeps a port-forward alive across app restarts, and noVNC moves to Service port 6080. `false` removes it and points the Service at the app. |
| `proxy.edgePort` | `8082` | Pod-private nginx listener referenced by the Ingress backend Service. It separates public Service traffic from the controller's forwarded scheme and must differ from `proxy.port`. |
| `proxy.adminPort` | `8081` | Relay mode only: pod-internal port carrying the sidecar's own `/healthz` for its probes. Never on the Service. |
| `service.port` | `80` | The one Service port (fronts the proxy). |
| `cremind.codexCallbackPort` | `1455` | Codex OAuth callback. Not really a knob — OpenAI hard-codes `localhost:1455`. Exposed as a second Service port purely so `port-forward svc/… 1455:1455` resolves; see [Sign in with ChatGPT](#sign-in-with-chatgpt-codex-oauth). |
| `ingress.enabled` | `false` | One hostname for everything (UI at `/`; noVNC at `/vnc/` on the desktop flavor). Sets `CREMIND_TLS_TERMINATION=edge`; mutually exclusive with in-pod `cremind.ssl`. |
| `ingress.tls` | `[]` | Edge certificate hosts and Secret. When nonempty, nginx-ingress redirects default to `"false"` so Cremind's restricted HTTP recovery page remains reachable; explicit annotations win. Other controllers need the equivalent dual HTTP/HTTPS policy. |
| `ingress.trustedProxyCidrs` | `""` | Exact controller source IPs/CIDRs. Required when Ingress TLS is enabled; blocks other cluster peers from the edge listener and safely restores the original client IP. With `proxy.enabled=false`, it is passed to Uvicorn. Wildcards and `/0` networks are rejected. |

## How the storage constraints are enforced

The chart only sets `INSTALL_MODE=kubernetes`; the backend does the rest
(defense in depth, so a hand-crafted API call can't bypass the UI):

- The `kubernetes` install-mode rule restricts every backing service's
  deployment-mode picker to **External**, dropping SQLite and ChromaDB's
  pod-local *persistent* mode.
- The server rejects `db_provider=sqlite` at the wizard write path and at the
  database factory; rejects non-external vector stores at the embedding-config
  write path and at the vector-store factory; and refuses the SQLite default in
  `cremind db upgrade`.

## Single instance — scaling is not supported

Cremind runs as **exactly one pod, by design**. It owns shared state — RWO PVCs
that attach to one node at a time, one agent against the shared database, one
runtime venv — so two pods would fight over it, not scale it. On the desktop
flavor each pod additionally bundles its own VNC virtual desktop (XFCE +
Chrome), so a second pod would also mean a second divergent desktop. Therefore:

- `replicaCount` is **fixed at 1** and the chart **fails to render** if you set
  anything else.
- There is **no HorizontalPodAutoscaler** and no `autoscaling` values.
- The Deployment uses the `Recreate` strategy, so even an upgrade never runs two
  pods at once.
- Do **not** `kubectl scale` the Deployment — it would break the single-instance
  model. (A `helm upgrade` resets it to 1.)

State survives a pod reschedule without scaling: PostgreSQL holds the dynamic
config (JWT signing secret, LLM keys, tool configs, profiles) and three PVCs hold
the rest — `system` (`/root/.cremind`: `bootstrap.toml`, OAuth tokens, per-profile
files), `venv` (`/opt/cremind/venv`: installed deps), and `work`
(`/root/Documents`: the files the agent creates). So a restarted/rescheduled
single pod boots straight through with no re-setup and no lost files. Only these
mounted paths persist — data written elsewhere (e.g. `/tmp`, or a manual
`kubectl exec` into `/root`) is ephemeral; add `extraVolumes`/`extraVolumeMounts`
to persist additional paths.

To serve more load, give the single pod more resources (`resources`) and a
bigger node; do not add replicas.

## Uninstalling and removing data

`helm uninstall <release> -n <ns>` deletes the Deployment, Service, ConfigMaps,
the generated Secret, and the chart's own three PVCs (`<release>-system`,
`-venv`, `-work`). Whether each PVC's underlying **PV and disk** also disappear
is governed by the StorageClass `reclaimPolicy`: `Delete` (the default on most
cloud provisioners) destroys the disk; `Retain` leaves a `Released` PV behind.

The bundled **StatefulSet** subcharts are the exception — their data PVCs come
from `volumeClaimTemplates`, which neither Helm nor Kubernetes garbage-collects
on uninstall, and they don't carry the release-wide labels. So they **survive**
by default, and a blanket `kubectl delete pvc -l app.kubernetes.io/instance=<release>`
would *miss* them. Handle each one:

- **PostgreSQL (on by default)** — opt into automatic deletion by enabling the
  StatefulSet PVC retention policy at install/upgrade:
  ```bash
  helm install cremind ./helm/cremind -n cremind --create-namespace \
    --set postgresql.primary.persistentVolumeClaimRetentionPolicy.enabled=true \
    --set postgresql.primary.persistentVolumeClaimRetentionPolicy.whenDeleted=Delete
  ```
  (Also documented under `postgresql:` in `values.yaml` — set it there instead
  if you prefer a values file.) `helm uninstall` then removes
  `data-<release>-postgresql-0` along with everything else. Requires the
  `StatefulSetAutoDeletePVC` feature — GA in k8s 1.32, beta on-by-default since
  1.27.
- **ChromaDB** (if enabled) — already deletes its PVC on uninstall by default
  (`chromadb.data.retentionPolicyOnDelete: Delete`); set it to `Retain` to keep.
- **Qdrant** (if enabled) — its subchart exposes **no** retention-policy value,
  so its PVC always survives. Delete it by name afterwards (the Service name is
  pinned to `cremind-qdrant`, single replica):
  ```bash
  kubectl -n <ns> delete pvc qdrant-storage-cremind-qdrant-0
  ```

True deletion is two steps: `whenDeleted: Delete` removes the **PVC**, then the
bound **PV + disk** follow the StorageClass `reclaimPolicy` — so pair the flag
with a `Delete`-reclaim StorageClass for a guaranteed full wipe. Finally,
`helm uninstall` does **not** remove the namespace (or anything
`--create-namespace` created); delete it separately if you want it gone.
