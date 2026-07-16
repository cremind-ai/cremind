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
    already populated, returning ``skip`` (auto-advance) without opening a
    dialog. This exercises the actual screen functions so a regression in
    the skip-logic would be caught.
  - The full :func:`app.installer.tui.run` walks the screen list cleanly
    when every slot is pre-populated.
  - The :func:`app.installer.tui.run` loop's back / skip / history logic
    is driven with scripted screen stubs (no prompt_toolkit dialogs), and
    :func:`screen_custom_fields` per-field Back is driven with a scripted
    ``_custom_field_screen`` stub.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from app.installer import __main__ as installer_main
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
    assert action == "skip"
    assert new_state.channel == "test"


def test_screen_version_mode_short_circuits_on_dev(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(channel="dev")
    new_state, action = tui.screen_version_mode(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.version_spec == ""


def test_screen_version_mode_pins_electron_version(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(channel="production")
    new_state, action = tui.screen_version_mode(
        state, _ctx(loaded_catalog, electron_version="0.2.5")
    )
    assert action == "skip"
    assert new_state.version_spec == "0.2.5"


def test_screen_version_mode_short_circuits_when_version_set(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(channel="production", version_spec="0.2.1")
    new_state, action = tui.screen_version_mode(state, _ctx(loaded_catalog))
    assert action == "skip"


def test_screen_version_picker_skips_when_version_set(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(channel="test", version_spec="0.2.1rc3")
    ctx = _ctx(loaded_catalog)
    ctx.version_mode = "specific"
    new_state, action = tui.screen_version_picker(state, ctx)
    assert action == "skip"


def test_screen_deployment_short_circuits_when_set(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(deployment="local")
    new_state, action = tui.screen_deployment(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.deployment == "local"


def test_screen_server_host_skips_when_not_server(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(deployment="local")
    new_state, action = tui.screen_server_host(state, _ctx(loaded_catalog))
    assert action == "skip"


def test_screen_custom_fields_skips_when_not_custom(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(deployment="local")
    new_state, action = tui.screen_custom_fields(state, _ctx(loaded_catalog))
    assert action == "skip"


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
    assert action == "skip"
    assert new_state == state


def test_screen_mode_defaults_to_native_without_docker(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult()
    new_state, action = tui.screen_mode(state, _ctx(loaded_catalog, has_docker=False))
    assert action == "skip"
    assert new_state.mode == "native"


def test_screen_mode_short_circuits_when_set(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(mode="docker")
    new_state, action = tui.screen_mode(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.mode == "docker"


def test_screen_desktop_skips_when_not_docker(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(mode="native")
    new_state, action = tui.screen_desktop(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.desktop == ""


def test_screen_desktop_short_circuits_when_set(loaded_catalog: catalog.Catalog) -> None:
    state = TuiResult(mode="docker", desktop="0")
    new_state, action = tui.screen_desktop(state, _ctx(loaded_catalog))
    assert action == "skip"
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


# ── run() loop: back / skip / history logic (scripted screens, no dialogs) ──


def test_run_back_steps_to_previous_prompted_screen(monkeypatch) -> None:
    """Back pops to the previous *prompted* screen, stepping over skips.

    Also verifies the first prompted screen never offers Back and that a
    re-run after Back sees its slot cleared (so it re-prompts).
    """
    calls: list[tuple[str, bool, str]] = []

    def make(name, actions, slot=None):
        seq = iter(actions)

        def screen(state, ctx):
            calls.append((name, ctx.can_go_back, state.channel))
            act = next(seq)
            if act == "advance" and slot:
                state = replace(state, **{slot: name})
            return state, act

        return screen

    scripted = [
        make("A", ["skip"]),                            # flag-prepopulated auto-skip
        make("B", ["advance", "advance"], "channel"),   # first prompt; revisited on Back
        make("C", ["skip", "skip"]),                    # inapplicable auto-skip (hit twice)
        make("D", ["back", "advance"], "deployment"),   # Back → pops to B (over C)
        make("E", ["advance"], "mode"),
    ]
    monkeypatch.setattr(tui, "_SCREENS", scripted)

    result = tui.run(
        catalog=catalog.Catalog(),
        initial=TuiResult(),
        in_container=False,
        has_docker=True,
        electron_version="",
    )

    assert [c[0] for c in calls] == ["A", "B", "C", "D", "B", "C", "D", "E"]
    # B is the first prompted screen on both visits → Back is never offered.
    assert all(c[1] is False for c in calls if c[0] == "B")
    # D always has a prior prompted screen → Back is offered.
    assert all(c[1] is True for c in calls if c[0] == "D")
    # The revisit re-runs B with its slot cleared (channel reset to "").
    assert calls[4] == ("B", False, "")
    assert result is not None
    assert (result.channel, result.deployment, result.mode) == ("B", "D", "E")


def test_run_cancel_returns_none(monkeypatch) -> None:
    def screen(state, ctx):
        return state, "cancel"

    monkeypatch.setattr(tui, "_SCREENS", [screen])
    result = tui.run(
        catalog=catalog.Catalog(),
        initial=TuiResult(),
        in_container=False,
        has_docker=True,
        electron_version="",
    )
    assert result is None


def test_version_picker_rate_limit_returns_back(
    monkeypatch, loaded_catalog: catalog.Catalog
) -> None:
    """A GitHub fetch failure returns 'back' (to the version-mode screen)."""

    def _raise(*args, **kwargs):
        raise tui.RateLimitExceeded(None)

    monkeypatch.setattr(tui, "list_releases", _raise)
    monkeypatch.setattr(tui, "_message", lambda *a, **k: None)  # no dialog

    state = TuiResult(channel="test")
    ctx = _ctx(loaded_catalog)
    ctx.version_mode = "specific"
    _new_state, action = tui.screen_version_picker(state, ctx)
    assert action == "back"


# ── screen_custom_fields: per-field Back (scripted _custom_field_screen) ────


def test_custom_fields_per_field_back(
    monkeypatch, loaded_catalog: catalog.Catalog
) -> None:
    """Back on field N>0 re-prompts field N-1; the rest complete normally."""
    scripted = iter(
        [
            ("h", "advance"),      # field 0 listen_host
            ("u", "advance"),      # field 1 public_url
            (None, "back"),        # field 2 allowed_origins → Back to field 1
            ("u2", "advance"),     # field 1 re-prompted
            ("o", "advance"),      # field 2 again
            ("local", "advance"),  # field 3 wizard_preset
        ]
    )

    def stub(field_def, current, *, allow_back):
        return next(scripted)

    monkeypatch.setattr(tui, "_custom_field_screen", stub)

    state = TuiResult(deployment="custom")
    new_state, action = tui.screen_custom_fields(state, _ctx(loaded_catalog, can_go_back=True))

    assert action == "advance"
    assert new_state.custom_listen_host == "h"
    assert new_state.custom_public_url == "u2"  # re-answered after Back
    assert new_state.custom_allowed_origins == "o"
    assert new_state.custom_wizard_preset == "local"


def test_custom_fields_first_field_back_visibility(
    monkeypatch, loaded_catalog: catalog.Catalog
) -> None:
    """First field offers Back only when a prior screen prompted."""
    seen: list[tuple[str, bool]] = []

    def stub(field_def, current, *, allow_back):
        seen.append((field_def.key, allow_back))
        return (field_def.key, "advance")

    monkeypatch.setattr(tui, "_custom_field_screen", stub)

    tui.screen_custom_fields(
        TuiResult(deployment="custom"), _ctx(loaded_catalog, can_go_back=False)
    )
    # No earlier prompted screen → the first field must not show Back...
    assert seen[0] == ("listen_host", False)
    # ...but later fields can go back to the previous field.
    assert seen[1][1] is True


def test_custom_fields_back_on_first_field_bubbles_to_driver(
    monkeypatch, loaded_catalog: catalog.Catalog
) -> None:
    def stub(field_def, current, *, allow_back):
        return (None, "back")

    monkeypatch.setattr(tui, "_custom_field_screen", stub)

    _new_state, action = tui.screen_custom_fields(
        TuiResult(deployment="custom"), _ctx(loaded_catalog, can_go_back=True)
    )
    assert action == "back"


# ── dialog result mapping (no prompt_toolkit dialogs driven) ────────────────


def test_handle_common_force_quit_raises(monkeypatch) -> None:
    """Ctrl+C (the _FORCE_QUIT sentinel) raises KeyboardInterrupt → exit 1."""
    with pytest.raises(KeyboardInterrupt):
        tui._handle_common(tui._FORCE_QUIT)


def test_handle_common_escape_confirmed_cancels(monkeypatch) -> None:
    monkeypatch.setattr(tui, "_confirm_cancel", lambda: True)
    assert tui._handle_common(tui._ESCAPE) == (None, "cancel")


def test_handle_common_escape_declined_reshows(monkeypatch) -> None:
    monkeypatch.setattr(tui, "_confirm_cancel", lambda: False)
    assert tui._handle_common(tui._ESCAPE) is None  # None → re-show the screen


def test_handle_common_passthrough_tuple() -> None:
    assert tui._handle_common(("a", "advance")) == ("a", "advance")
    assert tui._handle_common((None, "back")) == (None, "back")


# ── __main__ cancel sentinel (uv-run eats the exit code on Ctrl+C) ──────────


def test_main_writes_cancel_marker_on_keyboardinterrupt(tmp_path, monkeypatch) -> None:
    out = tmp_path / "tui.out"

    def _raise(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(installer_main.tui, "run", _raise)
    rc = installer_main.main(["--output", str(out), "--catalog", str(CATALOG_PATH)])
    assert rc == 1
    assert out.read_text(encoding="utf-8").strip() == installer_main.CANCEL_MARKER


def test_main_writes_cancel_marker_when_run_returns_none(tmp_path, monkeypatch) -> None:
    out = tmp_path / "tui.out"
    monkeypatch.setattr(installer_main.tui, "run", lambda **k: None)
    rc = installer_main.main(["--output", str(out), "--catalog", str(CATALOG_PATH)])
    assert rc == 1
    assert installer_main.CANCEL_MARKER in out.read_text(encoding="utf-8")


def test_main_writes_selections_on_success_without_marker(tmp_path, monkeypatch) -> None:
    out = tmp_path / "tui.out"
    result = TuiResult(channel="test", deployment="local", mode="docker")
    monkeypatch.setattr(installer_main.tui, "run", lambda **k: result)
    rc = installer_main.main(["--output", str(out), "--catalog", str(CATALOG_PATH)])
    assert rc == 0
    text = out.read_text(encoding="utf-8")
    assert "CHANNEL=test" in text
    assert installer_main.CANCEL_MARKER not in text
