"""`cremind profile wizard` — what each step asks, and what its answers mean.

Two jobs, both pure so they can be tested without a server:

* **Grammar.** Turn the ``KEY=VALUE`` pairs a user dictated in chat into the
  exact keys ``POST /api/config/setup`` stores. The web wizard's forms build
  those keys from provider TOMLs and tool catalogs; here the same mapping is
  written out, because an agent typing ``anthropic.api_key=`` unprompted is a
  worse experience than typing ``api_key=`` and having the provider filled in.
* **Validation.** Check the answers against the live catalogs before anything
  is stored, so a typo is caught in the turn it was made rather than three
  steps later as a ``channel_errors`` entry or a profile that cannot answer.

Catalogs are passed in rather than fetched, so every rule here is a pure
function of (payload, catalog).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

#: The model roles the wizard can set, and the ``<role>_model=`` key that sets
#: each. ``high`` is the main model everything else falls back to.
MODEL_ROLE_KEYS: dict[str, str] = {
    "model": "high",
    "plan_model": "plan",
    "low_model": "low",
    "vision_model": "vision",
    "audio_model": "audio",
}

#: Roles the user opts into. The web form always emits the toggle alongside the
#: model, so setting one here has to do the same or the role stays off.
_OPT_IN_ROLES = ("vision", "audio")


class StepInputError(Exception):
    """The pairs could not be turned into a payload at all."""


@dataclass
class Validation:
    """What the catalogs say about a payload.

    ``errors`` refuse the input. ``missing`` and ``warnings`` do not: a key can
    legitimately come from the server's environment, and a wizard that refused
    every incomplete answer would make "fill it in later" impossible.
    """

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def parse_pairs(pairs: Sequence[str]) -> dict[str, str]:
    """``["a=1", "b=2"]`` → ``{"a": "1", "b": "2"}``, splitting on the first ``=``."""
    out: dict[str, str] = {}
    for raw in pairs:
        if "=" not in raw:
            raise StepInputError(f"expected KEY=VALUE, got {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise StepInputError(f"expected KEY=VALUE, got {raw!r}")
        out[key] = value
    return out


def _truthy(value: str) -> bool:
    return str(value).strip().lower() in ("true", "1", "yes", "on")


# ── llm ──────────────────────────────────────────────────────────────────


def apply_llm(payload: dict[str, Any], kv: Mapping[str, str]) -> dict[str, Any]:
    """Merge ``KEY=VALUE`` answers into the ``llm_config`` record.

    Merging, not replacing: the user may name the provider and the model in one
    turn and paste the API key in the next.

    The grammar is a superset of the stored keys — anything already dotted
    (``anthropic.api_key``) passes through untouched, so an agent that read the
    provider TOML can be literal if it prefers.
    """
    result = dict(payload)
    provider = str(kv.get("provider") or result.get("default_provider") or "").strip()
    if kv.get("provider"):
        result["default_provider"] = provider

    for source_key, role in MODEL_ROLE_KEYS.items():
        if source_key not in kv:
            continue
        raw = str(kv[source_key]).strip()
        if not raw:
            continue
        if "/" in raw:
            model_value = raw
        else:
            if not provider:
                raise StepInputError(
                    f"{source_key}= needs provider= as well "
                    f"(or write {source_key}=<provider>/<model id>)"
                )
            model_value = f"{provider}/{raw}"
        result[f"model_group.{role}"] = model_value
        if role in _OPT_IN_ROLES:
            # The web form always emits this beside the model; without it the
            # role stays off and the model is stored but never used.
            result[f"model_group.{role}.enabled"] = "true"

    for key, value in kv.items():
        if key in MODEL_ROLE_KEYS or key == "provider":
            continue
        if key == "reasoning_effort":
            result["model_group.high.reasoning_effort"] = value
            continue
        if key.endswith("_reasoning_effort"):
            role = MODEL_ROLE_KEYS.get(key[: -len("_reasoning_effort")] + "_model")
            if role:
                result[f"model_group.{role}.reasoning_effort"] = value
                continue
        if "." in key or key == "default_provider":
            result[key] = value
            continue
        # Everything else is a field of the provider's sign-in method:
        # ``api_key=``, ``setup_token=``, ``auth_method=``…
        if not provider:
            raise StepInputError(
                f"{key}= needs provider= as well, so the wizard knows whose {key} it is"
            )
        result[f"{provider}.{key}"] = value
    return result


def _provider_entry(providers: Sequence[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    for p in providers:
        if str(p.get("name") or "") == name:
            return p
    return None


def active_auth_method(
    provider: Mapping[str, Any], payload: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """The sign-in method a payload selects, or the provider's default.

    Mirrors the server's own fallback (``app/api/llm.py``): an explicit
    ``<p>.auth_method`` wins, then the method flagged ``is_default``, then the
    first one listed.
    """
    methods = [m for m in (provider.get("auth_methods") or []) if isinstance(m, dict)]
    if not methods:
        return None
    chosen = str(payload.get(f"{provider.get('name')}.auth_method") or "").strip()
    if chosen:
        for m in methods:
            if str(m.get("id") or "") == chosen:
                return m
        return None
    for m in methods:
        if m.get("is_default"):
            return m
    return methods[0]


def validate_llm(
    payload: Mapping[str, Any],
    providers: Sequence[Mapping[str, Any]],
    models_by_provider: Mapping[str, Sequence[str]] | None = None,
) -> Validation:
    v = Validation()
    models_by_provider = models_by_provider or {}
    name = str(payload.get("default_provider") or "").strip()
    if not name:
        v.missing.append("provider")
        return v

    provider = _provider_entry(providers, name)
    if provider is None:
        known = ", ".join(sorted(str(p.get("name")) for p in providers if p.get("name")))
        v.errors.append(
            f"unknown LLM provider {name!r}. Known providers: {known}. "
            "List them with: cremind llm providers list"
        )
        return v

    method = active_auth_method(provider, payload)
    requested = str(payload.get(f"{name}.auth_method") or "").strip()
    if requested and method is None:
        ids = ", ".join(
            str(m.get("id")) for m in provider.get("auth_methods") or [] if m.get("id")
        )
        v.errors.append(
            f"{name} has no sign-in method {requested!r}. Available: {ids}"
        )
        return v
    if method is not None and str(method.get("kind")) == "oauth":
        # The server drops these anyway (they need a browser round-trip the
        # wizard cannot perform), so refusing here explains why instead of
        # silently creating a profile that cannot reach its provider.
        v.errors.append(
            f"{name}'s {method.get('id')!r} sign-in opens a browser, which this wizard "
            "cannot do. Choose another auth_method= now, or finish the sign-in later "
            "under Settings → LLM Providers."
        )
        return v

    if method is not None:
        for field_key, spec in (method.get("fields") or {}).items():
            if not isinstance(spec, dict) or not spec.get("required", True):
                continue
            if not str(payload.get(f"{name}.{field_key}") or "").strip():
                v.missing.append(f"{field_key} (required by {name}'s {method.get('id')} sign-in)")
        declared = set(method.get("fields") or {})
        for key in payload:
            if not key.startswith(f"{name}."):
                continue
            leaf = key.split(".", 1)[1]
            if leaf in ("auth_method", "api_key") or leaf in declared:
                continue
            v.warnings.append(
                f"{name} does not declare a {leaf!r} field for its "
                f"{method.get('id')} sign-in — it will be stored but may be ignored"
            )

    for role in set(MODEL_ROLE_KEYS.values()):
        value = str(payload.get(f"model_group.{role}") or "").strip()
        if not value:
            continue
        model_provider, _, model_id = value.partition("/")
        known_models = list(models_by_provider.get(model_provider) or [])
        if known_models and model_id not in known_models:
            sample = ", ".join(known_models[:10])
            v.errors.append(
                f"{model_provider} has no model {model_id!r}. Try one of: {sample}"
                + ("…" if len(known_models) > 10 else "")
                + f". List them with: cremind llm providers models {model_provider}"
            )

    if not str(payload.get("model_group.high") or "").strip():
        v.missing.append("model (the main model — the profile answers nothing without one)")
    return v


def llm_missing(payload: Mapping[str, Any], providers: Sequence[Mapping[str, Any]]) -> list[str]:
    """What ``finish`` must still complain about for a *set* llm step."""
    return validate_llm(payload, providers).missing


# ── tools ────────────────────────────────────────────────────────────────


def apply_tools(payload: dict[str, Any], kv: Mapping[str, str]) -> dict[str, Any]:
    """Merge ``<tool_id>.<what>=<value>`` answers into the tool payload.

    Four kinds of setting share one namespace, exactly as the web wizard packs
    them for the server: ``.enabled``, ``._arg.<name>``, ``._description`` and
    everything else (a tool variable).
    """
    tool_configs = dict(payload.get("tool_configs") or {})
    agent_configs = dict(payload.get("agent_configs") or {})

    for key, value in kv.items():
        if "." not in key:
            raise StepInputError(
                f"expected <tool_id>.<setting>=<value>, got {key!r}=… "
                "(list tool ids with: cremind tools list)"
            )
        tool_id, setting = key.split(".", 1)
        entry = dict(tool_configs.get(tool_id) or {})
        if setting == "enabled":
            entry["_enabled"] = "true" if _truthy(value) else "false"
        elif setting == "_description":
            agent_entry = dict(agent_configs.get(tool_id) or {})
            agent_entry["description"] = value
            agent_configs[tool_id] = agent_entry
            if entry:
                tool_configs[tool_id] = entry
            continue
        elif setting.startswith("_arg."):
            entry[setting] = value
        else:
            entry[setting] = value
        tool_configs[tool_id] = entry

    return {"tool_configs": tool_configs, "agent_configs": agent_configs}


def validate_tools(
    payload: Mapping[str, Any], tools: Sequence[Mapping[str, Any]],
) -> Validation:
    v = Validation()
    by_id = {str(t.get("tool_id")): t for t in tools if t.get("tool_id")}
    for tool_id, config in (payload.get("tool_configs") or {}).items():
        tool = by_id.get(tool_id)
        if tool is None:
            v.errors.append(
                f"unknown tool {tool_id!r}. List them with: cremind tools list "
                "(a profile's own skills are listed under the admin prefix, "
                "admin__<name>, and are remapped to the new profile at finish)"
            )
            continue
        enabled = str(config.get("_enabled", "")).lower()
        if enabled == "false" and tool.get("toggle_locked"):
            v.errors.append(f"{tool_id} cannot be disabled (it is a locked tool)")
        if enabled == "true":
            for name, spec in (tool.get("required_fields") or {}).items():
                if not isinstance(spec, dict) or not spec.get("required", False):
                    continue
                if not str(config.get(name) or "").strip():
                    v.missing.append(f"{tool_id}.{name}")
            feature = tool.get("requires_feature")
            if feature:
                v.warnings.append(
                    f"{tool_id} needs the {feature!r} feature — finish will "
                    f"pip-install cremind[{feature}], which can take a few minutes"
                )
        declared = set(tool.get("required_fields") or {})
        for key in config:
            if key == "_enabled" or key.startswith("_arg.") or key in declared:
                continue
            v.warnings.append(
                f"{tool_id} does not declare a {key!r} variable — it will be stored anyway"
            )
    return v


# ── memory ───────────────────────────────────────────────────────────────


def apply_memory(payload: dict[str, Any], kv: Mapping[str, str]) -> dict[str, Any]:
    """``enabled=true`` → ``{"memory.enabled": "true"}``.

    Other ``<group>.<key>=`` pairs pass through, so the step can carry any
    per-profile setting the schema declares without growing its own vocabulary.
    """
    result = dict(payload)
    for key, value in kv.items():
        if key == "enabled":
            result["memory.enabled"] = "true" if _truthy(value) else "false"
            continue
        if "." not in key:
            raise StepInputError(
                f"expected enabled=true|false or <group>.<key>=<value>, got {key!r}=… "
                "(list the settings with: cremind config schema)"
            )
        # The server stores booleans as the strings "true"/"false"; anything
        # else is passed through for the schema to judge.
        lowered = str(value).strip().lower()
        if lowered in ("true", "1", "yes", "on"):
            result[key] = "true"
        elif lowered in ("false", "0", "no", "off"):
            result[key] = "false"
        else:
            result[key] = value
    return result


def validate_memory(payload: Mapping[str, Any], schema: Mapping[str, Any]) -> Validation:
    v = Validation()
    groups = schema.get("groups") if isinstance(schema, dict) else None
    if not isinstance(groups, dict) or not groups:
        return v  # no schema to check against; the server validates too
    for key in payload:
        group, _, name = str(key).partition(".")
        fields = (groups.get(group) or {}).get("fields") if isinstance(groups.get(group), dict) else None
        if not isinstance(fields, dict) or name not in fields:
            v.errors.append(
                f"unknown setting {key!r}. List them with: cremind config schema"
            )
    return v


# ── channels ─────────────────────────────────────────────────────────────


def apply_channel(entries: list[dict[str, Any]], kv: Mapping[str, str],
                  catalog: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Add (or replace) one channel from ``KEY=VALUE`` answers.

    One channel per call, because the server allows one per type and a user
    dictates them one at a time. Re-naming a type replaces that entry rather
    than adding a second the server would reject.
    """
    channel_type = str(kv.get("channel_type") or "").strip()
    if not channel_type:
        raise StepInputError(
            "channel_type= is required (list the platforms with: cremind channels catalog)"
        )
    mode = str(kv.get("mode") or "").strip()
    if not mode:
        mode = _first_mode(catalog or {}, channel_type)
    if not mode:
        raise StepInputError(
            f"mode= is required for {channel_type} "
            "(list the modes with: cremind channels catalog)"
        )

    config: dict[str, Any] = {}
    entry: dict[str, Any] = {
        "channel_type": channel_type,
        "mode": mode,
        # Deprecated on the server and always neutral here; access is governed
        # by ``config.subscribe_auth`` below.
        "auth_mode": "none",
        "response_mode": str(kv.get("response_mode") or "normal"),
        "enabled": _truthy(kv["enabled"]) if "enabled" in kv else True,
        "config": config,
    }
    config["subscribe_auth"] = str(kv.get("subscribe_auth") or "open")
    for key, value in kv.items():
        if key in ("channel_type", "mode", "response_mode", "enabled", "subscribe_auth"):
            continue
        if key == "group_chats":
            # A real boolean, not the string: the adapters branch on the type.
            config["group_chats_enabled"] = _truthy(value)
            continue
        config[key] = value

    out = [e for e in entries if e.get("channel_type") != channel_type]
    out.append(entry)
    return out


def _channel_section(catalog: Mapping[str, Any], channel_type: str) -> Mapping[str, Any] | None:
    entry = catalog.get(channel_type) if isinstance(catalog, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    section = entry.get("channel")
    return section if isinstance(section, Mapping) else None


def _modes(catalog: Mapping[str, Any], channel_type: str) -> list[Mapping[str, Any]]:
    section = _channel_section(catalog, channel_type)
    modes = section.get("modes") if section else None
    return [m for m in modes if isinstance(m, Mapping)] if isinstance(modes, list) else []


def _first_mode(catalog: Mapping[str, Any], channel_type: str) -> str:
    for m in _modes(catalog, channel_type):
        if m.get("implemented") is not False:
            return str(m.get("id") or "")
    return ""


def mode_needs_pairing(catalog: Mapping[str, Any], channel_type: str, mode_id: str) -> bool:
    """Does this (type, mode) need an interactive pairing pass after setup?"""
    for m in _modes(catalog, channel_type):
        if str(m.get("id") or "") == mode_id:
            return bool(m.get("setup_kind"))
    return False


def validate_channels(
    entries: Sequence[Mapping[str, Any]], catalog: Mapping[str, Any],
) -> Validation:
    v = Validation()
    for entry in entries:
        channel_type = str(entry.get("channel_type") or "")
        section = _channel_section(catalog, channel_type)
        if section is None:
            known = ", ".join(sorted(str(k) for k in catalog)) if catalog else ""
            v.errors.append(
                f"unknown channel type {channel_type!r}"
                + (f". Known types: {known}" if known else "")
                + ". List them with: cremind channels catalog"
            )
            continue
        mode_id = str(entry.get("mode") or "")
        mode = next((m for m in _modes(catalog, channel_type) if str(m.get("id")) == mode_id), None)
        if mode is None:
            ids = ", ".join(str(m.get("id")) for m in _modes(catalog, channel_type))
            v.errors.append(f"{channel_type} has no mode {mode_id!r}. Available: {ids}")
            continue
        if mode.get("implemented") is False:
            v.errors.append(f"{channel_type}/{mode_id} is not implemented yet")
            continue
        config = entry.get("config") or {}
        declared = mode.get("fields") if isinstance(mode.get("fields"), Mapping) else {}
        for name, spec in (declared or {}).items():
            if isinstance(spec, Mapping) and spec.get("required") and not str(config.get(name) or "").strip():
                # A hard error: the server rejects the channel at finish and
                # reports it as a channel_error long after the user has gone.
                v.errors.append(f"{channel_type}/{mode_id} needs {name}=")
        for key in config:
            if key in ("subscribe_auth", "group_chats_enabled") or key in (declared or {}):
                continue
            v.warnings.append(
                f"{channel_type}/{mode_id} does not declare a {key!r} field — "
                "it will be stored anyway"
            )
        if mode.get("setup_kind"):
            v.warnings.append(
                f"{channel_type}/{mode_id} needs an interactive pairing step "
                "(QR scan or code) after the profile is created — finish prints the command"
            )
    return v


# ── the questions each step asks ─────────────────────────────────────────
#
# Rendered for a human reading a terminal *and* for an agent relaying them into
# chat, which is why each line names the command that lists the valid values:
# the agent can run it and offer the user real options instead of guessing.

_QUESTIONS: dict[str, dict[str, Any]] = {
    "llm": {
        "title": "LLM provider and main model",
        "why": (
            "REQUIRED for the profile to answer anything. Skipping it creates a "
            "profile that ignores every message until a model is chosen in "
            "Settings → LLM Providers."
        ),
        "fields": [
            ("provider=<name>", "which LLM provider", "cremind llm providers list"),
            ("model=<model id>", "the main model", "cremind llm providers models <provider>"),
            ("api_key=<key>", "the provider's credential (or setup_token=, etc.)", ""),
            ("auth_method=<id>", "optional — which sign-in to use; default is the provider's own", ""),
            ("plan_model= low_model= vision_model= audio_model=", "optional extra roles", ""),
            ("reasoning_effort=<low|medium|high>", "optional", ""),
        ],
        "example": "cremind profile wizard set {profile} llm provider=anthropic model=claude-sonnet-5 api_key=sk-...",
        "skip": (
            "the profile is created with no model and cannot answer anything until "
            "one is set"
        ),
    },
    "tools": {
        "title": "Built-in tools and skills",
        "why": "Which tools this profile may use, and any variables they need.",
        "fields": [
            ("<tool_id>.enabled=true|false", "turn a tool on or off", "cremind tools list"),
            ("<tool_id>.<VAR>=<value>", "a tool variable (API keys, endpoints)", "cremind --json tools list"),
            ("<tool_id>._arg.<name>=<json>", "a tool argument", ""),
            ("<tool_id>._description=<text>", "override what the agent is told the tool is for", ""),
        ],
        "example": "cremind profile wizard set {profile} tools web_search.enabled=true",
        "skip": "the server's defaults apply — default-enabled tools on, no variables",
    },
    "memory": {
        "title": "Long-term memory",
        "why": "Whether the agent remembers facts and habits across conversations.",
        "fields": [
            ("enabled=true|false", "turn memory on or off", ""),
            ("<group>.<key>=<value>", "any other per-profile setting", "cremind config schema"),
        ],
        "example": "cremind profile wizard set {profile} memory enabled=true",
        "skip": "memory stays off (its default)",
    },
    "channels": {
        "title": "Messaging channels",
        "why": "Platforms this profile can be messaged on. One channel per set call.",
        "fields": [
            ("channel_type=<type>", "the platform", "cremind channels catalog"),
            ("mode=<mode>", "bot / userbot / notification", "cremind --json channels catalog"),
            ("<field>=<value>", "the mode's own fields (bot_token, api_id…)", "cremind --json channels catalog"),
            ("response_mode=normal|detail  group_chats=true|false  subscribe_auth=open|passcode|otp|approval|allowlist", "optional", ""),
        ],
        "example": "cremind profile wizard set {profile} channels channel_type=telegram mode=bot bot_token=123:abc",
        "skip": "no channels — add them later with cremind channels add",
    },
}


def questions(step: str, profile: str) -> str:
    """The block a human (or an agent relaying to one) reads for ``step``."""
    q = _QUESTIONS[step]
    lines = [f"step {step} — {q['title']}", f"  {q['why']}"]
    for name, description, command in q["fields"]:
        line = f"  {name}"
        pad = max(1, 52 - len(name))
        line += " " * pad + description
        if command:
            line += f"   [{command}]"
        lines.append(line)
    lines.append(f"  answer with: {q['example'].format(profile=profile)}")
    lines.append(f"  or skip it: cremind profile wizard skip {profile} {step}   ({q['skip']})")
    return "\n".join(lines)


def questions_json(step: str, profile: str) -> dict[str, Any]:
    q = _QUESTIONS[step]
    return {
        "step": step,
        "title": q["title"],
        "why": q["why"],
        "fields": [
            {"key": name, "description": description, "catalog_command": command or None}
            for name, description, command in q["fields"]
        ],
        "example": q["example"].format(profile=profile),
        "skip_effect": q["skip"],
    }
