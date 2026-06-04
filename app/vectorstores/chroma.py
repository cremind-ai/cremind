import socket
from typing import Any, Dict, Iterable, List, Optional, Union

import chromadb
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config.settings import BaseConfig
from app.constants.status import Status
from app.lib.exception import VectorStoreException
from .base import EmbeddingProvider, StoredPoint, VectorStoreBase


class ChromaException(VectorStoreException):
    """Chroma Exception"""


def _build_chroma_client():
    if BaseConfig.get_chroma_mode() == "persistent":
        return chromadb.PersistentClient(path=BaseConfig.get_chroma_persist_path())
    api_key = BaseConfig.get_chroma_api_key()
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    return chromadb.HttpClient(
        host=BaseConfig.get_chroma_host() or "localhost",
        port=BaseConfig.get_chroma_port(),
        ssl=BaseConfig.get_chroma_ssl(),
        headers=headers,
    )


chroma_text_key = "text"


def _coerce_metadata(value: Any) -> dict:
    """Chroma metadata values must be primitive scalars; flatten anything else.

    Nested dicts/lists are JSON-stringified so the value still round-trips.
    """
    import json

    if not value:
        return {}
    out: dict = {}
    for k, v in value.items():
        if v is None or isinstance(v, (str, int, float, bool)):
            out[k] = v
        else:
            out[k] = json.dumps(v, ensure_ascii=False, default=str)
    return out


def _coerce_where_value(v: Any) -> Any:
    """Coerce a where-clause value to a Chroma-compatible scalar.

    Enums are unwrapped to their ``.value`` so callers can pass ``ToolType.SKILL``
    just like the Qdrant adapter accepts; everything else passes through.
    """
    from enum import Enum

    if isinstance(v, Enum):
        return v.value
    return v


def _to_chroma_where(filter: Optional[dict]) -> Optional[dict]:
    """Translate the abstract `{key: value}` filter form into Chroma's `where`.

    - ``None`` / empty   → ``None``
    - Scalar value       → equality (``{key: value}``)
    - List/tuple value   → ``IN``-set match (``{key: {"$in": [...]}}``)
    - Single-key dict    → returned as-is (after the above translation)
    - Multi-key dict     → ``{"$and": [{k1: v1}, {k2: v2}, ...]}``

    Chroma rejects multi-key plain dicts ("expected exactly one operator"),
    so we wrap them in ``$and``. Pre-formatted Chroma filters (those whose
    keys start with ``$``) are passed through unchanged.
    """
    if not filter:
        return None
    if any(isinstance(k, str) and k.startswith("$") for k in filter.keys()):
        return dict(filter)
    items: list = []
    for k, v in filter.items():
        coerced = _coerce_where_value(v)
        if isinstance(coerced, (list, tuple)):
            items.append((k, {"$in": [_coerce_where_value(x) for x in coerced]}))
        else:
            items.append((k, coerced))
    if len(items) == 1:
        k, v = items[0]
        return {k: v}
    return {"$and": [{k: v} for k, v in items]}


def _coerce_id(raw_id: Any) -> Union[int, str]:
    """Round-trip numeric ids back to int.

    Chroma stores ids as strings. Callers (e.g. ``DocumentSyncService``)
    construct numeric ids and compare against returned ids by equality, so
    a digits-only string id is coerced back to int. Non-numeric ids
    (UUIDs, slugs, etc.) pass through unchanged.
    """
    if isinstance(raw_id, str):
        s = raw_id[1:] if raw_id.startswith(("-", "+")) else raw_id
        if s.isdigit():
            try:
                return int(raw_id)
            except ValueError:
                return raw_id
    return raw_id


class ChromaClient(VectorStoreBase):
    def __init__(self, size: int):
        self._client = _build_chroma_client()
        self.size = size
        self._text_key = chroma_text_key

    # ── collection lifecycle ────────────────────────────────────────────────

    def create_collection(self, **kwargs: Any) -> str:
        uuid = self.create_collection_uuid()
        self._client.get_or_create_collection(name=uuid)
        return uuid

    def create_named_collection(self, collection_name: str, size: int) -> str:
        # Recreate semantics (mirror Qdrant): drop then make fresh.
        try:
            self._client.delete_collection(name=collection_name)
        except Exception:  # noqa: BLE001 — not-found is fine
            pass
        self._client.get_or_create_collection(name=collection_name)
        return collection_name

    def collection_exists(self, collection_name: str) -> bool:
        try:
            self._client.get_collection(name=collection_name)
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_collections(self) -> List[str]:
        try:
            cols = self._client.list_collections()
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))
        return [c.name for c in cols]

    def delete_collection(
        self, collection_name: str, force: bool = False, **kwargs: Any
    ) -> None:
        try:
            self._client.delete_collection(name=collection_name)
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))

    # ── retry-wrapped primitives ────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, Exception)),
    )
    def _safe_upsert(
        self,
        collection_name: str,
        ids: List[str],
        embeddings: List[List[float]],
        documents: List[str],
        metadatas: List[dict],
    ):
        col = self._client.get_or_create_collection(name=collection_name)
        return col.upsert(
            ids=ids,
            embeddings=embeddings,
            documents=documents,
            metadatas=metadatas,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, Exception)),
    )
    def _safe_get(self, collection_name: str, **kwargs):
        col = self._client.get_collection(name=collection_name)
        return col.get(**kwargs)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, Exception)),
    )
    def _safe_query(self, collection_name: str, **kwargs):
        col = self._client.get_collection(name=collection_name)
        return col.query(**kwargs)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, Exception)),
    )
    def _safe_delete(self, collection_name: str, **kwargs):
        col = self._client.get_collection(name=collection_name)
        return col.delete(**kwargs)

    # ── text CRUD ───────────────────────────────────────────────────────────

    def add_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        text_list = list(texts)
        embeddings = embedding_function.embed_documents(text_list)
        str_ids = [str(i) for i in ids]
        metas: List[dict] = []
        for i, text in enumerate(text_list):
            md = dict(metadatas[i]) if metadatas else {}
            md[self._text_key] = str(text)
            metas.append(_coerce_metadata(md))
        try:
            self._safe_upsert(
                collection_name=collection_name,
                ids=str_ids,
                embeddings=embeddings,
                documents=text_list,
                metadatas=metas,
            )
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))
        return ids

    def add_points(
        self,
        collection_name: str,
        points: List[StoredPoint],
    ) -> None:
        """Add pre-embedded points directly (skips embedding generation)."""
        ids = [str(p["id"]) for p in points]
        embeddings = [p["vector"] or [] for p in points]
        documents: List[str] = []
        metas: List[dict] = []
        for p in points:
            payload = dict(p.get("payload") or {})
            documents.append(str(payload.get(self._text_key, "")))
            metas.append(_coerce_metadata(payload))
        try:
            self._safe_upsert(
                collection_name=collection_name,
                ids=ids,
                embeddings=embeddings,
                documents=documents,
                metadatas=metas,
            )
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))

    def get_texts(
        self,
        collection_name: str,
        ids: List[int],
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        str_ids = [str(i) for i in ids]
        try:
            res = self._safe_get(
                collection_name=collection_name,
                ids=str_ids,
                include=["documents", "metadatas"],
            )
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))

        out: List[dict[Any, Any]] = []
        result_ids = res.get("ids") or []
        documents = res.get("documents") or []
        metadatas = res.get("metadatas") or []
        for i, raw_id in enumerate(result_ids):
            md = dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {}
            text = md.pop(self._text_key, None)
            if text is None and i < len(documents):
                text = documents[i]
            out.append({"id": raw_id, "text": text, "metadata": md})
        return out

    def list_all_points(
        self,
        collection_name: str,
        with_vectors: bool = False,
        filter: Optional[dict] = None,
    ) -> List[StoredPoint]:
        include = ["metadatas", "documents"]
        if with_vectors:
            include.append("embeddings")
        where = _to_chroma_where(filter)
        try:
            kwargs: Dict[str, Any] = {"include": include}
            if where is not None:
                kwargs["where"] = where
            res = self._safe_get(collection_name=collection_name, **kwargs)
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))

        ids = res.get("ids") or []
        metadatas = res.get("metadatas") or []
        documents = res.get("documents") or []
        embeddings = res.get("embeddings") or [] if with_vectors else []

        out: List[StoredPoint] = []
        for i, raw_id in enumerate(ids):
            payload = dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {}
            if self._text_key not in payload and i < len(documents) and documents[i] is not None:
                payload[self._text_key] = documents[i]
            vec: Optional[List[float]] = None
            if with_vectors and i < len(embeddings) and embeddings[i] is not None:
                vec = list(embeddings[i])
            out.append(StoredPoint(id=_coerce_id(raw_id), vector=vec, payload=payload))
        return out

    def update_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        # Chroma's `upsert` already does insert-or-update — same path as add.
        return self.add_texts(
            collection_name=collection_name,
            embedding_function=embedding_function,
            texts=texts,
            ids=ids,
            metadatas=metadatas,
            **kwargs,
        )

    def delete_texts(
        self,
        collection_name: str,
        ids: Optional[List[int]] = None,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        try:
            if ids is not None:
                self._safe_delete(
                    collection_name=collection_name,
                    ids=[str(i) for i in ids],
                )
            elif filter:
                # Chroma rejects multi-key plain dicts; translate via the
                # shared helper so the abstract {key: value} contract works
                # for both single- and multi-key filters.
                where = _to_chroma_where(filter)
                self._safe_delete(collection_name=collection_name, where=where)
            else:
                raise ValueError(
                    "Either 'ids' or 'filter' must be provided to delete texts."
                )
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))

    def query(
        self,
        query_text: str,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        limit: int = 3,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        embedding = embedding_function.embed_documents([query_text])[0]
        where = _to_chroma_where(filter)
        try:
            res = self._safe_query(
                collection_name=collection_name,
                query_embeddings=[embedding],
                n_results=limit,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))

        # Chroma returns parallel arrays nested under a per-query outer list.
        ids = (res.get("ids") or [[]])[0]
        documents = (res.get("documents") or [[]])[0]
        metadatas = (res.get("metadatas") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]

        formatted: List[dict[Any, Any]] = []
        for i, raw_id in enumerate(ids):
            md = dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {}
            text = md.pop(self._text_key, None)
            if text is None and i < len(documents):
                text = documents[i]
            distance = distances[i] if i < len(distances) else None
            # Cosine distance → similarity score; matches Qdrant's score orientation.
            score = (1.0 - distance) if distance is not None else None
            formatted.append({"id": raw_id, "text": text, "metadata": md, "score": score})
        return formatted

    def query_by_vector(
        self,
        collection_name: str,
        vector: List[float],
        limit: int = 10,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Search by a pre-computed vector; return flat-payload dicts with score."""
        where = _to_chroma_where(filter)
        try:
            res = self._safe_query(
                collection_name=collection_name,
                query_embeddings=[vector],
                n_results=limit,
                where=where,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            raise ChromaException(Status.VECTOR_STORE_ERROR, str(e))

        ids = (res.get("ids") or [[]])[0]
        documents = (res.get("documents") or [[]])[0]
        metadatas = (res.get("metadatas") or [[]])[0]
        distances = (res.get("distances") or [[]])[0]

        out: List[Dict[str, Any]] = []
        for i, raw_id in enumerate(ids):
            payload = dict(metadatas[i]) if i < len(metadatas) and metadatas[i] else {}
            if self._text_key not in payload and i < len(documents) and documents[i] is not None:
                payload[self._text_key] = documents[i]
            distance = distances[i] if i < len(distances) else None
            # Cosine distance → similarity score; matches Qdrant's score orientation.
            score = (1.0 - distance) if distance is not None else None
            payload["id"] = _coerce_id(raw_id)
            payload["score"] = score
            out.append(payload)
        return out
