"""`cremind llm ...` — manage LLM providers, models, and model groups.

Mirrors `cli/cmd/llm.go`.
"""

from __future__ import annotations

import json as _json
import sys
import time
from typing import Any, Optional

import typer

from app.cli.commands._helpers import graceful_errors


llm_app = typer.Typer(
    name="llm",
    help="Manage LLM providers, models, and model groups.",
    no_args_is_help=True,
)
providers_app = typer.Typer(
    name="providers",
    help="List, configure, and inspect LLM providers.",
    no_args_is_help=True,
)
model_groups_app = typer.Typer(
    name="model-groups",
    help="Read or write the configured model (and optional vision model).",
    no_args_is_help=True,
)
device_code_app = typer.Typer(
    name="device-code",
    help="GitHub Copilot device-code authentication flow.",
    no_args_is_help=True,
)
llm_app.add_typer(providers_app, name="providers")
llm_app.add_typer(model_groups_app, name="model-groups")
llm_app.add_typer(device_code_app, name="device-code")


# ── providers ─────────────────────────────────────────────────────────────

@providers_app.command("list")
@graceful_errors
def providers_list(ctx: typer.Context) -> None:
    """List configured and available providers."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import list_llm_providers
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import bool_field, string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> list[dict[str, Any]]:
        async with Client(cfg) as client:
            return await list_llm_providers(client)

    providers = asyncio.run(_run())

    if mode.json:
        print_json(providers)
        return

    table = Table(mode, "NAME", "DISPLAY", "CONFIGURED", "MODELS", "ACTIVE_AUTH")
    for p in providers:
        table.add_row(
            string_field(p, "name"),
            string_field(p, "display_name"),
            bool_field(p, "configured", False),
            _num_field(p, "model_count"),
            string_field(p, "active_auth_method"),
        )
    table.render()


@providers_app.command("models")
@graceful_errors
def providers_models(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider name."),
) -> None:
    """List models for a provider."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import get_provider_models
    from app.cli.config import Config
    from app.cli.output import OutputMode, Table, print_json
    from app.cli.output.formatting import string_field

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_provider_models(client, provider)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
        return

    models = out.get("models") if isinstance(out, dict) else None
    table = Table(mode, "ID", "NAME")
    if isinstance(models, list):
        for m in models:
            if isinstance(m, dict):
                table.add_row(string_field(m, "id"), string_field(m, "name"))
    table.render()


@providers_app.command("configure")
@graceful_errors
def providers_configure(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider name."),
    api_key: Optional[str] = typer.Option(
        None, "--api-key", help="API key value (kept secret server-side)."
    ),
    auth_method: Optional[str] = typer.Option(
        None, "--auth-method", help="Active auth method id."
    ),
    extra_json: Optional[str] = typer.Option(
        None, "--json", help="Additional fields as a JSON object."
    ),
) -> None:
    """Set provider configuration (api key, auth method, etc.)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import configure_provider
    from app.cli.config import Config

    body: dict[str, Any] = {}
    if api_key:
        body["api_key"] = api_key
    if auth_method:
        body["auth_method"] = auth_method
    if extra_json:
        try:
            extra = _json.loads(extra_json)
        except _json.JSONDecodeError as e:
            typer.echo(f"invalid --json: {e}", err=True)
            raise typer.Exit(code=1) from e
        if not isinstance(extra, dict):
            typer.echo("--json must be an object", err=True)
            raise typer.Exit(code=1)
        body.update(extra)

    if not body:
        typer.echo(
            "at least one of --api-key, --auth-method, or --json is required",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await configure_provider(client, provider, body)

    asyncio.run(_run())


@providers_app.command("delete-config")
@graceful_errors
def providers_delete_config(
    ctx: typer.Context,
    provider: str = typer.Argument(..., help="Provider name."),
) -> None:
    """Remove all stored config for a provider."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import delete_provider_config
    from app.cli.config import Config

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await delete_provider_config(client, provider)

    asyncio.run(_run())


# ── model-groups ──────────────────────────────────────────────────────────

@model_groups_app.command("get")
@graceful_errors
def model_groups_get(ctx: typer.Context) -> None:
    """Show model group assignments."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import get_model_groups
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    cfg.require_token()

    async def _run() -> dict[str, Any]:
        async with Client(cfg) as client:
            return await get_model_groups(client)

    out = asyncio.run(_run())

    if mode.json:
        print_json(out)
    else:
        sys.stdout.write(_json.dumps(out, indent=2, ensure_ascii=False, default=str) + "\n")


@model_groups_app.command("set")
@graceful_errors
def model_groups_set(
    ctx: typer.Context,
    model: Optional[str] = typer.Option(
        None, "--model", "--high",
        help="Provider/model id for the single configured model (e.g. groq/openai/gpt-oss-120b).",
    ),
    vision: Optional[str] = typer.Option(
        None, "--vision",
        help="Optional provider/model id for image understanding (defaults to --model).",
    ),
    vision_enabled: Optional[bool] = typer.Option(
        None, "--vision-enabled/--no-vision-enabled",
        help="Enable/disable the Specialized Vision Model feature (the image_understanding tool).",
    ),
    default_provider: Optional[str] = typer.Option(
        None, "--default-provider", help="Default provider name."
    ),
) -> None:
    """Update the configured model (and optional vision model)."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import update_model_groups
    from app.cli.config import Config

    body: dict[str, Any] = {}
    groups: dict[str, Any] = {}
    # The single model is stored under the historical ``high`` key.
    if model:
        groups["high"] = model
    if vision:
        groups["vision"] = vision
    if groups:
        body["model_groups"] = groups
    if default_provider:
        body["default_provider"] = default_provider
    if vision_enabled is not None:
        body["vision_enabled"] = vision_enabled

    if not body:
        typer.echo(
            "at least one of --model, --vision, --vision-enabled, --default-provider is required",
            err=True,
        )
        raise typer.Exit(code=1)

    cfg: Config = ctx.obj["cfg"]
    cfg.require_token()

    async def _run() -> None:
        async with Client(cfg) as client:
            await update_model_groups(client, body)

    asyncio.run(_run())


# ── device-code ───────────────────────────────────────────────────────────

@device_code_app.command("start")
@graceful_errors
def device_code_start(ctx: typer.Context) -> None:
    """Start a device-code flow; prints user code + verification URL."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import device_code_start as _start
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]

    async def _run():
        async with Client(cfg) as client:
            return await _start(client)

    resp = asyncio.run(_run())

    if mode.json:
        print_json(resp.to_dict())
        return
    print_kv([
        ("verification_uri", resp.verification_uri),
        ("user_code", resp.user_code),
        ("device_code", resp.device_code),
        ("expires_in", str(resp.expires_in)),
        ("interval", str(resp.interval)),
    ])


@device_code_app.command("poll")
@graceful_errors
def device_code_poll(
    ctx: typer.Context,
    device_code: str = typer.Argument(..., help="Device code from `device-code start`."),
) -> None:
    """Poll until the user completes the flow."""
    import asyncio

    from app.cli.client._base import Client
    from app.cli.client.llm import device_code_poll as _poll
    from app.cli.config import Config
    from app.cli.output import OutputMode, print_json, print_kv

    cfg: Config = ctx.obj["cfg"]
    mode: OutputMode = ctx.obj["mode"]
    interval = 5

    async def _run():
        nonlocal interval
        async with Client(cfg) as client:
            while True:
                resp = await _poll(client, device_code)
                if resp.status == "complete":
                    return resp
                if resp.status == "expired":
                    raise RuntimeError(
                        "device code expired; run `cremind llm device-code start` again"
                    )
                if resp.status == "error":
                    raise RuntimeError(f"device-code flow error: {resp.error}")
                if resp.status == "pending":
                    if resp.slow_down:
                        interval += 5
                    await asyncio.sleep(interval)
                    continue
                raise RuntimeError(f"unexpected status '{resp.status}'")

    try:
        resp = asyncio.run(_run())
    except RuntimeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    except KeyboardInterrupt:
        raise typer.Exit(code=130)

    if mode.json:
        print_json(resp.to_dict())
        return
    if resp.access_token:
        print_kv([("status", "complete"), ("access_token", resp.access_token)])
    else:
        sys.stdout.write("complete (token stored server-side)\n")


def _num_field(d: dict[str, Any], key: str) -> str:
    v = d.get(key)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(int(v))
    if v is None:
        return ""
    return str(v)
