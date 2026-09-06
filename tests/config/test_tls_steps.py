"""The HTTPS deployment runbook: notes stay prose, commands stay runnable.

The load-bearing property is the note/command split. The UI puts a copy button
on commands and nothing else, so a sentence that slipped into a command step
would be pasted into someone's terminal verbatim, and a command hidden inside a
note would have no button at all. ``command()`` refuses the first case; this
matrix is what makes it fail at test time rather than on a live request.
"""

from __future__ import annotations

import pytest

from app.config.tls_steps import (
    DOCKER_RECREATE_COMMAND,
    RESTART_COMMAND,
    SERVE_COMMAND,
    atlassian_callback_step,
    certificate_repair_steps,
    command,
    deployment_steps,
    flatten,
    note,
)

HTTPS_URL = "https://cremind.lan:1515"
ATLASSIAN = "Atlassian developer console"


def _steps(**overrides) -> list[dict]:
    kwargs = {
        "manager": "external", "install_mode": "kubernetes", "edge": False,
        "restart_supported": True, "activating": False, "https_url": HTTPS_URL,
    }
    kwargs.update(overrides)
    return deployment_steps(**kwargs)


def _commands(steps: list[dict]) -> list[str]:
    return [s["text"] for s in steps if s["kind"] == "command"]


def _notes(steps: list[dict]) -> list[str]:
    return [s["text"] for s in steps if s["kind"] == "note"]


# ── the constructors ─────────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "Run docker compose up -d",          # an instruction, not a command
    "docker compose up -d cremind.",     # a sentence period would be pasted
    "`cremind serve`",                   # backticks are markup, not shell
    "cremind serve\ncremind tls status",  # two lines, one box
    "",
    "   ",
])
def test_command_rejects_anything_that_is_not_a_bare_shell_line(text):
    with pytest.raises(ValueError):
        command(text)


def test_command_keeps_the_exact_text_it_is_given():
    assert command("cremind serve") == {"kind": "command", "text": "cremind serve"}
    # Placeholders and flags survive untouched; only the wrapper prose is banned.
    text = "kubectl --namespace <namespace> rollout status deployment/<release> --timeout=5m"
    assert command(text)["text"] == text


def test_note_refuses_backticks_because_it_renders_as_plain_text():
    with pytest.raises(ValueError):
        note("Run `cremind serve` afterwards.")
    assert note("Edit the .env file first.")["kind"] == "note"


def test_flatten_is_the_legacy_instruction_list():
    steps = _steps(install_mode="docker")
    assert flatten(steps) == [s["text"] for s in steps]


# ── the runbook, per deployment mode ─────────────────────────────────────


@pytest.mark.parametrize(
    ("kwargs", "expected_commands"),
    [
        pytest.param(
            {"edge": True},
            [
                "helm list --namespace <namespace>",
                "kubectl --namespace <namespace> create secret tls cremind-tls "
                "--cert=<path-to-fullchain.pem> --key=<path-to-privkey.pem>",
                "helm upgrade <release> <chart> --version <chart-version> "
                "--namespace <namespace> --reuse-values -f <your-values.yaml>",
                "kubectl --namespace <namespace> rollout status "
                "deployment/<release> --timeout=5m",
                "kubectl --namespace <namespace> get ingress <release>",
                f"curl --fail {HTTPS_URL}/api/tls/status",
            ],
            id="ingress",
        ),
        pytest.param(
            {"install_mode": "kubernetes"},
            [
                "helm list --namespace <namespace>",
                "helm upgrade <release> <chart> --version <chart-version> "
                "--namespace <namespace> --reuse-values --set cremind.ssl=auto",
                "kubectl --namespace <namespace> rollout status "
                "deployment/<release> --timeout=5m",
                "kubectl --namespace <namespace> port-forward svc/<release> 1515:80",
            ],
            id="kubernetes-in-pod",
        ),
        pytest.param(
            {"install_mode": "docker"}, [DOCKER_RECREATE_COMMAND], id="docker",
        ),
        pytest.param(
            {"install_mode": "native", "restart_supported": True},
            [RESTART_COMMAND], id="reverse-proxy-supervised",
        ),
        pytest.param(
            {"install_mode": "native", "restart_supported": False},
            [SERVE_COMMAND], id="reverse-proxy-unsupervised",
        ),
        pytest.param(
            {"manager": "native", "install_mode": "native", "restart_supported": False},
            [SERVE_COMMAND], id="unsupervised-native",
        ),
    ],
)
def test_each_mode_publishes_exactly_its_commands_in_order(kwargs, expected_commands):
    assert _commands(_steps(**kwargs)) == expected_commands


@pytest.mark.parametrize("kwargs", [
    pytest.param({"manager": "native", "restart_supported": True}, id="supervised-native"),
    pytest.param({"manager": "electron", "restart_supported": True}, id="electron"),
    pytest.param({"manager": "electron", "restart_supported": False}, id="electron-unsupervised"),
])
def test_installs_that_restart_themselves_have_nothing_to_run(kwargs):
    assert _steps(**kwargs) == []


@pytest.mark.parametrize("kwargs", [
    {"edge": True},
    {"install_mode": "kubernetes"},
    {"install_mode": "docker"},
    {"install_mode": "native", "restart_supported": True},
    {"install_mode": "native", "restart_supported": False},
    {"manager": "native", "install_mode": "native", "restart_supported": False},
])
def test_every_step_is_well_formed_and_reads_as_a_runbook(kwargs):
    steps = _steps(**kwargs)
    assert steps, "every deployment that must act needs steps"
    for step in steps:
        assert step["kind"] in ("note", "command")
        assert step["text"].strip() == step["text"] and step["text"]
        assert "`" not in step["text"]
        if step["kind"] == "command":
            assert "\n" not in step["text"]
            assert not step["text"].endswith(".")
            assert not step["text"].startswith("Run ")
    # Notes frame the commands: what to edit first, where it lands afterwards.
    assert steps[0]["kind"] == "note" and steps[-1]["kind"] == "note"
    assert HTTPS_URL in steps[-1]["text"]


def test_ingress_never_offers_a_port_forward():
    """The public hostname is the HTTPS address; a tunnel is meaningless here."""
    assert not any("port-forward" in s["text"] for s in _steps(edge=True))


@pytest.mark.parametrize(
    ("kwargs", "marker"),
    [
        ({"edge": True}, "ingress.tls"),
        ({"install_mode": "kubernetes"}, "cremind.atlassianRedirectUri"),
        ({"install_mode": "docker"}, "CREMIND_ATLASSIAN_REDIRECT_URI"),
        ({"install_mode": "native"}, "CREMIND_ATLASSIAN_REDIRECT_URI"),
    ],
)
def test_external_modes_name_their_own_configuration_keys(kwargs, marker):
    joined = " ".join(flatten(_steps(**kwargs)))
    assert marker in joined
    assert ATLASSIAN in joined


def test_a_native_install_is_never_told_about_atlassian_deployment_keys():
    """It has no deployment values to edit; the migrated-callback note covers it."""
    steps = _steps(manager="native", install_mode="native", restart_supported=False)
    assert ATLASSIAN not in " ".join(flatten(steps))


@pytest.mark.parametrize(
    ("activating", "opening"),
    [(True, "HTTPS settings are saved"), (False, "After activation")],
)
def test_the_unsupervised_native_note_matches_the_phase(activating, opening):
    steps = _steps(
        manager="native", install_mode="native",
        restart_supported=False, activating=activating,
    )
    assert steps[0]["text"].startswith(opening)


def test_docker_names_the_installers_own_compose_folder():
    """A Docker install leaves ~/.cremind untouched; the bundle lives elsewhere."""
    first = _steps(install_mode="docker")[0]["text"]
    assert "~/.local/share/cremind/docker" in first
    assert "~/.cremind/docker" not in first


# ── repairing a certificate on a server already serving HTTPS ────────────


@pytest.mark.parametrize(
    ("kwargs", "expected_commands"),
    [
        ({"install_mode": "docker"}, [DOCKER_RECREATE_COMMAND]),
        (
            {"install_mode": "kubernetes"},
            [
                "kubectl --namespace <namespace> rollout restart deployment/<release>",
                "kubectl --namespace <namespace> rollout status "
                "deployment/<release> --timeout=5m",
            ],
        ),
        ({"manager": "electron"}, []),
        ({"restart_supported": True}, [RESTART_COMMAND]),
        ({"restart_supported": False}, [SERVE_COMMAND]),
    ],
)
def test_certificate_repair_offers_only_the_reload(kwargs, expected_commands):
    defaults = {"manager": "native", "install_mode": "native", "restart_supported": True}
    steps = certificate_repair_steps(**{**defaults, **kwargs})
    assert _commands(steps) == expected_commands
    assert steps[0]["kind"] == "note"
    # Repair is not re-enabling: none of the enable-time configuration edits.
    joined = " ".join(flatten(steps))
    assert "CREMIND_SSL=auto" not in joined and "cremind.ssl=auto" not in joined


def test_a_container_repair_never_mentions_the_native_restart():
    steps = certificate_repair_steps(
        manager="external", install_mode="docker", restart_supported=True,
    )
    assert RESTART_COMMAND not in flatten(steps)


# ── the moved OAuth callback ─────────────────────────────────────────────


def test_the_migrated_callback_is_a_note_carrying_its_uri():
    """It is pasted into a web console, so it is guidance, not a shell line."""
    uri = "https://cremind.lan:1515/api/oauth/callback"
    step = atlassian_callback_step(uri)
    assert step["kind"] == "note"
    assert uri in step["text"] and ATLASSIAN in step["text"]
