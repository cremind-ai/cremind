"""Read-only access to the gdrive skill's OAuth token, for backend Drive calls.

The gdrive skill owns its token file (``<profile>/skills/gdrive/scripts/
.google_token.json``): it mints it during ``link`` and rewrites it on every
refresh, and its listener does the same from a separate process. This module
**never writes that file** — it reads the credentials, refreshes in memory when
the access token has expired, and lets the skill remain the single writer. Two
writers would race, and a half-written token file breaks linking for the user.

The backend needs this because the Drive settings page and the ``cremind drive``
CLI have to answer "which files can Cremind reach?" without shelling out to the
skill.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.utils.logger import logger

TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
LEGACY_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

SKILL_DIR_NAME = "gdrive"
_TOKEN_REL = Path("scripts") / ".google_token.json"
_GRANTS_REL = Path("scripts") / ".drive_grants.json"
_ENV_REL = Path("scripts") / ".env"

# Mirrors the skill's own fallback, used when cremind-connect is unreachable.
DRIVE_SCOPES_FALLBACK = ["openid", "email", DRIVE_FILE_SCOPE]

_FILE_FIELDS = "id,name,mimeType,modifiedTime,webViewLink,iconLink,size,trashed"

# In-memory access tokens keyed by profile: {token, expiry}. Refreshing without
# persisting means a backend restart re-refreshes once — cheap, and it keeps the
# token file single-writer.
_access_cache: Dict[str, Dict[str, Any]] = {}


class DriveTokenError(RuntimeError):
    pass


def skill_dir(profile: str) -> Optional[Path]:
    from app.skills.sync import profile_skills_dir

    path = profile_skills_dir(profile) / SKILL_DIR_NAME
    return path if path.is_dir() else None


def token_path(profile: str) -> Optional[Path]:
    base = skill_dir(profile)
    if base is None:
        return None
    path = base / _TOKEN_REL
    return path if path.is_file() else None


def read_token(profile: str) -> Optional[Dict[str, Any]]:
    path = token_path(profile)
    if path is None:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(f"[drive] unreadable gdrive token for profile={profile}: {exc}")
        return None


def _env_override(profile: str, key: str) -> str:
    """Read one variable out of the installed skill's ``scripts/.env``.

    That file is the skill's only config channel (the app materializes it from the
    saved variables on boot), so it is also the truest picture of what the skill
    would do on its next run.
    """
    base = skill_dir(profile)
    if base is None:
        return ""
    path = base / _ENV_REL
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


def token_client_id(profile: str) -> str:
    """The OAuth client the linked token was minted with.

    A grant attaches to the (app, user) pair, so a Picker request must name this
    client or the grant lands on one the skill's token cannot use. The skill takes
    the client from its own ``scripts/.env`` first, so this can legitimately differ
    from what the broker serves.
    """
    data = read_token(profile)
    return str((data or {}).get("client_id") or "")


def expected_scopes_or_none(profile: str) -> Optional[List[str]]:
    """What the gdrive skill would request at its next ``link``, if we can tell.

    ``None`` means cremind-connect could not be asked. That is deliberately not
    the same as "it advertises per-file": telling a user to re-link — which is
    irreversible on the shared client — because the broker was briefly down would
    be the most expensive false alarm this module can produce.
    """
    override = _env_override(profile, "GOOGLE_SCOPES")
    if override:
        return override.split()
    from app.calendar import google_discovery

    return google_discovery.resource_scopes_or_none("drive")


def expected_scopes(profile: str) -> List[str]:
    """The scopes the gdrive skill would request at its next ``link``.

    Resolved the same way the skill resolves them — env override first, then
    what cremind-connect advertises, then the built-in fallback — because any
    other answer would let this disagree with the skill's own ``status``.
    """
    return expected_scopes_or_none(profile) or list(DRIVE_SCOPES_FALLBACK)


def scopes_are_stale(
    scopes: Optional[List[str]], expected: Optional[List[str]] = None
) -> bool:
    """True when a linked account predates the per-file migration.

    Holding the whole-Drive scope is only "stale" if per-file access is what we
    would now ask for. Two cases legitimately hold the broad scope on purpose: a
    bring-your-own-credentials user who requested it, and any install made before
    cremind-connect starts advertising the narrower set. Comparing against
    ``expected`` keeps both of them out of the warning, and keeps this answer
    identical to the skill's own.

    ``expected`` of ``None`` means the advertisement could not be read, which
    proves nothing — so nothing is stale.
    """
    granted = set(scopes or [])
    if LEGACY_DRIVE_SCOPE not in granted or DRIVE_FILE_SCOPE in granted:
        return False
    if expected is None:
        return False
    return LEGACY_DRIVE_SCOPE not in set(expected)


def uses_own_client(profile: str) -> bool:
    """Whether this account was linked with the user's own OAuth client.

    Holding whole-Drive does not imply it: the shared client also requests the
    broad scope until cremind-connect is redeployed, and any account linked before
    that keeps it. Deciding by scope alone would tell a user they configured
    something they never touched.
    """
    if _env_override(profile, "GOOGLE_CLIENT_ID"):
        return True
    mine = token_client_id(profile)
    if not mine:
        return False
    try:
        from app.calendar import google_discovery

        shared = str(google_discovery.google_client().get("client_id") or "")
    except Exception as exc:  # noqa: BLE001 - unreachable broker proves nothing
        logger.debug(f"[drive] shared-client lookup failed: {exc}")
        return False
    return bool(shared) and mine != shared


def access_model(profile: str, scopes: List[str]) -> Dict[str, str]:
    """One authoritative description of what this account can reach, and why.

    Returned from here rather than phrased separately by the CLI, the settings
    page, and the API, so the three cannot describe the same account differently.
    """
    if LEGACY_DRIVE_SCOPE not in set(scopes):
        return {
            "access_model": "per-file (granted files + files Cremind created)",
            "access_note": "",
        }
    if uses_own_client(profile):
        why = "your own Google credentials"
    else:
        why = "the shared Cremind client still requests it"
    return {
        "access_model": f"whole-Drive ({why})",
        "access_note": (
            "Every file in this Drive is already reachable, so granting individual "
            "files adds nothing."
        ),
    }


def status(profile: str) -> Dict[str, Any]:
    data = read_token(profile)
    if not data:
        return {
            "linked": False, "email": None, "scopes": [],
            "expected_scopes": [], "scopes_stale": False, "expected_resolved": False,
            "whole_drive": False, "access_model": "not linked", "access_note": "",
        }
    scopes = data.get("scopes") or []
    want = expected_scopes_or_none(profile)
    return {
        "linked": True,
        "email": data.get("email"),
        "scopes": scopes,
        # Display keeps showing the fallback so the field is never empty, but the
        # staleness verdict is computed from the truthful value.
        "expected_scopes": want or list(DRIVE_SCOPES_FALLBACK),
        "expected_resolved": want is not None,
        "scopes_stale": scopes_are_stale(scopes, want),
        "whole_drive": LEGACY_DRIVE_SCOPE in set(scopes),
        **access_model(profile, scopes),
    }


def _refresh(profile: str, data: Dict[str, Any]) -> str:
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": data.get("refresh_token", ""),
        "client_id": data.get("client_id", ""),
        "client_secret": data.get("client_secret", ""),
    }
    if not payload["refresh_token"]:
        raise DriveTokenError("the linked Google account has no refresh token; re-link the gdrive skill")
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.post(TOKEN_ENDPOINT, data=payload)
            resp.raise_for_status()
            tok = resp.json()
    except httpx.HTTPStatusError as exc:
        body = exc.response.text if exc.response is not None else ""
        if "invalid_grant" in body:
            raise DriveTokenError(
                "Google rejected the stored refresh token. Re-link the gdrive skill, "
                "then grant files again."
            ) from exc
        raise DriveTokenError(f"could not refresh the Google access token: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise DriveTokenError(f"could not refresh the Google access token: {exc}") from exc
    access = tok.get("access_token")
    if not access:
        raise DriveTokenError("Google's refresh response contained no access token")
    _access_cache[profile] = {
        "token": access,
        "expiry": time.time() + int(tok.get("expires_in", 3600)),
    }
    return access


def access_token(profile: str) -> str:
    """A usable access token for ``profile``, refreshed in memory as needed."""
    cached = _access_cache.get(profile)
    if cached and time.time() < float(cached.get("expiry", 0)) - 60:
        return str(cached["token"])
    data = read_token(profile)
    if not data:
        raise DriveTokenError("the gdrive skill is not linked to a Google account")
    stored_expiry = float(data.get("expiry") or 0)
    stored = data.get("access_token")
    if stored and time.time() < stored_expiry - 60:
        return str(stored)
    return _refresh(profile, data)


def list_files(
    profile: str, *, page_token: Optional[str] = None, page_size: int = 50
) -> Dict[str, Any]:
    """Files this profile's Drive link can reach.

    Under ``drive.file`` this *is* the authoritative granted set — Google returns
    only files the user picked for this app plus files the app created — so no
    local record of grants has to be trusted.
    """
    params: Dict[str, Any] = {
        "pageSize": max(1, min(page_size, 100)),
        "orderBy": "modifiedTime desc",
        "q": "trashed = false",
        "spaces": "drive",
        # A picked file can live in a shared drive, and Google's defaults
        # (corpora=user, includeItemsFromAllDrives=false) would omit it — leaving a
        # granted file readable by id yet missing from the list that is supposed to
        # be authoritative, and invisible to the grant-round diff. Mirrors the
        # skill's own files.list call.
        "supportsAllDrives": "true",
        "includeItemsFromAllDrives": "true",
        "corpora": "allDrives",
        "fields": f"nextPageToken,files({_FILE_FIELDS})",
    }
    if page_token:
        params["pageToken"] = page_token
    token = access_token(profile)
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                _FILES_ENDPOINT, params=params, headers={"Authorization": f"Bearer {token}"}
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise DriveTokenError(f"Drive list failed: {exc}") from exc

    provenance = {entry.get("id"): entry for entry in read_grants(profile)}
    files = []
    for item in payload.get("files", []) or []:
        recorded = provenance.get(item.get("id"))
        files.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "mime_type": item.get("mimeType"),
                "modified_time": item.get("modifiedTime"),
                "web_view_link": item.get("webViewLink"),
                "size": item.get("size"),
                # "picker" when we recorded the grant, otherwise unknown: files
                # Cremind created are reachable without ever being picked, and a
                # grant made from another client leaves no local trace.
                "origin": (recorded or {}).get("source", ""),
            }
        )
    return {"files": files, "next_page_token": payload.get("nextPageToken")}


def reachable_ids(profile: str) -> set[str]:
    """Ids reachable right now, used to detect what a grant round added."""
    ids: set[str] = set()
    page: Optional[str] = None
    for _ in range(10):  # bounded: 10 pages x 100 is far beyond a sane grant set
        batch = list_files(profile, page_token=page, page_size=100)
        ids.update(f["id"] for f in batch["files"] if f.get("id"))
        page = batch.get("next_page_token")
        if not page:
            break
    return ids


def get_file(profile: str, file_id: str) -> Optional[Dict[str, Any]]:
    token = access_token(profile)
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(
                f"{_FILES_ENDPOINT}/{file_id}",
                params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code in (403, 404):
                return None
            resp.raise_for_status()
            item = resp.json()
    except DriveTokenError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[drive] get_file({file_id}) failed: {exc}")
        return None
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "mime_type": item.get("mimeType"),
        "modified_time": item.get("modifiedTime"),
        "web_view_link": item.get("webViewLink"),
    }


# ── grant provenance (best effort, never authoritative) ─────────────────────

def grants_path(profile: str) -> Optional[Path]:
    base = skill_dir(profile)
    return None if base is None else base / _GRANTS_REL


def read_grants(profile: str) -> List[Dict[str, Any]]:
    path = grants_path(profile)
    if path is None or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []


def record_grants(profile: str, files: List[Dict[str, Any]], *, source: str = "picker") -> None:
    path = grants_path(profile)
    if path is None:
        return
    existing = read_grants(profile)
    known = {e.get("id") for e in existing}
    now = time.time()
    existing.extend(
        {
            "id": f.get("id"),
            "name": f.get("name", ""),
            "mime_type": f.get("mime_type", ""),
            "granted_at": now,
            "source": source,
        }
        for f in files
        if f.get("id") and f.get("id") not in known
    )
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug(f"[drive] could not record grant provenance: {exc}")


def parse_file_reference(value: str) -> str:
    """Accept a bare id or any Drive/Docs/Sheets/Slides URL, as the skill does."""
    import re

    patterns = (
        r"/file/d/([a-zA-Z0-9-_]+)",
        r"/folders/([a-zA-Z0-9-_]+)",
        r"/document/d/([a-zA-Z0-9-_]+)",
        r"/spreadsheets/d/([a-zA-Z0-9-_]+)",
        r"/presentation/d/([a-zA-Z0-9-_]+)",
        r"[?&]id=([a-zA-Z0-9-_]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, value or "")
        if match:
            return match.group(1)
    return (value or "").strip()


def token_and_grant_paths(profile: str) -> Tuple[Optional[Path], Optional[Path]]:
    return token_path(profile), grants_path(profile)
