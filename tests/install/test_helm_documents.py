"""Render what the chart tells a pod about Documentation search's storage.

Two things, both in the env ConfigMap:

* ``HF_HOME`` / ``SENTENCE_TRANSFORMERS_HOME`` put the local embedding models on
  the system PVC. The image already does that for the default system dir; the
  chart derives them from ``cremind.systemDir`` so a custom one takes the models
  along instead of leaving them in the pod filesystem, where every rollout would
  download them again.
* ``CREMIND_DB_CAPACITY`` / ``CREMIND_VECTORSTORE_CAPACITY`` hand the storage
  governor the sizes of the bundled PostgreSQL / vector store volumes, which the
  pod cannot measure from where it runs. Only for a subchart the release runs,
  and only when that subchart creates the claim at that size.

``helm upgrade --reuse-values`` renders the new templates against the previous
release's values, which predate the explicit sizes; the last tests render the
chart with those keys removed to prove the templates cope.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "helm" / "cremind"
HELM = shutil.which("helm")
pytestmark = pytest.mark.skipif(
    not HELM or not (CHART / "charts").is_dir(),
    reason="needs helm and `helm dependency build helm/cremind`",
)

CAPACITY_KEYS = ("CREMIND_DB_CAPACITY", "CREMIND_VECTORSTORE_CAPACITY")


def _render(*values: str, chart: Path = CHART, set_strings: tuple[str, ...] = ()) -> dict:
    """``helm template`` → ``{"env": <ConfigMap data>, "claims": {sts: size}}``."""
    command = [HELM, "template", "documents-test", str(chart), "--set", "fullnameOverride=cremind"]
    for value in values:
        command.extend(["--set", value])
    for value in set_strings:
        command.extend(["--set-string", value])
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    resources = [document for document in yaml.safe_load_all(result.stdout) if document]
    env = next(d["data"] for d in resources
               if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "cremind-env")
    claims = {
        d["metadata"]["name"]: d["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"]
        for d in resources
        if d["kind"] == "StatefulSet" and d["spec"].get("volumeClaimTemplates")
    }
    return {"env": env, "claims": claims}


# ── the model cache ───────────────────────────────────────────────────────


def _dockerfile_env(name: str) -> str:
    match = re.search(rf"(?m)^\s+{name}=(\S+?)\s*\\?$", (ROOT / "Dockerfile").read_text(encoding="utf-8"))
    assert match, f"{name} is gone from the Dockerfile's core ENV block"
    return match.group(1)


def test_the_default_model_cache_is_the_images_own() -> None:
    """The chart's default and the image's ENV name the same folder, so a pod
    that predates the chart value and one that has it share a cache."""
    env = _render()["env"]

    assert env["HF_HOME"] == "/root/.cremind/.cache/huggingface"
    assert env["SENTENCE_TRANSFORMERS_HOME"] == "/root/.cremind/.cache/sentence-transformers"
    assert env["HF_HOME"] == _dockerfile_env("HF_HOME")
    assert env["SENTENCE_TRANSFORMERS_HOME"] == _dockerfile_env("SENTENCE_TRANSFORMERS_HOME")


def test_a_custom_system_dir_takes_the_model_cache_along() -> None:
    env = _render("cremind.systemDir=/srv/state")["env"]

    assert env["CREMIND_SYSTEM_DIR"] == "/srv/state"
    assert env["HF_HOME"] == "/srv/state/.cache/huggingface"
    assert env["SENTENCE_TRANSFORMERS_HOME"] == "/srv/state/.cache/sentence-transformers"


# ── the profiles' working directories ────────────────────────────────────


def test_every_profiles_folder_lives_on_the_work_volume() -> None:
    """``CREMIND_WORKSPACES_DIR`` = ``<work mountPath>/cremind-workspaces``:
    each profile's own folder survives a rollout. The mount itself is
    unchanged, so an upgraded release's admin keeps ``/root/Documents`` — and a
    ``workspaces/`` folder of its own there stays an ordinary folder, not an
    unowned entry of the workspaces root that nobody may open."""
    env = _render()["env"]
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["persistence"]["work"]["mountPath"] == "/root/Documents"
    assert env["CREMIND_WORKSPACES_DIR"] == "/root/Documents/cremind-workspaces"


def test_a_custom_work_mount_takes_the_workspaces_along() -> None:
    env = _render(set_strings=("persistence.work.mountPath=/srv/work/",))["env"]
    assert env["CREMIND_WORKSPACES_DIR"] == "/srv/work/cremind-workspaces"


def test_without_the_work_volume_the_workspaces_stay_on_the_system_volume() -> None:
    """Unset → the app's default, ``<systemDir>/workspaces`` — on the system
    PVC, never the pod filesystem a rollout discards."""
    env = _render("persistence.work.enabled=false")["env"]
    assert "CREMIND_WORKSPACES_DIR" not in env


# ── volume sizes for the governor ─────────────────────────────────────────


def test_the_default_release_states_only_the_database_size() -> None:
    """PostgreSQL is on by default, the vector subcharts are off."""
    rendered = _render()

    assert rendered["env"]["CREMIND_DB_CAPACITY"] == "8Gi"
    assert rendered["env"]["CREMIND_DB_CAPACITY"] == rendered["claims"]["cremind-postgresql"]
    assert "CREMIND_VECTORSTORE_CAPACITY" not in rendered["env"]


def test_the_parent_values_declare_the_subchart_sizes() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))

    assert values["postgresql"]["primary"]["persistence"]["size"] == "8Gi"
    assert values["qdrant"]["persistence"]["size"] == "10Gi"
    # Left at its default: document search's recommendation is documented, not
    # imposed on every install.
    assert values["persistence"]["system"]["size"] == "5Gi"


def test_bundled_qdrant_states_the_size_its_claim_gets() -> None:
    rendered = _render("qdrant.enabled=true")
    assert rendered["env"]["CREMIND_VECTORSTORE_CAPACITY"] == "10Gi"
    assert rendered["claims"]["cremind-qdrant"] == "10Gi"

    rendered = _render("qdrant.enabled=true", "qdrant.persistence.size=25Gi",
                       "postgresql.primary.persistence.size=20Gi")
    assert rendered["env"]["CREMIND_VECTORSTORE_CAPACITY"] == "25Gi"
    assert rendered["claims"]["cremind-qdrant"] == "25Gi"
    assert rendered["env"]["CREMIND_DB_CAPACITY"] == "20Gi"
    assert rendered["claims"]["cremind-postgresql"] == "20Gi"


def test_bundled_chromadb_states_its_much_smaller_volume() -> None:
    rendered = _render("chromadb.enabled=true")
    assert rendered["env"]["CREMIND_VECTORSTORE_CAPACITY"] == "1Gi"
    assert rendered["claims"]["cremind-chromadb"] == "1Gi"

    rendered = _render("chromadb.enabled=true", set_strings=("chromadb.chromadb.data.volumeSize=6Gi",))
    assert rendered["env"]["CREMIND_VECTORSTORE_CAPACITY"] == "6Gi"
    assert rendered["claims"]["cremind-chromadb"] == "6Gi"


def test_external_services_state_nothing() -> None:
    """An external store's size is unknown to the chart; the admin setting or
    the pod's own measurement has to do."""
    env = _render("postgresql.enabled=false")["env"]
    for key in CAPACITY_KEYS:
        assert key not in env


@pytest.mark.parametrize("values", [
    ("postgresql.primary.persistence.enabled=false",),
    ("postgresql.primary.persistence.existingClaim=my-claim",),
])
def test_no_database_size_without_a_claim_of_that_size(values: tuple[str, ...]) -> None:
    assert "CREMIND_DB_CAPACITY" not in _render(*values)["env"]


def test_no_vector_size_for_a_non_persistent_chromadb() -> None:
    env = _render("chromadb.enabled=true", "chromadb.chromadb.isPersistent=false")["env"]
    assert "CREMIND_VECTORSTORE_CAPACITY" not in env


def test_both_vector_subcharts_leave_the_size_unstated() -> None:
    """The app uses one of them, and which one is only known after setup."""
    env = _render("qdrant.enabled=true", "chromadb.enabled=true")["env"]
    assert "CREMIND_VECTORSTORE_CAPACITY" not in env
    assert env["CREMIND_DB_CAPACITY"] == "8Gi"


# ── --reuse-values ────────────────────────────────────────────────────────


def _chart_without_new_keys(tmp_path: Path) -> Path:
    """This chart with the values.yaml of a release that predates the sizes.

    ``--reuse-values`` swaps the NEW chart's values.yaml for the previous
    release's, so every key added since is simply absent at render time.
    """
    chart = tmp_path / "cremind"
    shutil.copytree(CHART, chart)
    values = yaml.safe_load((chart / "values.yaml").read_text(encoding="utf-8"))
    del values["postgresql"]["primary"]["persistence"]
    del values["qdrant"]["persistence"]
    (chart / "values.yaml").write_text(yaml.safe_dump(values, sort_keys=False), encoding="utf-8")
    return chart


def test_an_upgrade_reusing_old_values_still_renders_the_sizes(tmp_path: Path) -> None:
    chart = _chart_without_new_keys(tmp_path)

    env = _render("qdrant.enabled=true", chart=chart)["env"]
    # The subcharts' own defaults fill in, exactly as they size the claims.
    assert env["CREMIND_DB_CAPACITY"] == "8Gi"
    assert env["CREMIND_VECTORSTORE_CAPACITY"] == "10Gi"
    assert env["HF_HOME"] == "/root/.cremind/.cache/huggingface"


def test_a_size_missing_everywhere_falls_back_to_the_subchart_default() -> None:
    """``null`` deletes a key from the parent AND the subchart defaults — the
    harshest version of a value that is not there."""
    env = _render("qdrant.enabled=true", "qdrant.persistence.size=null",
                  "postgresql.primary.persistence.size=null")["env"]

    assert env["CREMIND_DB_CAPACITY"] == "8Gi"
    assert env["CREMIND_VECTORSTORE_CAPACITY"] == "10Gi"


def test_helm_lint_passes() -> None:
    result = subprocess.run([HELM, "lint", str(CHART)], capture_output=True, text=True,
                            encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
