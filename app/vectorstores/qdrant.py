import socket
from typing import Any, Dict, Iterable, List, Optional

import qdrant_client
from qdrant_client.http.models import (
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
    MatchAny,
    FilterSelector,
    PointIdsList,
    SearchParams,
)
from qdrant_client.models import Distance, VectorParams
from requests.exceptions import ConnectionError, ConnectTimeout
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from app.config.settings import BaseConfig
from app.constants.status import Status
from app.lib.exception import VectorStoreException
from .base import VectorStoreBase, EmbeddingProvider, StoredPoint


class QdrantException(VectorStoreException):
    """Qdrant Exception"""


qdrant_text_key = "text"


def _build_qdrant_client():
    """Build a fresh underlying Qdrant client from current config.

    Called per :class:`QdrantClient` instantiation so the Settings UI
    can swap host/port/auth without a server restart — module-level
    caching would silently keep using the old config until reimport.
    """
    return qdrant_client.QdrantClient(
        host=BaseConfig.get_qdrant_host() or "",
        port=BaseConfig.get_qdrant_port(),
        api_key=BaseConfig.get_qdrant_api_key() or None,
        https=BaseConfig.get_qdrant_https(),
    )


class QdrantClient(VectorStoreBase):
    def __init__(self, size: int):
        self._client = _build_qdrant_client()
        self.size = size
        self._text_key = qdrant_text_key

    def create_collection(self, **kwargs: Any) -> str:
        uuid = self.create_collection_uuid()
        self._client.recreate_collection(
            collection_name=uuid,
            vectors_config=VectorParams(size=self.size, distance=Distance.COSINE),
        )
        return uuid

    def create_named_collection(self, collection_name: str, size: int) -> str:
        """Create a collection with a specific name and vector size."""
        self._client.recreate_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=size, distance=Distance.COSINE),
        )
        return collection_name

    def collection_exists(self, collection_name: str) -> bool:
        """Check if a collection exists."""
        try:
            self._client.get_collection(collection_name)
            return True
        except Exception:
            return False

    def _build_filter(self, filter: Optional[dict]) -> Optional[Filter]:
        """Translate the abstract ``{key: value}`` filter into a Qdrant Filter.

        Scalar values match by equality (``MatchValue``). List/tuple values
        match any of the elements (``MatchAny``). Empty/None returns None.
        """
        if not filter:
            return None
        must = []
        for key, value in filter.items():
            if isinstance(value, (list, tuple)):
                must.append(FieldCondition(key=key, match=MatchAny(any=list(value))))
            else:
                must.append(FieldCondition(key=key, match=MatchValue(value=value)))
        return Filter(must=must)

    def list_collections(self) -> List[str]:
        collections = self._client.get_collections()
        return [c.name for c in collections.collections]

    def delete_collection(
        self, collection_name: str, force: bool = False, **kwargs: Any
    ) -> None:
        self._client.delete_collection(collection_name=collection_name)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, ConnectTimeout, Exception))
    )
    def _safe_upsert(self, collection_name: str, points: List[PointStruct], **kwargs):
        return self._client.upsert(collection_name=collection_name, points=points, **kwargs)

    def add_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        embeddings = embedding_function.embed_documents(list(texts))
        points = []
        for i, (text, embedding) in enumerate(zip(texts, embeddings)):
            metadata = metadatas[i] if metadatas else {}
            metadata[self._text_key] = str(text)
            point = PointStruct(id=ids[i], vector=embedding, payload=metadata)
            points.append(point)

        try:
            self._safe_upsert(collection_name=collection_name, points=points)
        except Exception as e:
            raise QdrantException(Status.VECTOR_STORE_ERROR, str(e))

        return ids

    def add_points(
        self,
        collection_name: str,
        points: List[StoredPoint],
    ) -> None:
        """Add pre-embedded points directly (skips embedding generation)."""
        qdrant_points = [
            PointStruct(id=p["id"], vector=p["vector"], payload=p.get("payload") or {})
            for p in points
        ]
        try:
            self._safe_upsert(collection_name=collection_name, points=qdrant_points)
        except Exception as e:
            raise QdrantException(Status.VECTOR_STORE_ERROR, str(e))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, ConnectTimeout, Exception))
    )
    def _safe_retrieve(self, collection_name: str, ids: List[int], **kwargs):
        return self._client.retrieve(collection_name=collection_name, ids=ids, **kwargs)

    def get_texts(
        self,
        collection_name: str,
        ids: List[int],
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Get texts from a collection using Qdrant."""
        point_ids = [id for id in ids]
        try:
            records = self._safe_retrieve(collection_name=collection_name, ids=point_ids)

            return [
                {
                    "id": record.id,
                    "text": record.payload["text"],
                    "metadata": {
                        key: value
                        for key, value in record.payload.items()
                        if key != "text"
                    },
                }
                for record in records
            ]
        except Exception as e:
            raise QdrantException(Status.VECTOR_STORE_ERROR, str(e))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, ConnectTimeout, Exception))
    )
    def _safe_scroll(self, collection_name: str, **kwargs):
        return self._client.scroll(collection_name=collection_name, **kwargs)

    def list_all_points(
        self,
        collection_name: str,
        with_vectors: bool = False,
        filter: Optional[dict] = None,
    ) -> List[StoredPoint]:
        """Enumerate points in a collection as provider-neutral StoredPoints."""
        all_points: List[StoredPoint] = []
        offset = None
        qdrant_filter = self._build_filter(filter)
        try:
            while True:
                records, next_offset = self._safe_scroll(
                    collection_name=collection_name,
                    limit=100,
                    offset=offset,
                    with_vectors=with_vectors,
                    scroll_filter=qdrant_filter,
                )
                for r in records:
                    vec = list(r.vector) if (with_vectors and r.vector is not None) else None
                    all_points.append(
                        StoredPoint(id=r.id, vector=vec, payload=dict(r.payload or {}))
                    )
                if next_offset is None:
                    break
                offset = next_offset
        except Exception as e:
            raise QdrantException(Status.VECTOR_STORE_ERROR, str(e))
        return all_points

    def update_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        embeddings = embedding_function.embed_documents(list(texts))
        points = []
        for i, (text, embedding) in enumerate(zip(texts, embeddings)):
            metadata = metadatas[i] if metadatas else {}
            metadata[self._text_key] = str(text)
            point = PointStruct(id=ids[i], vector=embedding, payload=metadata)
            points.append(point)

        try:
            self._safe_upsert(collection_name=collection_name, points=points)
        except Exception as e:
            raise QdrantException(Status.VECTOR_STORE_ERROR, str(e))

        return ids

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, ConnectTimeout, Exception))
    )
    def _safe_delete(self, collection_name: str, points_selector, **kwargs):
        return self._client.delete(collection_name=collection_name, points_selector=points_selector, **kwargs)

    def delete_texts(
        self,
        collection_name: str,
        ids: Optional[List[int]] = None,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        """Delete texts from a collection."""
        try:
            if ids is not None:
                points_selector = PointIdsList(points=ids)
            elif filter:
                qdrant_filter = self._build_filter(filter)
                if qdrant_filter is None:
                    raise ValueError("filter must be non-empty when ids is None")
                points_selector = FilterSelector(filter=qdrant_filter)
            else:
                raise ValueError(
                    "Either 'ids' or 'filter' must be provided to delete texts."
                )
            self._safe_delete(collection_name=collection_name, points_selector=points_selector)
        except Exception as e:
            raise QdrantException(Status.VECTOR_STORE_ERROR, str(e))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=5),
        retry=retry_if_exception_type((socket.error, ConnectionError, ConnectTimeout, Exception))
    )
    def _safe_search(self, collection_name: str, query_vector, query_filter=None, search_params=None, limit: int = 3, **kwargs):
        # qdrant-client >= 1.10 removed ``search`` in favor of ``query_points``,
        # which returns a ``QueryResponse`` wrapping a list of ``ScoredPoint``.
        response = self._client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=query_filter,
            search_params=search_params,
            limit=limit,
            **kwargs
        )
        return response.points

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

        qdrant_filter = self._build_filter(filter)

        search_params = SearchParams(
            hnsw_ef=kwargs.get("hnsw_ef", 128),
            exact=kwargs.get("exact", False),
            indexed_only=kwargs.get("indexed_only", False),
        )

        results = self._safe_search(
            collection_name=collection_name,
            query_vector=embedding,
            query_filter=qdrant_filter,
            search_params=search_params,
            limit=limit,
        )

        formatted_results = []
        for result in results:
            payload = result.payload
            item = {
                "id": result.id,
                "text": payload.get("text"),
                "metadata": {k: v for k, v in payload.items() if k != "text"},
                "score": result.score,
            }
            formatted_results.append(item)

        return formatted_results

    def query_by_vector(
        self,
        collection_name: str,
        vector: List[float],
        limit: int = 10,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Search by a pre-computed vector; return flat-payload dicts with score."""
        qdrant_filter = self._build_filter(filter)
        search_params = SearchParams(
            hnsw_ef=kwargs.get("hnsw_ef", 128),
            exact=kwargs.get("exact", False),
            indexed_only=kwargs.get("indexed_only", False),
        )
        results = self._safe_search(
            collection_name=collection_name,
            query_vector=vector,
            query_filter=qdrant_filter,
            search_params=search_params,
            limit=limit,
        )
        out: List[Dict[str, Any]] = []
        for r in results:
            payload = dict(r.payload or {})
            payload["id"] = r.id
            payload["score"] = r.score
            out.append(payload)
        return out
