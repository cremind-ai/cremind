"""Rename a profile's vector collection from ``ud_*`` to ``doc_*`` — by copying.

Collections created before the rename are called ``ud_<tag>_<epoch>_…`` and
the index DB's ``collections`` row persists that physical name, so search keeps
working with it untouched. This module moves each profile to the ``doc_`` name
without re-embedding anything: no vector store can rename a collection, so the
points are copied — ids, vectors and payloads exactly as stored — into the new
collection, verified, and only then does the row switch. The old collection is
dropped afterwards, as a separately recorded cleanup.

One bounded step at a time, from the documents engine's embedder thread
(:meth:`app.documents.service.DocumentsService._embedder`), never the event
loop. Every step persists its progress in the index DB's ``meta`` table, so a
restart resumes where it stopped:

``copy``     scroll the source's ids page by page; fetch those points; upsert
             them into the destination (created with the non-destructive
             ``ensure_collection`` — never ``create_named_collection``, which
             is drop-and-recreate).
``missing``  reconcile the id sets, page by page: copy what the destination
``extra``    lacks, delete what the source no longer has. Always run, because
             Chroma pages by position (a delete mid-copy shifts the rest and
             one id can be skipped) and a delete plus an insert would cancel
             out in the counts.
``verify``   equal point counts, and a sample of points equal on both sides
             (vector within float tolerance, payload identical, dimension as
             recorded). Then ONE index-DB transaction renames the row and
             records the source for cleanup (``IndexDB.rename_collection``).
             Counts that still differ send it back to ``missing``.

While a copy is in progress the embedder does not write new vectors for that
profile (``StepResult.hold``), so the two sides converge instead of chasing
each other; searches keep using the source until the switch.

Nothing here runs when it cannot finish safely: no live store (disabled,
rebuilding, unreachable), a store without the pre-embedded primitives, a
storage level that forbids a second copy of the vectors, or too little free
disk for it. The profile then simply keeps its ``ud_`` collection — vector and
keyword search both work — and the next pass tries again. A generation that a
model or store change retires mid-copy is abandoned (its partial destination
dropped when that is provably safe), since :mod:`vector_sync` re-embeds into a
``doc_`` name of its own.

Garbage collection keeps both sides throughout: the boot GC
(``DocumentsService._gc_orphan_indexes``) drops only collections whose uid
TAG belongs to no live profile, and ``parse_collection_name`` reads the tag
out of ``ud_`` and ``doc_`` names alike.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.utils.logger import logger

META_STATE = "vector_migration"
META_CLEANUP = "vector_migration_cleanup"
META_FAILED = "vector_migration_failed_at"

BATCH = 256
SAMPLE = 24
# Verification failures before the migration stops for RETRY_AFTER_S. Repeated
# failures mean the store does not round-trip what it is given, and copying
# again every few seconds would only double its load.
MAX_ATTEMPTS = 3
MAX_RECONCILES = 20
RETRY_AFTER_S = 24 * 3600.0
# The old collection outlives the switch by this long, so a search that read
# the row a moment before the switch still finds the collection it named.
CLEANUP_DELAY_S = 60.0
VECTOR_TOLERANCE = 1e-5
_DEFER_LOG_EVERY_S = 3600.0

_deferral_logged: dict[str, float] = {}


@dataclass
class StepResult:
    """``work``: units done this call (>0 asks the caller to come back soon).
    ``hold``: a copy is in progress, so do not embed into this profile's
    collection during this pass."""

    work: int = 0
    hold: bool = False


# ── State in the index DB ──────────────────────────────────────────────────


def _load_json(db: Any, key: str, default: Any) -> Any:
    raw = db.get_meta(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def _save_state(db: Any, state: Optional[dict[str, Any]]) -> None:
    db.set_meta(META_STATE, None if state is None else json.dumps(state, sort_keys=True))


def pending(db: Any) -> bool:
    """Is there anything left to do for this index (a ``ud_`` collection in
    use, a copy in progress, or an old collection waiting to be dropped)?"""
    from app.documents.vectors import LEGACY_COLLECTION_PREFIX

    if _load_json(db, META_STATE, None) or _load_json(db, META_CLEANUP, []):
        return True
    active = db.active_collection()
    return bool(active and str(active.get("name") or "").startswith(LEGACY_COLLECTION_PREFIX))


def status(db: Any) -> dict[str, Any]:
    """For diagnostics: where the migration stands."""
    return {
        "state": _load_json(db, META_STATE, None),
        "cleanup": _load_json(db, META_CLEANUP, []),
        "failed_at": db.get_meta(META_FAILED),
        "pending": pending(db),
    }


# ── Reading points (ids, vectors AND payloads) ─────────────────────────────


def _qdrant_points(adapter: Any, name: str, ids: list[int]) -> dict[int, tuple[list[float], dict]]:
    out: dict[int, tuple[list[float], dict]] = {}
    for i in range(0, len(ids), BATCH):
        records = adapter._client.retrieve(
            collection_name=name, ids=[int(x) for x in ids[i:i + BATCH]],
            with_payload=True, with_vectors=True,
        )
        for r in records:
            if not isinstance(r.vector, list):
                raise NotImplementedError("named or sparse vectors are not migrated")
            out[int(r.id)] = ([float(x) for x in r.vector], dict(r.payload or {}))
    return out


def _chroma_points(adapter: Any, name: str, ids: list[int]) -> dict[int, tuple[list[float], dict]]:
    from app.vectorstores.chroma import _coerce_id

    out: dict[int, tuple[list[float], dict]] = {}
    col = adapter._client.get_collection(name=name)
    for i in range(0, len(ids), BATCH):
        res = col.get(ids=[str(int(x)) for x in ids[i:i + BATCH]], include=["embeddings", "metadatas"])
        got = res.get("ids") or []
        embeddings = res.get("embeddings")  # an ndarray: never test it for truthiness
        metadatas = res.get("metadatas") or []
        for idx, raw_id in enumerate(got):
            pid = _coerce_id(raw_id)
            if not isinstance(pid, int) or embeddings is None or idx >= len(embeddings):
                continue
            emb = embeddings[idx]
            if emb is None:
                continue
            md = metadatas[idx] if idx < len(metadatas) and metadatas[idx] else {}
            out[pid] = ([float(x) for x in emb], dict(md))
    return out


def retrieve_points(
    store: Any, name: str, ids: list[int], rt: Any = None,
) -> tuple[dict[int, tuple[list[float], Optional[dict]]], bool]:
    """``({id: (vector, payload)}, payloads_are_stored)`` for ``ids``.

    The adapters' pre-embedded primitives return vectors only, so this reads
    payloads through the adapter's own client for the two backends Cremind
    ships (``retrieve_points`` on the store wins if it ever exists). For any
    other store the vectors come from ``retrieve_vectors`` and the payloads
    are rebuilt from the index — ``payloads_are_stored`` is then False and the
    verification compares vectors only."""
    ids = [int(i) for i in ids]
    if not ids:
        return {}, True
    fn = getattr(store, "retrieve_points", None)
    if callable(fn):
        try:
            got = fn(name, ids)
            return {int(k): (list(v[0]), dict(v[1] or {})) for k, v in got.items()}, True
        except NotImplementedError:
            pass
    adapter = getattr(store, "_client", store)
    kind = f"{type(adapter).__module__}.{type(adapter).__name__}"
    if kind == "app.vectorstores.qdrant.QdrantClient":
        return _qdrant_points(adapter, name, ids), True
    if kind == "app.vectorstores.chroma.ChromaClient":
        return _chroma_points(adapter, name, ids), True
    vectors = store.retrieve_vectors(name, ids)
    payloads: dict[int, Optional[dict]] = {}
    if rt is not None and rt.db is not None and vectors:
        from app.documents.vector_sync import _payloads

        rows = [r for r in rt.db.chunk_rows(list(vectors))]
        for row, payload in zip(rows, _payloads(rt, rows)):
            payloads[int(row["id"])] = payload
    # A point whose chunk is gone keeps an empty payload: it is copied anyway
    # (the counts must match) and the GC removes it from the new collection.
    return {i: (v, payloads.get(i, {})) for i, v in vectors.items()}, False


def existing_ids(store: Any, name: str, ids: list[int]) -> set[int]:
    """Which of ``ids`` have a point in ``name`` — without transferring the
    vectors where the adapter allows (the reconcile passes ask this of every
    point, twice)."""
    ids = [int(i) for i in ids]
    if not ids:
        return set()
    adapter = getattr(store, "_client", store)
    kind = f"{type(adapter).__module__}.{type(adapter).__name__}"
    if kind == "app.vectorstores.qdrant.QdrantClient":
        found: set[int] = set()
        for i in range(0, len(ids), BATCH):
            for r in adapter._client.retrieve(
                collection_name=name, ids=ids[i:i + BATCH], with_payload=False, with_vectors=False,
            ):
                found.add(int(r.id))
        return found
    if kind == "app.vectorstores.chroma.ChromaClient":
        from app.vectorstores.chroma import _coerce_id

        col = adapter._client.get_collection(name=name)
        found = set()
        for i in range(0, len(ids), BATCH):
            res = col.get(ids=[str(x) for x in ids[i:i + BATCH]], include=[])
            found.update(p for p in (_coerce_id(r) for r in res.get("ids") or []) if isinstance(p, int))
        return found
    return {int(k) for k in store.retrieve_vectors(name, ids)}


def _copy(store: Any, src: str, dst: str, ids: list[int], rt: Any) -> int:
    if not ids:
        return 0
    points, _stored = retrieve_points(store, src, ids, rt)
    got = [i for i in ids if i in points]
    if got:
        store.upsert_vectors(
            dst, got, [points[i][0] for i in got], [dict(points[i][1] or {}) for i in got],
        )
    return len(got)


def _payload_equal(a: Optional[dict], b: Optional[dict]) -> bool:
    def norm(p: Optional[dict]) -> dict:
        return {k: (int(v) if isinstance(v, float) and v.is_integer() else v)
                for k, v in (p or {}).items() if v is not None}

    return norm(a) == norm(b)


def _sample_matches(store: Any, src: str, dst: str, dim: int, rt: Any) -> bool:
    ids, _ = store.scroll_ids(src, offset=None, limit=max(SAMPLE * 8, 200))
    if not ids:
        return True
    stride = max(1, len(ids) // SAMPLE)
    sample = [int(i) for i in ids[::stride][:SAMPLE]]
    a, a_stored = retrieve_points(store, src, sample, rt)
    b, b_stored = retrieve_points(store, dst, sample, rt)
    for pid in sample:
        if (pid in a) != (pid in b):
            return False
        if pid not in a:
            continue
        va, pa = a[pid]
        vb, pb = b[pid]
        if len(va) != len(vb) or (dim and len(va) != dim):
            return False
        if any(not math.isclose(x, y, rel_tol=VECTOR_TOLERANCE, abs_tol=VECTOR_TOLERANCE)
               for x, y in zip(va, vb)):
            return False
        if a_stored and b_stored and not _payload_equal(pa, pb):
            return False
    return True


# ── Guards ─────────────────────────────────────────────────────────────────


def _note_deferred(rt: Any, why: str) -> None:
    now = time.monotonic()
    key = str(getattr(rt, "uid", "") or getattr(rt, "profile", ""))
    if now - _deferral_logged.get(key, -_DEFER_LOG_EVERY_S) >= _DEFER_LOG_EVERY_S:
        _deferral_logged[key] = now
        logger.info(
            f"[documents] {getattr(rt, 'profile', key)}: vector collection rename waits ({why}); "
            "search keeps using the current collection"
        )


def _capacity_ok(rt: Any, store: Any, src: str, dim: int) -> bool:
    """Room for a second copy of the profile's vectors while both exist."""
    from app.documents import governor as gov

    try:
        level = gov.Level(getattr(rt, "level", "ok") or "ok")
    except ValueError:
        level = gov.Level.OK
    if not gov.Governor.allows(level, "reembed_dual"):
        _note_deferred(rt, f"storage level is {level.value}")
        return False
    try:
        from app.documents import settings as uds
        from app.documents.runtime import _vector_backend

        policy = uds.read_admin_policy()
        cap = gov.measure_capacity(
            uds.system_dir(),
            vector_capacity_mb=policy.vector_capacity_mb, db_capacity_mb=policy.db_capacity_mb,
        )
        need = gov.estimate_bytes(int(store.count(src)), backend=_vector_backend(), dim=dim)["vectors"]
    except Exception as exc:  # noqa: BLE001 — cannot measure: the level above decided
        logger.debug(f"[documents] {getattr(rt, 'profile', '?')}: capacity unknown ({exc})")
        return True
    if cap.fs_free is not None and cap.fs_total:
        floor = max(gov.DISK_LOW_MIN, int(gov.DISK_LOW_PCT * cap.fs_total))
        if cap.fs_free - need < floor:
            _note_deferred(rt, f"needs about {need // (1024 * 1024)} MB of free disk")
            return False
    return True


# ── The step ───────────────────────────────────────────────────────────────


def _cleanup(db: Any, store: Any, skey: str) -> int:
    """Drop the old collections the switch recorded. Returns how many were
    handled. One held by another store stays recorded until that store is
    the live one again."""
    items = _load_json(db, META_CLEANUP, [])
    if not items:
        return 0
    now = time.time()
    if not any(
        item.get("store_key") == skey and float(item.get("after") or 0) <= now for item in items
    ):
        return 0  # nothing due in this store: no need to ask it anything
    names = set(store.list_collections())  # raises when unreachable: retry later
    active = db.active_collection() or {}
    remaining: list[dict[str, Any]] = []
    done = 0
    for item in items:
        name = str(item.get("name") or "")
        if (
            not name
            or item.get("store_key") != skey
            or name == active.get("name")
            or float(item.get("after") or 0) > now
        ):
            remaining.append(item)
            continue
        if name in names:
            store.delete_collection(name)
            logger.info(f"[documents] dropped the pre-rename collection {name}")
        done += 1
    if done:
        db.set_meta(META_CLEANUP, json.dumps(remaining) if remaining else None)
    return done


def _abandon(db: Any, store: Any, skey: str, state: dict[str, Any], active: Optional[dict]) -> None:
    """The generation being copied is gone (a model or store change). Drop
    the partial destination only when it is certainly ours: same store, and
    not the name the live generation now uses (a same-model re-embed into a
    new store reuses the name)."""
    dst = str(state.get("dst") or "")
    if dst and state.get("store_key") == skey and dst != (active or {}).get("name"):
        try:
            if dst in store.list_collections():
                store.delete_collection(dst)
        except Exception as exc:  # noqa: BLE001 — the GC finds it later
            logger.debug(f"[documents] could not drop the abandoned copy {dst}: {exc}")
    _save_state(db, None)
    logger.info(f"[documents] vector collection rename abandoned: generation {state.get('gen')} was retired")


def _fail(db: Any, store: Any, rt: Any, state: dict[str, Any], why: str) -> None:
    dst = str(state.get("dst") or "")
    try:
        if dst and dst in store.list_collections():
            store.delete_collection(dst)
    except Exception:  # noqa: BLE001
        pass
    _save_state(db, None)
    db.set_meta(META_FAILED, repr(time.time()))
    logger.warning(
        f"[documents] {getattr(rt, 'profile', '?')}: vector collection rename stopped ({why}); "
        f"search keeps using {state.get('src')}, retry in {int(RETRY_AFTER_S // 3600)}h"
    )


def step(rt: Any) -> StepResult:
    """One bounded unit of migration work for ``rt`` (see the module doc)."""
    from app.documents import vector_sync
    from app.documents.governor import is_enospc

    db = getattr(rt, "db", None)
    if db is None or getattr(db, "closed", False) or getattr(rt, "paused_user", False):
        return StepResult()
    if getattr(rt, "_vector_rename_settled", None) is db:
        # Nothing was left for this index file: every generation from here on
        # is created with a ``doc_`` name, so there is nothing to look up
        # again on every embedder pass.
        return StepResult()
    handles = vector_sync.live_handles()
    if handles is None:
        return StepResult()
    emb, store = handles
    skey = vector_sync.store_key()
    try:
        cleaned = _cleanup(db, store, skey)
        if cleaned:
            return StepResult(work=cleaned)
        result = _migrate(rt, db, emb, store, skey)
        if not result.work and not result.hold and not pending(db):
            try:
                rt._vector_rename_settled = db
            except AttributeError:  # a runtime stand-in without attributes
                pass
        return result
    except NotImplementedError:
        return StepResult()
    except Exception as exc:  # noqa: BLE001 — the store is down or failing: later
        if is_enospc(exc):
            try:
                rt.service.note_enospc(rt)
            except Exception:  # noqa: BLE001
                pass
        if getattr(db, "closed", False):
            return StepResult()
        logger.debug(f"[documents] {getattr(rt, 'profile', '?')}: vector collection rename step failed: {exc}")
        return StepResult()


def _migrate(rt: Any, db: Any, emb: Any, store: Any, skey: str) -> StepResult:
    from app.documents.vectors import LEGACY_COLLECTION_PREFIX, PAYLOAD_INDEXES, collection_name

    active = db.active_collection()
    state = _load_json(db, META_STATE, None)
    if state and (
        not active
        or int(active.get("gen") or 0) != int(state.get("gen") or -1)
        or active.get("name") != state.get("src")
    ):
        _abandon(db, store, skey, state, active)
        return StepResult(work=1)
    if not active or not str(active.get("name") or "").startswith(LEGACY_COLLECTION_PREFIX):
        return StepResult()
    dim = int(active.get("dim") or 0)
    if (
        active.get("store_key") != skey
        or active.get("model_key") != getattr(emb, "model_key", None)
        or dim != int(getattr(emb, "dimension", 0) or 0)
    ):
        # vector_sync is about to replace this generation with a doc_ one.
        return StepResult()
    src, gen = str(active["name"]), int(active["gen"])

    if state is None:
        failed_at = db.get_meta(META_FAILED)
        try:
            if failed_at and time.time() - float(failed_at) < RETRY_AFTER_S:
                return StepResult()
        except ValueError:
            pass
        if src not in store.list_collections():
            # Nothing to copy; vector_sync's existence check re-embeds it.
            return StepResult()
        if not _capacity_ok(rt, store, src, dim):
            return StepResult()
        dst = collection_name(rt.uid, db.epoch, str(active.get("model_key") or ""), dim)
        store.ensure_collection(dst, dim, payload_indexes=PAYLOAD_INDEXES)
        state = {
            "gen": gen, "src": src, "dst": dst, "store_key": skey, "phase": "copy",
            "offset": None, "copied": 0, "attempt": 1, "reconciles": 0,
            "total": int(store.count(src)), "started_at": time.time(),
        }
        _save_state(db, state)
        db.add_activity(
            "vector_migration",
            f"Moving {state['total']} vectors to the renamed collection (no re-embedding).",
            detail={"from": src, "to": dst},
        )
        logger.info(f"[documents] {rt.profile}: copying {state['total']} vectors {src} -> {dst}")
        return StepResult(work=1, hold=True)

    dst = str(state["dst"])
    phase = state.get("phase")
    if phase == "copy":
        ids, nxt = store.scroll_ids(src, offset=state.get("offset"), limit=BATCH)
        n = _copy(store, src, dst, [int(i) for i in ids], rt)
        state["copied"] = int(state.get("copied") or 0) + n
        state["offset"] = nxt
        if nxt is None:
            # Always reconcile the id sets before verifying: Chroma pages by
            # position, so a point deleted during the copy shifts the rest
            # and one can be skipped — and a delete plus an insert cancel out
            # in the counts, which is all a cheap verification could compare.
            state.update(phase="missing", offset=None)
        _save_state(db, state)
        return StepResult(work=max(1, n), hold=True)

    if phase == "missing":
        ids, nxt = store.scroll_ids(src, offset=state.get("offset"), limit=BATCH)
        ids = [int(i) for i in ids]
        have = existing_ids(store, dst, ids)
        n = _copy(store, src, dst, [i for i in ids if i not in have], rt)
        state["offset"] = nxt
        if nxt is None:
            state.update(phase="extra", offset=None)
        _save_state(db, state)
        return StepResult(work=max(1, n), hold=True)

    if phase == "extra":
        ids, nxt = store.scroll_ids(dst, offset=state.get("offset"), limit=BATCH)
        ids = [int(i) for i in ids]
        have = existing_ids(store, src, ids)
        extra = [i for i in ids if i not in have]
        if extra:
            store.delete_ids(dst, extra)
        state["offset"] = nxt
        if nxt is None:
            state.update(phase="verify", offset=None)
        _save_state(db, state)
        return StepResult(work=max(1, len(extra)), hold=True)

    # verify
    n_src, n_dst = int(store.count(src)), int(store.count(dst))
    if n_src != n_dst:
        state["reconciles"] = int(state.get("reconciles") or 0) + 1
        if state["reconciles"] > MAX_RECONCILES:
            _fail(db, store, rt, state, f"counts keep differing ({n_src} vs {n_dst})")
            return StepResult(work=1)
        state.update(phase="missing", offset=None)
        _save_state(db, state)
        return StepResult(work=1, hold=True)
    if not _sample_matches(store, src, dst, dim, rt):
        state["attempt"] = int(state.get("attempt") or 1) + 1
        if state["attempt"] > MAX_ATTEMPTS:
            _fail(db, store, rt, state, "copied points did not match their source")
            return StepResult(work=1)
        state.update(phase="copy", offset=None, copied=0)
        _save_state(db, state)
        return StepResult(work=1, hold=True)

    cleanup = [c for c in _load_json(db, META_CLEANUP, []) if c.get("name") != src]
    cleanup.append({"name": src, "store_key": skey, "after": time.time() + CLEANUP_DELAY_S})
    switched = db.rename_collection(
        gen, old=src, new=dst,
        meta={META_STATE: None, META_CLEANUP: json.dumps(cleanup), META_FAILED: None},
    )
    if not switched:
        _abandon(db, store, skey, state, db.active_collection())
        return StepResult(work=1)
    db.add_activity(
        "vector_migration",
        f"Vectors now live in the renamed collection ({n_dst} points); the old one is being removed.",
        detail={"from": src, "to": dst},
    )
    logger.info(f"[documents] {rt.profile}: switched to {dst} ({n_dst} points); {src} is dropped next")
    return StepResult(work=1)


__all__ = [
    "META_CLEANUP",
    "META_FAILED",
    "META_STATE",
    "StepResult",
    "pending",
    "retrieve_points",
    "status",
    "step",
]
