"""Top-level Cremind agent.

Wraps the registry-driven :class:`ReasoningAgent` for each profile and manages
the per-profile single configured model. Embeddings (when enabled) are used by
long-term memory search, Google Places type filtering, and document search; the
agent itself no longer keeps per-profile tool-card embeddings (those existed only
for the removed Automatic Skill Mode).
"""

from __future__ import annotations

from typing import Any, AsyncGenerator, List, TYPE_CHECKING

from app.agent.reasoning_agent import ReasoningAgent
from app.lib.embedding import LocalEmbeddings
from app.lib.llm import LLMProvider
from app.lib.llm.model_groups import ModelGroupManager
from app.storage import get_dynamic_config_storage
from app.storage.dynamic_config_storage import DynamicConfigStorage
from app.tools import ToolRegistry
from app.types import ReasoningStreamResponseType
from app.utils import logger

if TYPE_CHECKING:
    from app.vectorstores import VectorStore


def _memory_collection(profile: str) -> str:
    """Per-profile long-term memory collection (mirrors memory_vectorstore)."""
    return f"long_term_memory_{profile}"


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
        # (provider, model, reasoning effort) each cached runner was built for,
        # so a Settings change swaps it out on the next turn.
        self._runner_keys: dict[str, tuple] = {}
        if runner is not None:
            self._runners["admin"] = runner
        self.registry = registry
        self.embedding = embedding
        self._model_group_mgr = model_group_mgr
        self._config_storage = config_storage
        self.vector_store = vector_store
        self._conversation_storage = conversation_storage

    # ── runners ─────────────────────────────────────────────────────────

    def _ensure_setup(self, profile: str) -> ModelGroupManager:
        """Lazily create the model-group manager and assert setup is complete.

        Shared guard for both the main-model and plan-model resolution paths so
        they surface the same "run the setup wizard first" error.
        """
        if self._model_group_mgr is None:
            self._model_group_mgr = ModelGroupManager(get_dynamic_config_storage())
        if not self._model_group_mgr.config_storage.is_setup_complete():
            raise ValueError("LLM provider is not configured. Run the setup wizard first.")
        return self._model_group_mgr

    def _ensure_runner(self, profile: str) -> LLMProvider:
        """The LLM provider for ``profile``, reused while its config holds.

        Reuse matters beyond saving an object. A provider instance carries
        per-connection state that is meant to span a conversation — most visibly
        the Codex path's prompt-cache session id, which is generated in its
        constructor. Rebuilding the provider for every turn handed the API a
        brand-new cache key each time, so a conversation never read back a
        single cached token no matter how stable its prompt was.

        The cache is keyed on what the config resolves to, so changing the model
        or provider in Settings still takes effect on the next turn without any
        explicit invalidation.
        """
        mgr = self._ensure_setup(profile)
        try:
            provider_name, model_name = mgr.get_provider_and_model(
                "high", profile=profile,
            )
            effort = mgr._get_group_reasoning_effort("high", profile=profile)
            fingerprint = (provider_name, model_name, effort)
        except Exception:  # noqa: BLE001
            # Unreadable config: fall back to today's behaviour (build one) so a
            # turn still runs, and do not cache what we cannot key.
            return mgr.create_llm_for_model(profile=profile)

        cached = self._runners.get(profile)
        if cached is not None and self._runner_keys.get(profile) == fingerprint:
            return cached
        runner = mgr.create_llm_for_model(profile=profile)
        self._runners[profile] = runner
        self._runner_keys[profile] = fingerprint
        return runner

    def plan_llm(self, profile: str) -> LLMProvider:
        """Return the profile's plan-mode LLM (the ``plan`` model group).

        Used during plan mode's planning phase — research, clarifying questions,
        writing the plan for approval, and after a cancel. Resolves the optional
        ``plan`` group, transparently falling back to the single configured model
        when the user hasn't picked a dedicated planning model. Deliberately does
        *not* write ``self._runners`` so it can't leak into the main reasoning /
        execution path (which uses :meth:`_ensure_runner`).
        """
        mgr = self._ensure_setup(profile)
        return mgr.create_llm_for_group("plan", profile=profile)

    def auxiliary_llm(self, profile: str) -> LLMProvider:
        """Return the profile's LLM for auxiliary calls (compaction, etc.).

        This is the same single configured model as the main reasoning runner;
        kept as a separate accessor so auxiliary call sites read clearly. Use
        :meth:`low_performance_llm` instead for cheap, throw-away classification.
        """
        if self._model_group_mgr is None:
            self._model_group_mgr = ModelGroupManager(get_dynamic_config_storage())
        return self._model_group_mgr.create_llm_for_model(profile=profile)

    def low_performance_llm(self, profile: str) -> LLMProvider:
        """Return the profile's low-performance (cheap) LLM.

        Resolves the optional ``low`` model group, transparently falling back to
        the single configured model when the user hasn't picked a cheaper one.
        The reusable entry point for lightweight auxiliary work — e.g. the
        skill-event matching gate — where a small/economical model is preferred
        and no system prompt, tools, or chat history are involved.
        """
        if self._model_group_mgr is None:
            self._model_group_mgr = ModelGroupManager(get_dynamic_config_storage())
        return self._model_group_mgr.create_llm_for_group("low", profile=profile)

    # ── profile lifecycle ───────────────────────────────────────────────

    def drop_profile_embeddings(self, profile: str) -> None:
        """Drop a deleted profile's long-term memory vector collection."""
        if self.vector_store is None:
            return
        try:
            from app.vectorstores.qdrant import QdrantClient as QdrantVectorClient

            client: QdrantVectorClient = self.vector_store._client  # type: ignore[assignment]
            collection = _memory_collection(profile)
            if client.collection_exists(collection):
                client.delete_collection(collection)
                logger.info(f"Dropped memory collection '{collection}'")
        except Exception:  # noqa: BLE001
            logger.exception(
                f"Failed to drop memory collection for profile '{profile}'"
            )

    # ── run ─────────────────────────────────────────────────────────────

    async def run(
        self,
        query: str,
        task_history: List[Any],
        context_id: str | None,
        profile: str,
        reasoning: bool = True,
        triggered_by_event: bool = False,
        event_run: bool = False,
        mode: str = "reasoning",
        plan_phase: str | None = None,
        message_origin: dict | None = None,
        task_chain_depth: int = 0,
        maintenance: bool = False,
    ) -> AsyncGenerator[ReasoningStreamResponseType, None]:
        logger.debug(f"Running CremindAgent with query: {query} and profile: {profile}")

        # Block runs while the vector embedding subsystem is still loading. If
        # the user enabled embedding in the wizard, the model load is in flight
        # and the agent must not respond until it's ready — otherwise
        # embedding-dependent tools (search_memory, Google Places filter, doc
        # search) silently degrade.
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

        # Plan mode's planning phase runs on the dedicated Plan Model; once the
        # user accepts the plan (plan_phase == "execute") we switch to the single
        # configured model for the longer execution phase. A cancel keeps the run
        # in the "planning" phase, so it correctly stays on the Plan Model. Every
        # other mode (reasoning/instant, events) uses the single model as before.
        # The Plan Model falls back to the single model when the user hasn't
        # configured one, so this is a no-op for installs that don't use it.
        if mode == "plan" and plan_phase != "execute":
            runner = self.plan_llm(profile)
        else:
            runner = self._ensure_runner(profile)
        reasoning_agent = ReasoningAgent(
            llm=runner,
            registry=self.registry,
            profile=profile,
            context_id=context_id,
            reasoning=reasoning,
            triggered_by_event=triggered_by_event,
            event_run=event_run,
            mode=mode,
            plan_phase=plan_phase,
            # Web UI vs which channel + which sender. Callers that don't know
            # (e.g. the A2A executor) pass nothing and the prompt is unchanged.
            message_origin=message_origin,
            # How many event tasks this flow already chained — caps runaway
            # wait-continue-wait loops at dispatch time.
            task_chain_depth=task_chain_depth,
            # A nested turn the system runs on the conversation's behalf rather
            # than a turn of the conversation itself (the compaction fold). Every
            # other identity signal is absent on such a run, so it has to be said
            # explicitly or the run passes for an ordinary chat turn.
            maintenance=maintenance,
        )

        async for result in reasoning_agent.run(query, task_history):
            yield result
