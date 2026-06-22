"""Exec Shell built-in tool.

Executes shell commands on the terminal.  Each invocation spawns its own
subprocess (wrapped in the platform shell) and classifies the command by
observing its runtime behaviour:

- Process exits on its own        -> fire-and-forget  (returns result directly)
- TUI escape sequences in output  -> tui_fullscreen   (unsupported)
- Silence / stdin-blocked / long  -> long_running     (returns process_id)

Long-running processes are tracked in ``_process_registry``; their post-
classification stdout/stderr is streamed to incrementally numbered ``.log``
files under ``<user_working_dir>/tools/builtin/exec_shell/stdout/<process_id>/``.
"""

import asyncio
import collections
import contextlib
import glob
import json
import os
import platform
import shutil
import time
import uuid as _uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from app.config.settings import BaseConfig
from app.config.system_vars import build_system_env
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.task_context import current_task_id_var
from app.tools.builtin.exec_shell_classifier import detect_tui_sequences
from app.tools.builtin.exec_shell_input_mode import (
    TerminalState,
    detect_input_mode,
    update_terminal_state,
)
from app.tools.builtin.exec_shell_process_inspect import is_child_blocked_on_read
from app.tools.builtin.exec_shell_pty import (
    PtyProcess,
    _looks_like_tty_error,
    _spawn_command_pty,
)
from app.tools.builtin.exec_shell_rtk import maybe_rewrite_command
from app.utils.logger import logger

def _resolve_os(selected: Optional[str]) -> str:
    """Return concrete OS; resolve 'Auto-Detect'/None via platform.system()."""
    if not selected or selected == "Auto-Detect":
        return platform.system()
    return selected


def _shell_for(system: str) -> tuple[str, str]:
    """Return (shell, flag) for the given OS."""
    if system == "Windows":
        return "powershell.exe", "-Command"
    return "/bin/bash", "-c"


# ---------------------------------------------------------------------------
# Process registry & log writer state
# ---------------------------------------------------------------------------

_RING_BUFFER_DEFAULT_MAX_BYTES = 256 * 1024
_SUBSCRIBER_QUEUE_MAXSIZE = 256


@dataclass
class LogWriterState:
    """State for the background stdout log-writer task."""
    process_id: str
    log_dir: str
    current_file_number: int = 1
    silence_threshold: float = 3.0
    task: Optional[asyncio.Task] = None
    stopped: bool = False
    # rotate_lock serialises "writer is appending a chunk" with
    # "reader is flushing & rotating".  Prevents the reader from listing/
    # reading a file that the writer is mid-write on.
    rotate_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Current file handle (open only while the writer holds rotate_lock
    # during a write).  Exposed on the state so flush_and_rotate can close
    # it from outside the writer task.
    current_file: Optional[Any] = None
    # Running picture of the child's terminal state (DEC private-mode flags
    # + last-chunk transient signals).  Updated by the writer on every
    # stdout chunk and read by ExecShellOutputTool to classify input mode.
    terminal_state: TerminalState = field(default_factory=TerminalState)
    # UI side-channel: recent output chunks for late-joining WebSocket
    # subscribers ("snapshot") and live fan-out queues for active ones.
    # Populated alongside file writes inside rotate_lock so the snapshot
    # and the live stream can never disagree.  Tuples are (stream, chunk).
    ring_buffer: "collections.deque[Tuple[str, str]]" = field(
        default_factory=collections.deque,
    )
    ring_buffer_bytes: int = 0
    ring_buffer_max: int = _RING_BUFFER_DEFAULT_MAX_BYTES
    subscribers: Set["asyncio.Queue[Dict[str, Any]]"] = field(default_factory=set)
    # Long-poll signal for ExecShellOutputTool.  The writer sets this (always
    # under rotate_lock) whenever it appends a chunk or the process ends; a
    # blocked reader clears it under the same lock right before draining the
    # .log files, then awaits it.  Same-lock ordering rules out lost wakeups
    # (see ExecShellOutputTool.run).  Teardown paths (stop/cancel/cleanup)
    # also set it via _wake_readers so a waiting reader unblocks promptly.
    output_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Last input-mode classification produced by a read that returned output.
    # The reader deletes .log files after each read, so on an empty re-call the
    # output tail is gone and tail-based signals (e.g. a trailing ": " prompt)
    # can't be recomputed.  Caching the prior classification lets the heartbeat
    # path keep telling the agent "process is waiting for text/selection input"
    # instead of degrading to "unknown".
    last_input_mode: Optional[Dict[str, Any]] = None


@dataclass
class ProcessInfo:
    """Tracks a managed long-running process."""
    process: Union[asyncio.subprocess.Process, PtyProcess]
    created_at: float
    working_dir: str
    command: str
    log_dir: str = ""
    log_writer_state: Optional[LogWriterState] = None
    expire_time: float = 0.0
    is_long_running: bool = False
    is_pty: bool = False
    # The agent task that spawned this process, used for targeted cancellation
    # via ``cancel_processes_by_task``. Populated from the executor's ContextVar
    # at registration time.
    task_id: Optional[str] = None
    # Profile that spawned the process.  Used by the Process Manager API/UI
    # to scope list + WS access to the authenticated user's own processes.
    # Populated from ``arguments["_profile"]`` at registration time.
    profile: Optional[str] = None
    # Autostart registration id, set when the process was spawned (either at
    # server boot or via manual retry) from a row in ``autostart_processes``.
    autostart_id: Optional[str] = None


_process_registry: Dict[str, ProcessInfo] = {}
_DEFAULT_SILENCE_TIMEOUT = 3.0
_DEFAULT_LONG_RUNNING_TIMEOUT = 10.0
_MAX_CLASSIFICATION_TIME = 30.0
_DEFAULT_CLEANUP_TTL_HOURS = 24
# How long a single exec_shell_output call long-polls before returning a
# "still running" heartbeat the agent can immediately re-issue (no sleep).
_DEFAULT_OUTPUT_WAIT_TIMEOUT = 120.0
# Safety margin kept below MCP_TOOL_CALL_TIMEOUT so a heartbeat always returns
# before the adapter's hard tool-call timeout turns it into a generic error.
_OUTPUT_WAIT_MARGIN = 15.0
_STATE_FILENAME = "state.json"


def _state_path(log_dir: str) -> str:
    return os.path.join(log_dir, _STATE_FILENAME)


def _write_state(log_dir: str, data: Dict[str, Any]) -> None:
    """Atomically write ``state.json`` in *log_dir*.

    Uses write-to-temp + os.replace so the reader never observes a partial
    file.  Failures are logged but not raised — state.json is advisory and
    callers fall back to ``process.returncode`` when it's missing.
    """
    path = _state_path(log_dir)
    tmp_path = path + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp_path, path)
    except Exception as exc:
        logger.error(f"_write_state({log_dir}): {exc}")


def _read_state(log_dir: str) -> Optional[Dict[str, Any]]:
    """Read ``state.json`` from *log_dir*; return None if missing/invalid."""
    path = _state_path(log_dir)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as exc:
        logger.error(f"_read_state({log_dir}): {exc}")
        return None


def _wake_readers(state: Optional[LogWriterState]) -> None:
    """Unblock any ExecShellOutputTool long-poll waiting on *state*.

    Safe to call from anywhere (it only sets an asyncio.Event, never awaits).
    Invoked by the writer on new output / exit and by every teardown path
    (stop / task-cancel / TTL cleanup) so a blocked reader wakes, re-checks,
    and returns instead of hanging until its heartbeat deadline.
    """
    if state is not None:
        state.output_event.set()

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

SERVER_NAME = "Exec Shell"


class Var:
    """Variable keys for the Shell Executor tool."""
    LARGE_OUTPUT_MODE = "LARGE_OUTPUT_MODE"
    LARGE_OUTPUT_TOKEN_THRESHOLD = "LARGE_OUTPUT_TOKEN_THRESHOLD"
    LOG_SILENCE_THRESHOLD = "LOG_SILENCE_THRESHOLD"
    LONG_RUNNING_TIMEOUT = "LONG_RUNNING_TIMEOUT"
    OUTPUT_WAIT_TIMEOUT = "OUTPUT_WAIT_TIMEOUT"
    CLEANUP_TTL_HOURS = "CLEANUP_TTL_HOURS"
    # UI-only defaults used by the Process Manager terminal view.  The
    # agent's per-invocation ``cols`` / ``rows`` arguments on ExecShellTool
    # are untouched; these only seed the xterm.js instance when the user
    # hasn't specified a size.
    TERMINAL_DEFAULT_COLS = "TERMINAL_DEFAULT_COLS"
    TERMINAL_DEFAULT_ROWS = "TERMINAL_DEFAULT_ROWS"
    # RTK (Rust Token Killer) command-rewriter integration.  When enabled,
    # supported commands like ``git status`` are silently rewritten to
    # ``rtk git status`` before the shell spawns, compressing output by
    # 60-90%.  The agent sees only the smaller output; the rewrite is
    # transparent.  See app/tools/builtin/exec_shell_rtk.py.
    RTK_ENABLED = "RTK_ENABLED"
    RTK_BINARY_PATH = "RTK_BINARY_PATH"


TOOL_CONFIG: ToolConfig = {
    "name": "exec_shell",
    "display_name": "Shell Executor",
    "default_model_group": "low",
    # Visible in Settings (so its output-handling options can be configured)
    # but locked on — a core capability the user must not disable.
    "locked": True,
    "llm_parameters": {
        "tool_instructions": (
            "Execute command-line instructions on the terminal. Supports Linux, Windows, and macOS.\n"
        ),
        "system_prompt": (
            "You are a helpful assistant that can execute shell commands and provide their output.\n"
            "Don't answer any questions or provide explanations. Only execute the command and return the output."
        )
    },
    "required_config": {
        Var.LARGE_OUTPUT_MODE: {
            "description": (
                "How to handle large command output. "
                "'manual': ask user before returning output exceeding the token threshold. "
                "'automatic': always return full output regardless of size."
            ),
            "type": "string",
            "enum": ["manual", "automatic"],
            "default": "automatic",
        },
        Var.LARGE_OUTPUT_TOKEN_THRESHOLD: {
            "description": (
                "Token count threshold for large output handling. "
                "Only applies when LARGE_OUTPUT_MODE is 'manual'. "
                "Default: 10000."
            ),
            "type": "number",
            "default": 10000,
        },
        Var.LOG_SILENCE_THRESHOLD: {
            "description": (
                "Seconds of silence before closing the current log file "
                "and starting a new one for long-running processes. "
                "Default: 3."
            ),
            "type": "number",
            "default": 3,
        },
        Var.LONG_RUNNING_TIMEOUT: {
            "description": (
                "Seconds before a process is reclassified as 'long-running' "
                "and detached from the synchronous response. Default: 10."
            ),
            "type": "number",
            "default": _DEFAULT_LONG_RUNNING_TIMEOUT,
        },
        Var.OUTPUT_WAIT_TIMEOUT: {
            "description": (
                "Seconds a single exec_shell_output call blocks (long-polls) "
                "waiting for new output before returning a 'still running' "
                "heartbeat the agent can re-issue immediately. Returns the "
                "instant output appears or the process exits, so this only "
                "bounds dead-air waits. Automatically clamped below "
                "MCP_TOOL_CALL_TIMEOUT. Default: 120."
            ),
            "type": "number",
            "default": _DEFAULT_OUTPUT_WAIT_TIMEOUT,
        },
        Var.TERMINAL_DEFAULT_COLS: {
            "description": (
                "Default terminal width (columns) used by the Process "
                "Manager terminal view when a user opens a process's stdout "
                "stream without specifying a size.  Default: 80."
            ),
            "type": "number",
            "default": 80,
        },
        Var.TERMINAL_DEFAULT_ROWS: {
            "description": (
                "Default terminal height (rows) used by the Process "
                "Manager terminal view when a user opens a process's stdout "
                "stream without specifying a size.  Default: 24."
            ),
            "type": "number",
            "default": 24,
        },
        Var.CLEANUP_TTL_HOURS: {
            "description": (
                "Hours before expired process data (logs, registry entries) "
                "is automatically cleaned up. Default: 24."
            ),
            "type": "number",
            "default": 24,
        },
        Var.RTK_ENABLED: {
            "description": (
                "Route shell commands through RTK (Rust Token Killer) before "
                "execution. When enabled, supported commands like 'git status' "
                "or 'cargo test' are rewritten to 'rtk git status' / "
                "'rtk cargo test' so their output is filtered and compressed, "
                "saving 60-90% of LLM context tokens. Unsupported commands "
                "pass through unchanged. Requires the 'rtk' binary on PATH "
                "(see https://github.com/rtk-ai/rtk). Default: false."
            ),
            "type": "boolean",
            "default": False,
        },
        Var.RTK_BINARY_PATH: {
            "description": (
                "Path to the 'rtk' binary used when RTK_ENABLED is true. "
                "Defaults to 'rtk' (resolved via PATH). Set to an absolute "
                "path (e.g. C:\\Users\\you\\.local\\bin\\rtk.exe) when the "
                "spawned shell does not inherit your user PATH. Default: 'rtk'."
            ),
            "type": "string",
            "default": "rtk",
        },
    },
    "arguments": {
        "type": "object",
        "properties": {
            "os": {
                "type": "string",
                "enum": ["Windows", "Linux", "Darwin", "Auto-Detect"],
                "default": "Auto-Detect",
                "description": "Operating system to target for shell selection.",
            },
        },
    },
}


async def _spawn_command(
    command: str, working_dir: str, system: str, shell: str, shell_flag: str,
    extra_env: Optional[Dict[str, str]] = None,
    rtk_enabled: bool = False, rtk_binary: str = "rtk",
) -> asyncio.subprocess.Process:
    """Spawn a command as a standalone subprocess with piped stdin/stdout/stderr."""
    command = await maybe_rewrite_command(
        command, enabled=rtk_enabled, binary=rtk_binary,
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    # Force the child's stdout/stderr to be unbuffered. When stdout is a pipe
    # (not a TTY), CPython block-buffers it, so a command that prints a line
    # then blocks before filling the buffer (e.g. the OAuth `link` flow, which
    # prints the consent URL then parks on a loopback socket) never flushes —
    # the URL is captured as neither classification output nor a .log file.
    # Inherited through `uv run` into the child Python, same as PYTHONIOENCODING.
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update(extra_env)
    if system == "Windows":
        proc = await asyncio.create_subprocess_exec(
            shell, "-NoLogo", "-NoProfile", shell_flag, command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            shell, shell_flag, command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_dir,
            env=env,
        )
    return proc


async def _check_stdin_blocked(process: asyncio.subprocess.Process) -> bool | None:
    """True if the child process is blocked on stdin; None if inconclusive.

    Uses the host OS (the machine Cremind runs on), not the caller's target OS:
    stdin-block detection inspects the live process table via /proc (Linux) or
    Windows APIs, which only the host provides.
    """
    return await is_child_blocked_on_read(process.pid, platform.system())


# ---------------------------------------------------------------------------
# Classification (pre-long-running): read initial output and decide category
# ---------------------------------------------------------------------------

async def _classify_stream(
    process: Union[asyncio.subprocess.Process, PtyProcess],
    overall_timeout: float = 120.0,
    silence_timeout: float = _DEFAULT_SILENCE_TIMEOUT,
    long_running_timeout: float = _DEFAULT_LONG_RUNNING_TIMEOUT,
    skip_tui: bool = False,
    terminal_state: Optional[TerminalState] = None,
) -> dict:
    """Read stdout/stderr from *process* until classification is decided.

    Returns a dict with: stdout, stderr, return_code, completed, category,
    waiting_for_input, and optionally timed_out / tui_detected.
    """
    stdout_buf = ""
    stderr_buf = ""
    stdout_done = False
    stderr_done = False
    start = time.monotonic()
    last_data = start

    while not (stdout_done and stderr_done):
        elapsed = time.monotonic() - start
        if elapsed >= overall_timeout:
            return {
                "stdout": stdout_buf, "stderr": stderr_buf,
                "return_code": None, "completed": False,
                "category": "long_running",
                "waiting_for_input": False, "timed_out": True,
            }

        task_map: Dict[asyncio.Task, str] = {}
        if not stdout_done and process.stdout:
            task_map[asyncio.ensure_future(process.stdout.read(4096))] = "stdout"
        if not stderr_done and process.stderr:
            task_map[asyncio.ensure_future(process.stderr.read(4096))] = "stderr"
        if not task_map:
            break

        remaining = overall_timeout - elapsed
        done, pending = await asyncio.wait(
            task_map.keys(),
            timeout=min(silence_timeout, remaining),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

        if not done:
            # Silence window elapsed without new data.
            if process.returncode is not None:
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": process.returncode, "completed": True,
                    "category": "fire_and_forget",
                }

            silence_duration = time.monotonic() - last_data
            if silence_duration < silence_timeout:
                continue

            blocked = await _check_stdin_blocked(process)
            if blocked is True:
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "category": "long_running",
                    "waiting_for_input": True,
                }
            if silence_duration >= long_running_timeout:
                return {
                    "stdout": stdout_buf, "stderr": stderr_buf,
                    "return_code": None, "completed": False,
                    "category": "long_running",
                    "waiting_for_input": False,
                }
            continue

        for t in done:
            data = t.result()
            stream_name = task_map[t]
            if data == b"":
                if stream_name == "stdout":
                    stdout_done = True
                else:
                    stderr_done = True
                continue
            chunk = data.decode("utf-8", errors="replace")
            last_data = time.monotonic()

            if stream_name == "stdout":
                if not skip_tui and detect_tui_sequences(chunk):
                    return {
                        "stdout": stdout_buf + chunk,
                        "stderr": stderr_buf,
                        "return_code": None, "completed": False,
                        "category": "tui_fullscreen",
                        "tui_detected": True,
                    }
                if terminal_state is not None:
                    update_terminal_state(terminal_state, chunk)
                stdout_buf += chunk
            else:
                stderr_buf += chunk

        if elapsed >= _MAX_CLASSIFICATION_TIME:
            return {
                "stdout": stdout_buf, "stderr": stderr_buf,
                "return_code": None, "completed": False,
                "category": "long_running",
                "waiting_for_input": False,
            }

    # Both streams closed -> process finished.
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except (asyncio.TimeoutError, Exception):
        pass
    return {
        "stdout": stdout_buf, "stderr": stderr_buf,
        "return_code": process.returncode if process.returncode is not None else 0,
        "completed": True,
        "category": "fire_and_forget",
    }


# ---------------------------------------------------------------------------
# Background log writer for long-running processes
# ---------------------------------------------------------------------------

async def _log_writer_loop(
    process: asyncio.subprocess.Process,
    state: LogWriterState,
) -> None:
    """Stream process stdout/stderr into numbered ``.log`` files.

    Rotation rules:
    - New data opens ``<current_file_number>.log`` (if not already open) and
      appends.
    - ``silence_threshold`` seconds without new data closes the current file
      and increments ``current_file_number`` — the next output starts a new
      file.
    - On process exit, whatever file is open is closed, then a **fresh**
      ``<current_file_number>.log`` is created containing only
      ``__OPA_EXIT__:<rc>\\n``.  This keeps the sentinel in its own file.
    - ``ExecShellOutputTool`` serialises its flush + list + read + delete
      sequence against this loop by acquiring ``state.rotate_lock`` for the
      whole read, so no file on disk is ever observed mid-write.
    """
    os.makedirs(state.log_dir, exist_ok=True)

    # Persistent read tasks — never cancelled mid-stream.  Cancelling a
    # StreamReader.read() mid-flight on Windows ProactorEventLoop can drop
    # data that had already been pulled into the transport buffer; by
    # replacing cancelled tasks every silence cycle the original writer
    # loop lost chunks that arrived right as cancellation fired.  Keeping
    # one task per stream alive for the stream's entire lifetime, and only
    # replacing it when it completes with data (or EOF), avoids the race.
    read_tasks: Dict[str, asyncio.Task] = {}
    if process.stdout:
        read_tasks["stdout"] = asyncio.ensure_future(process.stdout.read(4096))
    if process.stderr:
        read_tasks["stderr"] = asyncio.ensure_future(process.stderr.read(4096))

    try:
        while not state.stopped and read_tasks:
            done, _ = await asyncio.wait(
                read_tasks.values(),
                timeout=state.silence_threshold,
                return_when=asyncio.FIRST_COMPLETED,
            )

            if not done:
                # Silence — rotate the current file (if any) and keep the
                # read tasks alive for the next cycle.
                async with state.rotate_lock:
                    if state.current_file is not None:
                        state.current_file.close()
                        state.current_file = None
                        state.current_file_number += 1
                if process.returncode is not None and not read_tasks:
                    break
                continue

            chunks: List[tuple[str, str]] = []
            finished_streams: List[str] = []
            for stream_name, task in list(read_tasks.items()):
                if task not in done:
                    continue
                data = task.result()
                if data == b"":
                    finished_streams.append(stream_name)
                    continue
                chunks.append((stream_name, data.decode("utf-8", errors="replace")))
                # Replace with a fresh read on the same stream.
                stream_obj = process.stdout if stream_name == "stdout" else process.stderr
                if stream_obj is not None:
                    read_tasks[stream_name] = asyncio.ensure_future(stream_obj.read(4096))

            for stream_name in finished_streams:
                read_tasks.pop(stream_name, None)

            if chunks:
                async with state.rotate_lock:
                    if state.current_file is None:
                        log_path = os.path.join(
                            state.log_dir, f"{state.current_file_number}.log",
                        )
                        state.current_file = open(log_path, "a", encoding="utf-8")
                    for stream_name, chunk in chunks:
                        if stream_name == "stderr":
                            state.current_file.write(f"[STDERR] {chunk}")
                        else:
                            update_terminal_state(state.terminal_state, chunk)
                            state.current_file.write(chunk)
                    state.current_file.flush()
                    # Side-channel fan-out for the Process Manager UI.  Runs
                    # inside rotate_lock so the snapshot handed to a new
                    # subscriber (also under the lock) and the live chunks
                    # it starts seeing afterwards can never disagree.  Uses
                    # put_nowait so a stuck WebSocket cannot block the log
                    # writer; overflowed queues are evicted next cycle.
                    _broadcast_chunks(state, chunks)
                    # Wake a long-polling reader: new output is on disk.  Set
                    # inside rotate_lock so it is ordered after a reader's
                    # clear() (also under the lock) — no lost wakeup.
                    _wake_readers(state)
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.error(f"log_writer_loop({state.process_id}): {exc}")
    finally:
        # Cancel any reads still outstanding (streams not EOF'd) before
        # closing files.  At shutdown the data would be lost anyway.
        for task in list(read_tasks.values()):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (asyncio.TimeoutError, Exception):
            pass

        # Exit status goes into state.json alongside the .log files.  The
        # .log files now contain only program output — no sentinel.
        try:
            async with state.rotate_lock:
                if state.current_file is not None:
                    state.current_file.close()
                    state.current_file = None
                    state.current_file_number += 1
                rc = process.returncode if process.returncode is not None else -1
                existing = _read_state(state.log_dir) or {}
                existing.update({
                    "status": "completed",
                    "return_code": rc,
                    "exited_at": time.time(),
                })
                _write_state(state.log_dir, existing)
                # Wake a long-polling reader: the process has exited, so the
                # next drain will see completed=True even with no new output.
                # Set under rotate_lock for the same no-lost-wakeup ordering.
                _wake_readers(state)
        except Exception as exc:
            logger.error(f"log_writer_loop({state.process_id}): state.json update failed: {exc}")

        # Belt-and-suspenders: ensure the event is set even if the state.json
        # update above raised before reaching _wake_readers.
        _wake_readers(state)
        state.stopped = True
        # Tell the Process Manager UI that this row's status flipped to
        # ``exited`` — registry entry is still present (it is only popped
        # by stop/cleanup), so the profile lookup is reliable here.
        try:
            exited_info = _process_registry.get(state.process_id)
            if exited_info is not None:
                publish_process_list_changed(exited_info.profile)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"log_writer_loop publish failed: {exc}")
        logger.debug(
            f"log_writer_loop({state.process_id}): finished, "
            f"exit_code={process.returncode}"
        )


@contextlib.asynccontextmanager
async def _noop_async_cm():
    yield


# ---------------------------------------------------------------------------
# Process Manager side-channel helpers
#
# These helpers expose the existing process registry and log-writer state to
# the Process Manager REST/WebSocket API.  The agent-facing tools
# (ExecShellInputTool / OutputTool / StopTool) continue to operate exactly
# as before — the file-based log flow is untouched.  The pub/sub layer is
# strictly additive: writers broadcast to subscribers inside the existing
# rotate_lock critical section using put_nowait (never awaits), and
# subscribe/unsubscribe take the same lock so a new subscriber's snapshot
# plus its live stream are handed over atomically.
# ---------------------------------------------------------------------------


def _broadcast_chunks(
    state: LogWriterState,
    chunks: List[Tuple[str, str]],
) -> None:
    """Fan chunks into ring buffer + subscriber queues.

    Must be called while holding ``state.rotate_lock``.  Never awaits: uses
    ``put_nowait`` on each subscriber and evicts any queue whose capacity is
    exhausted (one stuck UI tab must not stall the log writer).
    """
    if not chunks:
        return
    for stream_name, chunk in chunks:
        state.ring_buffer.append((stream_name, chunk))
        state.ring_buffer_bytes += len(chunk)
    # Trim oldest chunks until within budget.
    while state.ring_buffer_bytes > state.ring_buffer_max and state.ring_buffer:
        _stream, dropped = state.ring_buffer.popleft()
        state.ring_buffer_bytes -= len(dropped)

    if not state.subscribers:
        return
    evicted: List["asyncio.Queue[Dict[str, Any]]"] = []
    for stream_name, chunk in chunks:
        message = {"type": stream_name, "data": chunk}
        for queue in state.subscribers:
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                if queue not in evicted:
                    evicted.append(queue)
    for queue in evicted:
        state.subscribers.discard(queue)
        try:
            queue.put_nowait({"type": "overflow"})
        except asyncio.QueueFull:
            pass


async def subscribe(
    process_id: str,
) -> Tuple["asyncio.Queue[Dict[str, Any]]", List[Tuple[str, str]]]:
    """Register a new WebSocket subscriber and atomically hand over the
    current ring-buffer snapshot.

    Raises ``KeyError`` if ``process_id`` is unknown or has no writer state
    (e.g. the process exited and its log writer finalised before the caller
    connected — callers should handle this by showing an "already finished"
    view and falling back to REST).
    """
    info = _process_registry.get(process_id)
    if info is None or info.log_writer_state is None:
        raise KeyError(process_id)
    state = info.log_writer_state
    queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(
        maxsize=_SUBSCRIBER_QUEUE_MAXSIZE,
    )
    async with state.rotate_lock:
        snapshot = list(state.ring_buffer)
        state.subscribers.add(queue)
    return queue, snapshot


async def unsubscribe(
    process_id: str,
    queue: "asyncio.Queue[Dict[str, Any]]",
) -> None:
    """Remove a subscriber.  Tolerates unknown pids (process may have been
    cleaned up while the WebSocket was draining)."""
    info = _process_registry.get(process_id)
    if info is None or info.log_writer_state is None:
        return
    state = info.log_writer_state
    async with state.rotate_lock:
        state.subscribers.discard(queue)


def publish_process_list_changed(profile: Optional[str]) -> None:
    """Push a fresh process-list snapshot to SSE subscribers for ``profile``.

    Called at every state transition that affects ``list_processes``: spawn,
    exit, stop, expiry, autostart-row mutation. No-op when ``profile`` is
    falsy (rare; the list is profile-scoped).
    """
    if not profile:
        return
    try:
        from app.events.processes_bus import get_processes_stream_bus
        get_processes_stream_bus().publish(
            profile, {"processes": list_processes(profile)},
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"processes bus publish failed: {exc}")


def process_status(info: ProcessInfo) -> Tuple[str, Optional[int]]:
    """Return ``(status, exit_code)`` for a registry entry.

    Registry is authoritative.  ``state.json`` is only consulted to recover
    an exit code when the log writer reaped it before the registry entry
    was evicted.
    """
    rc = info.process.returncode
    if rc is not None:
        return "exited", rc
    try:
        state_data = _read_state(info.log_dir) if info.log_dir else None
    except Exception:
        state_data = None
    if state_data and state_data.get("status") == "completed":
        return "exited", state_data.get("return_code")
    return "running", None


def list_processes(profile: str) -> List[Dict[str, Any]]:
    """Snapshot the registry filtered to the authenticated profile.

    The fields returned power the Process Manager list UI.  ``expire_time``
    is converted from ``time.monotonic()`` deltas to a wall-clock ISO
    timestamp because the UI has no access to the backend's monotonic clock.

    Autostart registrations that are not currently running (e.g. their
    spawn failed on boot) are merged in as synthetic rows with status
    ``failed_to_autostart`` so the UI can surface a warning and a manual
    re-run control.
    """
    now_wall = time.time()
    now_mono = time.monotonic()
    result: List[Dict[str, Any]] = []
    live_autostart_ids: set[str] = set()
    for pid, info in list(_process_registry.items()):
        if profile and info.profile and info.profile != profile:
            continue
        status, exit_code = process_status(info)
        # ``expire_time`` is ``float('inf')`` for autostart-linked processes
        # (no expiration). Surface that as ``null`` since ``Infinity`` is not
        # valid JSON and the UI renders ``null`` as "never".
        expire_at_wall: Optional[float]
        if info.expire_time == float("inf"):
            expire_at_wall = None
        else:
            expire_at_wall = now_wall + max(0.0, info.expire_time - now_mono)
        if info.autostart_id:
            live_autostart_ids.add(info.autostart_id)
        result.append({
            "process_id": pid,
            "command": info.command,
            "working_dir": info.working_dir,
            "log_dir": info.log_dir,
            "status": status,
            "exit_code": exit_code,
            "created_at": info.created_at,
            "expire_at": expire_at_wall,
            "is_pty": info.is_pty,
            "autostart_id": info.autostart_id,
            "last_error": None,
        })

    # Merge autostart registrations not currently represented in the
    # registry.  Imported lazily to avoid a circular import at module load.
    try:
        from app.storage import get_autostart_storage
        autostart_rows = get_autostart_storage().list(profile) if profile else []
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"list_processes: autostart fetch failed: {exc}")
        autostart_rows = []

    for row in autostart_rows:
        if row["id"] in live_autostart_ids:
            continue
        result.append({
            "process_id": f"autostart:{row['id']}",
            "command": row["command"],
            "working_dir": row["working_dir"],
            "log_dir": "",
            "status": "failed_to_autostart",
            "exit_code": None,
            "created_at": row["created_at"],
            "expire_at": 0.0,
            "is_pty": row["is_pty"],
            "autostart_id": row["id"],
            "last_error": row["last_error"],
        })

    # Newest first — matches user expectation for a process list.
    result.sort(key=lambda row: row["created_at"], reverse=True)
    return result


def _require_process(process_id: str, profile: str) -> ProcessInfo:
    """Look up a process by id and verify profile ownership.

    Raises ``KeyError`` for unknown pids and ``PermissionError`` for
    cross-profile access.  Callers in the API layer translate these to 404
    / 403 (or WebSocket close 1008).
    """
    info = _process_registry.get(process_id)
    if info is None:
        raise KeyError(process_id)
    if profile and info.profile and info.profile != profile:
        raise PermissionError(process_id)
    return info


async def write_stdin_to_process(
    process_id: str,
    *,
    profile: str,
    mode: Optional[str] = None,
    input_text: Optional[str] = None,
    keys: Optional[List[str]] = None,
    line_ending: Optional[str] = None,
) -> Dict[str, Any]:
    """Send stdin to a long-running process.

    Shared between the agent-facing ``ExecShellInputTool`` and the Process
    Manager API (REST + WS).  Returns a structured dict suitable for the
    tool result or a JSON response; errors are surfaced under an ``error``
    key rather than raised, so the agent's contract (always returns a
    structured result, never a 500) is preserved.

    ``mode`` selects exactly one input channel: ``"input_text"``,
    ``"navigation"``, or ``"action"``. Navigation and action are mutually
    exclusive within a single call — the intended flow is to send
    navigation, observe stdout to confirm cursor/selection, then send
    a separate action call to commit.
    """
    if not process_id:
        return {
            "error": "Missing parameter",
            "message": "The 'process_id' parameter is required.",
        }

    if mode not in (None, "input_text", "navigation", "action"):
        return {
            "error": "Invalid parameters",
            "message": (
                f"'mode' must be one of 'input_text', 'navigation', "
                f"'action'; got {mode!r}."
            ),
        }

    if mode is None:
        if input_text is not None and keys is None:
            mode = "input_text"
        else:
            return {
                "error": "Missing parameter",
                "message": (
                    "'mode' is required when sending 'keys'. Use "
                    "'navigation' for cursor movement (up, down, left, "
                    "right, tab, space, backspace) or 'action' for "
                    "execution/confirmation (enter, escape, ctrl+c)."
                ),
            }

    if mode == "input_text":
        if input_text is None:
            return {
                "error": "Invalid parameters",
                "message": "mode='input_text' requires 'input_text'.",
            }
        if keys is not None:
            return {
                "error": "Invalid parameters",
                "message": "mode='input_text' must not supply 'keys'.",
            }
    else:
        if keys is None:
            return {
                "error": "Invalid parameters",
                "message": f"mode={mode!r} requires 'keys'.",
            }
        if input_text is not None:
            return {
                "error": "Invalid parameters",
                "message": f"mode={mode!r} must not supply 'input_text'.",
            }

    try:
        info = _require_process(process_id, profile)
    except KeyError:
        return {
            "error": "Process not found",
            "message": (
                f"No running process with id '{process_id}'. "
                "It may have exited or been cleaned up."
            ),
        }
    except PermissionError:
        return {
            "error": "Forbidden",
            "message": (
                f"Process '{process_id}' belongs to a different profile."
            ),
        }

    if line_ending is None:
        if info.is_pty and platform.system() == "Windows":
            line_ending = "\r"
        else:
            line_ending = "\n"

    resolved_key_bytes: Optional[List[str]] = None
    if mode in ("navigation", "action"):
        if not isinstance(keys, list) or not keys:
            return {
                "error": "Invalid parameters",
                "message": "'keys' must be a non-empty array.",
            }
        group_map = _KEY_GROUPS[mode]
        resolved_key_bytes = []
        for idx, name in enumerate(keys):
            if not isinstance(name, str) or name not in group_map:
                return {
                    "error": "Invalid parameters",
                    "message": (
                        f"keys[{idx}]={name!r} is not a valid {mode} key. "
                        f"Valid {mode} keys: {sorted(group_map)}."
                    ),
                }
            resolved_key_bytes.append(group_map[name])
        payload: Optional[str] = None
    else:
        if line_ending == "none":
            payload = input_text or ""
        else:
            payload = (input_text or "") + line_ending

    process = info.process

    if process.returncode is not None:
        return {
            "error": "Process already exited",
            "message": (
                f"Process '{process_id}' has already exited with "
                f"code {process.returncode}."
            ),
            "return_code": process.returncode,
            "process_id": process_id,
        }

    try:
        if resolved_key_bytes is not None:
            key_bytes = [s.encode("utf-8") for s in resolved_key_bytes]
            for i, chunk in enumerate(key_bytes):
                process.stdin.write(chunk)
                await process.stdin.drain()
                if i < len(key_bytes) - 1:
                    await asyncio.sleep(_KEYSTROKE_DELAY_SEC)
        else:
            payload_bytes = (payload or "").encode("utf-8")
            logger.debug(
                f"write_stdin_to_process({process_id}): writing "
                f"{payload_bytes!r} to stdin"
            )
            process.stdin.write(payload_bytes)
            await process.stdin.drain()
    except Exception as exc:
        return {
            "error": "Write error",
            "message": f"Failed to write to process stdin: {exc}",
            "process_id": process_id,
        }

    # Input was just sent, so the cached "process is waiting for input"
    # classification is now stale.  Clearing it lets the next exec_shell_output
    # call block for the process's *response* instead of immediately returning a
    # heartbeat that assumes the process is still sitting at a prompt.
    if info.log_writer_state is not None:
        info.log_writer_state.last_input_mode = None

    return {
        "process_id": process_id,
        "input_sent": True,
        "command": info.command,
        "message": "Input sent. Use exec shell stdout to read the response.",
    }


async def resize_pty(process_id: str, cols: int, rows: int, *, profile: str) -> Dict[str, Any]:
    """Resize the PTY window for a process.  No-op (returns ok=False) for
    non-PTY processes — the UI still gets a structured response."""
    try:
        info = _require_process(process_id, profile)
    except KeyError:
        return {"ok": False, "error": "Process not found"}
    except PermissionError:
        return {"ok": False, "error": "Forbidden"}
    if not info.is_pty:
        return {"ok": False, "error": "Not a PTY process", "is_pty": False}
    try:
        info.process.resize(int(cols), int(rows))  # type: ignore[union-attr]
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "cols": int(cols), "rows": int(rows)}


async def stop_process(process_id: str, *, profile: str) -> Dict[str, Any]:
    """Terminate a long-running process and return its final output.

    Shared between the agent-facing ``ExecShellStopTool`` and the Process
    Manager REST API.  Evicts the registry entry on success.
    """
    if not process_id:
        return {
            "error": "Missing parameter",
            "message": "The 'process_id' parameter is required.",
        }

    # Verify ownership before mutating state.
    try:
        info_preview = _require_process(process_id, profile)
    except KeyError:
        return {
            "error": "Process not found",
            "message": (
                f"No running process with id '{process_id}'. "
                "It may have already exited or been cleaned up."
            ),
        }
    except PermissionError:
        return {
            "error": "Forbidden",
            "message": (
                f"Process '{process_id}' belongs to a different profile."
            ),
        }

    info = _process_registry.pop(process_id, None)
    if info is None:
        # Race: someone else popped it between the check and now.
        info = info_preview
    stopped_profile = info.profile

    process = info.process
    state = info.log_writer_state

    # Wake any reader currently long-polling this process so it re-checks the
    # (now popped) registry and returns promptly instead of blocking until its
    # heartbeat deadline.  Done before cancelling the writer while state lives.
    _wake_readers(state)

    if state and state.task:
        state.stopped = True
        state.task.cancel()
        try:
            await state.task
        except (asyncio.CancelledError, Exception):
            pass

    # PTY processes need EOF (^D) before terminate so interactive shells
    # (ssh, bash -i) exit cleanly instead of being killed mid-session.
    if info.is_pty and process.returncode is None:
        try:
            process.send_eof()  # type: ignore[union-attr]
        except Exception:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except (asyncio.TimeoutError, Exception):
            pass

    if process.stdin:
        try:
            process.stdin.close()
        except Exception:
            pass

    if process.returncode is None:
        try:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        except Exception:
            pass

    if info.is_pty:
        try:
            process.close_master()  # type: ignore[union-attr]
        except Exception:
            pass

    # Read remaining logs + delete the dir under rotate_lock so this can't race
    # an in-flight ExecShellOutputTool drain (which lists/reads/removes the same
    # files); without the lock its os.listdir could hit a half-removed tree.
    final_output = ""
    read_lock = state.rotate_lock if state is not None else _noop_async_cm()
    async with read_lock:
        if info.log_dir and os.path.isdir(info.log_dir):
            try:
                log_files = sorted(
                    [f for f in os.listdir(info.log_dir) if f.endswith(".log")],
                    key=lambda f: int(f.split(".")[0]),
                )
            except FileNotFoundError:
                log_files = []
            for fname in log_files:
                try:
                    with open(os.path.join(info.log_dir, fname), "r",
                              encoding="utf-8") as fh:
                        final_output += fh.read()
                except Exception:
                    pass
            shutil.rmtree(info.log_dir, ignore_errors=True)

    publish_process_list_changed(stopped_profile)

    return {
        "stdout": final_output,
        "return_code": process.returncode,
        "stopped": True,
        "process_id": process_id,
        "command": info.command,
    }


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

async def cancel_processes_by_task(task_id: str) -> int:
    """Kill all subprocesses tagged with ``task_id``. Returns the count killed."""
    if not task_id:
        return 0
    killed = 0
    for pid, info in list(_process_registry.items()):
        if info.task_id != task_id:
            continue
        try:
            _wake_readers(info.log_writer_state)
            if info.log_writer_state and info.log_writer_state.task:
                info.log_writer_state.stopped = True
                info.log_writer_state.task.cancel()
            if info.process.returncode is None:
                info.process.kill()
            killed += 1
            logger.info(
                f"cancel_processes_by_task: killed process {pid} "
                f"({info.command!r}) for task {task_id}"
            )
        except Exception:
            logger.exception(
                f"cancel_processes_by_task: failed to kill process {pid}"
            )
    return killed


def cleanup_stdout_on_startup() -> int:
    """Remove every ``<process_id>`` directory under any profile's
    ``tools/builtin/exec_shell/stdout/`` tree.

    Long-running subprocesses don't survive a server restart, so the log
    directories they leave behind are orphaned — clear them on startup.
    Returns the number of directories removed.
    """
    base = BaseConfig.CREMIND_SYSTEM_DIR
    if not base or not os.path.isdir(base):
        return 0
    pattern = os.path.join(
        base, "*", "tools", "builtin", "exec_shell", "stdout", "*",
    )
    removed = 0
    for path in glob.glob(pattern):
        if not os.path.isdir(path):
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except Exception as exc:
            logger.error(f"cleanup_stdout_on_startup: failed to remove {path}: {exc}")
    if removed:
        logger.info(
            f"cleanup_stdout_on_startup: removed {removed} stale process stdout dir(s)"
        )
    return removed


async def _cleanup_stale_processes() -> int:
    """Kill and remove processes whose ``expire_time`` has passed."""
    now = time.monotonic()
    stale_ids = [
        pid for pid, info in _process_registry.items()
        if now >= info.expire_time
    ]
    affected_profiles: Set[Optional[str]] = set()
    for pid in stale_ids:
        info = _process_registry.pop(pid, None)
        if not info:
            continue
        affected_profiles.add(info.profile)
        _wake_readers(info.log_writer_state)
        if info.log_writer_state and info.log_writer_state.task:
            info.log_writer_state.stopped = True
            info.log_writer_state.task.cancel()
            try:
                await info.log_writer_state.task
            except (asyncio.CancelledError, Exception):
                pass
        if info.process.returncode is None:
            try:
                info.process.kill()
                await asyncio.wait_for(info.process.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
        if info.is_pty:
            try:
                info.process.close_master()  # type: ignore[union-attr]
            except Exception:
                pass
        if info.log_dir and os.path.isdir(info.log_dir):
            shutil.rmtree(info.log_dir, ignore_errors=True)
        logger.info(f"Cleaned up expired process {pid} ({info.command!r})")
    for profile in affected_profiles:
        publish_process_list_changed(profile)
    return len(stale_ids)


class ExecShellTool(BuiltInTool):
    name: str = "exec_shell"
    description: str = (
        "Executes a shell command on the terminal and returns its output. "
        "Automatically detects the operating system and uses the appropriate shell.\n"
        "Supports interactive commands that require a TTY (ssh, docker exec -it, "
        "kubectl exec -it, REPLs like python -i / mysql / psql). If a command "
        "fails with a TTY-required error (e.g. 'the input device is not a TTY'), "
        "it is automatically respawned under a pseudo-terminal. For commands "
        "that hang without a TTY instead of erroring (notably ssh), pass "
        "`pty: true` explicitly. In PTY mode stdout and stderr are merged into `stdout`.\n"
        "One important note: before deciding to stop reading **stdout**, check the "
        "**`completed`** flag. If it is **`False`**, you must **sleep for a few seconds** "
        "and then read **stdout** again to ensure that there is no remaining output "
        "still buffered."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to execute.",
            },
            "current_shell_directory": {
                "type": "string",
                "description": (
                    "OPTIONAL one-off override for THIS command's working "
                    "directory (absolute path only). Leave EMPTY in almost all "
                    "cases: the command already runs in the conversation's "
                    "current working directory (shown as 'Current User Working "
                    "Directory' in the system prompt). Set this only to run a "
                    "single command somewhere else; it does NOT change the "
                    "conversation's working directory and is not remembered for "
                    "later commands. To switch persistently, use "
                    "change_working_directory."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Maximum time in seconds to wait for fire-and-forget commands. Defaults to 120.",
            },
            "silence_timeout": {
                "type": "number",
                "description": (
                    "Seconds of output silence before checking whether the "
                    "process is waiting for input. Default: 3."
                ),
            },
            "pty": {
                "type": "boolean",
                "description": (
                    "Force PTY (pseudo-terminal) allocation on/off. When true, "
                    "the command runs under a real TTY — required for ssh, "
                    "docker exec -it, kubectl exec -it, and interactive REPLs. "
                    "In PTY mode, stderr is merged into stdout. Default: "
                    "auto-detect based on the command."
                ),
            },
            "cols": {
                "type": "integer",
                "description": "Terminal width (columns) for PTY mode. Default 80.",
            },
            "rows": {
                "type": "integer",
                "description": "Terminal height (rows) for PTY mode. Default 24.",
            },
            "os": {
                "type": "string",
                "enum": ["Windows", "Linux", "Darwin"],
                "default": "Linux",
                "description": (
                    "Target operating system for shell selection. Controls "
                    "which shell binary is used to run the command "
                    "(powershell.exe on Windows, /bin/bash on Linux/Darwin)."
                ),
            },
        },
        "required": ["command"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        command = arguments.get("command", "").strip()
        logger.debug(f"exec_shell called with command={command!r}")
        # The ONE conversation working directory, shared with system_file and
        # the reasoning-agent prompt line. It is the _working_directory_override
        # (set by change_working_directory or skill-load) injected by the
        # adapter as `_working_directory`, else CREMIND_SYSTEM_DIR.
        conversation_working_directory = (
            arguments.get("_working_directory", None)
            or BaseConfig.CREMIND_SYSTEM_DIR
        )

        # current_shell_directory is an OPTIONAL one-off override for THIS
        # command only — it is never persisted, so it can no longer silently
        # shadow the conversation cwd on later calls. To move the conversation
        # persistently, change_working_directory writes
        # _working_directory_override, which arrives here as _working_directory.
        current_dir_arg = (arguments.get("current_shell_directory") or "").strip() or None
        shell_working_directory = current_dir_arg or conversation_working_directory
        logger.debug(f"Conversation working directory: {conversation_working_directory!r}")
        logger.debug(f"Determined shell working directory: {shell_working_directory!r}")
        timeout = arguments.get("timeout", 120)
        silence_timeout = arguments.get("silence_timeout", _DEFAULT_SILENCE_TIMEOUT)
        pty_arg = arguments.get("pty")
        cols = int(arguments.get("cols") or 100)
        rows = int(arguments.get("rows") or 50)
        system = _resolve_os(arguments.get("os"))
        shell, shell_flag = _shell_for(system)
        variables = arguments.get("_variables") or {}
        log_silence_threshold = float(variables.get(Var.LOG_SILENCE_THRESHOLD) or 3.0)
        cleanup_ttl_hours = float(variables.get(Var.CLEANUP_TTL_HOURS) or _DEFAULT_CLEANUP_TTL_HOURS)
        long_running_timeout = float(variables.get(Var.LONG_RUNNING_TIMEOUT) or _DEFAULT_LONG_RUNNING_TIMEOUT)
        rtk_enabled = bool(variables.get(Var.RTK_ENABLED, False))
        rtk_binary = str(variables.get(Var.RTK_BINARY_PATH) or "rtk")

        if not command:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'command' parameter is required.",
                }
            )

        if shell_working_directory:
            os.makedirs(shell_working_directory, exist_ok=True)
            if not os.path.isdir(shell_working_directory):
                return BuiltInToolResult(
                    structured_content={
                        "error": "Invalid working directory",
                        "message": f"Directory does not exist: {shell_working_directory}",
                    }
                )

        await _cleanup_stale_processes()

        use_pty = bool(pty_arg) if pty_arg is not None else False
        auto_retry_allowed = pty_arg is None

        # Inject the system-variables env block (CREMIND_SERVER + CREMIND_TOKEN so the
        # spawned shell can invoke the `cremind` CLI against this server, plus the
        # _CREMIND_SYSTEM_DIR_ / _USER_WORKING_DIR_ / _SKILL_DIR_ sentinels).
        # 127.0.0.1:PORT bypasses any external APP_URL / reverse proxy — the
        # CLI runs on the same box as the server. See app/config/system_vars.py.
        profile = arguments.get("_profile")
        extra_env = build_system_env(profile)

        async def _spawn(pty_mode: bool):
            if pty_mode:
                return await _spawn_command_pty(
                    command, shell_working_directory, cols, rows, system,
                    extra_env=extra_env,
                    rtk_enabled=rtk_enabled, rtk_binary=rtk_binary,
                )
            return await _spawn_command(
                command, shell_working_directory, system, shell, shell_flag,
                extra_env=extra_env,
                rtk_enabled=rtk_enabled, rtk_binary=rtk_binary,
            )

        logger.debug(
            f"exec_shell: running '{command}' on {system} with shell {shell} "
            f"(pty={use_pty})"
        )

        try:
            proc = await _spawn(use_pty)
        except Exception as e:
            return BuiltInToolResult(
                structured_content={
                    "error": "Spawn error",
                    "message": f"Failed to spawn command: {str(e)}",
                    "command": command,
                    "os": system,
                    "shell": shell,
                    "pty": use_pty,
                }
            )

        terminal_state = TerminalState()

        try:
            classify_timeout = min(timeout, 5.0) if use_pty else timeout
            result = await _classify_stream(
                proc,
                overall_timeout=classify_timeout,
                silence_timeout=silence_timeout,
                long_running_timeout=long_running_timeout,
                skip_tui=use_pty,
                terminal_state=terminal_state,
            )

            # Runtime PTY detection: if the command exited with an error whose
            # output looks like a "TTY required" failure, respawn under PTY
            # and re-classify. Skipped if the caller pinned `pty` explicitly.
            if (
                auto_retry_allowed
                and not use_pty
                and result.get("completed")
                and (result.get("return_code") or 0) != 0
                and _looks_like_tty_error(result.get("stdout", ""), result.get("stderr", ""))
            ):
                logger.info(
                    f"exec_shell: detected TTY-required error for '{command}', "
                    "respawning under PTY"
                )
                use_pty = True
                try:
                    proc = await _spawn(True)
                except Exception as e:
                    return BuiltInToolResult(
                        structured_content={
                            "error": "Spawn error",
                            "message": f"Failed to respawn command under PTY: {str(e)}",
                            "command": command,
                            "os": system,
                            "shell": shell,
                            "pty": True,
                        }
                    )
                classify_timeout = min(timeout, 5.0)
                # Fresh state for the respawn — the previous process's toggles
                # died with it.
                terminal_state = TerminalState()
                result = await _classify_stream(
                    proc,
                    overall_timeout=classify_timeout,
                    silence_timeout=silence_timeout,
                    long_running_timeout=long_running_timeout,
                    skip_tui=True,
                    terminal_state=terminal_state,
                )

            # In PTY mode, user opted into a terminal session — don't reject
            # on TUI sequences; treat as long_running instead.
            if use_pty and result.get("category") == "tui_fullscreen":
                result["category"] = "long_running"
                result["completed"] = False
                result.pop("tui_detected", None)

            category = result.get("category", "fire_and_forget")
            logger.debug(
                f"exec_shell: classification result for '{command}': "
                f"category={category}, completed={result.get('completed')}"
            )

            if result["completed"]:
                # Drain any remaining output the classifier might have missed
                # (rare, but happens if EOF landed in the last read).
                if use_pty:
                    try:
                        proc.close_master()  # type: ignore[union-attr]
                    except Exception:
                        pass
                return BuiltInToolResult(
                    structured_content={
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                        "return_code": result["return_code"],
                        "category": "fire_and_forget",
                        "command": command,
                        "os": system,
                        "shell": shell,
                        **({"pty": True, "stderr_merged_into_stdout": True} if use_pty else {}),
                        **({"timed_out": True} if result.get("timed_out") else {}),
                    }
                )

            if category == "tui_fullscreen":
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, Exception):
                    pass
                if use_pty:
                    try:
                        proc.close_master()  # type: ignore[union-attr]
                    except Exception:
                        pass
                return BuiltInToolResult(
                    structured_content={
                        "error": "TUI application detected",
                        "unsupported": True,
                        "category": "tui_fullscreen",
                        "stdout": result["stdout"],
                        "stderr": result["stderr"],
                        "interrupted": True,
                        "message": (
                            f"The command '{command}' appears to be a TUI / full-screen "
                            "application (detected terminal control escape sequences). "
                            "This type of command is not supported."
                        ),
                        "command": command,
                        "os": system,
                        "shell": shell,
                    }
                )

            # --- LONG_RUNNING: keep subprocess alive, start log writer ---
            process_id = _uuid.uuid4().hex[:8]
            # Stdout/state files are Cremind-internal storage and live under
            # CREMIND_SYSTEM_DIR/<profile>, NOT under the User Working
            # Directory (which is a user-facing path like ~/Documents).
            profile_for_logs = arguments.get("_profile") or "admin"
            log_dir = os.path.join(
                BaseConfig.CREMIND_SYSTEM_DIR, profile_for_logs,
                "tools", "builtin", "exec_shell", "stdout", process_id,
            )
            logger.debug(f"exec_shell: creating log directory {log_dir!r} for process {process_id}")
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
                silence_threshold=log_silence_threshold,
                terminal_state=terminal_state,
            )

            # Seed the ring buffer with the output captured during classification.
            # Those bytes were consumed from the process streams before the log
            # writer task existed, so without this step the WebSocket snapshot
            # for a late-joining subscriber (including the Terminal panel chip
            # flow) would miss everything printed before the long-running
            # classification fired — e.g. a prompt like "Please enter your name:".
            # No subscribers exist yet, so _broadcast_chunks just populates
            # ring_buffer; no lock needed (the writer task hasn't started).
            initial_chunks: List[Tuple[str, str]] = []
            initial_stdout = result.get("stdout") or ""
            if initial_stdout:
                if use_pty:
                    update_terminal_state(writer_state.terminal_state, initial_stdout)
                initial_chunks.append(("stdout", initial_stdout))
            initial_stderr = result.get("stderr") or ""
            if initial_stderr and not use_pty:
                initial_chunks.append(("stderr", initial_stderr))
            if initial_chunks:
                _broadcast_chunks(writer_state, initial_chunks)

            writer_state.task = asyncio.create_task(
                _log_writer_loop(proc, writer_state)
            )

            expire_time = time.monotonic() + (cleanup_ttl_hours * 3600)
            _process_registry[process_id] = ProcessInfo(
                process=proc,
                created_at=time.time(),
                working_dir=shell_working_directory,
                command=command,
                log_dir=log_dir,
                log_writer_state=writer_state,
                expire_time=expire_time,
                is_long_running=True,
                is_pty=use_pty,
                task_id=current_task_id_var.get(),
                profile=arguments.get("_profile") or None,
            )
            publish_process_list_changed(arguments.get("_profile") or None)

            logger.info(
                f"exec_shell: long-running process {process_id} started "
                f"(command={command!r}, pty={use_pty})"
            )

            return BuiltInToolResult(
                structured_content={
                    "process_id": process_id,
                    "stdout": result.get("stdout", ""),
                    "stderr": result.get("stderr", ""),
                    "status": "running",
                    "category": "long_running",
                    "waiting_for_input": result.get("waiting_for_input", False),
                    "command": command,
                    "working_directory": shell_working_directory,
                    "os": system,
                    "shell": shell,
                    **({"pty": True, "stderr_merged_into_stdout": True} if use_pty else {}),
                    "message": (
                        f"Long-running process started with id '{process_id}'. "
                        "Use exec shell stdout to read output, exec shell input "
                        "to send input, or exec shell stop to terminate."
                    ),
                }
            )

        except Exception as e:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (asyncio.TimeoutError, Exception):
                pass
            if use_pty:
                try:
                    proc.close_master()  # type: ignore[union-attr]
                except Exception:
                    pass
            return BuiltInToolResult(
                structured_content={
                    "error": "Execution error",
                    "message": f"Failed to execute command: {str(e)}",
                    "command": command,
                    "os": system,
                    "shell": shell,
                    "pty": use_pty,
                }
            )


_NAV_KEY_TO_BYTES: Dict[str, str] = {
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "tab": "\t",
    "space": " ",
    "backspace": "\x7f",
}

_ACTION_KEY_TO_BYTES: Dict[str, str] = {
    "enter": "\r",
    "escape": "\x1b",
    "ctrl+c": "\x03",
}

_KEY_GROUPS: Dict[str, Dict[str, str]] = {
    "navigation": _NAV_KEY_TO_BYTES,
    "action": _ACTION_KEY_TO_BYTES,
}

# Inter-keystroke delay for `keys` mode. Some TUIs (menu selectors, npm init,
# ConPTY apps) drop keys when they arrive back-to-back; a short human-scale
# pause lets each one be processed before the next arrives.
_KEYSTROKE_DELAY_SEC = 0.1


class ExecShellInputTool(BuiltInTool):
    name: str = "exec_shell_input"
    description: str = (
        "Send input to a running process. Use this after exec_shell returns "
        "a process_id. Writes to the process's stdin; use exec shell output "
        "to read the response.\n"
        'Format input: [process_id: <id>]\n<mode and payload here>\n'
        "Pick exactly one 'mode' per call:\n"
        "  - input_text: send the 'input_text' field as text/raw bytes. "
        "You don't need to append a line terminator; the right one is "
        "added automatically ('\\r' on Windows PTY processes, '\\n' "
        "elsewhere). Override with 'line_ending' if needed.\n"
        "  - navigation: send the 'keys' field as cursor/focus movement "
        "keystrokes. Allowed names: up, down, left, right, tab, space, "
        "backspace. Use this to position the cursor or selection. No "
        "side effects — nothing is committed.\n"
        "  - action: send the 'keys' field as a single execution / "
        "confirmation keystroke. Allowed names: enter, escape, ctrl+c. "
        "These ARE side-effecting — enter submits, escape dismisses, "
        "ctrl+c aborts.\n"
        "Required flow: navigation and action are NEVER combined in one "
        "call. The correct sequence is:\n"
        "  1) call with mode='navigation' to position the cursor (you "
        "can batch many keys in one call),\n"
        "  2) call exec_shell output to verify the cursor/selection "
        "actually landed where you expect,\n"
        "  3) call again with mode='action' to commit (e.g. press enter).\n"
        "Skipping step 2 risks committing in the wrong place.\n"
        "Navigation example:\n"
        '  [process_id: 708e9873]\n'
        '  mode: "navigation"\n'
        '  keys: ["down","down"]\n'
        "Action example (after observing stdout):\n"
        '  [process_id: 708e9873]\n'
        '  mode: "action"\n'
        '  keys: ["enter"]\n'
        "Text example:\n"
        '  [process_id: 708e9873]\n'
        '  mode: "input_text"\n'
        '  input_text: "my input"\n'
        "Required: process_id, mode, and the field that mode selects "
        "(input_text for mode='input_text', keys for mode='navigation' "
        "or mode='action')."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "process_id": {
                "type": "string",
                "description": "The process_id returned by exec_shell.",
            },
            "mode": {
                "type": "string",
                "enum": ["input_text", "navigation", "action"],
                "description": (
                    "Selects the input channel for this call. "
                    "'input_text' sends the 'input_text' field as "
                    "text/raw bytes. 'navigation' sends the 'keys' "
                    "field as cursor/focus movement (up, down, left, "
                    "right, tab, space, backspace) — no side effects. "
                    "'action' sends the 'keys' field as execution / "
                    "confirmation (enter, escape, ctrl+c) — side-"
                    "effecting. navigation and action are mutually "
                    "exclusive: send navigation, observe stdout to "
                    "verify cursor position, then send action."
                ),
            },
            "input_text": {
                "type": "string",
                "description": (
                    "Text or raw bytes to send to stdin. Used when "
                    "mode='input_text'; ignored otherwise."
                ),
            },
            "keys": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "string",
                    "enum": (
                        list(_NAV_KEY_TO_BYTES.keys())
                        + list(_ACTION_KEY_TO_BYTES.keys())
                    ),
                },
                "description": (
                    "Ordered list of symbolic key names. Required "
                    "when mode='navigation' or mode='action'. With "
                    "mode='navigation', items must be drawn from: "
                    "up, down, left, right, tab, space, backspace. "
                    "With mode='action', items must be drawn from: "
                    "enter, escape, ctrl+c. Sent in order with a short "
                    "inter-keystroke delay; implies line_ending='none'."
                ),
            },
            "line_ending": {
                "type": "string",
                "enum": ["\n", "\r", "\r\n", "none"],
                "description": (
                    "Line terminator appended to input_text. Default "
                    "is OS/mode-aware: '\\r' for Windows PTY processes "
                    "(ConPTY treats '\\r' as Enter and ignores '\\n'), "
                    "'\\n' otherwise. Use 'none' to send raw bytes "
                    "(useful for control characters). Explicit values "
                    "are never coerced. Ignored when mode is "
                    "'navigation' or 'action'."
                ),
            },
        },
        "required": ["process_id"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        # Thin adapter over ``write_stdin_to_process`` — shared with the
        # Process Manager API so both entry points exercise the same code.
        process_id = arguments.get("process_id", "").strip()
        logger.debug(
            f"exec_shell input called with process_id={process_id!r}, "
            f"mode={arguments.get('mode')!r}, "
            f"input_text={arguments.get('input_text')!r}, "
            f"keys={arguments.get('keys')!r}, "
            f"line_ending={arguments.get('line_ending')!r}"
        )
        result = await write_stdin_to_process(
            process_id,
            profile=arguments.get("_profile") or "",
            mode=arguments.get("mode"),
            input_text=arguments.get("input_text"),
            keys=arguments.get("keys"),
            line_ending=arguments.get("line_ending"),
        )
        return BuiltInToolResult(structured_content=result)


def _build_output_instruction(
    *,
    completed: bool,
    return_code: Optional[int],
    input_mode: Optional[str],
    is_pty: bool,
    truncated: bool,
) -> str:
    """Deterministic next-step guidance for an ExecShellOutputTool result."""
    if truncated:
        return (
            "Output exceeds the configured token threshold. Confirm with the "
            "user before retrieving the full output, or set LARGE_OUTPUT_MODE="
            "'automatic' / raise LARGE_OUTPUT_TOKEN_THRESHOLD."
        )
    if completed:
        rc_text = f" (return_code={return_code})" if return_code is not None else ""
        return (
            f"The process has exited{rc_text}. No further input can be sent. "
            "Start a new command with exec_shell."
        )
    if input_mode == "text":
        default_ending = "\\r" if (is_pty and platform.system() == "Windows") else "\\n"
        return (
            "Process is waiting for text input. Call exec shell input with "
            f"your text; the default line_ending ('{default_ending}') works."
        )
    if input_mode == "selection":
        base = (
            "Process is showing a selection menu. Call exec shell input with "
            "either a symbolic `keys` array (preferred) or raw escape codes "
            "in `input_text` with `line_ending='none'`. You can batch multiple "
            "keystrokes in one call to reduce round-trips — e.g. "
            "`keys: ['down','down','down']`. Note: after making a selection and "
            "before pressing 'enter', you should observe the output to verify "
            "whether the selection has actually been applied.\n"
            "Valid key names: up, down, left, right, enter, tab, space, "
            "escape, backspace, ctrl+c. For long menus, re-read output "
            "between bursts to confirm the cursor position before confirming."
        )
        if not is_pty:
            base += (
                " Note: arrow keys are unreliable without a PTY — restart the "
                "command with pty=true if keys are ignored."
            )
        return base
    if input_mode == "unknown":
        return (
            "Process is running but the input mode is unclear. If input is "
            "expected, try plain text with exec shell input or special keys "
            "for selection menus. Otherwise call exec_shell_output again to "
            "keep waiting — it blocks until output arrives or the process "
            "exits."
        )
    return (
        "Process is still running with no new output yet. Call "
        "exec_shell_output again to keep waiting — it blocks until new output "
        "arrives or the process exits."
    )


def _refresh_expire(info: ProcessInfo, variables: Dict[str, Any]) -> None:
    """Re-arm a process's TTL cleanup window on agent interaction.

    Autostart-linked processes opt out — they're meant to live indefinitely
    while registered, and an agent read must not silently start a 24h timer.
    """
    if info.autostart_id is None:
        cleanup_ttl_hours = float(
            variables.get(Var.CLEANUP_TTL_HOURS) or _DEFAULT_CLEANUP_TTL_HOURS
        )
        info.expire_time = time.monotonic() + (cleanup_ttl_hours * 3600)


class ExecShellOutputTool(BuiltInTool):
    name: str = "exec_shell_output"
    description: str = (
        "Read stdout from a long-running process.\n"
        "E.g. [process_id: 708e9873]\nmode: 'exec shell output'\n"
        "Required: process_id"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "process_id": {
                "type": "string",
                "description": "The process_id returned by exec_shell.",
            },
        },
        "required": ["process_id"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        process_id = arguments.get("process_id", "").strip()
        variables = arguments.get("_variables") or {}
        large_output_mode = variables.get(Var.LARGE_OUTPUT_MODE) or "automatic"
        token_threshold = int(variables.get(Var.LARGE_OUTPUT_TOKEN_THRESHOLD) or 10000)

        if not process_id:
            return BuiltInToolResult(
                structured_content={
                    "error": "Missing parameter",
                    "message": "The 'process_id' parameter is required.",
                    "instruction": "Provide the 'process_id' returned by exec_shell.",
                }
            )

        # Long-poll window: block until new output is written or the process
        # exits, returning the instant either happens.  On a long silence,
        # return a "still running" heartbeat the agent can re-issue immediately
        # (no sleep).  Clamp below MCP_TOOL_CALL_TIMEOUT so the heartbeat always
        # beats the adapter's hard tool-call timeout (which would otherwise turn
        # it into a generic, guidance-free error).
        max_wait = float(
            variables.get(Var.OUTPUT_WAIT_TIMEOUT) or _DEFAULT_OUTPUT_WAIT_TIMEOUT
        )
        tool_cap = BaseConfig.MCP_TOOL_CALL_TIMEOUT
        if tool_cap:
            max_wait = min(max_wait, max(1.0, float(tool_cap) - _OUTPUT_WAIT_MARGIN))
        deadline = time.monotonic() + max_wait

        while True:
            # Re-fetch each iteration: a teardown (stop / cancel / TTL cleanup)
            # can pop the entry while we're blocked, and we must notice.
            info = _process_registry.get(process_id)
            if not info:
                return BuiltInToolResult(
                    structured_content={
                        "error": "Process not found",
                        "message": (
                            f"No process with id '{process_id}'. "
                            "It may have exited and been cleaned up."
                        ),
                        "instruction": (
                            "The process no longer exists. Start a new command "
                            "with exec_shell."
                        ),
                    }
                )

            state = info.log_writer_state

            # Drain whatever output is currently on disk.  Hold rotate_lock
            # across clear + flush + list + read + delete so the writer cannot
            # open a new .log file (or resume appending) while we iterate, and
            # so the event clear is ordered against the writer's set.
            merged_output = ""
            if info.log_dir and os.path.isdir(info.log_dir):
                lock_ctx = state.rotate_lock if state is not None else _noop_async_cm()
                async with lock_ctx:
                    # Clear the wake signal BEFORE draining, inside the lock, so
                    # any subsequent writer set() (also under the lock) is seen
                    # by the wait() below — no lost wakeup.
                    if state is not None:
                        state.output_event.clear()
                    if state is not None and state.current_file is not None:
                        try:
                            state.current_file.close()
                        except Exception:
                            pass
                        state.current_file = None
                        state.current_file_number += 1

                    try:
                        log_files: List[str] = [
                            f for f in os.listdir(info.log_dir) if f.endswith(".log")
                        ]
                    except FileNotFoundError:
                        # Dir vanished mid-wait (stop / cleanup).  Treat as no
                        # output; the next iteration re-checks the registry.
                        log_files = []
                    log_files.sort(key=lambda f: int(f.split(".")[0]))

                    read_paths: List[str] = []
                    for fname in log_files:
                        fpath = os.path.join(info.log_dir, fname)
                        try:
                            with open(fpath, "r", encoding="utf-8") as fh:
                                merged_output += fh.read()
                        except Exception:
                            continue
                        read_paths.append(fpath)

                    for fpath in read_paths:
                        try:
                            os.remove(fpath)
                        except Exception:
                            pass

            # Completion is authoritative from the registry / state.json, and is
            # checked BEFORE waiting so an exit can never be missed even if its
            # event set() happened to race our clear() above.
            state_data = (_read_state(info.log_dir) if info.log_dir else None) or {}
            completed = state_data.get("status") == "completed"
            exit_code: Optional[int] = state_data.get("return_code")
            if not completed and info.process.returncode is not None:
                completed = True
                exit_code = info.process.returncode

            # Something to report — return it immediately.
            if merged_output or completed:
                _refresh_expire(info, variables)
                return self._build_output_result(
                    process_id=process_id,
                    info=info,
                    state=state,
                    merged_output=merged_output,
                    completed=completed,
                    exit_code=exit_code,
                    large_output_mode=large_output_mode,
                    token_threshold=token_threshold,
                )

            # Nothing yet and the process is still running.  Decide: wait or
            # heartbeat.  Early-heartbeat when we last saw the process sitting
            # at a prompt (no new output can come until input is sent — and
            # write_stdin_to_process clears last_input_mode, so this won't fire
            # after input was sent while we await a response).
            remaining = deadline - time.monotonic()
            pending_input = (
                isinstance(getattr(state, "last_input_mode", None), dict)
                and state.last_input_mode.get("input_mode") in ("text", "selection")
            )
            if state is None or remaining <= 0 or pending_input:
                _refresh_expire(info, variables)
                return self._build_heartbeat(process_id, info, state)

            try:
                await asyncio.wait_for(state.output_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                _refresh_expire(info, variables)
                return self._build_heartbeat(process_id, info, state)
            # Woke on new output / exit / teardown — loop and re-drain.

    def _build_output_result(
        self,
        *,
        process_id: str,
        info: ProcessInfo,
        state: Optional[LogWriterState],
        merged_output: str,
        completed: bool,
        exit_code: Optional[int],
        large_output_mode: str,
        token_threshold: int,
    ) -> BuiltInToolResult:
        """Assemble the structured result for a read that produced output or
        observed completion (the original post-processing path)."""
        # Classify the current input mode from the tail of stdout + persistent
        # terminal-state flags.  Only meaningful while still running.  Cache it
        # on the writer state so a later empty heartbeat — whose tail is gone
        # because we delete .log files after reading — can still guide the agent.
        mode_info: Optional[dict] = None
        if not completed and state is not None:
            mode_info = detect_input_mode(
                state.terminal_state,
                merged_output[-4096:],
                info.is_pty,
            )
            state.last_input_mode = mode_info

        total_tokens = 0
        truncated = False
        if merged_output:
            try:
                from tiktoken import encoding_for_model
                encoder = encoding_for_model("gpt-4o")
                total_tokens = len(encoder.encode(merged_output))
            except Exception:
                total_tokens = len(merged_output) // 4

            if (
                large_output_mode == "manual"
                and total_tokens > token_threshold
            ):
                truncated = True
                merged_output = (
                    f"[Output too large: {total_tokens} tokens "
                    f"(threshold: {token_threshold}). "
                    "Please confirm with the user before retrieving the full output.]"
                )

        structured: Dict[str, Any] = {
            "process_id": process_id,
            "stdout": merged_output,
            "completed": completed,
            "return_code": exit_code,
            "truncated": truncated,
            "total_tokens": total_tokens,
            "command": info.command,
        }
        if mode_info is not None:
            structured["input_mode"] = mode_info["input_mode"]
            structured["input_mode_confidence"] = mode_info["confidence"]
            structured["input_signals"] = mode_info["signals"]
        structured["instruction"] = _build_output_instruction(
            completed=completed,
            return_code=exit_code,
            input_mode=mode_info["input_mode"] if mode_info is not None else None,
            is_pty=info.is_pty,
            truncated=truncated,
        )
        return BuiltInToolResult(structured_content=structured)

    def _build_heartbeat(
        self,
        process_id: str,
        info: ProcessInfo,
        state: Optional[LogWriterState],
    ) -> BuiltInToolResult:
        """No new output after the long-poll window; process is still running.

        Reuse the last cached input-mode classification (the live tail is gone
        after deletion) so the agent keeps "waiting for input" guidance instead
        of degrading to "unknown".
        """
        last_mode = state.last_input_mode if state is not None else None
        input_mode = last_mode.get("input_mode") if isinstance(last_mode, dict) else None
        structured: Dict[str, Any] = {
            "process_id": process_id,
            "stdout": "",
            "completed": False,
            "return_code": None,
            "truncated": False,
            "total_tokens": 0,
            "command": info.command,
            "waiting": True,
            "message": "No new output yet; the process is still running.",
        }
        if isinstance(last_mode, dict):
            structured["input_mode"] = last_mode["input_mode"]
            structured["input_mode_confidence"] = last_mode["confidence"]
            structured["input_signals"] = last_mode["signals"]
        structured["instruction"] = _build_output_instruction(
            completed=False,
            return_code=None,
            input_mode=input_mode,
            is_pty=info.is_pty,
            truncated=False,
        )
        return BuiltInToolResult(structured_content=structured)


class ExecShellStopTool(BuiltInTool):
    name: str = "exec_shell_stop"
    description: str = (
        "Stop a running process by process_id. "
        "Sends SIGTERM, waits briefly, then SIGKILL if needed.\n"
        "E.g., stop process '708e9873'\n"
        "Required: process_id"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "process_id": {
                "type": "string",
                "description": "The process_id of the process to stop.",
            },
        },
        "required": ["process_id"],
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        # Thin adapter over ``stop_process`` — shared with the Process
        # Manager REST API.
        result = await stop_process(
            arguments.get("process_id", "").strip(),
            profile=arguments.get("_profile") or "",
        )
        return BuiltInToolResult(structured_content=result)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [
        ExecShellTool(),
        ExecShellInputTool(),
        ExecShellOutputTool(),
        ExecShellStopTool(),
    ]
