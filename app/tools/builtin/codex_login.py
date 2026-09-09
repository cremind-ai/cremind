"""Device-code sign-in for the Codex tool, held open for the user's browser.

``codex login --device-auth`` is a long conversation: the app-server asks
OpenAI for a verification URL and a short user code, the user carries the code
to a browser on whatever machine they have one on, and the server polls until
they confirm. Cremind runs that conversation on the user's behalf so a person
with no shell on the server can still sign in - the Coding Agents card starts
one, shows the URL and the code, and polls this module until it finishes.

Three facts shape everything here:

* **The home must be chosen before the client is built.** The Codex app-server
  reads ``$CODEX_HOME/auth.json`` exactly once, at spawn, and writes the new
  credential back to the same place. So ``CODEX_HOME`` goes into the config env
  BEFORE ``AsyncCodex(config)`` is entered - naming the home afterwards would
  sign the user into whichever home the server happened to start with (the
  shared one, overwriting the operator's login).
* **The wait is live, not a poll of some server-side state.** ``handle.wait()``
  is held by the ``codex app-server`` child process, which the ``AsyncCodex``
  context owns. The context therefore stays open for the whole wait - up to 15
  minutes - and a server restart drops every pending session with it. That is
  why :func:`get` returning None is a real answer and not an internal error:
  the API turns an unknown login id into "sign-in interrupted, start again".
* **A login writes a credential.** So a pending session is cancelled - not just
  forgotten - when the profile is deleted or reset (see
  ``app/api/profiles.py`` and ``app/reset/engine.py``), otherwise a session
  started before the delete would recreate ``auth.json`` in the tree that was
  just removed, and nothing would ever collect it.

Sessions are per profile: the registry is keyed by profile and by login id, and
:func:`get` deliberately does not filter by profile - the API compares
``session.profile`` itself so it can answer 403 rather than 404 for another
profile's id (a 404 would tell a user their own sign-in had expired).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.config.coding_cli_homes import profile_codex_home
from app.tools.builtin import codex_runner as runner
from app.utils.logger import logger

# The start handshake (spawn the app-server, ask OpenAI for a code) is the only
# part the HTTP request waits on, so it gets a short leash: a card that spins
# for a minute reads as broken, and everything after the code is on screen is
# polled, not awaited.
_START_TIMEOUT = 30.0

# OpenAI's device codes expire in 15 minutes; waiting longer would leave a
# child process holding a code the user can no longer use.
_LOGIN_TIMEOUT = 900.0

# How long a finished session stays readable. The UI polls every few seconds
# and needs to see the terminal status once; 10 minutes is far past that and
# still short enough that a browser tab left open overnight cannot resurrect a
# stale "signed in" answer.
_FINISHED_TTL = 600.0

_TERMINAL_STATUSES = frozenset({"success", "error", "cancelled"})


@dataclass
class CodexLoginSession:
    """One device-code sign-in attempt, from start to a terminal status.

    ``status`` walks ``starting`` -> ``pending`` (the code is on screen) ->
    ``success`` / ``error`` / ``cancelled``. ``ready`` is set as soon as the
    session has something to show - the code, or the error that stopped it -
    which is what :func:`start` waits on before answering the HTTP request.

    ``handle`` and ``task`` are the live halves: the SDK's device-code handle
    (which can be cancelled upstream) and the asyncio task running the wait.
    They are never serialised - :meth:`public` is what leaves this module.
    """

    profile: str
    login_id: str
    status: str = "starting"
    verification_url: Optional[str] = None
    user_code: Optional[str] = None
    detail: Optional[str] = None
    account: Optional[Dict[str, Any]] = None
    home: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None
    handle: Any = None

    def public(self) -> Dict[str, Any]:
        """The JSON-safe view. Nothing secret: the verification URL and the
        user code are meant to be read aloud, and ``account`` is the same
        non-secret summary the probe returns."""
        return {
            "login_id": self.login_id,
            "status": self.status,
            "verification_url": self.verification_url,
            "user_code": self.user_code,
            "detail": self.detail,
            "account": self.account,
            "home": self.home,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }

    def _finish(self, status: str, *, detail: Optional[str] = None) -> None:
        self.status = status
        if detail is not None:
            self.detail = detail
        self.finished_at = time.time()
        self.ready.set()


# profile -> its one live/most recent session, and login_id -> session. Two maps
# rather than one scan because both lookups are on hot paths (the poll is by id,
# the "cancel whatever this profile had" is by profile) and because the by-id
# map is what keeps a *finished* session readable after the profile has moved
# on to another one.
_sessions_by_profile: Dict[str, CodexLoginSession] = {}
_by_login_id: Dict[str, CodexLoginSession] = {}

# One lock per profile, so two clicks from the same person serialise (the second
# cancels the first) while two profiles signing in at once do not wait on each
# other. Created lazily and never removed: a handful of entries keyed by profile
# name is nothing, and popping one while another coroutine is waiting on it
# would hand the next caller a different lock and defeat the point.
_profile_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(profile: str) -> asyncio.Lock:
    lock = _profile_locks.get(profile)
    if lock is None:
        lock = asyncio.Lock()
        _profile_locks[profile] = lock
    return lock


def _reap() -> None:
    """Forget finished sessions past the TTL.

    Called from :func:`start` and :func:`get` rather than from a timer: the
    registry only grows when someone signs in, so the two entry points are
    exactly the moments worth sweeping, and a background task would be one more
    thing to shut down.
    """
    now = time.time()
    stale = [
        login_id
        for login_id, session in _by_login_id.items()
        if session.finished_at is not None and (now - session.finished_at) > _FINISHED_TTL
    ]
    for login_id in stale:
        session = _by_login_id.pop(login_id, None)
        if session is not None and _sessions_by_profile.get(session.profile) is session:
            _sessions_by_profile.pop(session.profile, None)


async def start(profile: str, variables: dict) -> CodexLoginSession:
    """Begin a device-code sign-in for ``profile`` and return it once the code
    is known (or the attempt has already failed).

    Raises ``RuntimeError`` when the Codex SDK is not installed - there is
    nothing to spawn and the API turns that into a 409. Every other failure
    lands on the session as ``status="error"`` with a ``detail``, because by
    then there IS a session and the card has somewhere to show the reason.

    A second start for the same profile cancels the first. Two live device
    codes for one profile is not a state anyone wants: only one of them can
    write ``auth.json``, and the user is looking at whichever code the card
    last drew.
    """
    _reap()
    sdk, err = runner.load_sdk()
    if sdk is None:
        raise RuntimeError(
            "openai_codex is not installed - install it with "
            f"`cremind features install codex`. {err or ''}".strip()
        )

    async with _lock_for(profile):
        previous = _sessions_by_profile.get(profile)
        if previous is not None and previous.status not in _TERMINAL_STATUSES:
            await _cancel(previous)

        session = CodexLoginSession(profile=profile, login_id=uuid.uuid4().hex)
        _sessions_by_profile[profile] = session
        _by_login_id[session.login_id] = session
        session.task = asyncio.create_task(_run(session, sdk, variables))

        try:
            await asyncio.wait_for(session.ready.wait(), timeout=_START_TIMEOUT)
        except asyncio.TimeoutError:
            # The app-server never came back with a code. Cancel rather than
            # leave the child running: it would keep a code alive that nobody
            # can see.
            await _cancel(session)
            session._finish(
                "error",
                detail=(
                    f"Codex did not return a sign-in code within {int(_START_TIMEOUT)}s. "
                    "Try again; if it keeps failing, check that the codex binary can "
                    "run on this server."
                ),
            )
        return session


async def _run(session: CodexLoginSession, sdk, variables: dict) -> None:
    """Own one sign-in from spawn to terminal status.

    The whole flow lives inside one ``AsyncCodex`` context because the wait is
    the child process's: leaving the context ends the login. ``CODEX_HOME`` is
    forced to the PROFILE's home - never the resolved one - so a member profile
    signing in cannot overwrite the operator's shared login, which is the
    accident :mod:`app.config.coding_cli_homes` exists to prevent.
    """
    home = profile_codex_home(session.profile)
    session.home = str(home)
    try:
        try:
            home.mkdir(parents=True, exist_ok=True)
            # Best effort: POSIX modes are close to meaningless on Windows, and
            # a home we cannot tighten is still better than no sign-in at all.
            home.chmod(0o700)
        except OSError:
            logger.debug("codex-login: could not prepare the CODEX_HOME", exc_info=True)

        auth = runner.CodexAuth(env_overrides={"CODEX_HOME": str(home)}, scope="profile")
        # Built before the context is entered: the app-server reads auth.json
        # once, at spawn, and writes the new credential to the same home.
        config = runner.build_config(sdk, variables=variables, auth=auth, cwd=None)
        async with sdk.AsyncCodex(config) as codex:
            handle = await codex.login_chatgpt_device_code()
            session.handle = handle
            session.verification_url = getattr(handle, "verification_url", None)
            session.user_code = getattr(handle, "user_code", None)
            session.status = "pending"
            session.detail = "Waiting for the sign-in to be confirmed in a browser."
            session.ready.set()

            note = await asyncio.wait_for(handle.wait(), timeout=_LOGIN_TIMEOUT)
            if getattr(note, "success", False):
                session.account = await _account_summary(codex)
                session._finish("success", detail="Signed in to Codex.")
            else:
                session._finish(
                    "error",
                    detail=str(getattr(note, "error", "") or "The sign-in was not completed."),
                )
    except asyncio.CancelledError:
        # Cancellation is a user action (a new sign-in, a profile delete, a
        # shutdown), so it is a status and not an error - but the exception
        # must still propagate, or the task never actually cancels. Nothing is
        # awaited on this path on purpose: the only cancellation route is
        # :func:`_cancel`, which has already told the app-server to abandon the
        # attempt, and awaiting inside a cancelled task is how a "graceful"
        # cleanup turns into a second CancelledError from somewhere else.
        session._finish("cancelled", detail="The sign-in was cancelled.")
        raise
    except asyncio.TimeoutError:
        await _cancel_handle(session)
        session._finish(
            "error",
            detail=(
                f"The sign-in code expired after {int(_LOGIN_TIMEOUT / 60)} minutes. "
                "Start again to get a new one."
            ),
        )
    except Exception as exc:  # noqa: BLE001 - a sign-in never takes the server down
        logger.debug("codex-login: the device-code sign-in failed", exc_info=True)
        await _cancel_handle(session)
        session._finish("error", detail=f"The Codex sign-in failed: {exc}")
    finally:
        session.handle = None
        if session.finished_at is None:  # pragma: no cover - every path above finishes
            session._finish("error", detail="The sign-in ended without a result.")
        # A successful sign-in changes which account the tool authenticates as,
        # and the model list is cached per CODEX_HOME for five minutes - so the
        # card would otherwise keep showing the old account's models.
        runner._forget_models_cache(str(home))


async def _account_summary(codex) -> Optional[Dict[str, Any]]:
    """Who did we just sign in as? Best effort - a sign-in that worked must not
    be reported as a failure because the follow-up read did not."""
    try:
        resp = await asyncio.wait_for(codex.account(), timeout=15.0)
    except Exception:  # noqa: BLE001
        logger.debug("codex-login: reading the account after sign-in failed", exc_info=True)
        return None
    return runner.account_summary(resp)


async def _cancel_handle(session: CodexLoginSession) -> None:
    """Tell the app-server to abandon the login attempt. Never raises."""
    handle = session.handle
    if handle is None:
        return
    try:
        await handle.cancel()
    except Exception:  # noqa: BLE001
        logger.debug("codex-login: cancelling the login handle failed", exc_info=True)


async def _cancel(session: CodexLoginSession) -> None:
    """Cancel a session and wait for its task to unwind.

    Waiting matters: the task holds an ``AsyncCodex`` context whose child
    process would otherwise still be writing to the home the caller is about to
    delete (profile delete / reset) or replace (a second sign-in).
    """
    await _cancel_handle(session)
    task = session.task
    if task is not None and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception:  # noqa: BLE001
            logger.debug("codex-login: the cancelled session ended badly", exc_info=True)
    if session.finished_at is None:
        session._finish("cancelled", detail="The sign-in was cancelled.")


def get(login_id: str) -> Optional[CodexLoginSession]:
    """The session with this id, or None when it is unknown or reaped.

    None is the signal the API turns into "the sign-in was interrupted (the
    server restarted) - start again", which is the truth: sessions live only in
    this process, held open by a child process, and nothing recreates them.
    """
    _reap()
    return _by_login_id.get(login_id)


async def cancel(login_id: str) -> bool:
    """Cancel one session by id. True when there was a live one to cancel."""
    session = _by_login_id.get(login_id)
    if session is None or session.status in _TERMINAL_STATUSES:
        return False
    await _cancel(session)
    return True


async def cancel_for_profile(profile: str) -> bool:
    """Cancel ``profile``'s pending sign-in, if any. True when one was live.

    Called before a profile's CLI homes are deleted (profile delete, reset):
    the order has to be "stop producing credentials, then remove them", or the
    session writes a fresh ``auth.json`` into the tree that was just removed.
    """
    session = _sessions_by_profile.get(profile)
    if session is None or session.status in _TERMINAL_STATUSES:
        return False
    await _cancel(session)
    return True


async def close_all() -> None:
    """Cancel every pending sign-in. Called from the server shutdown hook.

    Each pending session holds a ``codex app-server`` child that outlives this
    process if nobody stops it, so a restart would leak one child per pending
    sign-in.
    """
    for session in list(_by_login_id.values()):
        if session.status in _TERMINAL_STATUSES:
            continue
        try:
            await _cancel(session)
        except Exception:  # noqa: BLE001 - shutdown never fails on cleanup
            logger.debug("codex-login: closing a pending sign-in failed", exc_info=True)


def _reset_for_tests() -> None:
    """Drop every registry entry. Tests only - a leftover session from one test
    would otherwise be the 'previous' session the next one cancels."""
    _sessions_by_profile.clear()
    _by_login_id.clear()
    _profile_locks.clear()


__all__ = [
    "CodexLoginSession",
    "cancel",
    "cancel_for_profile",
    "close_all",
    "get",
    "start",
]
