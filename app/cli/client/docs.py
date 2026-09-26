"""Documentation search endpoints — `/api/documentation-search/*`.

Everything is scoped to the caller's own profile except `admin`, which is the
server-wide gate and needs the admin profile. The indexed folder is always the
profile's working directory: a settings PUT naming one is refused (400
`root_not_configurable`). Error bodies callers should recognise:

- **409 ConfirmationRequired** — the change would remove indexed content; the
  body carries `plan` (what would go) and `confirm` (a token). Repeat the same
  request with `confirm` set to apply it.
- **409 FeatureNotInstalled** — allowing the feature needs optional extras;
  install them with `cremind features install documentation_search`.
- **409 DriveNotLinked / DriveFoldersRequired** — turning Drive on needs a
  linked gdrive skill, and a whole-Drive account needs folders to index
  (`drive/folders` lists them).

`query/{find|search|read}` run the agent's search leaves and answer with the
text the agent would read; `citations/resolve` looks citation tokens up.

`research` starts, follows, answers and cancels deep-research jobs. Every
answer carries `job` (the job view) and `text` (what the agent would read);
`wait` holds a request open up to the server's cap while the job runs, so
keep the client's timeout above it.
"""

from __future__ import annotations

from typing import Any, Optional
from urllib.parse import quote

from app.cli.client._base import Client


async def get_status(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/documentation-search/status")
    return resp if isinstance(resp, dict) else {}


async def get_settings(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/documentation-search/settings")
    return resp if isinstance(resp, dict) else {}


async def put_settings(client: Client, body: dict[str, Any]) -> dict[str, Any]:
    resp = await client.put_json("/api/documentation-search/settings", body)
    return resp if isinstance(resp, dict) else {}


async def get_admin(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/documentation-search/admin")
    return resp if isinstance(resp, dict) else {}


async def put_admin(client: Client, policy: dict[str, Any]) -> dict[str, Any]:
    resp = await client.put_json("/api/documentation-search/admin", {"policy": policy})
    return resp if isinstance(resp, dict) else {}


async def control(client: Client, action: str, **params: Any) -> dict[str, Any]:
    resp = await client.post_json("/api/documentation-search/control", {"action": action, **params})
    return resp if isinstance(resp, dict) else {}


async def confirm_root_change(client: Client) -> dict[str, Any]:
    """Index the profile's working directory where it is now, after the admin
    moved it (the ``hold(pending_root_change)`` state). Files still inside the
    new folder keep their index; the rest leave it."""
    return await control(client, "confirm_root_change")


async def list_files(client: Client, **params: Any) -> dict[str, Any]:
    clean = {k: v for k, v in params.items() if v is not None}
    resp = await client.get_json("/api/documentation-search/files", params=clean or None)
    return resp if isinstance(resp, dict) else {}


async def file_detail(client: Client, fid: str) -> dict[str, Any]:
    resp = await client.get_json(f"/api/documentation-search/files/{fid}")
    return resp if isinstance(resp, dict) else {}


async def activity(client: Client, *, before: Optional[int] = None, limit: int = 50) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if before is not None:
        params["before"] = before
    resp = await client.get_json("/api/documentation-search/activity", params=params)
    return resp if isinstance(resp, dict) else {}


async def get_estimate(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/documentation-search/estimate")
    return resp if isinstance(resp, dict) else {}


async def start_estimate(client: Client) -> dict[str, Any]:
    resp = await client.post_json("/api/documentation-search/estimate", {})
    return resp if isinstance(resp, dict) else {}


async def get_storage(client: Client) -> dict[str, Any]:
    resp = await client.get_json("/api/documentation-search/storage")
    return resp if isinstance(resp, dict) else {}


async def drive_folders(client: Client, *, parent: Optional[str] = None) -> dict[str, Any]:
    """One level of the linked Drive's folders: ``{folders: [{id, name}],
    parent: {id, name} | None, whole_drive}``. Without ``parent``, the top
    level (My Drive on a whole-Drive account, the granted folders otherwise)."""
    params = {"parent": parent} if parent else None
    resp = await client.get_json("/api/documentation-search/drive/folders", params=params)
    return resp if isinstance(resp, dict) else {}


def documents_stream_path() -> str:
    return "/api/documentation-search/stream"


async def query(client: Client, leaf: str, body: dict[str, Any]) -> dict[str, Any]:
    """``leaf`` is ``find``, ``search`` or ``read``; ``body`` takes the same
    arguments as the agent's ``documentation_search__*`` functions. The answer
    carries ``text`` (what the agent would read) plus the structured result."""
    clean = {k: v for k, v in body.items() if v is not None}
    resp = await client.post_json(f"/api/documentation-search/query/{leaf}", clean)
    return resp if isinstance(resp, dict) else {}


async def resolve_citations(
    client: Client, tokens: list[str], *, conversation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve ``[doc:…]`` tokens to their file, location and snippet. With a
    conversation id, each is also checked against what the tools issued there."""
    body: dict[str, Any] = {"tokens": tokens}
    if conversation_id:
        body["conversation_id"] = conversation_id
    resp = await client.post_json("/api/documentation-search/citations/resolve", body)
    return resp if isinstance(resp, dict) else {}


def _research_path(job_id: str, suffix: str = "") -> str:
    return f"/api/documentation-search/research/{quote(job_id, safe='')}{suffix}"


async def research_start(client: Client, body: dict[str, Any]) -> dict[str, Any]:
    """``body``: ``question``, and optionally ``mode`` (analyze | compile),
    ``domain`` (general | legal | financial), ``scope`` / ``reference_scope``
    (filter objects, as the agent's tool takes them) and ``wait`` (seconds)."""
    clean = {k: v for k, v in body.items() if v is not None}
    resp = await client.post_json("/api/documentation-search/research", clean)
    return resp if isinstance(resp, dict) else {}


async def research_list(client: Client, *, limit: int = 20) -> dict[str, Any]:
    resp = await client.get_json("/api/documentation-search/research", params={"limit": limit})
    return resp if isinstance(resp, dict) else {}


async def research_get(
    client: Client, job_id: str, *, page: Optional[int] = None, wait: Optional[float] = None,
) -> dict[str, Any]:
    """The job, its text (dossier page ``page`` once finished) and ``pages``."""
    params: dict[str, Any] = {}
    if page is not None:
        params["page"] = page
    if wait:
        params["wait"] = wait
    resp = await client.get_json(_research_path(job_id), params=params or None)
    return resp if isinstance(resp, dict) else {}


async def research_continue(
    client: Client, job_id: str, *, answers: Optional[dict[str, Any]] = None, wait: Optional[float] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {}
    if answers:
        body["answers"] = answers
    if wait:
        body["wait"] = wait
    resp = await client.post_json(_research_path(job_id, "/continue"), body)
    return resp if isinstance(resp, dict) else {}


async def research_cancel(client: Client, job_id: str) -> dict[str, Any]:
    resp = await client.post_json(_research_path(job_id, "/cancel"), {})
    return resp if isinstance(resp, dict) else {}
