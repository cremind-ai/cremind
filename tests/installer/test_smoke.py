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

import json
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


def test_output_round_trip_kubernetes_keys(tmp_path: Path) -> None:
    result = TuiResult(
        mode="kubernetes",
        kube_context="ctx-b",
        kube_namespace="lee-cremind",
        k8s_release_name="cremind",
        k8s_legacy_postgres_image="yes",
        k8s_delete_postgres_data="no",
        # A helm --set value: every character here is in the unquoted-safe
        # set, so it must survive a bash ``source`` without quoting.
        k8s_extra_set="postgresql.image.registry=docker.io,ingress.enabled=true",
    )
    target = tmp_path / "tui.out"
    write(result, target)

    raw = target.read_text(encoding="utf-8")
    assert "K8S_extra_set=postgresql.image.registry=docker.io,ingress.enabled=true" in raw
    parsed = _parse_sourced(raw)
    assert parsed["KUBE_CONTEXT"] == "ctx-b"
    assert parsed["KUBE_NAMESPACE"] == "lee-cremind"
    assert parsed["K8S_release_name"] == "cremind"
    assert parsed["K8S_legacy_postgres_image"] == "yes"
    assert parsed["K8S_delete_postgres_data"] == "no"
    assert parsed["K8S_extra_set"].startswith("postgresql.image.registry=")
    # Unset kubernetes slots are still written, like every other key.
    assert parsed["K8S_app_url"] == ""


def test_output_round_trip_quotes_a_set_value_with_brackets(tmp_path: Path) -> None:
    result = TuiResult(k8s_extra_set="ingress.hosts[0]=x y.example.com")
    target = tmp_path / "tui.out"
    write(result, target)
    parsed = _parse_sourced(target.read_text(encoding="utf-8"))
    assert parsed["K8S_extra_set"] == "ingress.hosts[0]=x y.example.com"


def test_output_rejects_newlines(tmp_path: Path) -> None:
    """install.ps1 parses this file line by line and install.sh greps it for
    the cancel sentinel before sourcing, so a multi-line value could both lose
    its tail and fake a cancellation."""
    result = TuiResult(k8s_extra_set="a=b\nCREMIND_TUI_CANCELLED=1")
    target = tmp_path / "tui.out"
    with pytest.raises(ValueError):
        write(result, target)
    assert not target.exists()


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
    assert mode_ids == ["docker", "native", "kubernetes"]

    # `requires` is the visibility gate every front-end applies, so it has to
    # survive the JSON round-trip — an empty tuple on native is what keeps a
    # fallback mode available on every machine.
    assert cat.mode("docker").requires == ("docker",)
    assert cat.mode("native").requires == ()
    assert cat.mode("kubernetes").requires == ("kubectl", "helm")
    assert cat.mode("kubernetes").order > cat.mode("docker").order

    # The docker desktop-UI sub-question is present and defaults to True.
    assert cat.docker_desktop.prompt
    assert cat.docker_desktop.default is True

    # Kubernetes prompts + advanced fields.
    assert cat.kubernetes.namespace_default == "cremind"
    assert cat.kubernetes.context_prompt
    k8s_keys = [f.key for f in cat.kubernetes.advanced_fields]
    assert k8s_keys == [
        "release_name",
        "app_url",
        "legacy_postgres_image",
        "delete_postgres_data",
        "extra_set",
    ]
    legacy = next(
        f for f in cat.kubernetes.advanced_fields if f.key == "legacy_postgres_image"
    )
    assert legacy.choices == ("yes", "no")
    assert legacy.default == "yes"
    # Non-empty by contract: install.sh / install.ps1 read a populated
    # K8S_release_name as "the advanced questions were already answered".
    release_name = next(
        f for f in cat.kubernetes.advanced_fields if f.key == "release_name"
    )
    assert release_name.default == "cremind"


def test_mode_order_default_matches_the_generator(tmp_path) -> None:
    """An entry without ``order`` sorts LAST, as it does in every other reader.

    build_catalog._ordered() and installCatalogApi.ts both default to 999. The
    TUI used to default to 0, which would have put an order-less entry first
    here and last everywhere else.
    """
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"modes": {"zzz": {"label": "Z"}, "aaa": {"label": "A", "order": 10}}}),
        encoding="utf-8",
    )
    cat = catalog.load(path)
    assert [m.id for m in cat.modes] == ["aaa", "zzz"]
    assert cat.mode("zzz").order == 999


def test_available_modes_filters_on_requires() -> None:
    """The gate: every requirement must be a capability the caller found."""
    cat = catalog.load(CATALOG_PATH)
    assert [m.id for m in cat.available_modes(frozenset())] == ["native"]
    assert [m.id for m in cat.available_modes(frozenset({"docker"}))] == [
        "docker",
        "native",
    ]
    assert [m.id for m in cat.available_modes(frozenset({"kubectl", "helm"}))] == [
        "native",
        "kubernetes",
    ]
    # kubectl alone is not enough — helm is a separate requirement.
    assert "kubernetes" not in [
        m.id for m in cat.available_modes(frozenset({"kubectl"}))
    ]
    assert [m.id for m in cat.available_modes(frozenset({"docker", "kubectl", "helm"}))] == [
        "docker",
        "native",
        "kubernetes",
    ]


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


def test_screen_mode_lists_only_available_modes(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The radio offers what this machine can run, and says what it cannot."""
    seen: dict[str, object] = {}

    def fake_radio(**kwargs: object) -> tuple[str, str]:
        seen.update(kwargs)
        return "docker", "advance"

    monkeypatch.setattr(tui, "_radio", fake_radio)
    ctx = _ctx(loaded_catalog, has_docker=True, has_kubectl=True, has_helm=True)
    _state, action = tui.screen_mode(TuiResult(), ctx)
    assert action == "advance"
    assert [v[0] for v in seen["values"]] == ["docker", "native", "kubernetes"]
    assert seen["default"] == "docker"  # catalog order is the recommendation
    assert "Not offered here" not in seen["text"]

    seen.clear()
    ctx = _ctx(loaded_catalog, has_docker=False, has_kubectl=True, has_helm=True)
    _state, action = tui.screen_mode(TuiResult(), ctx)
    assert [v[0] for v in seen["values"]] == ["native", "kubernetes"]
    assert seen["default"] == "native"
    assert "Not offered here" in seen["text"] and "Docker" in seen["text"]


def test_screen_mode_hides_kubernetes_without_helm(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both capabilities are required; kubectl alone is not enough."""
    seen: dict[str, object] = {}

    def fake_radio(**kwargs: object) -> tuple[str, str]:
        seen.update(kwargs)
        return "docker", "advance"

    monkeypatch.setattr(tui, "_radio", fake_radio)
    ctx = _ctx(loaded_catalog, has_docker=True, has_kubectl=True, has_helm=False)
    tui.screen_mode(TuiResult(), ctx)
    assert [v[0] for v in seen["values"]] == ["docker", "native"]


def test_screen_mode_auto_selects_the_only_available_mode(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One survivor means nothing to ask — and no dialog is opened."""
    monkeypatch.setattr(
        tui, "_radio", lambda **_k: pytest.fail("no dialog when one mode survives")
    )
    ctx = _ctx(loaded_catalog, has_docker=False, has_kubectl=False, has_helm=False)
    new_state, action = tui.screen_mode(TuiResult(), ctx)
    assert action == "skip"
    assert new_state.mode == "native"


# ── kubernetes screens ───────────────────────────────────────────────────


def test_parse_kube_contexts() -> None:
    parsed = tui.parse_kube_contexts(
        "a\thttps://a:6443\tns1\n\nb\thttps://b\n\na\thttps://dupe\tx\n"
        # Sibling files reuse a name: both rows survive, told apart by file.
        "default\thttps://c:6443\tfrp\t/home/me/.kube/c\t0\n"
        "default\thttps://d:6443\t\t/home/me/.kube/d\t0\n"
        "cur\thttps://e:6443\t\t\t1\n"
    )
    assert parsed == (
        tui.KubeContext("a", "https://a:6443", "ns1"),
        tui.KubeContext("b", "https://b", ""),
        tui.KubeContext("default", "https://c:6443", "frp", "/home/me/.kube/c"),
        tui.KubeContext("default", "https://d:6443", "", "/home/me/.kube/d"),
        tui.KubeContext("cur", "https://e:6443", "", "", True),
    )
    assert parsed[0].key == ("", "a")
    assert parsed[2].key == ("/home/me/.kube/c", "default")
    assert tui.parse_kube_contexts("") == ()


def test_kubeconfig_label_shortens_home() -> None:
    assert tui.kubeconfig_label("") == "default kubeconfig"
    assert tui.kubeconfig_label("/home/me/.kube/x", home="/home/me") == "~/.kube/x"
    assert tui.kubeconfig_label(r"C:\Users\me\.kube\x", home=r"C:\Users\me") == r"~\.kube\x"
    # A sibling directory that merely starts with the home path is not home.
    assert tui.kubeconfig_label("/home/meow/.kube/x", home="/home/me") == "/home/meow/.kube/x"


_CONTEXTS = (
    tui.KubeContext("ctx-a", "https://a.example:6443", "default"),
    tui.KubeContext("ctx-b", "https://b.example:6443", "team"),
)

# The user's layout that motivated the file column: kubectl's own config plus
# two sibling files that both call their only context "default".
_MULTI_FILE_CONTEXTS = (
    tui.KubeContext("buddy", "https://buddy.example:443", "", "", True),
    tui.KubeContext("default", "https://161.0.0.1:6443", "frp", "/home/me/.kube/cremind_config"),
    tui.KubeContext("default", "https://103.0.0.1:6443", "", "/home/me/.kube/ssp_config"),
    tui.KubeContext("staging", "https://103.0.0.1:6443", "stg", "/home/me/.kube/ssp_config"),
)


def test_screen_kube_context_skips_unless_kubernetes(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(mode="docker")
    new_state, action = tui.screen_kube_context(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.kube_context == ""


def test_screen_kube_context_short_circuits_when_set(
    loaded_catalog: catalog.Catalog,
) -> None:
    state = TuiResult(mode="kubernetes", kube_context="ctx-b")
    new_state, action = tui.screen_kube_context(
        state, _ctx(loaded_catalog, kube_contexts=_CONTEXTS)
    )
    assert action == "skip"
    assert new_state.kube_context == "ctx-b"
    assert new_state.kube_config_file == ""


def test_a_unique_context_flag_resolves_its_file(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--kube-context staging names a sibling file's context: the shell needs
    that file on every call, so the skip records it."""
    monkeypatch.setattr(tui, "_radio", lambda **_k: pytest.fail("opened a dialog"))
    state = TuiResult(mode="kubernetes", kube_context="staging")
    new_state, action = tui.screen_kube_context(
        state, _ctx(loaded_catalog, kube_contexts=_MULTI_FILE_CONTEXTS)
    )
    assert action == "skip"
    assert new_state.kube_config_file == "/home/me/.kube/ssp_config"
    # An unknown name is left for the shell to reject with the full list.
    state = TuiResult(mode="kubernetes", kube_context="nope")
    new_state, action = tui.screen_kube_context(
        state, _ctx(loaded_catalog, kube_contexts=_MULTI_FILE_CONTEXTS)
    )
    assert action == "skip"
    assert new_state.kube_config_file == ""


def test_an_ambiguous_context_flag_asks_which_file(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both sibling files call their context "default"; the name alone must
    not quietly pick one of them."""
    seen: dict[str, object] = {}

    def fake_radio(**kwargs: object) -> tuple[str, str]:
        seen.update(kwargs)
        return "2", "advance"

    monkeypatch.setattr(tui, "_radio", fake_radio)
    state = TuiResult(mode="kubernetes", kube_context="default")
    new_state, action = tui.screen_kube_context(
        state, _ctx(loaded_catalog, kube_contexts=_MULTI_FILE_CONTEXTS)
    )
    assert action == "advance"
    assert new_state.kube_context == "default"
    assert new_state.kube_config_file == "/home/me/.kube/ssp_config"
    assert "'default' exists in 2 kubeconfig files" in str(seen["text"])
    labels = [label for _value, label in seen["values"]]
    assert len(labels) == 2
    assert all(label.startswith("default — ") for label in labels)
    assert "/home/me/.kube/cremind_config" in labels[0]
    assert "/home/me/.kube/ssp_config" in labels[1]


def test_a_kubeconfig_flag_restricts_the_picker_to_that_file(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_radio(**kwargs: object) -> tuple[str, str]:
        seen.update(kwargs)
        return "3", "advance"

    monkeypatch.setattr(tui, "_radio", fake_radio)
    state = TuiResult(mode="kubernetes", kube_config_file="/home/me/.kube/ssp_config")
    new_state, action = tui.screen_kube_context(
        state, _ctx(loaded_catalog, kube_contexts=_MULTI_FILE_CONTEXTS)
    )
    assert action == "advance"
    assert [value for value, _label in seen["values"]] == ["2", "3"]
    assert new_state.kube_context == "staging"
    assert new_state.kube_config_file == "/home/me/.kube/ssp_config"


def test_screen_kube_context_preselects_the_current_one(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each row names the API server, because two contexts can look alike."""
    seen: dict[str, object] = {}

    def fake_radio(**kwargs: object) -> tuple[str, str]:
        seen.update(kwargs)
        return "1", "advance"

    monkeypatch.setattr(tui, "_radio", fake_radio)
    contexts = (_CONTEXTS[0], _CONTEXTS[1]._replace(current=True))
    ctx = _ctx(loaded_catalog, kube_contexts=contexts)
    new_state, action = tui.screen_kube_context(TuiResult(mode="kubernetes"), ctx)
    assert action == "advance"
    assert new_state.kube_context == "ctx-b"
    assert new_state.kube_config_file == ""
    assert seen["default"] == "1"
    labels = [label for _value, label in seen["values"]]
    assert "https://a.example:6443" in labels[0]
    assert "(default)" in labels[0]
    assert labels[1].endswith("[current]")
    # One file in play: no file column to clutter the rows.
    assert "kubeconfig" not in labels[0]


def test_rows_name_their_file_when_several_are_in_play(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_radio(**kwargs: object) -> tuple[str, str]:
        seen.update(kwargs)
        return "0", "advance"

    monkeypatch.setattr(tui, "_radio", fake_radio)
    ctx = _ctx(loaded_catalog, kube_contexts=_MULTI_FILE_CONTEXTS)
    new_state, action = tui.screen_kube_context(TuiResult(mode="kubernetes"), ctx)
    assert action == "advance"
    assert new_state.kube_context == "buddy"
    assert new_state.kube_config_file == ""
    assert seen["default"] == "0"  # the ambient current-context
    labels = [label for _value, label in seen["values"]]
    assert "default kubeconfig" in labels[0] and labels[0].endswith("[current]")
    assert "/home/me/.kube/cremind_config" in labels[1]


def test_screen_kube_context_with_no_contexts(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing to install into: go back if we can, otherwise stop."""
    monkeypatch.setattr(tui, "_message", lambda **_k: None)
    state = TuiResult(mode="kubernetes")
    _s, action = tui.screen_kube_context(
        state, _ctx(loaded_catalog, kube_contexts=(), can_go_back=True)
    )
    assert action == "back"
    _s, action = tui.screen_kube_context(
        state, _ctx(loaded_catalog, kube_contexts=(), can_go_back=False)
    )
    assert action == "cancel"


@pytest.mark.parametrize(
    "value", ["cremind", "a", "ns-1", "lee-cremind", "x" * 63]
)
def test_validate_kube_namespace_accepts(value: str) -> None:
    assert tui.validate_kube_namespace(value) is None


@pytest.mark.parametrize(
    "value", ["", "Cremind", "-a", "a-", "a_b", "a.b", "x" * 64, "a b"]
)
def test_validate_kube_namespace_rejects(value: str) -> None:
    assert tui.validate_kube_namespace(value) is not None


def test_validate_helm_release_name() -> None:
    assert tui.validate_helm_release_name("cremind") is None
    assert tui.validate_helm_release_name("lee-cremind") is None
    assert tui.validate_helm_release_name("") is not None
    assert tui.validate_helm_release_name("Cremind") is not None
    # Helm's own limit is 53, stricter than a DNS label's 63.
    assert tui.validate_helm_release_name("x" * 53) is None
    assert tui.validate_helm_release_name("x" * 54) is not None


def test_screen_kube_namespace_uses_the_catalog_default(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_text(**kwargs: object) -> tuple[str, str]:
        seen.update(kwargs)
        return "  lee-cremind  ", "advance"

    monkeypatch.setattr(tui, "_text", fake_text)
    new_state, action = tui.screen_kube_namespace(
        TuiResult(mode="kubernetes"), _ctx(loaded_catalog)
    )
    assert action == "advance"
    assert new_state.kube_namespace == "lee-cremind"
    assert seen["default"] == "cremind"
    assert seen["validator"] is tui.validate_kube_namespace


@pytest.mark.parametrize(
    "screen",
    ["screen_deployment", "screen_server_host", "screen_custom_fields"],
)
def test_deployment_screens_skip_under_kubernetes(
    screen: str, loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kubernetes install has no host to bind, so none of these are asked."""
    for name in ("_radio", "_text"):
        monkeypatch.setattr(tui, name, lambda **_k: pytest.fail(f"{screen} prompted"))
    # Even with a deployment pre-set by flag, the screens must not re-open.
    state = TuiResult(mode="kubernetes", deployment="custom")
    _s, action = getattr(tui, screen)(state, _ctx(loaded_catalog))
    assert action == "skip"


def test_desktop_and_vnc_apply_to_kubernetes(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kubernetes runs the same two images, so both questions still apply."""
    monkeypatch.setattr(tui, "_radio", lambda **_k: ("0", "advance"))
    new_state, action = tui.screen_desktop(
        TuiResult(mode="kubernetes", channel="test"), _ctx(loaded_catalog)
    )
    assert action == "advance"
    assert new_state.desktop == "0"

    monkeypatch.setattr(tui, "_text", lambda **_k: ("abc123", "advance"))
    monkeypatch.setattr(tui, "_message", lambda **_k: None)
    new_state, action = tui.screen_vnc_password(
        TuiResult(mode="kubernetes", desktop="1"), _ctx(loaded_catalog)
    )
    assert action == "advance"
    assert new_state.vnc_password == "abc123"


def test_desktop_is_forced_on_production_kubernetes(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production chart and the basic image share a Docker Hub tag, so a
    headless production install is impossible — there is nothing to ask."""
    monkeypatch.setattr(tui, "_radio", lambda **_k: pytest.fail("asked anyway"))
    new_state, action = tui.screen_desktop(
        TuiResult(mode="kubernetes", channel="production"), _ctx(loaded_catalog)
    )
    assert action == "skip"
    assert new_state.desktop == "1"
    # A flag that says otherwise is left alone here; the shells refuse it.
    new_state, action = tui.screen_desktop(
        TuiResult(mode="kubernetes", channel="production", desktop="0"),
        _ctx(loaded_catalog),
    )
    assert action == "skip"
    assert new_state.desktop == "0"


def test_ssl_never_keeps_under_kubernetes(
    loaded_catalog: catalog.Catalog, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TLS is a chart value on the pod; a host .env says nothing about it."""
    monkeypatch.setattr(tui, "_radio", lambda **_k: ("none", "advance"))
    docker_env = tmp_path / "docker.env"
    docker_env.write_text("CREMIND_SSL=auto\n", encoding="utf-8")
    ctx = _ctx(
        loaded_catalog,
        ssl_inherited=True,
        docker_env=str(docker_env),
        native_env=str(docker_env),
    )
    new_state, action = tui.screen_ssl(TuiResult(mode="kubernetes"), ctx)
    assert action == "advance"
    assert new_state.ssl_choice == "none"
    # Same context, docker mode: the previous install still wins.
    new_state, action = tui.screen_ssl(TuiResult(mode="docker"), ctx)
    assert action == "skip"
    assert new_state.ssl_choice == "keep"


def test_k8s_advanced_gate_recommended_fills_defaults(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tui, "_radio", lambda **_k: ("recommended", "advance"))
    ctx = _ctx(loaded_catalog)
    new_state, action = tui.screen_k8s_advanced(TuiResult(mode="kubernetes"), ctx)
    assert action == "advance"
    assert new_state.k8s_release_name == "cremind"
    assert new_state.k8s_legacy_postgres_image == "yes"
    assert new_state.k8s_delete_postgres_data == "no"
    assert new_state.k8s_app_url == ""
    assert new_state.k8s_extra_set == ""
    assert ctx.k8s_advanced == "recommended"

    # The field loop is then a no-op — and opens no dialog.
    monkeypatch.setattr(
        tui, "_custom_field_screen", lambda *_a, **_k: pytest.fail("prompted")
    )
    _s, action = tui.screen_k8s_fields(new_state, ctx)
    assert action == "skip"


def test_k8s_advanced_gate_skipped_when_a_flag_answered_part_of_it(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flag means the operator is already customizing; ask only the rest."""
    monkeypatch.setattr(tui, "_radio", lambda **_k: pytest.fail("gate shown"))
    state = TuiResult(mode="kubernetes", k8s_extra_set="ingress.enabled=true")
    ctx = _ctx(loaded_catalog)
    _s, action = tui.screen_k8s_advanced(state, ctx)
    assert action == "skip"
    assert ctx.k8s_advanced == "customize"

    asked: list[str] = []

    def stub(field_def, current, *, allow_back, validator=None):
        asked.append(field_def.key)
        return field_def.default, "advance"

    monkeypatch.setattr(tui, "_custom_field_screen", stub)
    new_state, action = tui.screen_k8s_fields(state, ctx)
    assert action == "advance"
    assert asked == [
        "release_name",
        "app_url",
        "legacy_postgres_image",
        "delete_postgres_data",
    ]
    assert new_state.k8s_extra_set == "ingress.enabled=true"


def test_k8s_advanced_gate_treats_a_release_name_as_answered(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A populated release name is the "already answered" signal.

    ``app_url`` and ``extra_set`` are legitimately blank, so emptiness cannot
    mean "not asked yet" — only ``release_name`` has a non-empty default, and
    both install scripts read it the same way.
    """
    monkeypatch.setattr(tui, "_radio", lambda **_k: pytest.fail("gate shown"))
    monkeypatch.setattr(
        tui, "_custom_field_screen", lambda *_a, **_k: pytest.fail("field prompted")
    )
    state = TuiResult(
        mode="kubernetes",
        k8s_release_name="cremind",
        k8s_app_url="",
        k8s_extra_set="",
    )
    ctx = _ctx(loaded_catalog)
    _s, action = tui.screen_k8s_advanced(state, ctx)
    assert action == "skip"
    assert ctx.k8s_advanced == "recommended"
    _s, action = tui.screen_k8s_fields(state, ctx)
    assert action == "skip"


def test_k8s_fields_per_field_back(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Back inside the loop re-asks the previous field, as custom fields do."""
    script = [
        ("mine", "advance"),   # release_name
        (None, "back"),        # app_url → back to release_name
        ("cremind", "advance"),
        ("", "advance"),
        ("no", "advance"),
        ("yes", "advance"),
        ("a=b", "advance"),
    ]
    calls: list[str] = []

    def stub(field_def, current, *, allow_back, validator=None):
        calls.append(field_def.key)
        return script.pop(0)

    monkeypatch.setattr(tui, "_custom_field_screen", stub)
    ctx = _ctx(loaded_catalog)
    ctx.k8s_advanced = "customize"
    new_state, action = tui.screen_k8s_fields(TuiResult(mode="kubernetes"), ctx)
    assert action == "advance"
    assert calls[:3] == ["release_name", "app_url", "release_name"]
    assert new_state.k8s_release_name == "cremind"
    assert new_state.k8s_extra_set == "a=b"


def test_k8s_fields_back_on_the_first_field_bubbles_out(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stub(field_def, current, *, allow_back, validator=None):
        return None, "back"

    monkeypatch.setattr(tui, "_custom_field_screen", stub)
    ctx = _ctx(loaded_catalog)
    ctx.k8s_advanced = "customize"
    _s, action = tui.screen_k8s_fields(TuiResult(mode="kubernetes"), ctx)
    assert action == "back"


def test_k8s_app_url_validator_respects_the_https_choice() -> None:
    """The chart refuses cremind.ssl together with an http:// app URL, and by
    the time helm says so the TUI is long gone."""
    secure = tui._k8s_validator("app_url", TuiResult(ssl_choice="after-setup"))
    assert secure("http://localhost:1515") is not None
    assert secure("https://localhost:1515") is None
    assert secure("") is None
    assert secure("ftp://x") is not None

    plain = tui._k8s_validator("app_url", TuiResult(ssl_choice="none"))
    assert plain("http://localhost:1515") is None

    assert tui._k8s_validator("release_name", TuiResult()) is tui.validate_helm_release_name
    assert tui._k8s_validator("legacy_postgres_image", TuiResult()) is None


def test_confirm_rows_for_kubernetes(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake_choice(**kwargs: object) -> tuple[None, str]:
        seen.update(kwargs)
        return None, "advance"

    monkeypatch.setattr(tui, "_choice", fake_choice)
    state = TuiResult(
        channel="production",
        mode="kubernetes",
        kube_context="ctx-b",
        kube_namespace="lee-cremind",
        k8s_release_name="cremind",
        k8s_legacy_postgres_image="yes",
        k8s_delete_postgres_data="yes",
        desktop="1",
    )
    tui.screen_confirm(state, _ctx(loaded_catalog, kube_contexts=_CONTEXTS))
    text = str(seen["text"])
    assert "ctx-b" in text and "https://b.example:6443" in text
    assert "Namespace" in text and "lee-cremind" in text
    assert "Release" in text and "Postgres image" in text and "Extra --set" in text
    assert "deleted on uninstall" in text
    assert "required on the production channel" in text
    assert "Deployment" not in text


def test_screen_confirm_names_the_kubeconfig_file(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two files call their context "default": the summary must show the
    server and file of the one actually chosen."""
    seen: dict[str, object] = {}

    def fake_choice(**kwargs: object) -> tuple[None, str]:
        seen.update(kwargs)
        return None, "advance"

    monkeypatch.setattr(tui, "_choice", fake_choice)
    state = TuiResult(
        channel="test",
        mode="kubernetes",
        kube_context="default",
        kube_config_file="/home/me/.kube/ssp_config",
        kube_namespace="cremind",
        desktop="1",
    )
    tui.screen_confirm(state, _ctx(loaded_catalog, kube_contexts=_MULTI_FILE_CONTEXTS))
    text = str(seen["text"])
    assert "https://103.0.0.1:6443" in text
    assert "https://161.0.0.1:6443" not in text
    assert "/home/me/.kube/ssp_config" in text


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


def test_screen_vnc_password_skips_when_not_docker(
    loaded_catalog: catalog.Catalog,
) -> None:
    """A native install has no container desktop to protect."""
    state = TuiResult(mode="native")
    new_state, action = tui.screen_vnc_password(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.vnc_password == ""


def test_screen_vnc_password_skips_when_desktop_declined(
    loaded_catalog: catalog.Catalog,
) -> None:
    """The basic image ships no VNC server, so there is nothing to ask about."""
    state = TuiResult(mode="docker", desktop="0")
    new_state, action = tui.screen_vnc_password(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.vnc_password == ""


def test_screen_vnc_password_short_circuits_when_set(
    loaded_catalog: catalog.Catalog,
) -> None:
    """``--vnc-password`` on the command line pre-answers the screen."""
    state = TuiResult(mode="docker", desktop="1", vnc_password="abc123")
    new_state, action = tui.screen_vnc_password(state, _ctx(loaded_catalog))
    assert action == "skip"
    assert new_state.vnc_password == "abc123"


@pytest.mark.parametrize("value", ["abc123", "Pw@6789", "a.b-c_d"])
def test_validate_vnc_password_accepts_the_vnc_range(value: str) -> None:
    assert tui.validate_vnc_password(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "abc12",       # 5 — below TigerVNC's minimum
        "abcdefghi",   # 9 — past the DES cut-off, would be silently truncated
        "abc 123",     # space: unquoted in the compose .env
        "abc|123",     # pipe: install.sh renders the template with sed s|…|…|g
        "abc$123",     # dollar: install.ps1 -replace, and shell expansion
        "abc#123",     # hash: starts a comment in a .env file
        'abc"123',     # quote: breaks the TOML string in credentials.toml
    ],
)
def test_validate_vnc_password_rejects_what_the_pipeline_cannot_carry(
    value: str,
) -> None:
    assert tui.validate_vnc_password(value) is not None


def test_validate_vnc_password_blank_depends_on_a_previous_install() -> None:
    """Blank means "keep the current password" — but only if there is one."""
    assert tui.validate_vnc_password("", allow_blank=True) is None
    assert tui.validate_vnc_password("", allow_blank=False) is not None


def test_vnc_password_rides_the_output_file_under_its_own_key(tmp_path) -> None:
    """Never as ``VNC_PASSWORD``: install.sh sources this file and owns that
    name for its own flag/previous/generated chain."""
    from app.installer.output import write

    out = tmp_path / "tui.out"
    write(TuiResult(mode="docker", desktop="1", vnc_password="abc123"), out)

    body = out.read_text(encoding="utf-8")
    assert "VNC_PASSWORD_INPUT=abc123" in body
    assert "\nVNC_PASSWORD=" not in body


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
        vnc_password="abc123",
        ssl_choice="none",
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


def test_run_kubernetes_with_all_values_prepopulated(
    loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fully flagged kubernetes install walks the screen list silently.

    Everything a kubernetes run can be asked is supplied, so no dialog may
    open — including the deployment screens, which do not apply at all.
    """
    for name in ("_radio", "_text", "_message"):
        monkeypatch.setattr(tui, name, lambda **_k: pytest.fail("opened a dialog"))
    state = TuiResult(
        channel="test",
        version_spec="0.0.17rc16.dev6",
        mode="kubernetes",
        kube_context="ctx-b",
        kube_namespace="lee-cremind",
        desktop="1",
        vnc_password="abc123",
        ssl_choice="none",
        k8s_release_name="cremind",
        k8s_app_url="",
        k8s_legacy_postgres_image="yes",
        k8s_delete_postgres_data="no",
        k8s_extra_set="",
    )
    original = tui._SCREENS
    tui._SCREENS = [s for s in original if s is not tui.screen_confirm]
    try:
        result = tui.run(
            catalog=loaded_catalog,
            initial=state,
            in_container=False,
            has_docker=False,
            has_kubectl=True,
            has_helm=True,
            kube_contexts=_CONTEXTS,
            electron_version="",
        )
    finally:
        tui._SCREENS = original

    assert result is not None
    assert result.mode == "kubernetes"
    assert result.kube_context == "ctx-b"
    assert result.kube_namespace == "lee-cremind"
    # The deployment questions never ran, so their slots stay empty.
    assert result.deployment == ""
    assert result.custom_listen_host == ""


@pytest.mark.parametrize("mode", ["none", "auto", "after-setup"])
def test_ssl_flag_skips_prompt(mode, loaded_catalog, monkeypatch) -> None:
    monkeypatch.setattr(tui, "_radio", lambda **kwargs: pytest.fail("flag must skip prompt"))
    state, action = tui.screen_ssl(TuiResult(ssl_choice=mode), _ctx(loaded_catalog))
    assert action == "skip"
    assert state.ssl_choice == mode


@pytest.mark.parametrize("selected", ["none", "after-setup"])
def test_fresh_ssl_choice_defaults_to_http(selected, loaded_catalog, monkeypatch) -> None:
    def choose(**kwargs):
        assert kwargs["default"] == "none"
        assert [value for value, label in kwargs["values"]] == ["none", "after-setup"]
        assert "Settings > Security" in kwargs["text"]
        return selected, "advance"

    monkeypatch.setattr(tui, "_radio", choose)
    state, action = tui.screen_ssl(TuiResult(mode="native"), _ctx(loaded_catalog))
    assert action == "advance"
    assert state.ssl_choice == selected
    assert state.as_env_dict()["SSL_CHOICE"] == selected


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_ssl_preserves_previous_install_for_selected_mode(mode, tmp_path, loaded_catalog, monkeypatch) -> None:
    previous = tmp_path / ".env"
    previous.write_text("CREMIND_SSL=auto\n", encoding="utf-8")
    ctx = _ctx(loaded_catalog, **{f"{mode}_env": str(previous)})
    monkeypatch.setattr(tui, "_radio", lambda **kwargs: pytest.fail("existing mode must not prompt"))
    state, action = tui.screen_ssl(TuiResult(mode=mode), ctx)
    assert (state.ssl_choice, action) == ("keep", "skip")


def test_ssl_does_not_inherit_other_install_mode(tmp_path, loaded_catalog, monkeypatch) -> None:
    native = tmp_path / "native.env"
    native.write_text("CREMIND_SSL=auto\n", encoding="utf-8")
    monkeypatch.setattr(tui, "_radio", lambda **kwargs: ("none", "advance"))
    state, action = tui.screen_ssl(
        TuiResult(mode="docker"), _ctx(loaded_catalog, native_env=str(native))
    )
    assert (state.ssl_choice, action) == ("none", "advance")


def test_ssl_preserves_inherited_settings(loaded_catalog, monkeypatch) -> None:
    monkeypatch.setattr(tui, "_radio", lambda **kwargs: pytest.fail("inherited mode must not prompt"))
    state, action = tui.screen_ssl(TuiResult(), _ctx(loaded_catalog, ssl_inherited=True))
    assert (state.ssl_choice, action) == ("keep", "skip")


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

    def stub(field_def, current, *, allow_back, validator=None):
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

    def stub(field_def, current, *, allow_back, validator=None):
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
    def stub(field_def, current, *, allow_back, validator=None):
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


def test_main_plumbs_the_kubernetes_flags(tmp_path, monkeypatch) -> None:
    """Every kubernetes flag reaches the TUI as a pre-populated slot or a
    context value — a dropped one would silently re-ask a question the
    operator already answered on the command line."""
    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> TuiResult:
        seen.update(kwargs)
        return kwargs["initial"]  # type: ignore[return-value]

    monkeypatch.setattr(installer_main.tui, "run", fake_run)
    contexts = tmp_path / "contexts"
    contexts.write_text("ctx-a\thttps://a:6443\tdefault\t\t1\n", encoding="utf-8")
    out = tmp_path / "tui.out"
    rc = installer_main.main(
        [
            "--output", str(out), "--catalog", str(CATALOG_PATH),
            "--mode", "kubernetes",
            "--kube-context", "ctx-a",
            "--kubeconfig", "/home/me/.kube/other",
            "--kube-namespace", "lee-cremind",
            "--k8s-release-name", "rel",
            "--k8s-app-url", "https://cremind.example.com",
            "--k8s-legacy-postgres-image", "no",
            "--k8s-delete-postgres-data", "yes",
            "--k8s-extra-set", "a=b",
            "--has-kubectl", "1",
            "--has-helm", "1",
            "--kube-contexts-file", str(contexts),
        ]
    )
    assert rc == 0
    initial: TuiResult = seen["initial"]  # type: ignore[assignment]
    assert initial.mode == "kubernetes"
    assert initial.kube_context == "ctx-a"
    assert initial.kube_config_file == "/home/me/.kube/other"
    assert initial.kube_namespace == "lee-cremind"
    assert initial.k8s_release_name == "rel"
    assert initial.k8s_app_url == "https://cremind.example.com"
    assert initial.k8s_legacy_postgres_image == "no"
    assert initial.k8s_delete_postgres_data == "yes"
    assert initial.k8s_extra_set == "a=b"
    assert seen["has_kubectl"] is True and seen["has_helm"] is True
    assert seen["kube_contexts"] == (
        tui.KubeContext("ctx-a", "https://a:6443", "default", "", True),
    )
    assert "kube_current_context" not in seen


def test_main_tolerates_a_missing_contexts_file(tmp_path, monkeypatch) -> None:
    """The context screen has its own message for an empty list; a missing
    file must not take the installer down before it gets there."""
    monkeypatch.setattr(
        installer_main.tui, "run", lambda **k: k["initial"]
    )
    out = tmp_path / "tui.out"
    rc = installer_main.main(
        [
            "--output", str(out), "--catalog", str(CATALOG_PATH),
            "--kube-contexts-file", str(tmp_path / "nope"),
        ]
    )
    assert rc == 0


def test_main_exits_2_on_an_unwritable_answer(tmp_path, monkeypatch) -> None:
    """A multi-line value is refused by write(); exit 2 makes the shell fall
    back to its text prompts instead of sourcing a corrupt file."""
    result = TuiResult(k8s_extra_set="a=b\nCREMIND_TUI_CANCELLED=1")
    monkeypatch.setattr(installer_main.tui, "run", lambda **k: result)
    out = tmp_path / "tui.out"
    rc = installer_main.main(["--output", str(out), "--catalog", str(CATALOG_PATH)])
    assert rc == 2
    assert not out.exists()


def test_the_kubeconfig_path_round_trips_quoted(tmp_path) -> None:
    """A Windows path is all backslashes, which are outside the unquoted-safe
    set, so it travels shell-quoted — under a key that is NOT the KUBECONFIG
    variable kubectl itself reads."""
    from app.installer import output as installer_output

    out = tmp_path / "tui.out"
    installer_output.write(
        TuiResult(kube_config_file=r"C:\Users\me\.kube\ssp_config"), out
    )
    text = out.read_text(encoding="utf-8")
    assert "KUBE_CONFIG_FILE='C:\\Users\\me\\.kube\\ssp_config'" in text
    assert "\nKUBECONFIG=" not in text
