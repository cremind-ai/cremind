"""Thin wrapper over the Google Calendar API (googleapiclient).

Event plane: events.watch()/channels.stop() + incremental events.list(syncToken).
Actions: list-calendars / list / get / create / update / delete events.
All calls use the local user's own token — the relay is never involved here.
"""
from __future__ import annotations

from typing import Any


def build_service(creds):
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# --- event plane ---

def watch(svc, *, calendar_id: str, channel_id: str, address: str, token: str, ttl_seconds: int | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"id": channel_id, "type": "web_hook", "address": address, "token": token}
    if ttl_seconds:
        body["params"] = {"ttl": str(ttl_seconds)}
    return svc.events().watch(calendarId=calendar_id, body=body).execute()


def stop_channel(svc, *, channel_id: str, resource_id: str) -> None:
    svc.channels().stop(body={"id": channel_id, "resourceId": resource_id}).execute()


def initial_sync_token(svc, *, calendar_id: str) -> str:
    """Page through the events collection once to obtain a starting syncToken."""
    page_token = None
    sync_token = ""
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=calendar_id,
                singleEvents=True,
                showDeleted=True,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        page_token = resp.get("nextPageToken")
        if not page_token:
            sync_token = resp.get("nextSyncToken", "")
            break
    return sync_token


def incremental_changes(svc, *, calendar_id: str, sync_token: str) -> tuple[list[dict[str, Any]], str]:
    """Return (changed_events, new_sync_token). Raises HttpError(410) if token expired."""
    changes: list[dict[str, Any]] = []
    page_token = None
    new_token = sync_token
    while True:
        resp = (
            svc.events()
            .list(
                calendarId=calendar_id,
                syncToken=sync_token,
                singleEvents=True,
                showDeleted=True,
                pageToken=page_token,
            )
            .execute()
        )
        changes.extend(resp.get("items", []) or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            new_token = resp.get("nextSyncToken", sync_token)
            break
    return changes, new_token


# --- actions ---

def list_calendars(svc) -> list[dict[str, Any]]:
    return svc.calendarList().list().execute().get("items", []) or []


def list_events(svc, *, calendar_id: str, time_min: str | None = None, time_max: str | None = None, query: str | None = None, max_results: int = 50) -> list[dict[str, Any]]:
    resp = (
        svc.events()
        .list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            q=query,
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )
    return resp.get("items", []) or []


def get_event(svc, *, calendar_id: str, event_id: str) -> dict[str, Any]:
    return svc.events().get(calendarId=calendar_id, eventId=event_id).execute()


def create_event(svc, *, calendar_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return svc.events().insert(calendarId=calendar_id, body=body).execute()


def update_event(svc, *, calendar_id: str, event_id: str, body: dict[str, Any]) -> dict[str, Any]:
    return svc.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()


def delete_event(svc, *, calendar_id: str, event_id: str) -> None:
    svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
