from abc import ABC, abstractmethod
from typing import Any, Dict, Iterable, List, Optional, Protocol, TypedDict, Union, runtime_checkable
import uuid


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Protocol for embedding providers.

    Any class implementing embed_documents and embed_query satisfies this
    protocol structurally (e.g. LocalEmbeddings).
    """

    def embed_documents(self, texts: List[str]) -> List[List[float]]: ...
    def embed_query(self, text: str) -> List[float]: ...


class StoredPoint(TypedDict):
    """Provider-neutral shape for a stored point.

    `vector` is None when the caller requested points without vectors.
    `payload` carries arbitrary metadata; cache.py uses `payload["key"]`
    as the logical record id, so providers don't need to round-trip the
    raw `id` field exactly.
    """

    id: Union[int, str]
    vector: Optional[List[float]]
    payload: Dict[str, Any]


class VectorStoreBase(ABC):
    """Interface for vector stores."""

    @abstractmethod
    def create_collection(
        self,
        **kwargs: Any,
    ) -> str:
        """Create a collection."""

    @abstractmethod
    def list_collections(self) -> List[str]:
        """Get collections."""

    @abstractmethod
    def delete_collection(
        self,
        collection_name: str,
        force: bool = False,
        **kwargs: Any,
    ) -> None:
        """Delete a collection."""

    @abstractmethod
    def add_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Add texts to a collection."""

    @abstractmethod
    def get_texts(
        self,
        collection_name: str,
        ids: List[int],
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Get texts from a collection."""

    @abstractmethod
    def update_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Update texts in a collection."""

    @abstractmethod
    def delete_texts(
        self,
        collection_name: str,
        ids: Optional[List[str]] = None,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        """Delete texts from a collection."""

    @abstractmethod
    def query(
        self,
        query_text: str,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        limit: int = 3,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Query text from a collection."""

    @abstractmethod
    def collection_exists(self, collection_name: str) -> bool:
        """Return True if the named collection exists."""

    @abstractmethod
    def create_named_collection(self, collection_name: str, size: int) -> str:
        """Create or recreate a collection with the given name and vector size."""

    @abstractmethod
    def list_all_points(
        self,
        collection_name: str,
        with_vectors: bool = False,
        filter: Optional[dict] = None,
    ) -> List[StoredPoint]:
        """Enumerate points in a collection (id + payload, optionally vectors).

        ``filter`` follows the abstract ``{key: value}`` shape: scalar values
        match by equality, list/tuple values match any element (``IN``-set).
        ``None`` returns every point.
        """

    @abstractmethod
    def query_by_vector(
        self,
        collection_name: str,
        vector: List[float],
        limit: int = 10,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        """Search by a pre-computed embedding (no re-embedding).

        Returns flat-payload dicts with added ``id`` and ``score`` keys:
        ``[{**payload, "id": id, "score": float}, ...]``. ``filter`` values:
        scalar = equality, list/tuple = ``IN``-set match.
        """

    @abstractmethod
    def add_points(
        self,
        collection_name: str,
        points: List[StoredPoint],
    ) -> None:
        """Insert pre-embedded points (skips embedding generation)."""

    def create_collection_uuid(self) -> str:
        """Create a uuid."""
        return str(uuid.uuid4()).replace("-", "")


class VectorStore(VectorStoreBase):
    def __init__(
        self,
        client: VectorStoreBase,
    ):
        self._client = client

    def create_collection(
        self,
        **kwargs: Any,
    ) -> str:
        """Create a collection."""
        return self._client.create_collection(**kwargs)

    def list_collections(
        self,
    ) -> List[str]:
        """List collections."""
        return self._client.list_collections()

    def delete_collection(
        self,
        collection_name: str,
        force: bool = False,
        **kwargs: Any,
    ) -> None:
        """Delete a collection."""
        return self._client.delete_collection(collection_name, force=force, **kwargs)

    def add_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Add texts to a collection."""
        return self._client.add_texts(
            collection_name=collection_name,
            embedding_function=embedding_function,
            texts=texts,
            ids=ids,
            metadatas=metadatas,
            **kwargs,
        )

    def get_texts(
        self,
        collection_name: str,
        ids: List[int],
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Get texts from a collection."""
        return self._client.get_texts(
            collection_name=collection_name,
            filter=filter,
            ids=ids,
            **kwargs,
        )

    def update_texts(
        self,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        texts: Iterable[str],
        ids: List[int],
        metadatas: Optional[List[dict]] = None,
        **kwargs: Any,
    ) -> List[int]:
        """Update texts in a collection."""
        return self._client.update_texts(
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
        ids: Optional[List[str]] = None,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> None:
        """Delete texts from a collection."""
        self._client.delete_texts(
            collection_name=collection_name,
            filter=filter,
            ids=ids,
            **kwargs,
        )

    def query(
        self,
        query_text: str,
        collection_name: str,
        embedding_function: EmbeddingProvider,
        limit: int = 3,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[dict[Any, Any]]:
        """Query text from a collection."""
        return self._client.query(
            query_text=query_text,
            collection_name=collection_name,
            embedding_function=embedding_function,
            limit=limit,
            filter=filter,
            **kwargs,
        )

    def collection_exists(self, collection_name: str) -> bool:
        return self._client.collection_exists(collection_name)

    def create_named_collection(self, collection_name: str, size: int) -> str:
        return self._client.create_named_collection(collection_name, size)

    def list_all_points(
        self,
        collection_name: str,
        with_vectors: bool = False,
        filter: Optional[dict] = None,
    ) -> List[StoredPoint]:
        return self._client.list_all_points(
            collection_name, with_vectors=with_vectors, filter=filter,
        )

    def query_by_vector(
        self,
        collection_name: str,
        vector: List[float],
        limit: int = 10,
        filter: Optional[dict] = None,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        return self._client.query_by_vector(
            collection_name=collection_name,
            vector=vector,
            limit=limit,
            filter=filter,
            **kwargs,
        )

    def add_points(
        self,
        collection_name: str,
        points: List[StoredPoint],
    ) -> None:
        self._client.add_points(collection_name=collection_name, points=points)
