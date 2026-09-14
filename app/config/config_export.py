"""The Cremind configuration file — one renderer, server-side.

The Setup Wizard hands the user a file on its last step (JWT token, agent URL,
project paths, database and vector-store parameters, channels, the VNC password
on desktop container installs, the Kubernetes identity on a Helm install). That
file used to be assembled in the browser, which meant only a profile that could
read four admin-only endpoints could ever produce one. Three callers need it
now — the wizard, the Profile page's re-download card, and ``cremind config
export`` (which is how the agent hands it to a user in chat) — so the assembly
and the three renderings live here, behind ``GET /api/config/export``.

Two scopes, because the file mixes two kinds of fact:

* **full** (the ``admin`` profile) — everything, as before.
* **profile** (everyone else) — the caller's own identity and nothing about the
  server: token, expiry, agent URL, login link, where the token is kept, the
  project directories, the deployment shape, whether embedding is on, and its
  own channels. The database, vector-store, desktop and Kubernetes sections
  describe the install rather than the profile and carry install-wide
  credentials, so they are omitted.

The JSON form keeps the camelCase keys the browser used to emit, so an admin's
``.json`` export stays key-compatible with the files users already have.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit

#: The formats ``?format=`` accepts, and what they are served as.
EXPORT_FORMATS: tuple[str, ...] = ("md", "json", "env")
EXPORT_MIME: dict[str, str] = {
    "md": "text/markdown",
    "json": "application/json",
    "env": "text/plain",
}


def export_filename(profile: str, fmt: str) -> str:
    """``cremind-<profile>-config.<ext>`` — stable across every producer.

    Both the wizard and the re-download card have always written this name, so
    a re-download versions alongside the original instead of looking like a
    different artefact.
    """
    return f"cremind-{profile}-config.{fmt}"


# ── noVNC URL builders (ports of ui/src/utils/vncDesktop.ts) ──────────────
#
# The split there is deliberate and survives here: the server names the *shape*
# (its own published port, or a path on Cremind's origin), the caller supplies
# the address. What the export adds is that its "browser" is the request that
# asked for it, so the origin comes from ``agentUrl``.

_DIRECT_NOVNC_PATH = "/vnc.html"
_PROXY_NOVNC_PATH = "/vnc/vnc.html"


def novnc_url_for_docker_host(host: str, port: int, path: str = _DIRECT_NOVNC_PATH) -> str:
    """noVNC on its own published port — the Docker desktop image.

    Always http: that port is websockify's, not Cremind's, so it is plain even
    when Cremind itself serves HTTPS.
    """
    return f"http://{host}:{port}{path}"


def novnc_url_on_origin(origin: str, path: str = _PROXY_NOVNC_PATH) -> str:
    """noVNC as another path on Cremind's own origin — the Kubernetes proxy."""
    return f"{origin}{path}"


# ── install-time secrets ─────────────────────────────────────────────────


def gather_install_secrets() -> dict[str, Any]:
    """Install-time connection info the config file needs, from three sources.

    Extracted verbatim from ``GET /api/config/install-secrets`` so that
    endpoint and this renderer can never disagree about what a Docker or
    Kubernetes install looks like. Walked in priority order:

    1. ``os.environ`` — inside the cremind Docker container the compose
       template sets VNC_PASSWORD, RESOLUTION, APP_URL, INSTALL_MODE directly,
       so env vars are the in-container truth.
    2. ``$CREMIND_COMPOSE_ENV_FILE`` — the bind-mounted host ``docker/.env``.
    3. ``$CREMIND_INSTALL_DIR/docker/.env`` — host install case.

    Postgres comes from ``bootstrap.toml`` (authoritative) with a fallback to
    ``docker/.env``'s ``PG_*``.

    Returns ``{"deployment": "native", "available": False}`` when no container
    runtime is detectable *and* no ``bootstrap.toml`` exists. Raises ``OSError``
    naming the file when a docker env file exists but cannot be read — the
    caller decides whether that is a 500 or a degraded export.

    ``deployment`` answers "container or host process", not "which
    orchestrator": a Kubernetes pod says ``docker`` here on purpose, because
    that is the question the export's container block branches on.
    ``install_mode`` beside it still says ``kubernetes``.
    """
    import os
    from pathlib import Path

    from app.config import runtime_env
    from app.config.bootstrap import read_bootstrap
    from app.config.credentials_file import parse_docker_env
    from app.config.settings import BaseConfig

    compose_env_file = os.environ.get("CREMIND_COMPOSE_ENV_FILE", "").strip()
    compose_env_path = Path(compose_env_file) if compose_env_file else None
    docker_env_host = Path(BaseConfig.CREMIND_INSTALL_DIR) / "docker" / ".env"
    bootstrap_path = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "bootstrap.toml"

    # File-based fallback for keys not promoted to container env vars. Pick the
    # first source that exists; both parsers return {} for missing files, so a
    # non-existent path is safe.
    file_parsed: dict[str, str] = {}
    for candidate in (compose_env_path, docker_env_host):
        if candidate is not None and candidate.exists():
            try:
                file_parsed = parse_docker_env(candidate)
            except OSError as e:
                # Re-raised with the path in the message so the handler's 500
                # reads the same as it did when this lived inline.
                raise OSError(f"Failed to read docker env file {candidate}: {e}") from e
            break

    def _runtime(key: str) -> str | None:
        """Priority chain for any docker-runtime KEY: env var → file."""
        val = os.environ.get(key, "").strip()
        if val:
            return val
        val = file_parsed.get(key, "").strip()
        if val:
            # parse_docker_env already drops __PLACEHOLDER__ values.
            return val
        return None

    def _runtime_int(key: str, default: int) -> int:
        raw = _runtime(key)
        try:
            return int(raw) if raw is not None else default
        except (TypeError, ValueError):
            return default

    # The mode this process really runs under, falling back to whatever the
    # compose ``.env`` on the host says when nothing here can tell (the Electron
    # app calls this from outside the container).
    from app.config.tls_managed_env import effective_install_mode

    install_mode = effective_install_mode() or (_runtime("INSTALL_MODE") or "").lower()
    vnc_password = _runtime("VNC_PASSWORD")
    # ``kubernetes`` belongs in here beside ``docker``: a pod running the basic
    # image has no VNC_PASSWORD, and without this it reported ``native`` with
    # every container field None while INSTALL_MODE=kubernetes sat in its own
    # environment.
    is_container = (
        install_mode in ("docker", "kubernetes")
        or compose_env_path is not None
        or docker_env_host.exists()
        or bool(vnc_password)
    )

    if not is_container and not bootstrap_path.exists():
        return {"deployment": "native", "available": False}

    bootstrap = read_bootstrap()
    pg_bootstrap = bootstrap.get("postgres") or {}
    has_postgres = bootstrap.get("db_provider") == "postgres"

    def _pg(field: str, env_key: str):
        """Prefer bootstrap.toml for Postgres; fall back to docker env."""
        if has_postgres:
            val = pg_bootstrap.get(field)
            if val not in (None, ""):
                return val
        return file_parsed.get(env_key) or None

    deployment = "docker" if is_container else "native"

    return {
        # "container" or "host process" — a Kubernetes pod says ``docker``.
        "deployment": deployment,
        "available": True,
        # Container runtime block: populated only when we detect a container
        # runtime. Values are None on native installs.
        "vnc_password": vnc_password,
        "app_url": _runtime("APP_URL") if is_container else None,
        "resolution": _runtime("RESOLUTION") if is_container else None,
        "install_mode": install_mode or None,
        "cors_allowed_origins": _runtime("CORS_ALLOWED_ORIGINS") if is_container else None,
        "setup_wizard_env": _runtime("SETUP_WIZARD_ENV") if is_container else None,
        "api_port": _runtime_int("API_PORT", 1112) if is_container else None,
        "spa_port": _runtime_int("SPA_PORT", 1515) if is_container else None,
        "novnc_port": _runtime_int("NOVNC_PORT", 6080) if is_container else None,
        "vnc_port": _runtime_int("VNC_PORT", 5900) if is_container else None,
        # Where noVNC actually answers, when the deployment knows and the
        # client cannot work it out.
        "novnc_url": _runtime("CREMIND_NOVNC_URL"),
        # Which namespace, Helm release and Deployment/Service this pod is, so
        # the file can carry the ``kubectl port-forward`` line that reconnects.
        "kubernetes": (
            runtime_env.kubernetes_identity("kubernetes")
            if install_mode == "kubernetes"
            else None
        ),
        # The full VNC descriptor, not the public subset the unauthenticated
        # tray endpoint gets: this block only ever reaches an admin export,
        # which already carries the VNC password.
        "vnc": runtime_env.describe_runtime_environment()["vnc"],
        # Postgres block from bootstrap.toml. ``db_provider`` states which
        # backend was chosen even when it is SQLite, because a post-setup
        # re-download has no other way to name it.
        "db_provider": bootstrap.get("db_provider"),
        "pg_host": _pg("host", "") if has_postgres else None,
        "pg_port": int(pg_bootstrap["port"]) if has_postgres and pg_bootstrap.get("port") else None,
        "pg_user": _pg("user", "PG_USER"),
        "pg_password": _pg("password", "PG_PASSWORD"),
        "pg_database": _pg("database", "PG_DATABASE"),
        "pg_sslmode": _pg("sslmode", "") if has_postgres else None,
        "pg_deployment_mode": (pg_bootstrap.get("deployment_mode") if has_postgres else None) or None,
    }


# ── snapshot assembly ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ConfigSnapshotSources:
    """Everything :func:`assemble_config_snapshot` needs, named explicitly.

    Every field is resolved by the endpoint before assembly so this function
    stays pure and testable: no request, no storage, no environment reads.
    """

    profile: str
    token: str
    token_expires_at: str
    agent_url: str
    #: True when ``agent_url`` is the HTTPS origin the server pivots to on the
    #: post-setup restart rather than the origin serving this request.
    agent_url_pending_https: bool
    generated_at: str
    #: ``local`` | ``server`` | ``custom`` | ``kubernetes``
    install_deployment: str
    #: ``docker`` | ``native`` | ``kubernetes``
    install_mode: str
    install_custom_values: Mapping[str, str]
    #: :func:`gather_install_secrets` output, or ``None`` for a reduced export.
    install_secrets: Mapping[str, Any] | None
    db_provider: str
    user_working_dir: str
    system_dir: str
    sqlite_db_path: str
    #: ``read_embedding_config`` shape, or ``None``.
    embedding_config: Mapping[str, Any] | None
    #: ``({"type", "mode", "id"}, ...)`` for this profile's own channels.
    channels: Sequence[Mapping[str, Any]] = ()
    #: Admin scope: include the install-wide sections.
    full: bool = False


def _optional_text(value: Any) -> str | None:
    """A field that is optional in three ways — absent, null, or the empty
    string a chart injects for a value it could not resolve — narrowed to
    "known" or "not known"."""
    text = str(value or "").strip()
    return text or None


def _compact(data: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None``-valued keys, the way ``JSON.stringify`` drops ``undefined``.

    Never drops ``False`` / ``0`` / ``""`` — those are answers.
    """
    return {k: v for k, v in data.items() if v is not None}


def assemble_config_snapshot(sources: ConfigSnapshotSources) -> dict[str, Any]:
    """Turn resolved facts into the snapshot the three renderers read.

    Keys are camelCase on purpose: the JSON form is the file users already
    have, and renaming them would break every script that reads one.
    """
    secrets: Mapping[str, Any] = sources.install_secrets or {}

    snapshot: dict[str, Any] = {
        "profile": sources.profile,
        "token": sources.token,
        "tokenExpiresAt": sources.token_expires_at,
        "agentUrl": sources.agent_url,
        "agentUrlPendingHttps": sources.agent_url_pending_https,
        # Where to sign in as this profile. The login screen takes a pasted
        # token (there is no token-in-URL flow), so this is the page to open,
        # not a one-click link.
        "loginUrl": f"{sources.agent_url.rstrip('/')}/#/login/{sources.profile}",
        # The server's own copy of the token, for the day this file is lost.
        "tokenFile": f"{sources.system_dir.rstrip('/')}/tokens/{sources.profile}.token",
        "generatedAt": sources.generated_at,
        "workingDir": sources.user_working_dir,
        "systemDir": sources.system_dir,
        "embedding": _compact({
            "enabled": bool((sources.embedding_config or {}).get("enabled")),
            # Which model is doing the embedding is server-wide configuration;
            # only the on/off flag is public (GET /api/config/embedding/status).
            "provider": ((sources.embedding_config or {}).get("provider") or None)
            if sources.full else None,
        }),
        "channels": [dict(ch) for ch in sources.channels],
    }

    deployment: dict[str, Any] = {
        "type": sources.install_deployment,
        # ``mode`` answers "container or host process?", so Kubernetes folds
        # into 'docker' — ``type`` above is what names the chart.
        "mode": "docker" if sources.install_mode == "kubernetes" else sources.install_mode,
    }

    if not sources.full:
        # A per-profile file describes the profile, not the install. Everything
        # below this point is install-wide (and half of it is credentials).
        snapshot["scope"] = "profile"
        snapshot["deployment"] = deployment
        return snapshot

    if sources.install_deployment == "custom":
        deployment["customFields"] = dict(sources.install_custom_values)
    snapshot["deployment"] = deployment

    # ── Kubernetes identity ──
    # Off Kubernetes the server sends no block at all, so its presence is also
    # the test for "did a Helm install produce this file?".
    k8s = secrets.get("kubernetes") or None
    if k8s:
        snapshot["kubernetes"] = _compact({
            "namespace": _optional_text(k8s.get("namespace")),
            "release": _optional_text(k8s.get("release")),
            # ``workload`` on the wire, because the pod infers one name that is
            # both; here it is split into the two objects the reader looks for.
            "deployment": _optional_text(k8s.get("workload")),
            "service": _optional_text(k8s.get("service")),
            "servicePort": k8s.get("service_port"),
            "source": k8s.get("source"),
            "portForward": _optional_text(k8s.get("port_forward")),
        })

    # ── VNC desktop ──
    # Only a container install has a desktop to describe; Kubernetes counts,
    # since it runs the same desktop image.
    container_install = sources.install_mode in ("docker", "kubernetes")
    if container_install and secrets.get("vnc_password"):
        # Derive the host from APP_URL so server / custom deployments show a
        # host the user can actually reach; localhost for a local install.
        host = "localhost"
        app_url = str(secrets.get("app_url") or "")
        match = re.match(r"^[a-z]+://([^/:]+)", app_url, re.IGNORECASE)
        if match:
            host = match.group(1)
        if secrets.get("install_mode") == "kubernetes":
            # Kubernetes usually fronts the SPA, API and noVNC on ONE port, with
            # the desktop at /vnc/vnc.html on the app origin. But the chart
            # bypasses that sidecar when the pod terminates TLS itself, and
            # noVNC then answers on its own Service port — indistinguishable
            # from here, so CREMIND_NOVNC_URL wins whenever it is set.
            origin = "http://localhost:1515"
            if app_url:
                split = urlsplit(app_url)
                if split.scheme and split.netloc:
                    origin = f"{split.scheme}://{split.netloc}"
            forwards = ((secrets.get("vnc") or {}).get("port_forward_commands")) or None
            snapshot["vnc"] = _compact({
                "password": secrets.get("vnc_password"),
                "host": host,
                "novnc_url": secrets.get("novnc_url") or novnc_url_on_origin(origin),
                "resolution": secrets.get("resolution") or None,
                "environment": "kubernetes",
                "port_forward_commands": [dict(c) for c in forwards] if forwards else None,
            })
        else:
            novnc_port = secrets.get("novnc_port") or 6080
            vnc_port = secrets.get("vnc_port") or 5900
            snapshot["vnc"] = _compact({
                "password": secrets.get("vnc_password"),
                "host": host,
                "novnc_port": novnc_port,
                "vnc_port": vnc_port,
                "novnc_url": novnc_url_for_docker_host(host, novnc_port),
                "vnc_endpoint": f"{host}:{vnc_port}",
                "resolution": secrets.get("resolution") or None,
                "environment": "docker",
            })

    # ── Database ──
    db_provider = "postgres" if sources.db_provider == "postgres" else "sqlite"
    database: dict[str, Any] = {"provider": db_provider}
    if db_provider == "postgres":
        database["postgres"] = _compact({
            "host": str(secrets.get("pg_host") or "localhost"),
            "port": int(secrets.get("pg_port") or 5432),
            "database": str(secrets.get("pg_database") or "cremind"),
            "user": str(secrets.get("pg_user") or "cremind"),
            "password": str(secrets.get("pg_password") or ""),
            "sslmode": _optional_text(secrets.get("pg_sslmode")),
        })
    else:
        database["sqlite"] = {"path": sources.sqlite_db_path}
    snapshot["database"] = database

    # ── Vector store ──
    # This lives in the embedding config, NOT server_config: the flat
    # ``vectorstore.*`` keys only land in the table after persist_embedding_config
    # runs, and a wizard-time export never refetches.
    vs = (sources.embedding_config or {}).get("vectorstore") or {}
    vs_provider = vs.get("provider")
    vector_provider = (
        vs_provider
        if (sources.embedding_config or {}).get("enabled") and vs_provider in ("qdrant", "chroma")
        else "none"
    )
    vector_store: dict[str, Any] = {"provider": vector_provider}
    if vector_provider == "qdrant":
        q = vs.get("qdrant") or {}
        vector_store["qdrant"] = _compact({
            "host": str(q.get("host") or "localhost"),
            "port": int(q.get("port") or 6333),
            "api_key": _optional_text(q.get("api_key")),
            "https": bool(q.get("https")),
        })
    elif vector_provider == "chroma":
        c = vs.get("chroma") or {}
        # Same persist-vs-http translation as app/lib/embedding_lifecycle.py
        # (Native → persistent file; Docker / External → http endpoint).
        vector_store["chroma"] = _compact({
            "mode": "persistent" if c.get("deployment_mode") == "native" else "http",
            "host": _optional_text(c.get("host")),
            "port": c.get("port"),
            "ssl": c.get("ssl"),
            "api_key": _optional_text(c.get("api_key")),
            "persist_path": _optional_text(c.get("persist_path")),
        })
    snapshot["vectorStore"] = vector_store

    return snapshot


# ── renderers ────────────────────────────────────────────────────────────


def _pg_connection_string(p: Mapping[str, Any]) -> str:
    # ``safe`` matches JavaScript's encodeURIComponent, which leaves exactly
    # these unreserved marks alone.
    def enc(value: Any) -> str:
        return quote(str(value), safe="-_.!~*'()")

    sslmode = p.get("sslmode")
    ssl_suffix = f"?sslmode={enc(sslmode)}" if sslmode else ""
    return (
        f"postgresql://{enc(p.get('user', ''))}:{enc(p.get('password', ''))}"
        f"@{p.get('host')}:{p.get('port')}/{enc(p.get('database', ''))}{ssl_suffix}"
    )


def _qdrant_url(q: Mapping[str, Any]) -> str:
    scheme = "https" if q.get("https") else "http"
    return f"{scheme}://{q.get('host')}:{q.get('port')}"


def render_markdown(s: Mapping[str, Any]) -> str:
    """The default form: secrets under a callout, ready-to-paste strings."""
    lines: list[str] = []
    lines.append(f"# Cremind Configuration — {s['profile']}")
    lines.append("")
    lines.append(f"_Generated at {s['generatedAt']}_")
    lines.append("")
    lines.append(
        "> **Sensitive.** This file contains secrets (JWT token, passwords, API keys). "
        "Store it somewhere safe and avoid sharing."
    )
    lines.append("")
    if s.get("scope") == "profile":
        lines.append(
            "_Per-profile file — the database, vector-store, desktop and Kubernetes "
            "sections are only in the admin profile's export._"
        )
        lines.append("")

    lines.append("## Profile & Token")
    lines.append("")
    lines.append(f"- **Profile:** `{s['profile']}`")
    lines.append(f"- **Agent URL:** `{s['agentUrl']}`")
    if s.get("agentUrlPendingHttps"):
        lines.append(
            "  - _This HTTPS address becomes active once setup finishes and the server restarts._"
        )
    lines.append(f"- **Sign in:** {s['loginUrl']} (paste the token below)")
    if s.get("tokenExpiresAt"):
        lines.append(f"- **Token expires:** {s['tokenExpiresAt']}")
    lines.append(f"- **Token recovery path on server:** `{s['tokenFile']}`")
    lines.append("")
    lines.append("```")
    lines.append(s["token"])
    lines.append("```")
    lines.append("")

    lines.append("## Deployment")
    lines.append("")
    lines.append(f"- **Type:** {s['deployment']['type']}")
    lines.append(f"- **Mode:** {s['deployment']['mode']}")
    custom_fields = s["deployment"].get("customFields")
    if custom_fields:
        entries = [(k, v) for k, v in custom_fields.items() if v]
        if entries:
            lines.append("- **Custom fields:**")
            for k, v in entries:
                lines.append(f"  - `{k}` = `{v}`")
    lines.append("")

    k = s.get("kubernetes")
    if k:
        lines.append("## Kubernetes")
        lines.append("")
        if k.get("namespace"):
            lines.append(f"- **Namespace:** `{k['namespace']}`")
        if k.get("release"):
            lines.append(f"- **Helm release:** `{k['release']}`")
        if k.get("deployment"):
            lines.append(f"- **Deployment:** `{k['deployment']}`")
        if k.get("service"):
            lines.append(f"- **Service:** `{k['service']}`")
        if k.get("servicePort") is not None:
            lines.append(f"- **Service port:** `{k['servicePort']}`")
        if k.get("source"):
            lines.append(f"- **Identity source:** {k['source']}")
        # An inferred identity is a real answer with one hole in it: the pod
        # name gives the Deployment away, nothing gives the release away.
        if k.get("source") == "inferred":
            lines.append("")
            lines.append(
                "_Read off the pod rather than stated by the chart — confirm the release "
                "name with `helm list --all-namespaces`._"
            )
        if k.get("portForward"):
            lines.append("")
            lines.append("Reconnect from your machine:")
            lines.append("")
            lines.append("```")
            lines.append(k["portForward"])
            lines.append("```")
            lines.append("")
            lines.append(
                "Add `1455:1455` as a second port on that command before signing in to Codex "
                "with a ChatGPT account — the browser sends the OAuth callback there, and it "
                "has to reach the pod."
            )
        lines.append("")

    lines.append("## Project Paths")
    lines.append("")
    lines.append(f"- **User working directory:** `{s['workingDir']}`")
    lines.append(f"- **System directory:** `{s['systemDir']}`")
    lines.append("")

    v = s.get("vnc")
    if v:
        host = v.get("host") or "localhost"
        is_k8s = v.get("environment") == "kubernetes"
        novnc_port = v.get("novnc_port") or 6080
        vnc_port = v.get("vnc_port") or 5900
        # Fallbacks only — the assembler passes ``novnc_url`` whenever the
        # deployment knows it. The Kubernetes shape reaches noVNC through the
        # *app* origin, so it inherits that origin's scheme; the Docker shape
        # is NOT derived (6080 is websockify's own port, always plain http).
        app_scheme = urlsplit(str(s.get("agentUrl") or "")).scheme or "http"
        novnc_url = v.get("novnc_url") or (
            novnc_url_on_origin(f"{app_scheme}://{host}:1515") if is_k8s
            else novnc_url_for_docker_host(host, novnc_port)
        )
        vnc_endpoint = v.get("vnc_endpoint") or f"{host}:{vnc_port}"
        lines.append(f"## VNC Desktop ({'Kubernetes' if is_k8s else 'Docker'})")
        lines.append("")
        lines.append("> **Sensitive.**")
        lines.append("")
        lines.append(f"- **Password:** `{v['password']}`")
        lines.append(f"- **Host:** `{host}`")
        lines.append(f"- **Web client (noVNC):** {novnc_url}")
        # Kubernetes fronts noVNC/VNC through a single proxy port — the raw
        # 6080/5900 ports and a direct VNC endpoint aren't exposed there.
        if not is_k8s:
            lines.append(f"- **VNC client:** `{vnc_endpoint}`")
            lines.append(f"- **noVNC port:** `{novnc_port}`")
            lines.append(f"- **VNC port:** `{vnc_port}`")
        if v.get("resolution"):
            lines.append(f"- **Resolution:** `{v['resolution']}`")
        # Relay mode: nothing on the app origin proxies noVNC, so the URL above
        # is dead until one of these tunnels exists. Printing the command is the
        # whole point of carrying it into the file — the reader is offline from
        # the cluster by the time they open it.
        forwards = v.get("port_forward_commands") or []
        if forwards:
            lines.append("")
            lines.append("**Reach it from your machine**")
            for cmd in forwards:
                lines.append("")
                lines.append(f"{cmd.get('label')}:")
                lines.append("")
                lines.append("```")
                lines.append(str(cmd.get("command")))
                lines.append("```")
                lines.append("")
                lines.append(f"Then open {cmd.get('open_url')}")
        lines.append("")

    db = s.get("database")
    if db:
        lines.append("## Database")
        lines.append("")
        if db.get("provider") == "postgres" and db.get("postgres"):
            p = db["postgres"]
            lines.append("- **Provider:** PostgreSQL")
            lines.append(f"- **Host:** `{p['host']}`")
            lines.append(f"- **Port:** `{p['port']}`")
            lines.append(f"- **Database:** `{p['database']}`")
            lines.append(f"- **User:** `{p['user']}`")
            lines.append(f"- **Password:** `{p['password']}`")
            if p.get("sslmode"):
                lines.append(f"- **SSL mode:** `{p['sslmode']}`")
            lines.append("")
            lines.append("Connection string:")
            lines.append("")
            lines.append("```")
            lines.append(_pg_connection_string(p))
            lines.append("```")
        elif db.get("sqlite"):
            lines.append("- **Provider:** SQLite")
            lines.append(f"- **Path:** `{db['sqlite']['path']}`")
        lines.append("")

    vector = s.get("vectorStore")
    if vector and vector.get("provider") != "none":
        lines.append("## Vector Store")
        lines.append("")
        if vector.get("provider") == "qdrant" and vector.get("qdrant"):
            q = vector["qdrant"]
            lines.append("- **Provider:** Qdrant")
            lines.append(f"- **Host:** `{q['host']}`")
            lines.append(f"- **Port:** `{q['port']}`")
            lines.append(f"- **HTTPS:** {'yes' if q.get('https') else 'no'}")
            if q.get("api_key"):
                lines.append(f"- **API key:** `{q['api_key']}`")
            lines.append("")
            lines.append("URL:")
            lines.append("")
            lines.append("```")
            lines.append(_qdrant_url(q))
            lines.append("```")
        elif vector.get("provider") == "chroma" and vector.get("chroma"):
            c = vector["chroma"]
            lines.append("- **Provider:** Chroma")
            lines.append(f"- **Mode:** {c['mode']}")
            if c["mode"] == "http":
                if c.get("host"):
                    lines.append(f"- **Host:** `{c['host']}`")
                if c.get("port") is not None:
                    lines.append(f"- **Port:** `{c['port']}`")
                lines.append(f"- **SSL:** {'yes' if c.get('ssl') else 'no'}")
                if c.get("api_key"):
                    lines.append(f"- **API key:** `{c['api_key']}`")
            elif c.get("persist_path"):
                lines.append(f"- **Persist path:** `{c['persist_path']}`")
        lines.append("")

    lines.append("## Embedding")
    lines.append("")
    lines.append(f"- **Enabled:** {'yes' if s['embedding'].get('enabled') else 'no'}")
    if s["embedding"].get("provider"):
        lines.append(f"- **Provider:** `{s['embedding']['provider']}`")
    lines.append("")

    if s["channels"]:
        lines.append("## Channels")
        lines.append("")
        for ch in s["channels"]:
            lines.append(f"- `{ch['type']}` ({ch['mode']}) — id: `{ch['id']}`")
        lines.append("")

    return "\n".join(lines)


def render_json(s: Mapping[str, Any]) -> str:
    return json.dumps(s, indent=2, ensure_ascii=False) + "\n"


_ENV_NEEDS_QUOTING = re.compile(r"[\s\"'#$`\\]")


def _quote_env_value(v: str) -> str:
    if v == "":
        return ""
    if _ENV_NEEDS_QUOTING.search(v):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return v


def _env_line(key: str, value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    else:
        rendered = str(value)
    return f"{key}={_quote_env_value(rendered)}"


def render_env(s: Mapping[str, Any]) -> str:
    out: list[str] = []

    def push(line: str | None) -> None:
        if line is not None:
            out.append(line)

    out.append("# Cremind configuration export")
    out.append(f"# Generated at {s['generatedAt']}")
    out.append("# Sensitive: contains JWT token, passwords, and API keys.")
    if s.get("scope") == "profile":
        out.append("# Per-profile file: database, vector-store, desktop and Kubernetes")
        out.append("# settings are only in the admin profile's export.")
    out.append("")

    out.append("# --- Profile & Token ---")
    push(_env_line("CREMIND_PROFILE", s["profile"]))
    # CREMIND_SERVER is the name the `cremind` CLI reads (app/cli/config.py).
    if s.get("agentUrlPendingHttps"):
        out.append("# HTTPS address; active after the post-setup restart.")
    push(_env_line("CREMIND_SERVER", s["agentUrl"]))
    push(_env_line("CREMIND_LOGIN_URL", s["loginUrl"]))
    push(_env_line("CREMIND_TOKEN", s["token"]))
    push(_env_line("CREMIND_TOKEN_EXPIRES_AT", s["tokenExpiresAt"]))
    push(_env_line("CREMIND_TOKEN_FILE", s["tokenFile"]))
    out.append("")

    out.append("# --- Deployment ---")
    push(_env_line("CREMIND_DEPLOYMENT_TYPE", s["deployment"]["type"]))
    push(_env_line("CREMIND_DEPLOYMENT_MODE", s["deployment"]["mode"]))
    for k, v in (s["deployment"].get("customFields") or {}).items():
        push(_env_line(f"CREMIND_{k.upper()}", v))
    out.append("")

    if s.get("kubernetes"):
        k8s = s["kubernetes"]
        out.append("# --- Kubernetes ---")
        push(_env_line("K8S_NAMESPACE", k8s.get("namespace")))
        push(_env_line("K8S_RELEASE", k8s.get("release")))
        push(_env_line("K8S_DEPLOYMENT", k8s.get("deployment")))
        push(_env_line("K8S_SERVICE", k8s.get("service")))
        push(_env_line("K8S_SERVICE_PORT", k8s.get("servicePort")))
        push(_env_line("K8S_IDENTITY_SOURCE", k8s.get("source")))
        push(_env_line("K8S_PORT_FORWARD", k8s.get("portForward")))
        out.append("")

    out.append("# --- Project Paths ---")
    push(_env_line("CREMIND_USER_WORKING_DIR", s["workingDir"]))
    push(_env_line("CREMIND_SYSTEM_DIR", s["systemDir"]))
    out.append("")

    if s.get("vnc"):
        v = s["vnc"]
        out.append("# --- VNC Desktop ---")
        push(_env_line("VNC_PASSWORD", v.get("password")))
        push(_env_line("VNC_HOST", v.get("host")))
        push(_env_line("VNC_NOVNC_PORT", v.get("novnc_port")))
        push(_env_line("VNC_PORT", v.get("vnc_port")))
        push(_env_line("VNC_NOVNC_URL", v.get("novnc_url")))
        push(_env_line("VNC_ENDPOINT", v.get("vnc_endpoint")))
        push(_env_line("VNC_RESOLUTION", v.get("resolution")))
        out.append("")

    if s.get("database"):
        db = s["database"]
        out.append("# --- Database ---")
        push(_env_line("DB_PROVIDER", db["provider"]))
        if db["provider"] == "postgres" and db.get("postgres"):
            p = db["postgres"]
            push(_env_line("PG_HOST", p.get("host")))
            push(_env_line("PG_PORT", p.get("port")))
            push(_env_line("PG_DATABASE", p.get("database")))
            push(_env_line("PG_USER", p.get("user")))
            push(_env_line("PG_PASSWORD", p.get("password")))
            push(_env_line("PG_SSLMODE", p.get("sslmode")))
            push(_env_line("PG_URL", _pg_connection_string(p)))
        elif db.get("sqlite"):
            push(_env_line("SQLITE_DB_PATH", db["sqlite"]["path"]))
        out.append("")

    vector = s.get("vectorStore")
    if vector and vector.get("provider") != "none":
        out.append("# --- Vector Store ---")
        push(_env_line("VECTORSTORE_PROVIDER", vector["provider"]))
        if vector["provider"] == "qdrant" and vector.get("qdrant"):
            q = vector["qdrant"]
            push(_env_line("QDRANT_HOST", q.get("host")))
            push(_env_line("QDRANT_PORT", q.get("port")))
            push(_env_line("QDRANT_HTTPS", q.get("https")))
            push(_env_line("QDRANT_API_KEY", q.get("api_key")))
            push(_env_line("QDRANT_URL", _qdrant_url(q)))
        elif vector["provider"] == "chroma" and vector.get("chroma"):
            c = vector["chroma"]
            push(_env_line("CHROMA_MODE", c.get("mode")))
            push(_env_line("CHROMA_HOST", c.get("host")))
            push(_env_line("CHROMA_PORT", c.get("port")))
            push(_env_line("CHROMA_SSL", c.get("ssl")))
            push(_env_line("CHROMA_API_KEY", c.get("api_key")))
            push(_env_line("CHROMA_PERSIST_PATH", c.get("persist_path")))
        out.append("")

    out.append("# --- Embedding ---")
    push(_env_line("EMBEDDING_ENABLED", s["embedding"].get("enabled")))
    push(_env_line("EMBEDDING_PROVIDER", s["embedding"].get("provider")))
    out.append("")

    if s["channels"]:
        out.append("# --- Channels ---")
        for i, ch in enumerate(s["channels"]):
            push(_env_line(f"CHANNEL_{i}_TYPE", ch.get("type")))
            push(_env_line(f"CHANNEL_{i}_MODE", ch.get("mode")))
            push(_env_line(f"CHANNEL_{i}_ID", ch.get("id")))
        out.append("")

    return "\n".join(out)


def render(fmt: str, snapshot: Mapping[str, Any]) -> str:
    if fmt == "json":
        return render_json(snapshot)
    if fmt == "env":
        return render_env(snapshot)
    return render_markdown(snapshot)
