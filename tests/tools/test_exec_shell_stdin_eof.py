"""exec_shell stdin/EOF handling: no read-until-EOF command should hang.

Regression for the Agent hang where a command that reads stdin until EOF
(``cat``, ``cremind profile persona set <name>``) never completed under
exec_shell, because the child's stdin was an open, non-TTY pipe that never
received EOF, so ``sys.stdin.read()`` blocked forever.

Two layers are covered:
  * Reactive — ``write_stdin_to_process(..., close_stdin=True)`` sends EOF so a
    process the agent is driving can finish.
  * Proactive — ``_classify_stream(auto_close_stdin_when_idle=True)`` auto-closes
    stdin for a non-PTY command that goes idle having printed nothing (the
    silent read-until-EOF signature), while leaving a command that printed a
    prompt waiting for the agent; and ``_prime_stdin`` writes an up-front
    ``stdin`` payload then closes it.

These drive a real subprocess that reads all of stdin then reports its length,
so they prove EOF actually unblocks the read.  Tests use ``asyncio.run`` per the
repo idiom (no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import sys

from app.tools.builtin.exec_shell import (
    ProcessInfo,
    _classify_stream,
    _prime_stdin,
    _process_registry,
    write_stdin_to_process,
)

# Reads everything on stdin (until EOF) then writes how many chars it got.
_READER = (
    "import sys; data = sys.stdin.read(); "
    "sys.stdout.write('GOT:%d' % len(data)); sys.stdout.flush()"
)

# Prints a prompt to stderr first, then reads stdin until EOF — the interactive
# signature that must NOT be auto-closed.
_PROMPT_READER = (
    "import sys; sys.stderr.write('Enter name: '); sys.stderr.flush(); "
    "data = sys.stdin.read(); "
    "sys.stdout.write('GOT:%d' % len(data)); sys.stdout.flush()"
)


async def _spawn_reader(pid: str, profile: str = "test") -> ProcessInfo:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _READER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    info = ProcessInfo(
        process=proc,
        created_at=0.0,
        working_dir=".",
        command="python -c <stdin reader>",
        is_pty=False,
        profile=profile,
    )
    _process_registry[pid] = info
    return info


async def _cleanup(pid: str) -> None:
    info = _process_registry.pop(pid, None)
    if info is None:
        return
    proc = info.process
    if proc.returncode is None:
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


def test_input_then_close_completes_process():
    async def _run():
        pid = "t_eof_1"
        info = await _spawn_reader(pid)
        try:
            result = await write_stdin_to_process(
                pid, profile="test", input_text="hello", close_stdin=True,
            )
            assert "error" not in result, result
            assert result["input_sent"] is True
            assert result["stdin_closed"] is True

            out = await asyncio.wait_for(info.process.stdout.read(), timeout=5)
            await asyncio.wait_for(info.process.wait(), timeout=5)
            assert info.process.returncode == 0
            # "hello" + the default line_ending "\n" == 6 chars.
            assert out.decode() == "GOT:6"
        finally:
            await _cleanup(pid)

    asyncio.run(_run())


def test_without_close_process_keeps_running():
    async def _run():
        pid = "t_eof_2"
        info = await _spawn_reader(pid)
        try:
            result = await write_stdin_to_process(
                pid, profile="test", input_text="hello",
            )
            assert result["input_sent"] is True
            assert "stdin_closed" not in result
            # No EOF -> the reader is still blocked in sys.stdin.read().
            await asyncio.sleep(0.3)
            assert info.process.returncode is None
        finally:
            await _cleanup(pid)

    asyncio.run(_run())


def test_close_only_after_prior_input_completes():
    async def _run():
        pid = "t_eof_3"
        info = await _spawn_reader(pid)
        try:
            r1 = await write_stdin_to_process(
                pid, profile="test", input_text="abc",
            )
            assert r1["input_sent"] is True
            await asyncio.sleep(0.1)
            assert info.process.returncode is None

            # Close-only call (no input) delivers the EOF that lets it finish.
            r2 = await write_stdin_to_process(pid, profile="test", close_stdin=True)
            assert "error" not in r2, r2
            assert r2["input_sent"] is False
            assert r2["stdin_closed"] is True

            out = await asyncio.wait_for(info.process.stdout.read(), timeout=5)
            await asyncio.wait_for(info.process.wait(), timeout=5)
            assert info.process.returncode == 0
            assert out.decode() == "GOT:4"  # "abc" + "\n"
        finally:
            await _cleanup(pid)

    asyncio.run(_run())


def test_close_stdin_rejected_with_keys():
    async def _run():
        pid = "t_eof_4"
        await _spawn_reader(pid)
        try:
            result = await write_stdin_to_process(
                pid,
                profile="test",
                mode="action",
                keys=["enter"],
                close_stdin=True,
            )
            assert result.get("error") == "Invalid parameters", result
        finally:
            await _cleanup(pid)

    asyncio.run(_run())


def test_write_after_close_is_rejected():
    async def _run():
        pid = "t_eof_5"
        info = await _spawn_reader(pid)
        try:
            r1 = await write_stdin_to_process(
                pid, profile="test", input_text="x", close_stdin=True,
            )
            assert r1["stdin_closed"] is True
            # Once stdin is closed, further writes must be refused.
            r2 = await write_stdin_to_process(
                pid, profile="test", input_text="more",
            )
            assert r2.get("error") == "Stdin closed", r2
            await asyncio.wait_for(info.process.wait(), timeout=5)
        finally:
            await _cleanup(pid)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Proactive path: classification auto-EOF + up-front stdin priming.  These
# spawn a raw subprocess (no registry) and exercise the internals directly.
# ---------------------------------------------------------------------------


async def _spawn_raw(code: str = _READER):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _kill(proc) -> None:
    if proc.returncode is None:
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception:
        pass


def test_classify_auto_eof_completes_silent_reader():
    """A silent read-until-EOF command is auto-closed and completes."""
    async def _run():
        proc = await _spawn_raw()
        try:
            result = await _classify_stream(
                proc,
                overall_timeout=10.0,
                silence_timeout=0.3,
                long_running_timeout=0.3,
                auto_close_stdin_when_idle=True,
            )
            assert result["completed"] is True, result
            assert result["category"] == "fire_and_forget", result
            # Empty stdin -> EOF -> the reader saw 0 chars.
            assert result["stdout"] == "GOT:0", result
            assert proc.returncode == 0
        finally:
            await _kill(proc)

    asyncio.run(_run())


def test_classify_no_auto_close_stays_long_running():
    """Without auto-close, the same reader stays blocked (long_running)."""
    async def _run():
        proc = await _spawn_raw()
        try:
            result = await _classify_stream(
                proc,
                overall_timeout=3.0,
                silence_timeout=0.3,
                long_running_timeout=0.3,
                auto_close_stdin_when_idle=False,
            )
            assert result["completed"] is False, result
            assert result["category"] == "long_running", result
            assert result.get("stdin_closed") is False, result
            assert proc.returncode is None
        finally:
            await _kill(proc)

    asyncio.run(_run())


def test_classify_prompt_reader_not_auto_closed():
    """A command that printed a prompt is left waiting, not auto-closed."""
    async def _run():
        proc = await _spawn_raw(_PROMPT_READER)
        try:
            # Let the child start and flush its prompt into the pipe before
            # classification, so the "no output" gate reliably sees the prompt.
            # (Production's silence window is seconds; the race here is only the
            # sub-second interpreter startup under CI load.)
            await asyncio.sleep(1.0)
            result = await _classify_stream(
                proc,
                overall_timeout=6.0,
                silence_timeout=1.0,
                long_running_timeout=1.0,
                auto_close_stdin_when_idle=True,
            )
            assert result["completed"] is False, result
            assert result["category"] == "long_running", result
            # Had output (the prompt) -> EOF was NOT sent -> still blocked.
            assert result.get("stdin_closed") is False, result
            assert proc.returncode is None
        finally:
            await _kill(proc)

    asyncio.run(_run())


def test_prime_stdin_writes_and_closes():
    """Up-front stdin is written then closed, so the reader completes."""
    async def _run():
        proc = await _spawn_raw()
        try:
            await _prime_stdin(proc, False, "hello", keep_stdin_open=False)
            out = await asyncio.wait_for(proc.stdout.read(), timeout=5)
            await asyncio.wait_for(proc.wait(), timeout=5)
            assert proc.returncode == 0
            assert out.decode() == "GOT:5"  # raw "hello", no line ending added
        finally:
            await _kill(proc)

    asyncio.run(_run())


def test_prime_stdin_keep_open_does_not_close():
    """keep_stdin_open writes the payload but leaves stdin open (no EOF)."""
    async def _run():
        proc = await _spawn_raw()
        try:
            await _prime_stdin(proc, False, "hi", keep_stdin_open=True)
            await asyncio.sleep(0.3)
            # No EOF sent -> the reader is still blocked in sys.stdin.read().
            assert proc.returncode is None
        finally:
            await _kill(proc)

    asyncio.run(_run())
