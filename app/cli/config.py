"""CLI configuration sourced from environment variables and root flags.

Mirrors `cli/internal/config/env.go`. The active profile is resolved
server-side from the JWT, so it is intentionally not part of the CLI's
configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal, Optional


ENV_SERVER = "CREMIND_SERVER"
ENV_TOKEN = "CREMIND_TOKEN"
ENV_PROFILE = "CREMIND_PROFILE"
ENV_OUTPUT = "OPA_OUTPUT"
ENV_NO_COLOR = "OPA_NO_COLOR"

DEFAULT_SERVER = "http://localhost:1112"


class ConfigError(Exception):
    """Raised when CLI configuration is invalid or required values are missing."""


@dataclass(frozen=True)
class Config:
    server: str
    token: str
    output: Literal["table", "json"]
    no_color: bool

    def require_token(self) -> None:
        if not self.token:
            raise ConfigError(
                "no Cremind profile selected and no token available. Run in an "
                "interactive terminal to pick a profile, pass `--profile <name>`, "
                f"set {ENV_TOKEN}, or run `cremind setup` to create a profile."
            )


def load_from_env(
    *,
    server: Optional[str] = None,
    token: Optional[str] = None,
    json_flag: bool = False,
) -> Config:
    """Build a `Config` from environment variables, with optional overrides.

    `server` and `token` from CLI flags take precedence over env vars.
    `json_flag` (the `--json` root flag) forces JSON output regardless of
    `OPA_OUTPUT`.
    """
    resolved_server = (server or os.getenv(ENV_SERVER) or DEFAULT_SERVER).rstrip("/")
    resolved_token = token if token is not None else os.getenv(ENV_TOKEN, "")

    if json_flag:
        output: Literal["table", "json"] = "json"
    else:
        env_output = os.getenv(ENV_OUTPUT, "").lower()
        if env_output in ("", "table"):
            output = "table"
        elif env_output == "json":
            output = "json"
        else:
            raise ConfigError(f"{ENV_OUTPUT} must be 'table' or 'json'")

    no_color = bool(os.getenv(ENV_NO_COLOR))
    return Config(
        server=resolved_server,
        token=resolved_token,
        output=output,
        no_color=no_color,
    )
