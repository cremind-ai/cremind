from typing import Any, Dict, List, Optional

from app.utils.logger import logger


class ReasoningContextStore:
    """Centralized storage for reasoning agent contexts."""

    _instance: Optional["ReasoningContextStore"] = None
    _storage: Dict[str, Dict[str, Any]]

    def __new__(cls) -> "ReasoningContextStore":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._storage = {}
        return cls._instance

    def get_context(self, context_id: str) -> Optional[Dict[str, Any]]:
        return self._storage.get(context_id)

    def save_context(
            self,
            context_id: str,
            steps: List[str],
            step_count: int) -> None:
        self._storage[context_id] = {
            'steps': steps.copy(),
            'step_count': step_count,
        }
        logger.info(
            f"Saved context for context_id: {context_id} with {len(steps)} total steps")

    def clear_context(self, context_id: str) -> None:
        if context_id in self._storage:
            del self._storage[context_id]
            logger.info(f"Cleared context for context_id: {context_id}")

    def clear_all_contexts(self) -> None:
        self._storage.clear()
        logger.info("Cleared all saved contexts")
