"""The HTTPS deployment runbook: notes stay prose, commands stay runnable.

The load-bearing property is the note/command split. The UI puts a copy button
on commands and nothing else, so a sentence that slipped into a command step
would be pasted into someone's terminal verbatim, and a command hidden inside a
note would have no button at all. ``command()`` refuses the first case; this
matrix is what makes it fail at test time rather than on a live request.
"""

from __future__ import annotations

import pytest

from app.config import runtime_env
from app.config.tls_steps import (
    CHART_REFERENCE,
    DOCKER_RECREATE_COMMAND,
    NAMESPACE_PLACEHOLDER,
    RELEASE_PLACEHOLDER,
    RESTART_COMMAND,
    SERVE_COMMAND,
    atlassian_callback_step,
    certificate_repair_steps,
    command,
    deployment_steps,
    flatten,
    note,
    pep440_to_semver,
    running_chart_version,
)

HTTPS_URL = "https://cremind.lan:1515"
ATLASSIAN = "Atlassian developer console"
CHART_VERSION = "0.0.17-rc.9.dev.4"

# What ``runtime_env.kubernetes_identity()`` returns for a pod whose chart
# states all four names: the shape, key for key, that reaches these builders.
IDENTITY = {
    "namespace": "lee-cremind", "release": "cremind", "workload": "cremind",
    "service": "cremind", "service_port": 80, "source": "chart",
    "port_forward": "kubectl --namespace lee-cremind port-forward svc/cremind 1515:80",
}
# An older chart states nothing, so the pod guesses its namespace from the
# mounted service-account file and its Deployment from its own hostname. Helm's
# release name leaves no trace in a pod, so it stays unknown.
INFERRED = {
    "namespace": "lee-cremind", "release": None, "workload": "lee-cremind",
    "service": "lee-cremind", "service_port": 80, "source": "inferred",
    "port_forward":
        "kubectl --namespace lee-cremind port-forward svc/lee-cremind 1515:80",
}
# A StatefulSet or bare pod: the hostname does not parse, so only the namespace
# is known and the Deployment name is still the operator's to supply.
NAMESPACE_ONLY = {
    "namespace": "lee-cremind", "release": None, "workload": None,
    "service": None, "service_port": 80, "source": "inferred",
    "port_forward": None,
}


def _steps(**overrides) -> list[dict]:
    kwargs = {
        "manager": "external", "install_mode": "kubernetes", "edge": False,
        "restart_supported": True, "activating": False, "https_url": HTTPS_URL,
        "chart_version": CHART_VERSION,
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
                "helm list --all-namespaces",
                "kubectl --namespace <namespace> create secret tls cremind-tls "
                "--cert=<path-to-fullchain.pem> --key=<path-to-privkey.pem>",
                f"helm upgrade <release> {CHART_REFERENCE} --version {CHART_VERSION} "
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
                "helm list --all-namespaces",
                f"helm upgrade <release> {CHART_REFERENCE} --version {CHART_VERSION} "
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


@pytest.mark.parametrize("kwargs", [{"edge": True}, {"install_mode": "kubernetes"}])
def test_helm_commands_carry_the_chart_source_and_version(kwargs):
    """Both are knowable from here, so the operator never looks them up."""
    upgrade = next(
        s["text"] for s in _steps(**kwargs)
        if s["kind"] == "command" and s["text"].startswith("helm upgrade")
    )
    assert f"{CHART_REFERENCE} --version {CHART_VERSION}" in upgrade
    assert "<chart>" not in upgrade and "<chart-version>" not in upgrade
    # Only what is genuinely local to the install is left to fill in.
    assert "<release>" in upgrade and "<namespace>" in upgrade


@pytest.mark.parametrize("kwargs", [{"edge": True}, {"install_mode": "kubernetes"}])
def test_the_first_helm_command_does_not_need_the_namespace_it_reports(kwargs):
    """`helm list --namespace <ns>` was circular: it is how you FIND the ns."""
    first = next(s["text"] for s in _steps(**kwargs) if s["kind"] == "command")
    assert first == "helm list --all-namespaces"


@pytest.mark.parametrize("kwargs", [{"edge": True}, {"install_mode": "kubernetes"}])
def test_the_pre_filled_chart_carries_its_escape_hatches(kwargs):
    """A pinned version, a checkout install and a mismatched chart all differ
    from the common case, and silently guessing wrong is worse than a note."""
    joined = " ".join(flatten(_steps(**kwargs)))
    assert CHART_VERSION in joined
    assert "--version pin" in joined and "calls latest" in joined
    assert "checkout" in joined and "helm list reports" in joined


def test_the_chart_version_is_the_semver_spelling_of_this_build():
    from app.__version__ import __version__

    assert running_chart_version() == pep440_to_semver(__version__)


@pytest.mark.parametrize(
    ("pep440", "semver"),
    [
        ("0.0.17rc9.dev4", "0.0.17-rc.9.dev.4"),
        ("0.0.17rc9", "0.0.17-rc.9"),
        ("0.0.2.1rc1.dev1", "0.0.2.1-rc.1.dev.1"),
        ("0.0.16", "0.0.16"),          # a stable version is already SemVer2
        ("0.0.17.dev1", "0.0.17.dev1"),  # not an RC shape: passed through
    ],
)
def test_pep440_to_semver_matches_the_release_workflows_translation(pep440, semver):
    """Helm rejects the PEP 440 spelling, so a drift here would print a chart
    version that does not resolve. The release workflow stamps the chart with
    scripts/sync_ui_version.py, which the wheel cannot import — hence the copy."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    try:
        from sync_ui_version import pep440_to_semver as canonical
    finally:
        sys.path.pop(0)

    assert pep440_to_semver(pep440) == semver
    assert canonical(pep440) == pep440_to_semver(pep440)


def test_docker_names_the_installers_own_compose_folder():
    """A Docker install leaves ~/.cremind untouched; the bundle lives elsewhere."""
    first = _steps(install_mode="docker")[0]["text"]
    assert "~/.local/share/cremind/docker" in first
    assert "~/.cremind/docker" not in first


# --- the pod that can name itself -------------------------------------
#
# A placeholder is work the operator has to do by hand, and getting it wrong
# ("services 'cremind' not found") looks like a broken cluster rather than a
# runbook that guessed. Where the chart states the names, every command must
# be paste-and-run.


def test_a_pod_that_knows_itself_prints_commands_with_nothing_left_to_fill_in():
    steps = _steps(kubernetes=IDENTITY)
    assert _commands(steps) == [
        f"helm upgrade cremind {CHART_REFERENCE} --version {CHART_VERSION} "
        "--namespace lee-cremind --reuse-values --set cremind.ssl=auto",
        "kubectl --namespace lee-cremind rollout status "
        "deployment/cremind --timeout=5m",
        "kubectl --namespace lee-cremind port-forward svc/cremind 1515:80",
    ]
    joined = " ".join(flatten(steps))
    # No blanks anywhere, so no "replace these with what helm list prints".
    assert "<" not in joined
    assert "helm list" not in " ".join(_commands(steps))
    assert "lee-cremind" in [s["text"] for s in steps if s["kind"] == "note"][2]


def test_an_ingress_runbook_names_the_real_ingress_not_the_release():
    """The Ingress carries the fullname; only the release goes to helm."""
    steps = _steps(edge=True, kubernetes=IDENTITY)
    assert _commands(steps) == [
        "kubectl --namespace lee-cremind create secret tls cremind-tls "
        "--cert=<path-to-fullchain.pem> --key=<path-to-privkey.pem>",
        f"helm upgrade cremind {CHART_REFERENCE} --version {CHART_VERSION} "
        "--namespace lee-cremind --reuse-values -f <your-values.yaml>",
        "kubectl --namespace lee-cremind rollout status "
        "deployment/cremind --timeout=5m",
        "kubectl --namespace lee-cremind get ingress cremind",
        f"curl --fail {HTTPS_URL}/api/tls/status",
    ]
    # The certificate paths and the values file are genuinely the operator's;
    # the cluster's own names are not.
    assert NAMESPACE_PLACEHOLDER not in " ".join(flatten(steps))
    assert RELEASE_PLACEHOLDER not in " ".join(flatten(steps))


@pytest.mark.parametrize("kwargs", [{}, {"edge": True}])
def test_an_inferred_identity_keeps_helm_list_and_says_where_the_name_came_from(kwargs):
    """The pod guessed its Deployment from its hostname and cannot know the
    release, so the one command that maps the two must stay."""
    steps = _steps(kubernetes=INFERRED, **kwargs)
    commands = _commands(steps)
    assert commands[0] == "helm list --all-namespaces"
    upgrade = next(c for c in commands if c.startswith("helm upgrade"))
    assert upgrade.startswith(f"helm upgrade {RELEASE_PLACEHOLDER} ")
    assert "--namespace lee-cremind" in upgrade
    assert any("deployment/lee-cremind" in c for c in commands)
    joined = " ".join(_notes(steps))
    assert "inferred from this pod's own name" in joined
    assert "(lee-cremind)" in joined
    # That caveat is about a name we printed, not about deriving one.
    assert "-cremind when the release name does not contain cremind" not in joined


def test_a_pod_that_knows_only_its_namespace_still_derives_the_deployment():
    """Workload falls back to the release, which is the rule the prose states."""
    steps = _steps(kubernetes=NAMESPACE_ONLY)
    commands = _commands(steps)
    assert commands[0] == "helm list --all-namespaces"
    assert (
        f"kubectl --namespace lee-cremind rollout status "
        f"deployment/{RELEASE_PLACEHOLDER} --timeout=5m" in commands
    )
    assert (
        "The Deployment and Service carry the release name"
        in " ".join(_notes(steps))
    )


def test_a_custom_service_port_reaches_the_tunnel():
    identity = {**IDENTITY, "service_port": 8080, "port_forward": None}
    tunnel = next(c for c in _commands(_steps(kubernetes=identity))
                  if "port-forward" in c)
    assert tunnel == "kubectl --namespace lee-cremind port-forward svc/cremind 1515:8080"


@pytest.mark.parametrize(
    "kubernetes", [None, IDENTITY, INFERRED, NAMESPACE_ONLY],
    ids=["unknown", "chart", "inferred", "namespace-only"],
)
@pytest.mark.parametrize("kwargs", [{}, {"edge": True}])
def test_a_named_runbook_is_still_a_well_formed_runbook(kubernetes, kwargs):
    """Every name is interpolated into a copy button's payload, so the step
    contract has to hold for the real names exactly as it does for blanks."""
    steps = _steps(kubernetes=kubernetes, **kwargs)
    for step in steps:
        assert step["text"].strip() == step["text"] and "`" not in step["text"]
        if step["kind"] == "command":
            assert "\n" not in step["text"] and not step["text"].endswith(".")
            assert not step["text"].startswith("Run ")
    assert steps[0]["kind"] == "note" and steps[-1]["kind"] == "note"


def test_the_tunnel_is_spelled_by_runtime_env_so_the_two_cannot_drift():
    """The chart's NOTES, the VNC card and this runbook print one command."""
    tunnel = next(c for c in _commands(_steps(kubernetes=IDENTITY))
                  if "port-forward" in c)
    assert tunnel == runtime_env.kubernetes_port_forward(
        "lee-cremind", "cremind", runtime_env.PORT_FORWARD_LOCAL_PORT, 80,
    )
    assert tunnel == IDENTITY["port_forward"]


def test_the_placeholders_are_the_ones_runtime_env_prints():
    """Two vocabularies for one unknown would read as two different unknowns.

    ``runtime_env`` spells the workload blank ``<release>`` too, because that
    is what the operator types into ``helm upgrade``.
    """
    assert NAMESPACE_PLACEHOLDER == runtime_env._NAMESPACE_PLACEHOLDER
    assert RELEASE_PLACEHOLDER == runtime_env._WORKLOAD_PLACEHOLDER


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


def test_certificate_repair_restarts_the_deployment_by_its_real_name():
    steps = certificate_repair_steps(
        manager="external", install_mode="kubernetes",
        restart_supported=True, kubernetes=IDENTITY,
    )
    assert _commands(steps) == [
        "kubectl --namespace lee-cremind rollout restart deployment/cremind",
        "kubectl --namespace lee-cremind rollout status "
        "deployment/cremind --timeout=5m",
    ]


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
