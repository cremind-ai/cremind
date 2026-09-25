"""User Document Search — settings, admin gate and progress API.

Backs Settings → My Documents, the admin card on the Vector Embedding page,
and the ``cremind userdocs`` CLI:

- ``GET/PUT /api/userdocs/admin``      — the server-wide gate (admin only).
- ``GET     /api/userdocs/status``     — this profile's progress snapshot.
- ``GET     /api/userdocs/stream``     — the same snapshot as SSE, live.
- ``GET/PUT /api/userdocs/settings``   — this profile's sources (folder, Drive).
- ``POST    /api/userdocs/validate-root`` — would this folder be accepted?
- ``GET     /api/userdocs/browse``     — directory picker for the root.
- ``GET     /api/userdocs/drive/folders`` — Drive folder picker (``include_folders``).

The profile is always the caller's own (``request.user.username``), never a
body or query field, so no route can read or change another profile's index.

Destructive saves are two-step. A ``PUT /settings`` that would remove indexed
content (turning Drive off, moving the folder, deleting the index) answers
``409 ConfirmationRequired`` with a plan of what would go and a ``confirm``
token; repeating the request with that token applies it. The token is bound to
the exact change and the row's version, so a stale dialog cannot confirm a
different change. Turning Drive off always deletes the Drive index once
applied: a Drive index outliving its switch would keep serving files the user
chose to stop sharing.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from app.api._auth import is_admin, require_admin, require_auth
from app.userdocs import settings as uds
from app.userdocs import state as uds_state
from app.utils.credential_paths import CREDENTIAL_DIR_NAMES
from app.utils.logger import logger

# The directory picker never lists more than this many subfolders; a folder
# with more is shown truncated rather than stalling the dialog.
_BROWSE_LIMIT = 2000

# A Drive file/folder id: what Google hands out, and all the Drive client will
# splice into a query ("'<id>' in parents").
_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

# The gdrive skill replaces its token file atomically, so a read can land
# mid-swap and see nothing. Before concluding "not linked", read once more.
_TOKEN_REREAD_S = 0.25

# Whole-Drive access: either scope lets a listing enumerate the entire Drive,
# which is why such an account must name the folders to index.
_WHOLE_DRIVE_SCOPES = frozenset({
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.readonly",
})


def _profile(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _storage():
    from app.storage.userdocs_storage import get_userdocs_storage
    return get_userdocs_storage()


def _feature_missing() -> List[Dict[str, Any]]:
    """The ``userdocs`` extras that are not installed, in the exact shape the
    embedding PUT uses, so the existing install dialog and CLI hint just work."""
    from app.features.manifest import FEATURES, missing_features

    return [
        {
            "feature_key": key,
            "extras": list(FEATURES[key].extras),
            "requires_restart_after_install": FEATURES[key].requires_restart,
        }
        for key in missing_features(["userdocs"])
    ]


def _gate_error(reason: Optional[str]) -> JSONResponse:
    if reason == "admin_gate_off":
        return JSONResponse(
            {
                "error": "FeatureDisabledByAdmin",
                "message": "User Document Search has not been allowed by the administrator.",
            },
            status_code=409,
        )
    return JSONResponse(
        {
            "error": "EmbeddingDisabled",
            "message": "User Document Search needs Vector Embedding, which is turned off.",
        },
        status_code=409,
    )


def _source_payload(row: Optional[Dict[str, Any]], kind: str) -> Dict[str, Any]:
    row = row or {}
    return {
        "kind": kind,
        "enabled": bool(row.get("enabled")),
        "root_mode": row.get("root_mode") or uds.ROOT_INHERIT,
        "root_path": row.get("root_path"),
        "excludes": uds.normalize_excludes(row.get("excludes")),
        "options": uds.normalize_options(row.get("options")),
        "first_sync_confirmed": bool(row.get("first_sync_confirmed_at")),
        "updated_at": row.get("updated_at"),
    }


def _is_whole_drive(scopes: Any) -> bool:
    try:
        from app.userdocs.sources.drive_client import is_whole_drive
    except ImportError:  # the Drive client is absent (a slim build): same rule, local copy
        return bool(_WHOLE_DRIVE_SCOPES & set(scopes or []))
    return bool(is_whole_drive(list(scopes or [])))


def _drive_index_view(profile: str) -> Optional[Dict[str, Any]]:
    """The engine's view of the Drive index (state, hold reason, counts, last
    sync), as the progress snapshot carries it; None when there is none."""
    try:
        drive = uds_state.build_snapshot(profile).get("drive")
    except Exception as exc:  # noqa: BLE001 — a settings page must render regardless
        logger.debug(f"[userdocs] drive index view failed for {profile}: {exc}")
        return None
    return drive if isinstance(drive, dict) else None


def _drive_view(profile: str) -> Dict[str, Any]:
    """Drive link state for the settings page. ``status`` may consult the
    Google broker for the expected scopes, so it runs off the event loop.

    ``whole_drive`` follows the indexing rule (``drive`` or ``drive.readonly``
    in the granted scopes), because it decides whether folders must be chosen.
    ``identity_email`` is the account the Drive index was built from — after a
    re-link to another address it differs from ``email`` until the engine has
    re-indexed. ``index`` is the engine's Drive view, when an engine runs."""
    index = _drive_index_view(profile)
    identity_email = (index or {}).get("account_email")
    try:
        from app.drive import skill_token

        st = skill_token.status(profile)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[userdocs] drive status failed for {profile}: {exc}")
        return {"linked": False, "identity_email": identity_email, "index": index}
    linked = bool(st.get("linked"))
    return {
        "linked": linked,
        "email": st.get("email"),
        "whole_drive": linked and _is_whole_drive(st.get("scopes")),
        "access_model": st.get("access_model"),
        "scopes_stale": bool(st.get("scopes_stale")),
        "identity_email": identity_email,
        "index": index,
    }


def _drive_token(profile: str) -> Optional[Dict[str, Any]]:
    """The gdrive skill's token file, read a second time before concluding it
    is missing (a read can land in the middle of the skill's atomic swap)."""
    from app.drive import skill_token

    data = skill_token.read_token(profile)
    if data or skill_token.skill_dir(profile) is None:
        return data or None
    time.sleep(_TOKEN_REREAD_S)
    return skill_token.read_token(profile) or None


def _drive_linked(profile: str) -> bool:
    try:
        return bool(_drive_token(profile))
    except Exception:  # noqa: BLE001
        return False


def _drive_not_linked(message: Optional[str] = None, **extra: Any) -> JSONResponse:
    return JSONResponse(
        {
            "error": "DriveNotLinked",
            "message": message or "Link Google Drive first (Settings → GSuite, or the gdrive skill).",
            **extra,
        },
        status_code=409,
    )


# ── admin ──────────────────────────────────────────────────────────────────


def _admin_view() -> Dict[str, Any]:
    from app.config.settings import BaseConfig

    policy = uds.read_admin_policy()
    effective, reason = uds.feature_effective(policy)
    rows = _storage().list_sources()
    per_profile: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        entry = per_profile.setdefault(r["profile"], {"profile": r["profile"]})
        # Counts and flags only — an admin overview never shows another
        # profile's folder paths or file names.
        entry[f"{r['kind']}_enabled"] = bool(r.get("enabled"))
    return {
        "policy": policy.to_dict(),
        "effective": effective,
        "reason": reason,
        "embedding_enabled": BaseConfig.is_embedding_enabled(),
        "feature": {"missing": _feature_missing()},
        "profiles": sorted(per_profile.values(), key=lambda e: e["profile"]),
    }


# ── settings ───────────────────────────────────────────────────────────────


def _settings_view(profile: str, admin: bool) -> Dict[str, Any]:
    storage = _storage()
    policy = uds.read_admin_policy()
    effective, reason = uds.feature_effective(policy)
    return {
        "local": _source_payload(storage.get_source(profile, uds.SOURCE_LOCAL), uds.SOURCE_LOCAL),
        "drive": _source_payload(storage.get_source(profile, uds.SOURCE_DRIVE), uds.SOURCE_DRIVE),
        "policy_view": {
            "allowed": policy.allowed,
            "effective": effective,
            "reason": reason,
            "is_admin": admin,
            "working_dir": uds.working_dir(),
            # Non-admin profiles may only index inside the working directory.
            "root_constraint": "any" if admin else "working_dir",
            "vision_daily_cap_default": policy.vision_daily_cap_default,
            "max_file_mb": policy.max_file_mb,
        },
        "drive_link": _drive_view(profile),
        "vision": _vision_view(profile, storage),
    }


def _vision_view(profile: str, storage) -> Dict[str, Any]:
    """Whether images can be captioned for this profile, and why not.

    ``reason`` is one of vision_disabled / vision_model_unset /
    vision_model_auth_incompatible / vision_model_not_capable (fix in
    Settings → LLM Providers → Specialized Vision Model) or ``no_consent``
    (the user has not agreed to send images to this provider yet)."""
    try:
        from app.userdocs.vision import captioner, resolver

        res = resolver.resolve_dedicated_vision(profile)
        row = storage.get_source(profile, uds.SOURCE_LOCAL) or {}
        opts = uds.normalize_options(row.get("options"))
        consent = resolver.consent_matches(opts, res)
        cap = captioner.daily_cap(opts)
        day = captioner.local_day(profile)
        used = storage.vision_usage(profile, day)
        return {
            "ready": bool(res.ok and consent),
            "provider": res.provider, "model": res.model,
            "reason": res.reason if not res.ok else (None if consent else "no_consent"),
            "consent": consent,
            "consent_for": opts.get("caption_consent"),
            "quota": {"day": day, "used": used["captions"], "ocr_pages": used["ocr_pages"], "cap": cap},
        }
    except Exception as exc:  # noqa: BLE001 — a settings page must render regardless
        logger.debug(f"[userdocs] vision view failed for {profile}: {exc}")
        return {"ready": False, "reason": "vision_model_error"}


class _BadRequest(Exception):
    def __init__(self, response: JSONResponse):
        self.response = response


def _validation_failed(details: Dict[str, str], code: Optional[str] = None) -> _BadRequest:
    body: Dict[str, Any] = {"error": "ValidationFailed", "details": details}
    if code:
        body["code"] = code
    return _BadRequest(JSONResponse(body, status_code=400))


def _build_patch(
    profile: str,
    kind: str,
    body: Dict[str, Any],
    current: Optional[Dict[str, Any]],
    admin: bool,
) -> Dict[str, Any]:
    """Turn a request body into validated column values for ``kind``.

    Raises :class:`_BadRequest` with the response to send on any problem.
    Only fields present in the body are touched, so a save that changes one
    option cannot reset the folder.
    """
    patch: Dict[str, Any] = {}
    cur = current or {}

    if "enabled" in body:
        enabled = bool(body.get("enabled"))
        if enabled:
            ok, reason = uds.feature_effective()
            if not ok:
                raise _BadRequest(_gate_error(reason))
            if kind == uds.SOURCE_DRIVE and not _drive_linked(profile):
                raise _BadRequest(_drive_not_linked())
        patch["enabled"] = enabled

    if kind == uds.SOURCE_LOCAL:
        wants_root = "root_mode" in body or "root_path" in body
        first_enable = patch.get("enabled") and not cur.get("root_path")
        if wants_root or first_enable:
            mode = body.get("root_mode") or (
                uds.ROOT_CUSTOM if body.get("root_path") else cur.get("root_mode") or uds.ROOT_INHERIT
            )
            if mode not in (uds.ROOT_INHERIT, uds.ROOT_CUSTOM):
                raise _validation_failed({"root_mode": "must be 'inherit' or 'custom'"})
            raw = None if mode == uds.ROOT_INHERIT else body.get("root_path") or cur.get("root_path")
            if mode == uds.ROOT_CUSTOM and not raw:
                raise _validation_failed({"root_path": "choose a folder"})
            check = uds.validate_root(raw, is_admin=admin)
            if not check.ok:
                raise _validation_failed({"root_path": check.message or "invalid folder"}, check.code)
            patch["root_mode"] = mode
            patch["root_path"] = check.path

    if "excludes" in body:
        patch["excludes"] = uds.normalize_excludes(body.get("excludes"))

    if "options" in body:
        raw_opts = body.get("options")
        if isinstance(raw_opts, dict):
            # Consent is recorded only by the consent_vision action, which
            # checks it names the model the user was shown.
            raw_opts = {k: v for k, v in raw_opts.items() if k != "caption_consent"}
        patch["options"] = uds.normalize_options(raw_opts, base=cur.get("options"))

    if kind == uds.SOURCE_DRIVE:
        _check_drive_folders(profile, patch, cur)

    if body.get("confirm_first_sync"):
        patch["first_sync_confirmed_at"] = time.time() * 1000
    return patch


def _check_drive_folders(profile: str, patch: Dict[str, Any], cur: Dict[str, Any]) -> None:
    """Folder ids must look like Drive ids, and a whole-Drive account must
    name at least one folder before Drive indexing can be on.

    Whole-Drive access would otherwise index everything in the account, which
    nobody asked for by flipping one switch. A per-file account needs no
    folders: what it can reach is already what the user granted.
    """
    opts = patch.get("options") or uds.normalize_options(cur.get("options"))
    folders = list(opts.get("include_folders") or [])
    if "options" in patch:
        bad = [f for f in folders if not _DRIVE_ID_RE.match(f)]
        if bad:
            raise _validation_failed({"include_folders": f"not a Drive folder id: {bad[0]}"})
    turning_on = patch.get("enabled") is True
    stays_on = bool(patch.get("enabled", cur.get("enabled")))
    if folders or not stays_on or not (turning_on or "options" in patch):
        return
    try:
        scopes = (_drive_token(profile) or {}).get("scopes")
    except Exception:  # noqa: BLE001 — unreadable token: the linked check decides
        return
    if _is_whole_drive(scopes):
        raise _BadRequest(JSONResponse(
            {
                "error": "DriveFoldersRequired",
                "message": (
                    "This Google account gives Cremind access to your whole Drive, so choose "
                    "at least one folder to index first (My Documents → Google Drive → Choose "
                    "folders, or `cremind userdocs drive enable --folder ID`)."
                ),
                "whole_drive": True,
            },
            status_code=409,
        ))


def _apply_settings(profile: str, admin: bool, body: Dict[str, Any]) -> JSONResponse:
    """The whole PUT /settings, run in a worker thread."""
    kind = body.get("kind") or uds.SOURCE_LOCAL
    if kind not in uds.SOURCE_KINDS:
        return JSONResponse(
            {"error": "ValidationFailed", "details": {"kind": "must be 'local' or 'drive'"}},
            status_code=400,
        )
    storage = _storage()
    current = storage.get_source(profile, kind)
    try:
        patch = _build_patch(profile, kind, body, current, admin)
    except _BadRequest as bad:
        return bad.response

    delete_index = bool(body.get("delete_index"))
    if delete_index and patch.get("enabled", current.get("enabled") if current else False):
        return JSONResponse(
            {
                "error": "ValidationFailed",
                "details": {"delete_index": "only valid when turning the source off"},
            },
            status_code=400,
        )

    plan = uds.plan_source_change(profile, kind, current, {**patch, "delete_index": delete_index})
    if plan.destructive and body.get("confirm") != plan.token:
        return JSONResponse(
            {
                "error": "ConfirmationRequired",
                "message": "This change removes indexed content. Review the plan and confirm.",
                "plan": plan.to_dict(),
                "confirm": plan.token,
            },
            status_code=409,
        )

    # Turning Drive off deletes its index (the plan above said so, and was
    # confirmed when there was anything to delete), with or without
    # delete_index.
    drive_off = (
        kind == uds.SOURCE_DRIVE
        and bool((current or {}).get("enabled"))
        and patch.get("enabled") is False
    )
    if patch:
        storage.upsert_source(profile, kind, **patch)
    if delete_index or drive_off:
        uds_state.request_purge(profile, kind)
    uds_state.notify_settings_changed(profile, kind)
    return JSONResponse({
        "settings": _settings_view(profile, admin),
        "snapshot": uds_state.build_snapshot(profile),
    })


# ── browse ─────────────────────────────────────────────────────────────────


def _browse(profile: str, admin: bool, raw: Optional[str], hidden: bool) -> JSONResponse:
    wd = uds.working_dir()
    sysdir = uds.system_dir()
    target = uds.real_path(raw) if raw else wd
    if not admin and not uds.is_inside(target, wd):
        return JSONResponse(
            {"error": "Forbidden", "message": "Only folders inside the working directory can be browsed."},
            status_code=403,
        )
    if uds.is_inside(target, sysdir):
        return JSONResponse(
            {"error": "Forbidden", "message": "Cremind's system folder cannot be indexed."},
            status_code=403,
        )
    if not os.path.isdir(target):
        return JSONResponse({"error": "NotFound", "message": "No such folder."}, status_code=404)

    entries: List[Dict[str, Any]] = []
    truncated = False
    try:
        with os.scandir(target) as it:
            for de in it:
                try:
                    if not de.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if not hidden and de.name.startswith("."):
                    continue
                if de.name in CREDENTIAL_DIR_NAMES:
                    continue
                path = os.path.join(target, de.name)
                if uds.is_inside(path, sysdir):
                    continue
                if len(entries) >= _BROWSE_LIMIT:
                    truncated = True
                    break
                entries.append({"name": de.name, "path": path})
    except PermissionError:
        return JSONResponse({"error": "Forbidden", "message": "Permission denied."}, status_code=403)
    except OSError as exc:
        return JSONResponse({"error": "ReadFailed", "message": str(exc)}, status_code=400)

    entries.sort(key=lambda e: e["name"].lower())
    parent = os.path.dirname(target.rstrip("\\/")) or None
    if parent and (parent == target or (not admin and not uds.is_inside(parent, wd))):
        parent = None
    roots = [wd] + ([os.path.expanduser("~")] if admin else [])
    return JSONResponse({
        "path": target,
        "parent": parent,
        "entries": entries,
        "truncated": truncated,
        "roots": roots,
    })


# ── Drive folder picker ────────────────────────────────────────────────────


def _drive_client(profile: str):
    """A Drive client on the profile's gdrive link (replaced in tests)."""
    from app.userdocs.sources.drive_client import DriveClient

    return DriveClient(profile)


def _drive_error(profile: str, exc: Exception) -> JSONResponse:
    kind = str(getattr(exc, "kind", "") or "")
    if kind in ("unlinked", "auth_revoked", "auth_failed", "auth_misconfigured"):
        return _drive_not_linked(
            None if kind == "unlinked"
            else "Google refused Cremind's Drive access. Re-link Google Drive (Settings → GSuite).",
            reason=kind,
        )
    if kind in ("not_found", "not_authorized"):
        return JSONResponse(
            {"error": "NotFound", "message": "No such Drive folder (or Cremind cannot see it).", "reason": kind},
            status_code=404,
        )
    logger.info(f"[userdocs] {profile}: Drive folder listing failed: {kind or type(exc).__name__}: {exc}")
    return JSONResponse(
        {
            "error": "DriveUnreachable",
            "message": "Google Drive could not be reached. Try again in a moment.",
            "reason": kind or "error",
        },
        status_code=503,
    )


def _drive_folders(profile: str, parent: Optional[str]) -> JSONResponse:
    """One level of the profile's Drive folders for the ``include_folders``
    picker: under ``parent``, or the top level (My Drive's root on a whole-Drive
    account, the granted folders on a per-file one)."""
    if parent is not None and not _DRIVE_ID_RE.match(parent):
        return JSONResponse(
            {"error": "ValidationFailed", "details": {"parent": "not a Drive folder id"}},
            status_code=400,
        )
    try:
        token = _drive_token(profile)
    except Exception:  # noqa: BLE001
        token = None
    if not token:
        return _drive_not_linked()
    try:
        client = _drive_client(profile)
    except Exception as exc:  # noqa: BLE001
        return _drive_error(profile, exc)
    try:
        parent_view = None
        if parent is not None:
            meta = client.get(parent)
            parent_view = {"id": parent, "name": str(meta.get("name") or parent)}
        rows = client.list_child_folders(parent)
    except Exception as exc:  # noqa: BLE001 — DriveError or a transport failure
        return _drive_error(profile, exc)
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    folders = [
        {"id": str(r["id"]), "name": str(r.get("name") or r["id"])}
        for r in rows or [] if isinstance(r, dict) and r.get("id")
    ]
    folders.sort(key=lambda f: (f["name"].lower(), f["id"]))
    return JSONResponse({
        "folders": folders,
        "parent": parent_view,
        "whole_drive": _is_whole_drive(token.get("scopes")),
    })


# ── engine access ──────────────────────────────────────────────────────────


async def _engine_call(fn, *, status: int = 200) -> JSONResponse:
    """Run ``fn(service)`` in a worker thread and map the engine's errors.

    The engine is synchronous (it owns SQLite files and worker threads), so
    every call leaves the event loop. ``EngineError`` carries its own HTTP
    status and error code; a missing engine means the server started without
    one (feature never allowed, or startup failed) and is reported as such.
    """
    from app.userdocs.errors import EngineError
    from app.userdocs.service import get_service

    svc = get_service()
    if svc is None:
        return JSONResponse(
            {"error": "EngineNotRunning", "message": "User Document Search is not running on this server."},
            status_code=503,
        )
    try:
        result = await asyncio.to_thread(fn, svc)
    except EngineError as exc:
        return JSONResponse(exc.to_dict(), status_code=exc.status)
    return JSONResponse(result, status_code=status)


# ── routes ─────────────────────────────────────────────────────────────────


def get_userdocs_routes() -> List[Route]:
    async def handle_admin_get(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        return JSONResponse(await asyncio.to_thread(_admin_view))

    async def handle_admin_put(request: Request) -> JSONResponse:
        denied = require_admin(request)
        if denied is not None:
            return denied
        body = await _json_body(request)
        patch = body.get("policy") if isinstance(body.get("policy"), dict) else body

        if patch.get("allowed") in (True, "true", 1, "1"):
            from app.config.settings import BaseConfig

            if not BaseConfig.is_embedding_enabled():
                return _gate_error("embedding_disabled")
            missing = await asyncio.to_thread(_feature_missing)
            if missing:
                return JSONResponse(
                    {
                        "error": "FeatureNotInstalled",
                        "missing": missing,
                        "message": (
                            "User Document Search requires optional dependencies that are "
                            "not installed: " + ", ".join(m["feature_key"] for m in missing)
                            + ". Install them via POST /api/features/install before allowing it."
                        ),
                    },
                    status_code=409,
                )

        from app.storage import get_dynamic_config_storage

        def _write() -> Dict[str, Any]:
            uds.write_admin_policy(patch, get_dynamic_config_storage())
            # Every profile's effective state may have moved (suspended ↔ live).
            for profile in {r["profile"] for r in _storage().list_sources()}:
                uds_state.notify_settings_changed(profile, uds.SOURCE_LOCAL)
            return _admin_view()

        try:
            view = await asyncio.to_thread(_write)
        except uds.PolicyValidationError as exc:
            return JSONResponse(
                {"error": "ValidationFailed", "details": exc.details}, status_code=400,
            )
        return JSONResponse(view)

    async def handle_status(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        snap = await asyncio.to_thread(uds_state.build_snapshot, _profile(request))
        return JSONResponse(snap)

    async def handle_stream(request: Request) -> Any:
        """SSE of this profile's snapshot: one on connect, one per change, and
        a keepalive every 15 s. The web UI uses it on the My Documents page;
        chat routes get the same frames through ``/api/profile-events/stream``."""
        denied = require_auth(request)
        if denied is not None:
            return denied
        from app.events.userdocs_bus import get_userdocs_stream_bus

        profile = _profile(request)
        bus = get_userdocs_stream_bus()
        queue = bus.subscribe(profile)

        async def generator():
            def _frame(payload: Dict[str, Any]) -> bytes:
                return f"event: userdocs\ndata: {json.dumps(payload, default=str)}\n\n".encode("utf-8")

            try:
                yield _frame(await asyncio.to_thread(uds_state.build_snapshot, profile))
                yield b"event: ready\ndata: {}\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        snapshot = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield _frame(snapshot)
            finally:
                bus.unsubscribe(profile, queue)

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    async def handle_settings_get(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        view = await asyncio.to_thread(_settings_view, _profile(request), is_admin(request))
        return JSONResponse(view)

    async def handle_settings_put(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        body = await _json_body(request)
        return await asyncio.to_thread(_apply_settings, _profile(request), is_admin(request), body)

    async def handle_validate_root(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        body = await _json_body(request)
        raw = None if body.get("root_mode") == uds.ROOT_INHERIT else body.get("path")
        check = await asyncio.to_thread(uds.validate_root, raw, is_admin=is_admin(request))
        return JSONResponse(check.to_dict())

    async def handle_browse(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        raw = request.query_params.get("path") or None
        hidden = request.query_params.get("hidden") in ("1", "true")
        return await asyncio.to_thread(_browse, _profile(request), is_admin(request), raw, hidden)

    # ── engine-backed routes ───────────────────────────────────────────────

    async def handle_control(request: Request) -> JSONResponse:
        """``{action: start|pause|resume|rescan|sync_now|reindex|retry_failed|
        rebuild|confirm_deletions|reject_deletions|confirm_root_change,
        targets?, reextract?, confirm?, source?}`` → 202 with the fresh
        snapshot. ``source`` (local | drive, default local) picks which
        source a rescan, sync, retry or deletion decision is about."""
        denied = require_auth(request)
        if denied is not None:
            return denied
        body = await _json_body(request)
        action = str(body.get("action") or "")
        # Only the known parameters travel on: anything else in the body
        # (a "profile", say) is dropped, never a way to aim at another profile.
        params = {k: body[k] for k in ("targets", "reextract", "confirm", "model", "source") if k in body}
        if "source" in params and params["source"] not in uds.SOURCE_KINDS:
            return JSONResponse(
                {"error": "ValidationFailed", "details": {"source": "must be 'local' or 'drive'"}},
                status_code=400,
            )
        return await _engine_call(
            lambda svc: svc.control(_profile(request), action, **params), status=202,
        )

    async def handle_files(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        q = request.query_params
        try:
            limit = max(1, min(500, int(q.get("limit") or 100)))
            after_id = int(q.get("after_id")) if q.get("after_id") else None
        except ValueError:
            return JSONResponse({"error": "ValidationFailed", "details": {"limit": "must be a number"}}, status_code=400)
        after = (q.get("after_path") or "", after_id) if after_id is not None else None
        source = (q.get("source") or "").lower() or None
        if source == "all":
            source = None
        if source is not None and source not in uds.SOURCE_KINDS:
            return JSONResponse(
                {"error": "ValidationFailed", "details": {"source": "must be local, drive or all"}},
                status_code=400,
            )
        return await _engine_call(lambda svc: svc.list_files(
            _profile(request),
            status=q.get("status") or None,
            kind=q.get("kind") or None,
            source=source,
            q=q.get("q") or None,
            after=after,
            limit=limit,
        ))

    async def handle_file_detail(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        fid = request.path_params.get("fid") or ""
        return await _engine_call(lambda svc: svc.file_detail(_profile(request), fid))

    async def handle_activity(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        q = request.query_params
        try:
            before = int(q.get("before")) if q.get("before") else None
            limit = max(1, min(200, int(q.get("limit") or 50)))
        except ValueError:
            return JSONResponse({"error": "ValidationFailed", "details": {"before": "must be a number"}}, status_code=400)
        return await _engine_call(lambda svc: svc.activity(_profile(request), before_id=before, limit=limit))

    async def handle_estimate_get(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        return await _engine_call(lambda svc: svc.get_estimate(_profile(request)))

    async def handle_estimate_post(request: Request) -> JSONResponse:
        """Start a stat-only walk that counts what a (first) sync would do.
        Runs as a background task with progress frames; poll GET for the result."""
        denied = require_auth(request)
        if denied is not None:
            return denied
        return await _engine_call(lambda svc: svc.start_estimate(_profile(request)), status=202)

    async def handle_storage(request: Request) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        return await _engine_call(lambda svc: svc.storage(_profile(request)))

    async def handle_drive_folders(request: Request) -> JSONResponse:
        """``?parent=<folder id>`` → ``{folders: [{id, name}], parent, whole_drive}``.
        Asks Google, so it runs in a worker thread."""
        denied = require_auth(request)
        if denied is not None:
            return denied
        parent = request.query_params.get("parent") or None
        return await asyncio.to_thread(_drive_folders, _profile(request), parent)

    return [
        Route("/api/userdocs/drive/folders", handle_drive_folders, methods=["GET"]),
        Route("/api/userdocs/control", handle_control, methods=["POST"]),
        Route("/api/userdocs/files", handle_files, methods=["GET"]),
        Route("/api/userdocs/files/{fid}", handle_file_detail, methods=["GET"]),
        Route("/api/userdocs/activity", handle_activity, methods=["GET"]),
        Route("/api/userdocs/estimate", handle_estimate_get, methods=["GET"]),
        Route("/api/userdocs/estimate", handle_estimate_post, methods=["POST"]),
        Route("/api/userdocs/storage", handle_storage, methods=["GET"]),
        Route("/api/userdocs/admin", handle_admin_get, methods=["GET"]),
        Route("/api/userdocs/admin", handle_admin_put, methods=["PUT"]),
        Route("/api/userdocs/status", handle_status, methods=["GET"]),
        Route("/api/userdocs/stream", handle_stream, methods=["GET"]),
        Route("/api/userdocs/settings", handle_settings_get, methods=["GET"]),
        Route("/api/userdocs/settings", handle_settings_put, methods=["PUT"]),
        Route("/api/userdocs/validate-root", handle_validate_root, methods=["POST"]),
        Route("/api/userdocs/browse", handle_browse, methods=["GET"]),
    ]
