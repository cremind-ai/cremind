"""GitHub Copilot LLM provider.

Wraps the OpenAI-compatible Copilot API with the required token exchange
(GitHub OAuth token → short-lived Copilot API token) and client
identification headers.
"""

import time
from typing import Any, Dict, Optional

import httpx
from openai import AsyncOpenAI
from tiktoken import encoding_for_model

from .openai import OpenAILLMProvider

_COPILOT_TOKEN_URL = "https://api.github.com/copilot_internal/v2/token"
_COPILOT_TOKEN_BUFFER = 300  # refresh 5 min before expiry

# Headers required by GitHub to identify an approved Copilot client
_COPILOT_HEADERS = {
    "Editor-Version": "vscode/1.100.0",
    "Editor-Plugin-Version": "copilot-chat/0.24.0",
    "User-Agent": "GithubCopilot/1.0.0",
    "Copilot-Integration-Id": "vscode-chat",
}

# Module-level cache for short-lived Copilot API tokens (keyed by OAuth token)
_token_cache: dict[str, dict] = {}


def _exchange_token(oauth_token: str) -> str:
    """Exchange a GitHub OAuth token for a short-lived Copilot API token.

    Uses a module-level cache to avoid hitting the exchange endpoint on
    every call.  Tokens are refreshed when they have fewer than 5 minutes
    of remaining validity.
    """
    cached = _token_cache.get(oauth_token)
    if cached and cached["expires_at"] - time.time() > _COPILOT_TOKEN_BUFFER:
        return cached["token"]

    try:
        resp = httpx.get(
            _COPILOT_TOKEN_URL,
            headers={
                "Authorization": f"token {oauth_token}",
                "Accept": "application/json",
                **_COPILOT_HEADERS,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise ValueError(
                "GitHub Copilot token exchange failed (401). "
                "Your GitHub token may be expired or your Copilot subscription inactive. "
                "Please re-authenticate via GitHub Device Login."
            ) from e
        raise ValueError(
            f"GitHub Copilot token exchange failed ({e.response.status_code}): {e.response.text}"
        ) from e
    except httpx.RequestError as e:
        raise ValueError(
            f"Failed to reach GitHub Copilot token endpoint: {e}"
        ) from e

    data = resp.json()
    token = data["token"]
    expires_at = data["expires_at"]
    _token_cache[oauth_token] = {"token": token, "expires_at": expires_at}
    return token


class GitHubCopilotLLMProvider(OpenAILLMProvider):
    """OpenAI-compatible provider for GitHub Copilot.

    Handles the OAuth-to-Copilot token exchange and injects the required
    client identification headers automatically.
    """

    def __init__(self, oauth_token: str, model_name: str, default_reasoning_effort: Optional[str] = None):
        copilot_token = _exchange_token(oauth_token)
        kwargs: Dict[str, Any] = {
            "api_key": copilot_token,
            "base_url": "https://api.githubcopilot.com",
            "default_headers": _COPILOT_HEADERS,
        }
        self.openai = AsyncOpenAI(**kwargs)
        self.model_name = model_name
        self.default_reasoning_effort = default_reasoning_effort
        self.encoder = encoding_for_model("gpt-4o")  # Fallback
