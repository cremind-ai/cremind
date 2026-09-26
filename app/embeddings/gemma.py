from typing import List

from sentence_transformers import SentenceTransformer

from app.config.settings import BaseConfig
from app.utils.logger import logger

from .base import EmbeddingProvider


class GemmaEmbeddingProvider(EmbeddingProvider):
    MODEL_ID = "google/embeddinggemma-300m"
    DIMENSION = 768
    PROVIDER_KEY = "gemma"
    # EmbeddingGemma's retrieval prompts (its model card's "query" and
    # "document" prompts); see EmbeddingProvider for why encode() skips them.
    QUERY_PREFIX = "task: search result | query: "
    PASSAGE_PREFIX = "title: none | text: "

    def __init__(self):
        token = BaseConfig.get_hf_token()
        if not token:
            raise ValueError(
                "Gemma embedding requires HF_TOKEN. Configure it in setup or "
                "set the HF_TOKEN environment variable."
            )
        logger.info(f"Loading model: {self.MODEL_ID}")
        self._model = SentenceTransformer(self.MODEL_ID, token=token)
        logger.info(f"Model loaded: {self.MODEL_ID}")

    def encode(self, text: str) -> List[float]:
        embedding = self._model.encode(text, normalize_embeddings=True)
        return embedding.tolist()

    def encode_batch(
        self, texts: List[str], batch_size: int = 32, *, prefix: str = "",
    ) -> List[List[float]]:
        if not texts:
            return []
        inputs = [prefix + t for t in texts] if prefix else list(texts)
        embeddings = self._model.encode(
            inputs,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return embeddings.tolist()

    @property
    def dimension(self) -> int:
        return self.DIMENSION

    @property
    def model_name(self) -> str:
        return self.MODEL_ID
