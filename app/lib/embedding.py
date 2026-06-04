"""In-process embedding wrapper.

Replaces the gRPC client that previously talked to the standalone
``embedding_grpc`` service. Loads a sentence-transformers model directly
into the Cremind process and exposes the same ``embed_documents`` /
``embed_query`` interface so all existing call sites continue to work.

Satisfies the ``EmbeddingProvider`` protocol declared in
``app/vectorstores/base.py``.
"""

from typing import List

from app.embeddings import create_embedding_provider


class LocalEmbeddings:
    def __init__(self):
        self._provider = create_embedding_provider()

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._provider.encode(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._provider.encode(text)
