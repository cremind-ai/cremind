"""Temporary upload folder for files attached from the Agent chat composer.

Files attached in the conversation UI are uploaded to a per-conversation
temporary directory under the profile's slice of ``CREMIND_SYSTEM_DIR``::

    <CREMIND_SYSTEM_DIR>/<profile>/uploads_tmp/<conversation_id>/<files>

That location already sits inside an allowed root for the ``system_file``
tool group (see ``app.tools.builtin.system_file._allowed_roots``), so the
Agent can read / convert / move uploaded files with absolute paths without
any widening of the trust boundary.

The tree is ephemeral by design:

* ``wipe_all_on_startup`` clears every profile's ``uploads_tmp`` on boot —
  pending uploads don't survive a restart (a file the user asked to *save*
  has already been moved into their working directory by then).
* ``prune_idle`` removes per-conversation directories whose newest file has
  not been touched within the inactivity threshold, run periodically by
  ``app.events.uploads_cleanup``.

Knobs (read via ``BaseConfig.get_server_config`` so a SQLite override wins
over the default):

* ``uploads.tmp_idle_minutes``           — idle threshold before pruning (60)
* ``uploads.tmp_prune_interval_minutes`` — how often the pruner runs (15)
* ``uploads.tmp_max_bytes``              — per-file upload ceiling (100 MiB)
"""

from __future__ import annotations

import glob
import os
import shutil
import time

from app.config.settings import BaseConfig
from app.utils.logger import logger

UPLOADS_TMP_DIRNAME = "uploads_tmp"

# Defaults; overridable via ``server_config`` SQLite keys of the same name.
_DEFAULT_IDLE_MINUTES = 60
_DEFAULT_PRUNE_INTERVAL_MINUTES = 15
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB


# ── config knobs ─────────────────────────────────────────────────────────

def idle_threshold_seconds() -> float:
    """Inactivity threshold (seconds) before a conversation temp dir is pruned."""
    minutes = _as_number(
        BaseConfig.get_server_config("uploads.tmp_idle_minutes", _DEFAULT_IDLE_MINUTES),
        _DEFAULT_IDLE_MINUTES,
    )
    return max(1.0, minutes) * 60.0


def prune_interval_seconds() -> float:
    """How often (seconds) the periodic pruner wakes up."""
    minutes = _as_number(
        BaseConfig.get_server_config(
            "uploads.tmp_prune_interval_minutes", _DEFAULT_PRUNE_INTERVAL_MINUTES,
        ),
        _DEFAULT_PRUNE_INTERVAL_MINUTES,
    )
    return max(1.0, minutes) * 60.0


def max_upload_bytes() -> int:
    """Per-file ceiling for a temporary upload."""
    value = _as_number(
        BaseConfig.get_server_config("uploads.tmp_max_bytes", _DEFAULT_MAX_BYTES),
        _DEFAULT_MAX_BYTES,
    )
    return max(1, int(value))


def _as_number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ── path helpers ─────────────────────────────────────────────────────────

def uploads_tmp_root(profile: str) -> str:
    """Return ``<CREMIND_SYSTEM_DIR>/<profile>/uploads_tmp`` (not created)."""
    return os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, profile, UPLOADS_TMP_DIRNAME)


def _conversation_tmp_path(profile: str, conversation_id: str) -> str:
    """Compute (without creating) the temp dir path for one conversation.

    ``conversation_id`` is server-generated but never trusted: it is reduced
    to its basename and rejected if empty / ``.`` / ``..`` so a crafted value
    can never escape the profile's ``uploads_tmp`` root.
    """
    if not profile:
        raise ValueError("profile is required for a temp upload directory")
    cid = os.path.basename((conversation_id or "").strip())
    if not cid or cid in (".", ".."):
        raise ValueError(f"invalid conversation_id for temp upload: {conversation_id!r}")
    return os.path.join(uploads_tmp_root(profile), cid)


def conversation_tmp_dir(profile: str, conversation_id: str) -> str:
    """Return (creating it) the temp dir for one conversation's uploads."""
    target = _conversation_tmp_path(profile, conversation_id)
    os.makedirs(target, exist_ok=True)
    return target


def is_inside_conversation_tmp(profile: str, conversation_id: str, abs_path: str) -> bool:
    """True iff ``abs_path`` resolves inside this conversation's temp dir.

    Used to validate client-supplied attachment paths before they are handed
    to the Agent — a path outside the conversation's own temp dir is dropped.
    Pure check: does not create the directory.
    """
    if not abs_path:
        return False
    try:
        base = os.path.realpath(_conversation_tmp_path(profile, conversation_id))
    except ValueError:
        return False
    target = os.path.realpath(abs_path)
    return target == base or target.startswith(base + os.sep)


# ── cleanup ──────────────────────────────────────────────────────────────

def wipe_all_on_startup() -> int:
    """Remove every profile's ``uploads_tmp`` tree. Returns dirs removed."""
    base = BaseConfig.CREMIND_SYSTEM_DIR
    if not base or not os.path.isdir(base):
        return 0
    removed = 0
    for path in glob.glob(os.path.join(base, "*", UPLOADS_TMP_DIRNAME)):
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(f"uploads_tmp: failed to remove {path} on startup: {exc}")
    if removed:
        logger.info(f"uploads_tmp: cleared {removed} temp upload tree(s) on startup")
    return removed


def prune_idle(threshold_seconds: float | None = None) -> int:
    """Remove per-conversation temp dirs idle beyond the threshold.

    A directory is idle when the newest mtime anywhere in its subtree (with
    the directory's own mtime as a floor for an empty dir) is older than
    ``threshold_seconds``. A dir holding a file mid-write keeps a fresh mtime
    and is left alone. Returns the number of conversation dirs removed.
    """
    base = BaseConfig.CREMIND_SYSTEM_DIR
    if not base or not os.path.isdir(base):
        return 0
    threshold = threshold_seconds if threshold_seconds is not None else idle_threshold_seconds()
    now = time.time()
    removed = 0
    for conv_dir in glob.glob(os.path.join(base, "*", UPLOADS_TMP_DIRNAME, "*")):
        if not os.path.isdir(conv_dir):
            continue
        newest = _newest_mtime(conv_dir)
        if now - newest <= threshold:
            continue
        try:
            shutil.rmtree(conv_dir, ignore_errors=True)
            removed += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(f"uploads_tmp: failed to prune {conv_dir}: {exc}")
    if removed:
        logger.info(f"uploads_tmp: pruned {removed} idle temp upload dir(s)")
    return removed


def _newest_mtime(directory: str) -> float:
    """Newest mtime in ``directory`` and its subtree (dir mtime as a floor)."""
    newest = 0.0
    try:
        newest = os.stat(directory).st_mtime
    except OSError:
        return 0.0
    for root, _dirs, files in os.walk(directory):
        for name in files:
            try:
                mtime = os.stat(os.path.join(root, name)).st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    return newest
