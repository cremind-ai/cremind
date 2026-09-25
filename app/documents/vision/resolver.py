"""Resolve the Specialized Vision Model strictly — with no fallback.

``ModelGroupManager.get_provider_and_model("vision")`` quietly answers with the
main ("high") model when no vision model is set, or when the chosen one does
not fit the provider's active auth method. That is the right behaviour for the
interactive ``image_understanding`` tool, and exactly wrong here: indexing
must never ship a user's photo library to a model they did not pick for
images. So this module repeats the checks and *stops* instead of falling back,
and builds the LLM from the tuple it checked (``create_llm_for_group`` would
re-resolve and could fall back again).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

REASON_DISABLED = "vision_disabled"
REASON_UNSET = "vision_model_unset"
REASON_AUTH = "vision_model_auth_incompatible"
REASON_NOT_CAPABLE = "vision_model_not_capable"
REASON_ERROR = "vision_model_error"


@dataclass(frozen=True)
class VisionResolution:
    ok: bool
    provider: str | None = None
    model: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "provider": self.provider, "model": self.model, "reason": self.reason}


def _manager():
    from app.lib.llm.model_groups import ModelGroupManager
    from app.storage import get_dynamic_config_storage

    return ModelGroupManager(get_dynamic_config_storage())


def resolve_dedicated_vision(profile: str) -> VisionResolution:
    """Which vision model captioning may use for ``profile``, or why none."""
    from app.config import model_supports_vision, vision_feature_enabled

    try:
        if not vision_feature_enabled(profile):
            return VisionResolution(False, reason=REASON_DISABLED)
        mgr = _manager()
        raw = mgr.config_storage.get("llm_config", "model_group.vision", profile=profile)
        if not raw:
            return VisionResolution(False, reason=REASON_UNSET)
        provider, model = mgr._parse_group_value(raw)
        if not mgr._model_auth_compatible(provider, model, profile):
            return VisionResolution(False, provider, model, REASON_AUTH)
        if not model_supports_vision(provider, model, profile=profile):
            return VisionResolution(False, provider, model, REASON_NOT_CAPABLE)
        return VisionResolution(True, provider, model)
    except Exception:  # noqa: BLE001 — a broken config means "no vision", not a crash
        return VisionResolution(False, reason=REASON_ERROR)


def build_vision_llm(profile: str, resolution: VisionResolution):
    """The LLM for exactly the checked (provider, model) — never re-resolved."""
    from app.lib.llm.factory import create_llm_provider

    if not resolution.ok:
        raise ValueError(f"no usable vision model: {resolution.reason}")
    mgr = _manager()
    effort = None
    try:
        effort = mgr._get_group_reasoning_effort("vision", profile=profile)
    except Exception:  # noqa: BLE001
        pass
    return create_llm_provider(
        resolution.provider, resolution.model,
        config_storage=mgr.config_storage, profile=profile, default_reasoning_effort=effort,
    )


def consent_matches(options: dict[str, Any] | None, resolution: VisionResolution) -> bool:
    """The user agreed to send images to *this* provider and model. Switching
    the vision model asks again: consent to one provider is not consent to
    another."""
    consent = (options or {}).get("caption_consent") or {}
    return bool(
        resolution.ok and consent
        and consent.get("provider") == resolution.provider
        and consent.get("model") == resolution.model
    )
