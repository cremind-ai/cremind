"""Discovery: which files under a local root exist, and what changed since the index last looked.

The first stage of the User Document Search pipeline, and the only one that
touches the user's directory tree as a tree. Everything here is synchronous
and thread-safe, because it runs in the engine's worker threads, and none of it
touches a database: the caller hands in the manifest (what the index knows) and
gets plain :mod:`app.userdocs.types` records back.

- :mod:`.ignore` — :class:`~.ignore.IgnoreMatcher`, the three-layer rule of
  what is never indexed, what is indexed by name only, and what is read.
- :mod:`.walker` — :func:`~.walker.walk`, an iterative, symlink-safe,
  placeholder-aware walk; :func:`~.walker.path_hash`, the manifest key.
- :mod:`.hashing` — streamed sha256 and the head+tail quick hash.
- :mod:`.guard` — :class:`~.guard.RootGuard`: "the root vanished" and "half
  the files vanished" must hold the index, never purge it.
- :mod:`.moves` — renames and moves reuse the indexed content.
- :mod:`.scan` — :func:`~.scan.scan_diff`, the full reconcile (boot, every
  few hours, and the poller when native watching is unavailable).
- :mod:`.watcher` — :class:`~.watcher.SourceWatcher`, debounced and
  stability-checked native change notifications.
- :mod:`.projects` — :func:`~.projects.detect_project`, "is this folder a
  code project, and what is it made of".

Submodules are deliberately not imported here: the watcher pulls in watchdog,
and a caller that only needs ``path_hash`` should not pay for it.
"""
