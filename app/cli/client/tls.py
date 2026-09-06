"""TLS configuration API; certificate trust/export remain local commands."""
import asyncio
import time

from app.cli.client._base import Client


async def status(client: Client) -> dict:
    return await client.get_json("/api/tls/status")


async def prepare(client: Client, source_origin: str | None = None) -> dict:
    return await client.post_json("/api/tls/prepare", {"source_origin": source_origin} if source_origin else {})


async def activate(client: Client, transition_id: str, certificate_sha256: str | None = None,
                   *, restart: bool = True) -> dict:
    deadline = time.monotonic() + 300
    while True:
        result = await client.post_json("/api/tls/activate", {
            "transition_id": transition_id,
            "certificate_sha256": certificate_sha256,
            "restart": restart,
        })
        if (result.get("transition") or {}).get("phase") != "quiescing":
            return result
        if time.monotonic() >= deadline:
            pending = result.get("quiesce_pending", "Some")
            raise RuntimeError(
                f"{pending} Cremind tab(s) did not finish preparing for HTTPS. "
                "Finish or cancel their uploads, or run `cremind tls cancel` and retry."
            )
        await asyncio.sleep(0.3)


async def cancel(client: Client, transition_id: str) -> dict:
    return await client.post_json("/api/tls/cancel", {"transition_id": transition_id})
