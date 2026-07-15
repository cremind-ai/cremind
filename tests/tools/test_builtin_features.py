"""Unit tests for built-in tool feature derivation + Setup Wizard
auto-install hook.

Covers the Phase-2 additions:

- ``feature_keys_for_tool_ids`` resolves slugs and module names to the
  ``TOOL_CONFIG["requires_feature"]`` field, returns de-duped feature
  keys, and silently drops unknown ids.
- ``_tool_feature_keys_from_payload`` reads only enabled tools from a
  Setup Wizard ``tool_configs`` payload.
- ``_features_required_by_setup_payload`` unions tool-derived features
  with the existing embedding / llm / channel sources.
- ``list_builtin_tool_catalog`` surfaces ``requires_feature`` on every
  row so the wizard / settings UI can render the install hint.
- ``missing_dependency_result`` (from Phase 1) still emits the expected
  install commands when called with ``extras`` + ``install_commands``.
"""

from __future__ import annotations


def test_feature_keys_for_tool_ids_by_slug_drops_unknown_and_disabled() -> None:
    from app.tools.builtin import feature_keys_for_tool_ids

    # Browser + Markdown Converter both have requires_feature set; the
    # unknown id and the weather slug (no requires_feature) are dropped.
    keys = feature_keys_for_tool_ids(
        ["browser", "markdown_converter", "accuweather_weather", "nope", ""],
    )
    assert keys == ["browser", "documents"]


def test_feature_keys_for_tool_ids_dedupes_google() -> None:
    from app.tools.builtin import feature_keys_for_tool_ids

    # google_places is reachable by slug and by module stem; both resolve to
    # the same tool and its "google" feature -- expect a single de-duped key.
    keys = feature_keys_for_tool_ids(["google_places", "gg_places"])
    assert keys == ["google"]


def test_feature_keys_for_tool_ids_accepts_module_names() -> None:
    from app.tools.builtin import feature_keys_for_tool_ids

    # Module stems work too -- some callers (e.g. internal tooling)
    # pass module names instead of slugs.
    assert feature_keys_for_tool_ids(["gg_places"]) == ["google"]


def test_required_feature_for_tool_id_returns_single_key() -> None:
    from app.tools.builtin import required_feature_for_tool_id

    assert required_feature_for_tool_id("browser") == "browser"
    assert required_feature_for_tool_id("markdown_converter") == "documents"
    assert required_feature_for_tool_id("accuweather_weather") is None
    assert required_feature_for_tool_id("not_a_tool") is None


def test_catalog_row_includes_requires_feature() -> None:
    from app.tools.builtin import list_builtin_tool_catalog

    rows = {row["tool_id"]: row for row in list_builtin_tool_catalog()}

    # `requires_feature` is surfaced on every catalog row: a string for
    # tools that depend on an optional pip-extras group (so the wizard
    # can render "Installs: cremind[<feature>]") and None for the rest.
    assert rows["browser"]["requires_feature"] == "browser"
    assert rows["accuweather_weather"]["requires_feature"] is None

    # ``google_places`` is visible-but-off-by-default (``default: False`` in
    # TOOL_CONFIG), so it IS surfaced by the catalog -- with its "google"
    # feature hint and its wizard toggle defaulting off.
    assert rows["google_places"]["requires_feature"] == "google"
    assert rows["google_places"]["default_enabled"] is False

    # Runtime-only system tools deliberately gated out of the Settings UI
    # (``hidden: True`` in TOOL_CONFIG) are NOT surfaced by the catalog API.
    # Their requires_feature plumbing is still exercised through
    # feature_keys_for_tool_ids / required_feature_for_tool_id (tests above),
    # which don't filter by catalog visibility.
    assert "markdown_converter" not in rows  # hidden: True
    assert "sleep" not in rows               # hidden: True


def test_tool_feature_keys_from_payload_only_counts_enabled() -> None:
    from app.api.config import _tool_feature_keys_from_payload

    payload = {
        "tool_configs": {
            "browser": {"_enabled": "true"},               # -> "browser"
            "markdown_converter": {"_enabled": "false"},   # disabled -> skipped
            "google_places": {},                           # no _enabled key -> skipped
            "accuweather_weather": {"_enabled": "true"},   # enabled, no requires_feature
        },
    }
    keys = _tool_feature_keys_from_payload(payload)
    # Browser enabled -> "browser". Markdown Converter disabled and Google
    # Places missing its _enabled key are both skipped (no "documents"/"google").
    assert set(keys) == {"browser"}


def test_features_required_by_setup_payload_unions_tools_with_other_sources() -> None:
    from app.api.config import _features_required_by_setup_payload

    payload = {
        "server_config": {"db_provider": "postgres"},
        "llm_config": {"openai.api_key": "sk-..."},
        "embedding_config": {"enabled": True, "provider": "me5",
                             "vectorstore": {"provider": "qdrant"}},
        "channel_configs": [{"channel_type": "telegram", "mode": "bot"}],
        "tool_configs": {
            "browser": {"_enabled": "true"},
            "markdown_converter": {"_enabled": "true"},
        },
    }
    features = _features_required_by_setup_payload(payload)

    # All five source-categories present, no duplicates.
    assert "postgres" in features
    assert "embedding.me5" in features
    assert "vectorstore.qdrant" in features
    assert "llm.openai" in features
    assert "channel.telegram.bot" in features
    assert "browser" in features
    assert "documents" in features
    assert len(features) == len(set(features))


def test_features_required_by_setup_payload_handles_missing_tool_configs() -> None:
    from app.api.config import _features_required_by_setup_payload

    # The wizard may submit without tool_configs (LLM-only install path).
    payload = {"llm_config": {"openai.api_key": "sk-..."}}
    features = _features_required_by_setup_payload(payload)
    assert "browser" not in features
    assert "documents" not in features
    assert "llm.openai" in features


def test_missing_dependency_result_emits_install_commands() -> None:
    from app.tools.builtin.base import missing_dependency_result

    # Default: ``pip install 'cremind[<extras>]'`` derived from extras.
    result = missing_dependency_result(
        tool="convert_to_markdown",
        feature_key="documents",
        extras=("documents",),
        detail="markitdown not importable",
    )
    sc = result.structured_content
    assert sc is not None
    assert sc["error"] == "MissingDependency"
    assert sc["feature_key"] == "documents"
    assert sc["install_commands"] == ["pip install 'cremind[documents]'"]
    assert "Tools & Skills UI" in sc["remediation"]


def test_missing_dependency_result_respects_install_commands_override() -> None:
    from app.tools.builtin.base import missing_dependency_result

    # Browser case: feature has a post-install step beyond pip install.
    result = missing_dependency_result(
        tool="browser",
        feature_key="browser",
        extras=("browser",),
        detail="playwright not importable",
        install_commands=(
            "pip install 'cremind[browser]'",
            "python -m playwright install --with-deps chromium",
        ),
    )
    sc = result.structured_content
    assert sc is not None
    assert sc["install_commands"] == [
        "pip install 'cremind[browser]'",
        "python -m playwright install --with-deps chromium",
    ]


def test_restart_required_result_shape() -> None:
    from app.tools.builtin.base import restart_required_result

    result = restart_required_result(
        tool="browser",
        feature_key="browser",
        detail="Playwright is installed but was not importable.",
    )
    sc = result.structured_content
    assert sc is not None
    assert sc["error"] == "RestartRequired"
    assert sc["tool"] == "browser"
    assert sc["feature_key"] == "browser"
    # Remediation must direct the user to restart, since the wheel is
    # already on disk. The word "restart" appearing before any pip
    # mention is enough — the agent has the error code + the message
    # to back this up.
    remediation = sc["remediation"].lower()
    restart_idx = remediation.find("restart")
    pip_idx = remediation.find("pip install")
    assert restart_idx != -1, "remediation must mention 'restart'"
    assert pip_idx == -1 or restart_idx < pip_idx, (
        "if 'pip install' appears, it must come AFTER the restart "
        "directive so the agent doesn't tell users to re-install"
    )


def test_browser_run_emits_restart_required_when_wheel_on_disk(monkeypatch) -> None:
    """Browser tool: when _PLAYWRIGHT_AVAILABLE is stale but playwright
    is importable on disk, run() must return RestartRequired (not
    MissingDependency) so the agent tells the user to restart, not
    pip-install again."""
    import asyncio
    import importlib.util

    import app.tools.builtin.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_PLAYWRIGHT_AVAILABLE", False)
    # Force the runtime probe to "see" a wheel on disk regardless of
    # whether the test runner actually has playwright installed.
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name: object() if name == "playwright" else None,
    )

    tool = browser_mod.BrowserTool(defaults={
        "cdp_url": "", "headless": False, "channel": "chrome", "executable_path": "",
    })
    result = asyncio.run(tool.run({"action": "navigate", "url": "https://example.com"}))
    sc = result.structured_content
    assert sc is not None
    assert sc["error"] == "RestartRequired"


def test_browser_run_emits_missing_dependency_when_wheel_absent(monkeypatch) -> None:
    """The truly-missing-on-disk path still returns MissingDependency
    with the install_commands list."""
    import asyncio
    import importlib.util

    import app.tools.builtin.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_PLAYWRIGHT_AVAILABLE", False)
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)

    tool = browser_mod.BrowserTool(defaults={
        "cdp_url": "", "headless": False, "channel": "chrome", "executable_path": "",
    })
    result = asyncio.run(tool.run({"action": "navigate", "url": "https://example.com"}))
    sc = result.structured_content
    assert sc is not None
    assert sc["error"] == "MissingDependency"
    assert "pip install 'cremind[browser]'" in sc["install_commands"]
