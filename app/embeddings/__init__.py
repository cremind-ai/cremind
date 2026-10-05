import importlib

from app.config.settings import BaseConfig
from app.utils.logger import logger

from . import vc_runtime
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
    # The provider modules import sentence_transformers, and with it torch,
    # whose Windows DLLs need a recent Visual C++ runtime.
    runtime_status = vc_runtime.prepare()
    try:
        module = importlib.import_module(module_path)
    except OSError as exc:
        hint = vc_runtime.explain_load_error(exc, runtime_status)
        if hint is None:
            raise
        logger.error(f"PyTorch failed to load: {exc}")
        raise RuntimeError(hint) from exc
    provider_class = getattr(module, class_name)

    logger.info(f"Creating embedding provider: {provider_key}")
    return provider_class()


__all__ = ["EmbeddingProvider", "create_embedding_provider"]
