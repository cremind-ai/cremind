---
description: "Enable HTTPS after the default HTTP installation and fix ERR_CERT_AUTHORITY_INVALID or connection is not private by trusting ca.pem for CREMIND_SSL=auto, CREMIND_SSL=true or CREMIND_SSL=after-setup. cremind tls status reports transport and deployment instructions, and answers without a token, but names the real Kubernetes namespace, release and Deployment only for the admin profile; prepare generates or validates a certificate; enable persists and activates HTTPS; cancel stops a switch at any point before HTTPS actually serves, and cancel --local does it offline when the server will not start. The HTTP application keeps working until the deployment change lands, so an outstanding switch never locks anyone out. Administrative mutations require an admin token. Trust, export and fingerprint operate locally without a token to install the Cremind CA in the device trust store. Covers native, Electron, Docker and Kubernetes with persistent certificate and browser-session migration."
---

# `cremind tls` — Enable HTTPS and trust the local certificate authority

When Cremind serves HTTPS with `CREMIND_SSL=auto` or `CREMIND_SSL=after-setup`,
it generates its own certificate authority in `<CREMIND_SYSTEM_DIR>/tls/` and
signs the server certificate with it. Browsers reject that chain until the CA
is installed in the **device's** trust store, which is what the warning page
means:

> Your connection is not private — `ERR_CERT_AUTHORITY_INVALID`
> Issuer: Cremind Local CA

No server setting can remove that warning. A certificate is trusted because it
chains to a root the device already holds, so one manual install per device is
unavoidable. The CA exists so it is a **one-off**: server certificates get
reissued (on expiry, or when a hostname is added) and stay trusted underneath
it, and on Kubernetes and Docker the CA lives on a persistent volume, so it
survives restarts.

With `after-setup` you should never see the warning at all: the server stays on
plain HTTP while the Setup Wizard runs, the wizard walks you through trusting
the CA there, and only then does it restart into HTTPS — to a browser that
already trusts the chain. On a **native** install the wizard's "Secure this
install" step does it in one click ("Trust it on this device" — the server and
the browser share the machine, so the server hands the CA to the OS itself),
and a **Docker** install's host is offered the same thing by the installer
right after the container starts. Skipping that step is not permanent — the
admin profile can come back to it any time under **Settings → HTTPS &
Certificate**, which offers the same one-click trust, the same fingerprint,
and the same CA download. These commands are how you do the same thing by
hand, on another device, or after the fact.

The certificate commands (`trust`, `export`, `fingerprint`) run entirely on the local machine — they read a file and hand it
to the operating system. They never call the Cremind API and need no token,
because the whole point is the moment when nothing can talk to the server yet.

## Enable HTTPS after installing over HTTP

The administrative commands are `cremind tls prepare`, `cremind tls enable` and
`cremind tls cancel`. Select the admin profile with the root `--profile admin` flag.
The enable command accepts `--yes` and `--restart/--no-restart`; cancel accepts
`--local`.

New installations use HTTP unless HTTPS was explicitly selected. HTTPS encrypts
sessions and application data in transit and enables browser secure-context APIs.

1. Run `cremind --profile admin tls prepare` against the running server.
2. For a generated certificate, run `cremind tls fingerprint` and verify the CA
   fingerprint. Trust the CA on every device with `cremind tls trust`, or export
   it for another device. Supplied certificates and Ingress certificates use
   their issuer's trust instructions; they do not use a Cremind CA.
3. Save your work, then run `cremind --profile admin tls enable --yes`.
   The command first waits for every registered browser tab or Electron window
   to finish current uploads and save its own private session handoff; the old
   HTTP server and token epoch remain usable until that barrier completes.
   A supervised native server restarts automatically. `--no-restart` saves and
   announces the switch for a later manual restart. Electron restarts through the
   desktop app. Docker and Kubernetes print host/Helm instructions instead of
   changing deployment-owned configuration.
4. Open or suspended Cremind tabs that joined the preparation retain their
   route and session when they move to HTTPS, including after the pod or
   container is replaced, as long as the Cremind CA is unchanged. An old HTTP
   bookmark, or a fully discarded tab that had no private handoff, opens a
   credential-free recovery page and then the HTTPS login with its intended
   route retained. Untrusted certificates or a disconnected port-forward leave
   retry instructions.

Docker, Kubernetes and reverse-proxy installs stay on HTTP until you apply the
deployment change, and that wait is safe: the HTTP application keeps serving,
existing sessions keep working, and `cremind tls cancel` still calls the whole
thing off. `cremind tls status` says `awaiting_operator` while that is the case.
Nothing is invalidated until a listener genuinely answers HTTPS.

The token transport epoch changes at that moment, not when you run `enable`.
On-host token files are reissued without extending their expiry, and browser
handoffs receive matching HTTPS tokens. If a shell exported the old token in
`CREMIND_TOKEN`, unset it and let the CLI read the reissued profile token (or
export the new canonical value) before the next authenticated command.

Do not roll a Cremind release back while a switch is waiting: an older build
completes it without moving the token epoch, which would leave credentials from
the HTTP era valid on the secure origin. Cancel first, then downgrade.

Native activation also changes an unset, bundled-default, or matching HTTP
`CREMIND_ATLASSIAN_REDIRECT_URI` to its HTTPS callback. The activation output
prints the exact URI; add it to the allowed redirect URI in the Atlassian
developer console before linking Jira or Confluence. An unrelated custom fixed
callback is preserved.

`cremind tls status` reports the current transport, target address, CA fingerprint
and installation-specific instructions without requiring a token — but run it as
`cremind --profile admin tls status` on Kubernetes, or the commands come back
with `<namespace>` and `<release>` still in them.
`cremind --profile admin tls prepare --source-origin http://host:1515` overrides
which browser origin the local CLI prepares (the internal CLI port stays HTTP).
`cremind --profile admin tls cancel` cancels a switch at any point before HTTPS
actually serves — including one already waiting for a deployment change — and
restores the settings a native activation wrote. It does not disable an already
active HTTPS server.

For Ingress TLS, `CREMIND_TLS_TERMINATION=edge` identifies certificate ownership
before HTTPS is enabled. Update `ingress.tls` in Helm values and the public HTTPS
`APP_URL`; keep in-pod SSL disabled. Apply the chart's proxy, service and probe
changes together. Keep HTTP document requests reaching Cremind's recovery page
instead of forcing an immediate redirect that loses old-origin browser storage.
The status output lists the Secret, Helm upgrade, rollout and verification
commands (`kubectl get ingress`, `curl --fail https://<host>/api/tls/status`) in
the order to run them; no port-forward is involved, because the public hostname
is the HTTPS address. For in-pod Kubernetes TLS, set `cremind.ssl=auto` instead
and retain the system PVC.

## Global flags

`cremind tls` accepts the root-level `--json` flag. No `CREMIND_TOKEN` and no
profile are needed for status, trust, export or fingerprint — though on
Kubernetes `status` fills the real cluster names into its commands only for the
admin profile (see below). Prepare, enable and cancel require the admin profile.

## Subcommands

### `cremind tls status`

```bash
cremind tls status
cremind --profile admin tls status
```

Reports the current public transport, HTTPS target, deployment manager,
certificate type and SHA-256 fingerprint, restart support, transition phase,
and the exact native, Docker, Kubernetes, or Ingress commands needed next.
This read-only command answers without a token — it has to, because the
plaintext recovery page and the pre-setup wizard poll the same endpoint before
anyone can sign in — but an unauthenticated caller gets a *narrower* answer:
no `kubernetes` block, and a Kubernetes runbook that still has `<namespace>`
and `<release>` in it. Add `--profile admin` to get the filled-in one. A
signed-in non-admin profile is treated the same as an anonymous caller here.
The CLI still reaches its loopback management listener over HTTP even when the
public app uses HTTPS.

The deployment steps print in the order to follow them, grouped as **Before you
start**, **Run in order** and **What to expect** when the list is long enough to
need the separation. Plain lines say what to edit or what to expect; lines
indented by four spaces are the exact commands to run.

Anything the server can know is already filled in, including the chart
reference (`oci://registry-1.docker.io/cremind/cremind`) and the `--version`
pin, which carries the chart version matching the running build — keep that pin,
because without it Helm resolves whatever the registry calls latest, skipping
pre-release charts entirely.

On Kubernetes the release name, namespace and Deployment/Service are filled in
too — **for the admin profile** — whenever the chart states them
(`CREMIND_K8S_NAMESPACE`, `_RELEASE`, `_WORKLOAD` — see `cremind server
environment`, whose `kubernetes.source` row says `chart` in that case). The
commands then read `helm upgrade cremind … --namespace lee-cremind`, `kubectl …
rollout status deployment/cremind` and `port-forward svc/cremind 1515:80` with
no placeholder left in them, **there is no `helm list` step at all**, and only
the certificate paths and values file on the Ingress runbook are yours to fill
in.

Each Helm upgrade also sets `cremind.appUrl` to the HTTPS form of the browser's
address, port included (`https://<public-host>` when an Ingress page was opened
through a tunnel), since `--reuse-values` would keep an `http://` one the chart
refuses. An `APP_URL` in `cremind.extraEnv` overrides it; remove that entry.

Those names are cluster facts that `cremind server environment` and the install
secrets keep admin-only, so without an admin token the `kubernetes` block is
`null` and the commands keep their placeholders. The web UI's **Settings →
HTTPS & Certificate** page sends the admin session token and shows the
filled-in runbook.

An **older chart** states nothing, so the pod can only read its namespace and
its own Deployment name off itself and `<release>` stays a placeholder. Then —
and only then, or when the caller is not the admin — `helm list
--all-namespaces` is the runbook's first command, and the note above the list
tells you which placeholders to substitute from what it prints. A partly
inferred identity (`kubernetes.source: inferred`) keeps that first command for
the same reason: the Deployment name below it came from the pod's hostname
rather than from the chart, and `helm list` is what confirms which release owns
it.
With `--json` the same list is the `steps` array of
`{"kind": "note" | "command", "text": ...}` objects; the flat `instructions`
array beside it is the same text, kept for older clients. On a server already
serving HTTPS the list is empty unless the certificate needs replacing, in which
case it holds only the restart to run once the new certificate is in place.

Three fields describe an outstanding switch: `transition.awaiting_operator` is
true while it waits for a deployment change (so the HTTP application is still
serving and nothing has been invalidated), `can_cancel` says whether calling it
off is still possible, and `activation_error` reports the rare case where HTTPS
came up but the on-host token files could not be re-signed.

### `cremind tls prepare`

```bash
cremind --profile admin tls prepare [--source-origin URL]
```

Generates or validates the certificate and records an authenticated transition
while the HTTP application remains available. `--source-origin` is the public
HTTP origin to move, such as `http://cremind.lan:1515`; omit it when the
configured public origin is correct. The source hostname must be covered by a
generated or supplied certificate before activation can succeed.

### `cremind tls enable`

```bash
cremind --profile admin tls enable [--yes|-y] [--restart|--no-restart]
```

Activates the transition prepared by `cremind tls prepare`. It verifies the
recorded certificate fingerprint, waits for registered tabs and windows to
finish uploads and save handoffs, persists managed native settings, and then
restarts when supervision is available. `--yes` skips the trust-and-save
confirmation. `--restart` is the default; `--no-restart` persists the change
and prints the exact manual restart command. Docker and Kubernetes remain
deployment-managed and print recreation or Helm rollout commands instead.

When a supervised restart cannot be scheduled, the output prints the failure and
then `cremind server restart --yes` as a command step of its own, so the recovery
line can be copied and run without editing the sentence around it.

### `cremind tls cancel`

```bash
cremind --profile admin tls cancel
cremind tls cancel --local
```

| Flag | Default | Meaning |
|---|---|---|
| `--local` | off | Cancel in the system directory directly, without a running server. Needs no token. |

Cancels a transition and tells participating tabs to release their upload gates.
It works while the switch is prepared or quiescing, and also after `enable` for
as long as HTTPS has not actually started serving — the window in which a
Docker, Kubernetes or reverse-proxy install waits for its deployment change, and
in which nothing has been invalidated yet. On a native or Electron install it
also restores the `.env` and credentials that activation rewrote.

It is refused once a listener answers HTTPS, and while a supervised restart into
HTTPS is already armed (a restart that never lands releases the block after
90 seconds). It does not turn an already active HTTPS server back to HTTP.

`--local` is the way back when activation persisted and the restart into HTTPS
then failed, leaving no server to ask: it applies the same rollback offline and
marks the switch cancelled. It refuses when HTTPS has already served, because at
that point sessions are bound to the new transport and only a deployment change
can move them back.

### `cremind tls trust`

Install the CA into this device's trust store.

```bash
cremind tls trust [--file PATH] [--print-only] [--yes]
```

| Flag | Default | Meaning |
|---|---|---|
| `--file PATH` | `<CREMIND_SYSTEM_DIR>/tls/ca.pem` | The CA to install — use this for a CA downloaded from a server running elsewhere. |
| `--print-only` | off | Print the command(s) for this OS and exit without running anything. |
| `--yes`, `-y` | off | Skip the confirmation prompt. Required with `--json`. |

It prints the subject and SHA-256 fingerprint and asks for confirmation, then
runs the right tool for the platform:

| OS | What it runs |
|---|---|
| Windows | `certutil -addstore -user Root <ca>` (per-user store; Windows shows its own confirmation dialog) |
| macOS | `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain <ca>` |
| Debian/Ubuntu | `sudo cp <ca> /usr/local/share/ca-certificates/cremind-local-ca.crt` then `sudo update-ca-certificates` |
| RHEL/Fedora | `sudo cp <ca> /etc/pki/ca-trust/source/anchors/cremind-local-ca.crt` then `sudo update-ca-trust extract` |

The `sudo` prefix is dropped when already running as root. Every tool is
checked for on `PATH` before anything runs, and if a command fails (no `sudo`,
no permission) it prints the exact command to run by hand and exits 1.

On Windows and macOS that is a single command, so a failure leaves nothing
behind. On Linux it is two — copy the anchor, then rehash the store — and if
the copy succeeded the output says so explicitly. That state is not yet
trusted, but the anchor is where the next `update-ca-certificates` /
`update-ca-trust` run would pick it up, so either finish with the printed
command or remove the anchor file.

It refuses to install a file that is not a CA certificate, so pointing `--file`
at the server certificate (`cert.pem`) by mistake is caught rather than
trusting a leaf as a root.

### `cremind tls export`

Copy the CA out, to carry to another device.

```bash
cremind tls export [--out PATH] [--file PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--out PATH`, `-o` | `cremind-local-ca.pem` | Destination. `-` writes the PEM to stdout. |
| `--file PATH` | `<CREMIND_SYSTEM_DIR>/tls/ca.pem` | Which CA to export. |

It writes bytes rather than text on purpose: redirecting text output in
PowerShell re-encodes it to UTF-16, and `certutil` then rejects the file.

### `cremind tls fingerprint`

```bash
cremind tls fingerprint [--file PATH]
```

Prints the subject, the SHA-256 fingerprint in colon-separated hex (the format
browser certificate viewers show, so the two can be compared directly), the
expiry date, and the path. `--json` returns the same fields plus
`not_valid_after` in ISO-8601.

## Getting the CA off a server running somewhere else

The commands above default to a CA on the local filesystem. When Cremind runs
in a container or a cluster, fetch it first:

```bash
# From the running server, in a browser or with curl. This transport is not
# authenticated yet, so compare the fingerprint out of band before trust:
curl -k -o cremind-ca.pem https://<host>:1515/ca.pem

# Docker
docker compose exec cremind cremind tls export --out - > cremind-ca.pem

# Kubernetes
kubectl -n <ns> exec deploy/cremind -c cremind -- \
  cat /root/.cremind/tls/ca.pem > cremind-ca.pem
```

Then `cremind tls trust --file cremind-ca.pem`.

In PowerShell replace `> file` with `| Out-File -Encoding ascii file` — the
default redirection writes UTF-16 with a BOM, which the trust tools reject.

**Check the fingerprint when you download it.** Fetching a CA over a connection
you have not yet authenticated — plain HTTP, or HTTPS whose warning you just
clicked through — is trust-on-first-use: whoever is in the middle could hand
you *their* CA instead, and you would be about to give it root authority. Run
`cremind tls fingerprint` on the server (or read it from the server's boot
environment) and compare it against `cremind tls fingerprint --file
cremind-ca.pem` before trusting. The `docker` and `kubectl` routes above go
through an already-authenticated channel and don't have this problem, which is
why they are the better option when available.

## Firefox and Chromium on Linux

Current Firefox releases normally follow the platform trust store. If the
warning remains, import the same file under **Settings → Privacy & Security →
Certificates → View Certificates → Authorities**. Some Chromium packages on
Linux use an NSS store and may also need an explicit import.

## Troubleshooting

- **`No CA certificate at ...`** — the server has never run with
  `CREMIND_SSL=auto` or `CREMIND_SSL=after-setup`, or it runs on another
  machine. Fetch the CA as above and pass `--file`.
- **The warning persists after trusting** — check the fingerprint matches
  (`cremind tls fingerprint` against the browser's certificate viewer), restart
  the browser, and confirm the address matches a name on the certificate. Names
  beyond `localhost` and the server's own hostname/IPs need
  `CREMIND_SSL_AUTO_HOSTS` set on the server.
- **A `sudo` step failed** — the printed command can be run by hand in a shell
  with the right privileges.
- **Linking Google after the switch** — Google still returns to
  `http://localhost:<port>`, which the server redirects to its HTTPS handler.
  Keep any port-forward open and trust the CA.
- **The server serves plain HTTP** — with `CREMIND_SSL=after-setup` that is
  expected until the Setup Wizard completes: the CA already exists and the
  wizard hands it over, then the server restarts into HTTPS. Otherwise
  `CREMIND_SSL` is ignored when `CREMIND_UI_PORT=0` (an external proxy owns
  the origin); the proxy owns certificate trust there. The Electron desktop app supports HTTPS — trust the CA on its device before enabling TLS.
