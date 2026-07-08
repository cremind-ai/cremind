"""Backup engine — create archives, stage them, and apply a restore.

Public surface (all lazy-import ``app.*`` internals so the offline CLI can use
this with the server stopped):

- :func:`create_backup` — snapshot the whole system into a ``.cremind-backup``
- :func:`read_manifest` / :func:`is_encrypted` / :func:`verify_passphrase`
- :func:`stage_backup` — decrypt+extract+verify into a directory
- :func:`apply_staged_restore` — import the staged DB dump (into the target's
  configured provider), relocate paths, replace file trees
- :func:`restore_backup` — stage+apply in one call (offline / setup-mode path)

Archive layout (tar+gzip, optionally wrapped in the encryption envelope):
``manifest.json`` (first), ``db/dump.jsonl.gz``, ``files/**``, ``inventory.json``
(last). See :mod:`app.backup.manifest`.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.backup import crypto
from app.backup.manifest import (
    ARCHIVE_SUFFIX,
    DB_MEMBER,
    FILES_PREFIX,
    INVENTORY_MEMBER,
    MANIFEST_MEMBER,
    BackupError,
    BackupPassphraseError,
    Manifest,
    SourcePaths,
    assert_restorable,
    now_iso,
)
from app.backup.paths import RelocationReport, build_path_map, transform_row
from app.backup.rules import iter_backup_files, long_path
from app.utils import logger

ProgressFn = Callable[[str, int, int], None]


@dataclass
class BackupOptions:
    dest: Path | None = None
    passphrase: str | None = None
    include_browser_profiles: bool = True


@dataclass
class BackupResult:
    path: Path
    manifest: Manifest
    bytes_written: int
    file_count: int = 0
    skipped: list[str] = field(default_factory=list)


@dataclass
class StagedBackup:
    staging_dir: Path
    manifest: Manifest
    verify_warnings: list[str] = field(default_factory=list)


@dataclass
class RestoreReport:
    ok: bool
    source: dict[str, Any]
    db_row_counts: dict[str, int] = field(default_factory=dict)
    relocations: dict[str, Any] = field(default_factory=dict)
    file_count: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "source": self.source,
            "db_row_counts": self.db_row_counts,
            "relocations": self.relocations,
            "file_count": self.file_count,
            "warnings": self.warnings,
            "error": self.error,
        }


# ── helpers ──────────────────────────────────────────────────────────────────


class _HashingReader(io.RawIOBase):
    """Wrap a binary file so tarfile's read updates a sha256 en passant."""

    def __init__(self, fh, hasher):
        self._fh = fh
        self._hasher = hasher

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        data = self._fh.read(len(b))
        if not data:
            return 0
        b[: len(data)] = data
        self._hasher.update(data)
        return len(data)


def _add_bytes(tf: tarfile.TarFile, arcname: str, data: bytes) -> None:
    info = tarfile.TarInfo(arcname)
    info.size = len(data)
    info.mtime = int(time.time())
    tf.addfile(info, io.BytesIO(data))


def _system_dir() -> str:
    from app.config.settings import BaseConfig

    return BaseConfig.CREMIND_SYSTEM_DIR


def _query_profiles() -> list[str]:
    from sqlalchemy import text

    from app.databases import get_database_provider

    try:
        engine = get_database_provider().sync_engine()
        with engine.connect() as conn:
            return [r[0] for r in conn.execute(text("SELECT name FROM profiles"))]
    except Exception as e:  # noqa: BLE001 — no profiles table on a bare DB
        logger.warning(f"[backup] could not list profiles: {e}")
        return []


def _source_paths() -> SourcePaths:
    from app.config.settings import get_user_working_directory

    try:
        uwd = get_user_working_directory()
    except Exception:  # noqa: BLE001
        uwd = ""
    return SourcePaths(
        system_dir=_system_dir(),
        home_dir=os.path.expanduser("~"),
        user_working_dir=uwd,
        sep=os.sep,
        case_insensitive=(sys.platform == "win32"),
    )


def _default_dest() -> Path:
    from app.__version__ import __version__ as ver

    root = Path(_system_dir()) / "backups"
    root.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    return root / f"cremind-{ver}-{ts}{ARCHIVE_SUFFIX}"


# ── create ─────────────────────────────────────────────────────────────────


def create_backup(options: BackupOptions, progress: ProgressFn | None = None) -> BackupResult:
    from app.__version__ import __version__ as ver
    from app.databases import get_database_provider

    def _p(phase: str, cur: int = 0, total: int = 0) -> None:
        if progress is not None:
            try:
                progress(phase, cur, total)
            except Exception:  # noqa: BLE001
                pass

    system_dir = _system_dir()
    provider = get_database_provider()
    profiles = _query_profiles()

    dest = Path(options.dest) if options.dest else _default_dest()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1. Spool the DB dump first so the manifest can carry revision + row counts.
    _p("dumping")
    spool = dest.parent / (dest.name + ".dump.tmp")
    db_sha = hashlib.sha256()
    try:
        with open(spool, "wb") as sf:
            stats = provider.dump_logical(sf)
        with open(spool, "rb") as sf:
            for chunk in iter(lambda: sf.read(1 << 20), b""):
                db_sha.update(chunk)
        db_size = spool.stat().st_size

        manifest = Manifest(
            app_version=ver,
            alembic_revision=stats.alembic_revision,
            db_provider=provider.name,
            platform=sys.platform,
            hostname=socket.gethostname(),
            source_paths=_source_paths(),
            profiles=profiles,
            db_row_counts=stats.row_counts,
            browser_profiles_included=options.include_browser_profiles,
            encrypted=bool(options.passphrase),
            created_at=now_iso(),
        )

        _p("archiving")
        dest_part = dest.with_suffix(dest.suffix + ".part")
        skipped: list[str] = []
        inventory_files: dict[str, dict[str, Any]] = {}
        file_count = 0

        raw = None
        enc = None
        try:
            if options.passphrase:
                raw = open(dest_part, "wb")
                header, salt, nonce_prefix = crypto.new_header(manifest.to_dict())
                crypto.write_envelope_header(raw, header)
                key, _np = crypto.derive_key_from_header(options.passphrase, header)
                enc = crypto.EncryptingWriter(raw, key, nonce_prefix)
                tf = tarfile.open(fileobj=enc, mode="w|gz")
            else:
                tf = tarfile.open(str(dest_part), mode="w:gz")

            with tf:
                # manifest.json — first member.
                _add_bytes(tf, MANIFEST_MEMBER, json.dumps(manifest.to_dict()).encode("utf-8"))

                # db/dump.jsonl.gz
                info = tarfile.TarInfo(DB_MEMBER)
                info.size = db_size
                info.mtime = int(time.time())
                with open(spool, "rb") as sf:
                    tf.addfile(info, sf)

                # files/**
                for abs_path, arc in iter_backup_files(
                    system_dir, profiles,
                    include_browser_profiles=options.include_browser_profiles,
                ):
                    member = FILES_PREFIX + arc
                    try:
                        tinfo = tf.gettarinfo(name=long_path(abs_path), arcname=member)
                    except OSError as e:
                        skipped.append(f"{arc}: {e}")
                        continue
                    if tinfo is None or not tinfo.isreg():
                        continue
                    hasher = hashlib.sha256()
                    try:
                        with open(long_path(abs_path), "rb") as fh:
                            tf.addfile(tinfo, _HashingReader(fh, hasher))
                    except OSError as e:
                        skipped.append(f"{arc}: {e}")
                        continue
                    inventory_files[arc] = {"sha256": hasher.hexdigest(), "size": tinfo.size}
                    file_count += 1

                # inventory.json — last member.
                inventory = {
                    "db": {"sha256": db_sha.hexdigest(), "size": db_size},
                    "files": inventory_files,
                }
                _add_bytes(tf, INVENTORY_MEMBER, json.dumps(inventory).encode("utf-8"))

            if enc is not None:
                enc.finalize()
        finally:
            if enc is not None:
                try:
                    enc.close()
                except Exception:  # noqa: BLE001
                    pass
            if raw is not None:
                raw.close()

        os.replace(dest_part, dest)
    finally:
        try:
            spool.unlink()
        except OSError:
            pass

    manifest.files_approx_bytes = sum(f["size"] for f in inventory_files.values())
    bytes_written = dest.stat().st_size if dest.exists() else 0
    _p("done")
    logger.info(
        f"[backup] created {dest.name} files={file_count} bytes={bytes_written} "
        f"encrypted={bool(options.passphrase)} skipped={len(skipped)}"
    )
    return BackupResult(
        path=dest, manifest=manifest, bytes_written=bytes_written,
        file_count=file_count, skipped=skipped,
    )


# ── read / verify ─────────────────────────────────────────────────────────


def is_encrypted(archive: Path) -> bool:
    return crypto.is_encrypted(archive)


def verify_passphrase(archive: Path, passphrase: str) -> bool:
    if not crypto.is_encrypted(archive):
        return True
    return crypto.verify_passphrase(archive, passphrase)


def read_manifest(archive: Path, passphrase: str | None = None) -> Manifest:
    """Read the manifest without a full extract.

    Encrypted archives carry a plaintext manifest in the envelope header, so no
    passphrase is needed here even for encrypted files.
    """
    archive = Path(archive)
    if crypto.is_encrypted(archive):
        with open(archive, "rb") as f:
            header = crypto.read_envelope_header(f)
        man = header.get("manifest")
        if not isinstance(man, dict):
            raise BackupError("Encrypted backup header is missing its manifest.")
        return Manifest.from_dict(man)

    with tarfile.open(str(archive), mode="r:gz") as tf:
        for member in tf:
            if member.name == MANIFEST_MEMBER:
                fh = tf.extractfile(member)
                if fh is None:
                    break
                return Manifest.from_dict(json.loads(fh.read().decode("utf-8")))
    raise BackupError("Backup archive has no manifest.json.")


# ── stage ─────────────────────────────────────────────────────────────────


def _open_read_tar(archive: Path, passphrase: str | None):
    """Return an open streaming TarFile for reading (caller closes it)."""
    if crypto.is_encrypted(archive):
        if not passphrase:
            raise BackupPassphraseError("This backup is encrypted; a passphrase is required.")
        f = open(archive, "rb")
        header = crypto.read_envelope_header(f)
        key, nonce_prefix = crypto.derive_key_from_header(passphrase, header)
        reader = crypto.DecryptingReader(f, key, header, nonce_prefix)
        return tarfile.open(fileobj=reader, mode="r|gz"), f
    return tarfile.open(str(archive), mode="r|gz"), None


def _safe_members(tf: tarfile.TarFile):
    for member in tf:
        name = member.name.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            logger.warning(f"[backup:restore] refusing unsafe archive member {member.name!r}")
            continue
        yield member


def stage_backup(archive: Path, passphrase: str | None, dest_dir: Path) -> StagedBackup:
    """Decrypt + extract + verify an archive into ``dest_dir``.

    Staging up front (decrypting now) is what lets the boot-time apply run
    without ever persisting the passphrase across a restart.
    """
    archive = Path(archive)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    tf, raw = _open_read_tar(archive, passphrase)
    try:
        for member in _safe_members(tf):
            tf.extract(member, str(dest_dir))
    finally:
        tf.close()
        if raw is not None:
            raw.close()

    man_path = dest_dir / MANIFEST_MEMBER
    if not man_path.is_file():
        raise BackupError("Staged backup is missing manifest.json.")
    manifest = Manifest.from_dict(json.loads(man_path.read_text(encoding="utf-8")))

    warnings = _verify_inventory(dest_dir)
    return StagedBackup(staging_dir=dest_dir, manifest=manifest, verify_warnings=warnings)


def _verify_inventory(staged_dir: Path) -> list[str]:
    inv_path = staged_dir / INVENTORY_MEMBER
    if not inv_path.is_file():
        return ["inventory.json missing — integrity not verified"]
    inv = json.loads(inv_path.read_text(encoding="utf-8"))
    warnings: list[str] = []

    # DB dump integrity is load-bearing — hard fail on mismatch.
    db_member = staged_dir / DB_MEMBER
    expected_db = (inv.get("db") or {}).get("sha256")
    if expected_db and db_member.is_file():
        got = _sha256_file(db_member)
        if got != expected_db:
            raise BackupError("Database dump failed integrity check (sha256 mismatch).")

    # File integrity is best-effort — warn and continue.
    for arc, meta in (inv.get("files") or {}).items():
        fp = staged_dir / FILES_PREFIX / arc
        if not fp.is_file():
            warnings.append(f"missing file: {arc}")
            continue
        exp = meta.get("sha256")
        if exp and _sha256_file(fp) != exp:
            warnings.append(f"checksum mismatch: {arc}")
    return warnings


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(long_path(str(path)), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── apply ──────────────────────────────────────────────────────────────────


def apply_staged_restore(staged_dir: Path, *, target_system_dir: str) -> RestoreReport:
    """Import the staged DB dump into the target's provider, relocate paths, and
    replace the file trees under ``target_system_dir``.

    Leaves the DB at head. Resolves the database provider from the *target's*
    bootstrap (which is how SQLite→Postgres works). Disposes the engine at the
    end so the subsequent boot rebuilds a clean provider.
    """
    from app.databases import get_database_provider, set_database_provider
    from app.storage import migrations

    staged_dir = Path(staged_dir)
    man_path = staged_dir / MANIFEST_MEMBER
    manifest = Manifest.from_dict(json.loads(man_path.read_text(encoding="utf-8")))
    assert_restorable(manifest)

    from app.backup.dbdump import drop_all_tables

    report = RelocationReport()
    provider = get_database_provider()
    engine = provider.sync_engine()
    target_home = os.path.expanduser("~")
    pm = build_path_map(manifest, target_system_dir, target_home)
    load_stats = None
    try:
        drop_all_tables(engine)
        migrations.upgrade(manifest.alembic_revision or "head")

        def _xform(table: str, row: dict[str, Any]) -> dict[str, Any]:
            return transform_row(pm, table, row, report)

        dump_path = staged_dir / DB_MEMBER
        with open(dump_path, "rb") as fh:
            load_stats = provider.load_logical(fh, row_transform=_xform)

        migrations.upgrade("head")
    finally:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001
            pass
        set_database_provider(None)

    # Replace file trees (merge-overwrite into the target system dir).
    file_count = _restore_file_trees(staged_dir, target_system_dir)

    warnings: list[str] = []
    if report.unmapped:
        warnings.append(
            f"{len(report.unmapped)} stored path(s) point outside the backed-up "
            f"home/system directories and were left unchanged; processes that use "
            f"them may fail until fixed."
        )

    logger.info(
        f"[backup:restore] applied db_rows={sum((load_stats.row_counts if load_stats else {}).values())} "
        f"files={file_count} relocated={len(report.relocated)} unmapped={len(report.unmapped)}"
    )
    return RestoreReport(
        ok=True,
        source=manifest.summary(),
        db_row_counts=(load_stats.row_counts if load_stats else {}),
        relocations=report.to_dict(),
        file_count=file_count,
        warnings=warnings,
    )


def _restore_file_trees(staged_dir: Path, target_system_dir: str) -> int:
    files_root = Path(staged_dir) / "files"
    if not files_root.is_dir():
        return 0
    target = Path(target_system_dir)
    count = 0
    for dirpath, _dirs, filenames in os.walk(str(files_root)):
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, str(files_root))
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(long_path(src), long_path(str(dst)))
                count += 1
            except OSError as e:
                logger.warning(f"[backup:restore] could not restore file {rel}: {e}")
    return count


def restore_backup(
    archive: Path, passphrase: str | None = None, *, target_system_dir: str
) -> RestoreReport:
    """One-shot stage + apply, cleaning up the temp staging dir.

    Used by the offline CLI and the setup-mode (fresh install) path, where
    there is no live server to quiesce and no restart is needed.
    """
    tmp = Path(tempfile.mkdtemp(prefix="cremind-restore-"))
    try:
        staged = stage_backup(Path(archive), passphrase, tmp)
        report = apply_staged_restore(staged.staging_dir, target_system_dir=target_system_dir)
        report.warnings.extend(staged.verify_warnings)
        return report
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


__all__ = [
    "BackupOptions",
    "BackupResult",
    "RestoreReport",
    "StagedBackup",
    "apply_staged_restore",
    "create_backup",
    "is_encrypted",
    "read_manifest",
    "restore_backup",
    "stage_backup",
    "verify_passphrase",
]
