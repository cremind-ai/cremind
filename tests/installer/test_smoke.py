"""Smoke tests for the installer TUI without driving the prompt_toolkit dialogs.

Driving prompt_toolkit's ``radiolist_dialog`` / ``input_dialog`` with a
``create_pipe_input`` is non-trivial because the dialog shortcuts spawn
their own Application instances with hard-coded key bindings. Instead we
test:

  - :mod:`app.installer.output` round-trips via ``write()`` + a shell-quote
    parser, so the file install.sh ``source``\\ s is well-formed even
    for values containing spaces / quotes / special chars.
  - :mod:`app.installer.catalog` parses the real ``install/_catalog.json``.
  - Every screen short-circuits when its slot in :class:`TuiResult` is
    already populated, returning ``advance`` without opening a dialog.
    This exercises the actual screen functions so a regression in the
    skip-logic would be caught.
  - The full :func:`app.installer.tui.run` walks the screen list cleanly
    when every slot is pre-populated.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.installer import catalog, output, tui
from app.installer.output import TuiResult, write


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "install" / "_catalog.json"


# ── output round-trip ────────────────────────────────────────────────────


def _parse_sourced(text: str) -> dict[str, str]:
    """Parse a KEY=VALUE file the way install.sh's ``source`` would."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
            inner = raw[1:-1]
            value = inner.replace("'\\''", "'")
        else:
            value = raw
        out[key] = value
    return out


def test_output_round_trip_simple(tmp_path: Path) -> None:
    result = TuiResult(
        channel="production",
        version_spec="0.2.1",
        deployment="local",
        mode="docker",
    )
    target = tmp_path / "tui.out"
    write(result, target)

    parsed = _parse_sourced(target.read_text(encoding="utf-8"))
    assert parsed["CHANNEL"] == "production"
    assert parsed["VERSION_SPEC"] == "0.2.1"
    assert parsed["DEPLOYMENT"] == "local"
    assert parsed["MODE"] == "docker"
    # Unset slots are written as empty strings so the shell guards keep working.
    assert parsed["APP_HOST"] == ""
    assert parsed["CUSTOM_listen_host"] == ""
    # DESKTOP_UI is emitted so install.sh / install.ps1 can source it.
    assert parsed["DESKTOP_UI"] == ""


def test_output_round_trip_desktop_ui(tmp_path: Path) -> None:
    result = TuiResult(mode="docker", desktop="0")
    target = tmp_path / "tui.out"
    write(result, target)

    parsed = _parse_sourced(target.read_text(encoding="utf-8"))
    assert parsed["DESKTOP_UI"] == "0"


def test_output_round_trip_quotes_special_chars(tmp_path: Path) -> None:
    result = TuiResult(
        channel="custom",
        deployment="custom",
        app_host="my host with spaces",
        custom_public_url="http://example.com/path?x=1&y=2",
        custom_allowed_origins="a,b,c",
        custom_wizard_preset="server",
    )
    target = tmp_path / "tui.out"
    write(result, target)

    parsed = _parse_sourced(target.read_text(encoding="utf-8"))
    assert parsed["APP_HOST"] == "my host with spaces"
    assert parsed["CUSTOM_public_url"] == "http://example.com/path?x=1&y=2"
    assert parsed["CUSTOM_allowed_origins"] == "a,b,c"


def test_output_round_trip_value_with_single_quote(tmp_path: Path) -> None:
    result = TuiResult(channel="production", app_host="bob's-box.local")
    target = tmp_path / "tui.out"
    write(result, target)

    parsed = _parse_sourced(target.read_text(encoding="utf-8"))
    assert parsed["APP_HOST"] == "bob's-box.local"


# ── catalog ──────────────────────────────────────────────────────────────


def test_catalog_loads_real_file() -> None:
    """Smoke-test that the shipped _catalog.json parses cleanly."""
    cat = catalog.load(CATALOG_PATH)
    deployment_ids = [d.id for d in cat.deployments]
    assert "local" in deployment_ids
    assert "server" in deployment_ids
    assert "custom" in deployment_ids

    custom = cat.deployment("custom")
    assert custom is not None
    keys = [f.key for f in custom.advanced_fields]
    assert keys == ["listen_host", "public_url", "allowed_origins", "wizard_preset"]

    wizard_field = next(f for f in custom.advanced_fields if f.key == "wizard_preset")
    assert wizard_field.choices == ("local", "docker", "server")

    mode_ids = [m.id for m in cat.modes]
    assert mode_ids == ["docker", "native"]

    # The docker desktop-UI sub-question is present and defaults to True.
    assert cat.docker_desktop.prompt
    assert cat.docker_desktop.default is True


# ── screen short-circuit (no dialog should open) ─────────────────────────


@pytest.fixture()
def loaded_catalog() -> catalog.Catalog:
    return catalog.load(CATALOG_PATH)


def _ctx(cat: catalog.Catalog, **overrides: object) -> tui.Context:
    defaults = dict(
        catalog=cat,
        in_container=False,
        has_docker=True,
        electron_version="",
    )
    defaults.update(overrides)
    return tui.Context(**defaults)  # type: ignore[arg-type]


def test_screen_channel_short_circuits_when_set(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(channel="test")
    new_state, action = tui.screen_channel(state, _ctx(loaded_catalog))
    assert action == "advance"
    assert new_state.channel == "test"


def test_screen_version_mode_short_circuits_on_dev(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(channel="dev")
    new_state, action = tui.screen_version_mode(state, _ctx(loaded_catalog))
    assert action == "advance"
    assert new_state.version_spec == ""


def test_screen_version_mode_pins_electron_version(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(channel="production")
    new_state, action = tui.screen_version_mode(
        state, _ctx(loaded_catalog, electron_version="0.2.5")
    )
    assert action == "advance"
    assert new_state.version_spec == "0.2.5"


def test_screen_version_mode_short_circuits_when_version_set(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(channel="production", version_spec="0.2.1")
    new_state, action = tui.screen_version_mode(state, _ctx(loaded_catalog))
    assert action == "advance"


def test_screen_version_picker_skips_when_version_set(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(channel="test", version_spec="0.2.1rc3")
    ctx = _ctx(loaded_catalog)
    ctx.version_mode = "specific"
    new_state, action = tui.screen_version_picker(state, ctx)
    assert action == "advance"


def test_screen_deployment_short_circuits_when_set(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(deployment="local")
    new_state, action = tui.screen_deployment(state, _ctx(loaded_catalog))
    assert action == "advance"
    assert new_state.deployment == "local"


def test_screen_server_host_skips_when_not_server(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(deployment="local")
    new_state, action = tui.screen_server_host(state, _ctx(loaded_catalog))
    assert action == "advance"


def test_screen_custom_fields_skips_when_not_custom(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(deployment="local")
    new_state, action = tui.screen_custom_fields(state, _ctx(loaded_catalog))
    assert action == "advance"


def test_screen_custom_fields_short_circuits_when_all_set(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(
        deployment="custom",
        custom_listen_host="0.0.0.0",
        custom_public_url="http://x:1112",
        custom_allowed_origins="x,y",
        custom_wizard_preset="local",
    )
    new_state, action = tui.screen_custom_fields(state, _ctx(loaded_catalog))
    assert action == "advance"
    assert new_state == state


def test_screen_mode_defaults_to_native_without_docker(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult()
    new_state, action = tui.screen_mode(state, _ctx(loaded_catalog, has_docker=False))
    assert action == "advance"
    assert new_state.mode == "native"


def test_screen_mode_short_circuits_when_set(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(mode="docker")
    new_state, action = tui.screen_mode(state, _ctx(loaded_catalog))
    assert action == "advance"
    assert new_state.mode == "docker"


def test_screen_desktop_skips_when_not_docker(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(mode="native")
    new_state, action = tui.screen_desktop(state, _ctx(loaded_catalog))
    assert action == "advance"
    assert new_state.desktop == ""


def test_screen_desktop_short_circuits_when_set(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(mode="docker", desktop="0")
    new_state, action = tui.screen_desktop(state, _ctx(loaded_catalog))
    assert action == "advance"
    assert new_state.desktop == "0"


def test_run_with_all_values_prepopulated(loaded_catalog: catalog.Catalog) -> None:
    """End-to-end: every screen short-circuits, run() returns to confirm.

    The confirm screen still opens a dialog. To avoid driving it we
    monkey-patch the screen list to drop it — this verifies the runner
    walks the screen sequence cleanly when nothing prompts.
    """
    state = TuiResult(
        channel="production",
        version_spec="0.2.1",
        deployment="local",
        mode="docker",
        desktop="1",
    )
    # Strip the confirm screen so the test doesn't open a dialog.
    original = tui._SCREENS
    tui._SCREENS = [s for s in original if s is not tui.screen_confirm]
    try:
        result = tui.run(
            catalog=loaded_catalog,
            initial=state,
            in_container=False,
            has_docker=True,
            electron_version="",
        )
    finally:
        tui._SCREENS = original

    assert result is not None
    assert result.channel == "production"
    assert result.version_spec == "0.2.1"
    assert result.deployment == "local"
    assert result.mode == "docker"
