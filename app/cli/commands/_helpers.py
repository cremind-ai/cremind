"""Shared helpers for CLI commands."""

from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

import typer

F = TypeVar("F", bound=Callable[..., Any])


def graceful_errors(func: F) -> F:
    """Convert known CLI exceptions into clean stderr messages + exit-1.

    Catches `ConfigError` (env/flag misconfiguration) and `APIError` (server
    returned non-2xx) and prints just the message — no Python traceback,
    matching the Go CLI's `SilenceErrors`/`SilenceUsage` behavior.

    Imports of `ConfigError` and `APIError` are deferred to the wrapper body
    so that decorating a command doesn't pull in `httpx` / `app.cli.client`
    on the `--help` path.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        import httpx

        from app.cli.client._base import APIError
        from app.cli.config import ConfigError

        try:
            return func(*args, **kwargs)
        except (ConfigError, APIError) as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1) from e
        except httpx.RequestError as e:
            typer.echo(f"could not reach Cremind server: {e}", err=True)
            raise typer.Exit(code=1) from e

    return wrapper  # type: ignore[return-value]
