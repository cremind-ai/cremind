---
description: "Fix the browser's HTTPS certificate warning — \"Your connection is not private\", ERR_CERT_AUTHORITY_INVALID, NET::ERR_CERT_AUTHORITY_INVALID, \"not secure\" — on a Cremind server running with CREMIND_SSL=auto, by trusting the Cremind Local CA it generates. `cremind tls trust` installs that CA into the Windows, macOS, or Linux trust store (one-off per device); `cremind tls export` copies ca.pem out to hand to another machine; `cremind tls fingerprint` prints its SHA-256 to compare against what the browser shows. Other devices download the CA from https://<host>:1515/ca.pem. Runs entirely locally — no server call, no token. Distinct from supplying your own certificate through CREMIND_SSL_CERTFILE/CREMIND_SSL_KEYFILE, and from terminating TLS at an Ingress or reverse proxy."
---

# `cremind tls` — Trust the local HTTPS certificate authority

When Cremind serves HTTPS with `CREMIND_SSL=auto`, it generates its own
certificate authority in `<CREMIND_SYSTEM_DIR>/tls/` and signs the server
certificate with it. Browsers reject that chain until the CA is installed in
the **device's** trust store, which is what the warning page means:

> Your connection is not private — `ERR_CERT_AUTHORITY_INVALID`
> Issuer: Cremind Local CA

No server setting can remove that warning. A certificate is trusted because it
chains to a root the device already holds, so one manual install per device is
unavoidable. The CA exists so it is a **one-off**: server certificates get
reissued (on expiry, or when a hostname is added) and stay trusted underneath
it, and on Kubernetes and Docker the CA lives on a persistent volume, so it
survives restarts.

These commands run entirely on the local machine — they read a file and hand it
to the operating system. They never call the Cremind API and need no token,
because the whole point is the moment when nothing can talk to the server yet.

## Global flags

`cremind tls` accepts the root-level `--json` flag. No `CREMIND_TOKEN` and no
profile are needed.

## Subcommands

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
# From the running server, in a browser or with curl (click through the
# warning once — the CA is public material, this is safe):
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

Firefox ships its own trust store and ignores the system one. Import the same
file under **Settings → Privacy & Security → Certificates → View Certificates →
Authorities**. Some Chromium builds on Linux use an NSS store the same way.

## Troubleshooting

- **`No CA certificate at ...`** — the server has never run with
  `CREMIND_SSL=auto`, or it runs on another machine. Fetch the CA as above and
  pass `--file`.
- **The warning persists after trusting** — check the fingerprint matches
  (`cremind tls fingerprint` against the browser's certificate viewer), restart
  the browser, and confirm the address matches a name on the certificate. Names
  beyond `localhost` and the server's own hostname/IPs need
  `CREMIND_SSL_AUTO_HOSTS` set on the server.
- **A `sudo` step failed** — the printed command can be run by hand in a shell
  with the right privileges.
- **The server serves plain HTTP** — `CREMIND_SSL=auto` is ignored when
  `CREMIND_UI_PORT=0` (an external proxy owns the origin) and under the
  Electron desktop app. Nothing needs trusting in either case.
