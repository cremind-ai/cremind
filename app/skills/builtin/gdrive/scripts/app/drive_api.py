"""Thin wrapper over the Google Drive API v3 (googleapiclient).

Event plane: changes.getStartPageToken / changes.watch (web_hook channel) /
channels.stop + incremental changes.list(pageToken).
Actions: list / get / download (get_media) / export (Google-native) / upload /
mkdir / update (rename, trash, restore) / move.
All calls use the local user's own token — the relay is never involved here.
Scoped to https://www.googleapis.com/auth/drive.
"""
from __future__ import annotations

import io
from typing import Any

# Fields requested for a file resource across actions and the changes feed.
FILE_FIELDS = (
    "id,name,mimeType,parents,createdTime,modifiedTime,trashed,size,"
    "webViewLink,iconLink,lastModifyingUser(displayName,emailAddress)"
)
_CHANGE_FIELDS = (
    "nextPageToken,newStartPageToken,"
    f"changes(changeType,time,removed,fileId,file({FILE_FIELDS}))"
)


def build_service(creds):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


# --- event plane ---

def start_page_token(svc) -> str:
    resp = svc.changes().getStartPageToken(supportsAllDrives=True).execute()
    return resp.get("startPageToken", "")


def incremental_changes(svc, *, page_token: str) -> tuple[list[dict[str, Any]], str]:
    """Return (changes, new_start_page_token). Raises HttpError if the token is
    invalid/expired (caller re-baselines)."""
    changes: list[dict[str, Any]] = []
    token = page_token
    new_start = page_token
    while True:
        resp = (
            svc.changes()
            .list(
                pageToken=token,
                spaces="drive",
                includeRemoved=True,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageSize=100,
                fields=_CHANGE_FIELDS,
            )
            .execute()
        )
        changes.extend(resp.get("changes", []) or [])
        if resp.get("nextPageToken"):
            token = resp["nextPageToken"]
            continue
        new_start = resp.get("newStartPageToken", token)
        break
    return changes, new_start


def watch_changes(svc, *, page_token: str, channel_id: str, address: str, token: str, ttl_seconds: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"id": channel_id, "type": "web_hook", "address": address, "token": token}
    if ttl_seconds:
        body["params"] = {"ttl": str(ttl_seconds)}
    return (
        svc.changes()
        .watch(
            pageToken=page_token,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            includeRemoved=True,
            body=body,
        )
        .execute()
    )


def stop_channel(svc, *, channel_id: str, resource_id: str) -> None:
    svc.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()


# --- actions ---

def list_files(svc, *, query: str | None = None, order_by: str = "modifiedTime desc", page_size: int = 50, page_token: str | None = None) -> dict[str, Any]:
    resp = (
        svc.files()
        .list(
            q=query,
            orderBy=order_by,
            pageSize=page_size,
            pageToken=page_token,
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            corpora="allDrives",
            fields=f"nextPageToken,files({FILE_FIELDS})",
        )
        .execute()
    )
    return resp


def get_file(svc, *, file_id: str) -> dict[str, Any]:
    return svc.files().get(fileId=file_id, supportsAllDrives=True, fields=FILE_FIELDS).execute()


def download_media(svc, *, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def export_file(svc, *, file_id: str, mime_type: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    request = svc.files().export_media(fileId=file_id, mimeType=mime_type)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


def upload_file(svc, *, path: str, name: str, mime_type: str | None = None, parent: str | None = None) -> dict[str, Any]:
    from googleapiclient.http import MediaFileUpload

    metadata: dict[str, Any] = {"name": name}
    if parent:
        metadata["parents"] = [parent]
    media = MediaFileUpload(path, mimetype=mime_type, resumable=True)
    return (
        svc.files()
        .create(body=metadata, media_body=media, supportsAllDrives=True, fields=FILE_FIELDS)
        .execute()
    )


def create_folder(svc, *, name: str, parent: str | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent:
        metadata["parents"] = [parent]
    return svc.files().create(body=metadata, supportsAllDrives=True, fields=FILE_FIELDS).execute()


def update_file(svc, *, file_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return svc.files().update(fileId=file_id, body=body, supportsAllDrives=True, fields=FILE_FIELDS).execute()


def move_file(svc, *, file_id: str, add_parent: str) -> dict[str, Any]:
    current = svc.files().get(fileId=file_id, supportsAllDrives=True, fields="parents").execute()
    remove_parents = ",".join(current.get("parents", []) or [])
    return (
        svc.files()
        .update(
            fileId=file_id,
            addParents=add_parent,
            removeParents=remove_parents or None,
            supportsAllDrives=True,
            fields=FILE_FIELDS,
        )
        .execute()
    )
