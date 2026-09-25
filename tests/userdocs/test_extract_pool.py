"""The extractor pool, end to end through real worker processes.

These spawn ``python -m app.userdocs.extract.worker`` for real (on Windows
through the venv launcher stub), so they prove the protocol, the deadline,
the kill-and-respawn paths and the "nothing is written" rule on this OS.
Kill paths are driven through the public ``extract`` with
``limits["test_op"]``, which the worker honours only when
``CREMIND_EXTRACT_TEST_OPS=1``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from email.message import EmailMessage

import pytest

from app.userdocs.extract.pool import ExtractorPool, wipe_stale_tmp
from app.userdocs.types import (
    EXTRACT_ERROR,
    EXTRACT_METADATA_ONLY,
    EXTRACT_OK,
    ExtractRequest,
)

from .extract_fixtures import make_cfb, make_docx, make_pdf, make_word_streams, zip_bytes


@pytest.fixture
def test_ops(monkeypatch):
    monkeypatch.setenv("CREMIND_EXTRACT_TEST_OPS", "1")


@pytest.fixture
def pool(tmp_path, test_ops):
    p = ExtractorPool(2, tmp_root=str(tmp_path / "extract-tmp"), mem_limit_mb=512)
    yield p
    p.close()


def _text_file(tmp_path, name: str = "a.txt", body: str = "hello there\n\nsecond para\n") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return str(path)


def _process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.1)
    return False


def _sleeping(path: str, seconds: float) -> ExtractRequest:
    return ExtractRequest(name="a.txt", kind="text", path=path,
                          limits={"test_op": {"op": "sleep", "seconds": seconds}})


# ── Happy paths ────────────────────────────────────────────────────────────


def test_text_and_pdf_through_a_real_worker(pool, tmp_path) -> None:
    r = pool.extract(ExtractRequest(name="a.txt", kind="text", path=_text_file(tmp_path)))
    assert r.status == EXTRACT_OK
    assert [b.text for b in r.blocks] == ["hello there", "second para"]
    assert r.blocks[1].locator == {"line_start": 3, "line_end": 3}

    pdf = make_pdf([[(22, 72, 720, "Heading"), (11, 72, 690, "Body text")], []], scanned=frozenset({2}))
    r = pool.extract(ExtractRequest(name="s.pdf", kind="pdf", data=pdf))
    assert r.status == EXTRACT_OK
    assert [b.text for b in r.blocks] == ["Heading", "Body text"]
    assert r.ocr_pages and r.ocr_pages[0]["page"] == 2  # base64 PNG survived the pipe
    worker = pool._workers[0]
    assert worker is not None and worker.files == 2
    assert isinstance(worker.pid, int)  # the real interpreter's pid, from the handshake


def test_relative_path_is_resolved_by_the_parent(pool, tmp_path, monkeypatch) -> None:
    path = _text_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    r = pool.extract(ExtractRequest(name="a.txt", kind="text", path="a.txt"))
    assert r.status == EXTRACT_OK and r.blocks[0].text == "hello there"
    assert os.path.exists(path)


def test_stray_stdout_does_not_corrupt_the_protocol(pool, tmp_path) -> None:
    r = pool.extract(ExtractRequest(name="a.txt", kind="text", path=_text_file(tmp_path),
                                    limits={"test_op": {"op": "noise"}}))
    assert r.status == EXTRACT_OK and r.blocks[0].text == "hello there"


def test_test_ops_are_ignored_without_the_env_var(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CREMIND_EXTRACT_TEST_OPS", raising=False)
    with ExtractorPool(1, tmp_root=str(tmp_path / "t")) as p:
        r = p.extract(ExtractRequest(name="a.txt", kind="text", path=_text_file(tmp_path),
                                     limits={"test_op": {"op": "crash", "code": 3}}), timeout_s=30)
    assert r.status == EXTRACT_OK


# ── Kill paths ─────────────────────────────────────────────────────────────


def test_timeout_kills_and_the_next_file_respawns(pool, tmp_path) -> None:
    path = _text_file(tmp_path)
    first = pool.extract(ExtractRequest(name="a.txt", kind="text", path=path))
    assert first.status == EXTRACT_OK
    old_pid = pool._workers[0].pid

    started = time.monotonic()
    r = pool.extract(_sleeping(path, 60), timeout_s=2)
    assert time.monotonic() - started < 15
    assert (r.status, r.reason, r.kind) == (EXTRACT_METADATA_ONLY, "timeout", "text")
    assert pool._workers[0] is None
    # The real interpreter is gone too, not just the Windows launcher stub.
    assert _wait_dead(old_pid)

    again = pool.extract(ExtractRequest(name="a.txt", kind="text", path=path))
    assert again.status == EXTRACT_OK
    assert pool._workers[0].pid != old_pid


def test_crash_is_reported_and_replaced(pool, tmp_path) -> None:
    path = _text_file(tmp_path)
    r = pool.extract(ExtractRequest(name="a.txt", kind="text", path=path,
                                    limits={"test_op": {"op": "crash", "code": 3}}))
    assert (r.status, r.reason) == (EXTRACT_METADATA_ONLY, "extractor_crash")
    assert r.doc_meta["exit_code"] == 3  # passed through the launcher on Windows
    assert pool.extract(ExtractRequest(name="a.txt", kind="text", path=path)).status == EXTRACT_OK


def test_exit_86_is_oom(pool, tmp_path) -> None:
    r = pool.extract(ExtractRequest(name="a.txt", kind="text", path=_text_file(tmp_path),
                                    limits={"test_op": {"op": "crash", "code": 86}}))
    assert (r.status, r.reason) == (EXTRACT_METADATA_ONLY, "oom")


def test_memory_watchdog_kills_a_bloated_worker(tmp_path, test_ops) -> None:
    with ExtractorPool(1, tmp_root=str(tmp_path / "t"), mem_limit_mb=200) as p:
        r = p.extract(ExtractRequest(name="a.txt", kind="text", path=_text_file(tmp_path),
                                     limits={"test_op": {"op": "alloc", "mb": 400, "seconds": 20}}),
                      timeout_s=60)
    assert (r.status, r.reason) == (EXTRACT_METADATA_ONLY, "oom")
    assert r.doc_meta["exit_code"] == 86


# ── Concurrency, recycling, shutdown ───────────────────────────────────────


def test_two_requests_run_in_two_workers(pool, tmp_path) -> None:
    path = _text_file(tmp_path)
    results = []
    slow = threading.Thread(target=lambda: results.append(pool.extract(_sleeping(path, 3), timeout_s=30)))
    slow.start()
    deadline = time.monotonic() + 20
    while not (pool._busy[0] and pool._workers[0] is not None) and time.monotonic() < deadline:
        time.sleep(0.05)
    fast = pool.extract(ExtractRequest(name="a.txt", kind="text", path=path))
    assert fast.status == EXTRACT_OK
    assert pool._workers[1] is not None  # the busy first worker forced a second one
    slow.join(30)
    assert results and results[0].status == EXTRACT_OK


def test_worker_is_recycled_after_max_files(tmp_path, test_ops) -> None:
    path = _text_file(tmp_path)
    with ExtractorPool(1, tmp_root=str(tmp_path / "t"), max_files_per_worker=2) as p:
        p.extract(ExtractRequest(name="a.txt", kind="text", path=path))
        first_pid = p._workers[0].pid
        p.extract(ExtractRequest(name="a.txt", kind="text", path=path))
        assert p._workers[0] is None  # retired after its second file
        assert _wait_dead(first_pid)
        p.extract(ExtractRequest(name="a.txt", kind="text", path=path))
        assert p._workers[0].pid != first_pid


def test_close_while_in_flight_returns_an_error(tmp_path, test_ops) -> None:
    p = ExtractorPool(1, tmp_root=str(tmp_path / "t"))
    results = []
    thread = threading.Thread(target=lambda: results.append(p.extract(_sleeping(_text_file(tmp_path), 60),
                                                                      timeout_s=120)))
    thread.start()
    deadline = time.monotonic() + 20
    while not (p._busy[0] and p._workers[0] is not None) and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    started = time.monotonic()
    p.close()
    thread.join(15)
    assert not thread.is_alive()
    assert time.monotonic() - started < 15
    assert (results[0].status, results[0].reason) == (EXTRACT_ERROR, "pool_closed")
    p.close()  # idempotent
    after = p.extract(ExtractRequest(name="a.txt", kind="text", path=_text_file(tmp_path)))
    assert (after.status, after.reason) == (EXTRACT_ERROR, "pool_closed")
    assert not [e for e in os.listdir(tmp_path / "t") if e.startswith("worker-")]


def test_unstartable_worker_is_reported(tmp_path, monkeypatch) -> None:
    import app.userdocs.extract.pool as pool_mod

    monkeypatch.setattr(pool_mod, "_WORKER_MODULE", "app.userdocs.extract.does_not_exist")
    with ExtractorPool(1, tmp_root=str(tmp_path / "t")) as p:
        r = p.extract(ExtractRequest(name="a.txt", kind="text", path=_text_file(tmp_path)))
    assert (r.status, r.reason) == (EXTRACT_ERROR, "extractor_unavailable")


# ── Nothing is written ─────────────────────────────────────────────────────


def _snapshot(root: str) -> dict[str, tuple[int, int, str]]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.stat(full)
            digest = ""
            if os.path.isfile(full):
                with open(full, "rb") as fh:
                    digest = hashlib.sha256(fh.read()).hexdigest()
            out[os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns, digest)
    return out


def test_extraction_writes_nothing(tmp_path, test_ops) -> None:
    import openpyxl
    from PIL import Image
    from pptx import Presentation

    docs = tmp_path / "Documents"
    docs.mkdir()
    (docs / "notes.md").write_text("# Ghi chú\n\nNội dung.\n", encoding="utf-8")
    (docs / "data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (docs / "report.pdf").write_bytes(make_pdf([[(20, 72, 700, "Report")], []], scanned=frozenset({2})))
    (docs / "letter.docx").write_bytes(make_docx([("Heading1", "Letter"), (None, "Body")]))
    (docs / "legacy.doc").write_bytes(make_cfb(make_word_streams([("Old Word text\r", True)])))
    (docs / "memo.rtf").write_bytes(rb"{\rtf1\ansi Memo text\par}")
    (docs / "bundle.zip").write_bytes(zip_bytes({"inner.txt": "x"}))
    wb = openpyxl.Workbook()
    wb.active.append(["k", "v"])
    wb.save(docs / "sheet.xlsx")
    Presentation().save(docs / "deck.pptx")
    Image.new("RGB", (64, 64), "red").save(docs / "photo.jpg")
    mail = EmailMessage()
    mail["Subject"] = "hi"
    mail.set_content("body")
    (docs / "mail.eml").write_bytes(mail.as_bytes())
    kinds = {"notes.md": "markdown", "data.csv": "csv", "report.pdf": "pdf", "letter.docx": "docx",
             "legacy.doc": "doc", "memo.rtf": "rtf", "bundle.zip": "archive", "sheet.xlsx": "xlsx",
             "deck.pptx": "pptx", "photo.jpg": "image", "mail.eml": "eml"}

    tmp_root = tmp_path / "extract-tmp"
    before = _snapshot(str(docs))
    with ExtractorPool(1, tmp_root=str(tmp_root)) as p:
        for name, kind in kinds.items():
            r = p.extract(ExtractRequest(name=name, kind=kind, path=str(docs / name)), timeout_s=120)
            assert r.status in (EXTRACT_OK, EXTRACT_METADATA_ONLY), (name, r.status, r.reason, r.doc_meta)
            # The worker's own temp dir stays empty too: no library spilled a file.
            assert os.listdir(p._workers[0].tmp_dir) == [], name
    assert _snapshot(str(docs)) == before
    assert os.listdir(tmp_root) == []  # close() wiped the worker dirs


# ── Helpers ────────────────────────────────────────────────────────────────


def test_timeout_for() -> None:
    mb = 1024 * 1024
    assert ExtractorPool.timeout_for("text", 0) == 90
    assert ExtractorPool.timeout_for("docx", 100 * mb) == 140
    assert ExtractorPool.timeout_for("pdf", 10 * mb, pages=1000) == 390  # pages dominate
    assert ExtractorPool.timeout_for("pdf", 100 * mb, pages=10) == 140   # a scan: size dominates
    assert ExtractorPool.timeout_for("pdf", 10 * mb, pages=5000) == 600  # capped
    assert ExtractorPool.timeout_for("text", 5000 * mb) == 600


def test_wipe_stale_tmp_only_touches_worker_dirs(tmp_path) -> None:
    root = tmp_path / "tmp"
    (root / "worker-3" / "nested").mkdir(parents=True)
    (root / "worker-3" / "nested" / "junk.bin").write_bytes(b"x")
    (root / "keep-me").mkdir()
    (root / "worker-notes.txt").write_text("a file, not a worker dir")
    assert wipe_stale_tmp(str(root)) == 1
    assert sorted(os.listdir(root)) == ["keep-me", "worker-notes.txt"]
    assert wipe_stale_tmp(str(tmp_path / "fresh")) == 0
    assert (tmp_path / "fresh").is_dir()
