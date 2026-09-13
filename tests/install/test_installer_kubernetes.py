"""Parity + contract guards for the installers' kubernetes mode.

The two installers are hand-maintained mirrors of each other, and nothing
executes either one in CI (they reach real clusters). So the couplings
asserted here are exactly the ones that rot silently:

- **the wrong-cluster guarantee**: every helm and kubectl invocation in the
  kubernetes branch carries an explicit context, so a stale
  ``current-context`` can never redirect an install. One forgotten flag and
  a release lands in production instead of staging, which no amount of
  re-running undoes;
- the ``requires`` gate is really read by both shells — the catalog has
  emitted those variables for a long time with nobody consuming them;
- every new flag exists, is documented, and is forwarded to the TUI in
  *both* of install.sh's launch blocks. install.ps1's read-back is a switch
  whitelist, where a missing case drops the answer with no error;
- the release record is read, never sourced: it carries two secrets and
  free-form ``--set`` text;
- the refusals that protect the operator stay in place (the production
  chart/image tag collision, the retained-Postgres-volume password).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"
TUI = REPO_ROOT / "app" / "installer" / "tui.py"
OUTPUT_PY = REPO_ROOT / "app" / "installer" / "output.py"
MAIN_PY = REPO_ROOT / "app" / "installer" / "__main__.py"

#: Every kubernetes answer the TUI can produce, as (output key, sh flag, ps
#: parameter). The TUI's output file is SOURCED by install.sh, so each key
#: must be round-tripped: the shell passes its current value in, the TUI
#: echoes it back, and sourcing is a no-op for a flag-supplied answer.
ANSWER_KEYS = [
    ("KUBE_CONTEXT", "--kube-context", "KubeContext"),
    ("KUBE_NAMESPACE", "--kube-namespace", "KubeNamespace"),
    ("K8S_release_name", "--k8s-release-name", "K8sReleaseName"),
    ("K8S_app_url", "--k8s-app-url", "K8sAppUrl"),
    ("K8S_legacy_postgres_image", "--k8s-legacy-postgres-image", "K8sLegacyPostgresImage"),
    ("K8S_delete_postgres_data", "--k8s-delete-postgres-data", "K8sDeletePostgresData"),
    ("K8S_extra_set", "--k8s-extra-set", "K8sExtraSet"),
]

#: Context the shell probes and hands to the TUI, which never writes it back.
CONTEXT_FLAGS = [
    "--has-kubectl",
    "--has-helm",
    "--kube-contexts-file",
    "--kube-current-context",
]


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


#: Shell helpers that print text. A line starting with one of these is
#: output, not an invocation, even when it mentions helm or kubectl.
_SH_PRINTERS = ("printf", "echo", "info", "warn", "err", "ok", "step", "cat")


def _sh_kubernetes_branch() -> str:
    """install.sh's kubernetes install branch, up to the docker one."""
    sh = _sh()
    start = sh.index('step "Kubernetes install"')
    end = sh.index('if [ "$MODE" = "docker" ]; then\n    step "Docker install"', start)
    return sh[start:end]


def _sh_code_lines(text: str) -> list[str]:
    """Lines that run something: no comments, no printed output."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.split(" ", 1)[0].rstrip("(") in _SH_PRINTERS:
            continue
        out.append(line)
    return out


def _sh_tui_launch() -> str:
    """The TUI bootstrap function, where the flags are forwarded twice."""
    sh = _sh()
    start = sh.index("tui_run_bootstrap() {")
    end = sh.index("\ntui_run_bootstrap\n", start)
    return sh[start:end]


def _ps1_kubernetes_branch() -> str:
    ps1 = _ps1()
    start = ps1.index('Write-Step "Kubernetes install"')
    end = ps1.index("if ($Mode -eq 'docker') {\n    Write-Step \"Docker install\"", start)
    return ps1[start:end]


# ── the mode itself ───────────────────────────────────────────────────────


def test_mode_is_selectable_in_both_installers() -> None:
    assert "[ValidateSet('','docker','native','kubernetes')] [string] $Mode" in _ps1()
    sh = _sh()
    assert "--kubernetes)" in sh  # the convenience alias
    assert "--mode docker|native|kubernetes" in sh.split("set -e", 1)[0]


def test_both_shells_consume_the_catalog_requires_gate() -> None:
    """``requires`` is a visibility gate, not documentation.

    The catalog has emitted MODE_REQUIRES_* / .Requires for a long time with
    nobody reading them; this asserts both shells now do, and that the old
    hardcoded "prompt only when Docker is present" gate is gone.
    """
    sh, ps1 = _sh(), _ps1()
    assert "MODE_REQUIRES_$1" in sh and "AVAILABLE_MODE_IDS" in sh
    assert "$script:Modes[$Id].Requires" in ps1 and "$AvailableModeIds" in ps1
    # The old gate forced native whenever Docker was missing, which would have
    # hidden kubernetes on a kubectl-only workstation.
    assert 'if [ "$HAS_DOCKER" -eq 1 ]; then\n        if [ "$UNATTENDED" -eq 1 ]' not in sh
    assert "if (-not $Mode) {\n    if ($HasDocker) {" not in ps1


def test_an_unknown_or_unavailable_mode_is_refused() -> None:
    """install.sh used to accept any --mode and fall through to native."""
    sh = _sh()
    assert "Unknown mode: $MODE (must be one of: $MODE_IDS)" in sh
    assert 'mode_available "$MODE"' in sh
    ps1 = _ps1()
    assert "Test-ModeAvailable $Mode" in ps1


def test_the_mode_question_precedes_the_deployment_questions() -> None:
    """It decides whether they are asked at all — kubernetes has no host to
    bind, so the chart answers those on the pod."""
    sh = _sh()
    assert sh.index('ok "Mode: $MODE"') < sh.index('step "Deployment"')
    assert 'if [ "$MODE" != "kubernetes" ]; then\n\nstep "Deployment"' in sh
    ps1 = _ps1()
    assert ps1.index('Write-Ok "Mode: $Mode"') < ps1.index('Write-Step "Deployment"')
    assert "if ($Mode -ne 'kubernetes') {\n\nWrite-Step \"Deployment\"" in ps1


# ── the wrong-cluster guarantee ───────────────────────────────────────────


def test_every_cluster_call_names_its_context_in_sh() -> None:
    """The whole point of the context picker. install.sh routes kubectl
    through ``kc``/``kcn`` (which add --context) and spells --kube-context on
    every helm call; a bare ``kubectl``/``helm`` would use whatever
    current-context happens to be."""
    branch = _sh_kubernetes_branch()
    assert 'kubectl --context "$KUBE_CONTEXT" --request-timeout=20s "$@"' in branch

    for line in _sh_code_lines(branch):
        for tool in ("kubectl ", "helm "):
            if not re.search(rf"(^|[|&;(\s]){tool}", line):
                continue
            if tool == "kubectl " and "--context" in line:
                continue
            if tool == "helm " and (
                "--kube-context" in line
                # Registry and chart work never touches a cluster.
                or "helm repo add" in line
                or "helm dependency build" in line
                or "helm show chart" in line
                # The assembled argv, built line by line just above.
                or 'helm "$@"' in line
            ):
                continue
            raise AssertionError(f"cluster call without an explicit context: {line}")


def test_every_cluster_call_names_its_context_in_ps1() -> None:
    branch = _ps1_kubernetes_branch()
    assert "-ArgumentList (@('--context', $KubeContext, '--request-timeout=20s')" in branch
    for match in re.finditer(r"'(helm|uninstall|upgrade|status)'[^\n]*", branch):
        line = match.group(0)
        if "'helm'" in line and "-ArgumentList" in line and "status" in line:
            assert "--kube-context" in line or "$KubeContext" in line


def test_the_context_picker_never_falls_back_to_current_context() -> None:
    """Unattended runs with several contexts must refuse, not guess: a stale
    current-context would silently install into the wrong cluster."""
    sh, ps1 = _sh(), _ps1()
    assert "--kube-context is required: the kubeconfig has $KUBE_CONTEXT_COUNT contexts" in sh
    assert "-KubeContext is required: the kubeconfig has $($KubeContexts.Count) contexts" in ps1
    # A single context is unambiguous and is used without asking.
    assert '[ "$KUBE_CONTEXT_COUNT" -eq 1 ]' in sh
    assert "$KubeContexts.Count -eq 1" in ps1


def test_a_context_flag_is_validated_against_the_kubeconfig() -> None:
    assert "Unknown kubeconfig context: $KUBE_CONTEXT" in _sh()
    assert "Unknown kubeconfig context: $KubeContext" in _ps1()


def test_the_text_path_confirms_the_cluster() -> None:
    """The TUI has a confirm screen; the text fallback needs its own."""
    assert "Install into this cluster? [y/N]" in _sh()
    assert "Install into this cluster? [y/N]" in _ps1()


def test_re_running_against_a_different_target_is_refused() -> None:
    """Otherwise the previously tracked release keeps running with nothing
    recording where it is."""
    assert "This machine already tracks a Cremind release:" in _sh()
    assert "This machine already tracks a Cremind release:" in _ps1()


# ── flags ─────────────────────────────────────────────────────────────────


def test_every_flag_is_declared_in_both_installers() -> None:
    sh, ps1 = _sh(), _ps1()
    for _key, flag, param in ANSWER_KEYS:
        assert f"{flag})" in sh and f"{flag}=*)" in sh, flag
        assert f"${param} = ''" in ps1, param
    for flag, param in [
        ("--k8s-postgres-password", "K8sPostgresPassword"),
        ("--helm-chart", "HelmChart"),
    ]:
        assert f"{flag})" in sh and f"{flag}=*)" in sh, flag
        assert f"${param} = ''" in ps1, param
    assert "--no-port-forward)" in sh
    assert "[switch] $NoPortForward" in ps1


def test_every_flag_is_documented() -> None:
    header = _sh().split("set -e", 1)[0]
    ps1 = _ps1()
    for _key, flag, param in ANSWER_KEYS:
        assert flag in header, flag
        assert f".PARAMETER {param}" in ps1, param


def test_both_tui_launch_blocks_forward_every_answer() -> None:
    """install.sh launches the TUI from two places (a checkout's .venv and a
    private uv). A flag forwarded in only one re-asks a question the operator
    already answered."""
    launch, ps1 = _sh_tui_launch(), _ps1()
    for key, flag, _param in ANSWER_KEYS:
        assert launch.count(f'{flag} "${key}"') == 2, flag
        assert f"$tuiArgs.Add('{flag}')" in ps1, flag
    for flag in CONTEXT_FLAGS:
        assert launch.count(f"{flag} ") == 2, flag
        assert f"$tuiArgs.Add('{flag}')" in ps1, flag


def test_powershell_reads_every_answer_back() -> None:
    """The read-back is a switch WHITELIST — an absent case silently discards
    the TUI's answer with no error anywhere."""
    ps1 = _ps1()
    for key, _flag, param in ANSWER_KEYS:
        assert re.search(
            rf"'{re.escape(key)}'\s*\{{[^}}]*Set-Variable -Scope Script {param}\b", ps1
        ), key


def test_the_answer_keys_are_the_tui_output_keys() -> None:
    out = OUTPUT_PY.read_text(encoding="utf-8")
    for key, _flag, _param in ANSWER_KEYS:
        assert f'"{key}"' in out, key
    main = MAIN_PY.read_text(encoding="utf-8")
    for _key, flag, _param in ANSWER_KEYS:
        assert f'"{flag}"' in main, flag


def test_the_namespace_rule_is_the_same_in_all_three_front_ends() -> None:
    """One RFC 1123 rule, three hand-written copies, no compiler to notice."""
    expected = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
    assert f"KUBE_NAME_RE='{expected}'" in _sh()
    assert f"$KubeNameRe = '{expected}'" in _ps1()
    assert f'KUBE_NAME_PATTERN = r"{expected}"' in TUI.read_text(encoding="utf-8")


# ── the helm contract ─────────────────────────────────────────────────────


def test_secrets_ride_a_values_file_not_the_command_line() -> None:
    """helm's --set parser splits on commas — which the VNC charset contains
    — and a command line is visible in the process list and the install log."""
    for branch in (_sh_kubernetes_branch(), _ps1_kubernetes_branch()):
        assert "values.yaml" in branch
        assert "vncPassword" in branch and "postgresql" in branch
        assert "--set-string" not in branch


def test_the_helm_invocation_omits_the_dangerous_flags() -> None:
    """--wait hides a Pending Postgres volume behind a generic timeout;
    --atomic would roll back a slow first install and take the chart-owned
    PVCs with it; --reuse-values would silently carry stale values forward;
    --devel is a no-op next to an exact --version and misleads anyone who
    copies the printed command."""
    # Slice the argv assembly itself, not the surrounding prose: an error
    # message may legitimately suggest `helm show chart ... --devel`.
    sh_branch = _sh_kubernetes_branch()
    sh_argv = sh_branch[
        sh_branch.index("set -- upgrade --install") : sh_branch.index('if ! helm "$@"')
    ]
    ps1_branch = _ps1_kubernetes_branch()
    ps1_argv = ps1_branch[
        ps1_branch.index("$helmArgs = ") : ps1_branch.index("$installed = Invoke-NativeCapture")
    ]
    for argv in (sh_argv, ps1_argv):
        assert "upgrade" in argv and "--install" in argv
        for flag in ("--atomic", "--reuse-values", "--devel", "--wait"):
            assert flag not in argv, flag
    # --wait IS used on the uninstall a --reinstall runs first, where waiting
    # for the teardown is exactly what we want.
    assert "--wait" in sh_branch and "--wait" in ps1_branch


def test_extra_set_is_applied_last() -> None:
    """So an operator's --set overrides the installer's own values."""
    sh = _sh_kubernetes_branch()
    assert sh.index('-f "$K8S_VALUES_FILE"') < sh.index('--set "$K8S_extra_set"')
    ps1 = _ps1_kubernetes_branch()
    assert ps1.index("$helmArgs.Add($K8sValuesFile)") < ps1.index("$helmArgs.Add($K8sExtraSet)")


def test_the_workload_name_is_discovered_not_computed() -> None:
    """A nameOverride/fullnameOverride passed through --k8s-extra-set would
    otherwise break the rollout wait and the port-forward silently."""
    for branch in (_sh_kubernetes_branch(), _ps1_kubernetes_branch()):
        assert "app.kubernetes.io/instance=" in branch
        assert "jsonpath={.items[0].metadata.name}" in branch


def test_the_app_url_is_left_to_the_chart_when_blank() -> None:
    """The chart derives http(s)://localhost:1515 itself, and an explicit
    http:// value is exactly what makes a later --ssl run fail its own
    validation."""
    for branch in (_sh_kubernetes_branch(), _ps1_kubernetes_branch()):
        assert "appUrl" in branch
    assert '[ -n "$K8S_APP_URL" ] && printf \'  appUrl:' in _sh_kubernetes_branch()
    assert "if ($K8sAppUrl) { $valuesLines.Add(\"  appUrl:" in _ps1_kubernetes_branch()


# ── refusals ──────────────────────────────────────────────────────────────


def test_production_plus_basic_image_is_refused() -> None:
    """On the production channel `cremind/cremind:<version>` on Docker Hub is
    the Helm chart artifact, not an image — the helm job pushes it to the tag
    the docker job just used. A headless production install could never pull."""
    for text in (_sh(), _ps1()):
        assert "A production Kubernetes install cannot use the basic (headless) image." in text
        assert "chart artifact, not a container image" in text


def test_a_retained_postgres_volume_is_refused_not_overwritten() -> None:
    """A fresh pin would write a password the retained database rejects, and
    setup would fail with "password authentication failed" much later."""
    assert "survives from an earlier release" in _sh()
    assert "survives from an earlier release" in _ps1()
    # The escape hatch is named, in each script's own spelling.
    assert "--k8s-postgres-password <the old password>" in _sh()
    assert "-K8sPostgresPassword <the old password>" in _ps1()


def test_an_existing_release_adopts_its_postgres_password() -> None:
    """Pinning a NEW password onto an existing release rewrites the Secret
    and locks the app out of its own database (the Bitnami helper honours a
    provided value)."""
    for branch in (_sh_kubernetes_branch(), _ps1_kubernetes_branch()):
        assert "Adopted the existing PostgreSQL password from the release." in branch


def test_chart_rejections_are_caught_before_helm_runs() -> None:
    """A Go template error several minutes into an install is a bad way to
    learn that two values are mutually exclusive."""
    for text in (_sh(), _ps1()):
        assert "ingress.enabled" in text and "mutually exclusive" in text
        assert "CREMIND_DB_PROVIDER must not be set on Kubernetes" in text
        assert "exactly one pod" in text


# ── state ─────────────────────────────────────────────────────────────────


def test_the_release_record_is_read_never_sourced() -> None:
    """It carries the Postgres and VNC passwords and free-form --set text;
    sourcing it would let any of them become shell code."""
    sh = _sh()
    assert 'sed -n "s/^$1=//p" "$K8S_RELEASE_ENV"' in sh
    assert '. "$K8S_RELEASE_ENV"' not in sh
    assert ". $K8S_RELEASE_ENV" not in sh
    ps1 = _ps1()
    assert "'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$'" in ps1
    assert ". $K8sReleaseEnv" not in ps1


def test_the_tls_key_is_named_so_the_existing_chain_reads_it() -> None:
    """Naming it CREMIND_SSL is what lets the previous-choice machinery read
    this file with no special case."""
    assert "printf 'CREMIND_SSL=%s\\n' \"$SSL_MODE\"" in _sh()
    assert '$releaseLines.Add("CREMIND_SSL=$SslMode")' in _ps1()
    assert 'PREVIOUS_SSL_ENV="$K8S_RELEASE_ENV"' in _sh()
    assert "$K8sReleaseEnv" in _ps1()


def test_credentials_record_the_kubernetes_install_mode() -> None:
    for branch in (_sh_kubernetes_branch(), _ps1_kubernetes_branch()):
        assert 'install_mode = "kubernetes"' in branch
        assert "[kubernetes]" in branch


def test_no_boot_service_for_kubernetes() -> None:
    """kubelet supervises the pod; a host service would supervise nothing."""
    assert 'elif [ "$MODE" = "kubernetes" ]; then' in _sh()
    assert "kubelet restarts the pod for you" in _sh()
    assert "kubelet restarts the pod for you" in _ps1()


# ── uninstall ─────────────────────────────────────────────────────────────


def test_uninstall_removes_the_release_with_an_explicit_context() -> None:
    sh, ps1 = _sh(), _ps1()
    assert 'helm uninstall "$K8S_REL" --kube-context "$K8S_CTX"' in sh
    assert "'uninstall', $K8sRel, '--kube-context', $K8sCtx" in ps1


def test_uninstall_keep_preserves_the_release_record() -> None:
    """--keep leaves the PostgreSQL volume in the cluster; without the
    recorded password that volume is unusable forever after."""
    assert "so the retained PostgreSQL volume stays usable" in _sh()
    assert "so the retained PostgreSQL volume stays usable" in _ps1()


def test_only_purge_deletes_cluster_volumes_and_only_our_namespace() -> None:
    sh, ps1 = _sh(), _ps1()
    assert 'if [ "$UNINSTALL_MODE" = "purge" ]; then\n                # The bundled' in sh
    assert '[ "$K8S_NS_CREATED" = "1" ]' in sh
    assert "$K8sNsCreated -eq '1'" in ps1
    # Volumes are found by label: their names follow the subchart's pinned
    # fullname (cremind-postgresql), not the Helm release name.
    assert "app.kubernetes.io/name=$_sub" in sh
    assert 'app.kubernetes.io/name=$sub' in ps1
