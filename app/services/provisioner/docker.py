"""Docker-mode provisioning via the bundled compose stack.

The compose template shipped by the installer ([install/templates/
docker-compose.yml.tmpl]) pre-defines every supported service
(``postgres``, ``qdrant``, ``chroma``) behind a ``profiles:`` gate.
This module activates a service by:

1. Appending its :attr:`DockerRecipe.compose_profile` to
   ``COMPOSE_PROFILES`` in the sibling ``.env`` (preserving order,
   de-duplicating).
2. Writing any per-service env vars the user supplied (Postgres
   password, …) into the same ``.env``.
3. Running ``docker compose -f <compose> --env-file <env> up -d
   <service>``.
4. Polling the service's port until it accepts TCP connections or the
   timeout expires.

The compose file path and the ``.env`` path are passed through the
environment by the entrypoint script (``CREMIND_COMPOSE_FILE`` and
``CREMIND_COMPOSE_ENV_FILE``). When those are unset — every native
install, plus broken Docker installs — :func:`is_supported` returns
``False`` and the capabilities endpoint masks the Docker option in
the wizard UI.

The connection returned to the wizard is the in-network address from
:class:`DockerRecipe` (e.g. ``postgres:5432``). The Cremind container
reaches sibling containers via the user-defined compose network using
the service's compose name. Host-side native callers reaching a
Docker service via published ports are out of scope for phase 1.
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from app.events.setup_progress_bus import publish_setup_event
from app.services.manifest import ServiceSpec
from app.services.provisioner.base import ProvisionedService, ProvisionError
from app.utils.logger import logger


def _emit(step: str, message: str, level: str = "info") -> None:
    """Publish one progress entry to the Setup Wizard live-log stream.

    Stays a no-op when nobody is subscribed (post-setup or non-wizard
    callers), so this is safe to sprinkle without gating on context.
    """
    try:
        publish_setup_event({
            "step": step,
            "message": message,
            "level": level,
            "ts": time.time(),
        })
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[provisioner] setup-event publish failed: {e}")


def _step_for_service(service_id: str) -> str:
    """Map a service id to the wizard's step bucket.

    Postgres → ``database``; everything else (Qdrant, Chroma) is part
    of the vector store stage.
    """
    return "database" if service_id == "postgres" else "vectorstore"


# Env var the entrypoint sets to the path of the compose file inside the
# cremind container. Tests set it manually. Missing => "no docker stack
# available; can't provision in Docker mode here."
_COMPOSE_FILE_ENV = "CREMIND_COMPOSE_FILE"
_COMPOSE_ENV_FILE_ENV = "CREMIND_COMPOSE_ENV_FILE"

# How long to wait for ``docker compose up`` to bring the container to
# a TCP-accepting state before giving up. Sized for image pulls on a
# slow connection (postgres:16 + qdrant total is ~250 MB).
_HEALTH_TIMEOUT_SECONDS = 180
_HEALTH_POLL_INTERVAL = 1.0


def is_supported() -> bool:
    """True if this process can drive ``docker compose`` against the bundle.

    Checked by :func:`app.services.provisioner.base.docker_available` to
    decide whether the wizard should even offer the Docker radio option.
    """
    if not shutil.which("docker"):
        return False
    compose_file = os.environ.get(_COMPOSE_FILE_ENV, "").strip()
    if not compose_file:
        return False
    return Path(compose_file).is_file()


async def provision(spec: ServiceSpec, config: dict[str, Any]) -> ProvisionedService:
    if spec.docker is None:
        raise ProvisionError(
            f"{spec.display_name} has no Docker recipe — this is a bug in "
            f"app/services/manifest.py"
        )
    if not is_supported():
        raise ProvisionError(
            "Docker provisioning is not available in this Cremind install. "
            "Re-run the installer in Docker mode, or pick Native / External."
        )

    compose_file = os.environ[_COMPOSE_FILE_ENV]
    env_file = os.environ.get(_COMPOSE_ENV_FILE_ENV, "").strip() or str(
        Path(compose_file).with_name(".env")
    )

    step = _step_for_service(spec.id)

    # Step 1: profile activation + per-service env vars in .env.
    _activate_compose_profile(env_file, spec.docker.compose_profile)
    creds = _resolve_service_credentials(env_file, spec, config)

    # Step 2: docker compose up -d <service>.
    _emit(step, f"Running `docker compose up -d {spec.docker.compose_service_name}`…")
    await _compose_up(compose_file, env_file, spec.docker.compose_service_name)
    _emit(step, f"{spec.display_name} container started.")

    # Step 3: wait for the service to accept TCP connections.
    _emit(
        step,
        f"Waiting for {spec.display_name} to accept connections on "
        f"{spec.docker.in_network_host}:{spec.docker.in_network_port} "
        f"(up to {_HEALTH_TIMEOUT_SECONDS}s)…",
    )
    await _wait_for_port(
        spec.docker.in_network_host,
        spec.docker.in_network_port,
        timeout=_HEALTH_TIMEOUT_SECONDS,
        display_name=spec.display_name,
        step=step,
    )

    # Build the connection dict the storage adapter consumes. The
    # provisioner is authoritative for service hostname / port (they
    # come from the compose recipe); any credentials are the effective
    # values it just resolved (user input → previous .env → generated).
    connection: dict[str, Any] = {
        "host": spec.docker.in_network_host,
        "port": spec.docker.in_network_port,
        **creds,
    }
    # ``sslmode=disable`` is the right default for compose-internal
    # Postgres traffic: it's on a private network and the postgres:16
    # image doesn't ship TLS certs.
    if spec.id == "postgres" and "sslmode" not in connection:
        connection["sslmode"] = "disable"

    return ProvisionedService(service_id=spec.id, mode="docker", connection=connection)


# ── .env helpers ───────────────────────────────────────────────────────────

# ``KEY=value`` matcher tolerant of optional surrounding quotes; comments
# and blank lines pass through unchanged. We don't want to roundtrip
# through a TOML/YAML parser here — the file is plain dotenv.
_ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


def _read_env_file(env_file: str) -> list[str]:
    try:
        return Path(env_file).read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []


def _write_env_file(env_file: str, lines: list[str]) -> None:
    text = "\n".join(lines)
    if not text.endswith("\n"):
        text += "\n"
    Path(env_file).write_text(text, encoding="utf-8")


def _upsert_env_var(lines: list[str], key: str, value: str) -> list[str]:
    """Set ``key=value`` in ``lines``, replacing the last assignment if any."""
    rendered = f"{key}={value}"
    out = list(lines)
    last_idx = -1
    for i, line in enumerate(out):
        m = _ENV_LINE.match(line)
        if m and m.group(1) == key:
            last_idx = i
    if last_idx >= 0:
        out[last_idx] = rendered
    else:
        out.append(rendered)
    return out


def _read_env_var(lines: list[str], key: str) -> str | None:
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and m.group(1) == key:
            # Strip surrounding single/double quotes if any.
            value = m.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            return value
    return None


def _activate_compose_profile(env_file: str, profile: str) -> None:
    """Add ``profile`` to ``COMPOSE_PROFILES`` if not already present.

    The variable is a comma-separated list per Compose's convention. We
    preserve any pre-existing entries (e.g. another service that the
    wizard already activated on a previous run) and dedupe.
    """
    lines = _read_env_file(env_file)
    current = _read_env_var(lines, "COMPOSE_PROFILES") or ""
    parts = [p.strip() for p in current.split(",") if p.strip()]
    if profile not in parts:
        parts.append(profile)
    new_value = ",".join(parts)
    lines = _upsert_env_var(lines, "COMPOSE_PROFILES", new_value)
    _write_env_file(env_file, lines)
    logger.info(
        f"[docker-provisioner] activated compose profile {profile!r} "
        f"(now: {new_value!r})"
    )


def _resolve_service_credentials(
    env_file: str, spec: ServiceSpec, config: dict[str, Any]
) -> dict[str, str]:
    """Determine the effective credentials and mirror them into ``.env``.

    For Docker-Postgres, the compose template references
    ``${PG_DATABASE}`` / ``${PG_USER}`` / ``${PG_PASSWORD}`` so the
    container picks up the same values we'll later write into
    ``bootstrap.toml`` for the Cremind client. Precedence per field:

    1. wizard payload (``config[...]``) — only set in the rare case
       where the user typed credentials into the wizard for a Docker
       deployment;
    2. existing ``.env`` value — preserves whatever the previous
       wizard run / installer chose;
    3. a sensible default (``"cremind"`` for db/user) or a freshly
       generated password (URL-safe 24 bytes).

    Returns the resolved values so the caller can echo them back to
    the wizard via the persisted ``server_config`` table — Cremind
    needs the password to connect to its own Postgres container, not
    just to start it.

    Non-Postgres services have no template-bound credentials today,
    so this is a no-op for them.
    """
    if spec.id != "postgres":
        return {}

    lines = _read_env_file(env_file)

    def _pick(field: str, env_key: str, default: str) -> str:
        wizard_value = (config.get(field) or "")
        if isinstance(wizard_value, str):
            wizard_value = wizard_value.strip()
        if wizard_value:
            return str(wizard_value)
        env_value = _read_env_var(lines, env_key)
        if env_value:
            return env_value
        return default

    database = _pick("database", "PG_DATABASE", "cremind")
    user = _pick("user", "PG_USER", "cremind")
    password = _pick("password", "PG_PASSWORD", secrets.token_urlsafe(24))

    lines = _upsert_env_var(lines, "PG_DATABASE", database)
    lines = _upsert_env_var(lines, "PG_USER", user)
    lines = _upsert_env_var(lines, "PG_PASSWORD", password)
    _write_env_file(env_file, lines)
    return {"database": database, "user": user, "password": password}


# ── docker compose up ──────────────────────────────────────────────────────

async def _compose_up(compose_file: str, env_file: str, service: str) -> None:
    """Bring a single compose service up (detached).

    We pass ``--env-file`` explicitly because the cremind container's
    working dir is not necessarily the compose project dir, so the
    default ``./.env`` lookup wouldn't find the right file.
    """
    cmd = [
        "docker", "compose",
        "-f", compose_file,
        "--env-file", env_file,
        "up", "-d", service,
    ]
    logger.info(f"[docker-provisioner] running: {' '.join(cmd)}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace")
    stderr = stderr_b.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        logger.error(f"[docker-provisioner] compose up service={service} rc={proc.returncode}")
        raise ProvisionError(
            f"`docker compose up -d {service}` failed (exit {proc.returncode}):\n"
            f"{stderr or stdout or '(no output)'}"
        )
    logger.info(f"[docker-provisioner] compose up service={service} rc=0")
    if stdout.strip():
        logger.debug(f"[docker-provisioner] {stdout.strip()}")


# ── readiness probe ────────────────────────────────────────────────────────

async def _wait_for_port(
    host: str, port: int, *, timeout: float, display_name: str, step: str = "vectorstore",
) -> None:
    """Poll ``host:port`` until it accepts TCP connections or we time out.

    Replaces the entrypoint's old ``wait_for`` loop. We use a plain TCP
    connect (not a service-specific health check) because every
    service we support today opens its primary port only when it's
    ready to accept queries — pg_isready / qdrant /healthz / chroma
    /api/v1/heartbeat all line up with port-open semantics.

    Emits a heartbeat to the setup-progress bus every 10s so the wizard
    UI doesn't appear frozen during slow image pulls.
    """
    start = time.monotonic()
    deadline = start + timeout
    last_err: str = ""
    next_heartbeat = start + 10.0
    while time.monotonic() < deadline:
        try:
            fut = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(fut, timeout=2.0)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 — closing failure isn't fatal
                pass
            logger.info(
                f"[docker-provisioner] {display_name} accepting connections "
                f"on {host}:{port}"
            )
            return
        except (OSError, asyncio.TimeoutError) as exc:
            last_err = str(exc)
            now = time.monotonic()
            if now >= next_heartbeat:
                elapsed = int(now - start)
                _emit(
                    step,
                    f"Still waiting for {display_name} ({elapsed}s elapsed)…",
                )
                next_heartbeat = now + 10.0
            await asyncio.sleep(_HEALTH_POLL_INTERVAL)
    raise ProvisionError(
        f"{display_name} did not become reachable on {host}:{port} within "
        f"{timeout:.0f}s (last error: {last_err or 'connection refused'})."
    )
