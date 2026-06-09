"""Vector embedding apply/rebuild orchestration.

The setup wizard and the Embedding Settings page both push a config
payload through this module. It persists the change, reloads the model
and vector store if needed, drops every collection that the new store
doesn't yet contain, and rebuilds the three known caches:

- ``gg_places_types``         — Google Places type embeddings (336 entries)
- ``tool_embeddings_<profile>`` — per-profile skill/tool embeddings
- ``documentation_search``    — shared + per-profile ``.md`` docs

A rebuild always runs after the new model/store come up because the
caller's intent is "re-sync everything"; partial rebuilds invite subtle
"why does my Google Places search behave like the old model" bugs.

The single entry point is :func:`apply_embedding_config`, which is
designed to run inside an executor thread (it is synchronous and
blocking — sentence-transformers loads can take 30+ seconds).
"""

from __future__ import annotations

from typing import Any, Optional

from app.config.embedding_state import EmbeddingStatus, embedding_state
from app.utils.logger import logger


# Collections we own and may rebuild. Other collections (created by
# user-installed tools) are left untouched on principle.
_OWNED_COLLECTION_PREFIXES = ("tool_embeddings_",)
_OWNED_COLLECTIONS_EXACT = ("gg_places_types", "documentation_search")


def persist_embedding_config(body: dict, config_storage) -> None:
    """Validate ``body`` (wizard / settings shape) and write it to SQLite.

    Raises :class:`ValueError` on a validation failure (gemma without
    HF_TOKEN, unknown provider, etc.) so the caller can return a 400.
    Persists to ``server_config``. Does NOT touch the runtime — callers
    that want the change to take effect must run
    :func:`apply_embedding_config` after.
    """
    enabled = bool(body.get("enabled", False))
    config_storage.set(
        "server_config", "embedding.enabled", "true" if enabled else "false",
    )

    if not enabled:
        # Disabled — leave the rest of the keys alone so re-enabling later
        # picks up the previously-saved choice without forcing the user
        # to retype it.
        return

    provider = (body.get("provider") or "me5").lower()
    if provider not in ("me5", "gemma"):
        raise ValueError(
            f"Unknown embedding provider: '{provider}'. Must be 'me5' or 'gemma'."
        )
    hf_token = (body.get("hf_token") or "").strip()
    if provider == "gemma" and not hf_token:
        raise ValueError("HF_TOKEN is required when embedding provider is 'gemma'.")

    config_storage.set("server_config", "embedding.provider", provider)
    if hf_token:
        config_storage.set(
            "server_config", "embedding.hf_token", hf_token, is_secret=True,
        )

    vectorstore = body.get("vectorstore") or {}
    vs_provider = (vectorstore.get("provider") or "qdrant").lower()
    if vs_provider not in ("qdrant", "chroma"):
        raise ValueError(
            f"Unknown vector store provider: '{vs_provider}'. Must be 'qdrant' or 'chroma'."
        )
    config_storage.set("server_config", "vectorstore.provider", vs_provider)

    # ``deployment_mode`` is the Setup Wizard's choice (docker / native /
    # external). It's stamped at the vectorstore top level by
    # ``_resolve_vectorstore`` after provisioning, and mirrored under the
    # provider block so a reconfigure can roundtrip the value without
    # caring about provider layout. Default to external for older
    # payloads that don't carry the field.
    provider_block = vectorstore.get(vs_provider) or {}
    deployment_mode = (
        vectorstore.get("deployment_mode")
        or provider_block.get("deployment_mode")
        or "external"
    ).lower()
    if deployment_mode not in ("docker", "native", "external"):
        raise ValueError(
            f"Unknown deployment_mode: '{deployment_mode}'. "
            "Must be 'docker', 'native', or 'external'."
        )

    # Seam C — Kubernetes enforcement. In a pod the only safe vector store is a
    # shared in-cluster service reached over HTTP (External). 'native' maps to
    # an in-process *persistent* local file and 'docker' to a sidecar over the
    # docker socket — both are pod-local and break horizontal scaling. The
    # wizard's mode-rule already hides them, but this is reached by the
    # unauthenticated first-setup endpoint and the Settings page, so reject a
    # hand-crafted submission too. Only runs when embeddings are enabled (the
    # function early-returns above when disabled), so the default is unaffected.
    from app.config.install_catalog import is_kubernetes_mode
    if is_kubernetes_mode() and deployment_mode != "external":
        raise ValueError(
            "In Kubernetes mode only external (in-cluster service) vector "
            f"stores are supported; got deployment_mode='{deployment_mode}'. "
            "Persistent local storage and Docker sidecars are not available "
            "in a pod."
        )

    config_storage.set("server_config", "vectorstore.deployment_mode", deployment_mode)

    if vs_provider == "qdrant":
        # Qdrant has no Native mode in phase 1; both Docker and External
        # use HTTP, so the persisted shape is the same in both cases.
        qd = provider_block
        if "host" in qd:
            config_storage.set("server_config", "qdrant.host", str(qd["host"]))
        if "port" in qd:
            config_storage.set("server_config", "qdrant.port", str(qd["port"]))
        if qd.get("api_key"):
            config_storage.set(
                "server_config", "qdrant.api_key", str(qd["api_key"]), is_secret=True,
            )
        if "https" in qd:
            config_storage.set(
                "server_config", "qdrant.https",
                "true" if qd["https"] else "false",
            )
    else:  # chroma
        ch = provider_block
        # The chromadb adapter still keys off ``chroma.mode`` (http vs
        # persistent); translate the deployment mode so we don't have to
        # touch the adapter. Native → persistent (in-process library);
        # Docker / External → http (the adapter dials host:port).
        chroma_mode = "persistent" if deployment_mode == "native" else "http"
        config_storage.set("server_config", "chroma.mode", chroma_mode)
        if chroma_mode == "http":
            if "host" in ch:
                config_storage.set("server_config", "chroma.host", str(ch["host"]))
            if "port" in ch:
                config_storage.set("server_config", "chroma.port", str(ch["port"]))
            if "ssl" in ch:
                config_storage.set(
                    "server_config", "chroma.ssl",
                    "true" if ch["ssl"] else "false",
                )
            if ch.get("api_key"):
                config_storage.set(
                    "server_config", "chroma.api_key", str(ch["api_key"]), is_secret=True,
                )
        else:  # persistent (Native deployment mode)
            if ch.get("persist_path"):
                config_storage.set(
                    "server_config", "chroma.persist_path", str(ch["persist_path"]),
                )


def read_embedding_config(config_storage) -> dict:
    """Read the persisted embedding config back out as a wizard-shape dict.

    Surfaces ``deployment_mode`` both at the vectorstore top level and
    inside the active provider block — the wizard reads either, and
    keeping both makes the API symmetric with what
    :func:`persist_embedding_config` writes. For old configs that
    pre-date the deployment-mode work, we infer the mode from the
    legacy ``chroma.mode`` field (persistent → native; http → external)
    so the wizard form mounts with a sensible default.
    """
    from app.config.settings import BaseConfig

    provider = BaseConfig.get_vectorstore_provider()
    deployment_mode = config_storage.get("server_config", "vectorstore.deployment_mode")
    if not deployment_mode:
        # Legacy roundtrip: pick the closest match from the old schema.
        if provider == "chroma":
            deployment_mode = "native" if BaseConfig.get_chroma_mode() == "persistent" else "external"
        else:
            deployment_mode = "external"

    return {
        "enabled": BaseConfig.is_embedding_enabled(),
        "provider": BaseConfig.get_embedding_provider(),
        "hf_token": BaseConfig.get_hf_token(),
        "vectorstore": {
            "provider": provider,
            "deployment_mode": deployment_mode,
            "qdrant": {
                "deployment_mode": deployment_mode if provider == "qdrant" else "external",
                "host": BaseConfig.get_qdrant_host(),
                "port": BaseConfig.get_qdrant_port(),
                "api_key": BaseConfig.get_qdrant_api_key(),
                "https": BaseConfig.get_qdrant_https(),
            },
            "chroma": {
                "deployment_mode": deployment_mode if provider == "chroma" else "native",
                "host": BaseConfig.get_chroma_host(),
                "port": BaseConfig.get_chroma_port(),
                "ssl": BaseConfig.get_chroma_ssl(),
                "api_key": BaseConfig.get_chroma_api_key(),
                "persist_path": BaseConfig.get_chroma_persist_path() or "",
            },
        },
    }


def _drop_owned_collections(vector_store) -> None:
    """Best-effort drop every Cremind-owned collection in the live store.

    Called when re-applying config — the new store may be a fresh empty
    one or a previously-used one with stale data. Either way we want a
    clean slate so the rebuild produces a known-good state.
    """
    if vector_store is None:
        return
    client = vector_store._client
    try:
        names = client.list_collections()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to list collections during rebuild prep")
        return

    for name in names:
        owned = (
            name in _OWNED_COLLECTIONS_EXACT
            or any(name.startswith(p) for p in _OWNED_COLLECTION_PREFIXES)
        )
        if not owned:
            continue
        try:
            client.delete_collection(name)
            logger.info(f"[embedding] dropped collection '{name}' for rebuild")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[embedding] failed to drop collection '{name}' during rebuild prep: {e}")


def _rebuild_caches(*, agent, embedding, vector_store, profiles: list[str]) -> None:
    """Re-populate every owned collection from authoritative sources.

    ``profiles`` is enumerated by the caller because conversation storage
    is async and this function runs on a worker thread. Order:

    - ``gg_places._build_embedding_table`` rebuilds the place-type cache
      eagerly so the first place search isn't slow.
    - ``CremindAgent.embedding_table_for(profile)`` lazy-builds each
      profile's tool embedding collection (cache was cleared on rewire).
    - ``DocumentSyncService.full_reconcile`` re-emits doc embeddings for
      the shared scope and each profile.
    """
    embedding_state.set_phase("rebuilding_places")
    try:
        from app.tools.builtin import gg_places  # noqa: WPS433 — lazy by design
        gg_places._build_embedding_table(vector_store=vector_store)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[embedding] gg_places rebuild failed; continuing: {e}")

    embedding_state.set_phase("rebuilding_tools")
    for profile in profiles:
        try:
            agent.embedding_table_for(profile)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[embedding] tool embeddings rebuild failed for profile '{profile}'; continuing: {e}")

    embedding_state.set_phase("rebuilding_docs")
    try:
        from app.documents import get_service
        service = get_service()
        if service is not None and vector_store is not None:
            service._vector_store = vector_store
            service._embedding = embedding
            # Force re-creation by clearing the "collection ready" flag —
            # the new store has no collections yet.
            service._collection_ready = False
            from app.documents.sync import SHARED_SCOPE
            service.full_reconcile(SHARED_SCOPE)
            for profile in profiles:
                service.full_reconcile(profile)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[embedding] documentation collection reconcile failed; continuing: {e}")

    embedding_state.set_phase(None)


def _rewire_agent(agent, embedding, vector_store) -> None:
    if agent is None:
        return
    agent.embedding = embedding
    agent.vector_store = vector_store
    # Drop any cached empty tables built when embedding was disabled.
    try:
        agent._embedding_tables.clear()
    except Exception:  # noqa: BLE001
        pass


def apply_embedding_config(
    *,
    profiles: Optional[list[str]] = None,
    force_rebuild: bool = True,
) -> None:
    """Reload the embedding subsystem from current SQLite config.

    Runs synchronously on a worker thread. The caller — typically
    :func:`apply_embedding_config_in_background` — must have already
    claimed the busy slot via ``embedding_state.mark_initializing()``
    when embedding is enabled, so the HTTP response that triggered the
    apply carries the visible busy state.

    Sequence:
      1. Read ``BaseConfig.is_embedding_enabled()``.
      2. If disabled: rewire agent to ``embedding=None, vector_store=None``;
         mark DISABLED. Existing on-disk vectors are NOT deleted (the user
         can re-enable later and reuse them).
      3. If enabled: instantiate ``LocalEmbeddings()`` and the configured
         vector store. On any failure mark FAILED and surface the error.
      4. Rewire the live ``CremindAgent`` so the new instances take effect.
      5. If ``force_rebuild`` (default): drop every owned collection in
         the new store and rebuild from authoritative sources.
      6. Mark READY.

    Raises only on programmer errors. All operational failures are
    captured into the global state so the UI can show them.
    """
    from app.config.settings import BaseConfig
    from app.lib.embedding import LocalEmbeddings
    from app.vectorstores import VectorStore, create_vector_store_client
    from app.events import runner as event_runner

    agent = event_runner.get_cremind_agent()

    # Disabled path
    if not BaseConfig.is_embedding_enabled():
        embedding_state.mark_disabled()
        _rewire_agent(agent, None, None)
        logger.info("[embedding] applied: disabled")
        return

    # Enabled path — slot was already claimed in
    # apply_embedding_config_in_background (synchronously, before the
    # response went out). Defensive re-claim only if a direct caller
    # forgot to set up the slot.
    if not embedding_state.is_busy():
        if not embedding_state.mark_initializing():
            raise RuntimeError(
                "Embedding apply is already in progress; refusing concurrent run."
            )

    try:
        embedding_state.set_phase("loading_model")
        embedding = LocalEmbeddings()
    except Exception as e:  # noqa: BLE001
        logger.error(f"[embedding] failed to load embedding model: {e}")
        embedding_state.mark_failed(f"embedding model load failed: {e}")
        _rewire_agent(agent, None, None)
        return

    try:
        embedding_state.set_phase("connecting_store")
        vector_store = VectorStore(client=create_vector_store_client())
    except Exception as e:  # noqa: BLE001
        logger.error(f"[embedding] failed to connect vector store: {e}")
        embedding_state.mark_failed(f"vector store connection failed: {e}")
        _rewire_agent(agent, None, None)
        return

    _rewire_agent(agent, embedding, vector_store)

    if force_rebuild:
        embedding_state.transition_to_rebuilding(phase="preparing_rebuild")
        try:
            _drop_owned_collections(vector_store)
            _rebuild_caches(
                agent=agent,
                embedding=embedding,
                vector_store=vector_store,
                profiles=profiles or [],
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("Embedding rebuild failed")
            embedding_state.mark_failed(f"rebuild failed: {e}")
            return

    embedding_state.mark_ready(embedding, vector_store)
    logger.info("[embedding] applied: ready")


def apply_embedding_config_in_background(
    *,
    profiles: Optional[list[str]] = None,
    force_rebuild: bool = True,
) -> bool:
    """Claim the busy slot synchronously, then schedule the apply on
    the executor.

    The synchronous claim is critical: ``run_in_executor`` returns
    before the worker thread has had a chance to enter
    :func:`apply_embedding_config`, so without claiming up-front the
    HTTP response races the worker — the response would carry the
    previous (often READY) state and the UI would think the apply
    finished instantly.

    Returns ``True`` if the apply was scheduled, ``False`` if a busy
    operation is already in flight (caller can treat as a benign no-op
    and let the existing run finish).
    """
    import asyncio

    from app.config.settings import BaseConfig

    # Claim before scheduling. Disabled path doesn't need a slot — the
    # apply just flips to DISABLED, which is fast.
    if BaseConfig.is_embedding_enabled():
        if not embedding_state.mark_initializing():
            return False

    loop = asyncio.get_running_loop()
    try:
        loop.run_in_executor(
            None,
            lambda: apply_embedding_config(
                profiles=profiles, force_rebuild=force_rebuild,
            ),
        )
    except Exception as e:  # noqa: BLE001 — scheduler failures
        embedding_state.mark_failed(f"failed to schedule apply: {e}")
        raise

    return True
