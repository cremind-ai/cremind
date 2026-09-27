"""Text extraction for Documentation search: out of process, in memory.

Parsing a user's files means running PDF, Office and image decoders over input
nobody vetted. One malformed PDF can spin a core for minutes or balloon to
gigabytes, and a native decoder can segfault. None of that may reach the server,
so the parsing runs in child processes:

- :mod:`.detect` names a file's kind from its magic bytes. The extension only
  breaks ties, because users rename files and cloud exports lie.
- :mod:`.dispatch` routes an :class:`~app.documents.types.ExtractRequest` to the
  per-format extractor in :mod:`.formats` and turns every failure into an
  :class:`~app.documents.types.ExtractResult` with a reason, never an exception.
- :mod:`.worker` is the child's entry point (``python -m
  app.documents.extract.worker``). It speaks length-prefixed JSON over its pipes,
  watches its own memory, and exits when it grows too large.
- :mod:`.pool` is the parent's side. It keeps a few long-lived workers, enforces
  a deadline on every file, and replaces a worker that hangs, crashes or runs
  out of memory.

Two rules hold everywhere here:

- **Nothing is written** anywhere except a worker's own temp dir. Input files are
  opened read-only, and never converted in place or through a temp copy.
- **The worker imports nothing heavy until it has to.** This package's
  ``__init__`` is imported before the worker can isolate its stdout, so it
  imports nothing. For the same reason the modules that run inside the worker
  (``detect``, ``dispatch``, ``formats``) log through ``loguru`` directly, not
  ``app.utils.logger``. The loguru ``logger`` is one process-wide object, so in
  the server their records reach the sinks ``app.utils.logger`` configured.
  In a worker, importing ``app.utils.logger`` would drag in about 800 modules
  (SQLAlchemy, Alembic, the config stack) and open a second writer on
  ``logs/app.log``.
"""

# Stored on every indexed file (as the version of its kind, see
# :func:`extractor_version`). Bump it when extraction output changes for the
# same input of every kind (a fixed shared parser); bump one kind's entry in
# ``_KIND_VERSIONS`` when only that kind changed. A file indexed by an older
# version is re-extracted the next time it goes through the pipeline instead
# of being taken as unchanged; a kind whose upgrade must reach every file at
# once is also queued by the engine (``ProfileRuntime.queue_stale_extraction``).
EXTRACTOR_VERSION = 1

# 2 (pdf): the whole scanned-page inventory is recorded, OCR runs in batches
# the engine resumes, and native text and transcriptions are chunked together.
_KIND_VERSIONS = {"pdf": 2}


def extractor_version(kind: str | None) -> int:
    """The extractor version a file of ``kind`` is indexed with today."""
    return _KIND_VERSIONS.get(kind or "", EXTRACTOR_VERSION)
