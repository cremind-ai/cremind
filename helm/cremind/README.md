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

## Key values

| Key | Default | Notes |
|-----|---------|-------|
| `replicaCount` | `1` | Do not raise without RWX storage — see Scaling. |
| `image.tag` | `""` → `appVersion` | The matching `cremind-desktop` image tag. |
| `cremind.installMode` | `kubernetes` | Drives external-only service modes. |
| `cremind.setupWizardEnv` | `kubernetes` | Pre-fills the wizard. |
| `persistence.system.*` | `5Gi`, RWO | `bootstrap.toml`, tokens, profiles. |
| `persistence.venv.*` | `8Gi`, RWO | Wizard-installed Python deps (LLM SDKs, embeddings). |
| `postgresql.enabled` | `true` | Bundled Bitnami PostgreSQL. |
| `qdrant.enabled` / `chromadb.enabled` | `false` | Enable when turning on embeddings. |
| `ingress.enabled` | `false` | Routes the SPA/wizard (UI port). |
| `autoscaling.enabled` | `false` | Must stay false under the single-replica model. |

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

## Scaling

This chart targets **single-replica + shared external state**:

- PostgreSQL holds dynamic config (JWT signing secret, LLM keys, tool configs,
  profiles), so a rescheduled pod stays authenticated.
- The PVCs hold `bootstrap.toml`, OAuth tokens, per-profile files, and the
  runtime venv, so a rescheduled pod boots straight through without re-setup.

True multi-replica (active-active) is **not** supported yet. Before raising
`replicaCount`:

1. Switch the PVCs to a **ReadWriteMany** storage class (NFS/CephFS/EFS).
   Per-pod `browser-profile/` cannot be shared (Chrome locks its profile) and
   needs per-pod state.
2. Add a leader or one-shot Setup Job so two pods don't race first-setup
   (`bootstrap.toml` write + admin-profile creation).
3. The JWT secret already lives in shared PostgreSQL — no per-pod secret
   migration is needed.
