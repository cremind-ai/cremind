"""The storage governor: estimates, capacity sources, levels and hysteresis.

The governor is what keeps a Docker volume from filling up (Compose volumes
share one filesystem with no quota; a Helm PVC is fixed). Everything here runs
on made-up capacities, so every threshold and transition is exact.
"""

from __future__ import annotations

import errno
import sqlite3
import threading
from collections import namedtuple

import pytest

from app.lib.exception import VectorStoreException
from app.constants.status import Status
from app.userdocs import governor as g
from app.userdocs.governor import (
    GB,
    KB,
    MB,
    Capacity,
    Governor,
    Level,
    Usage,
    classify_edit,
    estimate_bytes,
    estimate_chunks,
    is_enospc,
    measure_capacity,
    parse_k8s_quantity,
)


def _disk(free_gb: float, total_gb: float = 100) -> Capacity:
    return Capacity(
        fs_free=int(free_gb * GB), fs_total=int(total_gb * GB),
        vector_capacity=None, db_capacity=None, method="statvfs",
    )


UNKNOWN = Capacity(fs_free=None, fs_total=None, vector_capacity=None, db_capacity=None, method="unknown")


def _usage(global_gb: float = 0, profile_gb: float | None = None) -> Usage:
    total = int((profile_gb if profile_gb is not None else global_gb) * GB)
    return Usage(index_bytes=total // 3, vector_bytes_est=total - total // 3,
                 profile_total=total, global_total=int(global_gb * GB))


# ── estimate_chunks ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(("kind", "size", "extra", "expected"), [
    ("text", 14_000, {}, 10),
    ("markdown", 14_001, {}, 11),
    ("code", 1, {}, 1),
    ("csv", 0, {}, 1),
    ("pdf", 0, {"pages": 10}, 16),
    ("pdf", 0, {"pages": 1}, 2),
    ("pdf", 600 * KB, {}, 16),            # no page count: ~60 KB a page
    ("docx", 60 * KB, {}, 10),
    ("doc", 61 * KB, {}, 11),
    ("xlsx", 0, {"cells": 1500}, 10),
    ("xlsx", 15_000, {}, 10),             # no cell count: size ÷ 1500
    ("pptx", 0, {"slides": 12}, 13),
    ("pptx", 1024 * KB, {}, 7),           # ~200 KB a slide, plus 1
    ("image", 50 * MB, {}, 2),
    ("video", 5 * GB, {}, 1),
    ("archive", 10 * MB, {}, 1),
    ("encrypted", 10 * MB, {}, 1),
    ("other", 10 * MB, {}, 1),
    ("never-heard-of-it", 2800, {}, 2),   # read as text
])
def test_estimate_chunks(kind, size, extra, expected):
    assert estimate_chunks(kind, size, **extra) == expected


# ── estimate_bytes ──────────────────────────────────────────────────────────


def test_estimate_bytes_qdrant():
    est = estimate_bytes(1000, backend="qdrant")
    assert est["db"] == 1000 * (1.45 * 1600 + 380)
    assert est["vectors"] == 1000 * 5.5 * KB + 8 * MB  # points + one WAL
    assert est["total"] == est["db"] + est["vectors"]


def test_estimate_bytes_chroma():
    est = estimate_bytes(1000, backend="chroma")
    assert est["vectors"] == pytest.approx(1000 * 8.7 * KB, abs=1)
    assert est["total"] == est["db"] + est["vectors"]


def test_about_8_kb_per_chunk_overall():
    per_chunk = estimate_bytes(1_000_000, backend="qdrant")["total"] / 1_000_000
    assert 7 * KB < per_chunk < 9 * KB


def test_estimate_bytes_scales_with_dimension_and_text():
    full = estimate_bytes(1000, backend="chroma", dim=768)["vectors"]
    half = estimate_bytes(1000, backend="chroma", dim=384)["vectors"]
    assert half == pytest.approx(full / 2, abs=1)
    assert estimate_bytes(10, backend="qdrant", avg_text_bytes=0)["db"] == 10 * 380


def test_estimate_bytes_of_nothing_is_zero_and_bad_backend_raises():
    assert estimate_bytes(0, backend="qdrant") == {"db": 0, "vectors": 0, "total": 0}
    with pytest.raises(ValueError):
        estimate_bytes(1, backend="milvus")


# ── parse_k8s_quantity ──────────────────────────────────────────────────────


@pytest.mark.parametrize(("text", "expected"), [
    ("10Gi", 10 * 1024 ** 3),
    ("500Mi", 500 * 1024 ** 2),
    ("1G", 10 ** 9),
    ("1.5Ti", int(1.5 * 1024 ** 4)),
    ("1073741824", 1024 ** 3),
    ("  2Gi ", 2 * 1024 ** 3),
    ("100k", 100_000),
    ("64Ki", 65_536),
    ("1e9", 10 ** 9),
    ("2E3", 2000),
    (".5Gi", 512 * 1024 ** 2),
])
def test_parse_k8s_quantity(text, expected):
    assert parse_k8s_quantity(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "abc", "Gi", "10GB", "10gi", "-1Gi", "0", "0Gi", "1e3Gi", "10 G i", None,
])
def test_parse_k8s_quantity_rejects(text):
    assert parse_k8s_quantity(text) is None


# ── measure_capacity ────────────────────────────────────────────────────────


def test_capacity_from_admin_setting_wins_over_env(tmp_path):
    cap = measure_capacity(
        str(tmp_path), vector_capacity_mb=2048,
        env={"CREMIND_VECTORSTORE_CAPACITY": "10Gi", "CREMIND_DB_CAPACITY": "5Gi"},
    )
    assert cap.vector_capacity == 2048 * MB
    assert cap.db_capacity == 5 * 1024 ** 3   # no admin value: env fills in
    assert cap.method == "admin"
    assert cap.fs_total and cap.fs_free is not None  # the fs is measured too


def test_capacity_from_env(tmp_path):
    cap = measure_capacity(str(tmp_path), env={"CREMIND_VECTORSTORE_CAPACITY": "10Gi"})
    assert cap.vector_capacity == 10 * 1024 ** 3
    assert cap.db_capacity is None
    assert cap.method == "env"


def test_capacity_from_statvfs_ignores_bad_env(tmp_path):
    cap = measure_capacity(str(tmp_path), env={"CREMIND_VECTORSTORE_CAPACITY": "lots"})
    assert cap.vector_capacity is None
    assert cap.method == "statvfs"
    assert 0 <= cap.fs_free <= cap.fs_total


def test_capacity_of_a_dir_that_does_not_exist_yet_measures_its_parent(tmp_path):
    cap = measure_capacity(str(tmp_path / "storage" / "userdocs"), env={})
    assert cap.method == "statvfs"
    assert cap.fs_total


def test_capacity_unknown_when_nothing_answers(tmp_path, monkeypatch):
    def _fail(path):
        raise OSError("not supported")

    monkeypatch.setattr(g.shutil, "disk_usage", _fail)
    cap = measure_capacity(str(tmp_path), env={})
    assert cap == Capacity(None, None, None, None, "unknown")


def test_capacity_uses_disk_usage_of_the_system_dir(tmp_path, monkeypatch):
    DU = namedtuple("DU", "total used free")
    seen = []

    def _du(path):
        seen.append(path)
        return DU(100 * GB, 60 * GB, 40 * GB)

    monkeypatch.setattr(g.shutil, "disk_usage", _du)
    cap = measure_capacity(str(tmp_path), env={})
    assert (cap.fs_free, cap.fs_total) == (40 * GB, 100 * GB)
    assert seen == [str(tmp_path)]


# ── disk levels and hysteresis ──────────────────────────────────────────────


def test_disk_levels_rise_and_fall_with_hysteresis():
    # 100 GB volume: critical < 2 GB, low < 5 GB, warn < 10 GB; resume +1 GB.
    gov = Governor(global_budget_bytes=0)
    steps = [
        (50, Level.OK),
        (9, Level.WARN),
        (4, Level.DISK_LOW),
        (1.5, Level.DISK_CRITICAL),
        (2.5, Level.DISK_CRITICAL),   # above the trigger, inside the headroom
        (3.5, Level.DISK_LOW),        # critical lifts; still below the low trigger
        (5.5, Level.DISK_LOW),        # held: < 5 + 1 GB
        (6.5, Level.WARN),
        (10.5, Level.WARN),           # held: < 10 + 1 GB
        (11.5, Level.OK),
        (10.5, Level.OK),             # not re-entered from below the trigger
    ]
    for free, expected in steps:
        assert gov.evaluate(_usage(), _disk(free)) is expected, f"free={free} GB"


def test_small_disks_use_the_absolute_floors():
    # 20 GB: 5 % is 1 GB < 3 GB floor; 2 % is 0.4 GB < 1 GB floor.
    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(6.5, 20)) is Level.OK
    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(5.5, 20)) is Level.WARN
    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(2.9, 20)) is Level.DISK_LOW
    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(0.9, 20)) is Level.DISK_CRITICAL


def test_big_disks_use_the_percentages():
    # 1 TB: low < 51.2 GB (5 %), critical < 20.5 GB (2 %).
    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(50, 1024)) is Level.DISK_LOW
    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(20, 1024)) is Level.DISK_CRITICAL


def test_enospc_is_critical_until_space_is_confirmed():
    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(50), store_error_enospc=True) is Level.DISK_CRITICAL
    # Plenty measured: lifts on the next evaluation (the next write probes).
    assert gov.evaluate(_usage(), _disk(50)) is Level.OK

    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), _disk(2.5), store_error_enospc=True) is Level.DISK_CRITICAL
    assert gov.evaluate(_usage(), _disk(2.5)) is Level.DISK_CRITICAL  # inside the headroom

    gov = Governor(global_budget_bytes=0)
    assert gov.evaluate(_usage(), UNKNOWN, store_error_enospc=True) is Level.DISK_CRITICAL
    assert gov.evaluate(_usage(), UNKNOWN) is Level.OK  # nothing to hold on to


def test_unknown_capacity_leaves_only_the_budgets():
    gov = Governor(global_budget_bytes=10 * GB)
    assert gov.evaluate(_usage(1), UNKNOWN) is Level.OK
    assert gov.evaluate(_usage(10), UNKNOWN) is Level.BUDGET


def test_explicit_capacity_counts_the_index_against_it():
    # Helm PVC of 10 GiB, nothing measurable on the system fs.
    cap = Capacity(fs_free=None, fs_total=None, vector_capacity=10 * GB, db_capacity=None, method="env")
    assert Governor(global_budget_bytes=0).evaluate(_usage(1), cap) is Level.OK
    assert Governor(global_budget_bytes=0).evaluate(_usage(5), cap) is Level.WARN      # 5 GB free < 6
    assert Governor(global_budget_bytes=0).evaluate(_usage(8), cap) is Level.DISK_LOW  # 2 GB free < 3
    assert Governor(global_budget_bytes=0).evaluate(_usage(9.5), cap) is Level.DISK_CRITICAL


def test_capacities_add_up_and_the_worse_volume_wins():
    both = Capacity(fs_free=None, fs_total=None, vector_capacity=6 * GB, db_capacity=4 * GB, method="admin")
    assert Governor(global_budget_bytes=0).evaluate(_usage(1), both) is Level.OK  # 9 of 10 GB free
    mixed = Capacity(fs_free=500 * GB, fs_total=1000 * GB, vector_capacity=10 * GB, db_capacity=None, method="admin")
    assert Governor(global_budget_bytes=0).evaluate(_usage(8), mixed) is Level.DISK_LOW
    tight_fs = Capacity(fs_free=int(1.5 * GB), fs_total=100 * GB, vector_capacity=100 * GB, db_capacity=None, method="admin")
    assert Governor(global_budget_bytes=0).evaluate(_usage(1), tight_fs) is Level.DISK_CRITICAL


# ── budget levels and hysteresis ────────────────────────────────────────────


def test_global_budget_levels_with_hysteresis():
    gov = Governor(global_budget_bytes=10 * GB)
    plenty = _disk(500, 1000)
    steps = [
        (8.0, Level.OK),
        (8.6, Level.WARN),      # ≥ 85 %
        (10.0, Level.BUDGET),   # ≥ 100 %
        (9.7, Level.BUDGET),    # held until < 95 %
        (9.4, Level.WARN),
        (8.3, Level.WARN),      # warn held until < 80 %
        (7.9, Level.OK),
        (9.7, Level.WARN),      # from OK, 97 % is only a warning
    ]
    for used, expected in steps:
        assert gov.evaluate(_usage(used), plenty) is expected, f"used={used} GB"


def test_per_profile_budget():
    gov = Governor(global_budget_bytes=100 * GB, profile_budget_bytes=1 * GB)
    plenty = _disk(500, 1000)
    assert gov.evaluate(_usage(2, profile_gb=0.5), plenty, profile="a") is Level.OK
    assert gov.evaluate(_usage(2, profile_gb=1.0), plenty, profile="a") is Level.BUDGET
    assert gov.evaluate(_usage(2, profile_gb=0.5), plenty, profile="b") is Level.OK


def test_zero_budgets_are_disabled():
    gov = Governor(global_budget_bytes=0, profile_budget_bytes=0)
    assert gov.evaluate(_usage(10_000), _disk(500, 1000)) is Level.OK


def test_hysteresis_is_kept_per_profile():
    gov = Governor(global_budget_bytes=100 * GB, profile_budget_bytes=10 * GB)
    plenty = _disk(500, 1000)
    assert gov.evaluate(_usage(20, profile_gb=10), plenty, profile="alice") is Level.BUDGET
    # Same numbers, but bob never reached the budget: no hold for him.
    assert gov.evaluate(_usage(20, profile_gb=9.7), plenty, profile="bob") is Level.WARN
    assert gov.evaluate(_usage(20, profile_gb=9.7), plenty, profile="alice") is Level.BUDGET
    assert gov.last_level("alice") is Level.BUDGET
    assert gov.last_level("bob") is Level.WARN
    gov.forget("alice")
    assert gov.last_level("alice") is Level.OK
    assert gov.evaluate(_usage(20, profile_gb=9.7), plenty, profile="alice") is Level.WARN


def test_disk_and_budget_recover_independently():
    gov = Governor(global_budget_bytes=10 * GB)
    # Disk low while the budget is only at 97 %: the disk level shows.
    assert gov.evaluate(_usage(9.7), _disk(4)) is Level.DISK_LOW
    # Disk recovers; the budget was never at 100 %, so no BUDGET level appears.
    assert gov.evaluate(_usage(9.7), _disk(500, 1000)) is Level.WARN
    # Budget reached while the disk is low: disk wins, then budget remains.
    gov = Governor(global_budget_bytes=10 * GB)
    assert gov.evaluate(_usage(10), _disk(4)) is Level.DISK_LOW
    assert gov.evaluate(_usage(9.7), _disk(500, 1000)) is Level.BUDGET


def test_evaluate_is_thread_safe():
    gov = Governor(global_budget_bytes=10 * GB)
    errors: list = []

    def run(i):
        try:
            for _ in range(200):
                gov.evaluate(_usage(i % 12), _disk(3 + i % 5), profile=f"p{i}")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert errors == []


# ── allows / describe ───────────────────────────────────────────────────────

_TABLE = {
    #                OK    WARN  BUDGET LOW   CRIT
    "add_content":  (True, True, False, False, False),
    "grow_edit":    (True, True, False, False, False),
    "caption":      (True, True, False, False, False),
    "reembed_dual": (True, True, False, False, False),
    "shrink_edit":  (True, True, True, False, False),
    "move":         (True, True, True, True, False),
    "insert":       (True, True, True, True, False),
    "upsert":       (True, True, True, True, False),
    "delete":       (True, True, True, True, True),
    "purge":        (True, True, True, True, True),
    "gc":           (True, True, True, True, True),
    "search":       (True, True, True, True, True),
}
_LEVELS = (Level.OK, Level.WARN, Level.BUDGET, Level.DISK_LOW, Level.DISK_CRITICAL)


def test_allows_table():
    assert set(_TABLE) == set(g.OPS)
    for op, row in _TABLE.items():
        for level, expected in zip(_LEVELS, row):
            assert Governor.allows(level, op) is expected, (level, op)


def test_search_and_freeing_space_are_never_paused():
    for level in _LEVELS:
        for op in ("search", "delete", "purge", "gc"):
            assert Governor.allows(level, op)


def test_allows_accepts_level_strings_and_rejects_unknown_ops():
    assert Governor.allows("budget", "delete") is True
    with pytest.raises(ValueError):
        Governor.allows(Level.OK, "defragment")


def test_describe_has_a_sentence_per_level():
    texts = [Governor.describe(level) for level in _LEVELS]
    assert all(isinstance(s, str) and s.endswith(".") for s in texts)
    assert len(set(texts)) == len(texts)
    assert "search" in Governor.describe(Level.DISK_CRITICAL).lower()


def test_classify_edit():
    assert classify_edit(0) == "shrink_edit"
    assert classify_edit(-5000) == "shrink_edit"
    assert classify_edit(64 * KB) == "shrink_edit"
    assert classify_edit(64 * KB + 1) == "grow_edit"


@pytest.mark.parametrize(("err", "expected"), [
    (OSError(errno.ENOSPC, "No space left on device"), True),
    (VectorStoreException(Status.VECTOR_STORE_ERROR, "Service internal error: No space left on device (os error 28)"), True),
    (sqlite3.OperationalError("database or disk is full"), True),
    ("There is not enough space on the disk", True),
    (RuntimeError("connection refused"), False),
    (OSError(errno.EACCES, "Permission denied"), False),
    (None, False),
])
def test_is_enospc(err, expected):
    assert is_enospc(err) is expected
