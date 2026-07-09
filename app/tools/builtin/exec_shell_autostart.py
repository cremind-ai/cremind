"""Autostart runner for long-running processes.

Spawns commands registered in ``autostart_processes`` with a small retry
loop.  Each successful spawn registers a :class:`ProcessInfo` in
``_process_registry`` so the Process Manager API/WebSocket sees it exactly
like an agent-spawned long-running process.  If retries are exhausted, the
registration row's ``last_error`` is populated and the row is surfaced as
a ``failed_to_autostart`` synthetic entry by :func:`list_processes`.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
import time
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app.config.settings import BaseConfig
from app.config.system_vars import build_system_env
from app.storage import get_autostart_storage
from app.storage.autostart_storage import AutostartStorage
from app.tools.builtin.exec_shell import (
    LogWriterState,
    ProcessInfo,
    _kill_process_tree,
    _log_writer_loop,
    _process_registry,
    _shell_for,
    _spawn_command,
    _write_state,
    publish_process_list_changed,
    stop_process,
)
from app.tools.builtin.exec_shell_input_mode import TerminalState
from app.tools.builtin.exec_shell_pty import _spawn_command_pty
from app.utils.logger import logger


_URL_PREFIXES = ("http://", "https://", "ftp://", "file://", "git@", "ssh://", "git+")


def normalize_command_paths(command: str, working_dir: str) -> str:
    """Rewrite relative path tokens in *command* to use the OS-native separator.

    Skills (and other autostart entries) can declare commands with forward
    slashes for cross-platform readability — e.g. ``uv run scripts/foo.py``.
    On Windows, however, PowerShell + many native binaries expect ``\\`` and
    can fail to resolve a ``/``-separated relative path. We rewrite a token
    only when it resolves to a real file/dir under *working_dir*; URLs
    (``http://``, ``git@…``), short/long flags (``-x``, ``--key``), and any
    other ``/``-bearing argument that does not resolve to a path are left
    alone. Quoted tokens are also skipped — the user's quoting wins.

    No-op on POSIX (``os.sep == '/'``).
    """
    if os.sep == "/" or not command or not working_dir:
        return command

    def maybe_rewrite(match: "re.Match[str]") -> str:
        token = match.group(0)
        if "/" not in token or token.startswith(_URL_PREFIXES) or token.startswith("-"):
            return token
        native = token.replace("/", os.sep)
        if os.path.exists(os.path.join(working_dir, native)):
            return native
        return token

    return re.sub(r"[^\s\"']+", maybe_rewrite, command)


def _is_under(candidate: str, root: Path) -> bool:
    """True if *candidate* path is *root* itself or nested beneath it."""
    if not candidate:
        return False
    try:
        cp = Path(candidate).resolve()
    except (OSError, ValueError):
        return False
    return cp == root or root in cp.parents


async def teardown_processes_for_dir(directory: Path, *, profile: Optional[str]) -> dict:
    """Stop every long-running process bound to *directory* and drop its autostart row.

    Called before a skill directory is deleted or reset: a running listener
    holds OS file handles inside the directory (which blocks ``rmtree`` on
    Windows), and a lingering autostart registration would respawn the process
    from a directory that is gone or has been reset.

    Returns ``{"stopped": [pid], "removed_autostart": int}``.
    """
    root = directory.resolve()

    matched = [
        (pid, info)
        for pid, info in list(_process_registry.items())
        if _is_under(getattr(info, "working_dir", "") or "", root)
    ]
    for pid, info in matched:
        _kill_process_tree(getattr(info.process, "pid", None))
        try:
            await stop_process(pid, profile=info.profile or (profile or ""))
        except Exception:  # noqa: BLE001
            logger.warning(f"stop_process failed for {pid}", exc_info=True)

    removed = 0
    try:
        storage = get_autostart_storage()
        rows = storage.list(profile) if profile else storage.list_all()
        for row in rows:
            if _is_under(row.get("working_dir") or "", root):
                storage.delete(row["id"], row.get("profile") or profile)
                removed += 1
    except Exception:  # noqa: BLE001
        logger.warning("autostart cleanup failed during skill teardown", exc_info=True)

    if matched or removed:
        publish_process_list_changed(profile)
    return {"stopped": [pid for pid, _ in matched], "removed_autostart": removed}


async def _sanity_check(proc, delay: float = 2.0) -> Optional[int]:
    """Wait briefly; return the process's exit code if it died early, else None."""
    try:
        await asyncio.wait_for(proc.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return None
    return proc.returncode


async def _drain_early_output(proc) -> tuple[str, str]:
    """Read whatever stdout/stderr a dead-early subprocess produced.

    Used only after :func:`_sanity_check` has confirmed the process exited;
    once it's exited the pipes have a known finite amount of buffered output,
    so a bounded ``read()`` won't block. Each stream is capped to keep log
    lines reasonable. Returns ``("", "")`` for PTY processes (no separate
    stderr stream) or if the streams are unavailable.
    """
    max_bytes = 4096

    async def _read(stream) -> str:
        if stream is None:
            return ""
        try:
            data = await asyncio.wait_for(stream.read(max_bytes), timeout=1.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            return ""
        if not data:
            return ""
        return data.decode("utf-8", errors="replace").strip()

    stdout = await _read(getattr(proc, "stdout", None))
    stderr = await _read(getattr(proc, "stderr", None))
    return stdout, stderr


async def spawn_from_autostart(
    row: Dict[str, Any],
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    sanity_delay: float = 2.0,
) -> Tuple[Optional[str], Optional[str]]:
    """Spawn a command from an autostart row with retry/backoff.

    Returns ``(process_id, error)``.  On success ``process_id`` is the new
    registry id and ``error`` is ``None``; on terminal failure both are set
    (``process_id=None``, ``error`` populated with the last-attempt reason).
    """
    command = row.get("command", "").strip()
    if not command:
        return None, "Empty command"

    working_dir = row.get("working_dir") or BaseConfig.CREMIND_SYSTEM_DIR
    is_pty = bool(row.get("is_pty"))
    system = platform.system()
    shell, shell_flag = _shell_for(system)

    # Inject the same system-vars block the agent's exec_shell uses, so
    # autostart-spawned skills (e.g. file-watcher's event_listener.py) receive
    # CREMIND_USER_WORKING_DIR / CREMIND_SKILL_DIR / CREMIND_SERVER / CREMIND_TOKEN
    # exactly as a foreground exec_shell run would.
    profile_for_env = row.get("profile") or None
    extra_env = build_system_env(profile_for_env)

    if working_dir:
        try:
            os.makedirs(working_dir, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            return None, f"Invalid working directory {working_dir!r}: {exc}"

    # Defensive normalization for rows persisted with POSIX-style separators
    # (or registered via an older code path). Safe no-op on POSIX systems.
    original_command = command
    command = normalize_command_paths(command, working_dir)
    if command != original_command:
        logger.info(
            f"autostart[{row.get('id')}]: normalized command for {system} "
            f"({original_command!r} -> {command!r})"
        )
    logger.info(
        f"autostart[{row.get('id')}]: spawning (cwd={working_dir!r}, "
        f"shell={shell!r}, is_pty={is_pty}): {command!r}"
    )

    last_error: Optional[str] = None
    for attempt in range(max_attempts):
        if attempt > 0:
            delay = base_delay * (2 ** (attempt - 1))
            logger.info(
                f"autostart[{row.get('id')}]: waiting {delay:.1f}s before retry "
                f"{attempt + 1}/{max_attempts}",
            )
            await asyncio.sleep(delay)

        try:
            if is_pty:
                proc = await _spawn_command_pty(
                    command, working_dir, 100, 50, system,
                    extra_env=extra_env,
                )
            else:
                proc = await _spawn_command(
                    command, working_dir, system, shell, shell_flag,
                    extra_env=extra_env,
                )
        except Exception as exc:  # noqa: BLE001
            last_error = f"spawn failed: {exc}"
            logger.warning(f"autostart[{row.get('id')}]: {last_error}")
            continue

        early_rc = await _sanity_check(proc, delay=sanity_delay)
        if early_rc is not None:
            stdout, stderr = ("", "")
            if not is_pty:
                stdout, stderr = await _drain_early_output(proc)
            detail_parts = [f"exit code {early_rc}"]
            if stderr:
                detail_parts.append(f"stderr: {stderr}")
            if stdout:
                detail_parts.append(f"stdout: {stdout}")
            last_error = "process exited immediately — " + " | ".join(detail_parts)
            logger.warning(f"autostart[{row.get('id')}]: {last_error}")
            if is_pty:
                try:
                    proc.close_master()  # type: ignore[union-attr]
                except Exception:
                    pass
            continue

        # Success — register in the live registry so the Process Manager
        # picks it up.
        process_id = _uuid.uuid4().hex[:8]
        # Stdout/state files are Cremind-internal storage and live under
        # CREMIND_SYSTEM_DIR/<profile>, not under the spawned process's cwd.
        profile_for_logs = row.get("profile") or "admin"
        log_dir = os.path.join(
            BaseConfig.CREMIND_SYSTEM_DIR, profile_for_logs,
            "tools", "builtin", "exec_shell", "stdout", process_id,
        )
        os.makedirs(log_dir, exist_ok=True)
        _write_state(log_dir, {
            "process_id": process_id,
            "command": command,
            "status": "running",
            "return_code": None,
            "created_at": time.time(),
            "exited_at": None,
        })

        writer_state = LogWriterState(
            process_id=process_id,
            log_dir=log_dir,
            current_file_number=1,
            silence_threshold=3.0,
            terminal_state=TerminalState(),
        )
        writer_state.task = asyncio.create_task(_log_writer_loop(proc, writer_state))

        _process_registry[process_id] = ProcessInfo(
            process=proc,
            created_at=time.time(),
            working_dir=working_dir,
            command=command,
            log_dir=log_dir,
            log_writer_state=writer_state,
            # Autostart-linked processes have no expiration: while linked
            # they're meant to live as long as the registration exists.
            expire_time=float("inf"),
            is_long_running=True,
            is_pty=is_pty,
            task_id=None,
            profile=row.get("profile") or None,
            autostart_id=row.get("id"),
        )
        publish_process_list_changed(row.get("profile") or None)

        logger.info(
            f"autostart[{row.get('id')}]: process {process_id} started "
            f"(command={command!r}, attempt={attempt + 1})",
        )
        return process_id, None

    return None, last_error or "Unknown autostart failure"


async def _spawn_one(storage: AutostartStorage, row: Dict[str, Any]) -> None:
    process_id, error = await spawn_from_autostart(row)
    try:
        if error:
            storage.set_error(row["id"], error)
            # Spawn failed at boot — the row will surface in list_processes
            # as ``failed_to_autostart``; push the change so the UI lights it
            # up without waiting for any other event.
            publish_process_list_changed(row.get("profile") or None)
            _notify_autostart_failure(row, error)
        else:
            storage.clear_error(row["id"])
    except Exception as exc:  # noqa: BLE001
        logger.error(f"autostart[{row.get('id')}]: failed to update status row: {exc}")


def _notify_autostart_failure(row: Dict[str, Any], error: str) -> None:
    """Surface an autostart failure as a high-priority notification.

    The Process-Manager list bus already reflects the failed row, but that's
    only visible if the user is looking at the process list. After a restore
    onto a different machine, autostart commands may not be runnable in the new
    environment (missing binaries, incompatible paths) — the user asked to be
    *warned* about exactly this. The notifications buffer replays to UI clients
    that connect after boot, which is essential because these failures happen
    before anyone has subscribed. A notification must never break autostart
    handling, so this is fully guarded.
    """
    try:
        from app.events import get_event_notifications

        command = (row.get("command") or "").strip()
        get_event_notifications().push(
            profile=row.get("profile") or "admin",
            conversation_id="",
            conversation_title="Autostart process failed",
            message_preview=(
                f"'{command[:120]}' could not be started: {error}. "
                "Open the Process Manager to start it manually."
            ),
            kind="autostart_failed",
            priority="high",
            extra={"autostart_id": row.get("id"), "working_dir": row.get("working_dir")},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"autostart[{row.get('id')}]: failure notification push failed: {exc}")


async def run_autostart_on_boot(storage: AutostartStorage) -> None:
    """Launch every autostart registration as a fire-and-forget task.

    Intentionally does not await the per-row tasks: a slow or failing spawn
    must not block server startup.  Each task updates its own row with
    success/error state on completion.
    """
    try:
        rows = storage.list_all()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"run_autostart_on_boot: could not list registrations: {exc}")
        return
    if not rows:
        logger.info("run_autostart_on_boot: no autostart registrations")
        return
    logger.info(f"run_autostart_on_boot: launching {len(rows)} registration(s)")
    for row in rows:
        asyncio.create_task(_spawn_one(storage, row))
