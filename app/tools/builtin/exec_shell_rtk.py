"""RTK (Rust Token Killer) command rewriter for the Exec Shell tool.

`rtk rewrite "<cmd>"` is RTK's pure-string transformer: it inspects the user
command, returns the wrapped form on stdout (e.g. ``git status`` ->
``rtk git status``) when it can compress the output, and signals passthrough
via exit code 1 when it can't.  Exit-code protocol per
``rtk/src/hooks/rewrite_cmd.rs``::

    0 -> rewrite found        (use stdout)
    1 -> no equivalent        (passthrough; not an error)
    2 -> deny                 (passthrough; native deny handles it)
    3 -> ask                  (passthrough; we don't surface prompts here)

The integration is non-blocking by design: any failure (binary missing,
timeout, decode error, unexpected exit code) silently returns the original
command so the agent's shell call still runs.
"""

import asyncio
from typing import Literal

from app.utils.logger import logger

_PROBE_TIMEOUT_S = 2.0
_REWRITE_TIMEOUT_S = 2.0

_RtkStatus = Literal["unknown", "available", "missing"]
_rtk_status: _RtkStatus = "unknown"
_warned_missing = False


async def _probe(binary: str) -> bool:
    """Run ``<binary> --version`` once.  True if it exits 0 within the timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_PROBE_TIMEOUT_S)
        except asyncio.TimeoutError:
            with _suppress():
                proc.kill()
            return False
        return proc.returncode == 0
    except (FileNotFoundError, OSError):
        return False


class _suppress:
    """Tiny context manager: swallow any exception from the body."""
    def __enter__(self): return self
    def __exit__(self, *_): return True


async def maybe_rewrite_command(command: str, *, enabled: bool, binary: str) -> str:
    """Return the RTK-rewritten command, or ``command`` unchanged on any failure.

    Never raises.  Logs a single warning the first time the binary is found
    to be missing/broken; subsequent calls in the same process short-circuit
    via the ``_rtk_status`` cache and stay silent.
    """
    global _rtk_status, _warned_missing

    if not enabled or not command.strip():
        return command

    if _rtk_status == "unknown":
        _rtk_status = "available" if await _probe(binary) else "missing"

    if _rtk_status == "missing":
        if not _warned_missing:
            _warned_missing = True
            logger.warning(
                f"RTK rewriter enabled but '{binary}' is not callable. "
                "Falling back to raw commands. Set RTK_BINARY_PATH to an absolute "
                "path or install rtk (https://github.com/rtk-ai/rtk)."
            )
        return command

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "rewrite", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, _stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=_REWRITE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            with _suppress():
                proc.kill()
            logger.debug(f"rtk rewrite timed out for command: {command!r}")
            return command

        rc = proc.returncode
        if rc == 0:
            rewritten = stdout_b.decode("utf-8", errors="replace").strip()
            if rewritten:
                logger.debug(f"rtk rewrite: {command!r} -> {rewritten!r}")
                return rewritten
            return command
        if rc == 1:
            return command
        logger.debug(f"rtk rewrite returned exit {rc} for command: {command!r}")
        return command
    except Exception as exc:
        logger.debug(f"rtk rewrite errored ({exc!r}); using raw command")
        return command
