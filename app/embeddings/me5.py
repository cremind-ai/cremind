from typing import List

from sentence_transformers import SentenceTransformer

from app.utils.logger import logger

from .base import EmbeddingProvider


class Me5EmbeddingProvider(EmbeddingProvider):
    MODEL_ID = "intfloat/multilingual-e5-base"
    DIMENSION = 768

    def __init__(self):
        logger.info(f"Loading model: {self.MODEL_ID}")
        self._model = SentenceTransformer(self.MODEL_ID)
        logger.info(f"Model loaded: {self.MODEL_ID}")

    def encode(self, text: str) -> List[float]:
        embedding = self._model.encode(text, normalize_embeddings=True)
        return embedding.tolist()

    @property
    def dimension(self) -> int:
        return self.DIMENSION

    @property
    def model_name(self) -> str:
        return self.MODEL_ID
