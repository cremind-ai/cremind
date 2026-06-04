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
| `scripts/build_ui.sh` | Builds the SPA into `app/static/ui/` so the wheel ships with it. |
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

`uv run cremind serve` opens a second listener on `:1515` to serve the
bundled SPA at `app/static/ui/` — but **only if that directory contains
an `index.html`** (see [app/server.py](app/server.py)'s `_build_ui_server`).
In a fresh checkout it's empty and the listener stays silent. If you've
ever run `bash scripts/build_ui.sh` (e.g. for a release smoke test), it
isn't — and the backend will fight Vite for the port, leaving you
looking at a stale build with no HMR.

Pick **one** before starting Terminal A:

```powershell
# Option A (simplest): wipe the build artifact. It's gitignored; rebuild
# anytime with bash scripts/build_ui.sh.
Remove-Item -Recurse -Force app\static\ui

# Option B: keep the build, but disable the SPA listener for this session.
$env:CREMIND_UI_PORT = "0"

# Option C: keep the build, point the listener at a nonexistent path.
$env:CREMIND_UI_DIR = "C:\nonexistent"
```

If you skip this step you'll see `SPA listener: http://127.0.0.1:1515
(serving …\app\static\ui)` in Terminal A's log — that's the warning sign.
Apply one of the options above, then restart Terminal A.

### Terminal A — backend

```powershell
uv run cremind serve
```

The API listens on `:1112`. With the prereq applied, Terminal A logs
`SPA not present at …\app\static\ui; UI listener disabled` and leaves
`:1515` for Vite.

### Terminal B — Vite dev server

```powershell
cd ui
npm run web:dev
```

Open <http://localhost:1515>. `ui/src/services/runtimeConfig.ts`'s
port-swap heuristic detects the `:1515` host and auto-resolves the
backend at `:1112` — you don't need to set `VITE_AGENT_URL`.

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
| Backend only, no UI | `uv run cremind serve`. UI listener on `:1515` is silent. |
| UI pointed at a remote backend | `cd ui ; npm run web:dev`. Configure the agent URL via the setup wizard, or `$env:VITE_AGENT_URL = "https://..."` before `npm run web:dev`. |
| Single-port end-to-end smoke (no HMR) | `bash scripts/build_ui.sh ; uv run cremind serve`. The SPA gets bundled into `app/static/ui/`, served on `:1515` by the backend. Use for pre-release verification, not active dev. |
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
  populated. Run `bash scripts/build_ui.sh` once before `--channel dev`
  so the SPA listener comes up.
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
- **The port-swap heuristic only kicks in when the SPA is served at
  `:1515`**. Running Vite on a different port (e.g. via `$env:PORT`)
  skips it; you need `VITE_AGENT_URL`.
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
