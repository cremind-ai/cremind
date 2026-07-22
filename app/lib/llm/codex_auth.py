"""OpenAI Codex ("Sign in with ChatGPT") OAuth protocol + token refresh.

This module owns the credential half of the Codex OAuth feature: minting the
authorization URL (PKCE S256), exchanging the authorization code, refreshing the
access token, and handing a *valid* access token + ChatGPT account id to the
runtime transport (:mod:`app.lib.llm.openai_codex`).

It is deliberately dependency-light — ``httpx`` + stdlib only, no imports from
``app.server`` / ``app.api`` — so both the API flow layer
(:mod:`app.api.llm_codex_flow`) and the transport provider can import it.

The protocol constants and quirks below match the ChatGPT Codex CLI's OAuth
flow (the OpenAI ``auth.openai.com`` client):

* the authorize query is built manually so the scope's spaces encode as ``%20``
  (not ``+``), matching the Codex CLI;
* code exchange is ``application/x-www-form-urlencoded``;
* **refresh uses a JSON body** (not form-encoded) and sends no ``scope``;
* a refresh response that omits ``refresh_token`` means "keep the old one"
  (single-use rotation), and ``invalid_grant`` / ``refresh_token_expired`` /
  ``refresh_token_reused`` / ``refresh_token_invalidated`` are unrecoverable
  (the user must sign in again);
* the ChatGPT account id lives in the ``id_token`` claim
  ``https://api.openai.com/auth.chatgpt_account_id`` and is required at request
  time (a transport header), so the flow fails loudly if it can't be extracted.

Tokens persist as per-profile ``llm_config`` rows (no migration — the table is a
key/value store). ``openai.oauth_token`` (the access token) is the *existing*
key already read by :mod:`app.lib.llm.factory`, so populating it keeps the
provider's "configured" reporting working.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from app.utils.logger import logger

# ── protocol constants (ChatGPT Codex OAuth client) ────────────────────────
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_SCOPE = "openid profile email offline_access"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"

# Refresh 5 days before expiry; also proactively re-refresh once a token is more
# than 8 days old.
REFRESH_LEAD_S = 5 * 86400
MAX_REFRESH_AGE_S = 8 * 86400

# Error codes from the token endpoint that mean the refresh token is dead — no
# amount of retrying helps, the user must re-authorize.
UNRECOVERABLE_REFRESH_ERRORS = frozenset({
    "invalid_grant",
    "refresh_token_expired",
    "refresh_token_reused",
    "refresh_token_invalidated",
})

# llm_config keys (per profile). ``oauth_token`` is the existing access-token key.
_K_ACCESS = "openai.oauth_token"
_K_REFRESH = "openai.oauth_refresh_token"
_K_EXPIRES_AT = "openai.oauth_expires_at"
_K_ACCOUNT_ID = "openai.oauth_account_id"
_K_PLAN_TYPE = "openai.oauth_plan_type"
_K_EMAIL = "openai.oauth_email"
_K_LAST_REFRESH = "openai.oauth_last_refresh_at"

_HTTP_TIMEOUT = 30.0

# Single-flight refresh locks, one per profile, so concurrent turns don't each
# spend the single-use refresh token (which would invalidate the others).
_refresh_locks: Dict[str, asyncio.Lock] = {}


class CodexAuthError(RuntimeError):
    """Transient / generic Codex auth failure (network, 5xx, malformed response)."""


class CodexReauthRequired(CodexAuthError):
    """The stored credentials are unusable — the user must sign in with ChatGPT again."""


@dataclass(frozen=True)
class CodexCredentials:
    access_token: str
    account_id: str
    plan_type: Optional[str] = None
    email: Optional[str] = None


# ── storage helpers ────────────────────────────────────────────────────────

def _kw(profile: Optional[str]) -> dict:
    """Match the factory's convention: omit ``profile`` so storage defaults apply."""
    return {"profile": profile} if profile is not None else {}


def _get(config_storage, profile: Optional[str], key: str) -> Optional[str]:
    return config_storage.get("llm_config", key, **_kw(profile))


def _set(config_storage, profile: Optional[str], key: str, value: str, *, secret: bool) -> None:
    config_storage.set("llm_config", key, value, is_secret=secret, **_kw(profile))


def _parse_float(raw: Optional[str]) -> Optional[float]:
    try:
        return float(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ── PKCE / URL / claims ────────────────────────────────────────────────────

def generate_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for a PKCE S256 exchange."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    return verifier, challenge


def generate_state() -> str:
    """Return a random ``state`` (also used as the loopback-callback filename key)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def build_authorize_url(state: str, code_challenge: str) -> str:
    """Build the ChatGPT consent URL.

    The query is assembled by hand (rather than ``urlencode``) so spaces in the
    scope encode as ``%20`` — the Codex backend is picky and rejects the ``+``
    form that ``urlencode`` would emit.
    """
    params = [
        ("response_type", "code"),
        ("client_id", CODEX_CLIENT_ID),
        ("redirect_uri", CODEX_REDIRECT_URI),
        ("scope", CODEX_SCOPE),
        ("code_challenge", code_challenge),
        ("code_challenge_method", "S256"),
        ("id_token_add_organizations", "true"),
        ("codex_cli_simplified_flow", "true"),
        ("originator", "codex_cli_rs"),
        ("state", state),
    ]
    query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
    return f"{CODEX_AUTHORIZE_URL}?{query}"


def extract_id_token_claims(id_token: Optional[str]) -> Dict[str, Optional[str]]:
    """Pull ``account_id`` / ``plan_type`` / ``email`` from an id_token JWT.

    No signature verification — the token was just fetched from OpenAI over TLS
    and is only used to read the caller's own account id. Never raises; returns
    an empty dict on any malformed input.
    """
    if not id_token:
        return {}
    try:
        payload_seg = id_token.split(".")[1]
        payload_seg += "=" * (-len(payload_seg) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload_seg))
    except Exception:  # noqa: BLE001
        return {}
    auth = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(auth, dict):
        auth = {}
    return {
        "account_id": auth.get("chatgpt_account_id") or claims.get("account_id"),
        "plan_type": auth.get("chatgpt_plan_type") or claims.get("plan_type"),
        "email": claims.get("email"),
    }


# ── token endpoint calls ───────────────────────────────────────────────────

async def exchange_code(code: str, code_verifier: str) -> Dict[str, Any]:
    """Exchange an authorization ``code`` for tokens (form-urlencoded)."""
    data = {
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "code": code,
        "redirect_uri": CODEX_REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                CODEX_TOKEN_URL, data=data, headers={"Accept": "application/json"},
            )
    except httpx.RequestError as exc:
        raise CodexAuthError(f"could not reach the OpenAI token endpoint: {exc}") from exc
    if resp.status_code >= 400:
        raise CodexAuthError(f"code exchange failed ({resp.status_code}): {resp.text[:300]}")
    try:
        tok = resp.json()
    except ValueError as exc:
        raise CodexAuthError("code exchange returned a non-JSON response") from exc
    if not tok.get("access_token"):
        raise CodexAuthError("code exchange response had no access_token")
    return tok


async def refresh_access_token(refresh_token: str) -> Dict[str, Any]:
    """Refresh an access token using a **JSON** body (Codex-specific).

    Raises :class:`CodexReauthRequired` when the token endpoint reports an
    unrecoverable error, :class:`CodexAuthError` on transient failures.
    """
    body = {
        "client_id": CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            resp = await client.post(
                CODEX_TOKEN_URL,
                json=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise CodexAuthError(f"could not reach the OpenAI token endpoint: {exc}") from exc

    if resp.status_code >= 400:
        err_code = ""
        try:
            err_code = str((resp.json() or {}).get("error") or "")
        except ValueError:
            pass
        combined = f"{err_code} {resp.text}".lower()
        if any(marker in combined for marker in UNRECOVERABLE_REFRESH_ERRORS):
            raise CodexReauthRequired(
                "ChatGPT sign-in has expired or been revoked — sign in again."
            )
        raise CodexAuthError(f"token refresh failed ({resp.status_code}): {resp.text[:300]}")

    try:
        tok = resp.json()
    except ValueError as exc:
        raise CodexAuthError("token refresh returned a non-JSON response") from exc
    if not tok.get("access_token"):
        raise CodexAuthError("token refresh response had no access_token")
    return tok


# ── persistence ────────────────────────────────────────────────────────────

def persist_token_response(
    config_storage,
    profile: Optional[str],
    tok: Dict[str, Any],
    *,
    now: Optional[float] = None,
) -> CodexCredentials:
    """Persist a token-endpoint response and return the resulting credentials.

    Handles refresh-token rotation (a response without ``refresh_token`` keeps
    the stored one) and refresh responses that omit ``id_token`` (the account
    id / plan / email are preserved from storage). Raises
    :class:`CodexAuthError` if no ChatGPT account id can be resolved — it is a
    required request header and a session without it is unusable.
    """
    now = time.time() if now is None else now

    access = tok.get("access_token")
    if not access:
        raise CodexAuthError("token response had no access_token")

    # Rotation: keep the previous refresh token when the response omits one.
    refresh = tok.get("refresh_token") or _get(config_storage, profile, _K_REFRESH)

    claims = extract_id_token_claims(tok.get("id_token"))
    account_id = claims.get("account_id") or _get(config_storage, profile, _K_ACCOUNT_ID)
    if not account_id:
        raise CodexAuthError(
            "could not determine the ChatGPT account id from the sign-in response"
        )
    plan_type = claims.get("plan_type") or _get(config_storage, profile, _K_PLAN_TYPE) or ""
    email = claims.get("email") or _get(config_storage, profile, _K_EMAIL) or ""

    expires_in = int(tok.get("expires_in") or 3600)
    expires_at = now + expires_in

    _set(config_storage, profile, _K_ACCESS, access, secret=True)
    if refresh:
        _set(config_storage, profile, _K_REFRESH, refresh, secret=True)
    _set(config_storage, profile, _K_EXPIRES_AT, str(int(expires_at)), secret=False)
    _set(config_storage, profile, _K_ACCOUNT_ID, str(account_id), secret=False)
    _set(config_storage, profile, _K_PLAN_TYPE, plan_type, secret=False)
    _set(config_storage, profile, _K_EMAIL, email, secret=False)
    _set(config_storage, profile, _K_LAST_REFRESH, str(int(now)), secret=False)

    return CodexCredentials(
        access_token=access,
        account_id=str(account_id),
        plan_type=plan_type or None,
        email=email or None,
    )


def _read_creds(config_storage, profile: Optional[str], access: str, account_id: str) -> CodexCredentials:
    return CodexCredentials(
        access_token=access,
        account_id=account_id,
        plan_type=(_get(config_storage, profile, _K_PLAN_TYPE) or None),
        email=(_get(config_storage, profile, _K_EMAIL) or None),
    )


def _needs_refresh(expires_at: Optional[float], last_refresh: Optional[float], now: float) -> bool:
    if expires_at is None or last_refresh is None:
        return True
    return (expires_at - now < REFRESH_LEAD_S) or (now - last_refresh > MAX_REFRESH_AGE_S)


async def get_valid_access_token(config_storage, profile: Optional[str]) -> CodexCredentials:
    """Return a valid access token + account id, refreshing when needed.

    This is the contract consumed by the transport provider. Raises
    :class:`CodexReauthRequired` when the user must sign in again (missing
    tokens, legacy pasted access token with no refresh token, or an
    unrecoverable refresh error).
    """
    access = _get(config_storage, profile, _K_ACCESS)
    if not access:
        raise CodexReauthRequired("OpenAI Codex is not signed in.")
    refresh = _get(config_storage, profile, _K_REFRESH)
    account_id = _get(config_storage, profile, _K_ACCOUNT_ID)
    if not refresh or not account_id:
        # Legacy: a raw access token was pasted before the OAuth flow existed —
        # there's no refresh token / account id to work with.
        raise CodexReauthRequired(
            "OpenAI Codex sign-in is incomplete (no refresh token) — sign in with ChatGPT again."
        )

    now = time.time()
    if not _needs_refresh(
        _parse_float(_get(config_storage, profile, _K_EXPIRES_AT)),
        _parse_float(_get(config_storage, profile, _K_LAST_REFRESH)),
        now,
    ):
        return _read_creds(config_storage, profile, access, account_id)

    lock = _refresh_locks.setdefault(profile or "admin", asyncio.Lock())
    async with lock:
        # Re-read under the lock: a concurrent turn may have refreshed already.
        access = _get(config_storage, profile, _K_ACCESS) or access
        refresh = _get(config_storage, profile, _K_REFRESH) or refresh
        account_id = _get(config_storage, profile, _K_ACCOUNT_ID) or account_id
        expires_at = _parse_float(_get(config_storage, profile, _K_EXPIRES_AT))
        last_refresh = _parse_float(_get(config_storage, profile, _K_LAST_REFRESH))
        now = time.time()
        if not _needs_refresh(expires_at, last_refresh, now):
            return _read_creds(config_storage, profile, access, account_id)

        try:
            tok = await refresh_access_token(refresh)
        except CodexReauthRequired:
            raise
        except CodexAuthError:
            # Transient: if the current token is still comfortably valid, keep
            # using it rather than failing the request.
            if expires_at is not None and expires_at > now + 60:
                logger.warning("[codex_auth] transient refresh failure; using still-valid token")
                return _read_creds(config_storage, profile, access, account_id)
            raise
        return persist_token_response(config_storage, profile, tok, now=now)
