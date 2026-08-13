"""Take a Google Suite skill's OAuth link apart, in an order that actually works.

Why this runs in-process rather than shelling out to the skill's own ``unlink``
-------------------------------------------------------------------------------
The skills' "the skill is the single writer of its token file" doctrine (see
:mod:`app.calendar.skill_token` and :mod:`app.drive.skill_token`) exists to stop
two *refresh* writes racing and leaving a half-written credential — which is why
``read_token`` there tolerates a mid-swap parse failure. An unlink is not a write:
it is an ``os.unlink`` performed **after** step 1 below has provably stopped every
other writer, and it is atomic — the file is there or it is not, so there is no
torn state for anyone to read. The invariant holds in fact, not merely in spirit.

Against that, nothing under ``app/`` invokes a skill script today (the ``uv run``
strings elsewhere only *register* autostart commands), so doing it that way would
put ``uv`` on ``PATH`` inside the server container, a cold PEP-723 dependency
resolve, and stdout parsing on the critical path of a Settings button.
:mod:`app.api.drive` already reads and refreshes this very token in-process, and
the one Google call an unlink needs — ``channels.stop`` — is a plain REST POST.

The skill keeps its own ``unlink`` for the chat path and standalone use. Both are
idempotent, and either is safe to run after the other has done half the work.

Ordering is load-bearing
------------------------
1. Stop the managed listener and drop its autostart row. It re-creates the watch
   channel on every renewal and restart, holds ``scripts/.listener.lock`` open
   (which fails the delete on Windows), and rewrites the token file after any
   successful refresh — so it can resurrect what step 4 removes.
2. Stop the Google push channel. Needs a live credential, so it precedes revoke.
3. Revoke at Google. Needs the refresh token, so it precedes the delete — and
   once the token is gone, revoking becomes permanently impossible.
4. Delete the local files, token first.
5. Evict the per-profile caches that read through to that file.
6. Re-``stat`` the token path: success is reported from the filesystem, never from
   intent.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.google.registry import (
    GOOGLE_CONNECTIONS_URL,
    GOOGLE_REVOKE_ENDPOINT,
    GOOGLE_SKILLS,
    GOOGLE_TOKEN_ENDPOINT,
    GoogleSkill,
)
from app.utils.logger import logger

_HTTP_TIMEOUT = 15.0

# Revoke outcomes, as they appear in ``revoke_status``.
REVOKED = "revoked"
ALREADY_REVOKED = "already_revoked"
SKIPPED = "skipped"
SKIPPED_SHARED_GRANT = "skipped_shared_grant"
NO_TOKEN = "no_token"


# ── locating and reading ─────────────────────────────────────────────────────

def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def skill_dir(profile: str, spec: GoogleSkill) -> Optional[Path]:
    """The installed skill directory for ``profile``, or None when absent."""
    from app.skills.sync import profile_skills_dir

    if not profile:
        return None
    path = profile_skills_dir(profile) / spec.dir_name
    return path if path.is_dir() else None


def token_path(profile: str, spec: GoogleSkill) -> Optional[Path]:
    base = skill_dir(profile, spec)
    if base is None:
        return None
    path = base / spec.token_rel
    return path if path.is_file() else None


def _read_json(path: Optional[Path], *, what: str) -> Optional[Dict[str, Any]]:
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # The skill replaces these atomically, so a read can land mid-swap.
        logger.debug(f"[google] unreadable {what} at {path}: {exc}")
        return None
    return data if isinstance(data, dict) else None


def read_token(profile: str, spec: GoogleSkill) -> Optional[Dict[str, Any]]:
    return _read_json(token_path(profile, spec), what=f"{spec.dir_name} token")


def read_listener_state(profile: str, spec: GoogleSkill) -> Dict[str, Any]:
    base = skill_dir(profile, spec)
    if base is None or not spec.has_listener:
        return {}
    return _read_json(base / "scripts" / ".listener_state.json", what="listener state") or {}


def _env_value(profile: str, spec: GoogleSkill, key: str) -> str:
    """One variable out of the installed skill's ``scripts/.env``.

    That file is the skill's only config channel, so it is the truest picture of
    what the skill would do on its next run. Mirrors
    :func:`app.drive.skill_token._env_override`, which is bound to gdrive.
    """
    base = skill_dir(profile, spec)
    if base is None:
        return ""
    path = base / "scripts" / ".env"
    if not path.is_file():
        return ""
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip("'\"")
    except OSError:
        return ""
    return ""


def _shared_client_id() -> str:
    try:
        from app.calendar import google_discovery

        return str(google_discovery.google_client().get("client_id") or "")
    except Exception as exc:  # noqa: BLE001 - an unreachable broker proves nothing
        logger.debug(f"[google] shared-client lookup failed: {exc}")
        return ""


def _tool_id(profile: str, spec: GoogleSkill) -> str:
    from app.tools.ids import slugify

    return f"{profile}__{slugify(spec.dir_name)}"


def skill_enabled(profile: str, spec: GoogleSkill) -> bool:
    """Whether this profile has the skill switched on.

    Skills have no ``profile_tools`` row until a profile enables them, and no row
    means disabled. A *disabled* skill can still hold a live token, which is
    exactly why the link is keyed on the directory rather than on registry
    presence.
    """
    if not profile:
        return False
    try:
        from app.storage.tool_storage import get_tool_storage

        return bool(get_tool_storage().list_profile_tools(profile).get(_tool_id(profile, spec)))
    except Exception as exc:  # noqa: BLE001 - storage not ready proves nothing
        logger.debug(f"[google] enabled lookup failed for {profile}/{spec.dir_name}: {exc}")
        return False


# ── the shared-grant question ────────────────────────────────────────────────

def _grant_key(data: Optional[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """``(client_id, account)`` — what Google actually revokes.

    A revoke ends the grant for an (OAuth client, account) pair, not for one
    skill, so two skills sharing this key share a fate. ``account_key`` is the
    canonical identity; ``email`` is the fallback for a hand-edited file.
    """
    if not data:
        return None
    client = str(data.get("client_id") or "")
    account = str(data.get("account_key") or data.get("email") or "")
    if not client or not account:
        return None
    return (client, account.lower())


def siblings_sharing_grant(
    profile: str, spec: GoogleSkill, data: Optional[Dict[str, Any]] = None
) -> List[str]:
    """Other Google skills in ``profile`` whose link dies with this one."""
    mine = _grant_key(data if data is not None else read_token(profile, spec))
    if mine is None:
        return []
    shared: List[str] = []
    for other in GOOGLE_SKILLS:
        if other.dir_name == spec.dir_name:
            continue
        if _grant_key(read_token(profile, other)) == mine:
            shared.append(other.dir_name)
    return shared


# ── inventory ────────────────────────────────────────────────────────────────

def status(profile: str, spec: GoogleSkill) -> Dict[str, Any]:
    """Everything a client needs to describe (and warn about) one link."""
    base = skill_dir(profile, spec)
    data = read_token(profile, spec) if base is not None else None
    state = read_listener_state(profile, spec) if base is not None else {}
    own_client = bool(_env_value(profile, spec, "GOOGLE_CLIENT_ID")) if base is not None else False
    if not own_client and data:
        mine = str(data.get("client_id") or "")
        shared = _shared_client_id()
        own_client = bool(mine and shared and mine != shared)

    autostart_rows = 0
    if base is not None and spec.has_listener:
        autostart_rows = _autostart_row_count(profile, base)

    expiration = state.get("watch_expiration")
    return {
        "skill": spec.dir_name,
        "label": spec.label,
        "tool_id": _tool_id(profile, spec) if profile else "",
        "installed": base is not None,
        "enabled": skill_enabled(profile, spec) if base is not None else False,
        "linked": data is not None,
        "email": (data or {}).get("email"),
        "account_key": (data or {}).get("account_key"),
        "scopes": list((data or {}).get("scopes") or []),
        "own_client": own_client,
        "listener": {"declared": spec.has_listener, "autostart_rows": autostart_rows},
        "watch": {
            "active": bool(state.get("channel_id")),
            # Google reports channel expiry in milliseconds.
            "expires_at": int(expiration) // 1000 if expiration else None,
        },
        "subscriptions": {"idle_after_unlink": _idle_subscriptions(profile, spec)},
        "siblings_sharing_grant": siblings_sharing_grant(profile, spec, data),
        "consequence": spec.consequence,
    }


def _autostart_row_count(profile: str, base: Path) -> int:
    try:
        from app.storage.autostart_storage import get_autostart_storage

        root = base.resolve()
        rows = get_autostart_storage().list(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[google] autostart lookup failed for {profile}: {exc}")
        return 0
    count = 0
    for row in rows:
        raw = row.get("working_dir") or ""
        if not raw:
            continue
        try:
            candidate = Path(raw).resolve()
        except (OSError, ValueError):
            continue
        if candidate == root or root in candidate.parents:
            count += 1
    return count


def _idle_subscriptions(profile: str, spec: GoogleSkill) -> int:
    """Event automations that stop firing once the listener is gone.

    Reported, never deleted: re-linking and re-registering the listener brings
    them back, so removing them would destroy user configuration to describe a
    temporary state.
    """
    if not spec.event_types or not profile:
        return 0
    try:
        from app.tools.ids import slugify
        from app.storage.event_subscription_storage import EventSubscriptionStorage

        rows = EventSubscriptionStorage().list_by_profile(profile)
    except Exception as exc:  # noqa: BLE001 - storage not ready proves nothing
        logger.debug(f"[google] subscription lookup failed for {profile}: {exc}")
        return 0
    # Rows have been written under all three spellings over time, the same set
    # ``EventSubscriptionStorage.repoint_skill`` matches on.
    keys = {_tool_id(profile, spec), slugify(spec.dir_name), spec.dir_name}
    return sum(
        1
        for row in rows
        if str(row.get("skill_name") or "") in keys and not row.get("paused")
    )


def inventory(profile: str) -> Dict[str, Any]:
    """Per-skill link state plus the account groupings the warnings need."""
    skills = [status(profile, spec) for spec in GOOGLE_SKILLS]

    grouped: Dict[str, List[str]] = {}
    for row in skills:
        if row["linked"] and row["email"]:
            grouped.setdefault(str(row["email"]), []).append(row["skill"])
    accounts = [
        {"email": email, "skills": members, "shared_grant": len(members) > 1}
        for email, members in sorted(grouped.items())
    ]

    calendar: Dict[str, Any] = {"source": None, "connected": False, "app_credential_present": False}
    try:
        from app.calendar import google_auth

        current = google_auth.status(profile)
        calendar["source"] = current.get("source")
        calendar["connected"] = bool(current.get("connected"))
        calendar["app_credential_present"] = _app_credential_present(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[google] calendar status lookup failed for {profile}: {exc}")

    return {
        "ok": True,
        "profile": profile,
        "revoke_url": GOOGLE_CONNECTIONS_URL,
        "skills": skills,
        "accounts": accounts,
        "calendar": calendar,
    }


def _app_credential_present(profile: str) -> bool:
    """Whether the Calendar page holds its own Google credential.

    That credential is the documented fallback when the skill link goes away, so
    it is never deleted here — but a revoke on the shared client may kill it, and
    the user has to be told that.
    """
    try:
        from app.calendar.google_auth import AGENT_NAME, AGENT_TYPE
        from app.utils.client_storage import REFRESH_TOKEN, get_auth_client_storage

        stored = get_auth_client_storage().get_token(
            AGENT_NAME, profile, agent_type=AGENT_TYPE, token_kind=REFRESH_TOKEN
        )
        return bool(stored)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[google] app-credential lookup failed for {profile}: {exc}")
        return False


# ── the Google calls ─────────────────────────────────────────────────────────

def _access_token(data: Dict[str, Any]) -> Optional[str]:
    """A usable access token, refreshed in memory only. None when unavailable.

    Deliberately never writes the refreshed token back: the file is about to be
    deleted, and writing to it here would be the one thing the single-writer rule
    genuinely forbids.
    """
    stored = data.get("access_token")
    if stored and time.time() < _as_float(data.get("expiry")) - 60:
        return str(stored)
    refresh = str(data.get("refresh_token") or "")
    if not refresh:
        return None
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                GOOGLE_TOKEN_ENDPOINT,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": str(data.get("client_id") or ""),
                    "client_secret": str(data.get("client_secret") or ""),
                },
            )
            resp.raise_for_status()
            token = resp.json().get("access_token")
    except Exception as exc:  # noqa: BLE001 - any failure means "no token"
        logger.debug(f"[google] in-memory refresh failed: {exc}")
        return None
    return str(token) if token else None


def _stop_watch(
    spec: GoogleSkill, data: Optional[Dict[str, Any]], state: Dict[str, Any]
) -> Tuple[bool, Optional[str]]:
    """Close the Google push channel. ``(stopped, error)``.

    "Nothing to stop" counts as stopped — an absent channel is the state we want.
    """
    channel_id = str(state.get("channel_id") or "")
    resource_id = str(state.get("resource_id") or "")
    if not spec.watch_base or not channel_id or not resource_id:
        return True, None
    if not data:
        return False, (
            "the credential was already gone, so the Google push channel could not be "
            "stopped; it expires on its own within about a week"
        )
    token = _access_token(data)
    if not token:
        return False, (
            "no usable access token, so the Google push channel could not be stopped; "
            "it expires on its own within about a week"
        )
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(
                f"{spec.watch_base}/channels/stop",
                json={"id": channel_id, "resourceId": resource_id},
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"could not reach Google to stop the push channel: {exc}"
    if resp.status_code in (200, 204, 404):
        # 404 means Google has already forgotten the channel.
        return True, None
    body = (resp.text or "")[:200]
    if "channelIdNotFound" in body:
        return True, None
    return False, f"Google refused to stop the push channel (HTTP {resp.status_code}): {body}"


def _is_invalid_token(resp: httpx.Response) -> bool:
    """Whether a 400 means "this token is already unknown to Google"."""
    try:
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return "invalid_token" in (resp.text or "")
    if isinstance(payload, dict) and str(payload.get("error") or "") == "invalid_token":
        return True
    return "invalid_token" in (resp.text or "")


def revoke_grant(data: Dict[str, Any]) -> Tuple[bool, str]:
    """Revoke Cremind's grant at Google. ``(revoked, status)``.

    A 400 ``invalid_token`` counts as revoked: there is nothing left to revoke,
    so reporting failure would make a benign re-run — and every revoke after the
    first on a shared grant — look like a problem.
    """
    token = str(data.get("refresh_token") or "") or str(data.get("access_token") or "")
    if not token:
        return False, NO_TOKEN
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            resp = client.post(GOOGLE_REVOKE_ENDPOINT, data={"token": token})
    except Exception as exc:  # noqa: BLE001
        return False, f"network: {exc}"
    if resp.status_code == 200:
        return True, REVOKED
    if resp.status_code == 400 and _is_invalid_token(resp):
        return True, ALREADY_REVOKED
    return False, f"http_{resp.status_code}: {(resp.text or '')[:200]}"


# ── local wipe ───────────────────────────────────────────────────────────────

def _robust_unlink(path: Path, *, attempts: int = 3, delay: float = 0.2) -> Optional[str]:
    """Delete ``path``. Returns an error string on failure, else None.

    Retries like :func:`app.skills.sync._robust_rmtree`: after a process tree is
    killed, Windows can take a moment to release the handles it held.
    """
    last: Optional[OSError] = None
    for attempt in range(attempts):
        try:
            path.unlink()
            return None
        except FileNotFoundError:
            return None
        except OSError as exc:
            last = exc
            try:
                os.chmod(path, stat.S_IWRITE)
            except OSError:
                pass
            if attempt < attempts - 1:
                time.sleep(delay)
    return str(last) if last is not None else "could not be removed"


def _delete_files(profile: str, spec: GoogleSkill) -> Tuple[List[str], List[str]]:
    """Remove the link's files. ``(cleaned, failed)`` as skill-relative paths."""
    base = skill_dir(profile, spec)
    if base is None:
        return [], []
    cleaned: List[str] = []
    failed: List[str] = []

    for rel in spec.delete_rel:
        path = base / rel
        if not path.exists():
            continue
        error = _robust_unlink(path)
        if error is None:
            cleaned.append(rel.as_posix())
        else:
            failed.append(rel.as_posix())
            logger.warning(f"[google] could not remove {path}: {error}")

    # Event payloads hold calendar entries / file names from the account being
    # unlinked. The event manager treats this as a delete-only drop zone.
    for rel in spec.event_dirs_rel:
        folder = base / rel
        if not folder.is_dir():
            continue
        for payload in sorted(folder.glob("*.md")):
            error = _robust_unlink(payload)
            if error is None:
                cleaned.append((rel / payload.name).as_posix())
            else:
                failed.append((rel / payload.name).as_posix())
    return cleaned, failed


def _forget(profile: str) -> None:
    """Evict every per-profile cache that reads through to the deleted token.

    Called for *every* Google unlink, not only gcalendar/gdrive: these are
    per-profile dicts, the calls are free, and making them conditional on skill
    identity is one more thing to get wrong.
    """
    from app.calendar import skill_token as calendar_token
    from app.drive import grant_flow
    from app.drive import skill_token as drive_token

    for label, call in (
        ("calendar access cache", lambda: calendar_token.forget_access_token(profile)),
        ("drive access cache", lambda: drive_token.forget_access_token(profile)),
        ("drive grant rounds", lambda: grant_flow.abandon_rounds(profile)),
    ):
        try:
            call()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[google] could not clear the {label} for {profile}: {exc}")


# ── orchestration ────────────────────────────────────────────────────────────

async def _teardown_listener(profile: str, base: Path) -> Dict[str, Any]:
    """Stop anything running in the skill directory and drop its autostart row."""
    from app.tools.builtin.exec_shell_autostart import teardown_processes_for_dir

    try:
        return await teardown_processes_for_dir(base, profile=profile)
    except Exception as exc:  # noqa: BLE001 - never block the wipe on this
        logger.warning(f"[google] listener teardown failed for {profile}/{base.name}: {exc}")
        return {"stopped": [], "removed_autostart": 0, "error": str(exc)}


def _message(result: Dict[str, Any], spec: GoogleSkill) -> str:
    """The one human sentence every client shows.

    Built here because the CLI's error handling keeps only the machine ``error``
    code (``app/cli/client/_base.py``), so any prose a user must read has to
    travel in the payload.
    """
    who = result.get("email") or "the linked account"

    # Checked before "not linked": a surviving credential file means the wipe
    # failed, which is the opposite of nothing-to-do and must never read as it.
    if result.get("still_linked"):
        paths = ", ".join(result.get("failed_paths") or []) or "its credential file"
        revoked = (
            "Google access for that account IS revoked, but"
            if result.get("revoked")
            else "Google was not contacted, and"
        )
        return (
            f"{revoked} this machine still holds {paths} for {spec.label}. Cremind will "
            f"fail with invalid_grant until it is removed. Close anything using the "
            f"{spec.dir_name} skill — its listener, or an open shell in that directory — "
            "and try again."
        )

    if not result.get("unlinked"):
        return f"The {spec.dir_name} skill is not linked to a Google account — nothing to unlink."

    parts = [f"Unlinked {spec.label} ({who})"]
    status_value = result.get("revoke_status")
    if result.get("revoked"):
        parts.append(
            "and revoked Cremind's access at Google"
            if status_value == REVOKED
            else "and Google had already dropped this grant"
        )
    elif status_value == SKIPPED_SHARED_GRANT:
        shared = ", ".join(result.get("siblings_sharing_grant") or [])
        parts.append(
            "on this machine only. Google was not told, because the same account is "
            f"still linked in {shared} and revoking would break those too — unlink them "
            "as well, or use --all, to end the grant at Google"
        )
    elif status_value == SKIPPED:
        parts.append("on this machine only, as asked — the grant is still live at Google")
    else:
        parts.append(
            f"on this machine, but Google was not told: {result.get('revoke_error')}. "
            "Cremind can no longer use the account, but the grant is still listed on "
            "your Google account — and the token needed to revoke it is gone, so "
            f"re-running this will not help. Remove Cremind at {GOOGLE_CONNECTIONS_URL}"
        )

    if result.get("autostart_removed"):
        parts.append(
            f"The {spec.dir_name} listener was stopped and its autostart registration "
            "removed — register it again after re-linking"
        )
    if result.get("watch_error"):
        parts.append(str(result["watch_error"]))
    if result.get("calendar_source_after") is not None or spec.dir_name == "gcalendar":
        after = result.get("calendar_source_after")
        parts.append(
            "the Calendar & Schedule page now uses the account connected on that page"
            if after == "app"
            else "the Calendar & Schedule page now uses the built-in system calendar"
            if after is None
            else f"the Calendar & Schedule page now uses the {after} credential"
        )
    return ". ".join(part.rstrip(".") for part in parts) + "."


async def unlink_skill(
    profile: str,
    spec: GoogleSkill,
    *,
    revoke: bool = True,
    stop_watch: bool = True,
    force_revoke: bool = False,
    _forget_caches: bool = True,
) -> Dict[str, Any]:
    """Unlink one Google skill for one profile. Idempotent.

    ``force_revoke`` overrides the shared-grant guard: by default a revoke is
    skipped when another skill in this profile still holds the same grant, so a
    per-skill unlink cannot silently break its siblings.
    """
    result: Dict[str, Any] = {
        "skill": spec.dir_name,
        "label": spec.label,
        "ok": True,
        "unlinked": False,
        "already": False,
        "email": None,
        "revoked": False,
        "revoke_attempted": False,
        "revoke_status": SKIPPED,
        "revoke_error": None,
        "watch_stopped": True,
        "watch_error": None,
        "listener_stopped": False,
        "autostart_removed": 0,
        "cleaned": [],
        "failed_paths": [],
        "still_linked": False,
        "siblings_sharing_grant": [],
        "app_credential_at_risk": False,
        "calendar_source_after": None,
        "subscriptions_idle": 0,
    }

    base = skill_dir(profile, spec)
    if base is None:
        result["already"] = True
        result["reason"] = "not_installed"
        result["message"] = (
            f"The {spec.dir_name} skill is not installed for profile "
            f"{profile!r}, so it holds no Google link."
        )
        return result

    # 0. Read everything first — later steps destroy both sources.
    data = read_token(profile, spec)
    state = read_listener_state(profile, spec)
    result["email"] = (data or {}).get("email")
    result["subscriptions_idle"] = _idle_subscriptions(profile, spec)
    shared = siblings_sharing_grant(profile, spec, data)
    result["siblings_sharing_grant"] = shared

    # 1. Stop every other writer, always first. Also covers an in-flight ``link``
    #    running in this directory, which would otherwise re-create the token.
    teardown = await _teardown_listener(profile, base)
    result["listener_stopped"] = bool(teardown.get("stopped"))
    result["autostart_removed"] = int(teardown.get("removed_autostart") or 0)

    if data is None:
        # Not linked, but a previous life may have left derived files behind.
        cleaned, failed = await asyncio.to_thread(_delete_files, profile, spec)
        result["cleaned"] = cleaned
        result["failed_paths"] = failed
        result["already"] = True
        result["reason"] = "not_linked"
        result["message"] = _message(result, spec)
        return result

    # 2. Stop the Google push channel while the credential still works.
    if stop_watch:
        stopped, watch_error = await asyncio.to_thread(_stop_watch, spec, data, state)
        result["watch_stopped"] = stopped
        result["watch_error"] = watch_error

    # 3. Revoke — subject to the shared-grant guard.
    if revoke and shared and not force_revoke:
        result["revoke_status"] = SKIPPED_SHARED_GRANT
    elif revoke:
        result["revoke_attempted"] = True
        revoked, revoke_status = await asyncio.to_thread(revoke_grant, data)
        result["revoked"] = revoked
        result["revoke_status"] = revoke_status
        if not revoked:
            result["revoke_error"] = revoke_status
        if revoked:
            result["app_credential_at_risk"] = _app_credential_at_risk(profile, data)

    # 4. Delete the local files, token first.
    cleaned, failed = await asyncio.to_thread(_delete_files, profile, spec)
    result["cleaned"] = cleaned
    result["failed_paths"] = failed

    # 5. Evict the caches that read through to the file we just removed.
    if _forget_caches:
        _forget(profile)

    # 6. Report from the filesystem, not from intent.
    result["still_linked"] = token_path(profile, spec) is not None
    result["unlinked"] = not result["still_linked"]
    result["ok"] = not result["still_linked"]

    if spec.dir_name == "gcalendar":
        result["calendar_source_after"] = _calendar_source(profile)

    result["message"] = _message(result, spec)
    return result


def _app_credential_at_risk(profile: str, data: Dict[str, Any]) -> bool:
    """Whether the revoke may also have killed the Calendar page's credential.

    It shares the OAuth client, and the page's flow requests no email scope, so
    we cannot compare accounts — hence "at risk" rather than "revoked".
    """
    if not _app_credential_present(profile):
        return False
    mine = str(data.get("client_id") or "")
    return bool(mine) and mine == _shared_client_id()


def _calendar_source(profile: str) -> Optional[str]:
    try:
        from app.calendar import google_auth

        return google_auth.status(profile).get("source")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[google] calendar source lookup failed for {profile}: {exc}")
        return None


async def unlink_all(profile: str, *, revoke: bool = True) -> Dict[str, Any]:
    """Unlink every Google skill for ``profile``, phased across skills.

    Phased rather than looped, because revoking the first skill can end a shared
    grant and leave the next skill's ``channels.stop`` holding a dead token. So
    every listener stops, then every watch, then the revokes, then the deletes.
    """
    specs = [spec for spec in GOOGLE_SKILLS if skill_dir(profile, spec) is not None]

    # A. stop every writer.
    for spec in specs:
        base = skill_dir(profile, spec)
        if base is not None:
            await _teardown_listener(profile, base)

    # B. stop every watch channel, while all credentials are still valid.
    tokens: Dict[str, Optional[Dict[str, Any]]] = {}
    watch: Dict[str, Tuple[bool, Optional[str]]] = {}
    for spec in specs:
        tokens[spec.dir_name] = read_token(profile, spec)
        state = read_listener_state(profile, spec)
        watch[spec.dir_name] = await asyncio.to_thread(
            _stop_watch, spec, tokens[spec.dir_name], state
        )

    # C-D. revoke and wipe each. force_revoke: every sibling is going anyway, so
    # the shared-grant guard has nothing left to protect.
    results: List[Dict[str, Any]] = []
    for spec in specs:
        outcome = await unlink_skill(
            profile,
            spec,
            revoke=revoke,
            stop_watch=False,  # already done in phase B
            force_revoke=True,
            _forget_caches=False,  # once, in phase E
        )
        stopped, watch_error = watch.get(spec.dir_name, (True, None))
        outcome["watch_stopped"] = stopped
        if watch_error:
            outcome["watch_error"] = watch_error
        outcome["message"] = _message(outcome, spec)
        results.append(outcome)

    # E. evict once.
    _forget(profile)

    unlinked = sum(1 for row in results if row.get("unlinked"))
    already = sum(1 for row in results if row.get("already"))
    failed = [row["skill"] for row in results if row.get("still_linked")]
    summary = f"{unlinked} unlinked, {already} not linked"
    if failed:
        summary += f", {len(failed)} still holds a credential file"
    return {
        "ok": not failed,
        "results": results,
        "unlinked": unlinked,
        "already": already,
        "failed": failed,
        "message": summary + ".",
    }
