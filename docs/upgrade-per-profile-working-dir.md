# Upgrading: each profile gets its own working directory

Applies to the first release after **v0.0.18** that ships per-profile working
directories (the same release that ships Documentation search — see
[upgrade-search-tool-rename.md](upgrade-search-tool-rename.md)).

## In one paragraph

The User Working Directory used to be **one server-wide folder**
(`server_config.user_working_dir`, `~/Documents` by default) shared by every
profile: the file panel's root, the default directory of every built-in tool
and terminal, `$CREMIND_USER_WORKING_DIR`, and the folder Documentation search
indexes. So profiles saw — and indexed — each other's files. Now **every
profile has its own folder**, private to it. On an upgraded install the old
folder becomes the **admin's**; every other profile gets a new, empty folder of
its own. Nothing on disk is moved or deleted.

## What happens on the first boot

The Alembic migration `20260929_profile_working_dir` runs with the normal
`upgrade head` at boot:

- the server-wide `server_config.user_working_dir` moves onto the **admin**
  profile (`profiles.working_dir`) and the key is removed. The admin keeps the
  same folder, the same file panel and the same Documentation search index;
- every other profile has no folder stored (NULL), which means its **default
  folder**, `<workspaces folder>/<profile name>` (below). It is created the
  first time it is used, empty;
- nothing on disk moves. Files another profile saved into the old shared folder
  stay there — it is the admin's folder now. Move them into that profile's own
  folder if they belong to it.

The profile name `workspaces` is reserved.

## Where the folders are

Every profile's default folder lives in the **workspaces folder**:

| Install | Workspaces folder | A profile's default folder |
|---|---|---|
| Native | `~/.cremind/workspaces` (`$CREMIND_WORKSPACES_DIR` overrides) | `~/.cremind/workspaces/<profile>` |
| Docker (installer bundle) | `/root/Documents/cremind-workspaces`, inside the documents bind mount | on this machine: `<your Documents folder>/cremind-workspaces/<profile>` |
| Docker, read-only Documents folder | `/root/.cremind/workspaces`, in the `cremind-data` volume | not visible on this machine |
| Kubernetes, `persistence.work` enabled (default) | `<persistence.work.mountPath>/cremind-workspaces` on the work volume | `/root/Documents/cremind-workspaces/<profile>` |
| Kubernetes, work volume disabled | `<cremind.systemDir>/workspaces` on the system volume | `/root/.cremind/workspaces/<profile>` |

Inside a Documents folder it is `cremind-workspaces`, not `workspaces`: every
entry in the workspaces folder is taken to be a profile's (one that belongs to
no profile is off-limits to all of them, and `--purge-workspaces` deletes it),
and `workspaces` is a name people already give folders of their own.

The Docker bundle and the Helm chart set `CREMIND_WORKSPACES_DIR` for you.
**A Docker bundle written by an older installer** has no such line, so its
profiles' folders land in the `cremind-data` volume
(`/root/.cremind/workspaces`) — working, but invisible from the host. Re-run
the installer (it keeps your answers) to put them in your Documents folder; a
folder already created in the volume is not moved, copy it out with
`docker compose cp cremind:/root/.cremind/workspaces/<profile> <dest>`.

The **admin of an upgraded install keeps its old folder** — `~/Documents`
natively, `/root/Documents` (the whole mount) in Docker and Kubernetes. That
folder may contain the workspaces folder (Docker, Kubernetes); each profile's
folder inside it is still that profile's alone, and the admin's file panel and
Documentation search skip them.

## Privacy

A path inside another profile's working directory is off-limits to every file
surface: the file panel and file API, the agent's file tools, working-directory
switches, terminals, file watchers, coding agents, and Documentation search.
**The admin is not exempt** — being admin means being able to *assign* folders,
not to read other profiles' files. Deleted profiles' kept folders
(`<workspaces folder>/.deleted/…`) belong to nobody.

The limit: shell commands (`exec_shell`, terminals) and coding agents run as the
same operating-system user as Cremind, so a command can still read any path the
OS lets that user read. The rule governs Cremind's own file surfaces; it is not
an OS sandbox.

## Changing a profile's folder (admin only)

Only the admin may change a working directory — any profile's, its own
included. Other profiles see theirs read-only.

```bash
cremind profile working-dir                    # your own
cremind profile working-dir bob                # bob's
cremind profile working-dir bob /srv/work/bob  # admin: point bob elsewhere
cremind profile working-dir bob --default      # admin: back to <workspaces>/bob
cremind profile create carol --working-dir /data/carol
```

The folder must be an absolute path; not inside Cremind's system folder, not
inside the workspaces folder (every entry there is some profile's), not an
operating-system location, and not *inside* another profile's folder — pointing
two profiles at the **same** folder is allowed, and is how the admin lets them
share one. Changing a folder does not move files; Documentation search asks
before it drops the old folder's files from the index.

In Docker, to have the admin work in (and search) the whole mounted folder, as
before: `cremind profile working-dir admin /root/Documents`.

## Documentation search

Documentation search now **always indexes the profile's working directory**.
The separate folder choice is gone — the web UI's folder picker,
`cremind docs set-root`, and `--root` on `cremind docs enable`. A profile that
had chosen a different folder is switched to its working directory through the
existing confirmation: Documentation search shows the pending change and removes
nothing from the index until you confirm it. The admin of an upgraded install
indexes the same folder as before, so nothing is re-indexed.

## Deleting a profile

Deleting a profile asks what to do with its folder:

- **keep** (default) — a default-location folder moves to
  `<workspaces>/.deleted/<name>-<timestamp>`, so a new profile of the same
  name starts empty;
- **delete** — `cremind profile delete <name> --delete-working-dir`.

A folder the admin chose elsewhere is never touched either way.

## Backups

`cremind backup create` (and the web UI) now includes the workspaces folder —
every profile's default folder and `.deleted/` — **by default**. Leave it out
with `--no-workspaces` (web UI: untick *Include the profiles' working
directories*). A folder an admin chose *outside* the workspaces folder (the
admin's legacy `~/Documents`) is not archived, as before — back it up yourself;
`backup create` names each one.

A restore puts the folders into the **target's** workspaces folder (a native
backup restored into Docker lands in `/root/Documents/cremind-workspaces`), overwriting
files of the same name and deleting nothing; an archive without them leaves the
target's folders as they are. Stored paths follow: a folder the admin chose is
relocated like every other stored path, and the restore report names any that
does not exist on the new machine. One chosen on the other OS family that
cannot be mapped (`D:\work` restored onto Linux) is reset to the profile's
default folder. A backup made **before** this release restores its server-wide
folder as the admin's.

## Uninstalling

`install.sh --uninstall --purge` / `install.ps1 -Uninstall -Purge` now **keep
the workspaces folder** (everything else goes) and print where it is; a Docker
install's `<Documents>/cremind-workspaces` is never touched, and with a read-only
Documents folder the folders are copied out of the `cremind-data` volume to
`~/cremind-workspaces-<time>` before it is removed. `--purge-workspaces` /
`-PurgeWorkspaces` deletes them too (after typing `delete` on a terminal).
Folders an admin chose elsewhere are never touched; a purge names them.

On **Kubernetes**, `helm uninstall` still deletes the chart's `work` claim, and
every profile's folder with it. Take a backup first, or annotate the claim
`helm.sh/resource-policy: keep` (`persistence.work.annotations`).

## For contributors

- [`app/config/working_dirs.py`](../app/config/working_dirs.py) owns the
  layout: `workspaces_root()`, `profile_working_dir(profile)`, and
  `is_foreign(path, profile)` — the one predicate every file surface asks.
- `app.config.settings.get_user_working_directory(profile)` now **requires**
  the profile. There is no `admin` fallback.
- Backups: the workspaces folder is archived under its own `workspaces/` member
  prefix and restored into the target's root
  ([`app/backup/engine.py`](../app/backup/engine.py)); the manifest records
  `files.workspaces_included`, `source_paths.workspaces_root` and each
  profile's folder (`working_dirs`), and `profiles.working_dir` is a relocated
  path column ([`app/backup/paths.py`](../app/backup/paths.py)).
