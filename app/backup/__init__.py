"""Full-system Backup & Restore.

A ``.cremind-backup`` archive captures the entire Cremind system in an
environment-independent form: a portable logical database dump plus the file
trees under ``CREMIND_SYSTEM_DIR`` (skills, OAuth token files, personas,
per-profile documents, JWT tokens, channel sessions, browser login state).
It restores across environments — Windows-native→Docker/K8s, SQLite→PostgreSQL,
a new home directory — by relocating stored absolute paths to the target's
equivalents. Optional passphrase encryption protects the secrets a backup
necessarily contains.

This differs from ``app.storage.backup`` (``cremind db backup``), which is a
same-backend database-only snapshot the upgrader uses. The public engine surface
lives in :mod:`app.backup.engine`; orchestration (status file, detached runner,
boot-time apply) lives alongside it.

Import discipline: every module here keeps ``app.*`` imports inside function
bodies (except the small intra-package imports), so the offline CLI can drive a
restore with the server stopped.
"""

from app.backup.engine import (
    BackupOptions,
    BackupResult,
    RestoreReport,
    StagedBackup,
    apply_staged_restore,
    create_backup,
    is_encrypted,
    read_manifest,
    restore_backup,
    stage_backup,
    verify_passphrase,
)
from app.backup.manifest import (
    ARCHIVE_SUFFIX,
    BackupError,
    BackupIncompatibleError,
    BackupPassphraseError,
    Manifest,
    assert_restorable,
)

__all__ = [
    "ARCHIVE_SUFFIX",
    "BackupError",
    "BackupIncompatibleError",
    "BackupOptions",
    "BackupPassphraseError",
    "BackupResult",
    "Manifest",
    "RestoreReport",
    "StagedBackup",
    "apply_staged_restore",
    "assert_restorable",
    "create_backup",
    "is_encrypted",
    "read_manifest",
    "restore_backup",
    "stage_backup",
    "verify_passphrase",
]
