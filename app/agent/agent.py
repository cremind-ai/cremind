"""Top-level Cremind agent.

Wraps the registry-driven :class:`ReasoningAgent` for each profile and
manages the per-profile "high" group LLM. Tool-card embeddings are kept
per-profile -- each profile gets its own Qdrant collection
``tool_embeddings_<profile>`` and its own in-memory embedding table -- so
skill and tool visibility never leaks across profiles.

When a Qdrant vector store is available, tool embeddings are persisted so they
survive server restarts without re-calling the gRPC embedding service.
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, Iterable, List, Optional, TYPE_CHECKING

from app.agent.reasoning_agent import ReasoningAgent
from app.lib.embedding import LocalEmbeddings
from app.lib.llm import LLMProvider
from app.lib.llm.model_groups import ModelGroupManager
from app.storage import get_dynamic_config_storage
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.tools import ToolRegistry
from app.tools.base import ToolType
from app.types import EmbeddingTable, ReasoningStreamResponseType, ToolEmbeddingRecord
from app.utils import logger

if TYPE_CHECKING:
    from app.vectorstores import VectorStore


def _tool_embeddings_collection(profile: str) -> str:
    return f"tool_embeddings_{profile}"


def _card_to_embedding_text(card) -> str:
    description = (card.description or "").strip()
    skills = getattr(card, "skills", None) or []
    skill_lines = []
    for skill in skills:
        sk_name = getattr(skill, "name", None) or getattr(skill, "id", None) or ""
        sk_desc = (getattr(skill, "description", None) or "").strip()
        if not sk_name and not sk_desc:
            continue
        skill_lines.append(f"{sk_name}: {sk_desc}" if sk_name else sk_desc)
    if description and skill_lines:
        return description + "\n\n" + "\n".join(skill_lines)
    return description or "\n".join(skill_lines)


class CremindAgent:
    """Per-profile reasoning entry point."""

    def __init__(
        self,
        registry: ToolRegistry,
        embedding: LocalEmbeddings | None,
        runner: LLMProvider | None = None,
        model_group_mgr: ModelGroupManager | None = None,
        config_storage: DynamicConfigStorage | None = None,
        vector_store: VectorStore | None = None,
        conversation_storage: Any | None = None,
    ):
        self._runners: dict[str, LLMProvider] = {}
        if runner is not None:
            self._runners["admin"] = runner
        self.registry = registry
        self.embedding = embedding
        self._model_group_mgr = model_group_mgr
        self._config_storage = config_storage
        self.vector_store = vector_store
        self._conversation_storage = conversation_storage
        self._embedding_tables: dict[str, EmbeddingTable] = {}

        # Refresh embeddings whenever the registry mutates. The callback
        # accepts an optional ``profile`` kwarg so skill-scoped changes only
        # rebuild one table; global changes (builtin/a2a/mcp) rebuild all.
        self.registry.set_change_callback(self.update_embeddings)

    # ── embedding tables ────────────────────────────────────────────────

    def _collect_tool_data(self, profile: str) -> dict[str, ToolEmbeddingRecord]:
        """Extract tool card data for ``profile`` (skip stubs / hidden).

        Records are keyed by ``tool_id`` (stable registry identity) and carry
        filter metadata (``tool_type``, ``name``, ``enabled``) so the vector
        store payload can be queried by type without joining back to the
        registry. Embedding text is the card description plus one line per
        sub-skill — storing the full ``AgentCard`` JSON would bloat the input
        with protocol/transport fields that carry no retrieval signal.
        """
        agent_data: dict[str, ToolEmbeddingRecord] = {}
        for tool in self.registry.tools_for_profile(profile):
            if tool.hidden:
                continue
            if getattr(tool, "is_stub", False):
                continue
            card = tool.get_card()
            if card is None:
                continue
            agent_data[tool.tool_id] = ToolEmbeddingRecord(
                text=_card_to_embedding_text(card),
                tool_id=tool.tool_id,
                name=card.name,
                tool_type=tool.tool_type.value,
                enabled=self.registry.is_enabled_for_profile(tool, profile),
            )
        return agent_data

    def _build_connected_embeddings(self, profile: str) -> Optional[EmbeddingTable]:
        """Build the embedding table for ``profile``.

        Uses the shared cache helper to load from Qdrant when available,
        falling back to in-process embedding generation and persisting for
        next time. Returns ``None`` when embedding is disabled — callers
        treat that as "no similarity ranking available" and fall back to
        showing every tool.
        """
        if self.embedding is None or self.vector_store is None:
            return None

        from app.vectorstores import get_or_build_embedding_table

        agent_data = self._collect_tool_data(profile)
        table = get_or_build_embedding_table(
            vector_store=self.vector_store,
            embedding=self.embedding,
            data=agent_data,
            collection_name=_tool_embeddings_collection(profile),
        )
        logger.debug(f"Tool card embeddings for '{profile}': {table.dataframe}")
        return table

    def embedding_table_for(self, profile: str) -> Optional[EmbeddingTable]:
        """Return the profile's embedding table, building it lazily.

        Returns ``None`` when embedding is disabled.
        """
        if self.embedding is None or self.vector_store is None:
            return None
        table = self._embedding_tables.get(profile)
        if table is None:
            built = self._build_connected_embeddings(profile)
            if built is None:
                return None
            self._embedding_tables[profile] = built
            table = built
        return table

    def update_embeddings(self, profile: str | None = None) -> None:
        """Rebuild the embedding table(s) after tools are added/removed.

        - ``profile`` given → rebuild only that profile's table (skill sync).
        - ``profile is None`` → rebuild every profile we have already touched
          (builtin / a2a / mcp mutation that could affect anyone).

        No-op when embedding is disabled — pandas is in the embeddings
        extras group and importing the cache helpers would crash on a
        thin install.
        """
        if self.embedding is None or self.vector_store is None:
            return

        targets: Iterable[str]
        if profile is not None:
            targets = [profile]
        else:
            targets = list(self._embedding_tables.keys())

        for p in targets:
            built = self._build_connected_embeddings(p)
            if built is None:
                continue
            self._embedding_tables[p] = built
            logger.info(
                f"Updated tool embeddings for '{p}' "
                f"({len(self._embedding_tables[p])} entries)"
            )

    def drop_profile_embeddings(self, profile: str) -> None:
        """Remove a profile's embedding table and its Qdrant collection."""
        self._embedding_tables.pop(profile, None)
        if self.vector_store is None:
            return
        try:
            from app.vectorstores.qdrant import QdrantClient as QdrantVectorClient

            client: QdrantVectorClient = self.vector_store._client  # type: ignore[assignment]
            collection = _tool_embeddings_collection(profile)
            if client.collection_exists(collection):
                client.delete_collection(collection)
                logger.info(f"Dropped embedding collection '{collection}'")
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Failed to drop embedding collection for profile '{profile}'"
            )

    # ── runners ─────────────────────────────────────────────────────────

    def _ensure_runner(self, profile: str) -> LLMProvider:
        """Create (or re-create) the LLM provider for ``profile`` from current config."""
        if self._model_group_mgr is None:
            self._model_group_mgr = ModelGroupManager(get_dynamic_config_storage())

        if not self._model_group_mgr.config_storage.is_setup_complete():
            raise ValueError("LLM provider is not configured. Run the setup wizard first.")

        runner = self._model_group_mgr.create_llm_for_group("high", profile=profile)
        self._runners[profile] = runner
        return runner

    def low_group_llm(self, profile: str) -> LLMProvider:
        """Return the profile's 'low' group LLM provider for cheap auxiliary calls."""
        if self._model_group_mgr is None:
            self._model_group_mgr = ModelGroupManager(get_dynamic_config_storage())
        return self._model_group_mgr.create_llm_for_group("low", profile=profile)

    async def _resolve_allowed_skill_ids(
        self, query: str, profile: str,
    ) -> set[str] | None:
        """Return the skill tool_ids permitted for this run.

        - ``None`` → no filtering (manual mode, or automatic mode with any
          failure that warrants falling back to the full skill list so the
          user isn't silently left with zero skills).
        - non-empty set → restrict skills to this set (automatic mode, vector
          search succeeded).
        """
        storage = self._conversation_storage
        if storage is None:
            return None
        try:
            mode = await storage.get_skill_mode(profile)
        except Exception:  # noqa: BLE001
            logger.exception(f"Failed to read skill_mode for profile '{profile}'")
            return None
        if mode != "automatic":
            return None
        if self.vector_store is None:
            logger.warning(
                f"Skill mode 'automatic' for profile '{profile}' but no vector "
                f"store available; falling back to manual behavior."
            )
            return None
        # Authoritative set of skills the user has enabled for this profile.
        # The Qdrant payload also carries an ``enabled`` flag, but it's a
        # cache; we intersect with the registry to guarantee we never return
        # a skill the user has just disabled.
        enabled_skill_ids = {
            t.tool_id for t in self.registry.tools_for_profile(profile)
            if t.tool_type is ToolType.SKILL
            and self.registry.is_enabled_for_profile(t, profile)
        }
        if not enabled_skill_ids:
            logger.debug(
                f"Automatic skill mode for profile '{profile}': no enabled "
                f"skills; nothing to search."
            )
            return set()
        try:
            results = self.vector_store.query(
                query_text=query,
                collection_name=_tool_embeddings_collection(profile),
                embedding_function=self.embedding,
                limit=10,
                filter={"tool_type": ToolType.SKILL, "enabled": True},
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Vector skill search failed for profile '{profile}'; "
                f"falling back to manual behavior."
            )
            return None
        allowed: set[str] = set()
        for r in results or []:
            metadata = r.get("metadata") or {}
            tool_id = metadata.get("tool_id")
            # Second line of defense: even if the Qdrant ``enabled`` payload
            # is stale, only keep tool_ids the registry currently has enabled.
            if tool_id and tool_id in enabled_skill_ids:
                allowed.add(tool_id)
        if not allowed:
            logger.warning(
                f"Vector skill search returned no enabled matches for profile "
                f"'{profile}'; falling back to manual behavior."
            )
            return None
        logger.debug(
            f"Automatic skill mode for profile '{profile}': selected "
            f"{len(allowed)} skill(s) via vector search: {sorted(allowed)}"
        )
        return allowed

    async def run(
        self,
        query: str,
        task_history: List[Any],
        context_id: str | None,
        profile: str,
        reasoning: bool = True,
        triggered_by_event: bool = False,
        memory_context: str = "",
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        logger.debug(f"Running CremindAgent with query: {query} and profile: {profile}")

        # Block runs while the vector embedding subsystem is still loading.
        # If the user enabled embedding in the wizard, the model load is
        # in flight and the agent must not respond until it's ready —
        # otherwise embedding-dependent features (Automatic Skill Mode,
        # Google Places filter, doc search) silently degrade.
        from app.config.settings import BaseConfig
        from app.config.embedding_state import embedding_state, EmbeddingStatus
        from app.constants import ChatCompletionTypeEnum

        if BaseConfig.is_embedding_enabled() and embedding_state.status is not EmbeddingStatus.READY:
            status = embedding_state.status.value
            err = embedding_state.error
            if status == "initializing":
                msg = (
                    "Vector embedding is still initializing. The agent will be "
                    "ready in a moment — please retry."
                )
            elif status == "failed":
                msg = (
                    "Vector embedding initialization failed: "
                    f"{err or 'unknown error'}. Disable Vector Embedding in "
                    "Settings or check the server logs."
                )
            else:
                msg = (
                    "Vector embedding is enabled but not ready "
                    f"(status: {status}). Please wait or check server logs."
                )
            logger.warning(f"Refusing agent run while embedding {status}: {err}")
            yield {"type": ChatCompletionTypeEnum.DONE, "data": msg}
            return

        runner = self._ensure_runner(profile)
        # Ensure this profile's embedding table is built (lazy).
        self.embedding_table_for(profile)
        allowed_skill_ids = await self._resolve_allowed_skill_ids(query, profile)
        reasoning_agent = ReasoningAgent(
            llm=runner,
            registry=self.registry,
            profile=profile,
            context_id=context_id,
            reasoning=reasoning,
            allowed_skill_ids=allowed_skill_ids,
            triggered_by_event=triggered_by_event,
            memory_context=memory_context,
        )

        async for result in reasoning_agent.run(query, task_history):
            yield result
