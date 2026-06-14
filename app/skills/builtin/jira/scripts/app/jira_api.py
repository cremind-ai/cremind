"""Thin wrapper over the Jira Cloud REST API v3 (OAuth 2.0 / 3LO).

All calls go through https://api.atlassian.com/ex/jira/{cloudId}/rest/api/3 with the
user's own access token (the relay is never in the API path). v3 bodies use ADF
(Atlassian Document Format) for rich text, so descriptions/comments are wrapped via
``text_to_adf``. Stdlib ``urllib`` only — no third-party HTTP client.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API_ROOT = "https://api.atlassian.com"

_CURRENT_USER_RE = re.compile(r"currentUser\(\s*\)", re.IGNORECASE)


def webhook_jql(raw_jql: str, account_id: str) -> str:
    """Adapt a JQL filter for a Jira DYNAMIC WEBHOOK registration.

    The webhook jqlFilter accepts only a restricted JQL subset — operators
    =, !=, IN, NOT IN on fields issueKey, project, issuetype, status, priority,
    assignee, reporter (+ issue.property, cf[id]). JQL FUNCTIONS are NOT supported,
    and the matcher runs with no user context, so `currentUser()` does not resolve
    (it may even register, then silently match nothing). We substitute it with the
    registering user's literal accountId. An empty filter matches all issues.
    """
    jql = (raw_jql or "").strip()
    if not jql:
        return ""
    if _CURRENT_USER_RE.search(jql):
        if not account_id:
            raise ApiError(0, "cannot build webhook filter: currentUser() used but no accountId resolved")
        jql = _CURRENT_USER_RE.sub(f'"{account_id}"', jql)
    return jql


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"jira api error {status}: {message}")
        self.status = status
        self.body = body


def text_to_adf(text: str) -> dict[str, Any]:
    """Wrap plain text into a minimal ADF document (one paragraph per line)."""
    lines = (text or "").split("\n")
    content = []
    for line in lines:
        para: dict[str, Any] = {"type": "paragraph", "content": []}
        if line:
            para["content"].append({"type": "text", "text": line})
        content.append(para)
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


def _request(
    method: str,
    url: str,
    *,
    access_token: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
    max_retries: int = 3,
) -> Any:
    if params:
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None
    headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": "cremind-skill/1.0"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    attempt = 0
    while True:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace") if e.fp else ""
            if e.code == 429 and attempt < max_retries:
                retry_after = e.headers.get("Retry-After", "1") or "1"
                try:
                    delay = min(int(retry_after), 30)
                except ValueError:
                    delay = 2
                time.sleep(delay)
                attempt += 1
                continue
            try:
                parsed = json.loads(detail) if detail else None
            except ValueError:
                parsed = detail
            raise ApiError(e.code, detail[:300], parsed)
        except (urllib.error.URLError, OSError) as e:
            if attempt < max_retries:
                time.sleep(2 ** attempt)
                attempt += 1
                continue
            raise ApiError(0, str(e))


class JiraClient:
    def __init__(self, access_token: str, cloud_id: str):
        self.access_token = access_token
        self.cloud_id = cloud_id
        self.base = f"{API_ROOT}/ex/jira/{cloud_id}/rest/api/3"

    def _req(self, method: str, path: str, **kw) -> Any:
        return _request(method, f"{self.base}{path}", access_token=self.access_token, **kw)

    # --- actions ---

    def myself(self) -> dict[str, Any]:
        return self._req("GET", "/myself")

    def projects(self, *, max_results: int = 50) -> list[dict[str, Any]]:
        return self._req("GET", "/project/search", params={"maxResults": max_results}).get("values", []) or []

    def search(self, jql: str, *, fields: list[str] | None = None, max_results: int = 50, next_page_token: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"jql": jql, "maxResults": max_results}
        if fields:
            body["fields"] = fields
        if next_page_token:
            body["nextPageToken"] = next_page_token
        return self._req("POST", "/search/jql", body=body)

    def get_issue(self, key: str, *, fields: list[str] | None = None, expand: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if fields:
            params["fields"] = ",".join(fields)
        if expand:
            # e.g. expand="changelog" → response carries changelog.histories[], each with
            # created + items[] (field/fieldId/fromString/toString). The /search/jql search
            # endpoint can't reliably expand changelog, so per-issue fetch is the only path.
            params["expand"] = expand
        return self._req("GET", f"/issue/{urllib.parse.quote(key)}", params=params or None)

    def get_comments(self, key: str, *, order_by: str = "-created", max_results: int = 20, start_at: int = 0) -> dict[str, Any]:
        """Return the issue's comments: {startAt, maxResults, total, comments: [...]}.

        Comments are NOT part of the changelog, so detecting a new comment needs this
        separate fetch. Each comment carries id/author/body(ADF)/created/updated.
        """
        params = {"orderBy": order_by, "maxResults": max_results, "startAt": start_at}
        return self._req("GET", f"/issue/{urllib.parse.quote(key)}/comment", params=params)

    def create_issue(self, *, project_key: str, summary: str, issue_type: str = "Task", description: str | None = None) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "summary": summary,
            "issuetype": {"name": issue_type},
        }
        if description:
            fields["description"] = text_to_adf(description)
        return self._req("POST", "/issue", body={"fields": fields})

    def add_comment(self, key: str, text: str) -> dict[str, Any]:
        return self._req("POST", f"/issue/{urllib.parse.quote(key)}/comment", body={"body": text_to_adf(text)})

    def get_transitions(self, key: str) -> list[dict[str, Any]]:
        return self._req("GET", f"/issue/{urllib.parse.quote(key)}/transitions").get("transitions", []) or []

    def transition(self, key: str, transition_id: str) -> dict[str, Any]:
        return self._req("POST", f"/issue/{urllib.parse.quote(key)}/transitions", body={"transition": {"id": transition_id}})

    # --- dynamic webhooks (event plane) ---

    def register_webhook(self, *, url: str, events: list[str], jql: str) -> dict[str, Any]:
        body = {"url": url, "webhooks": [{"events": events, "jqlFilter": jql}]}
        return self._req("POST", "/webhook", body=body)

    def list_webhooks(self, *, max_results: int = 100) -> list[dict[str, Any]]:
        return self._req("GET", "/webhook", params={"maxResults": max_results}).get("values", []) or []

    def refresh_webhooks(self, ids: list[int]) -> dict[str, Any]:
        return self._req("PUT", "/webhook/refresh", body={"webhookIds": ids})

    def delete_webhooks(self, ids: list[int]) -> Any:
        return self._req("DELETE", "/webhook", body={"webhookIds": ids})
