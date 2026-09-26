"""The parent's side of extraction: a small pool of worker processes.

``ExtractorPool.extract`` is called from the engine's worker threads. It takes
an idle worker (starting one lazily, up to ``size``), sends one request and
waits for the reply under a deadline. A worker that misses the deadline is
killed and the file comes back ``metadata_only(timeout)``; one that dies comes
back ``oom`` (it exited with the watchdog's code 86, or was SIGKILLed, which in
a container is the kernel's OOM killer) or ``extractor_crash``. The slot is
refilled lazily on the next request, so one poisonous file costs one file.

**Nothing blocks forever.** The reply is read by a per-worker thread into a
queue, and the caller waits on that queue with a timeout, so a child that
hangs without closing its pipe cannot hang the caller. The one blocking
write (sending a large in-memory file) is covered by a timer that kills the
child at the deadline, which breaks the pipe.

**Temp files.** Each worker runs with its cwd and ``TMP``/``TEMP``/``TMPDIR``
set to ``<tmp_root>/worker-<n>``, wiped when the worker goes. Extractors are
not supposed to write at all; this only contains a library that does anyway.
``PYTHONDONTWRITEBYTECODE`` stops the child writing ``.pyc`` files next to
the code.

**Windows.** ``sys.executable`` of a venv is a launcher stub that runs the real
interpreter as its child, so ``Popen.pid`` is the stub's. Killing the stub
kills the child (the launcher holds it in a kill-on-close job), and the stub
passes the child's exit code through, but memory has to be measured inside the
child: see :mod:`.worker`.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Any

from app.documents.types import (
    EXTRACT_ERROR,
    EXTRACT_METADATA_ONLY,
    KIND_PDF,
    ExtractRequest,
    ExtractResult,
)
from app.utils.logger import logger

__all__ = ["ExtractorPool", "wipe_stale_tmp"]

_WORKER_MODULE = "app.documents.extract.worker"
_TMP_PREFIX = "worker-"
_STARTUP_TIMEOUT_S = 30.0
_REAP_TIMEOUT_S = 5.0
_RETIRE_GRACE_S = 2.0
_MAX_FRAME = 1024 * 1024 * 1024
_EXIT_OOM = 86
_SIGKILL = -9

# The directory holding the ``app`` package, put on the child's PYTHONPATH:
# the child's cwd is its temp dir, so ``-m app...`` cannot rely on the cwd.
_CODE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

REASON_TIMEOUT = "timeout"
REASON_OOM = "oom"
REASON_CRASH = "extractor_crash"
REASON_UNAVAILABLE = "extractor_unavailable"
REASON_PROTOCOL = "extractor_protocol"
REASON_CLOSED = "pool_closed"

_EOF = object()      # the worker's stdout closed
_CLOSED = object()   # close() woke the caller
_TIMEOUT = object()  # the deadline passed


def _wipe(path: str) -> None:
    """Remove a worker temp dir. On Windows a just-killed process can hold
    its cwd for a moment, so retry briefly before giving up."""
    for attempt in range(5):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            return
        time.sleep(0.1 * (attempt + 1))


def wipe_stale_tmp(tmp_root: str) -> int:
    """Delete worker temp dirs a previous run left under ``tmp_root`` (a crash
    or a kill skips the normal cleanup). Only ``worker-*`` entries are
    touched, so a misconfigured root loses nothing else. Returns how many
    were removed. Call at boot, before any pool starts."""
    os.makedirs(tmp_root, exist_ok=True)
    removed = 0
    with os.scandir(tmp_root) as entries:
        stale = [e.path for e in entries
                 if e.name.startswith(_TMP_PREFIX) and e.is_dir(follow_symlinks=False)]
    for path in stale:
        _wipe(path)
        removed += 1
    if removed:
        logger.info(f"[documents:extract] removed {removed} stale worker temp dir(s) under {tmp_root}")
    return removed


class _SpawnError(Exception):
    pass


class _Worker:
    """One child process and the threads that drain its pipes."""

    def __init__(self, slot: int, proc: subprocess.Popen, tmp_dir: str) -> None:
        self.slot = slot
        self.proc = proc
        self.tmp_dir = tmp_dir
        self.replies: queue.Queue[Any] = queue.Queue()
        self.files = 0
        self.pid: int | None = None
        self.expired = False
        self._reader = threading.Thread(target=self._read_replies, name=f"documents-extract-w{slot}-out",
                                        daemon=True)
        self._logger = threading.Thread(target=self._forward_stderr, name=f"documents-extract-w{slot}-err",
                                        daemon=True)
        self._reader.start()
        self._logger.start()

    def _read_replies(self) -> None:
        stream = self.proc.stdout
        try:
            while True:
                header = stream.read(4)
                if len(header) < 4:
                    break
                (size,) = struct.unpack(">I", header)
                if size > _MAX_FRAME:
                    break
                body = stream.read(size)
                if len(body) < size:
                    break
                try:
                    self.replies.put(json.loads(body))
                except ValueError:
                    self.replies.put({"ok": False, "error": "unparseable reply"})
        except (OSError, ValueError):
            pass
        finally:
            self.replies.put(_EOF)

    def _forward_stderr(self) -> None:
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    logger.debug(f"[documents:extract:w{self.slot}] {line[:2000]}")
        except (OSError, ValueError):
            pass

    def send(self, message: dict[str, Any]) -> None:
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        stdin = self.proc.stdin
        stdin.write(struct.pack(">I", len(body)))
        stdin.write(body)
        stdin.flush()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass

    def reap(self, timeout: float = _REAP_TIMEOUT_S) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()
            try:
                return self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return None

    def retire(self) -> None:
        """Ask the worker to exit (EOF on its stdin), killing it if it lingers."""
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=_RETIRE_GRACE_S)
        except subprocess.TimeoutExpired:
            self.kill()

    def dispose(self) -> None:
        """Kill, reap, close the pipes and wipe the temp dir. Idempotent."""
        self.kill()
        self.reap()
        for thread in (self._reader, self._logger):
            thread.join(timeout=0.5)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        _wipe(self.tmp_dir)


class ExtractorPool:
    """Up to ``size`` long-lived extractor processes, shared by threads.

    Workers start on demand, are recycled after ``max_files_per_worker``
    files (parsers leak), and are replaced after a timeout or crash.
    """

    def __init__(self, size: int = 2, *, tmp_root: str, mem_limit_mb: int = 1536,
                 max_files_per_worker: int = 200) -> None:
        self._size = max(1, int(size))
        self._tmp_root = os.path.abspath(tmp_root)
        self._mem_limit_mb = int(mem_limit_mb)
        self._max_files = max(1, int(max_files_per_worker))
        self._cond = threading.Condition()
        self._workers: list[_Worker | None] = [None] * self._size
        self._busy = [False] * self._size
        self._closed = False
        os.makedirs(self._tmp_root, exist_ok=True)

    # ── public ─────────────────────────────────────────────────────────────

    @staticmethod
    def timeout_for(kind: str, size_bytes: int, pages: int | None = None) -> float:
        """Seconds one file may take: 90 s plus 0.5 s per MB, or for a PDF
        with a known page count 0.3 s per page when that is longer (a scanned
        PDF is few pages but many MB, a text PDF the reverse), capped at 600 s."""
        extra = 0.5 * max(size_bytes, 0) / (1024 * 1024)
        if kind == KIND_PDF and pages:
            extra = max(extra, 0.3 * pages)
        return min(90.0 + extra, 600.0)

    def extract(self, req: ExtractRequest, timeout_s: float | None = None) -> ExtractResult:
        """Extract one file in a worker process. Thread-safe; blocks the
        calling thread until the reply, the deadline or :meth:`close`."""
        if timeout_s is None:
            timeout_s = self.timeout_for(req.kind, _request_size(req))
        slot = self._acquire()
        if slot is None:
            return _result(EXTRACT_ERROR, req, REASON_CLOSED)
        try:
            try:
                worker = self._ensure_worker(slot)
            except _SpawnError as exc:
                logger.warning(f"[documents:extract] cannot start an extractor worker: {exc}")
                result = _result(EXTRACT_ERROR, req, REASON_UNAVAILABLE)
                result.doc_meta["error"] = str(exc)[:300]
                return result
            if worker is None:
                return _result(EXTRACT_ERROR, req, REASON_CLOSED)
            return self._roundtrip(slot, worker, req, timeout_s)
        finally:
            self._release(slot)

    def close(self) -> None:
        """Stop every worker and wipe their temp dirs. Idempotent; an
        ``extract`` in flight on another thread returns ``error(pool_closed)``."""
        with self._cond:
            if self._closed:
                return
            self._closed = True
            workers = [w for w in self._workers if w is not None]
            self._workers = [None] * self._size
            self._cond.notify_all()
        for worker in workers:
            worker.replies.put(_CLOSED)  # wake a waiting caller before the EOF arrives
            worker.kill()
        for worker in workers:
            worker.dispose()

    def __enter__(self) -> "ExtractorPool":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── slots ──────────────────────────────────────────────────────────────

    def _acquire(self) -> int | None:
        with self._cond:
            while not self._closed:
                idle = [i for i in range(self._size) if not self._busy[i]]
                if idle:
                    # A running worker first; a new process only when none is idle.
                    live = [i for i in idle if self._workers[i] is not None]
                    slot = (live or idle)[0]
                    self._busy[slot] = True
                    return slot
                self._cond.wait(timeout=1.0)
            return None

    def _release(self, slot: int) -> None:
        with self._cond:
            self._busy[slot] = False
            self._cond.notify()

    def _ensure_worker(self, slot: int) -> _Worker | None:
        with self._cond:
            worker = self._workers[slot]
        if worker is not None and worker.alive():
            return worker
        if worker is not None:
            self._drop(slot, worker)
        worker = self._spawn(slot)
        with self._cond:
            closed = self._closed
            if not closed:
                self._workers[slot] = worker
        if closed:
            worker.dispose()
            return None
        return worker

    def _drop(self, slot: int, worker: _Worker, *, graceful: bool = False) -> None:
        with self._cond:
            if self._workers[slot] is worker:
                self._workers[slot] = None
        if graceful:
            worker.retire()
        worker.dispose()

    # ── process ────────────────────────────────────────────────────────────

    def _spawn(self, slot: int) -> _Worker:
        tmp_dir = os.path.join(self._tmp_root, f"{_TMP_PREFIX}{slot + 1}")
        _wipe(tmp_dir)
        os.makedirs(tmp_dir, exist_ok=True)
        env = dict(os.environ)
        env.update({
            "TMP": tmp_dir, "TEMP": tmp_dir, "TMPDIR": tmp_dir,
            "CREMIND_EXTRACT_MEM_MB": str(self._mem_limit_mb),
            "DISABLE_LOG": "true",
            "PYTHONDONTWRITEBYTECODE": "1",
            # stderr is forwarded to the log; keep Vietnamese text intact on Windows.
            "PYTHONIOENCODING": "utf-8",
            # A background parser gets one core's worth of BLAS/OpenMP threads.
            "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
        })
        env["PYTHONPATH"] = os.pathsep.join(p for p in (_CODE_ROOT, env.get("PYTHONPATH")) if p)
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True  # a terminal's Ctrl+C is the server's to handle
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", _WORKER_MODULE],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=tmp_dir, env=env, close_fds=True, **kwargs,
            )
        except OSError as exc:
            _wipe(tmp_dir)
            raise _SpawnError(f"{type(exc).__name__}: {exc}") from exc
        worker = _Worker(slot + 1, proc, tmp_dir)
        reply = self._call(worker, {"op": "ping"}, _STARTUP_TIMEOUT_S)
        if not (isinstance(reply, dict) and reply.get("op") == "pong"):
            code = worker.reap(1.0) if reply is _EOF else None
            worker.dispose()
            raise _SpawnError(f"worker did not answer ping (exit code {code})")
        worker.pid = reply.get("pid")
        logger.debug(f"[documents:extract] worker {slot + 1} started (pid {worker.pid})")
        return worker

    def _call(self, worker: _Worker, message: dict[str, Any], timeout_s: float) -> Any:
        """Send one request and wait for its reply, ``_EOF``, ``_CLOSED`` or
        ``_TIMEOUT``. The timer kills the child at the deadline, which also
        unblocks a write stuck on a full pipe."""
        deadline = time.monotonic() + timeout_s

        def expire() -> None:
            worker.expired = True
            worker.kill()

        timer = threading.Timer(timeout_s, expire)
        timer.daemon = True
        timer.start()
        try:
            try:
                worker.send(message)
            except (OSError, ValueError):
                pass  # the child is gone; its reader reports EOF
            try:
                reply = worker.replies.get(timeout=max(deadline - time.monotonic(), 0.0))
            except queue.Empty:
                reply = _TIMEOUT
        finally:
            timer.cancel()
        if reply is _EOF and worker.expired:
            return _TIMEOUT
        return reply

    def _roundtrip(self, slot: int, worker: _Worker, req: ExtractRequest, timeout_s: float) -> ExtractResult:
        message = {"op": "extract", "req": _wire(req), "deadline_s": timeout_s}
        reply = self._call(worker, message, timeout_s)
        if reply is _CLOSED or (self._closed and not isinstance(reply, dict)):
            self._drop(slot, worker)
            return _result(EXTRACT_ERROR, req, REASON_CLOSED)
        if reply is _TIMEOUT:
            self._drop(slot, worker)
            logger.warning(f"[documents:extract] '{req.name}' ({req.kind}) timed out after {timeout_s:.0f}s")
            return _result(EXTRACT_METADATA_ONLY, req, REASON_TIMEOUT)
        if reply is _EOF:
            code = worker.reap()
            self._drop(slot, worker)
            reason = REASON_OOM if code in (_EXIT_OOM, _SIGKILL) else REASON_CRASH
            logger.warning(f"[documents:extract] worker died on '{req.name}' ({req.kind}): exit {code} -> {reason}")
            result = _result(EXTRACT_METADATA_ONLY, req, reason)
            if code is not None:
                result.doc_meta["exit_code"] = code
            return result
        if not reply.get("ok") or not isinstance(reply.get("result"), dict):
            result = _result(EXTRACT_ERROR, req, REASON_PROTOCOL)
            result.doc_meta["error"] = str(reply.get("error"))[:300]
            return result
        worker.files += 1
        if worker.files >= self._max_files:
            self._drop(slot, worker, graceful=True)
        return ExtractResult.from_dict(reply["result"])


def _wire(req: ExtractRequest) -> dict[str, Any]:
    return {
        "name": req.name,
        "kind": req.kind,
        # The child's cwd is its temp dir: a relative path would not resolve.
        "path": os.path.abspath(req.path) if req.path else None,
        "data_b64": base64.b64encode(req.data).decode("ascii") if req.data is not None else None,
        "limits": dict(req.limits or {}),
    }


def _request_size(req: ExtractRequest) -> int:
    if req.data is not None:
        return len(req.data)
    try:
        return os.path.getsize(req.path) if req.path else 0
    except OSError:
        return 0


def _result(status: str, req: ExtractRequest, reason: str) -> ExtractResult:
    return ExtractResult(status=status, kind=req.kind, reason=reason)
