"""exec_shell_output batches bursty process output into one tool result.

A chatty process (an installer streaming progress lines) used to end the tool
call on its very first burst, so the agent paid one LLM reasoning step per
burst.  ``ExecShellOutputTool`` now keeps draining until the stream goes quiet
for ``OUTPUT_QUIET_WINDOW`` seconds — or the process exits, hits an interactive
prompt, fills a batch, matches a caller-supplied ``wait_until`` pattern, or runs
out of time.

Most tests drive a hand-rolled writer (``_emit``) rather than a real subprocess:
the reader only ever sees numbered ``.log`` files plus ``output_event``, so
faking that side makes burst timing exact instead of racy.  One test runs a real
child through ``_log_writer_loop`` to prove the two halves fit together.

Tests use ``asyncio.run`` per the repo idiom (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any, Dict, Optional
from unittest.mock import patch

from app.tools.builtin.exec_shell import (
    ExecShellOutputTool,
    LogWriterState,
    ProcessInfo,
    Var,
    _log_writer_loop,
    _process_registry,
    _wake_readers,
    _write_state,
)


class _FakeProc:
    """Minimal stand-in for asyncio.subprocess.Process (see
    tests/tools/test_shutdown_process_reap.py)."""

    def __init__(self, pid: int = 4242, returncode: Optional[int] = None):
        self.pid = pid
        self.returncode = returncode

    def terminate(self) -> None:  # pragma: no cover - never reached
        pass


def _setup(pid: str, log_dir: str) -> tuple[ProcessInfo, LogWriterState]:
    """Register a process whose writer we drive by hand."""
    os.makedirs(log_dir, exist_ok=True)
    state = LogWriterState(process_id=pid, log_dir=log_dir)
    info = ProcessInfo(
        process=_FakeProc(),
        created_at=0.0,
        working_dir=".",
        command="fake-command",
        log_dir=log_dir,
        log_writer_state=state,
        expire_time=float("inf"),
        is_pty=False,
        profile="test",
    )
    _process_registry[pid] = info
    return info, state


async def _emit(state: LogWriterState, text: str) -> None:
    """Write one burst the way _log_writer_loop does.

    The file write and the wake both happen under rotate_lock so a concurrent
    drain can never observe a half-written file — the ordering the no-lost-
    wakeup invariant depends on.
    """
    async with state.rotate_lock:
        path = os.path.join(state.log_dir, f"{state.current_file_number}.log")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
        state.current_file_number += 1
        _wake_readers(state)


async def _finish(state: LogWriterState, text: str, return_code: int) -> None:
    """Emit a final burst and mark the process completed, as the writer's exit
    path does (last chunk, then state.json, then wake)."""
    async with state.rotate_lock:
        if text:
            path = os.path.join(state.log_dir, f"{state.current_file_number}.log")
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(text)
            state.current_file_number += 1
        _write_state(
            state.log_dir,
            {"status": "completed", "return_code": return_code, "exited_at": 0.0},
        )
        _wake_readers(state)


async def _call(pid: str, variables: Dict[str, Any], **kwargs: Any) -> Dict[str, Any]:
    args: Dict[str, Any] = {"process_id": pid, "_variables": variables}
    args.update(kwargs)
    result = await ExecShellOutputTool().run(args)
    return result.structured_content


def test_bursts_coalesce_into_single_result(tmp_path):
    """Three bursts 0.2s apart come back as ONE result, not three."""

    async def _run():
        pid = "t_batch_coalesce"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 1.0, Var.OUTPUT_WAIT_TIMEOUT: 30}
            task = asyncio.ensure_future(_call(pid, variables))
            await asyncio.sleep(0.1)
            await _emit(state, "burst1\n")
            await asyncio.sleep(0.2)
            await _emit(state, "burst2\n")
            await asyncio.sleep(0.2)
            await _emit(state, "burst3\n")

            out = await asyncio.wait_for(task, timeout=15)
            assert out["stdout"] == "burst1\nburst2\nburst3\n"
            assert out["completed"] is False
            assert "waiting" not in out
            # Everything on disk was consumed.
            assert [f for f in os.listdir(tmp_path) if f.endswith(".log")] == []
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_quiet_window_returns_after_silence(tmp_path):
    """A single burst returns on the quiet window, not at the deadline."""

    async def _run():
        pid = "t_batch_quiet"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 0.3, Var.OUTPUT_WAIT_TIMEOUT: 30}
            started = time.monotonic()
            task = asyncio.ensure_future(_call(pid, variables))
            await asyncio.sleep(0.1)
            await _emit(state, "only burst")

            out = await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
            assert out["stdout"] == "only burst"
            assert out["completed"] is False
            assert elapsed < 5, f"returned at deadline, not quiet window ({elapsed}s)"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_exit_mid_accumulation_returns_everything_and_exit_code(tmp_path):
    """Exit beats the quiet window and carries the whole batch + return code."""

    async def _run():
        pid = "t_batch_exit"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 5, Var.OUTPUT_WAIT_TIMEOUT: 30}
            started = time.monotonic()
            task = asyncio.ensure_future(_call(pid, variables))
            await asyncio.sleep(0.1)
            await _emit(state, "burst1")
            await asyncio.sleep(0.2)
            await _finish(state, "burst2", return_code=7)

            out = await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
            assert out["stdout"] == "burst1burst2"
            assert out["completed"] is True
            assert out["return_code"] == 7
            assert elapsed < 4, f"waited out the 5s quiet window ({elapsed}s)"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_size_cap_triggers(tmp_path):
    """A flood returns on the size cap even though nothing else fires."""

    async def _run():
        pid = "t_batch_cap"
        _, state = _setup(pid, str(tmp_path))
        try:
            # threshold 25 tokens -> cap 100 chars.
            variables = {
                Var.OUTPUT_QUIET_WINDOW: 30,
                Var.OUTPUT_WAIT_TIMEOUT: 30,
                Var.LARGE_OUTPUT_TOKEN_THRESHOLD: 25,
            }
            started = time.monotonic()
            task = asyncio.ensure_future(_call(pid, variables))
            await asyncio.sleep(0.1)
            await _emit(state, "x" * 200)

            out = await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
            # Only the cap can explain a fast return: quiet window and deadline
            # are both 30s.
            assert elapsed < 5, f"cap did not fire ({elapsed}s)"
            assert out["stdout"] == "x" * 200
            assert out["completed"] is False
            assert out["truncated"] is False
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_size_cap_bounds_manual_mode_loss(tmp_path):
    """In manual mode the cap bounds how much output the too-large notice can
    discard (the .log files are already deleted by then)."""

    async def _run():
        pid = "t_batch_cap_manual"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {
                Var.OUTPUT_QUIET_WINDOW: 30,
                Var.OUTPUT_WAIT_TIMEOUT: 30,
                Var.LARGE_OUTPUT_MODE: "manual",
                Var.LARGE_OUTPUT_TOKEN_THRESHOLD: 10,
            }
            task = asyncio.ensure_future(_call(pid, variables))
            await asyncio.sleep(0.1)
            # Varied text: tiktoken compresses a repeated character hard, and
            # the truncation decision uses the real encoder.
            await _emit(
                state, "".join(f"line {i} of installer output\n" for i in range(40))
            )

            out = await asyncio.wait_for(task, timeout=15)
            assert out["truncated"] is True
            assert "Output too large" in out["stdout"]
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_prompt_detection_early_returns(tmp_path):
    """An interactive prompt returns immediately — no point waiting out the
    quiet window when no more output can arrive without input."""

    async def _run():
        pid = "t_batch_prompt"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 30, Var.OUTPUT_WAIT_TIMEOUT: 30}
            fake_mode = {
                "input_mode": "text",
                "confidence": 0.9,
                "score": 3,
                "signals": ["mock"],
            }
            with patch(
                "app.tools.builtin.exec_shell.detect_input_mode",
                return_value=fake_mode,
            ):
                started = time.monotonic()
                task = asyncio.ensure_future(_call(pid, variables))
                await asyncio.sleep(0.1)
                await _emit(state, "Enter name: ")
                out = await asyncio.wait_for(task, timeout=15)
                elapsed = time.monotonic() - started

            assert elapsed < 5, f"waited out the quiet window ({elapsed}s)"
            assert out["stdout"] == "Enter name: "
            assert out["input_mode"] == "text"
            assert state.last_input_mode == fake_mode
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_empty_path_still_heartbeats(tmp_path):
    """With no output at all, the call still returns the "still running"
    heartbeat at the deadline — unchanged behavior."""

    async def _run():
        pid = "t_batch_heartbeat"
        _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 2, Var.OUTPUT_WAIT_TIMEOUT: 0.7}
            started = time.monotonic()
            out = await asyncio.wait_for(_call(pid, variables), timeout=15)
            elapsed = time.monotonic() - started

            assert out["waiting"] is True
            assert out["stdout"] == ""
            assert out["completed"] is False
            assert 0.5 < elapsed < 5, elapsed
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_deadline_with_collected_returns_output_not_heartbeat(tmp_path):
    """When the deadline expires with output in hand, return the output — a
    heartbeat would silently drop it (the .log files are gone)."""

    async def _run():
        pid = "t_batch_deadline"
        _, state = _setup(pid, str(tmp_path))
        try:
            # quiet_window > max_wait, so only the deadline can end the call.
            variables = {Var.OUTPUT_QUIET_WINDOW: 30, Var.OUTPUT_WAIT_TIMEOUT: 1.0}
            task = asyncio.ensure_future(_call(pid, variables))
            await asyncio.sleep(0.1)
            await _emit(state, "partial output")

            out = await asyncio.wait_for(task, timeout=15)
            assert out["stdout"] == "partial output"
            assert out["completed"] is False
            assert "waiting" not in out
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_quiet_window_zero_is_legacy_first_burst(tmp_path):
    """quiet_window=0 restores return-on-first-burst.

    Also guards the read path: 0 is falsy, so the usual
    ``variables.get(...) or DEFAULT`` idiom would silently ignore it.
    """

    async def _run():
        pid = "t_batch_zero"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 0, Var.OUTPUT_WAIT_TIMEOUT: 30}
            await _emit(state, "burst1")
            out = await asyncio.wait_for(_call(pid, variables), timeout=15)
            assert out["stdout"] == "burst1"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_registry_vanish_mid_wait_returns_collected(tmp_path):
    """A teardown that pops the process mid-accumulation must still hand back
    what we drained — those bytes exist nowhere else."""

    async def _run():
        pid = "t_batch_vanish"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 5, Var.OUTPUT_WAIT_TIMEOUT: 30}
            task = asyncio.ensure_future(_call(pid, variables))
            await asyncio.sleep(0.1)
            await _emit(state, "drained before teardown")
            # Let the reader drain and settle into its quiet-window wait.
            await asyncio.sleep(0.3)
            _process_registry.pop(pid, None)
            _wake_readers(state)

            out = await asyncio.wait_for(task, timeout=15)
            assert "error" not in out, out
            assert out["stdout"] == "drained before teardown"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_wait_until_matches_mid_stream(tmp_path):
    """wait_until returns as soon as the pattern shows up."""

    async def _run():
        pid = "t_batch_wait_hit"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 30, Var.OUTPUT_WAIT_TIMEOUT: 30}
            started = time.monotonic()
            task = asyncio.ensure_future(
                _call(pid, variables, wait_until="Installation complete")
            )
            await asyncio.sleep(0.1)
            await _emit(state, "downloading...\n")
            await asyncio.sleep(0.2)
            await _emit(state, "Installation complete\n")

            out = await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
            assert out["stdout"] == "downloading...\nInstallation complete\n"
            assert out["wait_until_matched"] is True
            assert elapsed < 5, elapsed
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_wait_until_ignores_ansi_codes(tmp_path):
    """Styled output still matches a plain pattern."""

    async def _run():
        pid = "t_batch_wait_ansi"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 30, Var.OUTPUT_WAIT_TIMEOUT: 30}
            task = asyncio.ensure_future(_call(pid, variables, wait_until="DONE"))
            await asyncio.sleep(0.1)
            await _emit(state, "\x1b[32mDONE\x1b[0m\n")

            out = await asyncio.wait_for(task, timeout=15)
            assert out["wait_until_matched"] is True
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_wait_until_suppresses_quiet_window(tmp_path):
    """With wait_until set, silence alone must not end the call — the agent
    asked to wait for a pattern, so ride quiet phases out to the deadline."""

    async def _run():
        pid = "t_batch_wait_miss"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 0.2, Var.OUTPUT_WAIT_TIMEOUT: 1.5}
            started = time.monotonic()
            task = asyncio.ensure_future(_call(pid, variables, wait_until="NEVER"))
            await asyncio.sleep(0.1)
            await _emit(state, "phase one\n")
            await asyncio.sleep(0.5)  # >> the 0.2s quiet window
            await _emit(state, "phase two\n")

            out = await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
            # Both bursts made it into one result despite a 0.5s silence.
            assert out["stdout"] == "phase one\nphase two\n"
            assert out["wait_until_matched"] is False
            assert elapsed >= 1.0, f"returned on the quiet window ({elapsed}s)"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_wait_until_invalid_regex_errors(tmp_path):
    """A bad pattern fails fast instead of blocking for the full window."""

    async def _run():
        pid = "t_batch_wait_bad"
        _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 30, Var.OUTPUT_WAIT_TIMEOUT: 30}
            started = time.monotonic()
            out = await asyncio.wait_for(
                _call(pid, variables, wait_until="(unclosed"), timeout=15
            )
            elapsed = time.monotonic() - started
            assert out["error"] == "Invalid wait_until pattern"
            assert elapsed < 5, f"blocked before validating ({elapsed}s)"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_per_call_quiet_window_overrides_variable(tmp_path):
    """The per-call parameter wins over the configured variable."""

    async def _run():
        pid = "t_batch_param_quiet"
        _, state = _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 30, Var.OUTPUT_WAIT_TIMEOUT: 30}
            started = time.monotonic()
            task = asyncio.ensure_future(_call(pid, variables, quiet_window=0.2))
            await asyncio.sleep(0.1)
            await _emit(state, "burst")

            out = await asyncio.wait_for(task, timeout=15)
            elapsed = time.monotonic() - started
            assert out["stdout"] == "burst"
            assert elapsed < 5, f"used the 30s variable, not the param ({elapsed}s)"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_per_call_max_wait_overrides_variable(tmp_path):
    """max_wait shortens the total call the same way."""

    async def _run():
        pid = "t_batch_param_wait"
        _setup(pid, str(tmp_path))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 2, Var.OUTPUT_WAIT_TIMEOUT: 30}
            started = time.monotonic()
            out = await asyncio.wait_for(
                _call(pid, variables, max_wait=0.6), timeout=15
            )
            elapsed = time.monotonic() - started
            assert out["waiting"] is True
            assert elapsed < 5, f"used the 30s variable, not the param ({elapsed}s)"
        finally:
            _process_registry.pop(pid, None)

    asyncio.run(_run())


def test_real_process_bursts_coalesce_to_completion(tmp_path):
    """End-to-end against the real writer: a child printing three bursts then
    exiting is read by a single call."""

    child = (
        "import sys, time\n"
        "for chunk in ('AAA', 'BBB', 'CCC'):\n"
        "    sys.stdout.write(chunk + '\\n'); sys.stdout.flush(); time.sleep(0.3)\n"
    )

    async def _run():
        pid = "t_batch_real"
        log_dir = str(tmp_path)
        os.makedirs(log_dir, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", child,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        state = LogWriterState(
            process_id=pid, log_dir=log_dir, silence_threshold=0.2,
        )
        info = ProcessInfo(
            process=proc,
            created_at=0.0,
            working_dir=".",
            command="python -c <burst printer>",
            log_dir=log_dir,
            log_writer_state=state,
            expire_time=float("inf"),
            is_pty=False,
            profile="test",
        )
        _process_registry[pid] = info
        state.task = asyncio.ensure_future(_log_writer_loop(proc, state))
        try:
            variables = {Var.OUTPUT_QUIET_WINDOW: 2, Var.OUTPUT_WAIT_TIMEOUT: 30}
            out = await asyncio.wait_for(_call(pid, variables), timeout=30)

            assert out["completed"] is True
            assert out["return_code"] == 0
            for chunk in ("AAA", "BBB", "CCC"):
                assert chunk in out["stdout"], out["stdout"]
        finally:
            _process_registry.pop(pid, None)
            state.stopped = True
            if state.task is not None:
                state.task.cancel()
                try:
                    await state.task
                except (asyncio.CancelledError, Exception):
                    pass
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

    asyncio.run(_run())
