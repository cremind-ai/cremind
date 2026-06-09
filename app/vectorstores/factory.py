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
        # Seam E — Kubernetes backstop. Chroma's "persistent" mode is an
        # in-process local file (pod-local) that breaks horizontal scaling.
        # persist_embedding_config (Seam C) rejects it at write time, but a
        # bulk server_config import or CLI write could set chroma.mode directly;
        # this catches it at the point of use, regardless of how it was stored.
        from app.config.install_catalog import is_kubernetes_mode
        if is_kubernetes_mode() and BaseConfig.get_chroma_mode() == "persistent":
            raise ValueError(
                "Persistent (in-process) ChromaDB is not supported in "
                "Kubernetes mode (INSTALL_MODE=kubernetes); pod-local storage "
                "breaks horizontal scaling. Use an external (HTTP) Chroma or "
                "Qdrant service."
            )
        from .chroma import ChromaClient
        return ChromaClient(size=0)
    raise ValueError(f"Unknown vector store provider: {provider!r}")
