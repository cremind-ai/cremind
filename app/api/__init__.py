from starlette.routing import Route

from app.api.admin_stream import get_admin_stream_routes
from app.api.agents import get_agent_routes
from app.api.calendar import get_calendar_routes
from app.api.channels import get_channel_routes
from app.api.conversations import get_conversation_routes
from app.api.events import get_event_routes
from app.api.file_watchers import get_file_watcher_routes
from app.api.files import get_file_routes
from app.api.oauth2 import get_oauth2_routes
from app.api.processes import get_process_routes
from app.api.profiles import get_profile_routes
from app.api.embedding_stream import get_embedding_stream_routes
from app.api.logs_stream import get_logs_stream_routes
from app.api.profile_events import get_profile_events_routes
from app.api.settings_stream import get_settings_stream_routes
from app.api.system_vars import get_system_vars_routes
from app.api.tokens import get_token_routes
from app.api.user_config import get_user_config_routes


def get_api_routes(
    *,
    registry,
    pending_return_urls,
    mcp_llm_factory=None,
    conversation_storage,
    config_storage,
    connect_persisted_tool=None,
    drop_profile_embeddings=None,
    agent_executor=None,
) -> list[Route]:
    """Collect the post-storage API routes.

    Returned routes assume storage is initialized and the agent has been
    built. Pre-storage routes (``/version``, ``/health``, ``/api/upgrade``,
    ``/api/llm/*``, ``/api/tools/*``, and the wizard's ``/api/config/*``
    endpoints) are registered separately in :func:`app.server.main` so the
    Setup Wizard can run before any DB file exists. The LLM and Tools
    routes resolve ``state.config_storage`` / ``state.registry`` lazily so
    they work both pre- and post-storage from the same registration.

    ``connect_persisted_tool`` is an async callable ``(tool_id) -> (bool, err)``
    used by enable-toggle endpoints to lazily connect a stub MCP/A2A tool
    when a profile turns it on.
    """
    routes = []
    routes.extend(get_agent_routes(
        registry=registry,
        pending_return_urls=pending_return_urls,
        mcp_llm_factory=mcp_llm_factory,
        config_storage=config_storage,
        connect_persisted_tool=connect_persisted_tool,
    ))
    routes.extend(get_oauth2_routes(
        registry=registry, pending_return_urls=pending_return_urls,
    ))
    routes.extend(get_profile_routes(
        conversation_storage,
        registry=registry,
        drop_profile_embeddings=drop_profile_embeddings,
    ))
    routes.extend(get_token_routes())
    routes.extend(get_system_vars_routes())
    routes.extend(get_file_routes())
    routes.extend(get_conversation_routes(
        conversation_storage, agent_executor=agent_executor,
    ))
    routes.extend(get_channel_routes(conversation_storage))
    routes.extend(get_user_config_routes(config_storage))
    routes.extend(get_process_routes())
    routes.extend(get_event_routes())
    routes.extend(get_file_watcher_routes())
    routes.extend(get_calendar_routes(conversation_storage))
    routes.extend(get_admin_stream_routes())
    routes.extend(get_settings_stream_routes())
    routes.extend(get_embedding_stream_routes())
    routes.extend(get_logs_stream_routes())
    routes.extend(get_profile_events_routes(conversation_storage))
    return routes
