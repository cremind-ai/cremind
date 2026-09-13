"""Render the chart the way the installer's kubernetes mode drives it.

install.sh and install.ps1 write a values file and hand it to
``helm upgrade --install``. Nothing in CI runs either script against a
cluster, so what is asserted here is the other half of that contract: that
the values they write actually produce the manifests they promise, and that
the combinations they refuse up front are the ones the chart would reject
anyway.

``installer_helm_args`` below is the executable spelling of the mapping table
in install/README.md. When an installer's values change, change it here too —
a test that renders something the installer never sends proves nothing.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
HELM = shutil.which("helm")
CHARTS = ROOT / "helm" / "cremind" / "charts"
pytestmark = pytest.mark.skipif(
    not HELM or not CHARTS.is_dir(),
    reason="needs helm and `helm dependency build helm/cremind`",
)


def installer_values(
    *,
    desktop: bool = True,
    vnc_password: str = "abc12345",
    ssl: str = "none",
    app_url: str = "",
    image_tag: str = "",
    legacy_postgres_image: bool = True,
    delete_postgres_data: bool = False,
    postgres_password: str = "pinned-by-the-installer",
    external_postgres: bool = False,
) -> dict:
    """The values file the installer writes, as a dict.

    A file rather than ``--set`` pairs because that is what the scripts do,
    and the difference matters: ``--set-string desktop.enabled=false`` yields
    the *string* "false", which Helm's ``if`` reads as true, and a trailing
    ``--set`` overrides a file but not an earlier ``--set-string``.
    """
    values: dict = {
        "desktop": {"enabled": desktop},
        "cremind": {"ssl": ssl},
    }
    if desktop:
        values["cremind"]["vncPassword"] = vnc_password
    if app_url:
        values["cremind"]["appUrl"] = app_url
    if image_tag:
        values["image"] = {"tag": image_tag}
    if external_postgres:
        values["postgresql"] = {"enabled": False}
    else:
        postgres: dict = {"auth": {"password": postgres_password}}
        if legacy_postgres_image:
            postgres["image"] = {
                "registry": "docker.io",
                "repository": "bitnamilegacy/postgresql",
            }
        if delete_postgres_data:
            postgres["primary"] = {
                "persistentVolumeClaimRetentionPolicy": {
                    "enabled": True,
                    "whenDeleted": "Delete",
                }
            }
        values["postgresql"] = postgres
    return values


def _render(
    release: str = "cremind",
    namespace: str = "cremind",
    *,
    extra_set: tuple[str, ...] = (),
    succeeds: bool = True,
    tmp_path: Path | None = None,
    **inputs,
):
    """``helm template`` with the installer's own values. No fullnameOverride:
    the installer never sets one, and the names are part of what is asserted."""
    import tempfile

    directory = tmp_path or Path(tempfile.mkdtemp())
    values_file = directory / "values.yaml"
    values_file.write_text(yaml.safe_dump(installer_values(**inputs)), encoding="utf-8")

    command = [HELM, "template", release, str(ROOT / "helm/cremind"),
               "--namespace", namespace, "-f", str(values_file)]
    # extra_set is applied last, exactly as the installer applies it — and a
    # --set always beats a -f, which is what makes it an override.
    for value in extra_set:
        command.extend(["--set", value])
    result = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    if not succeeds:
        assert result.returncode != 0, result.stdout
        return result.stderr
    assert result.returncode == 0, result.stderr
    docs = [d for d in yaml.safe_load_all(result.stdout) if d]
    return {(d["kind"], d["metadata"]["name"]): d for d in docs}


def _one(rendered: dict, kind: str) -> dict:
    matches = [d for (k, _n), d in rendered.items() if k == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


# ── the default install ───────────────────────────────────────────────────


def test_the_test_channel_defaults_render() -> None:
    """What `--mode kubernetes --channel test` produces with no other flags."""
    rendered = _render(image_tag="0.0.17rc16.dev6")

    deployment = rendered[("Deployment", "cremind")]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "docker.io/cremind/cremind-desktop:0.0.17rc16.dev6"

    env = rendered[("ConfigMap", "cremind-env")]["data"]
    assert env["INSTALL_MODE"] == "kubernetes"
    assert env["SETUP_WIZARD_ENV"] == "kubernetes"
    assert env["VNC_PASSWORD"] == "abc12345"
    assert env["APP_URL"] == "http://localhost:1515"
    assert env["CREMIND_NOVNC_URL"] == "http://localhost:1515/vnc/vnc.html"
    # An RC image tag is what makes the pod report the test channel in-app.
    assert env["CREMIND_UPGRADE_CHANNEL"] == "test"
    # Never set by the installer: it would skip the Setup Wizard entirely.
    assert "CREMIND_DB_PROVIDER" not in env

    service = rendered[("Service", "cremind")]
    assert {p["port"] for p in service["spec"]["ports"]} == {80, 1455}


def test_a_production_image_tag_reports_the_production_channel() -> None:
    rendered = _render(image_tag="0.0.17")
    env = rendered[("ConfigMap", "cremind-env")]["data"]
    assert env["CREMIND_UPGRADE_CHANNEL"] == "production"


def test_a_local_chart_without_an_image_tag_renders_the_placeholder() -> None:
    """Chart.yaml ships appVersion 0.0.0 and CI stamps the real one, so a
    checkout's chart MUST be given an explicit image.tag — which is why the
    installer pins it on the dev channel."""
    rendered = _render()
    container = rendered[("Deployment", "cremind")]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].endswith(":0.0.0")


def test_the_basic_flavor_drops_the_desktop_pieces() -> None:
    rendered = _render(desktop=False, image_tag="0.0.17rc16.dev6")
    container = rendered[("Deployment", "cremind")]["spec"]["template"]["spec"]["containers"][0]
    assert container["image"] == "docker.io/cremind/cremind:0.0.17rc16.dev6"
    env = rendered[("ConfigMap", "cremind-env")]["data"]
    assert "VNC_PASSWORD" not in env and "RESOLUTION" not in env
    ports = {p["port"] for p in rendered[("Service", "cremind")]["spec"]["ports"]}
    assert 6080 not in ports


# ── the values the user's original command carried ────────────────────────


def test_the_legacy_postgres_image_toggle() -> None:
    """Bitnami froze its free images into the bitnamilegacy namespace, so the
    chart's own default no longer pulls. The installer defaults this on."""
    sts = _render()[("StatefulSet", "cremind-postgresql")]
    image = sts["spec"]["template"]["spec"]["containers"][0]["image"]
    assert image.startswith("docker.io/bitnamilegacy/postgresql:")

    sts = _render(legacy_postgres_image=False)[("StatefulSet", "cremind-postgresql")]
    image = sts["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "bitnamilegacy" not in image


def test_the_postgres_volume_retention_toggle() -> None:
    sts = _render(delete_postgres_data=True)[("StatefulSet", "cremind-postgresql")]
    assert sts["spec"]["persistentVolumeClaimRetentionPolicy"] == {
        "whenDeleted": "Delete",
        "whenScaled": "Retain",
    }
    sts = _render(delete_postgres_data=False)[("StatefulSet", "cremind-postgresql")]
    policy = sts["spec"].get("persistentVolumeClaimRetentionPolicy") or {}
    assert policy.get("whenDeleted", "Retain") == "Retain"


@pytest.mark.parametrize("release", ["cremind", "lee", "lee-cremind"])
def test_the_postgres_volume_name_ignores_the_release_name(release: str) -> None:
    """``--uninstall --purge`` must find this volume. The subchart pins its
    fullname, so the claim is data-cremind-postgresql-0 for EVERY release
    name — which is why the installers delete it by label, not by name."""
    rendered = _render(release=release)
    sts = rendered[("StatefulSet", "cremind-postgresql")]
    assert sts["spec"]["volumeClaimTemplates"][0]["metadata"]["name"] == "data"
    labels = sts["spec"]["template"]["metadata"]["labels"]
    assert labels["app.kubernetes.io/name"] == "postgresql"
    assert labels["app.kubernetes.io/instance"] == release


def test_the_pinned_postgres_password_reaches_the_secret_and_the_pod() -> None:
    """Pinning it is what lets an uninstall/reinstall cycle reuse a retained
    volume: the subchart's generated password would change every time."""
    rendered = _render(postgres_password="s3cret-pin")
    secret = rendered[("Secret", "cremind-postgresql")]
    assert base64.b64decode(secret["data"]["password"]).decode() == "s3cret-pin"

    container = rendered[("Deployment", "cremind")]["spec"]["template"]["spec"]["containers"][0]
    ref = next(
        e["valueFrom"]["secretKeyRef"]
        for e in container["env"]
        if e["name"] == "CREMIND_POSTGRES_PASSWORD"
    )
    assert ref == {"name": "cremind-postgresql", "key": "password"}


# ── names the installer discovers ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("release", "expected"),
    [("cremind", "cremind"), ("lee", "lee-cremind"), ("lee-cremind", "lee-cremind")],
)
def test_the_workload_name_follows_the_release(release: str, expected: str) -> None:
    rendered = _render(release=release)
    assert _one(rendered, "Deployment")["metadata"]["name"] == expected
    # The Service carries the same name; the installer port-forwards to it.
    assert ("Service", expected) in rendered


def test_a_fullname_override_wins_which_is_why_the_name_is_queried() -> None:
    """An operator's --k8s-extra-set can rename everything, so the installers
    ask the cluster for the Deployment name instead of recomputing the rule."""
    rendered = _render(extra_set=("fullnameOverride=renamed",))
    assert _one(rendered, "Deployment")["metadata"]["name"] == "renamed"


def test_every_object_carries_the_instance_label_the_installer_selects_on() -> None:
    rendered = _render(release="lee")
    deployment = _one(rendered, "Deployment")
    assert deployment["metadata"]["labels"]["app.kubernetes.io/instance"] == "lee"


# ── HTTPS ─────────────────────────────────────────────────────────────────


def test_after_setup_moves_novnc_and_derives_an_https_app_url() -> None:
    """The installer forwards 6080 only when the Service declares it, which
    is exactly this case: in-pod TLS turns the sidecar into an L4 relay, and
    the /vnc/ route lives in the L7 one."""
    rendered = _render(ssl="after-setup", image_tag="0.0.17rc16.dev6")
    env = rendered[("ConfigMap", "cremind-env")]["data"]
    assert env["CREMIND_SSL"] == "after-setup"
    assert env["APP_URL"] == "https://localhost:1515"
    assert env["CREMIND_NOVNC_URL"] == "http://localhost:6080/vnc.html"
    ports = {p["port"] for p in rendered[("Service", "cremind")]["spec"]["ports"]}
    assert ports == {80, 1455, 6080}


def test_plain_http_keeps_novnc_on_the_one_port() -> None:
    rendered = _render(ssl="none", image_tag="0.0.17rc16.dev6")
    ports = {p["port"] for p in rendered[("Service", "cremind")]["spec"]["ports"]}
    assert ports == {80, 1455}


# ── combinations the installer refuses before helm runs ───────────────────


@pytest.mark.parametrize(
    ("inputs", "extra_set", "expected"),
    [
        # The installer checks each of these itself so the operator gets a
        # sentence instead of a Go template error minutes into an install.
        ({"ssl": "after-setup", "app_url": "http://localhost:1515"}, (), "appUrl"),
        ({"ssl": "auto"}, ("ingress.enabled=true",), "mutually exclusive"),
        ({}, ("replicaCount=2",), "ONE pod"),
        ({}, ("cremind.extraEnv[0].name=CREMIND_DB_PROVIDER",), "CREMIND_DB_PROVIDER"),
    ],
)
def test_the_chart_rejects_what_the_installer_rejects(
    inputs: dict, extra_set: tuple[str, ...], expected: str
) -> None:
    stderr = _render(succeeds=False, extra_set=extra_set, **inputs)
    assert expected in stderr


def test_extra_set_is_applied_last_and_wins() -> None:
    """The contract for --k8s-extra-set: an operator override beats the
    installer's own value."""
    rendered = _render(
        legacy_postgres_image=True,
        extra_set=("postgresql.image.repository=bitnami/postgresql",),
    )
    sts = rendered[("StatefulSet", "cremind-postgresql")]
    image = sts["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "bitnamilegacy" not in image
    assert "bitnami/postgresql" in image


def test_an_external_postgres_drops_the_bundled_one() -> None:
    rendered = _render(external_postgres=True)
    assert ("StatefulSet", "cremind-postgresql") not in rendered
