import asyncio
import base64
import json
import os
import time
import urllib.parse
import webbrowser

import httpx
from a2a.types import AgentCard

from app.utils.logger import logger
from app.utils.client_storage import get_client_storage


class OAuthClient:
    def __init__(self, agent_card: AgentCard, agent_name: str, profile: str = "default"):
        self.agent_card = agent_card
        self.profile = profile
        self.agent_name = agent_name
        self.storage = get_client_storage()
        self.token = None
        self.state = None  # Store state for CSRF validation

    def get_token(self, profile: str | None = None) -> str:
        """Get token for the current profile or a specified profile.
        
        Args:
            profile: Optional profile override. If None, uses self.profile
            
        Returns:
            The token string, or empty string if not found
        """
        target_profile = profile if profile is not None else self.profile
        
        # If requesting current profile and we have cached token, return it
        if target_profile == self.profile and self.token:
            return self.token

        # Load from storage backend
        token = self.storage.get_token(self.agent_name, target_profile)
        
        # Cache token if it's for current profile
        if target_profile == self.profile:
            self.token = token
            
        return token

    def _find_oauth_flow(self):
        # Look for OAuth2 security scheme in agent card
        if not self.agent_card.security_schemes:
            return None

        for scheme_name, scheme in self.agent_card.security_schemes.items():
            if scheme.root.type == 'oauth2':
                flows = scheme.root.flows
                if flows.authorization_code:
                    return flows.authorization_code
        return None

    def _decode_jwt_exp(self, token: str) -> int | None:
        """Decode JWT token and extract the 'exp' claim.
        
        Args:
            token: The JWT token string
            
        Returns:
            The exp timestamp (seconds since epoch) or None if unable to decode
        """
        try:
            # JWT structure: header.payload.signature
            parts = token.split('.')
            if len(parts) != 3:
                return None

            # Decode the payload (middle part)
            # Add padding if needed (JWT base64 doesn't include padding)
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

    def get_auth_status(self, profile: str | None = None) -> str:
        """Get the authentication status of this agent for a specific profile.
        
        Args:
            profile: Optional profile override. If None, uses self.profile
        
        Returns:
            One of:
            - "not_supported": Agent does not support OAuth authentication
            - "authenticated": Agent has a valid, non-expired token
            - "expired": Agent has a token but it has expired
            - "not_authenticated": Agent supports OAuth but has no token
        """
        # Check if OAuth is supported
        flow_config = self._find_oauth_flow()
        if not flow_config:
            return "not_supported"
        
        # Check if we have a token for the specified profile
        token = self.get_token(profile)
        if not token:
            return "not_authenticated"
        
        # Check if token is expired
        exp_timestamp = self._decode_jwt_exp(token)
        if exp_timestamp:
            current_time = int(time.time())
            if exp_timestamp <= current_time:
                return "expired"
        
        return "authenticated"

    def get_auth_url(self, redirect_uri: str, profile: str = "default", source: str = "api") -> str | None:
        """Construct the OAuth authorization URL for server-redirect flow.

        Args:
            redirect_uri: The callback URL where the OAuth provider will redirect
            profile: The profile name to encode in the state parameter
            source: The source of the auth request ('api' or 'chat') to determine post-auth behavior

        Returns:
            The authorization URL to redirect the user to, or None if OAuth is not supported
        """
        flow_config = self._find_oauth_flow()
        if not flow_config:
            return None

        # Generate and store state for CSRF protection, encoding profile and source into it
        # Format: {random_token}:{profile}:{source}
        random_token = base64.urlsafe_b64encode(os.urandom(16)).decode()
        state_data = f"{random_token}:{profile}:{source}"
        self.state = base64.urlsafe_b64encode(state_data.encode()).decode()
        
        params = {
            "client_id": "cremind-agent-client",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": self.state
        }
        
        auth_endpoint = flow_config.authorization_url
        auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"
        
        return auth_url

    async def handle_oauth_callback(self, code: str, redirect_uri: str, state: str | None = None, profile: str | None = None) -> bool:
        """Handle the OAuth callback by exchanging code for token.
        
        Args:
            code: The authorization code from the OAuth provider
            redirect_uri: The redirect URI used in the initial auth request
            state: The state parameter from OAuth callback (contains encoded profile)
            profile: Optional profile override. If None, extracts from state or uses self.profile
            
        Returns:
            True if authentication succeeded, False otherwise
        """
        # Extract profile from state if provided
        # State format: {random_token}:{profile}:{source}
        if state and profile is None:
            try:
                decoded_state = base64.urlsafe_b64decode(state).decode()
                parts = decoded_state.split(':')
                if len(parts) >= 2:
                    profile = parts[1]
            except Exception as e:
                logger.warning(f"Failed to decode profile from state: {e}")
        
        target_profile = profile if profile is not None else self.profile
        
        flow_config = self._find_oauth_flow()
        if not flow_config:
            logger.error(f"No OAuth flow found for {self.agent_name}")
            return False
        
        try:
            # Exchange code for token using token endpoint from Agent Card
            token_endpoint = flow_config.token_url
            async with httpx.AsyncClient() as client:
                resp = await client.post(token_endpoint, data={
                    "code": code,
                    "client_id": "cremind-agent-client",
                    "grant_type": "authorization_code",
                    "redirect_uri": redirect_uri
                })
                resp.raise_for_status()
                data = resp.json()
                token = data.get("access_token")
                if token:
                    self.storage.save_token(self.agent_name, target_profile, token)
                    # Update cached token if for current profile
                    if target_profile == self.profile:
                        self.token = token
                    logger.info(f"Authentication successful for {self.agent_name} (profile: {target_profile}) & token saved.")
                    return True
                else:
                    logger.error("No access_token in response")
                    return False
        except Exception as e:
            logger.error(f"OAuth callback failed for {self.agent_name}: {e}")
            return False

    def unlink_token(self, profile: str | None = None) -> bool:
        """Remove the stored authentication token for this agent and profile.
        
        Args:
            profile: Optional profile override. If None, uses self.profile
        
        Returns:
            True if token was successfully unlinked, False otherwise
        """
        target_profile = profile if profile is not None else self.profile
        
        try:
            success = self.storage.delete_token(self.agent_name, target_profile)
            if success:
                # Clear cached token if it's for current profile
                if target_profile == self.profile:
                    self.token = None
                logger.info(f"Authentication token unlinked for {self.agent_name} (profile: {target_profile})")
            return success
        except Exception as e:
            logger.error(f"Failed to unlink token for {self.agent_name} (profile: {target_profile}): {e}")
            return False

    def get_expiration_info(self, profile: str | None = None) -> dict:
        """Get human-readable expiration information for the token.
        
        Args:
            profile: Optional profile override. If None, uses self.profile
        
        Returns:
            Dictionary with 'timestamp', 'formatted', and 'relative' time strings,
            or None if no token or no expiration claim
        """
        token = self.get_token(profile)
        if not token:
            return None
        
        exp_timestamp = self._decode_jwt_exp(token)
        if not exp_timestamp:
            return None
        
        from datetime import datetime, timezone
        
        exp_datetime = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc)
        current_datetime = datetime.now(timezone.utc)
        
        # Calculate time difference
        time_diff = exp_datetime - current_datetime
        total_seconds = int(time_diff.total_seconds())
        
        # Format as relative time
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
        
        # Format as absolute time (local timezone)
        exp_local = exp_datetime.astimezone()
        formatted = exp_local.strftime("%B %d, %Y at %I:%M %p")
        
        return {
            'timestamp': exp_timestamp,
            'formatted': formatted,
            'relative': relative
        }

    async def authenticate(self):
        flow_config = self._find_oauth_flow()
        if not flow_config:
            logger.info("No OAuth2 authorization code flow found in Agent Card.")
            return

        logger.info(f"Initiating Authentication for {self.agent_name}...")

        # The consent redirect is captured by the always-running backend at
        # GET /api/oauth/a2a/callback (app/api/oauth_callback.py), which resolves
        # the in-process Future we register here keyed by ``state``. The backend
        # is reachable at localhost:<PORT> from wherever the consent browser runs
        # (the same host/pod as this process), so no auxiliary published port is
        # needed. The external provider must have this redirect URL registered.
        from app.config.settings import BaseConfig
        from app.tools.a2a.oauth_rendezvous import cancel, register

        callback_uri = f"http://localhost:{BaseConfig.PORT}/api/oauth/a2a/callback"

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        state = base64.urlsafe_b64encode(os.urandom(16)).decode()
        self.state = state  # for CSRF validation (the route matches by state)
        register(state, future)

        try:
            params = {
                "client_id": "cremind-agent-client",  # usually provider-dependent
                "redirect_uri": callback_uri,
                "response_type": "code",
                "state": state,
            }

            # Use authorization endpoint from Agent Card
            auth_endpoint = flow_config.authorization_url
            auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"

            logger.info(f"Opening browser: {auth_url}")
            webbrowser.open(auth_url)

            logger.info(f"Waiting for callback from {self.agent_name}...")
            # Wait for the backend route to resolve the Future (5 minutes).
            code = await asyncio.wait_for(future, timeout=300)

            if not code:
                raise Exception("Authentication failed: No code received")

            # Exchange code for token using token endpoint from Agent Card
            token_endpoint = flow_config.token_url
            async with httpx.AsyncClient() as client:
                resp = await client.post(token_endpoint, data={
                    "code": code,
                    "client_id": "cremind-agent-client",  # Should match the one sent in auth url
                    "grant_type": "authorization_code",
                    "redirect_uri": callback_uri
                })
                resp.raise_for_status()
                data = resp.json()
                self.token = data.get("access_token")
                if self.token:
                    self.storage.save_token(self.agent_name, self.profile, self.token)
                    logger.info(f"Authentication successful for {self.agent_name} & token saved.")
                else:
                    logger.error("No access_token in response")

        except asyncio.TimeoutError:
            logger.error("Authentication timed out.")
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
        finally:
            cancel(state)
