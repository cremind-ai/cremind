import importlib

from app.config.settings import BaseConfig
from app.utils.logger import logger

from .base import EmbeddingProvider

_PROVIDER_REGISTRY = {
    "gemma": "app.embeddings.gemma.GemmaEmbeddingProvider",
    "me5": "app.embeddings.me5.Me5EmbeddingProvider",
}


def create_embedding_provider() -> EmbeddingProvider:
    provider_key = (BaseConfig.get_embedding_provider() or "me5").lower()

    if provider_key not in _PROVIDER_REGISTRY:
        raise ValueError(
            f"Unknown embedding provider: '{provider_key}'. "
            f"Available: {list(_PROVIDER_REGISTRY.keys())}"
        )

    module_path, class_name = _PROVIDER_REGISTRY[provider_key].rsplit(".", 1)
    module = importlib.import_module(module_path)
    provider_class = getattr(module, class_name)

    logger.info(f"Creating embedding provider: {provider_key}")
    return provider_class()


__all__ = ["EmbeddingProvider", "create_embedding_provider"]
