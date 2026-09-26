from typing import List

from sentence_transformers import SentenceTransformer

from app.utils.logger import logger

from .base import EmbeddingProvider


class Me5EmbeddingProvider(EmbeddingProvider):
    MODEL_ID = "intfloat/multilingual-e5-base"
    DIMENSION = 768
    PROVIDER_KEY = "me5"
    # e5 was trained with these; see EmbeddingProvider for why encode() skips them.
    QUERY_PREFIX = "query: "
    PASSAGE_PREFIX = "passage: "

    def __init__(self):
        logger.info(f"Loading model: {self.MODEL_ID}")
        self._model = SentenceTransformer(self.MODEL_ID)
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
        # A list input returns a 2-D ndarray; the progress bar would write to
        # stderr from a worker thread on every batch.
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
