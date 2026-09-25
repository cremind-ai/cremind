"""The Setup Wizard's in-container ``docker compose`` call never touches cremind.

:mod:`app.services.provisioner.docker` runs the LINUX compose CLI inside the
cremind container against the host daemon. Relative sources in the bundle
(``./`` and the ``./documents`` fallback) resolve against the container's
``/opt/cremind-compose`` there, not the host folder, so re-creating the cremind
service from inside would hand the daemon paths that do not exist on the host
and lose the Documents mount. Today it only ever names a sidecar; these tests
keep it that way.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml

from app.services import manifest
from app.services.provisioner import docker as provisioner

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_TEMPLATE = REPO_ROOT / "install" / "templates" / "docker-compose.yml.tmpl"

_DOCKER_SPECS = [spec for spec in manifest.SERVICES.values() if spec.docker is not None]


def _template_services() -> dict:
    return yaml.safe_load(COMPOSE_TEMPLATE.read_text(encoding="utf-8"))["services"]


def test_there_is_something_to_check() -> None:
    assert _DOCKER_SPECS


@pytest.mark.parametrize("spec", _DOCKER_SPECS, ids=lambda spec: spec.id)
def test_every_docker_recipe_names_a_profile_gated_sidecar(spec) -> None:
    services = _template_services()
    name = spec.docker.compose_service_name

    assert name != "cremind"
    assert name in services, f"{name} is not a service in the bundled compose file"
    # Gated by exactly the profile the provisioner activates; cremind is the
    # one service with no profile (it is always up).
    assert spec.docker.compose_profile in services[name].get("profiles", [])
    assert "profiles" not in services["cremind"]


def test_no_sidecar_pulls_cremind_in_as_a_dependency() -> None:
    """``up -d <service>`` also starts (and may recreate) its ``depends_on``."""
    for name, service in _template_services().items():
        if name == "cremind":
            continue
        # A list or a mapping keyed by service name; ``in`` covers both.
        assert "cremind" not in (service.get("depends_on") or []), name


class _FinishedProcess:
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


@pytest.mark.parametrize("spec", _DOCKER_SPECS, ids=lambda spec: spec.id)
def test_provision_runs_compose_up_for_the_sidecar_alone(spec, tmp_path, monkeypatch) -> None:
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text(COMPOSE_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text("COMPOSE_PROJECT_NAME=cremind\nCOMPOSE_PROFILES=\n", encoding="utf-8")
    monkeypatch.setenv("CREMIND_COMPOSE_FILE", str(compose_file))
    monkeypatch.setenv("CREMIND_COMPOSE_ENV_FILE", str(env_file))
    # The container's own copy of a value the compose file interpolates: it
    # must not outrank the bundle's .env in the child.
    monkeypatch.setenv("CREMIND_COMPOSE_HOST_DIR", "/stale/from/the/container")
    monkeypatch.setattr(provisioner, "is_supported", lambda: True)

    async def _ready(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(provisioner, "_wait_for_port", _ready)

    calls: list[tuple[list[str], dict]] = []

    async def _exec(*argv, **kwargs):
        calls.append((list(argv), kwargs.get("env") or {}))
        return _FinishedProcess()

    monkeypatch.setattr(provisioner.asyncio, "create_subprocess_exec", _exec)

    asyncio.run(provisioner.provision(spec, {}))

    assert len(calls) == 1
    argv, child_env = calls[0]
    assert argv[:2] == ["docker", "compose"]
    up = argv.index("up")
    # Exactly one service after ``up -d``, it is the sidecar, and --no-deps
    # keeps compose from also (re)creating anything it depends on.
    assert argv[up:] == ["up", "-d", "--no-deps", spec.docker.compose_service_name]
    assert "cremind" not in argv[up:]
    assert "--force-recreate" not in argv
    assert "CREMIND_COMPOSE_HOST_DIR" not in child_env


def test_the_cremind_service_itself_is_refused() -> None:
    """The last line of defence if a recipe ever named the app service."""
    with pytest.raises(provisioner.ProvisionError, match="refusing to recreate the cremind service"):
        asyncio.run(provisioner._compose_up("/opt/cremind-compose/docker-compose.yml",
                                            "/opt/cremind-compose/.env", "cremind"))
