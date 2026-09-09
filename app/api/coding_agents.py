"""Coding-agent readiness and sign-in API - ``/api/coding-agents``.

One place that answers "can this profile actually use Claude Code / Codex?" for
both the Settings UI and the ``cremind tools coding-agents`` CLI, and the one
place that can *fix* the answer without shell access to the server.

The three facts a user needs are owned by three different subsystems - the pip
extras (:mod:`app.features.manifest`), the per-profile enabled flag (the tool
registry), and the credential (each runner's ``credential_info``) - so this
module reads all three rather than adding a fourth source of truth.

**The sign-in belongs to the agent's own CLI, not to Cremind's LLM providers.**
An earlier version of this file pointed the card at Settings -> LLM Providers,
because Anthropic and OpenAI credentials live there. That was wrong in a way
that cost users a real sign-in: the ``claude`` CLI keeps its login in its own
config home and never reads the Anthropic provider settings, so a user who
followed the card pasted a token into a page the coding tool does not consult.
So the descriptors here name a *command* (``claude auth login``, ``codex login
--device-auth``) and the routes below run it for the user: Claude Code under a
built-in PTY they answer in the browser, Codex through the device-code flow the
card renders as a URL and a code.

Two invariants run through every route:

- **Per profile, with the server's login as an inherited fallback.** A sign-in
  always writes the *profile's own* CLI home (never the resolved one, which may
  be the shared server home - that is how a member profile would silently
  overwrite the operator's login), and a sign-out states which of the two it
  means. ``shared`` is admin-only, because it is the credential every other
  profile falls back to.
- **The UI only offers the safe cases, and that is not the enforcement.** The
  same routes are reachable from ``cremind tools coding-agents`` and from any
  authenticated client, so the scope checks here are load-bearing rather than
  decoration.

The ``probe`` route deliberately does NOT reimplement the live login check; it
runs the very same status leaf the agent calls (``ClaudeCodeStatusTool`` /
``CodexStatusTool``) and returns its structured payload verbatim, so the card
and the assistant can never disagree about whether a delegate is signed in.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import is_admin
from app.runtime import BootedState
from app.utils.logger import logger

# The coding delegates, in the order the UI renders them. Only static metadata
# lives here - the installed / enabled / credential thirds are resolved per
# request.
#
# ``sign_in`` describes how a user *without shell access on the server* signs
# this delegate's CLI in, and nothing about Cremind's LLM provider settings:
# ``method`` picks the flow the card runs (a PTY the user types into, or a
# device code it displays), and ``cli_login`` / ``cli_logout`` are the same
# commands spelled out for someone who does have a shell on the server.
_AGENTS: tuple[dict, ...] = (
    {
        "tool_id": "claude_code",
        "display_name": "Claude Code",
        "feature_key": "claude_code",
        "sign_in": {
            "method": "terminal",
            "label": "Sign in",
            "instructions": (
                "Cremind runs `claude auth login` in a terminal here and you answer "
                "its prompts - no shell access to the server needed. The login is "
                "written to this profile's own CLI home; a profile that never signs "
                "in falls back to the server's own login."
            ),
            "cli_login": "claude auth login",
            "cli_logout": "claude auth logout",
        },
    },
    {
        "tool_id": "codex",
        "display_name": "Codex",
        "feature_key": "codex",
        "sign_in": {
            "method": "device_code",
            "label": "Sign in with ChatGPT",
            "instructions": (
                "Cremind runs the Codex device-code sign-in for you: open the link "
                "it shows, enter the code, and the login lands in this profile's "
                "own CLI home. A CODEX_API_KEY tool variable works instead, and a "
                "profile that never signs in falls back to the server's own login."
            ),
            "cli_login": "codex login --device-auth",
            "cli_logout": "codex logout",
        },
    },
)

_AGENTS_BY_ID: dict[str, dict] = {a["tool_id"]: a for a in _AGENTS}

# The two scopes a login can have. ``profile`` is this profile's own CLI home;
# ``shared`` is the server-wide home every profile inherits when it has none.
_SCOPES = ("profile", "shared")


# A live probe is not free. For Codex it spawns the app-server (capped at 30s);
# for Claude Code it runs the CLI's own ``auth status``. Before this route
# existed the same leaf was only reachable through the agent's turn loop, which
# serialises it - an HTTP route reachable by any authenticated profile turns N
# clicks into N subprocesses, so probes are coalesced per (profile, tool) and
# their answer is held briefly.
#
# 15s is chosen against the one loop that must never be served a stale answer:
# the user reads "not signed in", signs in, and comes back. That round trip is
# far longer than 15s - and the sign-in routes below invalidate the entry
# anyway, as does an explicit ``{"fresh": true}``. What the window does absorb
# is every burst that actually happens: the card probing both rows, `cremind
# tools coding-agents --probe` doing the same, and an impatient second click.
_PROBE_CACHE_TTL = 15.0  # seconds

# (profile, tool_id) -> lock, so concurrent callers await one run instead of
# each spawning their own. Keyed by profile FIRST and always: a probe answers
# "is THIS profile signed in?", and one profile must never be handed another's
# answer - the credential behind it is per-profile.
_probe_locks: dict[tuple[str, str], asyncio.Lock] = {}

# (profile, tool_id) -> (monotonic time, payload). Only a run that returned
# lands here; a probe that raised is never cached, so a transient failure does
# not stick around for the rest of the window.
_probe_cache: dict[tuple[str, str], tuple[float, dict]] = {}

# Login ids whose terminal status has already been reported to a client. A
# finished device-code login changes the credential, so the first poll that
# sees ``success`` / ``error`` / ``cancelled`` must drop the cached probe - but
# only the first: every later poll of the same finished session would otherwise
# keep throwing away a fresh answer the card just paid for.
_login_observed: set[str] = set()

# Sessions are reaped by ``codex_login`` on its own clock, so this set has no
# one to tell it an id is gone. It only grows by one per sign-in, and losing
# the whole set costs at most one extra invalidation, so it is simply dropped
# once it gets silly rather than tracked.
_LOGIN_OBSERVED_MAX = 256


def _profile_from_request(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


def _require_auth(request: Request):
    if not getattr(request.user, "is_authenticated", False):
        return JSONResponse({"error": "Unauthenticated"}, status_code=401)
    return None


def _storage_not_ready() -> JSONResponse:
    """503 response for handlers invoked before storage is booted."""
    return JSONResponse(
        {"error": "Setup not complete - storage is not ready yet."},
        status_code=503,
    )


async def _json_body(request: Request) -> dict:
    """The request body as a dict, or ``{}``.

    Every body here is optional (``{"fresh": true}``, ``{"scope": "shared"}``),
    and clients legitimately send none at all - ``fetch`` with no body, or the
    CLI posting an empty dict - so a missing or malformed body is a default,
    never a 400.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def get_coding_agents_routes(state: BootedState) -> list[Route]:
    """Coding-agent status, sign-in, sign-out and CLI-location routes.

    Registered next to :func:`app.api.tools.get_tool_routes`; handlers resolve
    ``state.registry`` at request time and return 503 while storage is still
    booting, matching the tool routes' contract.
    """

    def _registry_and_profile(request: Request):
        """``(registry, profile, error)`` - the preamble every route shares."""
        unauth = _require_auth(request)
        if unauth is not None:
            return None, "", unauth
        registry = state.registry
        if registry is None:
            return None, "", _storage_not_ready()
        profile = _profile_from_request(request)
        if not profile:
            return None, "", JSONResponse(
                {"error": "Profile is required"}, status_code=400,
            )
        return registry, profile, None

    async def handle_list_coding_agents(request: Request) -> JSONResponse:
        """Report install / enabled / credential state for every coding delegate.

        One row per delegate, always both rows in the same order, even when a
        feature is uninstalled - the card's whole point is to show a delegate
        the user does not have yet and offer to install it.
        """
        registry, profile, error = _registry_and_profile(request)
        if error is not None:
            return error

        from app.features.manifest import FEATURES, is_installed
        from app.tools.builtin import claude_code_runner, codex_runner

        credential_info_fns: dict[str, Callable] = {
            "claude_code": claude_code_runner.credential_info,
            "codex": codex_runner.credential_info,
        }
        find_cli_fns: dict[str, Callable] = {
            "claude_code": claude_code_runner.find_cli,
            "codex": codex_runner.find_cli,
        }

        # A tool whose feature was never installed is not in the registry at all
        # (the built-in group never registered), so a missing row means "off",
        # not an error.
        enabled_by_id = {
            row["tool_id"]: bool(row.get("enabled"))
            for row in registry.visible_for_profile(profile)
        }

        agents: list[dict] = []
        for spec in _AGENTS:
            tool_id = spec["tool_id"]
            feature_key = spec["feature_key"]
            feature = FEATURES.get(feature_key)
            try:
                sdk_installed = is_installed(feature_key)
            except KeyError:  # pragma: no cover - both keys ship in FEATURES
                sdk_installed = False

            # The credential chain reads config / env / disk only, so it answers
            # honestly even with the SDK absent - which is exactly the case where
            # the user most wants to know whether signing in is still to do.
            info: dict[str, Any] = {
                "source": None, "scope": None, "cli_home": None, "account_hint": None,
            }
            cli_available = False
            try:
                variables = registry.config.get_variables(
                    tool_id, profile, include_secrets=True,
                )
                info = credential_info_fns[tool_id](variables, profile)
                # Whether the *binary* is there, which is a different question
                # from whether the SDK is: the card's Sign-in button runs the
                # binary, and an SDK wheel built without one (or a stale
                # CLI-path variable) would make that button do nothing.
                cli_available = bool(find_cli_fns[tool_id](variables))
            except Exception:  # noqa: BLE001 - status must never fail on one tool
                logger.debug(
                    f"coding-agents: credential resolution failed for '{tool_id}'",
                    exc_info=True,
                )

            enabled = enabled_by_id.get(tool_id, False)
            source = info.get("source")
            agents.append({
                "tool_id": tool_id,
                "display_name": spec["display_name"],
                "feature_key": feature_key,
                "extras": list(feature.extras) if feature is not None else [],
                "sdk_installed": sdk_installed,
                "requires_restart_after_install": (
                    bool(feature.requires_restart) if feature is not None else False
                ),
                "enabled": enabled,
                "credential_source": source,
                "credential_scope": info.get("scope"),
                "cli_home": info.get("cli_home"),
                "account_hint": info.get("account_hint"),
                "cli_available": cli_available,
                "credentials_configured": source is not None,
                "sign_in": dict(spec["sign_in"]),
                "message": _summary(
                    spec,
                    sdk_installed=sdk_installed,
                    enabled=enabled,
                    source=source,
                    scope=info.get("scope"),
                    cli_available=cli_available,
                ),
            })
        return JSONResponse({"agents": agents})

    async def handle_probe_coding_agent(request: Request) -> JSONResponse:
        """Run the delegate's own status leaf and return its payload verbatim.

        Gated on the FEATURE being installed, and deliberately NOT on the
        per-profile enabled flag the sibling listing reports. The Coding Agents
        card offers "Check sign-in" as a pre-enable diagnostic - its whole point
        is to answer "will this work if I switch it on?" *before* switching it
        on - so gating on ``enabled`` would refuse exactly the user who needs
        the answer. ``sdk_installed`` is the honest gate instead: with the extra
        absent there is no binary to spawn and nothing to learn.

        Coalesced and briefly cached per (profile, tool) - see
        ``_PROBE_CACHE_TTL`` - because the work behind a probe is a subprocess,
        not a database read. ``{"fresh": true}`` skips the cached answer: it is
        what an explicit "Check sign-in" click and a just-closed login dialog
        send, where the user has changed the very thing being reported and a
        cached verdict predates the change.

        Never 500s: a probe is a diagnostic, and a diagnostic that itself blows
        up tells the user nothing. Unexpected failures come back as a 200 body
        carrying ``error`` so the card can show what went wrong.
        """
        registry, profile, error = _registry_and_profile(request)
        if error is not None:
            return error
        tool_id = request.path_params["tool_id"]
        spec = _AGENTS_BY_ID.get(tool_id)
        if spec is None:
            return _unknown_agent(tool_id)

        body = await _json_body(request)
        fresh = bool(body.get("fresh"))

        installed = _installed_or_409(spec, verb="check")
        if installed is not None:
            return installed

        key = (profile, tool_id)
        try:
            from app.config.settings import get_user_working_directory

            # Everything that decides whether to spend the work happens under
            # the lock, so a caller that arrives while a run is in flight waits
            # for it and then reads its answer instead of starting a second one.
            lock = _probe_locks.setdefault(key, asyncio.Lock())
            async with lock:
                cached = _probe_cache.get(key)
                if (
                    not fresh
                    and cached is not None
                    and (time.monotonic() - cached[0]) < _PROBE_CACHE_TTL
                ):
                    payload = cached[1]
                else:
                    variables = registry.config.get_variables(
                        tool_id, profile, include_secrets=True,
                    )
                    leaf = _status_leaf(tool_id)
                    result = await leaf.run({
                        "probe": True,
                        "_profile": profile,
                        "_variables": variables,
                        "_working_directory": get_user_working_directory(),
                    })
                    payload = result.structured_content or {}
                    _probe_cache[key] = (time.monotonic(), payload)
        except Exception as exc:  # noqa: BLE001 - a probe never returns a 500
            logger.exception(f"coding-agents: probe failed for '{tool_id}'")
            return JSONResponse({
                "tool_id": tool_id,
                "error": f"The sign-in check could not run: {exc}",
            })
        return JSONResponse({"tool_id": tool_id, **payload})

    # -- Codex device-code sign-in ------------------------------------------
    #
    # Three routes for one conversation the server holds open on the user's
    # behalf: start it, poll it, abandon it. The session (and the ``codex
    # app-server`` child behind it) lives in this process only, which is why a
    # poll for an unknown id is a 404 the SPA words as "the server restarted".

    async def handle_start_codex_login(request: Request) -> JSONResponse:
        """Begin a device-code sign-in and return the URL and code to show.

        201 with the code, 409 when the Codex extra is not installed (there is
        nothing to spawn), and 200 with ``error`` when the flow could be started
        but refused - a reason on screen beats a bare failure, and the dialog
        renders exactly that field.
        """
        registry, profile, error = _registry_and_profile(request)
        if error is not None:
            return error

        spec = _AGENTS_BY_ID["codex"]
        installed = _installed_or_409(spec, verb="sign in to")
        if installed is not None:
            return installed

        from app.tools.builtin import codex_login

        try:
            variables = registry.config.get_variables(
                "codex", profile, include_secrets=True,
            )
            session = await codex_login.start(profile, variables)
        except RuntimeError as exc:
            # The SDK is missing. Same shape as the feature gate above, because
            # to the user it is the same problem.
            return JSONResponse({"tool_id": "codex", "error": str(exc)}, status_code=409)
        except Exception as exc:  # noqa: BLE001 - a sign-in never 500s
            logger.exception("coding-agents: starting the Codex device login failed")
            return JSONResponse({
                "tool_id": "codex",
                "error": f"The Codex sign-in could not be started: {exc}",
            })

        payload = session.public()
        if session.status == "error":
            return JSONResponse({
                "tool_id": "codex",
                **payload,
                "error": session.detail or "The Codex sign-in could not be started.",
            })
        return JSONResponse(
            {
                "tool_id": "codex",
                **payload,
                "expires_in": int(codex_login._LOGIN_TIMEOUT),  # noqa: SLF001
            },
            status_code=201,
        )

    async def handle_get_codex_login(request: Request) -> JSONResponse:
        """Poll one device-code sign-in.

        404 for an id this process does not know - unknown, reaped, or lost to
        a restart, all of which mean "start again" and none of which mean
        "error". 403 rather than 404 for another profile's id: telling a user
        their own sign-in had expired when it had not would send them round the
        whole flow again.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        login_id = request.path_params["login_id"]

        from app.tools.builtin import codex_login

        session = codex_login.get(login_id)
        if session is None:
            return JSONResponse(
                {
                    "error": (
                        "That sign-in is no longer in progress. It was cancelled, it "
                        "expired, or the server restarted - start a new one."
                    )
                },
                status_code=404,
            )
        if session.profile != profile:
            return JSONResponse(
                {"error": "That sign-in belongs to another profile."}, status_code=403,
            )

        if session.status in ("success", "error", "cancelled"):
            _observe_finished_login(profile, login_id)
        return JSONResponse({"tool_id": "codex", **session.public()})

    async def handle_cancel_codex_login(request: Request) -> JSONResponse:
        """Abandon a pending sign-in (the dialog's Cancel, and its unmount).

        Cancelling is what releases the app-server child holding the code, so
        it must work even for a session that has already finished - hence
        ``ok: false`` rather than an error when there was nothing live left.
        """
        unauth = _require_auth(request)
        if unauth is not None:
            return unauth
        profile = _profile_from_request(request)
        login_id = request.path_params["login_id"]

        from app.tools.builtin import codex_login

        session = codex_login.get(login_id)
        if session is None:
            return JSONResponse({"error": "Unknown sign-in."}, status_code=404)
        if session.profile != profile:
            return JSONResponse(
                {"error": "That sign-in belongs to another profile."}, status_code=403,
            )
        cancelled = await codex_login.cancel(login_id)
        _invalidate(profile, "codex")
        return JSONResponse({
            "ok": cancelled, "login_id": login_id, "status": session.status,
        })

    # -- Terminal sign-in / sign-out / where the CLI is ----------------------

    async def handle_login_terminal(request: Request) -> JSONResponse:
        """Open a PTY already running the agent's own ``login`` command.

        The binary is spawned DIRECTLY under the PTY rather than through a
        shell that then runs it: the user is meant to answer this one command
        and nothing else, and a shell would leave them at a live prompt on the
        server with the credential home exported when it exits.

        The credential home is FORCED (never the resolved one): a profile with
        no login of its own resolves to the shared server home, so a sign-in
        run there would overwrite the operator's login with the member's - the
        one accident the per-profile homes exist to prevent. ``shared`` is
        admin-only for the same reason.
        """
        registry, profile, error = _registry_and_profile(request)
        if error is not None:
            return error
        tool_id = request.path_params["tool_id"]
        spec = _AGENTS_BY_ID.get(tool_id)
        if spec is None:
            return _unknown_agent(tool_id)

        body = await _json_body(request)
        scope, scope_error = _scope_from(body, request)
        if scope_error is not None:
            return scope_error
        try:
            cols = int(body.get("cols") or 100)
            rows = int(body.get("rows") or 24)
        except (TypeError, ValueError):
            cols, rows = 100, 24

        installed = _installed_or_409(spec, verb="sign in to")
        if installed is not None:
            return installed

        from app.api.terminals import TerminalLimitReached, create_terminal
        from app.config.settings import get_user_working_directory
        from app.config.system_vars import build_system_env

        runner = _runner(tool_id)
        variables = registry.config.get_variables(tool_id, profile, include_secrets=True)
        binary = runner.find_cli(variables)
        if not binary:
            return JSONResponse(
                {
                    "tool_id": tool_id,
                    "error": (
                        f"Cremind cannot find the {spec['display_name']} command-line "
                        f"tool on this server, so it has nothing to run. Set its CLI "
                        f"path variable, or install the CLI on the server."
                    ),
                },
                status_code=409,
            )

        argv = list(runner.login_argv(binary))
        home = _cli_home(tool_id, profile, scope)
        # The CLI writes its credential here, so the directory has to exist and
        # be private before the command starts. Both are best effort: POSIX
        # modes mean little on Windows, and a home we cannot tighten is still
        # better than refusing the sign-in.
        try:
            home.mkdir(parents=True, exist_ok=True)
            home.chmod(0o700)
        except OSError:
            logger.debug("coding-agents: could not prepare the CLI home", exc_info=True)

        env = dict(build_system_env(profile))
        env.update(_home_env(profile, scope))

        try:
            info = await create_terminal(
                profile,
                cwd=get_user_working_directory(),
                cols=cols,
                rows=rows,
                extra_env=env,
                argv=argv,
                title=f"Sign in to {spec['display_name']}",
            )
        except TerminalLimitReached as exc:
            return JSONResponse({"tool_id": tool_id, "error": str(exc)}, status_code=409)
        except Exception as exc:  # noqa: BLE001 (already logged by create_terminal)
            logger.exception(f"coding-agents: the sign-in terminal for '{tool_id}' failed")
            return JSONResponse(
                {
                    "tool_id": tool_id,
                    "error": f"Failed to open the sign-in terminal: {exc}",
                },
                status_code=500,
            )

        # Whatever the last probe said about this profile, a login is now being
        # typed against it.
        _invalidate(profile, tool_id)
        return JSONResponse(
            {
                "terminal_id": info.terminal_id,
                "title": info.title,
                "shell": info.shell,
                "working_dir": info.working_dir,
                "created_at": info.created_at,
                "tool_id": tool_id,
                "scope": scope,
                "cli_home": str(home),
                "command": _display_command(argv),
            },
            status_code=201,
        )

    async def handle_logout(request: Request) -> JSONResponse:
        """Sign one CLI home out, and say which one it was.

        The 409 below is the important half. A profile that never signed in
        *resolves* to the shared server login, so a bare "Sign out" on its card
        would have signed the whole server out - every other profile with it.
        The card only offers the button for a login the viewer owns, but the
        same route is reachable from the CLI, so the refusal lives here.
        """
        registry, profile, error = _registry_and_profile(request)
        if error is not None:
            return error
        tool_id = request.path_params["tool_id"]
        spec = _AGENTS_BY_ID.get(tool_id)
        if spec is None:
            return _unknown_agent(tool_id)

        body = await _json_body(request)
        scope, scope_error = _scope_from(body, request)
        if scope_error is not None:
            return scope_error

        if scope == "profile" and not _profile_login_present(tool_id, profile):
            resolved = _resolved_home(tool_id, profile)
            if resolved.has_login:
                detail = (
                    f"This profile has no {spec['display_name']} login of its own - it "
                    f"uses the shared server login in {resolved.path}. Only an admin "
                    f"can sign that out (scope=shared)."
                )
            else:
                detail = (
                    f"Nothing is signed in to {spec['display_name']} for this profile, "
                    f"so there is nothing to sign out."
                )
            return JSONResponse(
                {"tool_id": tool_id, "scope": scope, "error": detail}, status_code=409,
            )

        runner = _runner(tool_id)
        variables = registry.config.get_variables(tool_id, profile, include_secrets=True)
        try:
            result = await runner.logout(variables, profile, scope=scope)
        except Exception as exc:  # noqa: BLE001 - the runner promises not to raise
            logger.exception(f"coding-agents: signing '{tool_id}' out failed")
            return JSONResponse({
                "tool_id": tool_id,
                "ok": False,
                "scope": scope,
                "detail": f"The sign-out could not run: {exc}",
            })
        _invalidate(profile, tool_id)
        return JSONResponse({"tool_id": tool_id, **result})

    async def handle_cli(request: Request) -> JSONResponse:
        """Where this agent's CLI is on the server, and how it would be invoked.

        This is what lets ``cremind tools coding-agents login`` refuse politely
        instead of exec'ing something that is not there: the CLI compares
        ``binary`` and ``system_dir`` against its own filesystem and, when they
        are not the same machine, names the host to run the login on. Nothing
        here is a secret - paths, argv and two environment variables that are
        themselves paths.
        """
        registry, profile, error = _registry_and_profile(request)
        if error is not None:
            return error
        tool_id = request.path_params["tool_id"]
        spec = _AGENTS_BY_ID.get(tool_id)
        if spec is None:
            return _unknown_agent(tool_id)

        from app.config.settings import BaseConfig

        runner = _runner(tool_id)
        variables = registry.config.get_variables(tool_id, profile, include_secrets=True)
        binary = runner.find_cli(variables)
        return JSONResponse({
            "tool_id": tool_id,
            "display_name": spec["display_name"],
            "binary": binary,
            "binary_source": runner.cli_binary_source(variables),
            # Empty rather than a command with a null in it: there is no binary
            # to name, and a caller that renders argv would print "None login".
            "login_argv": list(runner.login_argv(binary)) if binary else [],
            "logout_argv": list(runner.logout_argv(binary)) if binary else [],
            "status_argv": list(runner.status_argv(binary)) if binary else [],
            "profile_env": _home_env(profile, "profile", tool_id=tool_id),
            "shared_env": _home_env(profile, "shared", tool_id=tool_id),
            "server_hostname": socket.gethostname(),
            "system_dir": str(BaseConfig.CREMIND_SYSTEM_DIR),
            "platform": sys.platform,
        })

    return [
        # The device-code routes come first so their literal segments are never
        # shadowed by the ``{tool_id}`` patterns below.
        Route("/api/coding-agents/codex/login", handle_start_codex_login, methods=["POST"]),
        Route(
            "/api/coding-agents/codex/login/{login_id}",
            handle_get_codex_login,
            methods=["GET"],
        ),
        Route(
            "/api/coding-agents/codex/login/{login_id}/cancel",
            handle_cancel_codex_login,
            methods=["POST"],
        ),
        Route("/api/coding-agents", handle_list_coding_agents, methods=["GET"]),
        Route(
            "/api/coding-agents/{tool_id}/probe",
            handle_probe_coding_agent,
            methods=["POST"],
        ),
        Route(
            "/api/coding-agents/{tool_id}/login-terminal",
            handle_login_terminal,
            methods=["POST"],
        ),
        Route("/api/coding-agents/{tool_id}/logout", handle_logout, methods=["POST"]),
        Route("/api/coding-agents/{tool_id}/cli", handle_cli, methods=["GET"]),
    ]


# -- helpers ----------------------------------------------------------------


def _unknown_agent(tool_id: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": (
                f"'{tool_id}' is not a coding agent. Valid ids: "
                f"{', '.join(_AGENTS_BY_ID)}."
            )
        },
        status_code=400,
    )


def _installed_or_409(spec: dict, *, verb: str) -> JSONResponse | None:
    """409 when the delegate's extra is absent, else None.

    Everything past this point spawns the delegate's own binary, and the binary
    ships with the extra: without it there is nothing to run and nothing to
    learn, so the honest answer is a refusal naming the install as the fix.
    """
    from app.features.manifest import is_installed

    try:
        installed = is_installed(spec["feature_key"])
    except KeyError:  # pragma: no cover - both keys ship in FEATURES
        installed = False
    if installed:
        return None
    return JSONResponse(
        {
            "tool_id": spec["tool_id"],
            "error": (
                f"{spec['display_name']} is not installed on this server, so there "
                f"is nothing to {verb}. Install the '{spec['feature_key']}' feature "
                f"first."
            ),
        },
        status_code=409,
    )


def _scope_from(body: dict, request: Request) -> tuple[str, JSONResponse | None]:
    """``(scope, error)`` for a request that names a credential scope.

    ``shared`` is admin-only wherever it appears: it is the login every profile
    without one of its own inherits, so a member signing it in (or out) would
    change every other profile's credential.
    """
    scope = str(body.get("scope") or "profile").strip().lower()
    if scope not in _SCOPES:
        return "profile", JSONResponse(
            {"error": f"scope must be one of: {', '.join(_SCOPES)}."}, status_code=400,
        )
    if scope == "shared" and not is_admin(request):
        return scope, JSONResponse(
            {
                "error": (
                    "The shared server login is the credential every profile without "
                    "one of its own falls back to, so only the admin profile may "
                    "change it."
                )
            },
            status_code=403,
        )
    return scope, None


def _runner(tool_id: str):
    """The runner module owning one delegate's credential, CLI and sign-out.

    Imported here rather than at module scope: the runners pull in their
    optional SDKs' import shims, and this API module is imported at server
    construction time - well before anyone asks about a coding agent.
    """
    if tool_id == "claude_code":
        from app.tools.builtin import claude_code_runner as mod
    else:
        from app.tools.builtin import codex_runner as mod
    return mod


def _cli_home(tool_id: str, profile: str, scope: str) -> Path:
    """The CLI home a sign-in / sign-out at ``scope`` targets, as a path.

    Deliberately NOT the resolved home: resolution answers "where would a run
    authenticate from", which for a profile with no login of its own is the
    shared one. Writing there is exactly the accident the scopes exist to
    prevent, so both callers state which home they mean.
    """
    from app.config import coding_cli_homes as homes

    if tool_id == "claude_code":
        return Path(
            homes.shared_claude_config_dir() if scope == "shared"
            else homes.profile_claude_config_dir(profile)
        )
    return Path(
        homes.shared_codex_home() if scope == "shared" else homes.profile_codex_home(profile)
    )


def _home_env(profile: str, scope: str, *, tool_id: str | None = None) -> dict[str, str]:
    """The ``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` block for one scope.

    With ``tool_id`` only that agent's variable is returned - what the ``/cli``
    route publishes, where the payload is about one binary. Without it both are
    returned, which is what a login terminal needs: it is a real session for
    this profile, and a shell there must not be able to reach the other
    agent's shared home by accident.
    """
    from app.config import coding_cli_homes as homes

    if scope == "shared":
        env = {
            "CLAUDE_CONFIG_DIR": str(homes.shared_claude_config_dir()),
            "CODEX_HOME": str(homes.shared_codex_home()),
        }
    else:
        env = dict(homes.coding_cli_env(profile, force_profile=True))
    if tool_id == "claude_code":
        return {"CLAUDE_CONFIG_DIR": env["CLAUDE_CONFIG_DIR"]}
    if tool_id == "codex":
        return {"CODEX_HOME": env["CODEX_HOME"]}
    return env


def _profile_login_present(tool_id: str, profile: str) -> bool:
    """Has ``profile`` signed this agent in itself (as opposed to inheriting)?"""
    from app.config import coding_cli_homes as homes

    home = _cli_home(tool_id, profile, "profile")
    if tool_id == "claude_code":
        return homes.claude_login_present(home)
    return homes.codex_login_present(home)


def _resolved_home(tool_id: str, profile: str):
    """The home a run for ``profile`` would actually authenticate from."""
    from app.config import coding_cli_homes as homes

    if tool_id == "claude_code":
        return homes.resolve_claude_config_dir(profile)
    return homes.resolve_codex_home(profile)


def _display_command(argv: list[str]) -> str:
    """``argv`` as one line for the dialog's "this is what is running" note.

    Display only, so the quoting just has to be readable on the platform the
    user is looking at: POSIX quoting turns a Windows path into
    ``'C:\\...\\claude.exe'``, which reads as a mistake, so Windows gets a plain
    join instead.
    """
    if os.name == "nt":
        return " ".join(argv)
    return shlex.join(argv)


def _observe_finished_login(profile: str, login_id: str) -> None:
    """Drop the cached probe the first time a finished login is reported.

    Only the first time: the dialog keeps polling a finished session while the
    user reads "Signed in as ...", and invalidating on every one of those polls
    would throw away the fresh probe the card runs right after.
    """
    if login_id in _login_observed:
        return
    if len(_login_observed) >= _LOGIN_OBSERVED_MAX:
        _login_observed.clear()
    _login_observed.add(login_id)
    _invalidate(profile, "codex")


def _invalidate(profile: str, tool_id: str) -> None:
    """Forget what we last learned about ``(profile, tool_id)``. Never raises.

    Called whenever a route changes the credential (a sign-in started, a login
    finished, a sign-out). Two caches are stale at that moment: this module's
    probe answer, and the runner's per-credential model list - the latter is
    keyed by credential, so a card that signs a new account in would otherwise
    keep offering the old one's models for the rest of its five-minute window.
    """
    _probe_cache.pop((profile, tool_id), None)
    try:
        if tool_id == "claude_code":
            from app.tools.builtin import claude_code_runner

            claude_code_runner._models_cache.clear()  # noqa: SLF001
        else:
            from app.config import coding_cli_homes as homes
            from app.tools.builtin import codex_runner

            codex_runner._forget_models_cache(str(homes.profile_codex_home(profile)))  # noqa: SLF001
            codex_runner._forget_models_cache(str(homes.shared_codex_home()))  # noqa: SLF001
    except Exception:  # noqa: BLE001 - cache hygiene never fails a request
        logger.debug(
            f"coding-agents: could not clear the model cache for '{tool_id}'",
            exc_info=True,
        )


def _status_leaf(tool_id: str):
    """Instantiate the delegate's ``status`` leaf.

    Imported here rather than at module scope: the leaf modules pull in their
    runners (and, transitively, the optional SDKs' import shims), and this API
    module is imported at server construction time - well before anyone asks
    about a coding agent.
    """
    if tool_id == "claude_code":
        from app.tools.builtin.claude_code import ClaudeCodeStatusTool

        return ClaudeCodeStatusTool()
    from app.tools.builtin.codex import CodexStatusTool

    return CodexStatusTool()


def _summary(
    spec: dict,
    *,
    sdk_installed: bool,
    enabled: bool,
    source: str | None,
    scope: str | None,
    cli_available: bool,
) -> str:
    """One sentence naming the single next thing standing between the profile
    and a working delegate - install, sign in, or switch the tool on."""
    name = spec["display_name"]
    login = spec["sign_in"]["cli_login"]
    if not sdk_installed:
        return (
            f"{name} is not installed on this server. Install the "
            f"'{spec['feature_key']}' feature to add it - no shell access needed."
        )
    if source is None:
        if not cli_available:
            return (
                f"{name} is installed, but its command-line tool is not on this "
                f"server, so there is no `{login}` to run. Set its CLI path variable, "
                f"or use an API key variable instead."
            )
        return (
            f"{name} is installed, but nothing is signed in for this profile. "
            f"Sign in to run `{login}` here, or set an API key variable."
        )
    where = _scope_phrase(scope)
    if not enabled:
        return (
            f"{name} is installed and has a credential ({source}{where}), but the "
            f"tool is switched off for this profile."
        )
    return f"{name} is installed, enabled, and using {source}{where}."


def _scope_phrase(scope: str | None) -> str:
    """", this profile's own login" / ", the shared server login" / ""."""
    if scope == "profile":
        return ", this profile's own login"
    if scope == "shared":
        return ", the shared server login"
    return ""
