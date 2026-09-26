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
``manifest.json`` (first), ``db/dump.jsonl.gz``, ``files/**``, ``workspaces/**``
(the profiles' working directories, unless ``include_workspaces=False``),
``inventory.json`` (last). See :mod:`app.backup.manifest`.

``files/**`` restores into the target's system dir and ``workspaces/**`` into
the target's workspaces root (:func:`_target_workspaces_root`) — the same
folder only when neither side sets ``CREMIND_WORKSPACES_DIR``. Both merge:
a restore overwrites files the archive carries and deletes nothing, so an
archive without the working directories leaves the ones on disk as they are.
Folders an admin chose outside the workspaces root are never archived.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
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
    WORKSPACES_PREFIX,
    BackupError,
    BackupPassphraseError,
    Manifest,
    SourcePaths,
    assert_restorable,
    now_iso,
)
from app.backup.paths import RelocationReport, build_path_map, transform_row
from app.backup.rules import iter_backup_files, iter_workspace_files, long_path
from app.utils import logger

ProgressFn = Callable[[str, int, int], None]


@dataclass
class BackupOptions:
    dest: Path | None = None
    passphrase: str | None = None
    include_browser_profiles: bool = True
    # Every profile's default-location working directory (the workspaces
    # root, deleted profiles' kept folders included). ``--no-workspaces``.
    include_workspaces: bool = True


@dataclass
class BackupResult:
    path: Path
    manifest: Manifest
    bytes_written: int
    file_count: int = 0
    skipped: list[str] = field(default_factory=list)
    # Of ``file_count``, how many came from the workspaces root.
    workspaces_file_count: int = 0


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
    return [name for name, _uid in _query_profile_rows()]


def _query_profile_rows(engine: Any = None) -> list[tuple[str, str]]:
    """``(name, uuid)`` of every profile. The uuid keys the profile's Cremind
    manual pages (``storage/cremind_documents/profiles/<uuid>``), which live
    outside its name-keyed tree."""
    from sqlalchemy import text

    try:
        if engine is None:
            from app.databases import get_database_provider

            engine = get_database_provider().sync_engine()
        with engine.connect() as conn:
            return [
                (str(r[0]), str(r[1]))
                for r in conn.execute(text("SELECT name, id FROM profiles"))
                if r[0] and r[1]
            ]
    except Exception as e:  # noqa: BLE001 — no profiles table on a bare DB
        logger.warning(f"[backup] could not list profiles: {e}")
        return []


def _workspaces_root() -> str:
    from app.config.working_dirs import workspaces_root

    return workspaces_root()


def _target_workspaces_root(target_system_dir: str) -> str:
    """Where a restore puts the archived working directories: the configured
    ``CREMIND_WORKSPACES_DIR``, else ``<target system dir>/workspaces`` —
    what :func:`app.config.working_dirs.workspaces_root` answers once the
    target runs, spelled against ``target_system_dir`` so a restore into a
    directory other than the live one stays self-consistent."""
    from app.config.working_dirs import WORKSPACES_DIRNAME, WORKSPACES_ENV

    if (os.environ.get(WORKSPACES_ENV) or "").strip():
        return _workspaces_root()
    return os.path.normpath(os.path.join(target_system_dir, WORKSPACES_DIRNAME))


def _stored_working_dirs(engine: Any = None) -> dict[str, str]:
    """``{profile: folder an admin chose}``, read from the DB so the offline
    CLI sees it too; a profile on its default folder is absent. Empty when
    the column does not exist yet (a DB not at head) or there is no table."""
    from sqlalchemy import text

    try:
        if engine is None:
            from app.databases import get_database_provider

            engine = get_database_provider().sync_engine()
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name, working_dir FROM profiles WHERE working_dir IS NOT NULL")
            ).all()
    except Exception:  # noqa: BLE001 — no column yet, no table
        return {}
    return {str(n): str(v).strip() for n, v in rows if n and isinstance(v, str) and v.strip()}


def _working_dirs_record(
    profiles: list[str], workspaces_root: str, included: bool
) -> dict[str, dict[str, Any]]:
    """Each profile's working directory for the manifest (see
    :attr:`Manifest.working_dirs`). Nothing is created."""
    from app.config.working_dirs import default_working_dir, is_inside, real_path

    stored = _stored_working_dirs()
    root = real_path(workspaces_root) if workspaces_root else ""
    record: dict[str, dict[str, Any]] = {}
    for name in profiles:
        chosen = stored.get(name)
        try:
            path = (
                os.path.normpath(os.path.abspath(os.path.expanduser(chosen)))
                if chosen else default_working_dir(name)
            )
        except ValueError:  # a pseudo profile: no working directory
            continue
        inside = bool(root) and is_inside(real_path(path), root)
        record[name] = {
            "path": path,
            "default": not chosen,
            "in_workspaces": inside,
            "archived": inside and included,
        }
    return record


def _source_paths() -> SourcePaths:
    """``user_working_dir`` starts as the admin's default folder; the caller
    replaces it with the admin's actual folder once the profiles are read."""
    from app.config.working_dirs import default_working_dir

    try:
        ws = _workspaces_root()
    except Exception:  # noqa: BLE001
        ws = ""
    try:
        admin_default = default_working_dir("admin")
    except Exception:  # noqa: BLE001
        admin_default = ""
    return SourcePaths(
        system_dir=_system_dir(),
        home_dir=os.path.expanduser("~"),
        user_working_dir=admin_default,
        sep=os.sep,
        case_insensitive=(sys.platform == "win32"),
        workspaces_root=ws,
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
    profile_rows = _query_profile_rows()
    profiles = [name for name, _uid in profile_rows]
    profile_uids = [uid for _name, uid in profile_rows]
    source_paths = _source_paths()
    include_workspaces = bool(options.include_workspaces and source_paths.workspaces_root)
    working_dirs = _working_dirs_record(profiles, source_paths.workspaces_root, include_workspaces)
    if "admin" in working_dirs:
        # Informational (see SourcePaths): where the admin's files are.
        source_paths.user_working_dir = working_dirs["admin"]["path"]

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
            source_paths=source_paths,
            profiles=profiles,
            db_row_counts=stats.row_counts,
            browser_profiles_included=options.include_browser_profiles,
            workspaces_included=include_workspaces,
            working_dirs=working_dirs,
            encrypted=bool(options.passphrase),
            created_at=now_iso(),
        )

        _p("archiving")
        dest_part = dest.with_suffix(dest.suffix + ".part")
        skipped: list[str] = []
        inventory_files: dict[str, dict[str, Any]] = {}
        inventory_workspaces: dict[str, dict[str, Any]] = {}
        file_count = 0

        def _add_file(abs_path: str, member: str, rel: str, inventory: dict[str, dict[str, Any]]) -> bool:
            try:
                tinfo = tf.gettarinfo(name=long_path(abs_path), arcname=member)
            except OSError as e:
                skipped.append(f"{member}: {e}")
                return False
            if tinfo is None or not tinfo.isreg():
                return False
            hasher = hashlib.sha256()
            try:
                with open(long_path(abs_path), "rb") as fh:
                    tf.addfile(tinfo, _HashingReader(fh, hasher))
            except OSError as e:
                skipped.append(f"{member}: {e}")
                return False
            inventory[rel] = {"sha256": hasher.hexdigest(), "size": tinfo.size}
            return True

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
                    profile_uids=profile_uids,
                ):
                    if _add_file(abs_path, FILES_PREFIX + arc, arc, inventory_files):
                        file_count += 1

                # workspaces/** — every profile's default working directory.
                # The system dir is pruned in case the root contains it, and
                # the archive's own spool/part files in case it was pointed
                # into the root.
                if manifest.workspaces_included:
                    for abs_path, rel in iter_workspace_files(
                        source_paths.workspaces_root,
                        exclude_dirs=(system_dir,),
                        exclude_files=(str(dest_part), str(spool)),
                    ):
                        if _add_file(abs_path, WORKSPACES_PREFIX + rel, rel, inventory_workspaces):
                            file_count += 1

                # inventory.json — last member.
                inventory = {
                    "db": {"sha256": db_sha.hexdigest(), "size": db_size},
                    "files": inventory_files,
                    "workspaces": inventory_workspaces,
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

    manifest.files_approx_bytes = sum(
        f["size"] for f in (*inventory_files.values(), *inventory_workspaces.values())
    )
    bytes_written = dest.stat().st_size if dest.exists() else 0
    _p("done")
    logger.info(
        f"[backup] created {dest.name} files={file_count} bytes={bytes_written} "
        f"encrypted={bool(options.passphrase)} skipped={len(skipped)} "
        f"workspaces={'%d file(s)' % len(inventory_workspaces) if manifest.workspaces_included else 'excluded'}"
    )
    return BackupResult(
        path=dest, manifest=manifest, bytes_written=bytes_written,
        file_count=file_count, skipped=skipped,
        workspaces_file_count=len(inventory_workspaces),
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
    """Members that stay inside the staging dir. A backup only ever holds
    regular files (and the directories tar implies), so links and devices are
    refused too: a crafted ``workspaces/x -> /etc`` link would otherwise let
    a later member, or the copy into the target, write outside it."""
    for member in tf:
        name = member.name.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/") or re.match(r"^[A-Za-z]:", name):
            logger.warning(f"[backup:restore] refusing unsafe archive member {member.name!r}")
            continue
        if not (member.isfile() or member.isdir()):
            logger.warning(f"[backup:restore] refusing non-file archive member {member.name!r}")
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
    for section, prefix in (("files", FILES_PREFIX), ("workspaces", WORKSPACES_PREFIX)):
        for arc, meta in (inv.get(section) or {}).items():
            fp = staged_dir / prefix / arc
            label = arc if section == "files" else f"{WORKSPACES_PREFIX}{arc}"
            if not fp.is_file():
                warnings.append(f"missing file: {label}")
                continue
            exp = meta.get("sha256")
            if exp and _sha256_file(fp) != exp:
                warnings.append(f"checksum mismatch: {label}")
    return warnings


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(long_path(str(path)), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ── apply ──────────────────────────────────────────────────────────────────


def _close_out_undelivered_event_results(engine: Any) -> int:
    """Stop restored event results from being reported into live chats.

    ``event_runs`` travels inside every archive, delivery state and all, and the
    exactly-once lock IS one of its columns — so a restore rewinds it. Without
    this the next boot's sweep would report an archive's owed results into
    whatever conversations survived, including a Cremind room or a Telegram
    group, and would report a second time anything delivered after the backup
    was taken.

    ``pending`` rows are left alone: they hold no result yet (the run stopped to
    ask a question), so a human answering later produces a fresh outcome rather
    than a replay. Returns the number closed out, or -1 when it could not run.
    """
    from sqlalchemy import update as sa_update

    from app.storage.models import EventRunModel

    now_ms = time.time() * 1000
    try:
        with engine.begin() as conn:
            result = conn.execute(
                sa_update(EventRunModel.__table__)
                .where(
                    EventRunModel.__table__.c.deliver_to_origin.is_(True),
                    EventRunModel.__table__.c.origin_delivered_at.is_(None),
                    EventRunModel.__table__.c.status != "pending",
                )
                .values(origin_delivered_at=now_ms, origin_delivery_mode="skipped")
            )
        return int(result.rowcount or 0)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[backup:restore] could not close out undelivered event results",
            exc_info=True,
        )
        return -1


def apply_staged_restore(
    staged_dir: Path,
    *,
    target_system_dir: str,
    close_out_owed_results: bool = True,
) -> RestoreReport:
    """Import the staged DB dump into the target's provider, relocate paths, and
    replace the file trees under ``target_system_dir``.

    Leaves the DB at head. Resolves the database provider from the *target's*
    bootstrap (which is how SQLite→Postgres works). Disposes the engine at the
    end so the subsequent boot rebuilds a clean provider.

    ``close_out_owed_results`` is what stops an archive's event results from
    being replayed into live conversations. A ROLLBACK passes ``False``: it
    restores this install's own state from minutes ago, so its owed results are
    genuinely still owed and discarding them would be the rollback quietly
    breaking flows it was supposed to leave untouched.
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
    target_workspaces = _target_workspaces_root(target_system_dir)
    pm = build_path_map(
        manifest, target_system_dir, target_home, target_workspaces_root=target_workspaces,
    )

    # The JWT signing secret is local to this installation, not part of the
    # backup: capture it before the DB is wiped (generate one if this is a
    # fresh, pre-setup target) so it can be re-pinned after the load. Restoring
    # the archive's secret instead would invalidate every token this install
    # already issued — a 401 across the board on the next boot.
    local_secret = _read_local_jwt_secret(engine) or secrets.token_urlsafe(32)

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

        # After the head upgrade, so the delivery columns exist even when the
        # archive predates them.
        closed_out = (
            _close_out_undelivered_event_results(engine)
            if close_out_owed_results else 0
        )

        # Re-pin the local secret — overwrites any value an older archive
        # carried and guarantees the row exists for newer archives that omit it
        # — then re-mint each restored profile's token file under it.
        from app.storage.dynamic_config_storage import DynamicConfigStorage

        DynamicConfigStorage(provider).set(
            "server_config", "jwt_secret", local_secret, is_secret=True
        )
        _reissue_profile_tokens(engine, target_system_dir, local_secret)
        # The restored profiles' (name, uuid): what the relocation below maps
        # an older archive's name-keyed manual pages with.
        restored_profiles = _query_profile_rows(engine)
    finally:
        try:
            engine.dispose()
        except Exception:  # noqa: BLE001
            pass
        set_database_provider(None)

    # Replace file trees (merge-overwrite into the target system dir).
    file_count = _restore_file_trees(staged_dir, target_system_dir)
    # The profiles' working directories, into THIS install's workspaces root
    # (merge-overwrite too: nothing already there is deleted).
    workspace_count = _restore_workspaces(staged_dir, target_workspaces)
    file_count += workspace_count

    warnings: list[str] = [
        "JWT signing secret and session tokens were kept local to this "
        "installation (not restored from the backup); token files were "
        "re-issued for each restored profile."
    ]
    warnings.extend(_workspace_notes(manifest, target_workspaces, workspace_count))

    # An archive from before the Cremind manual moved puts each profile's
    # pages back at ``<profile>/documents``: move them to the uuid-keyed
    # directory now, while nothing runs. The next boot runs the same
    # relocation again (a no-op by then, or the finish of what this could
    # not do) BEFORE any document service starts.
    try:
        from app.documents.relocate import run_after_restore

        moved = run_after_restore(target_system_dir, restored_profiles)
        for err in moved.errors:
            warnings.append(str(err.get("error") or err))
        if moved.busy:
            warnings.append(
                "Document folders could not be relocated right after the restore "
                "(another process held the lock); the next start relocates them."
            )
    except Exception as e:  # noqa: BLE001 — the boot relocation is the backstop
        logger.warning(f"[backup:restore] post-restore document relocation failed: {e}")
        warnings.append(f"Document folders were not relocated after the restore ({e}); the next start retries.")
    reset = [u for u in report.unmapped if u.get("reset_to_default")]
    unmapped = [u for u in report.unmapped if not u.get("reset_to_default")]
    if reset:
        chosen = ", ".join(f"'{u.get('profile') or '?'}' ({u.get('value')})" for u in reset)
        warnings.append(
            f"The working directory an admin chose for {chosen} belongs to another "
            f"operating system and could not be mapped here; those profiles now use "
            f"their default folder in {target_workspaces}. An admin can choose another."
        )
    if unmapped:
        warnings.append(
            f"{len(unmapped)} stored path(s) point outside the backed-up "
            f"home/system directories and were left unchanged; processes that use "
            f"them may fail until fixed."
        )
    warnings.extend(_chosen_dir_notes(report, target_workspaces))
    if closed_out > 0:
        warnings.append(
            f"{closed_out} automation result(s) that had not yet reached their "
            f"conversation were closed out (marked 'skipped') and will not be "
            f"reported — a restore never replays results into chats, rooms or "
            f"channel groups."
        )
    elif closed_out < 0:
        warnings.append(
            "Could not close out the archive's undelivered automation results; "
            "the next boot may report results from this backup into live "
            "conversations."
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


def _read_local_jwt_secret(engine) -> str:
    """Read the target's current ``jwt_secret`` before the DB is wiped.

    Returns ``""`` when there is no ``server_config`` row yet (a fresh install
    that hasn't completed setup) — the caller then generates one.
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT value FROM server_config WHERE key = 'jwt_secret'")
            ).first()
        return (row[0] if row else "") or ""
    except Exception:  # noqa: BLE001 — table may not exist on a bare DB
        return ""


def _reissue_profile_tokens(engine, target_system_dir: str, secret: str) -> None:
    """Re-mint every restored profile's JWT token file under the local secret.

    The ``tokens/`` tree is not restored from the archive (those tokens were
    signed with the *source's* secret). Instead each restored profile gets a
    fresh recovery token signed with THIS installation's secret, so the token
    files stay valid for CLI use / login-by-paste. Best-effort per profile — a
    single failure must never abort the restore.

    Each token is minted at the serial the **restored** row carries, read from
    the restore engine rather than the live one. Minting at 0 instead would
    produce tokens that every decode site rejects on sight.
    """
    from sqlalchemy import text

    from app.config.settings import BaseConfig

    try:
        with engine.connect() as conn:
            try:
                rows = [
                    (r[0], int(r[1] or 0))
                    for r in conn.execute(text("SELECT name, token_serial FROM profiles"))
                ]
            except Exception:  # noqa: BLE001
                # Archive predates the revocation column. Migrations run later
                # (``ensure_at_head`` from ConversationStorage.initialize), and
                # they backfill these rows to 0 — which is what we mint at.
                rows = [
                    (r[0], 0) for r in conn.execute(text("SELECT name FROM profiles"))
                ]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[backup:restore] could not list profiles to re-mint tokens: {e}")
        return

    tokens_dir = Path(target_system_dir) / "tokens"
    for name, serial in rows:
        try:
            tokens_dir.mkdir(parents=True, exist_ok=True)
            token, _exp = BaseConfig.mint_token(name, secret=secret, serial=serial)
            (tokens_dir / f"{name}.token").write_text(token, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[backup:restore] could not re-mint token for profile {name!r}: {e}")


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
            # Never restore archived JWT token files — they were signed with the
            # source's secret. Restore re-mints them under the local secret
            # (see _reissue_profile_tokens). Guards older archives that still
            # carry a tokens/ tree.
            rel_posix = rel.replace(os.sep, "/")
            if rel_posix == "tokens" or rel_posix.startswith("tokens/"):
                continue
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(long_path(src), long_path(str(dst)))
                count += 1
            except OSError as e:
                logger.warning(f"[backup:restore] could not restore file {rel}: {e}")
    return count


def _restore_workspaces(staged_dir: Path, target_root: str) -> int:
    """Copy ``workspaces/**`` into ``target_root``, overwriting files the
    archive carries and deleting nothing — an archive made without the
    working directories has no such member and leaves them untouched."""
    src_root = Path(staged_dir) / WORKSPACES_PREFIX.rstrip("/")
    if not src_root.is_dir() or not target_root:
        return 0
    target = Path(target_root)
    count = 0
    for dirpath, _dirs, filenames in os.walk(str(src_root)):
        for fn in filenames:
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, str(src_root))
            dst = target / rel
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(long_path(src), long_path(str(dst)))
                count += 1
            except OSError as e:
                logger.warning(f"[backup:restore] could not restore working-directory file {rel}: {e}")
    return count


def _workspace_notes(manifest: Manifest, target_root: str, restored: int) -> list[str]:
    """What the restore did with the profiles' working directories, for the
    report: nothing (not in the archive), or where they went when that is not
    where the backup found them."""
    if not manifest.workspaces_included:
        return [
            "This backup does not include the profiles' working directories; "
            f"the ones in {target_root} were left as they are."
        ]
    source = manifest.source_paths.workspaces_root
    if restored and source and os.path.normcase(os.path.normpath(source)) != os.path.normcase(
        os.path.normpath(target_root)
    ):
        return [
            f"The profiles' working directories ({restored} file(s)) were restored into "
            f"{target_root}; on the machine the backup came from they were in {source}."
        ]
    return []


def _chosen_dir_notes(report: RelocationReport, target_root: str) -> list[str]:
    """Folders an admin chose for a profile outside the workspaces root are
    never archived. After a restore onto another machine the (relocated)
    folder is usually not there: say which profiles that affects, rather
    than let their file panel open on an empty folder unexplained."""
    from app.config.working_dirs import is_inside, real_path

    root = real_path(target_root) if target_root else ""
    missing: list[str] = []
    for entry in (*report.relocated, *report.unmapped):
        if "profile" not in entry or entry.get("reset_to_default"):
            continue
        path = entry.get("new") or entry.get("value")
        if not isinstance(path, str) or not path:
            continue
        if root and is_inside(real_path(path), root):
            continue  # in the workspaces root: restored with it (or not included at all)
        if not os.path.isdir(path):
            missing.append(f"'{entry['profile']}' ({path})")
    if not missing:
        return []
    return [
        f"The working directory an admin chose for {', '.join(missing)} does not exist on "
        f"this machine: a backup never carries a folder chosen outside the workspaces "
        f"folder. Copy those files there, or have an admin choose another folder."
    ]


def restore_backup(
    archive: Path,
    passphrase: str | None = None,
    *,
    target_system_dir: str,
    close_out_owed_results: bool = True,
) -> RestoreReport:
    """One-shot stage + apply, cleaning up the temp staging dir.

    Used by the offline CLI and the setup-mode (fresh install) path, where
    there is no live server to quiesce and no restart is needed — and by the
    rollback of a failed restore, which passes ``close_out_owed_results=False``
    because it is putting this install's own state back, not replaying someone
    else's archive.
    """
    tmp = Path(tempfile.mkdtemp(prefix="cremind-restore-"))
    try:
        staged = stage_backup(Path(archive), passphrase, tmp)
        report = apply_staged_restore(
            staged.staging_dir,
            target_system_dir=target_system_dir,
            close_out_owed_results=close_out_owed_results,
        )
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
