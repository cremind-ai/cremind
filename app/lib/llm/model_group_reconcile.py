"""Model-group reconciliation on provider auth-method changes.

Switching a provider's active auth method can change which models it can serve
(most notably OpenAI's "Sign in with ChatGPT" Codex backend, which serves a
different, restricted model set from the API-key path). A ``model_group.*``
assignment left pointing at a model the new method can't serve would silently
4xx at request time — the failure that made ``documentation_search``'s relevance
judge (``low`` group) return "no relevant result found".

This helper clears such stale assignments so they transparently fall back to the
``high`` group. It is called from every path that changes an auth method: the
Settings provider PATCH (``app/api/llm.py``) and the Codex OAuth sign-in
completion (``app/api/llm_codex_flow.py``).

It lives under ``app.lib.llm`` (not ``app.api``) so both API modules can import
it without an ``api ← lib`` cycle; it depends only on ``app.config``.
"""

from __future__ import annotations

from app.config import models_for_auth_method, resolve_catalog

# The five configurable model groups (mirrors app/api/llm.py and model_groups.py).
_GROUPS = ("high", "vision", "audio", "low", "plan")


def reconcile_model_groups_for_auth(
    config_storage,
    provider_name: str,
    auth_method: str | None,
    profile: str | None = None,
) -> list[str]:
    """Delete any ``model_group.{group}`` pointing at a ``<provider>/<model>`` the
    given ``auth_method`` can't serve; return the cleared group names.

    Only groups whose stored value belongs to ``provider_name`` are considered, so
    switching one provider's auth never disturbs a group assigned to another. A
    cleared group falls back to ``high`` at resolution time. Uses
    ``resolve_catalog`` so ``custom:`` providers work unchanged (their models
    declare no ``auth_methods`` → nothing is cleared).
    """
    catalog = resolve_catalog(provider_name, profile)
    visible_ids = {
        m.get("id")
        for m in models_for_auth_method(catalog, auth_method)
        if isinstance(m, dict)
    }
    prefix = f"{provider_name}/"
    _kw = {"profile": profile} if profile is not None else {}
    cleared: list[str] = []
    for group in _GROUPS:
        gv = config_storage.get("llm_config", f"model_group.{group}", **_kw)
        if gv and gv.startswith(prefix) and gv[len(prefix):] not in visible_ids:
            config_storage.delete("llm_config", f"model_group.{group}", **_kw)
            cleared.append(group)
    return cleared
