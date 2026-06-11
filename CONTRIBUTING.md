# Contributing to Cremind

Cremind's backend (Python) and UI (Vue 3 + Electron) live together in
this repo. A feature that touches both ships from one branch and one
PR. This document covers everyday development; for the release pipeline
see [RELEASING.md](RELEASING.md).

## Repo layout

| Path | What |
|---|---|
| `app/` | Python backend — Starlette server, CLI, agent, tools, skills, channels. |
| `app/__version__.py` | The single source of truth for the project version. |
| `app/static/ui/` | Built SPA artifact (gitignored). Populated by `scripts/build_ui.sh`. |
| `ui/` | Vue 3 + Vite + Electron source. |
| `ui/src/` | Vue components, stores, services. |
| `ui/electron/` | Electron main process + preload. |
| `scripts/build_ui.sh` (Windows: `scripts/build_ui.ps1`) | Builds the SPA into `app/static/ui/` so the wheel ships with it. |
| `scripts/sync_ui_version.py` | Mirrors `app/__version__.py` → `ui/package.json` (also runs as the `prebuild`/`preweb:build` npm hook). |
| `.github/workflows/` | `pr.yml`, `release.yml`, `release-test.yml`. |
| `pyproject.toml` | Python deps + hatch config. |
| `RELEASING.md` | How to ship a new version. |

## First-time setup

```powershell
# Python deps
uv sync                                  # reads pyproject.toml + uv.lock

# Node deps
cd ui
npm install
cd ..

# Initial config (profile, LLM keys, ports, etc.)
uv run cremind setup
```

`uv run cremind setup` is interactive and writes `~/.cremind/` with your
profile and provider credentials. You only need to run it once unless
you blow away `~/.cremind/`.

## Daily dev — two terminals

The everyday loop: backend on `:1112`, Vite on `:1515`, each side
hot-reloads its own half.

### Prereq: free port `:1515` for Vite

`uv run cremind serve` now serves one merged app on the single public port
(`CREMIND_UI_PORT`, default `:1515`) — the SPA, API, A2A, and OAuth together —
plus an internal loopback API on `:1112`. So it binds `:1515` regardless of
whether `app/static/ui/` is built, and would fight Vite for the port. Free it
by running the backend on loopback only:

```powershell
# Bind only the internal API (loopback :1112); open no public :1515 bind.
$env:CREMIND_UI_PORT = "0"
```

If you skip this you'll see the backend bind `:1515` in Terminal A's log —
that's the warning sign. Set `CREMIND_UI_PORT=0` and restart Terminal A.

### Terminal A — backend

```powershell
$env:CREMIND_UI_PORT = "0"               # bind the internal API on :1112 only; free :1515 for Vite
$env:APP_URL = "http://localhost:1112"   # agent card + OAuth redirects target the backend, not Vite
uv run cremind serve
```

With `CREMIND_UI_PORT=0` Terminal A logs `serving loopback-only on
http://127.0.0.1:1112` — the backend binds only the internal API and
leaves `:1515` for Vite. **`APP_URL=http://localhost:1112` is required to
test account-linking (Gmail/Atlassian) in dev**: the OAuth redirect derives
from `APP_URL`, so the production default (`:1515`) would send the consent
redirect to Vite's SPA ("Select a profile…") instead of the backend's
`/api/oauth/.../callback`. Pointing it at `:1112` lets linking complete.

### Terminal B — Vite dev server

```powershell
cd ui
$env:VITE_AGENT_URL = "http://localhost:1112"   # point the SPA at the backend's internal API
npm run web:dev
```

Open <http://localhost:1515> (Vite). Set `VITE_AGENT_URL=http://localhost:1112`
so the dev SPA reaches the backend's internal API — the merged app is
same-origin in production, so without this override `runtimeConfig.ts`
resolves the API at Vite's own `:1515`, which doesn't serve it.

To confirm you're hitting Vite (not a stale backend bundle), open
browser DevTools → Sources. You should see `/@vite/client` —
that's HMR. If you only see hashed `assets/index-Cabc123.js`, the
backend's SPA listener won out; revisit the prereq above.

### What hot-reloads

| Change | Reload |
|---|---|
| `ui/src/**` (Vue, TS, CSS) | Vite HMR — instant in the browser. |
| `ui/vite.config*.ts`, `ui/package.json` | Restart Terminal B (`Ctrl+C`, then `npm run web:dev`). |
| `app/**` (Python) | Restart Terminal A (`Ctrl+C`, then `uv run cremind serve`). No `--reload` flag on `cremind serve` today — see "Auto-restart on Python edits" below if you want one. |
| `pyproject.toml` deps | `uv sync --all-extras`, then restart Terminal A. |
| `app/__version__.py` | The pre-commit hook syncs `ui/package.json` automatically. Manual: `python scripts/sync_ui_version.py`. |

### Auto-restart on Python edits (optional)

`cremind serve` doesn't expose `--reload`, but you can wrap it with
[`watchfiles`](https://github.com/samuelcolvin/watchfiles) from the outside:

```powershell
uv run --with watchfiles watchfiles "uv run cremind serve" app
```

Watches `app/` and restarts the whole process on any change. Slower than
uvicorn's in-process reload (~2 s for a cold restart) but it always works.

## Variations

| You want… | Run |
|---|---|
| Backend only (API on loopback) | `$env:CREMIND_UI_PORT=0; uv run cremind serve` — binds `:1112` only, no public `:1515`. |
| UI pointed at a remote backend | `cd ui ; npm run web:dev`. Configure the agent URL via the setup wizard, or `$env:VITE_AGENT_URL = "https://..."` before `npm run web:dev`. |
| Single-port end-to-end smoke (no HMR) | `bash scripts/build_ui.sh ; uv run cremind serve` (Windows: `.\scripts\build_ui.ps1 ; uv run cremind serve`). The SPA gets bundled into `app/static/ui/`, served on `:1515` by the backend. Use for pre-release verification, not active dev. |
| Electron desktop dev | `cd ui ; npm run dev`. Wraps the SPA in an Electron window. Talks to the backend URL from `~/.cremind-ui/cremind-config.json`. |

## Installing your checkout via the installer scripts

The two-terminal flow above is the fastest dev loop. If you want to
exercise the actual installer scripts users will run — to test changes
to `install/install.sh` or `install/install.ps1`, or to bring up the
full Docker bundle with Postgres + Qdrant against your checkout — pass
`--channel dev` (Linux/macOS) or `-Channel dev` (Windows):

```bash
bash install/install.sh --channel dev --deployment local
```

```powershell
.\install\install.ps1 -Channel dev -Deployment local
```

Dev channel skips PyPI and runs `pip install -e .` against your
checkout. Templates (`local.env`, `docker-compose.yml.tmpl`, …) come
from the checkout's `install/templates/`, not GitHub. The shared
catalog (deployment / mode labels, mode-rule visibility table) is
read from `install/_catalog.sh` (or `_catalog.ps1`) in the checkout
— both are generated from `install/catalog.toml` by
`python install/scripts/build_catalog.py`.

When editing `install/catalog.toml`, re-run the generator and commit
both the master and the generated files:

```bash
python install/scripts/build_catalog.py
```

CI runs `python install/scripts/build_catalog.py --check`; the build
fails when the committed includes diverge from the master.

Caveats:

- The wizard at `http://localhost:1515` needs `app/static/ui/`
  populated. Run `bash scripts/build_ui.sh` (Windows: `.\scripts\build_ui.ps1`)
  once before `--channel dev` so the backend serves the SPA.
- `--channel dev` requires running the script *from* a checkout —
  piping it via `curl | bash` (no file on disk) is rejected.
- `--channel dev --mode docker` works: the installer emits a
  `docker-compose.override.yml` that points the build context at the
  checkout, swaps the pip install for `-e /src`, and bind-mounts the
  checkout into the container. Host edits to `app/` show up after
  `docker compose restart cremind`.
- `CREMIND_UPGRADE_CHANNEL` is intentionally not written to `.env` in
  dev channel. Don't run `cremind upgrade` from a dev install — pull
  from git instead.

## Gotchas

- **Stale UI on `:1515` after a previous `build_ui.sh`** — see the
  "Prereq" subsection above. Symptom in Terminal A: `SPA listener:
  http://127.0.0.1:1515 (serving …\app\static\ui)`. Symptom in browser:
  edits to `ui/src/**` don't show up.
- **First page load after `setup`**: the SPA on `:1515` may show the
  setup wizard until the agent URL is configured. After that it sticks.
- **Backend not picking up code changes**: kill + restart Terminal A.
  Don't restart Vite — it's stateful for HMR.
- **The dev SPA needs `VITE_AGENT_URL`**. The old `:1515`→`:1112`
  port-swap was removed (the SPA is same-origin in production), so in
  dev point it at the backend with `VITE_AGENT_URL=http://localhost:1112`
  (or `ui/.env.local`).
- **`ui/node_modules` is large** — ~600 MB. Stay patient on first
  install.
- **Don't edit `ui/package.json`'s `version` field** — it's regenerated
  from `app/__version__.py` by `scripts/sync_ui_version.py` (runs as
  the `prebuild`/`preweb:build` npm hook).

## Tests

| What | Command |
|---|---|
| Backend pytest | `uv run pytest` |
| UI type-check | `cd ui ; npx vue-tsc --noEmit` |
| UI web smoke build | `cd ui ; npm run web:build` |

The same commands run on every PR via
[`.github/workflows/pr.yml`](.github/workflows/pr.yml). A failing job
blocks merge.

## Branch, commit, PR

```powershell
git checkout main ; git pull
git checkout -b my-feature
# edit anywhere under app/ or ui/src
git push -u origin my-feature
```

Open a pull request against `main`. `pr.yml` runs three jobs in
parallel: `backend` (pytest), `ui` (vue-tsc + web bundle), and
`smoke-build` (wheel + Linux Electron .AppImage,
uploaded as PR artifacts so reviewers can install and try the build).
All three must pass before merge.

## Releasing

See [RELEASING.md](RELEASING.md). Releases are coordinated by a **Core
Maintainer** (owns the version line) and one or more **Component
Maintainers** (one per PR).

Short version: a Core Maintainer assigns each PR slated for the next
version an RC index (`rc1`, `rc2`, …). The Component Maintainer
cuts `v<X.Y.Z>rc<N>.dev<M>` tags from the PR branch tip — no
`app/__version__.py` bump on the PR branch. Iterate `dev<M+1>` per fix
until validated on a test-channel install. Merge with **merge-commit**
(not rebase, not squash — the dev tag's commit must remain reachable
from main). After every slated PR has shipped a validated dev release
and merged, the Core Maintainer bumps `app/__version__.py` on main,
commits, tags `v<X.Y.Z>`, and approves the prod-release run in the
Actions UI. CI enforces that a prod tag matches `app/__version__.py`,
points at a commit on main, and is backed by at least one
successful dev release whose commit is also on main — there's no
"skip the dev release" path.
