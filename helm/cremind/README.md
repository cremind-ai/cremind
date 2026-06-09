# Cremind Helm chart

Deploys [Cremind](https://github.com/cremind/cremind) on Kubernetes as a
**single-replica** Deployment that boots straight into the **Setup Wizard** —
no install script runs. Storage is constrained for a clustered environment:
**PostgreSQL only** (SQLite is rejected), **embeddings off by default**, and if
embeddings are enabled, **only external/in-cluster vector stores** (Qdrant or
ChromaDB over HTTP) — never pod-local persistent storage.

The pod runs the published `cremind/cremind-desktop` image. The chart sets
`INSTALL_MODE=kubernetes` and `SETUP_WIZARD_ENV=kubernetes`, mounts
`CREMIND_SYSTEM_DIR` and the runtime venv on PersistentVolumeClaims, and — by
design — **never sets `CREMIND_DB_PROVIDER`** (which would skip the wizard).

### Single entry point

An in-pod **nginx reverse-proxy sidecar** fronts the whole workflow on **one
port** so you never forward more than one: it routes `/api` + `/health` to the
backend, `/vnc/` to the agent's desktop (noVNC, always on), and everything else
to the SPA. The Service exposes only that port, and an Ingress targets it. The
SPA calls the backend **same-origin**, so it works behind a single port-forward
*or* a single Ingress hostname.

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

Reach it with a single port-forward (or an Ingress hostname):

```bash
kubectl -n cremind port-forward svc/cremind 8080:80
# UI / wizard:   http://localhost:8080/#/setup
# agent desktop: http://localhost:8080/vnc/vnc.html
```

Follow the `NOTES` printed after install: open `/#/setup` and click through. With
the bundled PostgreSQL, the Database step is fully pre-filled — including the
password, which is auto-wired into the pod from the generated Secret (via
`secretKeyRef`) and used server-side, so you **leave the password blank and just
click Next** (the Docker-Compose-like "everything is wired" experience). You only
type credentials when using an external PostgreSQL without `cremind.postgresPasswordSecret`.

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
>      --set global.security.allowInsecureImages=true \
>      --set postgresql.image.registry=docker.io \
>      --set postgresql.image.repository=bitnamilegacy/postgresql
>    ```
>    (`allowInsecureImages=true` is required because the image is no longer the
>    chart's signed default — use it only for evaluation.)
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

## Key values

| Key | Default | Notes |
|-----|---------|-------|
| `replicaCount` | `1` | **Fixed at 1.** The chart rejects any other value (VNC = single desktop). |
| `resources.requests` | `2` CPU, `2Gi` | Minimum guaranteed for the cremind container; the node must have it free. |
| `image.tag` | `""` → `appVersion` | The matching `cremind-desktop` image tag. |
| `cremind.installMode` | `kubernetes` | Drives external-only service modes. |
| `cremind.setupWizardEnv` | `kubernetes` | Pre-fills the wizard. |
| `cremind.appUrl` | `""` → auto | A2A card URL; auto-derives the Ingress URL or `http://localhost:8080`. |
| `persistence.system.*` | `5Gi`, RWO | `bootstrap.toml`, tokens, profiles. |
| `persistence.venv.*` | `8Gi`, RWO | Wizard-installed Python deps (LLM SDKs, embeddings). |
| `postgresql.enabled` | `true` | Bundled Bitnami PostgreSQL. |
| `qdrant.enabled` / `chromadb.enabled` | `false` | Enable when turning on embeddings. |
| `proxy.enabled` | `true` | nginx single-entry sidecar (UI + API + noVNC on one port). |
| `service.port` | `80` | The one Service port (fronts the proxy). |
| `ingress.enabled` | `false` | One hostname for everything (UI at `/`, noVNC at `/vnc/`). |

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

Cremind runs as **exactly one pod, by design**. The image bundles a VNC virtual
desktop (XFCE + Chrome) that the agent drives, so each pod is its own
independent desktop. Two pods would mean two divergent desktops and two agents
fighting over the same shared database — not a scaled service. Therefore:

- `replicaCount` is **fixed at 1** and the chart **fails to render** if you set
  anything else.
- There is **no HorizontalPodAutoscaler** and no `autoscaling` values.
- The Deployment uses the `Recreate` strategy, so even an upgrade never runs two
  pods at once.
- Do **not** `kubectl scale` the Deployment — it would break the VNC model. (A
  `helm upgrade` resets it to 1.)

State survives a pod reschedule without scaling: PostgreSQL holds the dynamic
config (JWT signing secret, LLM keys, tool configs, profiles) and the PVCs hold
`bootstrap.toml`, OAuth tokens, per-profile files, and the runtime venv — so a
restarted/rescheduled single pod boots straight through with no re-setup.

To serve more load, give the single pod more resources (`resources`) and a
bigger node; do not add replicas.
