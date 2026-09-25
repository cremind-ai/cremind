from abc import ABC, abstractmethod
from typing import List


class EmbeddingProvider(ABC):
    # Short, stable id of the provider ("me5", "gemma"), the same key
    # ``[embedding] provider`` uses. Read off the instance rather than the
    # config so a model key can never name a provider other than the one
    # actually loaded (the config can change while a model is in memory).
    PROVIDER_KEY: str = ""

    # The prompts the model was trained with: e5 expects "query: "/"passage: ",
    # EmbeddingGemma its task prompts. Without them retrieval quality drops.
    #
    # They are used ONLY by the User Document Search path
    # (``LocalEmbeddings.embed_passages`` / ``embed_search_query``). Every
    # collection that existed before it (documentation, memory, tool tables)
    # was built by ``encode()`` with no prefix, and a query embedded with a
    # prefix would not be comparable with those vectors, so ``encode()`` must
    # stay prefix-free. The userdocs collections record which prompt scheme
    # built them in their name (``app.userdocs.embed_prompts.PROMPT_SCHEME``).
    QUERY_PREFIX: str = ""
    PASSAGE_PREFIX: str = ""

    @abstractmethod
    def encode(self, text: str) -> List[float]:
        ...

    def encode_batch(
        self, texts: List[str], batch_size: int = 32, *, prefix: str = "",
    ) -> List[List[float]]:
        """Embed many texts, each with ``prefix`` prepended.

        The default loops over :meth:`encode` so a provider without a native
        batch path still works; sentence-transformers providers override it,
        because one padded forward pass is several times faster than N.
        """
        return [self.encode(prefix + t) for t in texts]

    @property
    @abstractmethod
    def dimension(self) -> int:
        ...

    @property
    @abstractmethod
    def model_name(self) -> str:
        ...
