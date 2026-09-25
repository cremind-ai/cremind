"""The extractor child process: ``python -m app.userdocs.extract.worker``.

**Protocol.** Frames on stdin/stdout, each a 4-byte big-endian length and a
UTF-8 JSON object. Requests and replies alternate strictly; the parent never
sends a second request before the first reply.

- ``{"op": "ping"}`` → ``{"ok": true, "op": "pong", "pid": <real pid>}``
- ``{"op": "extract", "req": {name, kind, path, data_b64, limits}, "deadline_s": N}``
  → ``{"ok": true, "result": ExtractResult.to_dict()}``
- anything malformed → ``{"ok": false, "error": "..."}``

**The protocol pipe is guarded before anything else runs.** A library that
prints (to ``sys.stdout`` or straight to file descriptor 1 from C) would
corrupt a frame, and one that reads stdin would swallow one. So the first
thing :func:`main` does is duplicate fds 0 and 1 for the protocol, then point
fd 1 at stderr and fd 0 at the null device. Only then are the extractors
imported.

**The worker polices itself.** On Windows the ``Popen`` handle of a venv
Python is the launcher stub, not this interpreter, so the parent cannot
measure or cap this process's memory. A daemon thread samples its own resident
size every 0.5 s and exits with code 86 (which the pool reports as ``oom``)
above ``CREMIND_EXTRACT_MEM_MB``. The same thread exits with 87 when one file
runs far past the parent's deadline, so a worker orphaned by a crashed server
cannot spin forever. On POSIX ``RLIMIT_AS`` backs this up (a runaway
allocation then fails as MemoryError inside the parser), and the process
lowers its own CPU priority everywhere.

With ``CREMIND_EXTRACT_TEST_OPS=1`` an extract request may carry
``limits["test_op"]`` (``sleep``, ``alloc``, ``crash`` or ``noise``), run
before the extraction. It exists only so tests can drive the pool's kill
paths through its public ``extract``; without the variable the key is ignored.
"""

from __future__ import annotations

# Standard library only up here: nothing below may print before main() has
# moved the protocol off fd 1.
import base64
import gc
import json
import os
import struct
import sys
import threading
import time
from typing import BinaryIO, Callable

EXIT_OOM = 86
EXIT_RUNAWAY = 87
_MAX_FRAME = 1024 * 1024 * 1024
_SAMPLE_S = 0.5
# Grace past the parent's deadline before an orphaned request kills itself.
_RUNAWAY_GRACE_S = 60.0


def _isolate_stdio() -> tuple[BinaryIO, BinaryIO]:
    """Private binary streams for the protocol; fd 0/1 no longer reach it."""
    proto_in = os.fdopen(os.dup(0), "rb")
    proto_out = os.fdopen(os.dup(1), "wb")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    os.close(devnull)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    return proto_in, proto_out


def _rss_reader() -> Callable[[], int] | None:
    """A function returning this process's resident bytes, or None."""
    if sys.platform.startswith("linux"):
        page = os.sysconf("SC_PAGE_SIZE")

        def linux() -> int:
            with open("/proc/self/statm", "rb") as fh:
                return int(fh.read().split()[1]) * page

        return linux
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class _Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
        kernel32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
        process = kernel32.GetCurrentProcess()

        def windows() -> int:
            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            if not kernel32.K32GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb):
                raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
            return int(counters.WorkingSetSize)

        return windows
    try:
        import resource
    except ImportError:
        return None
    # macOS and the BSDs have no cheap current-RSS call; the peak is enough
    # for a kill switch (it only rises past the limit when usage did).
    scale = 1 if sys.platform == "darwin" else 1024

    def peak() -> int:
        return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * scale

    return peak


class _Watchdog:
    """Memory and runaway guard, run on a daemon thread."""

    def __init__(self, mem_limit_mb: int) -> None:
        self.limit = mem_limit_mb * 1024 * 1024
        self.busy_since: float | None = None
        self.deadline_s: float | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="extract-watchdog", daemon=True).start()

    def _run(self) -> None:
        reader = _rss_reader() if self.limit > 0 else None
        while True:
            time.sleep(_SAMPLE_S)
            if reader is not None:
                try:
                    rss = reader()
                except Exception:
                    reader = None  # measuring failed once; it will not start working
                else:
                    if rss > self.limit:
                        self._die(EXIT_OOM, f"resident memory {rss >> 20} MB is over {self.limit >> 20} MB")
            since, deadline = self.busy_since, self.deadline_s
            if since is not None and deadline and time.monotonic() - since > deadline + _RUNAWAY_GRACE_S:
                self._die(EXIT_RUNAWAY, f"one file ran {deadline + _RUNAWAY_GRACE_S:.0f}s; parent gone?")

    @staticmethod
    def _die(code: int, why: str) -> None:
        try:
            os.write(2, f"[extract-worker] exiting: {why}\n".encode("utf-8", "replace"))
        except OSError:
            pass
        os._exit(code)


def _lower_priority() -> None:
    try:
        if sys.platform == "win32":
            import ctypes

            below_normal = 0x00004000
            kernel32 = ctypes.WinDLL("kernel32")
            kernel32.GetCurrentProcess.restype = ctypes.c_void_p
            kernel32.SetPriorityClass(ctypes.c_void_p(kernel32.GetCurrentProcess()), below_normal)
        else:
            os.nice(10)
    except Exception:
        pass  # best effort: a normal-priority worker still works


def _limit_address_space(mem_limit_mb: int) -> None:
    if sys.platform == "win32" or mem_limit_mb <= 0:
        return
    try:
        import resource

        cap = mem_limit_mb * 4 * 1024 * 1024
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            cap = min(cap, hard)
        resource.setrlimit(resource.RLIMIT_AS, (cap, hard))
    except Exception:
        pass  # macOS refuses RLIMIT_AS; the RSS watchdog still applies


def _read_frame(stream: BinaryIO) -> bytes | None:
    header = stream.read(4)
    if len(header) < 4:
        return None
    (size,) = struct.unpack(">I", header)
    if size > _MAX_FRAME:
        raise ValueError(f"frame of {size} bytes is over the limit")
    payload = stream.read(size)
    if len(payload) < size:
        return None
    return payload


def _write_frame(stream: BinaryIO, message: dict) -> None:
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8", "replace")
    stream.write(struct.pack(">I", len(body)))
    stream.write(body)
    stream.flush()


def _run_test_op(spec: dict) -> None:
    """Kill-path fixtures, run before an extraction; only reachable with
    CREMIND_EXTRACT_TEST_OPS=1."""
    op = spec.get("op")
    if op == "sleep":
        time.sleep(min(float(spec.get("seconds", 1)), 600))
    elif op == "alloc":
        hold = b"x" * (int(spec.get("mb", 1)) * 1024 * 1024)  # written, so resident
        time.sleep(min(float(spec.get("seconds", 5)), 600))
        del hold
    elif op == "crash":
        os._exit(int(spec.get("code", 3)))
    elif op == "noise":
        print("stray print to stdout")
        os.write(1, b"raw bytes on fd 1\n")


def main() -> int:
    proto_in, proto_out = _isolate_stdio()
    os.environ["DISABLE_LOG"] = "true"  # if anything imports app.utils.logger: no file sink

    try:
        mem_limit_mb = int(os.environ.get("CREMIND_EXTRACT_MEM_MB", "1536"))
    except ValueError:
        mem_limit_mb = 1536
    test_ops = os.environ.get("CREMIND_EXTRACT_TEST_OPS") == "1"

    _lower_priority()
    _limit_address_space(mem_limit_mb)
    watchdog = _Watchdog(mem_limit_mb)
    watchdog.start()

    from loguru import logger

    logger.remove()
    logger.add(sys.stderr, level="DEBUG", format="{level} {name}: {message}")

    from app.userdocs.extract.dispatch import extract_any
    from app.userdocs.types import ExtractRequest

    while True:
        try:
            frame = _read_frame(proto_in)
        except ValueError as exc:
            _write_frame(proto_out, {"ok": False, "error": str(exc)})
            return 2  # the stream is out of sync; only a restart recovers
        if frame is None:
            return 0  # parent closed the pipe
        try:
            message = json.loads(frame)
            op = message.get("op")
        except (ValueError, AttributeError) as exc:
            _write_frame(proto_out, {"ok": False, "error": f"bad request: {exc}"})
            continue
        finally:
            # The raw frame is a full copy of any inline data; the parsed
            # message is all that is needed from here on.
            del frame
        if op == "ping":
            reply = {"ok": True, "op": "pong", "pid": os.getpid()}
        elif op == "extract":
            wire = message.get("req") or {}
            deadline_s = float(message.get("deadline_s") or 0) or None
            # Pop, so the message no longer holds the base64 string once it
            # is decoded: an in-memory file (a Drive download) would otherwise
            # sit in the worker three times over while it is parsed.
            data_b64 = wire.pop("data_b64", None)
            limits = dict(wire.get("limits") or {})
            test_op = limits.pop("test_op", None)
            name, kind, path = wire.get("name") or "", wire.get("kind") or "", wire.get("path")
            del message, wire
            # b"" is data too: a blank Google Doc exports as zero bytes and
            # must extract as empty, not fail as "no input".
            data = base64.b64decode(data_b64) if data_b64 is not None else None
            del data_b64
            req = ExtractRequest(name=name, kind=kind, path=path, data=data, limits=limits)
            del data
            watchdog.deadline_s = deadline_s
            watchdog.busy_since = time.monotonic()
            try:
                if test_ops and isinstance(test_op, dict):
                    _run_test_op(test_op)
                reply = {"ok": True, "result": extract_any(req).to_dict()}
            finally:
                watchdog.busy_since = None
            del req
        else:
            reply = {"ok": False, "error": f"unknown op {op!r}"}
        _write_frame(proto_out, reply)
        del reply
        gc.collect()  # one big PDF's garbage must not ride along to the next file


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
