"""MCP OAuth client for servers that support authentication.

Discovers OAuth metadata via .well-known/oauth-authorization-server
and provides the same interface as the A2A OAuthClient.
"""

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from typing import Any, Dict, Optional

import httpx

from app.utils.client_storage import (
    get_auth_client_storage,
    ACCESS_TOKEN, REFRESH_TOKEN, DCR_CLIENT_ID, DCR_CLIENT_SECRET, SERVER_PROFILE,
)
from app.utils.logger import logger


class MCPOAuthClient:
    """OAuth client for MCP servers.

    Supports two modes:
    1. Discovery via .well-known/oauth-authorization-server (HTTP MCP servers)
    2. Direct configuration with explicit OAuth endpoints (stdio MCP servers, e.g. Google)
    """

    def __init__(self, server_url: str, server_name: str, profile: str = "default",
                 client_id: Optional[str] = None, client_secret: Optional[str] = None,
                 extra_authorize_params: Optional[Dict[str, str]] = None):
        self.server_url = server_url
        self.server_name = server_name
        self.profile = profile
        self.storage = get_auth_client_storage()
        self.token = None
        self.state = None
        self._auth_metadata: Optional[Dict[str, Any]] = None
        self._client_id = client_id or "cremind-agent-client"
        self._client_secret = client_secret
        self._extra_authorize_params = extra_authorize_params or {}
        self._pkce_verifiers: Dict[str, str] = {}  # state -> code_verifier
        # Stable storage key derived from URL (never changes, unlike server_name)
        self._storage_key = self._url_to_storage_key(server_url)

    @staticmethod
    def _url_to_storage_key(url: str) -> str:
        """Derive a stable, filesystem-safe storage key from a URL."""
        parsed = urllib.parse.urlparse(url)
        host = parsed.hostname or "unknown"
        port = parsed.port
        path = parsed.path.strip("/").replace("/", "_") if parsed.path.strip("/") else ""
        key = f"{parsed.scheme}_{host}"
        if port:
            key += f"_{port}"
        if path:
            key += f"_{path}"
        return key
        self._pkce_verifiers: Dict[str, str] = {}  # state -> code_verifier

    def set_auth_metadata(self, metadata: Dict[str, Any]) -> None:
        """Set OAuth metadata directly (for stdio servers with known OAuth endpoints).

        Args:
            metadata: Dict with 'authorization_endpoint', 'token_endpoint',
                      and optionally 'scopes_supported'
        """
        self._auth_metadata = metadata
        logger.info(
            f"Set OAuth metadata for MCP server '{self.server_name}': "
            f"authorization_endpoint={metadata.get('authorization_endpoint')}"
        )

    async def discover_auth_metadata(self) -> bool:
        """Fetch .well-known/oauth-authorization-server from the MCP server.

        Returns:
            True if auth metadata was discovered, False otherwise
        """
        url = f"{self.server_url.rstrip('/')}/.well-known/oauth-authorization-server"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    self._auth_metadata = resp.json()
                    logger.info(
                        f"Discovered OAuth metadata for MCP server '{self.server_name}': "
                        f"issuer={self._auth_metadata.get('issuer')}"
                    )
                    return True
                else:
                    logger.info(
                        f"No OAuth metadata at {url} (status {resp.status_code}). "
                        f"MCP server '{self.server_name}' does not support authentication."
                    )
                    return False
        except Exception as e:
            logger.info(f"Could not discover OAuth metadata for '{self.server_name}': {e}")
            return False

    async def register_client(self, redirect_uri: str) -> bool:
        """Register this client via Dynamic Client Registration (RFC 7591).

        Must be called after discover_auth_metadata() if the server has a
        registration_endpoint. Sets self._client_id and self._client_secret
        from the server's response.

        Returns:
            True if registration succeeded, False otherwise
        """
        if not self._auth_metadata:
            return False

        registration_endpoint = self._auth_metadata.get("registration_endpoint")
        if not registration_endpoint:
            logger.info(f"No registration_endpoint for '{self.server_name}', using existing client_id")
            return True  # No DCR needed

        # Check if we already have stored client credentials for this server
        stored_client_id = self.storage.get_token(
            self._storage_key, SERVER_PROFILE, agent_type="mcp", token_kind=DCR_CLIENT_ID
        )
        stored_client_secret = self.storage.get_token(
            self._storage_key, SERVER_PROFILE, agent_type="mcp", token_kind=DCR_CLIENT_SECRET
        )
        if stored_client_id:
            self._client_id = stored_client_id
            self._client_secret = stored_client_secret or self._client_secret
            logger.info(f"Using stored DCR credentials for '{self.server_name}': client_id={self._client_id}")
            return True

        # Build registration request per RFC 7591
        scopes = self._auth_metadata.get("scopes_supported", [])
        client_metadata = {
            "client_name": "Cremind Agent",
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "client_secret_post",
        }
        if scopes:
            client_metadata["scope"] = " ".join(scopes)

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(registration_endpoint, json=client_metadata)
                if resp.status_code in (200, 201):
                    data = resp.json()
                    new_client_id = data.get("client_id")
                    new_client_secret = data.get("client_secret")
                    if new_client_id:
                        self._client_id = new_client_id
                        self._client_secret = new_client_secret or self._client_secret
                        # Persist DCR credentials (server-level, not per-profile)
                        self.storage.save_token(
                            self._storage_key, SERVER_PROFILE,
                            self._client_id, agent_type="mcp", token_kind=DCR_CLIENT_ID
                        )
                        if new_client_secret:
                            self.storage.save_token(
                                self._storage_key, SERVER_PROFILE,
                                new_client_secret, agent_type="mcp", token_kind=DCR_CLIENT_SECRET
                            )
                        logger.info(
                            f"DCR successful for '{self.server_name}': "
                            f"client_id={self._client_id}"
                        )
                        return True
                    else:
                        logger.error(f"DCR response missing client_id for '{self.server_name}'")
                        return False
                else:
                    logger.error(
                        f"DCR failed for '{self.server_name}': "
                        f"status={resp.status_code}, body={resp.text}"
                    )
                    return False
        except Exception as e:
            logger.error(f"DCR request failed for '{self.server_name}': {e}")
            return False

    def _decode_jwt_exp(self, token: str) -> Optional[int]:
        """Decode JWT token and extract the 'exp' claim."""
        try:
            parts = token.split('.')
            if len(parts) != 3:
                return None
            payload = parts[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += '=' * padding
            decoded_bytes = base64.urlsafe_b64decode(payload)
            payload_dict = json.loads(decoded_bytes)
            return payload_dict.get('exp')
        except Exception as e:
            logger.warning(f"Failed to decode JWT exp claim: {e}")
            return None

    def get_token(self, profile: Optional[str] = None) -> str:
        """Get token for the current profile or a specified profile."""
        target_profile = profile if profile is not None else self.profile
        if target_profile == self.profile and self.token:
            return self.token
        token = self.storage.get_token(self._storage_key, target_profile,
                                       agent_type="mcp", token_kind=ACCESS_TOKEN)
        if target_profile == self.profile:
            self.token = token
        return token

    def get_auth_status(self, profile: Optional[str] = None) -> str:
        """Get the authentication status for this MCP server.

        Returns:
            One of: "not_supported", "authenticated", "expired", "not_authenticated"
        """
        if not self._auth_metadata:
            return "not_supported"

        token = self.get_token(profile)
        if not token:
            return "not_authenticated"

        # Google access tokens are opaque (not JWT), so we can't decode exp.
        # If token exists, consider it authenticated. Actual expiration is
        # handled by refresh_access_token() when a 401 is encountered.
        exp_timestamp = self._decode_jwt_exp(token)
        if exp_timestamp:
            current_time = int(time.time())
            if exp_timestamp <= current_time:
                return "expired"

        return "authenticated"

    async def get_auth_url(self, redirect_uri: str, profile: str = "default", source: str = "api") -> Optional[str]:
        """Construct the OAuth authorization URL from discovered metadata.

        Automatically performs Dynamic Client Registration if the server
        has a registration_endpoint and we don't have credentials yet.

        Args:
            redirect_uri: The callback URL where the OAuth provider will redirect
            profile: The profile name to encode in the state parameter
            source: The source of the auth request ('api' or 'chat')

        Returns:
            The authorization URL, or None if auth is not supported
        """
        if not self._auth_metadata:
            return None

        authorization_endpoint = self._auth_metadata.get("authorization_endpoint")
        if not authorization_endpoint:
            return None

        # Dynamic Client Registration if needed
        if self._auth_metadata.get("registration_endpoint"):
            registered = await self.register_client(redirect_uri)
            if not registered:
                logger.error(f"DCR failed for '{self.server_name}', cannot get auth URL")
                return None

        # Generate state for CSRF protection with profile, source, and agent name encoded
        # Format: {random_token}:{profile}:{source}:{agent_name}
        random_token = base64.urlsafe_b64encode(os.urandom(16)).decode()
        state_data = f"{random_token}:{profile}:{source}:{self.server_name}"
        self.state = base64.urlsafe_b64encode(state_data.encode()).decode()

        scopes = self._auth_metadata.get("scopes_supported", [])

        # Generate PKCE code_verifier and code_challenge (S256)
        code_verifier = secrets.token_urlsafe(48)
        challenge_bytes = hashlib.sha256(code_verifier.encode()).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode().rstrip("=")
        self._pkce_verifiers[self.state] = code_verifier

        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": self.state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }

        if scopes:
            params["scope"] = " ".join(scopes)

        # Add extra authorize params (e.g., access_type=offline for Google)
        if self._extra_authorize_params:
            params.update(self._extra_authorize_params)

        auth_url = f"{authorization_endpoint}?{urllib.parse.urlencode(params)}"
        return auth_url

    async def handle_oauth_callback(
        self, code: str, redirect_uri: str,
        state: Optional[str] = None, profile: Optional[str] = None
    ) -> bool:
        """Handle OAuth callback by exchanging code for token.

        Args:
            code: The authorization code
            redirect_uri: The redirect URI used in the initial auth request
            state: The state parameter from OAuth callback
            profile: Optional profile override

        Returns:
            True if authentication succeeded, False otherwise
        """
        if state and profile is None:
            try:
                decoded_state = base64.urlsafe_b64decode(state).decode()
                parts = decoded_state.split(':')
                if len(parts) >= 2:
                    profile = parts[1]
            except Exception as e:
                logger.warning(f"Failed to decode profile from state: {e}")

        target_profile = profile if profile is not None else self.profile

        if not self._auth_metadata:
            logger.error(f"No OAuth metadata for MCP server '{self.server_name}'")
            return False

        token_endpoint = self._auth_metadata.get("token_endpoint")
        if not token_endpoint:
            logger.error(f"No token_endpoint in OAuth metadata for '{self.server_name}'")
            return False

        try:
            token_data = {
                "code": code,
                "client_id": self._client_id,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            }

            # Include PKCE code_verifier if we have one for this state
            if state and state in self._pkce_verifiers:
                token_data["code_verifier"] = self._pkce_verifiers.pop(state)

            # Include client_secret if configured (required for Google OAuth)
            if self._client_secret:
                token_data["client_secret"] = self._client_secret

            async with httpx.AsyncClient() as client:
                resp = await client.post(token_endpoint, data=token_data)
                resp.raise_for_status()
                data = resp.json()
                token = data.get("access_token")
                refresh_token = data.get("refresh_token")

                if token:
                    self.storage.save_token(self._storage_key, target_profile, token,
                                            agent_type="mcp", token_kind=ACCESS_TOKEN)
                    if refresh_token:
                        self.storage.save_token(
                            self._storage_key, target_profile,
                            refresh_token, agent_type="mcp", token_kind=REFRESH_TOKEN
                        )
                    if target_profile == self.profile:
                        self.token = token
                    logger.info(
                        f"MCP OAuth successful for '{self.server_name}' "
                        f"(profile: {target_profile}) & token saved."
                    )
                    return True
                else:
                    logger.error("No access_token in MCP OAuth response")
                    return False
        except Exception as e:
            logger.error(f"MCP OAuth callback failed for '{self.server_name}': {e}")
            return False

    async def refresh_access_token(self, profile: Optional[str] = None) -> bool:
        """Refresh the access token using stored refresh token.

        Returns:
            True if token was refreshed, False otherwise
        """
        target_profile = profile if profile is not None else self.profile

        if not self._auth_metadata:
            return False

        token_endpoint = self._auth_metadata.get("token_endpoint")
        if not token_endpoint:
            return False

        refresh_token = self.storage.get_token(
            self._storage_key, target_profile, agent_type="mcp", token_kind=REFRESH_TOKEN
        )
        if not refresh_token:
            logger.warning(f"No refresh token for '{self.server_name}' (profile: {target_profile})")
            return False

        try:
            token_data = {
                "client_id": self._client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            if self._client_secret:
                token_data["client_secret"] = self._client_secret

            async with httpx.AsyncClient() as client:
                resp = await client.post(token_endpoint, data=token_data)
                resp.raise_for_status()
                data = resp.json()
                new_token = data.get("access_token")
                if new_token:
                    self.storage.save_token(self._storage_key, target_profile, new_token,
                                            agent_type="mcp", token_kind=ACCESS_TOKEN)
                    # Update refresh token if a new one is provided
                    new_refresh = data.get("refresh_token")
                    if new_refresh:
                        self.storage.save_token(
                            self._storage_key, target_profile,
                            new_refresh, agent_type="mcp", token_kind=REFRESH_TOKEN
                        )
                    if target_profile == self.profile:
                        self.token = new_token
                    logger.info(f"Token refreshed for '{self.server_name}' (profile: {target_profile})")
                    return True
                return False
        except Exception as e:
            logger.error(f"Token refresh failed for '{self.server_name}': {e}")
            return False

    def unlink_token(self, profile: Optional[str] = None) -> bool:
        """Remove the stored authentication token and refresh token."""
        target_profile = profile if profile is not None else self.profile
        try:
            success = self.storage.delete_token(self._storage_key, target_profile,
                                                agent_type="mcp", token_kind=ACCESS_TOKEN)
            # Also delete refresh token
            self.storage.delete_token(self._storage_key, target_profile,
                                      agent_type="mcp", token_kind=REFRESH_TOKEN)
            if success and target_profile == self.profile:
                self.token = None
                logger.info(f"MCP token unlinked for '{self.server_name}' (profile: {target_profile})")
            return success
        except Exception as e:
            logger.error(f"Failed to unlink MCP token for '{self.server_name}': {e}")
            return False

    def get_expiration_info(self, profile: Optional[str] = None) -> Optional[Dict]:
        """Get human-readable expiration information for the token."""
        token = self.get_token(profile)
        if not token:
            return None

        exp_timestamp = self._decode_jwt_exp(token)
        if not exp_timestamp:
            return None

        from datetime import datetime, timezone

        exp_datetime = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
        current_datetime = datetime.now(timezone.utc)

        time_diff = exp_datetime - current_datetime
        total_seconds = int(time_diff.total_seconds())

        if total_seconds <= 0:
            relative = "Expired"
        elif total_seconds < 60:
            relative = f"Expires in {total_seconds} second{'s' if total_seconds != 1 else ''}"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            relative = f"Expires in {minutes} minute{'s' if minutes != 1 else ''}"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            relative = f"Expires in {hours} hour{'s' if hours != 1 else ''}"
        else:
            days = total_seconds // 86400
            relative = f"Expires in {days} day{'s' if days != 1 else ''}"

        exp_local = exp_datetime.astimezone()
        formatted = exp_local.strftime("%B %d, %Y at %I:%M %p")

        return {
            'timestamp': exp_timestamp,
            'formatted': formatted,
            'relative': relative,
        }


class NoOpOAuthClient:
    """No-op OAuth client for MCP servers that don't support authentication."""

    def __init__(self, server_name: str = ""):
        self.server_name = server_name
        self.profile = "default"
        self.token = None
        self.state = None

    def get_token(self, profile: Optional[str] = None) -> str:
        return ""

    def get_auth_status(self, profile: Optional[str] = None) -> str:
        return "not_supported"

    def get_auth_url(self, redirect_uri: str, profile: str = "default", source: str = "api") -> Optional[str]:
        return None

    async def handle_oauth_callback(self, code: str, redirect_uri: str,
                                    state: Optional[str] = None, profile: Optional[str] = None) -> bool:
        return False

    def unlink_token(self, profile: Optional[str] = None) -> bool:
        return False

    def get_expiration_info(self, profile: Optional[str] = None) -> Optional[Dict]:
        return None
