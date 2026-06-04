from collections.abc import Callable
from typing import AsyncGenerator

import httpx
from a2a.client import A2AClient
from a2a.types import (
    AgentCard,
    SendMessageRequest,
    SendStreamingMessageRequest,
    SendMessageResponse,
    SendStreamingMessageResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
)
from dotenv import load_dotenv

from app.config.settings import BaseConfig
from app.tools.a2a.auth import OAuthClient
from app.utils.logger import logger

load_dotenv()

TaskCallbackArg = Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent
TaskUpdateCallback = Callable[[TaskCallbackArg, AgentCard], Task]


class RemoteAgentConnections:
    """A class to hold the connections to the remote agents with per-profile HTTP client isolation."""

    def __init__(self, agent_card: AgentCard, agent_url: str):
        self.card = agent_card
        self.agent_url = agent_url

        # Cache for per-profile OAuth clients
        self._profile_oauth_clients: dict[str, OAuthClient] = {}

        # Cache for per-profile A2A clients (each with its own httpx client and auth header)
        self._profile_clients: dict[str, A2AClient] = {}

        # Initialize client for configured profile if one is set
        profile = BaseConfig.PROFILE
        if profile:
            self.oauth_client = OAuthClient(agent_card, agent_card.name, profile=profile)
            self._profile_oauth_clients[profile] = self.oauth_client
            self._create_client_for_profile(profile)
        else:
            self.oauth_client = None

    def get_oauth_client_for_profile(self, profile: str) -> OAuthClient:
        """Get or create an OAuth client for a specific profile.
        
        Args:
            profile: The profile name
            
        Returns:
            OAuthClient instance for the specified profile
        """
        if profile not in self._profile_oauth_clients:
            self._profile_oauth_clients[profile] = OAuthClient(
                self.card, self.card.name, profile=profile
            )
        return self._profile_oauth_clients[profile]

    def _create_client_for_profile(self, profile: str) -> A2AClient:
        """Create a new A2A client for a specific profile with its own httpx client.
        
        Args:
            profile: The profile name
            
        Returns:
            A2AClient instance for the specified profile
        """
        # Create a new httpx client for this profile
        httpx_client = httpx.AsyncClient(timeout=30)
        
        # Get OAuth client for this profile and load its token
        oauth_client = self.get_oauth_client_for_profile(profile)
        logger.info(f"Loading token for profile '{profile}'")
        token = oauth_client.get_token()
        logger.info(f"Token for profile '{profile}': {'found' if token else 'not found'}")
        
        # Set Authorization header if token exists
        if token:
            httpx_client.headers["Authorization"] = f"Bearer {token}"
        
        # Create and cache A2A client
        client = A2AClient(httpx_client, self.card, url=self.agent_url)
        self._profile_clients[profile] = client
        
        return client

    def get_client_for_profile(self, profile: str) -> A2AClient:
        """Get or create an A2A client for a specific profile.
        
        Each profile gets its own httpx.AsyncClient with independent Authorization header.
        
        Args:
            profile: The profile name
            
        Returns:
            A2AClient instance for the specified profile
        """
        if profile not in self._profile_clients:
            return self._create_client_for_profile(profile)
        return self._profile_clients[profile]

    async def authenticate(self):
        """Perform authentication if no token exists.

        Loads existing tokens for the configured profile; auth flows are
        initiated by the app UI via the agents API, not by this method.
        """
        profile = BaseConfig.PROFILE
        if not profile:
            return
        oauth_client = self.get_oauth_client_for_profile(profile)
        token = oauth_client.get_token()
        
        if token and profile in self._profile_clients:
            # Update the httpx client's Authorization header for default profile
            client = self._profile_clients[profile]
            # Access the underlying httpx client from A2AClient
            if hasattr(client, '_client'):
                client._client.headers["Authorization"] = f"Bearer {token}"

    def update_auth_header(self, profile: str | None = None):
        """Update the Authorization header with the token for a specific profile.
        
        Args:
            profile: Optional profile name. If None, uses the default profile
        """
        if profile is None:
            profile = BaseConfig.PROFILE
            
        # Get or create client for this profile
        if profile not in self._profile_clients:
            self._create_client_for_profile(profile)
            return
            
        # Get OAuth client and token for this profile
        oauth_client = self.get_oauth_client_for_profile(profile)
        token = oauth_client.get_token()
        
        # Update the profile's httpx client Authorization header
        client = self._profile_clients[profile]
        if hasattr(client, '_client'):
            if token:
                client._client.headers["Authorization"] = f"Bearer {token}"
            else:
                # Remove Authorization header if no token
                client._client.headers.pop("Authorization", None)

    def update_auth_header_for_profile(self, profile: str):
        """Update the Authorization header to use a specific profile's token.
        
        Args:
            profile: The profile name to use for authentication
        """
        self.update_auth_header(profile)

    def get_agent(self) -> AgentCard:
        return self.card

    async def send_message(
        self, message_request: SendMessageRequest, profile: str
    ) -> SendMessageResponse:
        """Send a message using the A2A client for the specified profile.
        
        Args:
            message_request: The message request
            profile: The profile name to use for authentication
            
        Returns:
            The message response
        """
        client = self.get_client_for_profile(profile)
        return await client.send_message(message_request)

    async def send_message_streaming(
            self, message_request: SendStreamingMessageRequest, profile: str
    ) -> AsyncGenerator[SendStreamingMessageResponse, None]:
        """Send a message with streaming support using the A2A client for the specified profile.
        
        Args:
            message_request: The streaming message request
            profile: The profile name to use for authentication
            
        Yields:
            Streaming message response chunks
        """
        client = self.get_client_for_profile(profile)
        async for chunk in client.send_message_streaming(message_request):
            yield chunk
