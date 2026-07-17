"""Configuration management using Dynaconf + TOML with SQLite dynamic overrides.

Priority chain: SQLite dynamic config > TOML file defaults > environment variable fallback.
"""

import os
from pathlib import Path

import toml
from dynaconf import Dynaconf

# Config directory is the same directory as this file (app/config/)
_CONFIG_DIR = Path(__file__).resolve().parent

# Initialize Dynaconf for TOML-based configuration
settings = Dynaconf(
    settings_files=[str(_CONFIG_DIR / "settings.toml")],
    envvar_prefix="CREMIND",
    environments=False,
    load_dotenv=True,
)


def load_provider_catalog(provider_name: str) -> dict:
    """Load a provider's model catalog from its TOML config file.

    Returns the parsed TOML dict, or empty dict if file not found.
    """
    toml_path = _CONFIG_DIR / "providers" / f"{provider_name}.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "r") as f:
        return toml.load(f)


def load_custom_providers(profile: str | None = None) -> dict:
    """Load the per-profile registry of user-defined "custom" providers.

    Custom providers are OpenAI-API-compatible endpoints the user adds from the
    LLM Providers page. They are stored per-profile in ``llm_config`` under a
    single non-secret JSON row keyed ``custom_providers`` — a mapping of
    ``slug -> {display_name, base_url, models: [...]}``. Their API keys live in
    separate secret rows (``custom:<slug>.api_key``), never in this registry.

    Returns an empty dict when no profile is given, storage isn't ready, or the
    row is missing/corrupt.
    """
    if profile is None:
        return {}
    import json

    from app.config.settings import get_dynamic

    raw = get_dynamic("llm_config", "custom_providers", profile=profile)
    if not raw:
        return {}
    try:
        registry = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return {}
    return registry if isinstance(registry, dict) else {}


def resolve_catalog(provider_name: str, profile: str | None = None) -> dict:
    """Resolve a provider's catalog, transparently handling custom providers.

    For built-in providers this is just ``load_provider_catalog``. For
    user-defined ``custom:<slug>`` providers it synthesizes a TOML-catalog-shaped
    dict (``{provider: {name, display_name, base_url}, models: [...]}``) from the
    per-profile registry so every catalog consumer — capability flags, the LLM
    factory, the models API — works unchanged.

    Returns ``{}`` for an unknown built-in provider or a deleted custom slug.
    """
    if provider_name and provider_name.startswith("custom:"):
        entry = load_custom_providers(profile).get(provider_name[len("custom:"):])
        if not entry:
            return {}
        return {
            "provider": {
                "name": provider_name,
                "display_name": entry.get("display_name", provider_name),
                "base_url": entry.get("base_url"),
            },
            "models": entry.get("models", []) or [],
        }
    return load_provider_catalog(provider_name)


def _vision_overrides() -> set[str]:
    """Models force-flagged vision-capable via the ``CREMIND_VISION_MODELS``
    env var (comma-separated ``provider/model`` or bare ``model`` ids).

    Escape hatch for custom / dynamic / proxy models (ollama, vllm, litellm,
    OpenAI-compatible gateways) whose catalog entries are illustrative and may
    not list the actual vision-capable model the user runs.
    """
    raw = os.environ.get("CREMIND_VISION_MODELS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def model_supports_vision(provider_name: str, model_name: str, profile: str | None = None) -> bool:
    """Return True if ``model_name`` is flagged vision-capable for its provider.

    Looks up the ``vision`` flag on the matching ``[[models]]`` entry in the
    provider catalog. Unknown models (not listed — e.g. a custom or dynamic
    model) default to False so the vision tool raises a clean, actionable error
    rather than letting the request fail opaquely at the provider API. The
    ``CREMIND_VISION_MODELS`` env var can force-enable a specific model.

    Passing ``profile`` lets a per-profile ``custom:<slug>`` provider's stored
    ``vision`` flag be honored (see ``resolve_catalog``).
    """
    if not provider_name or not model_name:
        return False
    model = model_name
    prefix = f"{provider_name}/"
    if model.startswith(prefix):
        model = model[len(prefix):]

    overrides = _vision_overrides()
    if f"{provider_name}/{model}" in overrides or model in overrides:
        return True

    catalog = resolve_catalog(provider_name, profile)
    for entry in catalog.get("models", []) or []:
        if isinstance(entry, dict) and entry.get("id") == model:
            return bool(entry.get("vision", False))
    return False


def vision_feature_enabled(profile: str | None = None) -> bool:
    """Whether the Specialized Vision Model feature is enabled for ``profile``.

    The feature is opt-in: when on, image understanding is routed through the
    dedicated ``vision`` model group and the ``image_understanding`` tool is
    exposed to the agent (and listed in Settings → Tools). When off (the
    default — an unset flag reads as False), the tool is withheld entirely.

    Reads the ``model_group.vision.enabled`` flag from ``llm_config`` (written
    via the model-groups API). Stored as ``"true"``/``"false"``; coerced with an
    explicit truthy set so the string ``"false"`` is correctly falsy.
    """
    from app.config.settings import get_dynamic
    raw = get_dynamic("llm_config", "model_group.vision.enabled", profile=profile)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _audio_overrides() -> set[str]:
    """Models force-flagged audio-input-capable via the ``CREMIND_AUDIO_MODELS``
    env var (comma-separated ``provider/model`` or bare ``model`` ids).

    Escape hatch for custom / dynamic / proxy models (ollama, vllm, litellm,
    OpenAI-compatible gateways) whose catalog entries are illustrative and may
    not list the actual audio-capable model the user runs. Mirrors
    ``_vision_overrides``.
    """
    raw = os.environ.get("CREMIND_AUDIO_MODELS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def model_supports_audio(provider_name: str, model_name: str, profile: str | None = None) -> bool:
    """Return True if ``model_name`` is flagged audio-input-capable for its provider.

    Looks up the ``audio`` flag on the matching ``[[models]]`` entry in the
    provider catalog. Unknown models (not listed — e.g. a custom or dynamic
    model) default to False so the audio tool raises a clean, actionable error
    rather than letting the request fail opaquely at the provider API. The
    ``CREMIND_AUDIO_MODELS`` env var can force-enable a specific model.

    Passing ``profile`` lets a per-profile ``custom:<slug>`` provider's stored
    ``audio`` flag be honored (see ``resolve_catalog``). Mirrors
    ``model_supports_vision``.
    """
    if not provider_name or not model_name:
        return False
    model = model_name
    prefix = f"{provider_name}/"
    if model.startswith(prefix):
        model = model[len(prefix):]

    overrides = _audio_overrides()
    if f"{provider_name}/{model}" in overrides or model in overrides:
        return True

    catalog = resolve_catalog(provider_name, profile)
    for entry in catalog.get("models", []) or []:
        if isinstance(entry, dict) and entry.get("id") == model:
            return bool(entry.get("audio", False))
    return False


def audio_feature_enabled(profile: str | None = None) -> bool:
    """Whether the Specialized Audio Model feature is enabled for ``profile``.

    The feature is opt-in: when on, audio understanding is routed through the
    dedicated ``audio`` model group and the ``audio_understanding`` tool is
    exposed to the agent (and listed in Settings → Tools). When off (the
    default — an unset flag reads as False), the tool is withheld entirely
    unless the main model itself accepts audio.

    Reads the ``model_group.audio.enabled`` flag from ``llm_config`` (written
    via the model-groups API). Stored as ``"true"``/``"false"``; coerced with an
    explicit truthy set so the string ``"false"`` is correctly falsy. Mirrors
    ``vision_feature_enabled``.
    """
    from app.config.settings import get_dynamic
    raw = get_dynamic("llm_config", "model_group.audio.enabled", profile=profile)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def model_supports_prompt_cache(provider_name: str, model_name: str) -> bool:
    """Return True if ``model_name`` is flagged prompt-cache-capable for its provider.

    Looks up the ``prompt_cache`` flag on the matching ``[[models]]`` entry in the
    provider catalog. Unknown / unlisted models default to False. This is
    informational metadata (surfaced via the model catalog API); cached-token
    *accounting* happens at runtime in the provider classes regardless of this flag.
    """
    if not provider_name or not model_name:
        return False
    model = model_name
    prefix = f"{provider_name}/"
    if model.startswith(prefix):
        model = model[len(prefix):]

    catalog = load_provider_catalog(provider_name)
    for entry in catalog.get("models", []) or []:
        if isinstance(entry, dict) and entry.get("id") == model:
            return bool(entry.get("prompt_cache", False))
    return False


def model_parallel_tool_calls(provider_name: str, model_name: str) -> bool:
    """Return True if ``model_name`` may emit multiple tool calls in one turn.

    Looks up the ``parallel_tool_calls`` flag on the matching ``[[models]]`` entry
    in the provider catalog. Unlike ``supports_reasoning``/``prompt_cache``, the
    default is **True** — parallel tool use is the default-on behavior for every
    provider, and the agent already executes leaf tool calls concurrently. Set the
    flag to ``false`` on a model entry only to *opt out* (e.g. OpenAI o-series /
    reasoning models that reject the ``parallel_tool_calls`` request parameter).
    Unknown / unlisted models default to True.
    """
    if not provider_name or not model_name:
        return True
    model = model_name
    prefix = f"{provider_name}/"
    if model.startswith(prefix):
        model = model[len(prefix):]

    catalog = load_provider_catalog(provider_name)
    for entry in catalog.get("models", []) or []:
        if isinstance(entry, dict) and entry.get("id") == model:
            return bool(entry.get("parallel_tool_calls", True))
    return True


def _reasoning_overrides() -> set[str]:
    """Models force-flagged native-reasoning-capable via the
    ``CREMIND_REASONING_MODELS`` env var (comma-separated ``provider/model`` or
    bare ``model`` ids).

    Escape hatch for custom / dynamic / proxy models (ollama, vllm, litellm,
    OpenAI-compatible gateways) whose catalog entries are illustrative. Marking
    a model here means it HAS native reasoning, so the agent's ``reasoning``
    think-tool is *disabled* for it (the inverse of the vision override, which
    enables a feature).
    """
    raw = os.environ.get("CREMIND_REASONING_MODELS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def model_supports_reasoning(provider_name: str, model_name: str, profile: str | None = None) -> bool:
    """Return True if ``model_name`` has native step-by-step reasoning.

    Looks up the ``supports_reasoning`` flag on the matching ``[[models]]``
    entry in the provider catalog. Unknown / unlisted models (e.g. a custom or
    dynamic model) default to ``False`` — i.e. treated as *non*-reasoning — so
    the agent's ``reasoning`` think-tool is enabled for them by default (the
    safe, beneficial default: a local/unknown model almost certainly lacks
    native reasoning). The ``CREMIND_REASONING_MODELS`` env var can force-mark a
    specific model as reasoning-capable.

    Passing ``profile`` lets a per-profile ``custom:<slug>`` provider's stored
    ``supports_reasoning`` flag be honored (see ``resolve_catalog``).
    """
    if not provider_name or not model_name:
        return False
    model = model_name
    prefix = f"{provider_name}/"
    if model.startswith(prefix):
        model = model[len(prefix):]

    overrides = _reasoning_overrides()
    if f"{provider_name}/{model}" in overrides or model in overrides:
        return True

    catalog = resolve_catalog(provider_name, profile)
    for entry in catalog.get("models", []) or []:
        if isinstance(entry, dict) and entry.get("id") == model:
            return bool(entry.get("supports_reasoning", False))
    return False


def load_all_provider_catalogs() -> dict[str, dict]:
    """Load all provider catalogs from app/config/providers/*.toml.

    Returns dict keyed by provider name.
    """
    providers_dir = _CONFIG_DIR / "providers"
    catalogs = {}
    if not providers_dir.exists():
        return catalogs
    for toml_file in providers_dir.glob("*.toml"):
        data = toml.load(toml_file)
        provider_info = data.get("provider", {})
        name = provider_info.get("name", toml_file.stem)
        catalogs[name] = data
    return catalogs


def load_channel_catalog(channel_type: str) -> dict:
    """Load a single channel's TOML config (auth modes, fields, instructions)."""
    toml_path = _CONFIG_DIR / "channels" / f"{channel_type}.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "r", encoding="utf-8") as f:
        return toml.load(f)


def load_all_channel_catalogs() -> dict[str, dict]:
    """Load every ``app/config/channels/*.toml``, keyed by channel type.

    The loader validates that each file declares ``[channel]`` with
    ``type``, ``display_name``, and at least one ``[[channel.modes]]`` entry.
    Files that fail validation are skipped with a warning.
    """
    channels_dir = _CONFIG_DIR / "channels"
    catalogs: dict[str, dict] = {}
    if not channels_dir.exists():
        return catalogs
    for toml_file in channels_dir.glob("*.toml"):
        try:
            data = toml.load(toml_file)
            channel_info = data.get("channel") or {}
            channel_type = channel_info.get("type")
            if not channel_type or not channel_info.get("display_name"):
                continue
            modes = channel_info.get("modes") or []
            if not modes:
                continue
            catalogs[channel_type] = data
        except Exception:  # noqa: BLE001
            # Bad TOML shouldn't crash the server; skip and continue.
            continue
    return catalogs


