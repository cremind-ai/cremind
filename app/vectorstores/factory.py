"""Vector store factory — picks a provider from config and instantiates it."""

from app.config.settings import BaseConfig
from .base import VectorStoreBase


def create_vector_store_client() -> VectorStoreBase:
    """Build the configured vector store client.

    Reads `[vectorstore] provider` (TOML) — defaults to "qdrant" for back-compat.
    Imports are lazy so a misconfigured provider never pulls in unused SDK code.
    """
    provider = BaseConfig.get_vectorstore_provider()
    if provider == "qdrant":
        from .qdrant import QdrantClient
        return QdrantClient(size=0)
    if provider == "chroma":
        from .chroma import ChromaClient
        return ChromaClient(size=0)
    raise ValueError(f"Unknown vector store provider: {provider!r}")
