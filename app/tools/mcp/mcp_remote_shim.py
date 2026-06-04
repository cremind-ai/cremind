"""Shim that makes MCP servers appear as RemoteAgentConnections.

This allows the existing API code to interact with MCP servers using the
same interface as A2A remote agents, without any changes.
"""

from typing import Optional, Union

from app.tools.mcp.mcp_auth import MCPOAuthClient, NoOpOAuthClient
from app.utils.logger import logger


class MCPRemoteConnectionShim:
    """Makes an MCP server look like a RemoteAgentConnections to existing code.

    Provides the same methods that agents.py calls on
    RemoteAgentConnections, delegating to MCPOAuthClient for auth.
    """

    def __init__(
        self,
        server_name: str,
        mcp_auth: Optional[MCPOAuthClient] = None,
    ):
        self.server_name = server_name
        self._mcp_auth = mcp_auth

        # Cache for per-profile OAuth clients
        self._profile_oauth_clients: dict[str, Union[MCPOAuthClient, NoOpOAuthClient]] = {}

        # Default oauth_client (for backward compat with code that accesses .oauth_client directly)
        if mcp_auth:
            self.oauth_client = mcp_auth
        else:
            self.oauth_client = NoOpOAuthClient(server_name)

    def get_oauth_client_for_profile(self, profile: str) -> Union[MCPOAuthClient, NoOpOAuthClient]:
        """Get or create an OAuth client for a specific profile.

        For MCP servers with auth: returns a new MCPOAuthClient for the profile.
        For MCP servers without auth: returns a NoOpOAuthClient.
        """
        if profile not in self._profile_oauth_clients:
            if self._mcp_auth:
                # Create a new MCPOAuthClient for this profile, sharing config
                client = MCPOAuthClient(
                    server_url=self._mcp_auth.server_url,
                    server_name=self._mcp_auth.server_name,
                    profile=profile,
                    client_id=self._mcp_auth._client_id,
                    client_secret=self._mcp_auth._client_secret,
                    extra_authorize_params=self._mcp_auth._extra_authorize_params,
                )
                # Share the discovered/configured auth metadata and PKCE verifiers
                client._auth_metadata = self._mcp_auth._auth_metadata
                client._pkce_verifiers = self._mcp_auth._pkce_verifiers
                self._profile_oauth_clients[profile] = client
            else:
                self._profile_oauth_clients[profile] = NoOpOAuthClient(self.server_name)
        return self._profile_oauth_clients[profile]

    def reinitialize_oauth(self, mcp_auth: Optional["MCPOAuthClient"] = None):
        """Replace the OAuth client after credentials have been updated.

        Clears all cached per-profile clients so they are recreated
        with the new auth configuration on next access.
        """
        self._mcp_auth = mcp_auth
        self._profile_oauth_clients.clear()
        if mcp_auth:
            self.oauth_client = mcp_auth
        else:
            self.oauth_client = NoOpOAuthClient(self.server_name)

    def update_auth_header(self, profile: Optional[str] = None):
        """For MCP servers, auth reconnection happens lazily in MCPAgentAdapter._ensure_auth()."""
        logger.info(f"Auth header updated for MCP server '{self.server_name}' (profile: {profile})")

    def update_auth_header_for_profile(self, profile: str):
        """For MCP servers, auth reconnection happens lazily in MCPAgentAdapter._ensure_auth()."""
        logger.info(f"Auth header updated for MCP server '{self.server_name}' (profile: {profile})")

    def get_agent(self):
        """Not applicable for MCP servers. Returns None."""
        return None
