"""Server configuration and setup API endpoints."""

import json
import os
import re
import secrets
import time
from pathlib import Path

import jwt
from datetime import datetime, timezone, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_admin
from app.config.bootstrap import write_bootstrap
from app.config.settings import (
    BaseConfig,
    DEFAULT_USER_WORKING_DIR,
    relocate_system_directory,
)
from app.config.install_catalog import (
    get_active_install_mode,
    load_install_catalog,
)
from app.config.setup_profiles import get_active_setup_profile_id, list_setup_profiles
from app.events.setup_progress_bus import publish_setup_event
from app.runtime import BootedState
from app.storage import (
    invalidate_storage_singletons,
)
from app.utils.logger import logger
from app.utils.persona import ensure_persona_file

# Server-config keys that may not be set via the generic ``server_config``
# write path during the wizard. The DB-provider choice itself lives in
# bootstrap.toml (chicken-and-egg: we have to know the provider before we
# can connect to any DB), so it must never leak into the SQLite/Postgres
# ``server_config`` table. ``system_dir`` is resolved from the
# ``CREMIND_SYSTEM_DIR`` env var (and the file it lives in), not the DB —
# it must also be filtered out of generic server_config writes.
_BOOTSTRAP_ONLY_KEYS = {"db_provider", "postgres", "system_dir"}


# Profile name validation: lowercase, numbers, underscore, hyphen only
PROFILE_NAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")


def _emit_setup(step: str, message: str, level: str = "info") -> None:
    """Publish one progress entry for the Setup Wizard live-log stream.

    Never raises — a broken bus must not break setup. ``level`` is one of
    ``info`` / ``success`` / ``warning`` / ``error``; the UI uses it to
    colour the log line.
    """
    try:
        publish_setup_event(
            {
                "step": step,
                "message": message,
                "level": level,
                "ts": time.time(),
            }
        )
    except Exception:  # noqa: BLE001
        logger.debug("setup-progress emit failed", exc_info=True)


def _validate_profile_name(name: str) -> str | None:
    """Validate profile name. Returns error message or None if valid."""
    if not name:
        return "Profile name is required"
    if not PROFILE_NAME_PATTERN.match(name):
        return "Profile name must contain only lowercase letters, numbers, hyphens, and underscores"
    if len(name) > 64:
        return "Profile name must be 64 characters or less"
    return None


def _tool_feature_keys_from_payload(body: dict) -> list[str]:
    """Subset of feature keys required by the built-in tools the wizard enabled.

    Pulled out from :func:`_features_required_by_setup_payload` so the
    setup handler can identify "tool-derived" features and treat their
    install failures leniently (continue setup, mark tool unavailable)
    versus core features (embedding, llm, postgres) which still fail
    setup hard.
    """
    from app.tools.builtin import feature_keys_for_tool_ids

    tool_configs = body.get("tool_configs") or {}
    enabled_tool_ids: list[str] = []
    if isinstance(tool_configs, dict):
        for tool_id, cfg in tool_configs.items():
            if not isinstance(cfg, dict):
                continue
            if str(cfg.get("_enabled", "")).strip().lower() == "true":
                enabled_tool_ids.append(tool_id)
    return feature_keys_for_tool_ids(enabled_tool_ids)


def _embedding_feature_keys(embedding_body: dict) -> list[str]:
    """Return the feature keys an enabled embedding config implies.

    ``embedding_body`` is the same shape ``PUT /api/config/embedding``
    receives (no ``embedding_config`` wrapper). Returns an empty list
    when embedding is disabled. Always derives both the embedding
    provider's feature and the vector-store provider's feature so the
    preflight in :func:`handle_put_embedding_config` doesn't need to
    second-guess which extras the apply step will reach for.
    """
    from app.features.manifest import FEATURES

    if not embedding_body.get("enabled"):
        return []
    out: list[str] = []
    provider = str(embedding_body.get("provider") or "me5").lower()
    feature_key = f"embedding.{provider}"
    if feature_key in FEATURES:
        out.append(feature_key)
    vs = embedding_body.get("vectorstore") or {}
    vs_provider = str(vs.get("provider") or "qdrant").lower()
    vs_key = f"vectorstore.{vs_provider}"
    if vs_key in FEATURES:
        out.append(vs_key)
    return out


def _features_required_by_setup_payload(body: dict) -> list[str]:
    """Map a wizard payload to the feature keys whose deps it implies.

    Used by :func:`handle_setup` to install only the extras groups the
    user actually opted into — vector embedding deps stay off disk for
    an LLM-only install, Postgres deps stay off for a SQLite install,
    etc.
    """
    from app.features.manifest import FEATURES, LLM_PROVIDER_TO_FEATURE

    features: list[str] = []

    server_config = body.get("server_config") or {}
    if str(server_config.get("db_provider", "")).lower() == "postgres":
        features.append("postgres")

    emb = body.get("embedding_config") or {}
    if emb.get("enabled"):
        provider = str(emb.get("provider") or "me5").lower()
        feature_key = f"embedding.{provider}"
        if feature_key in FEATURES:
            features.append(feature_key)
        vs = emb.get("vectorstore") or {}
        vs_provider = str(vs.get("provider") or "qdrant").lower()
        vs_key = f"vectorstore.{vs_provider}"
        if vs_key in FEATURES:
            features.append(vs_key)

    # LLM provider keys appear as ``<provider>.api_key`` /
    # ``<provider>.auth_method`` / etc. in the flat llm_config dict, plus
    # often as ``provider`` -> ``<name>`` somewhere. Derive the provider
    # set by collecting the prefix of any key with a ``.`` separator.
    llm_config = body.get("llm_config") or {}
    providers_in_use: set[str] = set()
    for key in llm_config.keys():
        if "." in key:
            providers_in_use.add(key.split(".", 1)[0].lower())
    for provider in providers_in_use:
        feature = LLM_PROVIDER_TO_FEATURE.get(provider)
        if feature is None:
            # Generic OpenAI-compatible third-parties (chutes, deepseek,
            # mistral, ...). They all use the openai SDK so the
            # ``llm-openai`` group is the right one to install.
            feature = "llm.openai_compatible"
        if feature in FEATURES:
            features.append(feature)

    # Channel adapters declared in the wizard.
    for entry in body.get("channel_configs") or []:
        if not isinstance(entry, dict):
            continue
        ch_type = str(entry.get("channel_type", "")).lower()
        mode = str(entry.get("mode") or "").lower()
        if ch_type == "telegram":
            key = "channel.telegram.userbot" if mode == "userbot" else "channel.telegram.bot"
            if key in FEATURES:
                features.append(key)

    # Built-in tools the user enabled in the wizard. Each tool config row
    # carries an ``_enabled`` flag (string ``"true"`` / ``"false"``);
    # tools whose ``TOOL_CONFIG["requires_feature"]`` names a feature get
    # that feature pip-installed. Skills (tool_type == "skill") never
    # appear here because skills don't declare optional Python deps.
    for feature_key in _tool_feature_keys_from_payload(body):
        if feature_key in FEATURES:
            features.append(feature_key)

    # De-dupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for k in features:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


class _ProvisionFailed(Exception):
    """Internal: surfaces a provisioner error as a 400 in :func:`handle_setup`.

    We re-raise :class:`ProvisionError` as this thinner type so the
    setup handler can write a single try/except around all of its
    pre-persist provisioning without dragging service-internal types
    into the wizard's error path.
    """


def _reject_external_in_docker_install(service_id: str) -> str | None:
    """Backend-side enforcement of the Docker-install policy.

    The :func:`get_service_capabilities` endpoint filters External out
    of every service that supports Docker mode when the install can
    drive ``docker compose``. The Setup Wizard's UI honours that, but a
    hand-crafted submission could still try External. Reject those
    explicitly so the failure mode is "wizard says no", not "Cremind
    tries to connect to a localhost Postgres that doesn't exist."

    Returns an error message string to send back as a 400, or ``None``
    if the submission is allowed.
    """
    from app.services.manifest import get_service
    from app.services.provisioner import docker_available

    if not docker_available():
        return None
    try:
        spec = get_service(service_id)
    except KeyError:
        return None
    if "docker" not in spec.supported_modes:
        return None
    return (
        f"This install runs Cremind in Docker, so {spec.display_name} "
        "is provisioned by Cremind — External mode is not offered here. "
        "Pick Docker (or Native if the service supports it) in the wizard."
    )


async def _resolve_vectorstore(embedding_body: dict) -> dict:
    """Run the vectorstore provisioner and return a ready-to-persist body.

    The wizard sends ``embedding_config`` with a per-provider
    ``deployment_mode``:

    - ``docker`` / ``native`` → call the provisioner so the service is
      up before we even validate the connection. The returned
      :attr:`ProvisionedService.connection` is merged into the provider
      block so :func:`persist_embedding_config` gets the *effective*
      host/port/persist_path (not whatever the wizard form happened to
      hold for fields the user didn't fill in).
    - ``external`` → no provisioning; the existing path-through-to-
      ``persist_embedding_config`` validates required fields.

    Embedding-disabled or missing-vectorstore payloads pass through
    unchanged.

    Re-raises :class:`ProvisionError` as :class:`_ProvisionFailed` so
    the caller can write a uniform 400 handler without importing
    service-internal types.
    """
    from app.services.manifest import SERVICES
    from app.services.provisioner import ProvisionError, provision

    if not embedding_body.get("enabled"):
        return embedding_body
    vs_in = embedding_body.get("vectorstore") or {}
    provider = (vs_in.get("provider") or "").strip().lower()
    if provider not in SERVICES:
        # Unknown / unset provider — let persist_embedding_config raise
        # the usual ValueError so the wizard's existing 400 fires.
        return embedding_body

    # The wizard nests per-provider details under ``vectorstore[<provider>]``;
    # the deployment_mode lives there. Fall back to ``external`` so
    # legacy payloads (without deployment_mode) keep working as before.
    provider_block = dict(vs_in.get(provider) or {})
    deployment_mode = (provider_block.get("deployment_mode") or "external").strip().lower()

    if deployment_mode == "external":
        # Docker-install policy: External is hidden from the wizard for
        # any service that supports Docker mode. Reject a hand-crafted
        # submission too.
        err = _reject_external_in_docker_install(provider)
        if err is not None:
            raise _ProvisionFailed(err)
        # Stamp the mode so persist_embedding_config can record it for
        # the UI's roundtrip; nothing to provision.
        provider_block["deployment_mode"] = "external"
        _emit_setup(
            "vectorstore",
            f"Using external {provider.capitalize()} (no provisioning).",
        )
    else:
        _emit_setup(
            "vectorstore",
            f"Provisioning {provider.capitalize()} ({deployment_mode} mode)…",
        )
        try:
            provisioned = await provision(provider, deployment_mode, provider_block)
        except ProvisionError as exc:
            _emit_setup(
                "vectorstore",
                f"{provider.capitalize()} provisioning failed: {exc}",
                level="error",
            )
            raise _ProvisionFailed(str(exc)) from exc
        provider_block.update(provisioned.connection)
        provider_block["deployment_mode"] = deployment_mode
        _emit_setup(
            "vectorstore",
            f"{provider.capitalize()} ready.",
            level="success",
        )

    new_vs = dict(vs_in)
    new_vs[provider] = provider_block
    new_vs["deployment_mode"] = deployment_mode
    new_body = dict(embedding_body)
    new_body["vectorstore"] = new_vs
    return new_body


def _generate_token(jwt_secret: str, profile: str, hours: int | None = None) -> tuple[str, str]:
    """Generate a JWT token for a profile. Returns (token, expires_at)."""
    if hours is None:
        hours = BaseConfig.JWT_EXPIRATION_HOURS
    now = datetime.now(timezone.utc)
    payload = {
        "sub": profile,
        "profile": profile,
        "iat": now,
        "exp": now + timedelta(hours=hours),
    }
    token = jwt.encode(payload, jwt_secret, algorithm="HS256")
    expires_at = (now + timedelta(hours=hours)).isoformat()
    return token, expires_at


def get_config_routes(state: BootedState) -> list[Route]:
    """Setup-wizard / server-config endpoints.

    Always registered — pre-storage too — so the Setup Wizard can run before
    any DB file exists. Handlers that need post-storage objects resolve them
    via ``state`` at call time. Endpoints that strictly require storage
    return 503 with ``"Setup is not complete"`` while ``state.storage_ready``
    is False.

    The DB-provider choice in ``server_config`` is what flips the server
    out of deferred-storage mode: :func:`handle_setup` validates the choice,
    writes ``bootstrap.toml``, then calls ``state.boot_fn`` to materialise
    storage, register the rest of the API routes, and start channel
    adapters in-process.
    """

    def _require_storage() -> JSONResponse | None:
        if not state.storage_ready:
            return JSONResponse(
                {"error": "Setup is not complete"},
                status_code=503,
            )
        return None

    async def handle_install_catalog(request: Request) -> JSONResponse:
        """Return the install/setup catalog plus the active install mode.

        Public (pre-auth) so the Setup Wizard can render its installer
        stage and the service-mode radios without holding a token. The
        payload is purely descriptive — labels, descriptions, and the
        mode-rule visibility table; no secrets or runtime state.

        ``active_install_mode`` reflects the ``INSTALL_MODE`` env var
        written by the installer. ``null`` means the variable is unset
        or names an unknown mode, in which case service-mode filtering
        becomes a no-op.
        """
        try:
            catalog = load_install_catalog()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to load install catalog: {exc}")
            catalog = {}
        try:
            active = get_active_install_mode()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to resolve active install mode: {exc}")
            active = None
        return JSONResponse({"catalog": catalog, "active_install_mode": active})

    async def handle_setup_profiles(request: Request) -> JSONResponse:
        """Return the wizard's environment-preset catalogue plus the active id.

        Public (pre-auth) like ``/api/config/setup-status`` so the wizard can
        fetch this before any token exists.

        ``selected`` is the preset id taken from ``SETUP_WIZARD_ENV`` (read
        fresh from the process environment, populated from the project's
        ``.env`` file at startup). ``null`` when the variable is unset, blank,
        or names a profile that doesn't exist — in that case the wizard
        falls back to its built-in component-level defaults.

        The preset is purely a pre-fill hint: the wizard seeds form values
        from it, but every field stays editable so the operator can override
        anything before submitting.
        """
        try:
            profiles = list_setup_profiles()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to load setup profiles: {exc}")
            profiles = []
        try:
            selected = get_active_setup_profile_id()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to resolve active setup profile: {exc}")
            selected = None
        return JSONResponse({"profiles": profiles, "selected": selected})

    async def handle_setup_status(request: Request) -> JSONResponse:
        """Check if first-time setup has been completed. No auth required.

        Also accepts ?profile=xxx to check if a specific profile exists.

        In deferred-storage mode (no ``bootstrap.toml`` yet — Setup Wizard
        hasn't run) this answers ``setup_complete=false`` without touching
        the database, because doing so would create a SQLite file before
        the user has picked a backend.
        """
        profile = request.query_params.get("profile")
        if not state.storage_ready:
            result = {"setup_complete": False, "storage_ready": False}
            if profile:
                result["profile_exists"] = False
            return JSONResponse(result)

        config_storage = state.config_storage
        conversation_storage = state.conversation_storage
        setup_complete = config_storage.is_setup_complete()
        result = {
            "setup_complete": setup_complete,
            "storage_ready": True,
        }
        if profile:
            result["profile_exists"] = await conversation_storage.profile_exists(profile)
        # When setup is marked complete, report whether any profiles exist.
        # This lets clients detect the orphaned state where setup_complete=true
        # but all profiles have been deleted externally.
        if setup_complete:
            profiles = await conversation_storage.list_profiles()
            visible = [p for p in profiles if not p["name"].startswith("__")]
            result["has_profiles"] = len(visible) > 0
        return JSONResponse(result)

    async def _persist_db_provider_choice(server_config: dict) -> str | None:
        """Validate the wizard's ``db_provider`` choice and write bootstrap.toml.

        Returns ``None`` on success or an error string for the caller to
        surface as a 400 response. Cases:

        - ``bootstrap.toml`` already exists → choice is locked. Silently
          drop any disagreement in the body (the wizard UI hides the
          field after first setup; treat the server as authoritative).
        - ``db_provider=sqlite`` → write bootstrap.toml. No connection to
          validate. ``deployment_mode`` is ignored — SQLite is always
          local.
        - ``db_provider=postgres`` with ``deployment_mode=docker`` →
          provision the bundled compose service (starts the container,
          waits for readiness), then validate the connection against
          the now-running instance.
        - ``db_provider=postgres`` with ``deployment_mode=external`` (or
          omitted) → validate the user-supplied connection details.

        Only persist after validation; a failed health-check must leave
        the working dir untouched so the next attempt re-enters
        deferred-storage mode cleanly. Storage initialisation
        (migrations, tool persistence, agent build) is the caller's
        job — see :func:`handle_setup`, which invokes ``state.boot_fn``
        after this returns.
        """
        from app.databases import (
            create_database_provider,
            set_database_provider,
        )
        from app.databases.postgres import PostgresDatabaseProvider
        from app.services.provisioner import ProvisionError, provision

        requested = (server_config.get("db_provider") or "sqlite").strip().lower()
        if requested not in ("sqlite", "postgres"):
            return f"Unknown db_provider: {requested!r}"

        bootstrap_path = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "bootstrap.toml"
        if bootstrap_path.exists():
            # Choice is locked; silently ignore any disagreement.
            return None

        _emit_setup("database", f"Configuring database backend: {requested}…")

        if requested == "postgres":
            pg_in = dict(server_config.get("postgres") or {})
            deployment_mode = (pg_in.get("deployment_mode") or "external").strip().lower()

            if deployment_mode == "docker":
                _emit_setup(
                    "database",
                    "Starting PostgreSQL container (docker compose)…",
                )
                # Provisioner brings up the container, returns the
                # in-network connection (host=postgres, port=5432) plus
                # the resolved credentials (generated if missing).
                try:
                    provisioned = await provision("postgres", "docker", pg_in)
                except ProvisionError as exc:
                    return f"Could not start PostgreSQL container: {exc}"
                pg_in.update(provisioned.connection)
                pg_in.setdefault("sslmode", "disable")
            elif deployment_mode == "external":
                policy_err = _reject_external_in_docker_install("postgres")
                if policy_err is not None:
                    return policy_err
                required = ("host", "database", "user")
                missing = [k for k in required if not pg_in.get(k)]
                if missing:
                    return f"Postgres (external) connection requires: " f"{', '.join(missing)}"
            else:
                return (
                    f"Unsupported deployment_mode for PostgreSQL: "
                    f"{deployment_mode!r} (must be 'docker' or 'external')"
                )

            _emit_setup(
                "database",
                f"Validating PostgreSQL connection at {pg_in['host']}:{pg_in.get('port') or 5432}…",
            )
            candidate = PostgresDatabaseProvider(
                host=pg_in["host"],
                port=int(pg_in.get("port") or 5432),
                database=pg_in["database"],
                user=pg_in["user"],
                password=pg_in.get("password", ""),
                sslmode=pg_in.get("sslmode", "prefer"),
            )
            try:
                await candidate.health_check()
            except Exception as exc:  # noqa: BLE001
                await candidate.dispose()
                _emit_setup(
                    "database",
                    f"PostgreSQL health-check failed: {exc}",
                    level="error",
                )
                return f"Could not connect to PostgreSQL: {exc}"
            await candidate.dispose()
            _emit_setup("database", "PostgreSQL reachable.", level="success")

            write_bootstrap(
                {
                    "db_provider": "postgres",
                    "postgres": {
                        "deployment_mode": deployment_mode,
                        "host": pg_in["host"],
                        "port": int(pg_in.get("port") or 5432),
                        "database": pg_in["database"],
                        "user": pg_in["user"],
                        "password": pg_in.get("password", ""),
                        "sslmode": pg_in.get("sslmode", "prefer"),
                    },
                }
            )
        else:
            # SQLite path: nothing to validate, just lock in the choice.
            write_bootstrap({"db_provider": "sqlite"})

        _emit_setup("database", "Writing bootstrap.toml…")

        # Refresh $CREMIND_INSTALL_DIR/credentials.toml so the user-facing
        # summary picks up the new [postgres] block (or drops it on sqlite).
        # A failure to write this informational file must not roll back the
        # database provisioning that just succeeded.
        try:
            from app.config.credentials_file import write_credentials_file

            write_credentials_file()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Failed to update credentials.toml: {exc} — continuing")

        # Rebuild the provider singleton from the freshly-written
        # bootstrap.toml. Clear caches so the next ``get_*_storage()`` call
        # builds against the new provider. In deferred mode no engine was
        # ever materialised, so there's nothing to dispose; in the legacy
        # reconfigure path the caller's lock check above already short-
        # circuited.
        set_database_provider(None)
        invalidate_storage_singletons()
        set_database_provider(create_database_provider())

        logger.info(f"Database provider configured: {requested} (first-setup).")
        return None

    async def handle_setup(request: Request) -> JSONResponse:
        """Complete setup for a profile.

        First profile (admin): unauthenticated — this is the bootstrap
        window before any JWT can possibly exist. Validates the DB-provider
        choice, writes ``bootstrap.toml``, runs the deferred boot
        (storage init + tool registration + agent build) **if it hasn't run
        yet**, then saves server / LLM / tool config, creates the profile,
        generates the token, and marks setup complete.

        Subsequent profiles: requires the admin profile's JWT. Creates
        the profile, saves LLM and tool configs, and generates a token.
        Server-level config cannot be changed for non-first profiles.

        Expects JSON body with:
        - profile: str (required) — the profile name to create
        - server_config: dict of server settings (first setup only)
        - llm_config: dict of LLM settings
        - tool_configs: dict of {tool_name: {key: value}}
        """
        # In deferred-storage mode (no ``bootstrap.toml`` yet), this is a
        # first-setup by definition — there is no DB to ask. Once storage
        # is up, defer to the persisted ``setup_complete`` flag.
        if not state.storage_ready:
            is_first_setup = True
        else:
            is_first_setup = not state.config_storage.is_setup_complete()

        # Only the admin can onboard additional profiles. The first-run
        # bootstrap is intentionally unauthenticated because no JWT can
        # exist yet.
        if not is_first_setup:
            denied = require_admin(request)
            if denied is not None:
                return denied

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        profile_name = body.get("profile", "").strip()

        # Validate profile name
        validation_error = _validate_profile_name(profile_name)
        if validation_error:
            return JSONResponse({"error": validation_error}, status_code=400)

        # First setup must be 'admin'
        if is_first_setup and profile_name != "admin":
            return JSONResponse(
                {"error": "First profile must be named 'admin'"},
                status_code=400,
            )

        _emit_setup(
            "start",
            (
                f"Starting setup for profile {profile_name!r}…"
                if is_first_setup
                else f"Creating profile {profile_name!r}…"
            ),
        )

        # ── Optional-feature install ─────────────────────────────────────
        # Map the wizard payload to the feature keys whose deps it
        # implies, then run a single ``pip install cremind[...]==<version>``
        # for any that aren't already importable. We do this BEFORE
        # ``_persist_db_provider_choice`` because the postgres branch
        # there immediately connects via ``asyncpg`` / ``psycopg`` —
        # without the install those imports fail at the DB layer with a
        # less actionable error.
        #
        # If any installed feature is flagged ``requires_restart=True``
        # (sentence-transformers, watchdog, channels), we still want the
        # rest of the wizard to finish — the config gets persisted and
        # the response tells the user to restart ``cremind serve``. The
        # apply-now steps that depend on those features are skipped on
        # this request and will run on the next boot.
        required_features = _features_required_by_setup_payload(body)
        restart_required_from_install = False
        installed_features: list[str] = []
        failed_features: list[str] = []
        if required_features:
            from app.features import installer
            from app.features.manifest import missing_features as _missing

            try:
                missing = _missing(required_features)
            except KeyError as e:
                return JSONResponse(
                    {"error": f"Unknown feature in payload: {e}"},
                    status_code=400,
                )

            if missing:
                logger.info(
                    f"[setup] Installing features for wizard payload: {missing}",
                )
                _emit_setup(
                    "features",
                    f"Installing optional features: {', '.join(missing)}…",
                )

                # Synchronous install — the UI either streamed progress
                # via /api/features/install before submitting, or it's
                # showing a spinner while we wait. Either way the call
                # is idempotent and safe to repeat. Re-publish each pip
                # event into the setup-progress bus so the wizard sees
                # incremental log lines instead of a single multi-minute
                # silence.
                def _forward_install_event(evt) -> None:
                    level = "error" if not evt.ok else "info"
                    _emit_setup("features", evt.message, level=level)

                result = installer.install_features(missing, _forward_install_event)
                installed_features = result.installed
                failed_features = result.failed
                if result.failed:
                    # Split failures by source: core features (postgres /
                    # embedding / vectorstore / llm / channels) are
                    # load-bearing and still 500 the wizard. Tool-derived
                    # features (browser, documents, google) are
                    # best-effort — the tool falls back to MissingDependency
                    # at first use and the user can retry from Settings,
                    # so a failed pip install shouldn't trap the user
                    # mid-wizard.
                    tool_feature_set = set(_tool_feature_keys_from_payload(body))
                    core_failed = [k for k in result.failed if k not in tool_feature_set]
                    tool_failed = [k for k in result.failed if k in tool_feature_set]
                    if core_failed:
                        _emit_setup(
                            "features",
                            f"Feature install failed: {', '.join(core_failed)}",
                            level="error",
                        )
                        return JSONResponse(
                            {
                                "error": ("Some required features could not be installed: " + ", ".join(core_failed)),
                                "install_error": result.error,
                                "failed_features": core_failed,
                            },
                            status_code=500,
                        )
                    # Tool-only failure path: log + continue. The wizard
                    # response will carry ``failed_features`` so the UI
                    # can render "Browser feature could not be installed
                    # — the tool will report MissingDependency until you
                    # retry from Settings".
                    _emit_setup(
                        "features",
                        (
                            f"Optional tool feature(s) failed to install "
                            f"({', '.join(tool_failed)}); affected tools will "
                            "report MissingDependency until you retry from "
                            "Settings."
                        ),
                        level="warning",
                    )
                if result.restart_required:
                    restart_required_from_install = True
                    _emit_setup(
                        "features",
                        "Features installed (server restart required to load them).",
                        level="warning",
                    )
                else:
                    _emit_setup(
                        "features",
                        "Features installed.",
                        level="success",
                    )

        # ── System Directory relocation (first-setup only) ───────────────
        # If the wizard payload includes a custom ``system_dir`` and it
        # differs from the current ``CREMIND_SYSTEM_DIR``, relocate now —
        # before ``_persist_db_provider_choice`` writes ``bootstrap.toml``
        # and before ``boot_fn()`` creates the SQLite DB. At this point
        # the System Directory holds at most an install-script ``.env``,
        # so the move is cheap.
        if is_first_setup:
            requested_system_dir = (body.get("server_config", {}) or {}).get("system_dir")
            if requested_system_dir:
                try:
                    relocated = relocate_system_directory(requested_system_dir)
                    if relocated != os.path.normpath(BaseConfig.CREMIND_SYSTEM_DIR):
                        # ``relocate_system_directory`` already updates
                        # ``BaseConfig.CREMIND_SYSTEM_DIR`` in place, but we
                        # keep this check so logs reflect that a move
                        # actually happened.
                        pass
                    _emit_setup(
                        "system_dir",
                        f"Cremind System Directory set to {relocated!r}.",
                        level="success",
                    )
                except ValueError as exc:
                    _emit_setup(
                        "system_dir",
                        f"System Directory rejected: {exc}",
                        level="error",
                    )
                    return JSONResponse({"error": str(exc)}, status_code=400)

        # ── Database provider selection ──────────────────────────────────
        # First-setup-only. Validate the requested DB provider (Postgres
        # connection check if applicable), write bootstrap.toml, then run
        # the deferred boot to materialise storage and the rest of the
        # API. After this returns, all subsequent writes in this handler
        # land in the chosen DB.
        #
        # For non-first-setup calls, any db_provider field in the body is
        # silently ignored — the choice was made on first setup and is
        # locked. Other profiles can never see or change it.
        if is_first_setup:
            err = await _persist_db_provider_choice(body.get("server_config", {}) or {})
            if err is not None:
                return JSONResponse({"error": err}, status_code=400)

            # Run the deferred boot if we haven't yet. ``state.boot_fn`` is
            # idempotent (guarded by ``state.boot_lock`` + ``storage_ready``),
            # so this is also safe on the legacy reconfigure-from-already-
            # booted path.
            if not state.storage_ready and state.boot_fn is not None:
                _emit_setup(
                    "database",
                    "Initializing storage and tool registry…",
                )
                try:
                    await state.boot_fn()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Deferred storage boot failed during setup")
                    _emit_setup(
                        "database",
                        f"Storage initialization failed: {exc}",
                        level="error",
                    )
                    # Roll back ``bootstrap.toml`` so the next attempt
                    # re-enters deferred mode cleanly. Best-effort —
                    # surface a 500 either way.
                    try:
                        (Path(BaseConfig.CREMIND_SYSTEM_DIR) / "bootstrap.toml").unlink()
                    except OSError:
                        pass
                    from app.databases import set_database_provider as _sdp

                    _sdp(None)
                    invalidate_storage_singletons()
                    state.reset_storage()
                    return JSONResponse(
                        {"error": f"Failed to initialize storage: {exc}"},
                        status_code=500,
                    )

        # Resolve storage handles from the now-booted state.
        if not state.storage_ready:
            # Defensive: only reachable if ``boot_fn`` is missing entirely
            # (misconfigured server). Avoid an opaque AttributeError below.
            return JSONResponse(
                {"error": "Setup is not complete"},
                status_code=503,
            )
        config_storage = state.config_storage
        conversation_storage = state.conversation_storage
        registry = state.registry
        on_first_setup = state.on_first_setup

        # For subsequent profiles, check the profile doesn't already exist
        if not is_first_setup:
            if await conversation_storage.profile_exists(profile_name):
                return JSONResponse(
                    {"error": f"Profile '{profile_name}' already exists"},
                    status_code=409,
                )

        # Create the profile first (needed for FK constraints on llm_config / tool_configs)
        _emit_setup("profile", f"Creating profile {profile_name!r}…")
        if not await conversation_storage.profile_exists(profile_name):
            await conversation_storage.create_profile(profile_name)

        # Create profile directory and copy PERSONA.md template
        ensure_persona_file(profile_name)

        # Save server config only on first setup
        if is_first_setup:
            _emit_setup("server_config", "Saving server configuration…")
            server_config = body.get("server_config", {})
            for key, value in server_config.items():
                # The DB-provider keys live in bootstrap.toml, never in the
                # server_config table — they were applied above.
                if key in _BOOTSTRAP_ONLY_KEYS:
                    continue
                is_secret = key in ("jwt_secret",)
                config_storage.set("server_config", key, str(value), is_secret=is_secret)

            # Ensure the User Working Directory is recorded with a sensible
            # default so all built-in tools have a writable active path on
            # first run, even if the wizard payload didn't include it.
            user_working_dir = server_config.get("user_working_dir")
            if not user_working_dir:
                user_working_dir = DEFAULT_USER_WORKING_DIR
                config_storage.set("server_config", "user_working_dir", user_working_dir)
            expanded_uwd = (
                os.path.expanduser(user_working_dir) if user_working_dir.startswith("~") else user_working_dir
            )
            try:
                os.makedirs(expanded_uwd, exist_ok=True)
            except OSError as exc:
                logger.warning(f"Could not create user working directory {expanded_uwd!r}: {exc}")

            # Generate JWT secret if not provided
            jwt_secret = config_storage.get("server_config", "jwt_secret")
            if not jwt_secret and not BaseConfig.get_jwt_secret():
                jwt_secret = secrets.token_urlsafe(32)
                config_storage.set("server_config", "jwt_secret", jwt_secret, is_secret=True)

            # Vector embedding configuration. Stored in server_config because
            # the model loads once into the Cremind process and is shared
            # across all profiles. Only persisted on first setup.
            from app.lib.embedding_lifecycle import persist_embedding_config

            try:
                resolved_embedding = await _resolve_vectorstore(body.get("embedding_config") or {})
            except _ProvisionFailed as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            try:
                persist_embedding_config(resolved_embedding, config_storage)
            except ValueError as e:
                _emit_setup(
                    "vectorstore",
                    f"Embedding config rejected: {e}",
                    level="error",
                )
                return JSONResponse({"error": str(e)}, status_code=400)

            config_storage.mark_setup_complete()

        # Save LLM and tool configs for ALL profiles (including first setup)
        # (must happen before on_first_setup so tool enabled states are persisted)
        _emit_setup("llm_config", "Saving LLM provider settings…")
        llm_config = body.get("llm_config", {})
        for key, value in llm_config.items():
            is_secret = (
                "api_key" in key
                or "service_account" in key
                or "setup_token" in key
                or "oauth_token" in key
                or "bearer_token" in key
            )
            # auth_method selections are not secrets
            if key.endswith(".auth_method") or key == "auth_method":
                is_secret = False
            config_storage.set("llm_config", key, str(value), is_secret=is_secret, profile=profile_name)

        # Tool config payload from the wizard. The frontend bundles four
        # different settings into a single ``tool_configs[tool_id]`` dict using
        # reserved key prefixes:
        #   - ``_enabled``         -> per-profile enabled state (profile_tools)
        #   - ``_full_reasoning``  -> LLM scope override
        #   - ``_arg.<name>``      -> tool argument
        #   - everything else      -> tool variable (env-style secret/value)
        # Route each to the right storage; otherwise these settings would be
        # silently dropped into the variables table and never honored.
        tool_configs = body.get("tool_configs", {})
        if registry is not None and tool_configs:
            _emit_setup(
                "tool_configs",
                f"Saving configuration for {len(tool_configs)} tool(s)…",
            )
        if registry is not None:
            from app.tools.ids import slugify

            for tool_key, configs in tool_configs.items():
                # Tool keys may arrive as either tool_id (slug) or display name
                tool_id = tool_key if registry.get(tool_key) else slugify(tool_key)
                arg_values: dict[str, object] = {}
                for key, value in configs.items():
                    if key == "_enabled":
                        try:
                            registry.set_profile_tool_enabled(
                                profile_name,
                                tool_id,
                                str(value).lower() == "true",
                            )
                        except (KeyError, ValueError) as e:
                            logger.warning(f"Skipping _enabled for '{tool_id}': {e}")
                        continue
                    if key == "_full_reasoning":
                        registry.config.set_llm_param(
                            tool_id,
                            profile_name,
                            "full_reasoning",
                            str(value).lower() == "true",
                        )
                        continue
                    if key.startswith("_arg."):
                        arg_name = key[len("_arg.") :]
                        try:
                            arg_values[arg_name] = json.loads(value)
                        except (TypeError, ValueError):
                            arg_values[arg_name] = value
                        continue
                    is_secret = "secret" in key.lower() or "key" in key.lower() or "password" in key.lower()
                    registry.config.set_variable(
                        tool_id,
                        profile_name,
                        key,
                        str(value),
                        is_secret=is_secret,
                    )
                if arg_values:
                    registry.config.set_arguments(tool_id, profile_name, arg_values)

        # Per-built-in-tool LLM overrides land in the llm scope of the new tool_configs table.
        agent_configs = body.get("agent_configs", {})
        if registry is not None and agent_configs:
            from app.tools.ids import slugify

            for tool_key, config in agent_configs.items():
                tool_id = tool_key if registry.get(tool_key) else slugify(tool_key)
                for key in ("llm_provider", "llm_model", "reasoning_effort", "full_reasoning"):
                    if key in config and config[key] is not None:
                        registry.config.set_llm_param(
                            tool_id,
                            profile_name,
                            key,
                            config[key],
                        )
                if "system_prompt" in config and config["system_prompt"]:
                    registry.config.set_meta(
                        tool_id,
                        profile_name,
                        "system_prompt",
                        config["system_prompt"],
                    )
                if "description" in config and config["description"]:
                    registry.config.set_meta(
                        tool_id,
                        profile_name,
                        "description",
                        config["description"],
                    )

        # Channels declared during the setup wizard. Validation + creation
        # mirror POST /api/channels but happen pre-token because the setup
        # endpoint is the bootstrap entry point. A single bad channel must
        # not block account creation; per-entry errors are accumulated and
        # echoed back in the response so the wizard can surface them
        # alongside the successfully-created channels.
        channel_configs = body.get("channel_configs") or []
        created_channels: list[dict] = []
        channel_errors: list[dict] = []
        if isinstance(channel_configs, list) and channel_configs:
            _emit_setup(
                "channels",
                f"Registering {len(channel_configs)} channel(s)…",
            )
            from app.api.channels import (
                _decorate,
                _redact,
                create_channel_for_profile,
            )
            from app.config import load_all_channel_catalogs

            channels_catalog = load_all_channel_catalogs()
            for entry in channel_configs:
                if not isinstance(entry, dict):
                    continue
                channel_type = entry.get("channel_type") or "channel"
                _emit_setup("channels", f"Registering channel: {channel_type}…")
                ch, err = await create_channel_for_profile(
                    conversation_storage,
                    profile_name,
                    entry,
                )
                if err is not None:
                    channel_errors.append(
                        {
                            "channel_type": entry.get("channel_type"),
                            "error": err["error"],
                        }
                    )
                    _emit_setup(
                        "channels",
                        f"Channel {channel_type!r} failed: {err['error']}",
                        level="warning",
                    )
                elif ch is not None:
                    created_channels.append(_decorate(_redact(ch, channels_catalog)))
                    _emit_setup(
                        "channels",
                        f"Channel {channel_type!r} registered.",
                        level="success",
                    )

        # Generate and save token
        jwt_secret = config_storage.get("server_config", "jwt_secret") or BaseConfig.get_jwt_secret()
        if not jwt_secret:
            jwt_secret = secrets.token_urlsafe(32)
            config_storage.set("server_config", "jwt_secret", jwt_secret, is_secret=True)

        token, expires_at = _generate_token(jwt_secret, profile_name)

        # Persist token to disk for recovery
        tokens_dir = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "tokens"
        tokens_dir.mkdir(parents=True, exist_ok=True)
        token_file = tokens_dir / f"{profile_name}.token"
        token_file.write_text(token, encoding="utf-8")
        logger.info(f"Token saved to {token_file}")

        # Initialize built-in tools that were skipped at startup due to missing LLM
        if is_first_setup and on_first_setup:
            try:
                await on_first_setup(profile_name)
            except Exception as e:
                logger.warning(f"Post-setup built-in tool initialization failed: {e}")

        # If first setup just turned Vector Embedding on, kick off model
        # load + rebuild in the background so the agent becomes usable
        # without requiring a manual server restart. The wizard reads the
        # embedding-state SSE stream to know when it's safe to proceed.
        #
        # We try this even when the install step above flagged
        # ``restart_required``: those features were freshly pip-installed
        # into the venv and the running process has not yet imported them,
        # so a clean ``import sentence_transformers`` in the worker thread
        # picks them up. On the rare platform where the in-process import
        # fails (e.g. a Windows native install where a previous torch DLL
        # is locked), the lifecycle marks the state ``failed`` and surfaces
        # the error — the user can then restart manually and the next boot
        # applies cleanly via the deferred-boot path.
        if is_first_setup and BaseConfig.is_embedding_enabled():
            from app.config.embedding_state import embedding_state
            from app.lib.embedding_lifecycle import apply_embedding_config_in_background

            if not embedding_state.is_busy() and not embedding_state.is_ready():
                # On first setup the only profile that exists is the one
                # we just created, so we know what to rebuild.
                _emit_setup(
                    "embedding",
                    "Loading vector embedding model in background (this can take a minute)…",
                )
                apply_embedding_config_in_background(
                    profiles=[profile_name],
                    force_rebuild=True,
                )
                logger.info("Vector embedding initialization started in background after first setup.")

        from app.events.settings_state_bus import publish_settings_state_changed

        publish_settings_state_changed(profile_name)

        _emit_setup(
            "done",
            (
                "Setup complete. New features installed — loading in background; "
                "restart Cremind only if the embedding status reports a failure."
                if restart_required_from_install
                else "Setup complete."
            ),
            level="success",
        )

        return JSONResponse(
            {
                "success": True,
                "token": token,
                "expires_at": expires_at,
                "profile": profile_name,
                "embedding_enabled": BaseConfig.is_embedding_enabled(),
                "channels": created_channels,
                "channel_errors": channel_errors,
                # When True, the wizard installed deps for a feature that
                # needs a process restart before its module can be loaded
                # cleanly. The frontend should surface a "Please restart
                # Cremind" notice; the persisted config will apply on the
                # next boot via the normal deferred-boot path.
                "restart_required": restart_required_from_install,
                "installed_features": installed_features,
                # Tool-derived features that failed to pip-install. Setup was
                # NOT aborted (core features all succeeded); the wizard UI
                # uses this list to warn the user that the affected tools
                # will report MissingDependency until they retry from
                # Settings.
                "failed_features": failed_features,
            }
        )

    async def handle_reconfigure(request: Request) -> JSONResponse:
        """Reset setup status to allow reconfiguration from scratch.

        Requires admin auth. Does NOT delete profiles or data.
        """
        if gate := _require_storage():
            return gate
        state.config_storage.delete("server_config", "setup_complete")
        return JSONResponse({"success": True, "message": "Setup status reset. Reload to reconfigure."})

    async def handle_reset_orphaned_setup(request: Request) -> JSONResponse:
        """Reset setup_complete when no profiles exist (orphaned setup state).

        No auth required, but ONLY works when setup_complete=true and zero
        visible profiles exist. This handles the edge case where the DB was
        partially wiped externally.
        """
        if gate := _require_storage():
            return gate
        config_storage = state.config_storage
        conversation_storage = state.conversation_storage
        if not config_storage.is_setup_complete():
            return JSONResponse({"error": "Setup is not complete"}, status_code=400)

        profiles = await conversation_storage.list_profiles()
        visible = [p for p in profiles if not p["name"].startswith("__")]
        if len(visible) > 0:
            return JSONResponse(
                {"error": "Profiles still exist; use authenticated reconfigure instead"},
                status_code=403,
            )

        config_storage.delete("server_config", "setup_complete")
        return JSONResponse({"success": True, "message": "Orphaned setup state cleared."})

    async def handle_embedding_status(request: Request) -> JSONResponse:
        """Report the lifecycle state of the vector embedding subsystem.

        Unauthenticated so the setup wizard can poll while the user is
        still pre-token. Returns ``enabled`` (config flag) plus the
        current load ``status`` and any error from the last attempt.
        """
        from app.config.embedding_state import embedding_state

        return JSONResponse(
            {
                "enabled": BaseConfig.is_embedding_enabled(),
                **embedding_state.to_dict(),
            }
        )

    async def handle_embedding_initialize(request: Request) -> JSONResponse:
        """Trigger an asynchronous load + rebuild of the embedding subsystem.

        Used by the setup wizard right after ``handle_setup`` saved the
        new ``embedding_config``. Returns immediately with ``status =
        initializing``; the wizard polls ``handle_embedding_status``
        until ready or failed.

        If embedding is disabled this returns the disabled state. If a
        busy operation (initialize or rebuild) is already in flight,
        returns its current state without scheduling a duplicate.
        """
        from app.config.embedding_state import embedding_state
        from app.lib.embedding_lifecycle import apply_embedding_config_in_background

        if not BaseConfig.is_embedding_enabled():
            embedding_state.mark_disabled()
            return JSONResponse(
                {
                    "enabled": False,
                    **embedding_state.to_dict(),
                }
            )

        if embedding_state.is_busy() or embedding_state.is_ready():
            return JSONResponse(
                {
                    "enabled": True,
                    **embedding_state.to_dict(),
                }
            )

        if gate := _require_storage():
            return gate
        profile_names = [
            row["name"] for row in await state.conversation_storage.list_profiles() if not row["name"].startswith("__")
        ]
        apply_embedding_config_in_background(profiles=profile_names, force_rebuild=True)

        return JSONResponse(
            {
                "enabled": True,
                **embedding_state.to_dict(),
            },
            status_code=202,
        )

    async def handle_get_embedding_config(request: Request) -> JSONResponse:
        """Return the persisted embedding config (admin only).

        Powers the Embedding Settings panel — secret values like
        ``hf_token`` and the vector-store API keys come back unmasked
        because the user is the admin and is editing them directly.
        """
        denied = require_admin(request)
        if denied is not None:
            return denied
        if gate := _require_storage():
            return gate
        from app.config.embedding_state import embedding_state
        from app.lib.embedding_lifecycle import read_embedding_config

        return JSONResponse(
            {
                "config": read_embedding_config(state.config_storage),
                **embedding_state.to_dict(),
            }
        )

    async def handle_put_embedding_config(request: Request) -> JSONResponse:
        """Persist an updated embedding config and trigger reload + rebuild.

        Admin-only. Body shape mirrors the wizard's ``embedding_config``.
        Returns 202 with the new status; the UI polls
        ``/api/config/embedding/status`` until ready or failed. While
        busy, the agent refuses to run.
        """
        denied = require_admin(request)
        if denied is not None:
            return denied
        if gate := _require_storage():
            return gate

        from app.config.embedding_state import embedding_state
        from app.lib.embedding_lifecycle import (
            apply_embedding_config_in_background,
            persist_embedding_config,
        )

        if embedding_state.is_busy():
            return JSONResponse(
                {
                    "error": ("Embedding subsystem is currently " f"{embedding_state.status.value}; please wait."),
                    **embedding_state.to_dict(),
                },
                status_code=409,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        # Feature pre-flight: if the user just enabled embedding (or
        # switched to a provider whose extras aren't installed), surface
        # a 409 with the same shape ``PUT /api/tools/{id}/enabled``
        # already uses for tool-level features. The frontend opens its
        # install dialog, drives ``POST /api/features/install`` over
        # SSE, and retries this PUT. Without this gate the apply step
        # crashes with ``No module named 'sentence_transformers'``
        # because the setup wizard only installed the extras for the
        # features that were on at setup time.
        if bool(body.get("enabled")):
            from app.features.manifest import FEATURES, missing_features

            try:
                required = _embedding_feature_keys(body)
                missing = missing_features(required)
            except KeyError as exc:
                return JSONResponse(
                    {"error": f"Unknown feature in payload: {exc}"},
                    status_code=400,
                )
            if missing:
                return JSONResponse(
                    {
                        "error": "FeatureNotInstalled",
                        "missing": [
                            {
                                "feature_key": key,
                                "extras": list(FEATURES[key].extras),
                                "requires_restart_after_install": FEATURES[key].requires_restart,
                            }
                            for key in missing
                        ],
                        "message": (
                            "Vector Embedding requires the following optional "
                            "dependencies, which are not installed: "
                            + ", ".join(missing)
                            + ". Install them via POST /api/features/install before enabling."
                        ),
                    },
                    status_code=409,
                )

        try:
            resolved_body = await _resolve_vectorstore(body)
        except _ProvisionFailed as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            persist_embedding_config(resolved_body, state.config_storage)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        # ``defer_apply`` is set by the Settings page after a feature
        # install that needs a process restart (sentence-transformers +
        # torch can't be hot-loaded). The config is persisted as
        # enabled=true so the boot path's ``initialize_embedding_subsystem``
        # picks it up on next start; trying to apply now would just fail
        # with ModuleNotFoundError and leave the state FAILED.
        if bool(body.get("defer_apply")):
            return JSONResponse(
                {
                    "success": True,
                    "deferred": True,
                    **embedding_state.to_dict(),
                },
                status_code=200,
            )

        profile_names = [
            row["name"] for row in await state.conversation_storage.list_profiles() if not row["name"].startswith("__")
        ]
        apply_embedding_config_in_background(profiles=profile_names, force_rebuild=True)

        return JSONResponse(
            {"success": True, **embedding_state.to_dict()},
            status_code=202,
        )

    async def handle_channel_catalog(request: Request) -> JSONResponse:
        """Public catalog of supported channel types.

        Used by the setup wizard before any JWT exists. Mirrors the payload
        shape of the auth-guarded ``/api/channels/catalog`` — the data is
        static metadata loaded from ``app/config/channels/*.toml`` (platform
        names, supported modes, field definitions), so exposing it pre-auth
        is safe and keeps the auth boundary on ``/api/channels/*`` untouched.
        """
        from app.config import load_all_channel_catalogs

        return JSONResponse({"channels": load_all_channel_catalogs()})

    async def handle_get_server_config(request: Request) -> JSONResponse:
        """Get server configuration (non-secret values). Requires auth."""
        if gate := _require_storage():
            return gate
        config = state.config_storage.get_all("server_config", include_secrets=False)
        return JSONResponse({"config": config})

    async def handle_update_server_config(request: Request) -> JSONResponse:
        """Update server configuration. Requires auth (admin only)."""
        if gate := _require_storage():
            return gate
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        config = body.get("config", {})
        cfg_storage = state.config_storage
        for key, value in config.items():
            # The DB-provider choice can only change during the very first
            # setup wizard. Silently drop any later attempt to override it
            # via this endpoint — the value still lives in bootstrap.toml
            # untouched.
            if key in _BOOTSTRAP_ONLY_KEYS:
                continue
            is_secret = key in ("jwt_secret",)
            cfg_storage.set("server_config", key, str(value), is_secret=is_secret)

        return JSONResponse({"success": True})

    async def handle_install_secrets(request: Request) -> JSONResponse:
        """Return install-time secrets for the post-setup config export.

        Admin-only. Aggregates connection info the Setup Wizard's downloadable
        config bundle can't otherwise see, from three sources walked in
        priority order:

        1. ``os.environ`` — inside the cremind Docker container, the compose
           template (install/templates/docker-compose.yml.tmpl) sets
           VNC_PASSWORD, RESOLUTION, APP_URL, INSTALL_MODE, etc. directly,
           so env vars are the in-container truth.
        2. ``$CREMIND_COMPOSE_ENV_FILE`` — the bind-mounted host docker/.env
           (set by the compose template to /opt/cremind-compose/.env). Covers
           values not promoted to env vars (e.g. NOVNC_PORT, VNC_PORT, ports
           defaults).
        3. ``$CREMIND_INSTALL_DIR/docker/.env`` — host install case (no
           container in front of the backend).

        Postgres connection info comes from bootstrap.toml (authoritative)
        with fallback to docker/.env's PG_* (Docker provisioner output).

        Returns ``{deployment: "native", available: false}`` when no docker
        runtime is detectable AND no bootstrap.toml exists.
        """
        import os

        from app.config.bootstrap import read_bootstrap
        from app.config.credentials_file import parse_docker_env

        denied = require_admin(request)
        if denied is not None:
            return denied

        compose_env_file = os.environ.get("CREMIND_COMPOSE_ENV_FILE", "").strip()
        compose_env_path = Path(compose_env_file) if compose_env_file else None
        docker_env_host = Path(BaseConfig.CREMIND_INSTALL_DIR) / "docker" / ".env"
        bootstrap_path = Path(BaseConfig.CREMIND_SYSTEM_DIR) / "bootstrap.toml"

        # File-based fallback for keys not promoted to container env vars.
        # Pick the first source that exists; both parsers return {} for
        # missing files, so a non-existent path is safe.
        file_parsed: dict[str, str] = {}
        for candidate in (compose_env_path, docker_env_host):
            if candidate is not None and candidate.exists():
                try:
                    file_parsed = parse_docker_env(candidate)
                except OSError as e:
                    return JSONResponse(
                        {"error": f"Failed to read docker env file {candidate}: {e}"},
                        status_code=500,
                    )
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

        install_mode = (_runtime("INSTALL_MODE") or "").lower()
        vnc_password = _runtime("VNC_PASSWORD")
        is_docker = (
            install_mode == "docker" or compose_env_path is not None or docker_env_host.exists() or bool(vnc_password)
        )

        if not is_docker and not bootstrap_path.exists():
            return JSONResponse({"deployment": "native", "available": False})

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

        deployment = "docker" if is_docker else "native"

        return JSONResponse(
            {
                "deployment": deployment,
                "available": True,
                # Docker runtime block — populated only when we detect a docker
                # runtime. Values are None on native installs.
                "vnc_password": vnc_password,
                "app_url": _runtime("APP_URL") if is_docker else None,
                "resolution": _runtime("RESOLUTION") if is_docker else None,
                "install_mode": install_mode or None,
                "cors_allowed_origins": _runtime("CORS_ALLOWED_ORIGINS") if is_docker else None,
                "setup_wizard_env": _runtime("SETUP_WIZARD_ENV") if is_docker else None,
                "api_port": _runtime_int("API_PORT", 1112) if is_docker else None,
                "spa_port": _runtime_int("SPA_PORT", 1515) if is_docker else None,
                "novnc_port": _runtime_int("NOVNC_PORT", 6080) if is_docker else None,
                "vnc_port": _runtime_int("VNC_PORT", 5900) if is_docker else None,
                # Postgres block — populated from bootstrap.toml when the user
                # picked Postgres in the wizard.
                "pg_host": _pg("host", "") if has_postgres else None,
                "pg_port": int(pg_bootstrap["port"]) if has_postgres and pg_bootstrap.get("port") else None,
                "pg_user": _pg("user", "PG_USER"),
                "pg_password": _pg("password", "PG_PASSWORD"),
                "pg_database": _pg("database", "PG_DATABASE"),
                "pg_sslmode": _pg("sslmode", "") if has_postgres else None,
                "pg_deployment_mode": (pg_bootstrap.get("deployment_mode") if has_postgres else None) or None,
            }
        )

    return [
        # Unauthenticated endpoints for setup and recovery
        Route("/api/config/install-catalog", handle_install_catalog, methods=["GET"]),
        Route("/api/config/setup-profiles", handle_setup_profiles, methods=["GET"]),
        Route("/api/config/setup-status", handle_setup_status, methods=["GET"]),
        Route("/api/config/setup", handle_setup, methods=["POST"]),
        Route("/api/config/reset-orphaned-setup", handle_reset_orphaned_setup, methods=["POST"]),
        Route("/api/config/embedding/status", handle_embedding_status, methods=["GET"]),
        Route("/api/config/embedding/initialize", handle_embedding_initialize, methods=["POST"]),
        Route("/api/config/channel-catalog", handle_channel_catalog, methods=["GET"]),
        # Authenticated endpoints
        Route("/api/config/server", handle_get_server_config, methods=["GET"]),
        Route("/api/config/server", handle_update_server_config, methods=["PUT"]),
        Route("/api/config/install-secrets", handle_install_secrets, methods=["GET"]),
        Route("/api/config/embedding", handle_get_embedding_config, methods=["GET"]),
        Route("/api/config/embedding", handle_put_embedding_config, methods=["PUT"]),
        Route("/api/config/reconfigure", handle_reconfigure, methods=["POST"]),
    ]
