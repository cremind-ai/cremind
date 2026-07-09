"""Thin wrapper over the Google Docs API v1 (googleapiclient).

Actions only: get / create / append text / replace-all-text. Google offers no
push API for document content, so there is no event plane — file-level changes
surface via the gdrive skill. All calls use the local user's own token.
Scoped to https://www.googleapis.com/auth/documents.
"""
from __future__ import annotations

from typing import Any


def build_service(creds):
    from googleapiclient.discovery import build

    return build("docs", "v1", credentials=creds, cache_discovery=False)


def get_document(svc, *, document_id: str) -> dict[str, Any]:
    return svc.documents().get(documentId=document_id).execute()


def create_document(svc, *, title: str) -> dict[str, Any]:
    return svc.documents().create(body={"title": title}).execute()


def batch_update(svc, *, document_id: str, requests: list[dict[str, Any]]) -> dict[str, Any]:
    return svc.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()


def append_text(svc, *, document_id: str, text: str) -> dict[str, Any]:
    # endOfSegmentLocation with an empty segmentId targets the end of the document
    # body, so no index arithmetic is needed.
    requests = [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]
    return batch_update(svc, document_id=document_id, requests=requests)


def replace_all(svc, *, document_id: str, find: str, replace_with: str, match_case: bool = False) -> dict[str, Any]:
    requests = [
        {
            "replaceAllText": {
                "containsText": {"text": find, "matchCase": match_case},
                "replaceText": replace_with,
            }
        }
    ]
    resp = batch_update(svc, document_id=document_id, requests=requests)
    replies = resp.get("replies", []) or []
    occurrences = 0
    if replies:
        occurrences = (replies[0].get("replaceAllText", {}) or {}).get("occurrencesChanged", 0) or 0
    return {"occurrences_changed": occurrences}
