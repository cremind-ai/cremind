# Releasing Cremind

Cremind ships from a single git tag: the Python wheel goes to PyPI, the
Electron installers (Windows / macOS / Linux) attach to a GitHub Release,
and the Docker image pushes to `cremind/cremind-desktop` on Docker Hub.
All three artifacts carry the same version number.

Releases are coordinated by two roles:

- **Core Maintainer.** Owns the version line. Decides which PRs land in
  the next release, assigns each one an RC index (`rc1`, `rc2`, …),
  and ultimately bumps [`app/__version__.py`](app/__version__.py) and
  cuts the production tag.
- **Component Maintainer.** Owns the per-PR work. Cuts test releases
  from the PR branch tip using the index the Core Maintainer assigned,
  iterates dev releases until validated, and pushes fixes to the PR
  branch.

The model is still **validate-before-merge** — every production tag
`vX.Y.Z` points at a commit on main whose contributing PRs each shipped
a `vX.Y.Zrc<PR>.dev<M>` test release that was installed and
exercised on a real machine. `release-prod.yml` refuses to ship a tag
unless [the four verify gates](#the-four-verify-gates) all pass, and it
pauses for a required-reviewer approval in the Actions UI before
publishing anything. That approval is the human "I validated this"
signal.

This doc walks one worked example end-to-end (v0.0.2, two PRs). Use it
as a template; substitute your own version, PR numbers, and SHAs.

---

## How v0.0.2 shipped

Suppose the last shipped version is `v0.0.1` and the Core Maintainer
has two PRs slated for `0.0.2`:

- PR #41 — adds feature X (Component Maintainer: Alice).
- PR #42 — adds feature Y (Component Maintainer: Bob).

### 1. Core Maintainer assigns RC indexes

The Core Maintainer leaves a comment on each PR (or pins an issue
tracking the `0.0.2` slate) assigning the index:

> **PR #41 → `rc1`** for the upcoming `v0.0.2`. Cut your dev releases
> as `v0.0.2rc1.dev<M>`.
>
> **PR #42 → `rc2`** for the upcoming `v0.0.2`. Cut your dev releases
> as `v0.0.2rc2.dev<M>`.

This assignment is *communication only*. No code change, no commit.
The PR branches' `app/__version__.py` stays at `0.0.1` until the final
prod release on main.

### 2. Open the PRs

Each Component Maintainer opens their PR as normal:

```powershell
git checkout -b feat/feature-x
# edits across app/, ui/, install/, tests/
git push -u origin feat/feature-x
gh pr create --title "Feature X" --body "..."
```

[`.github/workflows/pr.yml`](.github/workflows/pr.yml) runs three jobs:
`backend` (pytest), `ui` (`vue-tsc --noEmit` + `npm run web:build`
smoke), and `smoke-build` (wheel + Linux `.AppImage` uploaded as PR
artifacts). All three must be green.

### 3. Cut the first dev release for each PR

Once `pr.yml` is green, the Component Maintainer tags from the **remote
PR branch tip** using the index the Core Maintainer assigned:

```powershell
# Alice for PR #41
git fetch origin
git tag v0.0.2rc1.dev1 origin/feat/feature-x
git push origin v0.0.2rc1.dev1
```

```powershell
# Bob for PR #42
git fetch origin
git tag v0.0.2rc2.dev1 origin/feat/feature-y
git push origin v0.0.2rc2.dev1
```

[`release-rc.yml`](.github/workflows/release-rc.yml) triggers for each
tag. It rewrites `app/__version__.py` in-CI only (never committed) to
the PEP 440 form `0.0.2rc1.dev1` / `0.0.2rc2.dev1` (which is just the
tag with the `v` stripped — the tag already carries PEP 440 canonical
form), builds the wheel, uploads it to Test PyPI, builds and pushes
`cremind/cremind-desktop:0.0.2rc1.dev1` (and `:0.0.2rc2.dev1`) to Docker
Hub, and attaches Windows / macOS / Linux Electron installers to an
auto-published GitHub prerelease. Test-channel installs see it on the
next Check-for-updates poll. ~10–15 minutes end-to-end per PR.

`app/__version__.py` on the PR branch stays at `0.0.1` throughout. The
in-CI rewrite means the wheel that hits Test PyPI carries the dev
version, but the branch source does not.

### 4. Validate on a real machine

This is the human checkpoint. The Component Maintainer (and anyone
they ask to help) installs from the test channel and exercises the
feature end-to-end:

```powershell
.\install\install.ps1 -Channel test -Deployment local
```

```bash
bash install/install.sh --channel test --deployment local
```

The installer fetches the highest-numbered dev wheel from Test PyPI
(or builds the Docker bundle with Test-PyPI indexes if you pass
`--mode docker`).

If validation surfaces a bug, push the fix to the **same PR branch**
and cut the next dev iteration:

```powershell
git tag v0.0.2rc1.dev2 origin/feat/feature-x
git push origin v0.0.2rc1.dev2
```

Iterate `dev<M+1>` per bug. The PR branch's `app/__version__.py` still
stays at `0.0.1` — only the dev counter advances.

### 5. Merge each PR with **merge-commit**

Once a PR's dev release is validated, merge it. Use **merge-commit**,
not `gh pr merge --rebase`, not `--squash`:

```powershell
gh pr merge 41 --merge
gh pr merge 42 --merge
```

> **Why not rebase-merge?** `gh pr merge --rebase` re-applies the
> feature commits with fresh committer dates, producing **new SHAs**.
> The dev tag still points at the *original* PR-branch SHA, which
> is no longer reachable from main — and verify gate 4 (the dev tag's
> commit must be reachable from `origin/main`) fails. Merge-commit
> creates a merge node on top of main and keeps the original feature
> commits as parents; the dev release's commit is still reachable from
> main and gate 4 passes.
>
> **Why not squash?** Squash rolls the whole branch into one new
> commit, which also has a different SHA. Same failure mode.

You can merge PRs in any order. Each PR's validation is independent.

### 6. Core Maintainer cuts the production release

After every slated PR has shipped at least one validated dev release
**and** been merged to main, the Core Maintainer bumps the version on
main and tags prod:

```powershell
git checkout main
git pull --ff-only
# Edit app/__version__.py: __version__ = "0.0.2"
# Leave MIN_SUPPORTED_UPGRADE_FROM alone unless you are explicitly
# dropping support for an older version.
python scripts/sync_ui_version.py     # copies the version into ui/package.json
git add app/__version__.py ui/package.json
git commit -m "Bump to 0.0.2"
git push origin main
git tag v0.0.2
git push origin v0.0.2
```

The version bump is a fresh commit on main. The prod tag points at
that commit — *not* at any of the dev-release commits. Gate 4 doesn't
require same-commit matching anymore; it just needs at least one
validated `v0.0.2rc<N>.dev<M>` dev tag reachable from main.

### 7. Approve the prod-release run in the Actions UI

[`release-prod.yml`](.github/workflows/release-prod.yml) triggers on
`v0.0.2`. The first job (`verify`) runs the four mechanical gates; if
any fails, no draft is created, no PyPI upload happens, nothing
publishes. Inspect the failing gate's log and either re-tag a
different commit or fix the underlying issue.

Once `verify` passes, the workflow pauses on the `approve` job. Go to
**Actions → release-prod → the running workflow** and click **Review
deployments → prod-release → Approve and deploy**. This is where you
re-affirm "I (or the Component Maintainers I trust) installed and
validated each PR's dev release in step 4." Rejecting cancels the run
before any artifact is built or published.

The reviewer roster lives in **repo Settings → Environments →
prod-release → Required reviewers**. Only listed users see the
Approve button.

### 8. Watch the prod workflow publish

After approval the workflow runs seven jobs:

1. **`verify`** — the four mechanical gate checks (next section).
2. **`approve`** — the human-validation gate (just completed).
3. **`prepare-draft`** — creates a draft GitHub Release with
   auto-generated notes from PR titles since the previous tag.
4. **`wheel`** (Ubuntu) — builds the SPA via `scripts/build_ui.sh`,
   runs `hatch build`, smoke-tests the wheel, publishes to PyPI,
   attaches `.whl` + `.tar.gz` to the draft.
5. **`electron`** matrix (Ubuntu / macOS / Windows) — `npm run build`
   in `ui/`, then electron-builder publishes signed installers +
   `latest*.yml` update manifests to the same draft.
6. **`docker`** (Ubuntu) — waits for `wheel` (PyPI must serve the new
   version), builds `Dockerfile.desktop` with
   `CREMIND_PIP_SPEC=cremind==0.0.2`, pushes
   `cremind/cremind-desktop:0.0.2` + `:latest` to Docker Hub.
7. **`publish`** — promotes the draft from hidden to public, **only
   after** all of the above succeed. Half-finished releases never reach
   users.

```powershell
gh run watch <run-id> --exit-status   # optional: block until done
```

When `publish` finishes, the release is live at
`https://github.com/cremind-ai/cremind/releases/tag/v0.0.2`. Users can
`pip install cremind==0.0.2`, `docker pull
cremind/cremind-desktop:latest`, or download the platform installer.

### 9. Delete the feature branches

```powershell
git push origin --delete feat/feature-x
git push origin --delete feat/feature-y
git branch -d feat/feature-x feat/feature-y
```

(If anyone used rebase-merge by accident, `-d` will refuse — `git
branch -D` to force-delete locally. The content is on main under
different SHAs, but gate 4 won't accept that PR's dev tag.)

That's the whole cycle.

---

## The four verify gates

`release-prod.yml`'s first job is `verify`. It runs four mechanical
checks and refuses to start the rest of the workflow unless every one
passes. No draft, no PyPI upload, no Electron build until the gate
accepts the tag — and after the four pass, the `approve` job adds the
human-validation gate (step 7 above).

1. **Tag format.** `vX.Y.Z` or `vX.Y.Z.N` (the hotfix form). A tag
   containing `rc` is excluded by the workflow trigger and handed off
   to `release-rc.yml` instead.

2. **Version match.** The tag's bare version equals
   `app/__version__.py:__version__`. This is what forces the Core
   Maintainer's bump commit on main before a prod tag can ship —
   without that bump the source still reads the previous prod version
   and the gate fails.

3. **On main.** `git merge-base --is-ancestor <tag commit>
   origin/main`. No shipping from feature branches; no shipping
   commits that never landed on main.

4. **At least one validated dev release on main.** Some
   `v<X.Y.Z>rc<N>.dev<M>` tag must exist in the repo whose
   `release-rc.yml` run concluded with `success` AND whose commit is
   reachable from `origin/main`. The dev tag does *not* have to live
   at the same commit as the prod tag — the prod commit is the
   version-bump commit, which by design has no dev tag of its own.
   What this gate catches: shipping `v0.0.2` when no PR for `0.0.2`
   ever went through the dev-release cycle, or when the PR that did
   was never merged.

After the four pass, the `approve` job pauses the run for required-
reviewer approval on the `prod-release` environment. That click is
the "validated by a human" signal — without it, no PyPI upload,
Electron build, or Docker push happens.

`release-rc.yml` enforces exactly one constraint of its own: the tag
must match the canonical shape
`^v(\d+\.\d+\.\d+(?:\.\d+)?)rc(\d+)\.dev(\d+)$`. Anything else — the
legacy hyphenated `v<X.Y.Z>-rc.<N>[.dev.<M>]` form, a bare
`v<X.Y.Z>rc<N>` without a dev counter, a tag missing the leading `v`
— is rejected at the `Compute rc version` step and the workflow
fails before any wheel/Docker/Electron job runs. Dev tags intentionally
ship from PR branches whose `app/__version__.py` still reads the prior
prod version, so no source-version-match check is performed at RC
time — verify-gate-4 on `release-prod.yml` is what ensures every
shipped prod version is preceded by a validated dev release on main.

---

## Docs-only PRs (and other no-release scenarios)

**Most documentation changes don't trigger a release at all.** A PR
that only updates `README.md`, `RELEASING.md`, `CONTRIBUTING.md`, code
comments, or developer-facing files under `docs/` lands on main like
any other PR and rolls into the *next* feature release. There is
nothing to ship to users.

The same applies to CI-only changes (workflow YAML, `.github/`),
repo-metadata files, test refactors that don't touch shipped code, and
anything else with no effect on the installed binary.

**Concretely, for a docs-only PR:**

1. Open the PR.
2. `pr.yml` runs the same three jobs (`backend`, `ui`,
   `smoke-build`). They should all be green because nothing in the
   runtime behaviour changed.
3. Merge with **merge-commit** (no RC index assignment, no dev
   release, no version bump — there is nothing operator-facing).
4. Done.

The next feature release picks the doc change up automatically. The
auto-generated release notes pull from PR titles since the last tag,
so the docs-only PR appears under the next version's release notes
without you doing anything extra.

### When a docs-only PR *does* want its own release

Rare, but possible — e.g. a critical correction to operator-facing
docs that they need before the next feature release ships. Treat it
as a single-PR release:

1. Core Maintainer assigns the PR `rc1` for the next patch version
   (`v0.0.3`).
2. Component Maintainer tags `v0.0.3rc1.dev1` from the PR branch
   tip. Validation is trivial: open the auto-published prerelease on
   GitHub and confirm the auto-generated notes mention the doc PR.
3. Merge the PR with merge-commit.
4. Core Maintainer bumps `app/__version__.py` to `0.0.3` on main,
   commits, tags `v0.0.3`, approves the prod-release run.

The wheel, Electron installers, and Docker image all rebuild even
though the runtime code is unchanged. That's fine — they're
idempotent and the cost is one CI cycle.

---

## Variations

### Single-PR releases

When only one PR is slated for the next version, the flow degenerates
naturally: Core Maintainer assigns `rc1`, Component Maintainer ships
`v<X.Y.Z>rc1.dev<M>` until validated, merges, Core Maintainer bumps
+ tags prod. Same six numbered steps; the only difference is there's
no PR #42 to wait on.

### Hotfix on top of a shipped release

Four-segment version. To hotfix `0.0.2`:

```powershell
git checkout -b hotfix/v0.0.2.1 v0.0.2
# fix, commit, push, open PR
# Core Maintainer assigns rc1 for v0.0.2.1
git tag v0.0.2.1rc1.dev1 origin/hotfix/v0.0.2.1
git push origin v0.0.2.1rc1.dev1
# validate the dev release; iterate dev<M+1> per fix
# merge (merge-commit), then on main:
# Edit app/__version__.py: __version__ = "0.0.2.1"
python scripts/sync_ui_version.py
git add app/__version__.py ui/package.json
git commit -m "Bump to 0.0.2.1"
git push origin main
git tag v0.0.2.1
git push origin v0.0.2.1
# approve the prod run in Actions UI when it pauses on `approve`
```

PEP 440 accepts the four-segment form; hatchling and the upgrader both
handle it. The RC workflow's tag regex matches
`v0.0.2.1rc<N>.dev<M>` exactly.

### Schema change

When the PR touches [`app/storage/models.py`](app/storage/models.py):

1. Generate the migration: `cremind db revision --autogenerate -m
   "short_description"`.
2. **Review the autogenerate output by hand.** It misses or
   mis-handles column renames (sees them as drop+add),
   `server_default` changes, check constraints, enum additions, and
   index renames.
3. Prefer additive shapes within one release. Split destructive
   changes across two releases (release N adds + dual-writes; release
   N+1 drops the old shape) so rollback works.
4. Backfill **inside** the migration, not in application code.
5. Test the upgrade from a real older install, not just a fresh DB.

### A release fails partway through

Because `publish` is the last job, a partial failure leaves the draft
hidden. To retry:

```powershell
gh release delete v0.0.2 --cleanup-tag --yes
# fix the underlying issue on main (or a follow-up branch + PR)
# if the fix needs its own validation, run a dev-release cycle for it
git tag v0.0.2          # re-tag the (possibly new) commit on main
git push origin v0.0.2
# approve in Actions UI
```

**Exception: PyPI is irreversible.** Once `cremind==0.0.2` uploads to
PyPI, you cannot re-upload the same version. If the wheel job
succeeded but Electron failed, either re-tag at a higher patch
version (`v0.0.3`), or `pip uninstall cremind==0.0.2` on the clients
you control and leave the broken wheel published as a known bad
release.

---

## Reference

### Tag conventions

RC tags carry the **PEP 440 canonical form** verbatim with a leading
`v` — the same string lands on GitHub, Test PyPI, and Docker Hub
(modulo the `v`). The `.dev<M>` counter is mandatory.

| Tag pattern        | PEP 440          | npm / installer       | Docker tag                            | PyPI     | Channel      |
|--------------------|------------------|-----------------------|---------------------------------------|----------|--------------|
| `v0.0.2rc1.dev1`   | `0.0.2rc1.dev1`  | `0.0.2-rc.1.dev.1`    | `cremind-desktop:0.0.2rc1.dev1`        | TestPyPI | `test`       |
| `v0.0.2`           | `0.0.2`          | `0.0.2`               | `cremind-desktop:0.0.2` + `:latest`    | PyPI     | `production` |

Within a version's slate:
- `rc<N>` indexes the PR (`rc1` for the first PR, `rc2` for the
  second, etc.) and is assigned by the Core Maintainer.
- `dev<M>` increments per bug-fix iteration on that PR (`dev1`,
  `dev2`, …).

Hotfixes use the four-segment form: `v0.0.2.1` (production) is built
from `v0.0.2.1rc<N>.dev<M>` (dev release).

**Strict, single canonical shape.** RC tags must match
`^v(\d+\.\d+\.\d+(?:\.\d+)?)rc(\d+)\.dev(\d+)$` exactly. The legacy
hyphenated `v<X.Y.Z>-rc.<N>[.dev.<M>]` form and the bare
`v<X.Y.Z>rc<N>` (no `.dev<M>`) are both rejected — `release-rc.yml`
refuses to publish them and the in-app updater ignores GitHub
prereleases whose tag fails this regex. The npm/installer column is
the SemVer form `scripts/sync_ui_version.py` derives from the PEP 440
version internally (npm and electron-builder reject the PEP 440
no-separator form as a prerelease identifier); the SemVer string
never appears on GitHub, PyPI, or Docker Hub.

### Version source

[`app/__version__.py`](app/__version__.py)'s `__version__` field is
the single source of truth.

- `pyproject.toml` reads it via
  `[tool.hatch.version] path = "app/__version__.py"`.
- `ui/package.json` is regenerated by
  [`scripts/sync_ui_version.py`](scripts/sync_ui_version.py), wired
  into the `predev` / `prebuild` / `preweb:dev` / `preweb:build` npm
  hooks. Never edit it by hand.
- `release-rc.yml` rewrites `app/__version__.py` in-CI to the PEP 440
  form (the RC tag with the leading `v` stripped, e.g.
  `v0.0.2rc1.dev1` → `0.0.2rc1.dev1`) before building. The rewrite is
  never committed; the prod workflow uses the value as-committed.

`MIN_SUPPORTED_UPGRADE_FROM` in the same file is the oldest version
this build knows how to migrate from. The upgrader refuses to proceed
when the live install is older. Bump it only when explicitly dropping
support, not on every release.

### Required secrets

`PYPI_API_TOKEN`, `TEST_PYPI_API_TOKEN`, `DOCKERHUB_USERNAME`, and
`DOCKERHUB_TOKEN` are required — a missing secret fails the release.
`GITHUB_TOKEN` is auto-provided.

Optional (code-signing):

- `CSC_LINK` — base64-encoded code-signing cert (Windows + macOS).
- `CSC_KEY_PASSWORD`.
- `APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID` — macOS
  notarization.

Missing signing secrets produce *unsigned* builds — the release still
succeeds, but users see SmartScreen / Gatekeeper warnings. The per-job
gating is in `release-prod.yml`'s *Configure code-signing env* step.

Docker Hub setup (one-time):

1. Create the repo on Docker Hub: name `cremind-desktop`, namespace
   `cremind`, visibility **Public**.
2. Create a Personal Access Token: *Account Settings → Security → New
   Access Token*, scope **Read & Write**.
3. Add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` as GitHub repo
   secrets.

### Cheat sheet

**Component Maintainer — per PR**

```powershell
# Open the PR (no version bump on the PR branch)
git checkout -b feat/<name>
# code…
git push -u origin feat/<name>
gh pr create --title "..." --body "..."

# Once Core Maintainer assigns rc<N> for the upcoming v<X.Y.Z>,
# and pr.yml is green:
git tag v<X.Y.Z>rc<N>.dev1 origin/feat/<name>
git push origin v<X.Y.Z>rc<N>.dev1
# install via test channel, validate

# Per bug found, push fix and iterate:
git tag v<X.Y.Z>rc<N>.dev2 origin/feat/<name>
git push origin v<X.Y.Z>rc<N>.dev2

# When validated, merge — MERGE-COMMIT only. NOT --rebase, NOT --squash.
gh pr merge <PR#> --merge
```

**Core Maintainer — per version, after every slated PR is merged**

```powershell
git checkout main
git pull --ff-only
# Edit app/__version__.py: __version__ = "<X.Y.Z>"
python scripts/sync_ui_version.py
git add app/__version__.py ui/package.json
git commit -m "Bump to <X.Y.Z>"
git push origin main
git tag v<X.Y.Z>
git push origin v<X.Y.Z>
# Actions → release-prod → Review deployments → Approve and deploy

# Cleanup after release-prod.yml promotes the draft
git push origin --delete feat/<name-1> feat/<name-2> ...
```
