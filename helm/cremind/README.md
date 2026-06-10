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

### Linking Google accounts (Gmail / Calendar)

The Gmail/Calendar skills use a Google **Desktop** OAuth client, which may only
redirect to a loopback address. The chart handles this through the single proxy
port:

- **With a port-forward (`port-forward svc/cremind <anyport>:80`)** linking is
  **one-click** — after you approve in the browser, the consent redirect comes
  back through the proxy to the backend at `/api/oauth/google/callback` and the
  page shows "Authentication complete". The redirect **auto-tracks whatever local
  port you used** (the backend reads it from your browser's own requests), so you
  can change the port-forward port freely and you do **not** need to set
  `cremind.appUrl` for it. No second port-forward is needed.
- **With Ingress (a real hostname),** a Desktop OAuth client cannot redirect to
  the pod, so the post-approval page will fail to load (`ERR_CONNECTION_REFUSED`)
  — this is expected. Copy the **full URL** from your browser's address bar (it
  contains `code=…&state=…`) and give it back to the agent; it finishes linking
  with the skill's `complete-link` step while the original `link` is still
  running. Then ask it to check `status`.

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
| `replicaCount` | `1` | **Fixed at 1.** The chart rejects any other value (VNC = single desktop). |
| `resources.requests` | `2` CPU, `2Gi` | Minimum guaranteed for the cremind container; the node must have it free. |
| `image.tag` | `""` → `appVersion` | The matching `cremind-desktop` image tag. |
| _(release channel)_ | auto from `image.tag` | Not a knob. `test` when the effective tag is an RC (`…rcN.devM`, i.e. the `--devel` chart), else `production`; the in-app **Updates** page reports this. Force it via `cremind.extraEnv` (`CREMIND_UPGRADE_CHANNEL`). |
| `cremind.installMode` | `kubernetes` | Drives external-only service modes. |
| `cremind.setupWizardEnv` | `kubernetes` | Pre-fills the wizard. |
| `cremind.appUrl` | `""` → auto | A2A card URL; auto-derives the Ingress URL or `http://localhost:8080`. A loopback value also enables one-click Google account linking (see above). |
| `persistence.system.*` | `5Gi`, RWO | `bootstrap.toml`, tokens, profiles. |
| `persistence.venv.*` | `8Gi`, RWO | Wizard-installed Python deps (LLM SDKs, embeddings). |
| `persistence.work.*` | `10Gi`, RWO | Agent working dir (files it creates); `mountPath` must match the wizard's User Working Directory. |
| `extraVolumes` / `extraVolumeMounts` | `[]` | Persist any additional paths (raw volume specs). |
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
