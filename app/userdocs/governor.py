"""Storage governor: keeps the document index from filling the disk.

Why it exists. In Docker Compose the named volumes (``cremind-data``,
``qdrant-data``, ``chroma-data``, ``pg-data``) have no quota: they all share the
one Docker data-root filesystem, which on Docker Desktop is the VM's virtual
disk. When it fills, Qdrant and the database refuse writes for *everything*,
chat included. A Helm PVC is a fixed size with the same failure. So the index
must stop growing well before the disk does, while the operations that free
space (deletes, purges, garbage collection) and search keep working.

The module is deliberately pure: callers measure (:func:`measure_capacity`, the
index DB size, the store's point count) and pass the numbers in, so every
threshold and transition is unit-testable with made-up capacities.

Levels, from least to most severe (plan §3):

- ``warn``          ≥ 85 % of a budget, or free space < 2 × the disk-low floor.
                    Nothing is paused; big syncs ask first.
- ``budget``        ≥ 100 % of a budget. New content, growing edits, captions
                    and dual-collection re-embeds wait.
- ``disk_low``      free < max(3 GB, 5 % of the volume). All ingestion waits.
- ``disk_critical`` free < max(1 GB, 2 %), or a store just failed with ENOSPC.
                    Also every insert and upsert waits; only freeing runs.

Levels are sticky (hysteresis) so the engine does not flap on the boundary: a
disk level holds until free space is 1 GB above its trigger, the budget level
until usage falls below 95 % (warn: 80 %).

The budget and the disk are tracked as two separate axes, each with its own
hysteresis, and the reported level is the worse of the two — so recovering
disk space never leaves a stale budget level behind, or vice versa.
"""

from __future__ import annotations

import errno
import math
import os
import re
import shutil
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

from app.userdocs import types as t

KB = 1024
MB = 1024 * KB
GB = 1024 * MB

# ── Estimation (plan §3) ───────────────────────────────────────────────────

# Text-like kinds: size ÷ 1400 bytes per chunk.
_TEXT_KINDS = frozenset({
    t.KIND_TEXT, t.KIND_MARKDOWN, t.KIND_CODE, t.KIND_CSV, t.KIND_JSON,
    t.KIND_XML, t.KIND_HTML, t.KIND_EML, t.KIND_MSG,
})
# Word-processor documents: size ÷ 6 KB (zipped or binary, so fewer chunks
# per byte than plain text).
_DOC_KINDS = frozenset({t.KIND_DOCX, t.KIND_DOC, t.KIND_ODT, t.KIND_RTF, t.KIND_EPUB})
_SHEET_KINDS = frozenset({t.KIND_XLSX, t.KIND_XLS, t.KIND_ODS})
_SLIDE_KINDS = frozenset({t.KIND_PPTX, t.KIND_PPT, t.KIND_ODP})

_TEXT_BYTES_PER_CHUNK = 1400
_DOC_BYTES_PER_CHUNK = 6 * KB
_CHUNKS_PER_PDF_PAGE = 1.6
_CELLS_PER_CHUNK = 150
_IMAGE_CHUNKS = 2  # caption + file card
# Fallbacks when the page / cell / slide count is not known before extraction
# (a pre-sync estimate over thousands of files cannot open each one). Rough
# averages, erring high: an over-estimate asks the user once too often, an
# under-estimate lets a sync run into the budget.
_PDF_BYTES_PER_PAGE = 60 * KB
_SHEET_BYTES_PER_CHUNK = 1500   # ≈ 150 cells × ~10 compressed bytes
_SLIDE_BYTES = 200 * KB


def estimate_chunks(
    kind: str,
    size_bytes: int,
    *,
    pages: int | None = None,
    cells: int | None = None,
    slides: int | None = None,
) -> int:
    """Expected number of chunks for one file, before it is extracted. ≥ 1."""
    size = max(0, int(size_bytes or 0))
    if kind in t.METADATA_ONLY_KINDS:
        return 1
    if kind == t.KIND_IMAGE:
        return _IMAGE_CHUNKS
    if kind == t.KIND_PDF:
        n_pages = pages if pages is not None and pages > 0 else size / _PDF_BYTES_PER_PAGE
        return max(1, math.ceil(n_pages * _CHUNKS_PER_PDF_PAGE))
    if kind in _SHEET_KINDS:
        if cells is not None and cells > 0:
            return max(1, math.ceil(cells / _CELLS_PER_CHUNK))
        return max(1, math.ceil(size / _SHEET_BYTES_PER_CHUNK))
    if kind in _SLIDE_KINDS:
        n_slides = slides if slides is not None and slides > 0 else math.ceil(size / _SLIDE_BYTES)
        return max(1, int(n_slides)) + 1
    if kind in _DOC_KINDS:
        return max(1, math.ceil(size / _DOC_BYTES_PER_CHUNK))
    # Text-like, and anything unknown that still gets its content read.
    return max(1, math.ceil(size / _TEXT_BYTES_PER_CHUNK))


# Per-chunk storage (plan §3), measured at d = 768 and scaled linearly by dim.
_DB_BYTES_PER_TEXT_BYTE = 1.45   # chunk text + folded copy + FTS5 postings
_DB_BYTES_PER_CHUNK = 380        # row, locator JSON, indexes
_QDRANT_BYTES_PER_POINT_768 = 5.5 * KB   # float vector + int8 copy + HNSW + payload
_QDRANT_WAL_BYTES = 8 * MB               # per collection (wal_capacity_mb=8)
_CHROMA_BYTES_PER_POINT_768 = 8.7 * KB   # full-precision HNSW on disk


def estimate_bytes(
    chunks: int,
    *,
    backend: str,
    dim: int = 768,
    avg_text_bytes: int = 1600,
) -> dict[str, int]:
    """Disk bytes ``{db, vectors, total}`` for ``chunks`` chunks.

    ``db`` is the per-profile index DB; ``vectors`` the store (``"qdrant"`` or
    ``"chroma"``). Chroma additionally holds ≈ 3 KB of RAM per point in
    process — a memory cost, not counted here.
    """
    n = max(0, int(chunks))
    db = n * (_DB_BYTES_PER_TEXT_BYTE * max(0, avg_text_bytes) + _DB_BYTES_PER_CHUNK)
    scale = max(1, int(dim)) / 768
    if backend == "qdrant":
        vectors = n * _QDRANT_BYTES_PER_POINT_768 * scale + (_QDRANT_WAL_BYTES if n else 0)
    elif backend == "chroma":
        vectors = n * _CHROMA_BYTES_PER_POINT_768 * scale
    else:
        raise ValueError(f"unknown vector store backend {backend!r}")
    db_i, vec_i = int(math.ceil(db)), int(math.ceil(vectors))
    return {"db": db_i, "vectors": vec_i, "total": db_i + vec_i}


# ── Capacity ───────────────────────────────────────────────────────────────

_QUANTITY = re.compile(r"^\s*([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE]([+-]?[0-9]+))?\s*([A-Za-z]*)\s*$")
_SUFFIX = {
    "": 1,
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    # "K" is not Kubernetes syntax, but a hand-written value may use it.
    "k": 1000, "K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6,
}


def parse_k8s_quantity(s: str) -> int | None:
    """Bytes in a Kubernetes quantity (``10Gi``, ``500Mi``, ``1G``, ``1.5Ti``,
    ``1e9``, plain ``1073741824``). ``None`` for anything unparseable, zero or
    negative — a capacity that cannot be trusted is treated as unknown."""
    m = _QUANTITY.match(s or "")
    if not m:
        return None
    number, exponent, suffix = m.group(1), m.group(2), m.group(3)
    if suffix not in _SUFFIX or (exponent is not None and suffix):
        return None
    try:
        value = float(number) * (10 ** int(exponent) if exponent else 1) * _SUFFIX[suffix]
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(value) or value < 1:
        return None
    return int(value)


@dataclass
class Usage:
    """What the index occupies now, in bytes.

    ``index_bytes`` / ``vector_bytes_est``: this profile's index DB file and
    estimated vector footprint (reported, not judged). ``profile_total``: this
    profile's whole footprint, judged against the per-profile budget.
    ``global_total``: every profile's footprint, judged against the global
    budget and any explicit capacity.
    """

    index_bytes: int
    vector_bytes_est: int
    profile_total: int
    global_total: int


@dataclass
class Capacity:
    """Where the index can grow. ``fs_*`` is the filesystem under the system
    dir (it always holds the index DBs, and in Compose the vector store too);
    ``vector_capacity`` / ``db_capacity`` are explicit volume sizes (admin
    setting or the Helm-set environment), ``None`` when not configured.
    ``method`` names the highest-precedence source that contributed:
    ``admin`` > ``env`` > ``statvfs`` > ``unknown``."""

    fs_free: int | None
    fs_total: int | None
    vector_capacity: int | None
    db_capacity: int | None
    method: str


ENV_VECTOR_CAPACITY = "CREMIND_VECTORSTORE_CAPACITY"
ENV_DB_CAPACITY = "CREMIND_DB_CAPACITY"


def _existing_dir(path: str) -> str | None:
    """``path`` or its nearest existing ancestor (the userdocs storage dir may
    not exist yet on a fresh install)."""
    p = Path(path).expanduser()
    for candidate in (p, *p.parents):
        if candidate.exists():
            return str(candidate)
    return None


def measure_capacity(
    system_dir: str,
    *,
    vector_capacity_mb: int = 0,
    db_capacity_mb: int = 0,
    env: Mapping[str, str] = os.environ,
) -> Capacity:
    """Measure where the index can grow.

    Each explicit capacity comes from the admin setting (MB, 0 = unset) or else
    the environment (a Kubernetes quantity, set by the Helm chart from an
    explicit PVC size). The filesystem is measured as well whenever it can be:
    it holds the index DBs no matter where the vector store lives.
    """
    method: str | None = None
    vector_cap: int | None = None
    db_cap: int | None = None
    if vector_capacity_mb and vector_capacity_mb > 0:
        vector_cap, method = int(vector_capacity_mb) * MB, "admin"
    if db_capacity_mb and db_capacity_mb > 0:
        db_cap, method = int(db_capacity_mb) * MB, "admin"
    if vector_cap is None:
        vector_cap = parse_k8s_quantity(env.get(ENV_VECTOR_CAPACITY) or "")
        if vector_cap is not None:
            method = method or "env"
    if db_cap is None:
        db_cap = parse_k8s_quantity(env.get(ENV_DB_CAPACITY) or "")
        if db_cap is not None:
            method = method or "env"

    fs_free = fs_total = None
    target = _existing_dir(system_dir) if system_dir else None
    if target is not None:
        try:
            du = shutil.disk_usage(target)
            fs_free, fs_total = int(du.free), int(du.total)
            method = method or "statvfs"
        except OSError:
            pass
    return Capacity(
        fs_free=fs_free,
        fs_total=fs_total,
        vector_capacity=vector_cap,
        db_capacity=db_cap,
        method=method or "unknown",
    )


# ── Levels and the governor ────────────────────────────────────────────────


class Level(str, Enum):
    OK = "ok"
    WARN = "warn"
    BUDGET = "budget"
    DISK_LOW = "disk_low"
    DISK_CRITICAL = "disk_critical"


_SEVERITY = {
    Level.OK: 0, Level.WARN: 1, Level.BUDGET: 2, Level.DISK_LOW: 3, Level.DISK_CRITICAL: 4,
}


def _worse(a: Level, b: Level) -> Level:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


# Thresholds (plan §3).
WARN_BUDGET_RATIO = 0.85
BUDGET_RATIO = 1.0
DISK_LOW_MIN = 3 * GB
DISK_LOW_PCT = 0.05
DISK_CRITICAL_MIN = 1 * GB
DISK_CRITICAL_PCT = 0.02
# Hysteresis: how far past its trigger a level must recover before it lifts.
RESUME_HEADROOM = 1 * GB
BUDGET_RESUME_RATIO = 0.95
WARN_RESUME_RATIO = 0.80
# An edit that grows the file's indexed text by more than this is "growing".
GROW_EDIT_BYTES = 64 * KB

OPS = frozenset({
    "add_content", "grow_edit", "caption", "reembed_dual", "shrink_edit",
    "delete", "move", "purge", "gc", "search", "insert", "upsert",
})

# What each level still permits (plan §3 table). Search reads only and is
# never paused; deletes, purges and GC free space and always run.
_ALLOWED: dict[Level, frozenset[str]] = {
    Level.OK: OPS,
    Level.WARN: OPS,
    Level.BUDGET: frozenset({
        "shrink_edit", "delete", "move", "purge", "gc", "search",
        # The paused file still gets its deferred row and ≤ 1 KB card.
        "insert", "upsert",
    }),
    # All ingestion waits. A move stays allowed: it rewrites rows in place
    # (no growth) and keeps a renamed file's vectors instead of turning it
    # into a delete plus a paused add. Raw inserts/upserts are still allowed
    # so bookkeeping and in-flight transactions can finish.
    Level.DISK_LOW: frozenset({"delete", "move", "purge", "gc", "search", "insert", "upsert"}),
    # Only what frees space.
    Level.DISK_CRITICAL: frozenset({"delete", "purge", "gc", "search"}),
}

_DESCRIPTIONS: dict[Level, str] = {
    Level.OK: "Storage is fine.",
    Level.WARN: (
        "Storage for the document index is getting full; large syncs will ask "
        "before they start."
    ),
    Level.BUDGET: (
        "The document index has reached its storage budget: new and growing "
        "files wait, while deletes, moves and search keep working."
    ),
    Level.DISK_LOW: (
        "Disk space is low, so indexing is paused. Deletes and purges still run, "
        "and search keeps working."
    ),
    Level.DISK_CRITICAL: (
        "Disk space is critically low: every index write is paused except those "
        "that free space. Search keeps working."
    ),
}


def classify_edit(growth_bytes: int) -> str:
    """``"grow_edit"`` when an edit adds more than :data:`GROW_EDIT_BYTES` of
    indexed text, else ``"shrink_edit"`` (small growth counts as shrinking:
    it cannot move a full index meaningfully)."""
    return "grow_edit" if growth_bytes > GROW_EDIT_BYTES else "shrink_edit"


def _disk_floors(total: int) -> tuple[int, int, int]:
    """(critical, low, warn) free-space triggers for a volume of ``total`` bytes."""
    low = max(DISK_LOW_MIN, int(DISK_LOW_PCT * total))
    critical = max(DISK_CRITICAL_MIN, int(DISK_CRITICAL_PCT * total))
    return critical, low, 2 * low


class Governor:
    """Turns usage and capacity into a :class:`Level`, with per-profile memory
    for hysteresis. Thread-safe; one instance serves every profile.

    Budgets of 0 are disabled.
    """

    def __init__(self, *, global_budget_bytes: int, profile_budget_bytes: int = 0) -> None:
        self.global_budget_bytes = max(0, int(global_budget_bytes or 0))
        self.profile_budget_bytes = max(0, int(profile_budget_bytes or 0))
        self._lock = threading.Lock()
        # profile → (disk axis level, budget axis level) from the last evaluate
        self._last: dict[str, tuple[Level, Level]] = {}

    # ── the two axes ───────────────────────────────────────────────────────

    @staticmethod
    def _volumes(usage: Usage, cap: Capacity) -> list[tuple[int, int]]:
        """(free, total) for every volume that can be judged.

        The measured filesystem, plus one synthetic volume for the explicit
        capacities, whose "used" is ``global_total``. That over-counts when
        the index DBs sit on another volume than the vectors — on purpose:
        the governor would rather stop early than let a store hit ENOSPC.
        """
        vols: list[tuple[int, int]] = []
        if cap.fs_free is not None and cap.fs_total:
            vols.append((max(0, cap.fs_free), cap.fs_total))
        explicit = (cap.vector_capacity or 0) + (cap.db_capacity or 0)
        if explicit > 0:
            vols.append((max(0, explicit - max(0, usage.global_total)), explicit))
        return vols

    @staticmethod
    def _disk_level(vols: list[tuple[int, int]], enospc: bool, prev: Level) -> Level:
        raw = Level.OK
        for free, total in vols:
            critical, low, warn = _disk_floors(total)
            if free < critical:
                lvl = Level.DISK_CRITICAL
            elif free < low:
                lvl = Level.DISK_LOW
            elif free < warn:
                lvl = Level.WARN
            else:
                lvl = Level.OK
            raw = _worse(raw, lvl)
        if enospc:
            raw = Level.DISK_CRITICAL

        # Hold the previous level (or any level between it and ``raw``) while
        # some volume is still within RESUME_HEADROOM of that level's trigger.
        held = Level.OK
        for lvl, idx in ((Level.DISK_CRITICAL, 0), (Level.DISK_LOW, 1), (Level.WARN, 2)):
            if _SEVERITY[lvl] > _SEVERITY[prev]:
                continue
            if any(free < _disk_floors(total)[idx] + RESUME_HEADROOM for free, total in vols):
                held = lvl
                break
        return _worse(raw, held)

    def _budget_ratio(self, usage: Usage) -> float:
        ratio = 0.0
        if self.global_budget_bytes > 0:
            ratio = max(ratio, max(0, usage.global_total) / self.global_budget_bytes)
        if self.profile_budget_bytes > 0:
            ratio = max(ratio, max(0, usage.profile_total) / self.profile_budget_bytes)
        return ratio

    @staticmethod
    def _budget_level(ratio: float, prev: Level) -> Level:
        if ratio >= BUDGET_RATIO:
            raw = Level.BUDGET
        elif ratio >= WARN_BUDGET_RATIO:
            raw = Level.WARN
        else:
            raw = Level.OK
        if prev is Level.BUDGET and ratio >= BUDGET_RESUME_RATIO:
            return _worse(raw, Level.BUDGET)
        if prev in (Level.BUDGET, Level.WARN) and ratio >= WARN_RESUME_RATIO:
            return _worse(raw, Level.WARN)
        return raw

    # ── public ─────────────────────────────────────────────────────────────

    def evaluate(
        self,
        usage: Usage,
        cap: Capacity,
        *,
        store_error_enospc: bool = False,
        profile: str = "",
    ) -> Level:
        """The level for ``profile`` now, remembering it for hysteresis.

        ``store_error_enospc``: the last vector-store or index-DB write failed
        with "No space left on device" (see :func:`is_enospc`); that alone is
        ``disk_critical``, whatever the measurements say. With nothing
        measurable afterwards the level lifts on the next evaluation, and the
        next write probes the store again.
        """
        vols = self._volumes(usage, cap)
        ratio = self._budget_ratio(usage)
        with self._lock:
            prev_disk, prev_budget = self._last.get(profile, (Level.OK, Level.OK))
            disk = self._disk_level(vols, store_error_enospc, prev_disk)
            budget = self._budget_level(ratio, prev_budget)
            self._last[profile] = (disk, budget)
        return _worse(disk, budget)

    def last_level(self, profile: str = "") -> Level:
        """The level from ``profile``'s last :meth:`evaluate` (``OK`` if none)."""
        with self._lock:
            disk, budget = self._last.get(profile, (Level.OK, Level.OK))
        return _worse(disk, budget)

    def forget(self, profile: str) -> None:
        """Drop ``profile``'s hysteresis memory (profile deleted or purged)."""
        with self._lock:
            self._last.pop(profile, None)

    @staticmethod
    def allows(level: Level, op: str) -> bool:
        """Whether ``op`` may run at ``level``. Unknown ops raise ValueError."""
        if op not in OPS:
            raise ValueError(f"unknown storage operation {op!r}")
        return op in _ALLOWED[Level(level)]

    @staticmethod
    def describe(level: Level) -> str:
        """One user-facing sentence for ``level``."""
        return _DESCRIPTIONS[Level(level)]


def is_enospc(error: BaseException | str | None) -> bool:
    """True when an error (or its message) means the disk is full.

    Matches errno ENOSPC and the texts the stores relay it with: SQLite's
    "database or disk is full", Qdrant's and Chroma's "No space left on
    device".
    """
    if error is None:
        return False
    # Windows' ERROR_DISK_FULL surfaces as errno ENOSPC on an OSError too.
    if isinstance(error, OSError) and getattr(error, "errno", None) == errno.ENOSPC:
        return True
    text = str(error).lower()
    return (
        "no space left" in text
        or "disk is full" in text
        or "enospc" in text
        or "not enough space" in text  # Windows ERROR_DISK_FULL wording
    )
