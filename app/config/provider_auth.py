"""Normalize provider auth methods from TOML catalogs.

Handles both the new ``auth_methods`` format and the legacy
``requires_api_key`` / ``config_fields`` format, producing a uniform
list of auth method descriptors for the API and factory layers.
"""


def normalize_provider_auth_methods(provider_info: dict) -> list[dict]:
    """Return a uniform ``auth_methods`` list for a provider.

    If the TOML ``[provider]`` section already contains ``auth_methods``,
    return them as-is.  Otherwise, synthesize a list from the legacy
    ``requires_api_key``, ``requires_service_account``, and
    ``config_fields`` keys.
    """
    if "auth_methods" in provider_info:
        return list(provider_info["auth_methods"])

    # Legacy: requires_api_key
    if provider_info.get("requires_api_key"):
        return [{
            "id": "api_key",
            "label": "API Key",
            "kind": "api_key",
            "is_default": True,
            "fields": {
                "api_key": {
                    "description": "API Key",
                    "type": "string",
                    "secret": True,
                    "required": True,
                },
            },
        }]

    # Legacy: config_fields (e.g. Vertex AI service account)
    config_fields = provider_info.get("config_fields")
    if config_fields:
        return [{
            "id": "service_account",
            "label": "Service Account",
            "kind": "service_account",
            "is_default": True,
            "fields": dict(config_fields),
        }]

    # No auth required (e.g. Ollama, vLLM)
    return [{
        "id": "none",
        "label": "No Auth Required",
        "kind": "none",
        "is_default": True,
        "fields": {},
    }]
