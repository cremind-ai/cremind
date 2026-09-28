"""A worker hosted in the backend reaches Cremind through the connector API's
own rules: the in-process adapter (app.tags.hosting.local_connector) and the
HTTP adapter (app.api.tag_connector) answer and refuse alike, and the
in-process one also checks what the worker believes it is (profile UUID,
generation).

Both clients are the runtime's real ConnectorClient; the HTTP one goes
through the real Starlette routes over an in-memory ASGI transport.
"""

from __future__ import annotations

import asyncio
import threading
import uuid

import httpx
import pytest
from sqlalchemy import text

from tests.tags._helpers import run, tagenv  # noqa: F401

pytest.importorskip("cbor2")


def _secret() -> str:
    from app.tags import credentials as creds

    return creds.new_secret()


@pytest.fixture
def worker(tagenv):
    """A private worker of p1 (profile UUID pid1): its hardware and content credentials."""
    from app.tags import credentials as creds

    cid = str(uuid.uuid4())
    hw_id, ct_id = creds.new_credential_id(), creds.new_credential_id()
    hw_secret, ct_secret = _secret(), _secret()
    with tagenv.engine.begin() as c:
        c.execute(text(
            "INSERT INTO tag_companions (id, name, created_by, created_at, updated_at, mode, owner_profile, "
            "owner_profile_id, controller_pub, generation, state, paused, gateway_device_id) VALUES "
            "(:id, 'Desk gateway', 'p1', 0, 0, 'private', 'p1', 'pid1', :ctl, 0, 'active', false, :gw)"),
            {"id": cid, "ctl": "11" * 32, "gw": "22" * 16})
        for cred_id, kind, profile, secret in ((hw_id, "hardware", None, hw_secret),
                                               (ct_id, "content", "p1", ct_secret)):
            c.execute(text(
                "INSERT INTO tag_credentials (id, companion_id, kind, profile, secret_sha256, label, created_by, "
                "created_at) VALUES (:id, :cid, :kind, :profile, :sha, 'test', 'test', 0)"),
                {"id": cred_id, "cid": cid, "kind": kind, "profile": profile, "sha": creds.hash_secret(secret)})
    return {"companion_id": cid, "hardware": f"{hw_id}.{hw_secret}", "content": f"{ct_id}.{ct_secret}"}


def _clients(value: str, companion_id: str, *, generation: int = 0, profile_id: str = "pid1"):
    from starlette.applications import Starlette

    from app.api.tag_connector import get_tag_connector_routes
    from app.tags.connector_service import WorkerExpectation
    from app.tags.hosting.local_connector import LocalConnector
    from app.tags.runtime.connector.client import ConnectorClient, parse_credential

    credential = parse_credential(value)
    transport = httpx.ASGITransport(app=Starlette(routes=get_tag_connector_routes()))
    http = ConnectorClient("http://cremind.test", credential, transport=transport)
    local = LocalConnector(credential, expect=WorkerExpectation(companion_id, profile_id, generation))
    return http, local


def _stable(answer: dict) -> dict:
    return {k: v for k, v in answer.items() if k not in ("server_time", "expires_at")}


def test_both_adapters_give_the_same_answers(worker):
    async def go():
        hw_http, hw_local = _clients(worker["hardware"], worker["companion_id"])
        ct_http, ct_local = _clients(worker["content"], worker["companion_id"])
        try:
            who_http, who_local = await hw_http.whoami(), await hw_local.whoami()
            assert (who_http.kind, who_http.companion_id) == (who_local.kind, who_local.companion_id)
            assert _stable(await hw_http.lease()) == _stable(await hw_local.lease())
            assert await hw_http.worker_state() == await hw_local.worker_state()
            body = {"companion": {"version": "t"}, "queue": {"depth": 0}, "devices": []}
            assert (await hw_http.heartbeat(body)).commands_pending == (await hw_local.heartbeat(body)).commands_pending
            assert await hw_http.commands(wait=0) == await hw_local.commands(wait=0) == []
            sync_http, sync_local = await ct_http.sync(None), await ct_local.sync(None)
            assert (sync_http.profile, sync_http.stream_id, sync_http.head_seq) == \
                   (sync_local.profile, sync_local.stream_id, sync_local.head_seq)
            page_http, page_local = await ct_http.events(0), await ct_local.events(0)
            assert (page_http.stream_id, page_http.jobs, page_http.next_after) == \
                   (page_local.stream_id, page_local.jobs, page_local.next_after)
        finally:
            for c in (hw_http, hw_local, ct_http, ct_local):
                await c.aclose()

    run(go())


def test_both_adapters_refuse_alike(worker, tagenv):
    from app.tags.runtime.connector.client import (
        ConnectorAuthError,
        ConnectorNotFound,
        CursorExpired,
    )

    async def refusal(coro):
        try:
            await coro
        except Exception as exc:  # noqa: BLE001
            return type(exc), getattr(exc, "status", None), getattr(exc, "code", None)
        return None

    async def go():
        hw_http, hw_local = _clients(worker["hardware"], worker["companion_id"])
        ct_http, ct_local = _clients(worker["content"], worker["companion_id"])
        try:
            # a content credential on a hardware endpoint
            assert await refusal(ct_http.lease()) == await refusal(ct_local.lease()) == \
                   (ConnectorAuthError, 403, "wrong_credential_kind")
            # an unknown command
            assert await refusal(hw_http.claim("nope")) == await refusal(hw_local.claim("nope")) == \
                   (ConnectorNotFound, 404, "command_not_found")
            # a cursor ahead of the stream (a restore)
            await ct_local.sync(None)
            first = await refusal(ct_http.events(10_000))
            assert first == await refusal(ct_local.events(10_000))
            assert first[0] is CursorExpired
        finally:
            for c in (hw_http, hw_local, ct_http, ct_local):
                await c.aclose()

    run(go())
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE tag_credentials SET revoked_at = 1 WHERE companion_id = :c"),
                  {"c": worker["companion_id"]})

    async def revoked():
        hw_http, hw_local = _clients(worker["hardware"], worker["companion_id"])
        try:
            assert await refusal(hw_http.lease()) == await refusal(hw_local.lease()) == \
                   (ConnectorAuthError, 401, "credential_revoked")
        finally:
            await hw_http.aclose()
            await hw_local.aclose()

    run(revoked())


def test_the_in_process_worker_must_still_be_who_it_believes(worker, tagenv):
    from app.tags.runtime.connector.client import ConnectorAuthError

    async def lease_code(**expect) -> str | None:
        _, local = _clients(worker["hardware"], worker["companion_id"], **expect)
        try:
            await local.lease()
        except ConnectorAuthError as exc:
            return exc.code
        return None

    assert run(lease_code()) is None
    # A recovery moved the worker to another computer (its generation moved on).
    assert run(lease_code(generation=1)) == "worker_revoked"
    # Another profile's UUID.
    assert run(lease_code(profile_id="pid2")) == "worker_revoked"
    # The profile was deleted and recreated under the same name: a different owner.
    with tagenv.engine.begin() as c:
        c.execute(text("UPDATE profiles SET id = 'pid1-new' WHERE name = 'p1'"))
    assert run(lease_code()) == "worker_revoked"


def test_a_worker_on_its_own_loop_is_served_on_the_servers(worker):
    """The runtime's thread calls in; storage work runs on the server's loop."""
    from app.tags.connector_service import WorkerExpectation
    from app.tags.hosting.local_connector import LocalConnector
    from app.tags.runtime.connector.client import parse_credential

    async def server() -> dict:
        server_loop = asyncio.get_running_loop()
        answer: dict = {}

        def runtime_thread() -> None:
            async def work() -> None:
                client = LocalConnector(parse_credential(worker["hardware"]),
                                        expect=WorkerExpectation(worker["companion_id"], "pid1", 0),
                                        server_loop=server_loop)
                answer["lease"] = await client.lease()
                answer["thread_loop"] = asyncio.get_running_loop()

            asyncio.run(work())

        thread = threading.Thread(target=runtime_thread)
        thread.start()
        while thread.is_alive():
            await asyncio.sleep(0.01)
        assert answer["thread_loop"] is not server_loop
        return answer["lease"]

    lease = run(server())
    assert lease["state"] == "active" and lease["generation"] == 0
