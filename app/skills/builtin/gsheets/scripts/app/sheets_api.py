"""Thin wrapper over the Google Sheets API v4 (googleapiclient).

Actions only: create / info (metadata) / read (values.batchGet) / update /
append / clear. Google offers no push API for spreadsheet content, so there is
no event plane here — file-level changes surface via the gdrive skill. All calls
use the local user's own token.
Scoped to https://www.googleapis.com/auth/spreadsheets.
"""
from __future__ import annotations

from typing import Any


def build_service(creds):
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def create_spreadsheet(svc, *, title: str, tabs: list[str] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"properties": {"title": title}}
    if tabs:
        body["sheets"] = [{"properties": {"title": t}} for t in tabs]
    return svc.spreadsheets().create(body=body).execute()


def get_metadata(svc, *, spreadsheet_id: str) -> dict[str, Any]:
    fields = "spreadsheetId,spreadsheetUrl,properties(title,locale,timeZone),sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)))"
    return svc.spreadsheets().get(spreadsheetId=spreadsheet_id, fields=fields).execute()


def get_values(svc, *, spreadsheet_id: str, ranges: list[str], value_render_option: str = "FORMATTED_VALUE") -> list[dict[str, Any]]:
    resp = (
        svc.spreadsheets()
        .values()
        .batchGet(spreadsheetId=spreadsheet_id, ranges=ranges, valueRenderOption=value_render_option)
        .execute()
    )
    return resp.get("valueRanges", []) or []


def update_values(svc, *, spreadsheet_id: str, range_a1: str, values: list[list[Any]], value_input_option: str = "USER_ENTERED") -> dict[str, Any]:
    return (
        svc.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=value_input_option,
            body={"values": values},
        )
        .execute()
    )


def append_values(svc, *, spreadsheet_id: str, range_a1: str, values: list[list[Any]], value_input_option: str = "USER_ENTERED") -> dict[str, Any]:
    return (
        svc.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=value_input_option,
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        )
        .execute()
    )


def clear_values(svc, *, spreadsheet_id: str, range_a1: str) -> dict[str, Any]:
    return svc.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=range_a1, body={}).execute()
