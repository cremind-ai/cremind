"""Documentation search — deep research jobs, for the CLI and scripts.

``/api/documentation-search/research`` starts, follows, answers and cancels the same
research jobs the agent runs through ``documentation_search__research`` (see
:mod:`app.documents.research.jobs`):

- ``POST /api/documentation-search/research``                 — start a job (202).
- ``GET  /api/documentation-search/research``                 — this profile's recent jobs.
- ``GET  /api/documentation-search/research/{id}?page=&wait=`` — a job, its rendered text
  (a dossier page once finished) and how many pages the dossier has.
- ``POST /api/documentation-search/research/{id}/continue``   — answer what the job asked,
  or resume an interrupted one.
- ``POST /api/documentation-search/research/{id}/cancel``

A job runs on the server whatever the request does: ``wait`` only holds the
response open (up to :func:`jobs.wait_cap`) so a caller can long-poll instead
of spinning, and a dropped connection never cancels the job.

The profile is always the caller's own (``request.user.username``); another
profile's job id is simply not found. A job spends the profile's own research
model group and token budget (the Documentation Search tool variables, which the
runner reads when none are passed), as when the agent starts it. Jobs started
here belong to no
conversation, so nothing is injected into a chat when they finish, and — as in
:mod:`app.api.documents_query` — no citations are registered: there is no
conversation to register them in. The text is rendered whole, not cut to the
agent's tool-result budget.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Dict, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.api._auth import require_auth
from app.documents.research.errors import InvalidRequest, JobNotFound, ResearchError
from app.documents.research.types import (
    ACTIVE,
    DOMAIN_GENERAL,
    DOMAINS,
    FINAL,
    MODE_ANALYZE,
    MODES,
    WAITING,
)
from app.utils.logger import logger

# The agent's tool takes the same cap; a question longer than this is a
# document, and belongs in the scope instead.
MAX_QUESTION_CHARS = 4000  # the job API's own limit (jobs.MAX_QUESTION_CHARS)
MAX_ANSWERS = 20
MAX_ANSWER_CHARS = 2000
# Retention keeps the newest 50 jobs per profile; a longer list has nothing more.
MAX_LIST_LIMIT = 50
# Job ids are 12 hex characters; anything much longer is not one.
_MAX_JOB_ID = 64


def _jobs() -> Any:
    """The job runner module, imported on first use (it pulls in storage and
    the model manager). A seam for tests."""
    from app.documents.research import jobs

    return jobs


def _render_mod() -> Any:
    from app.documents.research import render

    return render


def _profile(request: Request) -> str:
    return getattr(request.user, "username", "") or ""


async def _json_body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _error(exc: ResearchError) -> JSONResponse:
    return JSONResponse(exc.to_dict(), status_code=exc.status)


def _invalid_filter(field: str, message: str) -> JSONResponse:
    # Same code the query API uses for a bad ``filters``, naming which scope.
    return JSONResponse({"error": "InvalidFilter", "message": f"{field}: {message}"}, status_code=400)


# ── request parsing ────────────────────────────────────────────────────────


def _number(raw: Any, name: str) -> Optional[float]:
    """A number from a query string or a JSON body; None when absent."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise InvalidRequest(f"{name} must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise InvalidRequest(f"{name} must be a number") from None
    if not math.isfinite(value):
        raise InvalidRequest(f"{name} must be a finite number")
    return value


def _wait(raw: Any) -> float:
    """Seconds to hold the response, clamped to [0, wait_cap()]: a caller
    asking for longer than the server allows gets the cap, not an error."""
    value = _number(raw, "wait")
    if not value or value <= 0:
        return 0.0
    return min(value, float(_jobs().wait_cap()))


def _page(raw: Any) -> int:
    value = _number(raw, "page")
    return max(1, int(value)) if value is not None else 1


def _limit(raw: Any) -> int:
    value = _number(raw, "limit")
    return max(1, min(MAX_LIST_LIMIT, int(value))) if value is not None else 20


def _choice(raw: Any, name: str, allowed: tuple[str, ...], default: str) -> str:
    if raw is None or raw == "":
        return default
    value = str(raw).strip().lower()
    if value not in allowed:
        raise InvalidRequest(f"{name} must be one of {', '.join(allowed)}")
    return value


def _scope(raw: Any, field: str) -> Optional[Dict[str, Any]]:
    """Validate a scope with the tool's own filter parser, so a typo in a
    filter key is a 400 here rather than a job that silently reads the whole
    index. The dict itself travels on unchanged: the job parses it again."""
    from app.documents.query.filters import FilterError, parse_filters

    if raw is None or raw == {}:
        return None
    try:
        parse_filters(raw)
    except FilterError as exc:
        raise _BadFilter(field, str(exc)) from None
    return dict(raw)


class _BadFilter(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def _spec(body: Dict[str, Any]) -> Any:
    from app.documents.research.context import ResearchSpec

    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        raise InvalidRequest("question is required: what the research should answer")
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        raise InvalidRequest(
            f"question is {len(question)} characters; the limit is {MAX_QUESTION_CHARS}. "
            "Put long material in a file and name it in the scope."
        )
    return ResearchSpec(
        question=question,
        mode=_choice(body.get("mode"), "mode", MODES, MODE_ANALYZE),
        domain=_choice(body.get("domain"), "domain", DOMAINS, DOMAIN_GENERAL),
        scope=_scope(body.get("scope"), "scope"),
        reference_scope=_scope(body.get("reference_scope"), "reference_scope"),
    )


def _answers(raw: Any) -> Optional[Dict[str, Any]]:
    """What the user answered: ``{key: string | boolean | number}``, the
    shape the tool leaf accepts. Keys are the clarification's answer keys."""
    if raw is None or raw == {}:
        return None
    if not isinstance(raw, dict):
        raise InvalidRequest("answers must be an object of key → value")
    if len(raw) > MAX_ANSWERS:
        raise InvalidRequest(f"at most {MAX_ANSWERS} answers")
    out: Dict[str, Any] = {}
    for key, value in raw.items():
        key = str(key).strip()
        if not key:
            raise InvalidRequest("answers: empty key")
        if not isinstance(value, (str, bool, int, float)):
            raise InvalidRequest(f"answers.{key} must be a string, boolean or number")
        if isinstance(value, str) and len(value) > MAX_ANSWER_CHARS:
            raise InvalidRequest(f"answers.{key} is longer than {MAX_ANSWER_CHARS} characters")
        out[key] = value
    return out


def _job_id(request: Request) -> str:
    job_id = str(request.path_params.get("job_id") or "").strip()
    if not job_id or len(job_id) > _MAX_JOB_ID:
        raise JobNotFound(f"No research job {job_id!r}.")
    return job_id


# ── responses ──────────────────────────────────────────────────────────────


def _deliverable(view: Any) -> bool:
    return view.status in FINAL or view.status in WAITING


def _render(profile: str, view: Any, page: int) -> tuple[str, int]:
    """The job's text (the dossier page ``page`` once finished) and its page
    count. Blocking (rendering walks the dossier): run in a worker thread.
    ``db=None``: the index is only consulted to register the printed tokens as
    citations, and REST has no conversation to register them in."""
    from app.tools.builtin.documentation_search import render_context

    render = _render_mod()
    rendered = render.render_job(view, ctx=render_context(profile, budgeted=False), db=None, page=page)
    return rendered.text, len(render.dossier_pages(view))


async def _respond(profile: str, view: Any, *, page: int = 1, status: int = 200,
                   with_pages: bool = False) -> JSONResponse:
    text, pages = await asyncio.to_thread(_render, profile, view, page)
    if _deliverable(view) and not view.conversation_id:
        # The requester now has this state in hand: claim its delivery. Only
        # for a job with no conversation (started here or from the CLI): a
        # job the agent started in a chat still owes that chat its result,
        # however many times someone looks at it from the terminal. A job
        # whose row went meanwhile has nothing left to deliver; the answer
        # already rendered still stands.
        try:
            await asyncio.to_thread(_jobs().mark_collected, profile, view.job_id)
        except JobNotFound:
            logger.debug(f"[documents] research job {view.job_id} vanished before its delivery was claimed")
    payload: Dict[str, Any] = {"job": view.to_dict(), "text": text}
    if with_pages:
        payload["pages"] = pages
    return JSONResponse(payload, status_code=status)


async def _settle(profile: str, view: Any, wait: float) -> Any:
    """Hold the response up to ``wait`` seconds while the job is running.
    ``wait_job`` waits on the job's event, never on its task, so a caller
    that gives up (or disconnects) never cancels the job."""
    if wait > 0 and view.status in ACTIVE:
        return await _jobs().wait_job(profile=profile, job_id=view.job_id, timeout=wait)
    return view


# ── routes ─────────────────────────────────────────────────────────────────


def get_documents_research_routes() -> List[Route]:
    async def guarded(request: Request, handler) -> JSONResponse:
        denied = require_auth(request)
        if denied is not None:
            return denied
        try:
            return await handler(_profile(request))
        except ResearchError as exc:
            return _error(exc)
        except _BadFilter as exc:
            return _invalid_filter(exc.field, exc.message)

    async def handle_start(request: Request) -> JSONResponse:
        async def run(profile: str) -> JSONResponse:
            body = await _json_body(request)
            spec = _spec(body)
            wait = _wait(body.get("wait"))
            # No ``variables``: the runner reads the profile's saved ones.
            view = await _jobs().start_job(profile=profile, spec=spec, conversation_id=None)
            view = await _settle(profile, view, wait)
            return await _respond(profile, view, status=202)

        return await guarded(request, run)

    async def handle_list(request: Request) -> JSONResponse:
        async def run(profile: str) -> JSONResponse:
            limit = _limit(request.query_params.get("limit"))
            views = await asyncio.to_thread(_jobs().list_jobs, profile, limit=limit)
            return JSONResponse({"jobs": [v.to_dict(with_dossier=False) for v in views]})

        return await guarded(request, run)

    async def handle_get(request: Request) -> JSONResponse:
        async def run(profile: str) -> JSONResponse:
            job_id = _job_id(request)
            q = request.query_params
            page = _page(q.get("page"))
            wait = _wait(q.get("wait"))
            view = await asyncio.to_thread(_jobs().get_job, profile, job_id)
            view = await _settle(profile, view, wait)
            return await _respond(profile, view, page=page, with_pages=True)

        return await guarded(request, run)

    async def handle_continue(request: Request) -> JSONResponse:
        async def run(profile: str) -> JSONResponse:
            job_id = _job_id(request)
            body = await _json_body(request)
            answers = _answers(body.get("answers"))
            wait = _wait(body.get("wait"))
            view = await _jobs().continue_job(profile=profile, job_id=job_id, answers=answers)
            view = await _settle(profile, view, wait)
            return await _respond(profile, view)

        return await guarded(request, run)

    async def handle_cancel(request: Request) -> JSONResponse:
        async def run(profile: str) -> JSONResponse:
            job_id = _job_id(request)
            view = await _jobs().cancel_job(profile=profile, job_id=job_id)
            return await _respond(profile, view)

        return await guarded(request, run)

    return [
        Route("/api/documentation-search/research", handle_start, methods=["POST"]),
        Route("/api/documentation-search/research", handle_list, methods=["GET"]),
        Route("/api/documentation-search/research/{job_id}", handle_get, methods=["GET"]),
        Route("/api/documentation-search/research/{job_id}/continue", handle_continue, methods=["POST"]),
        Route("/api/documentation-search/research/{job_id}/cancel", handle_cancel, methods=["POST"]),
    ]


__all__ = ["MAX_QUESTION_CHARS", "get_documents_research_routes"]
