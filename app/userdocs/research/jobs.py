"""The research job runner: start, long-poll, continue, cancel, deliver.

A research job reads many files and makes many model calls, so it outlives
the tool call that started it. It runs as one :class:`asyncio.Task` per run on
the server loop, checkpoints to its row in ``userdoc_research_jobs`` after
every file or issue, and reports back to the conversation that asked for it.

**Waiting never cancels.** :func:`wait_job` waits on the run's
:class:`asyncio.Event`, never on the task: a tool call that times out, or a
turn the user stops, leaves the job running. Only :func:`cancel_job` (and the
feature going off, and the index being deleted) sets the run's cancel event.

**One running job per profile.** A second :func:`start_job` for a profile that
has one running raises :class:`ResearchBusy` naming it; another profile is
unaffected. The check and the insert happen under one lock with the run
registered in memory, so two racing starts cannot both win.

**Resumable.** A run is the pipeline called with the job's saved ``state``.
When it stops to ask (``needs_*``), or the server restarts under it
(``interrupted``), :func:`continue_job` merges the user's answers and calls
the pipeline again with that state; the pipeline skips what it already did.
The time limit counts running time only, summed across runs.

**Delivered exactly once per state.** Every transition into a state the
requester must hear about (``complete``/``partial``/``failed``, ``needs_*``,
``interrupted`` at boot) bumps the row's ``rev``; showing that state claims it
(``delivered_rev = rev``, a compare-and-set). The tool marks a state collected
when it returned it to the agent; otherwise the job injects one turn into its
conversation — at once when the conversation is idle, at the end of the live
turn otherwise (:func:`on_turn_end`), or at the next boot (:func:`boot_recover`).
A cancellation is never announced: whoever cancelled already knows.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine

from app.userdocs.research import context as C
from app.userdocs.research.context import (
    BudgetExceeded,
    Cancelled,
    NeedsInput,
    ProgressSink,
    ResearchContext,
    ResearchLLM,
    ResearchSpec,
    TimeUp,
    Usage,
)
from app.userdocs.research.errors import InvalidRequest, JobNotFound, ResearchBusy, ResearchUnavailable
from app.userdocs.research.types import (
    ACTIVE,
    CANCELLED,
    COMPLETE,
    DOMAIN_GENERAL,
    DOMAINS,
    FAILED,
    FINAL,
    INTERRUPTED,
    MODE_ANALYZE,
    MODE_COMPILE,
    MODES,
    NEEDS_CLARIFICATION,
    NEEDS_CONFIRMATION,
    PARTIAL,
    QUEUED,
    READ_FULL,
    RUNNING,
    WAITING,
    Dossier,
    JobView,
    dossier_from_dict,
)
from app.utils.logger import logger

TOOL_ID = "user_documents"
# The tool's variable names (``app.tools.builtin.user_documents.Var``), spelled
# out so importing the runner does not import the tool.
VAR_MODEL_GROUP = "RESEARCH_MODEL_GROUP"
VAR_TOKEN_BUDGET = "RESEARCH_TOKEN_BUDGET"
DEFAULT_MODEL_GROUP = "high"
DEFAULT_BUDGET = 250_000
MODEL_GROUPS = ("high", "low")

# Retention: the newest jobs per profile, never counting away a running or
# waiting one.
KEEP_JOBS = 50
# The long-poll ceiling: under the MCP tool-call timeout with room to render.
MAX_WAIT_S = 240.0
WAIT_MARGIN_S = 45.0
# A cancel waits this long for the pipeline to notice (it checks between
# steps) before the task is cancelled outright, mid model call.
CANCEL_GRACE_S = 3.0
# The last steps a JobView carries.
PROGRESS_STEPS = 12
MAX_QUESTION_CHARS = 4000

_REASON_BUDGET = "stopped at the token budget"
_REASON_TIME = "stopped at the time limit"


# ── in-memory state ───────────────────────────────────────────────────────


@dataclass(eq=False)
class _Run:
    """One run of one job in this process. Gone when the run ends; the row
    is the durable half."""

    job_id: str
    profile: str
    question: str
    conversation_id: str | None
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    # Set when the run is over (any outcome). What waiters wait on.
    done: asyncio.Event = field(default_factory=asyncio.Event)
    loop: asyncio.AbstractEventLoop | None = None
    task: asyncio.Task | None = None
    sink: ProgressSink | None = None
    llm: ResearchLLM | None = None
    cancel_reason: str | None = None
    # Callers waiting on this run that will show its outcome to the agent and
    # claim it themselves (the tool's long-poll): while one waits, the
    # outcome is theirs to report, not an injected turn's.
    collectors: int = 0
    # A collector hold taken at launch (``start_job``/``continue_job`` with
    # ``collect=True``), before the run could reach an outcome; the caller's
    # ``wait_job(collect=True)`` takes it over instead of adding its own.
    prehold: bool = False


# job_id -> the run executing it in this process.
_runs: dict[str, _Run] = {}
# Held across "is the profile busy?" + "insert/restart the job" + "register the
# run" (a worker thread), so two starts for one profile cannot both pass.
_start_lock = threading.Lock()
# Conversations that may be owed a result: the turn-end hook's cheap gate, so
# an ordinary turn end costs no query. Lost on restart by design — the boot
# sweep delivers everything owed.
_pending_convs: set[str] = set()
# Strong references to fire-and-forget tasks (the loop only keeps weak ones).
_background: set[asyncio.Task] = set()
# The server loop, for cancellations requested from worker threads.
_loop: asyncio.AbstractEventLoop | None = None


class _RowGone(Cancelled):
    """The job's row was deleted under the run (its conversation was deleted,
    the index was purged): stop, write nothing, deliver nothing."""


def _storage():
    from app.storage.userdocs_research_storage import get_userdocs_research_storage

    return get_userdocs_research_storage()


def _now_ms() -> float:
    return time.time() * 1000


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Run ``coro`` in the background on the running loop, keeping a strong
    reference until it is done (the server's boot hook uses this too)."""
    task = asyncio.get_running_loop().create_task(coro, name=name)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


def _capture_loop() -> None:
    global _loop
    try:
        _loop = asyncio.get_running_loop()
    except RuntimeError:
        pass


def wait_cap() -> float:
    """How long a caller may long-poll a job in one call: under the MCP
    tool-call timeout with a margin to render the result, at most 240 s,
    never below 5 s."""
    from app.config.settings import BaseConfig

    try:
        timeout = float(BaseConfig.MCP_TOOL_CALL_TIMEOUT or 300)
    except (TypeError, ValueError):
        timeout = 300.0
    return max(5.0, min(MAX_WAIT_S, timeout - WAIT_MARGIN_S))


def artifacts_root(profile: str) -> str:
    """``<SYSTEM_DIR>/<profile>/exports/research`` — under the profile's own
    ``exports``, which the file API serves to that profile only."""
    from app.config.settings import BaseConfig

    return os.path.join(BaseConfig.CREMIND_SYSTEM_DIR, profile, "exports", "research")


def artifacts_dir(profile: str, job_id: str) -> str:
    return os.path.join(artifacts_root(profile), job_id)


def _remove_artifacts(profile: str, job_ids: list[str]) -> None:
    for job_id in job_ids:
        shutil.rmtree(artifacts_dir(profile, job_id), ignore_errors=True)


# ── seams (tests replace these) ───────────────────────────────────────────


def _open_engine(profile: str) -> Any:
    """The profile's query engine, or :class:`ResearchUnavailable` saying why
    there is none. Blocking."""
    from app.userdocs.query import open_engine

    access = open_engine(profile)
    if access.engine is None:
        raise ResearchUnavailable(
            access.message or "User Document Search is not available for this profile.",
            status_code=access.code,
        )
    return access.engine


def _create_llm(group: str, profile: str) -> Any:
    """The research model for ``group``. Blocking (reads the profile's LLM
    configuration)."""
    from app.lib.llm.model_groups import ModelGroupManager
    from app.storage import get_dynamic_config_storage

    return ModelGroupManager(get_dynamic_config_storage()).create_llm_for_group(group, profile)


def _pipeline_for(mode: str) -> Callable[[ResearchContext], Awaitable[Dossier]]:
    if mode == MODE_COMPILE:
        from app.userdocs.research.compile import run_compile

        return run_compile
    from app.userdocs.research.analyze import run_analyze

    return run_analyze


def _activity_module() -> Any:
    """The Research activity panel (:mod:`.activity`), or None when it cannot
    be loaded — a job runs without its panel rather than not at all."""
    try:
        from app.userdocs.research import activity

        return activity
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"[userdocs] research activity panel unavailable: {exc}")
        return None


def _render(profile: str, view: JobView) -> tuple[str, list[Any]]:
    """The job as the agent reads it (``render.render_job``, sized to the
    profile's tool-result budget) and the citations it printed. Blocking."""
    from app.tools.builtin.user_documents import render_context
    from app.userdocs.citations import profile_index
    from app.userdocs.research import render as R

    with profile_index(profile) as db:
        rendered = R.render_job(view, ctx=render_context(profile, budgeted=True), db=db)
    return rendered.text, list(rendered.citations or [])


def _configured_variables(profile: str) -> dict[str, Any]:
    """The profile's saved Tool Variables for User Documents (the adapter
    hands the same values to the tool as ``_variables``)."""
    try:
        from app.storage import get_tool_storage
        from app.tools.config_manager import ToolConfigManager

        return dict(ToolConfigManager(get_tool_storage()).get_variables(TOOL_ID, profile) or {})
    except Exception as exc:  # noqa: BLE001 — the defaults are a fine answer
        logger.debug(f"[userdocs] could not read research settings for {profile}: {exc}")
        return {}


def _settings(profile: str, variables: dict[str, Any] | None) -> tuple[str, int]:
    """``(model_group, token_budget)`` from the tool's variables (the saved
    ones when the caller has none). Blocking when it has to read them."""
    if variables is None:
        variables = _configured_variables(profile)
    group = str(variables.get(VAR_MODEL_GROUP) or DEFAULT_MODEL_GROUP).strip().lower()
    if group not in MODEL_GROUPS:
        group = DEFAULT_MODEL_GROUP
    try:
        budget = int(float(variables.get(VAR_TOKEN_BUDGET) or DEFAULT_BUDGET))
    except (TypeError, ValueError):
        budget = DEFAULT_BUDGET
    return group, max(1000, budget)


# ── views ─────────────────────────────────────────────────────────────────


def _progress(run: _Run) -> dict[str, Any]:
    sink = run.sink
    if sink is None:
        return {}
    steps = list(getattr(sink, "steps", None) or [])[-PROGRESS_STEPS:]
    return {
        "phase": getattr(sink, "phase", None),
        "done": int(getattr(sink, "done", 0) or 0),
        "total": int(getattr(sink, "total", 0) or 0),
        "steps": [
            {"id": s.get("id"), "label": s.get("label"), "detail": s.get("detail"), "status": s.get("status")}
            for s in steps if isinstance(s, dict)
        ],
    }


def _view(row: dict[str, Any], *, with_dossier: bool = True) -> JobView:
    run = _runs.get(row["id"])
    live = run is not None and run.profile == row["profile"] and row.get("status") in ACTIVE
    tokens_in = int(row.get("tokens_in") or 0)
    tokens_out = int(row.get("tokens_out") or 0)
    phase = row.get("phase")
    progress: dict[str, Any] = {}
    if live:
        progress = _progress(run)
        if run.llm is not None:
            tokens_in, tokens_out = run.llm.spent.tokens_in, run.llm.spent.tokens_out
        if progress.get("phase"):
            phase = progress["phase"]
    dossier = None
    if with_dossier:
        try:
            dossier = dossier_from_dict(row.get("dossier"))
        except Exception as exc:  # noqa: BLE001 — a malformed checkpoint must not hide the job
            logger.warning(f"[userdocs] research job {row['id']}: unreadable dossier ({exc})")
        if dossier is not None:
            # The row is authoritative: a late checkpoint cannot contradict it.
            dossier.status = row["status"]
            dossier.tokens_in, dossier.tokens_out = tokens_in, tokens_out
    return JobView(
        job_id=row["id"], profile=row["profile"], status=row["status"], mode=row["mode"],
        domain=row["domain"], question=row["question"], conversation_id=row.get("conversation_id"),
        phase=phase, created_at=row.get("created_at"), updated_at=row.get("updated_at"),
        finished_at=row.get("finished_at"), tokens_in=tokens_in, tokens_out=tokens_out,
        budget=int(row.get("budget") or 0), error=row.get("error"), progress=progress, dossier=dossier,
    )


def _get_row(profile: str, job_id: str) -> dict[str, Any]:
    row = _storage().get(profile, str(job_id or "")) if job_id else None
    if row is None:
        raise JobNotFound(f"No research job {job_id!r} for this profile.")
    return row


def get_job(profile: str, job_id: str) -> JobView:
    """The job as it is now. Blocking; :class:`JobNotFound` for another
    profile's job id."""
    return _view(_get_row(profile, job_id))


def list_jobs(profile: str, *, limit: int = 20, conversation_id: str | None = None) -> list[JobView]:
    """The profile's jobs, newest first, without their dossiers. Blocking."""
    limit = max(1, min(200, int(limit or 20)))
    return [_view(r, with_dossier=False) for r in _storage().list(profile, limit=limit, conversation_id=conversation_id)]


def mark_collected(profile: str, job_id: str) -> None:
    """The caller just showed the job's current state to its requester (the
    tool returned it to the agent, REST returned it): claim it, so no turn is
    injected for it. Blocking."""
    if not _storage().mark_collected(profile, str(job_id or "")):
        raise JobNotFound(f"No research job {job_id!r} for this profile.")


def resolve_conversation_id(profile: str, context_id: str | None) -> str | None:
    """A tool's ``_context_id`` → the conversation it belongs to, or None (a
    web conversation's context id is its id; a channel/A2A one is resolved
    through its row, within this profile). Blocking."""
    if not context_id:
        return None
    try:
        from app.storage.userdocs_citations_storage import get_userdocs_citations_storage

        return get_userdocs_citations_storage().resolve_conversation(profile, context_id)
    except Exception as exc:  # noqa: BLE001 — a job without a conversation still runs
        logger.warning(f"[userdocs] could not resolve conversation for {profile}: {exc}")
        return None


def has_live_run(profile: str) -> bool:
    """Whether this process is running a job for ``profile``."""
    return any(r.profile == profile for r in list(_runs.values()))


# ── validation ────────────────────────────────────────────────────────────


def _validate(spec: ResearchSpec) -> ResearchSpec:
    if not isinstance(spec, ResearchSpec):
        raise InvalidRequest("A research request needs a question.")
    question = (spec.question or "").strip() if isinstance(spec.question, str) else ""
    if not question:
        raise InvalidRequest("Pass `question`: what to research in the user's documents.")
    if len(question) > MAX_QUESTION_CHARS:
        raise InvalidRequest(f"The question is too long (at most {MAX_QUESTION_CHARS} characters).")
    mode = str(spec.mode or MODE_ANALYZE).strip().lower()
    if mode not in MODES:
        raise InvalidRequest(f"Unknown mode {spec.mode!r}; use one of: {', '.join(MODES)}.")
    domain = str(spec.domain or DOMAIN_GENERAL).strip().lower()
    if domain not in DOMAINS:
        raise InvalidRequest(f"Unknown domain {spec.domain!r}; use one of: {', '.join(DOMAINS)}.")
    from app.userdocs.query.filters import FilterError, parse_filters

    scopes: dict[str, dict[str, Any] | None] = {}
    for name in ("scope", "reference_scope"):
        raw = getattr(spec, name)
        if raw in (None, {}):
            scopes[name] = None
            continue
        if not isinstance(raw, dict):
            raise InvalidRequest(f"`{name}` must be an object of filters.")
        try:
            parse_filters(raw)
        except FilterError as exc:
            raise InvalidRequest(f"`{name}`: {exc}") from exc
        scopes[name] = dict(raw)
    return ResearchSpec(question=question, mode=mode, domain=domain, scope=scopes["scope"],
                        reference_scope=scopes["reference_scope"])


def _check_answers(answers: Any) -> dict[str, Any]:
    if answers is None:
        return {}
    if not isinstance(answers, dict):
        raise InvalidRequest("`answers` must be an object of key → value.")
    out: dict[str, Any] = {}
    for key, value in answers.items():
        if not isinstance(key, str) or not key:
            raise InvalidRequest("`answers` keys must be non-empty strings.")
        if value is not None and not isinstance(value, (str, bool, int, float)):
            raise InvalidRequest(f"`answers.{key}` must be a string, number or boolean.")
        out[key] = value
    return out


# ── starting a run ────────────────────────────────────────────────────────


def _claim_slot(profile: str, run: _Run, write: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    """Under the start lock: refuse when the profile is busy, then ``write``
    (insert or restart the row) and register ``run``. Blocking.

    A running row with no run in this process belongs to a process that died
    before the boot sweep saw it; it is marked interrupted — quietly, since the
    user is starting something new — instead of blocking the profile forever.
    """
    storage = _storage()
    with _start_lock:
        for other in list(_runs.values()):
            if other.profile == profile and other.job_id != run.job_id:
                raise ResearchBusy(other.job_id, other.question)
        for stale in storage.active_for_profile(profile):
            if stale["id"] == run.job_id or stale["id"] in _runs:
                continue
            logger.warning(f"[userdocs] research job {stale['id']} of {profile} had no runner; interrupted")
            storage.bump_rev(profile, stale["id"], quiet=True, expect=ACTIVE, status=INTERRUPTED)
        row = write()
        if row is not None:
            _runs[run.job_id] = run
        return row


def _launch(run: _Run, row: dict[str, Any], engine: Any) -> None:
    run.loop = asyncio.get_running_loop()
    run.task = spawn(_runner(run, row, engine), name=f"userdocs-research:{run.job_id}")


async def start_job(
    *,
    profile: str,
    spec: ResearchSpec,
    conversation_id: str | None,
    run_id: str | None = None,
    variables: dict[str, Any] | None = None,
    collect: bool = False,
) -> JobView:
    """Start a research job and return at once (``queued``); follow it with
    :func:`wait_job`. :class:`InvalidRequest` for a bad request,
    :class:`ResearchUnavailable` when the profile has no usable index,
    :class:`ResearchBusy` when it already has a job running.

    ``collect``: the caller will follow with ``wait_job(collect=True)`` and
    report the outcome itself (the tool) — the hold is taken before the run
    starts, so even an outcome reached at once is not also injected."""
    _capture_loop()
    spec = _validate(spec)
    engine = await asyncio.to_thread(_open_engine, profile)
    group, budget = await asyncio.to_thread(_settings, profile, variables)
    job_id = uuid.uuid4().hex[:12]
    dossier = Dossier(job_id=job_id, mode=spec.mode, domain=spec.domain, question=spec.question, status=QUEUED)
    run = _Run(job_id=job_id, profile=profile, question=spec.question, conversation_id=conversation_id)
    if collect:
        run.collectors, run.prehold = 1, True
    storage = _storage()

    def write() -> dict[str, Any]:
        return storage.create(
            job_id=job_id, profile=profile, conversation_id=conversation_id, run_id=run_id,
            status=QUEUED, mode=spec.mode, domain=spec.domain, question=spec.question,
            scope=spec.scope, reference_scope=spec.reference_scope, answers={}, state={},
            dossier=dossier.to_dict(), model_group=group, budget=budget,
        )

    row = await asyncio.to_thread(_claim_slot, profile, run, write)
    _launch(run, row, engine)
    logger.info(f"[userdocs] research job {job_id} started for {profile} ({spec.mode}/{spec.domain}, "
                f"{group}, budget {budget})")
    try:
        pruned = await asyncio.to_thread(storage.prune, profile, KEEP_JOBS)
        if pruned:
            await asyncio.to_thread(_remove_artifacts, profile, pruned)
    except Exception as exc:  # noqa: BLE001 — retention is housekeeping
        logger.warning(f"[userdocs] research retention prune failed for {profile}: {exc}")
    return _view(row)


async def continue_job(
    *,
    profile: str,
    job_id: str,
    answers: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
    collect: bool = False,
) -> JobView:
    """Resume a job waiting for an answer (``needs_*``) or cut short by a
    restart (``interrupted``) from its checkpoint, with ``answers`` merged into
    what it was told before. A running or finished job is returned as it is
    (the answers are ignored). ``answers["budget"]`` may raise the job's token
    budget (a ``budget`` clarification)."""
    _capture_loop()
    answers = _check_answers(answers)
    row = await asyncio.to_thread(_get_row, profile, job_id)
    job_id = row["id"]
    if row["status"] not in WAITING or job_id in _runs:
        return _view(row)
    engine = await asyncio.to_thread(_open_engine, profile)
    merged = {**(row.get("answers") or {}), **answers}
    budget = int(row.get("budget") or DEFAULT_BUDGET)
    try:
        raised = int(float(answers.get("budget"))) if answers.get("budget") is not None else 0
    except (TypeError, ValueError):
        raised = 0
    budget = max(budget, raised)
    group = row.get("model_group")
    if group not in MODEL_GROUPS:
        group, _ = await asyncio.to_thread(_settings, profile, variables)
    dossier = row.get("dossier")
    if isinstance(dossier, dict) and dossier:
        dossier = {**dossier, "clarification": None}
    run = _Run(job_id=job_id, profile=profile, question=row["question"], conversation_id=row.get("conversation_id"))
    if collect:
        run.collectors, run.prehold = 1, True  # see start_job
    storage = _storage()

    def write() -> dict[str, Any] | None:
        if not storage.update(profile, job_id, expect=WAITING, status=QUEUED, answers=merged, budget=budget,
                              model_group=group, dossier=dossier, error=None, finished_at=None):
            return None  # continued or cancelled concurrently
        # The waiting state is being answered: it is owed to nobody now.
        storage.mark_collected(profile, job_id)
        return storage.get(profile, job_id)

    restarted = await asyncio.to_thread(_claim_slot, profile, run, write)
    if restarted is None:
        return await asyncio.to_thread(get_job, profile, job_id)
    _launch(run, restarted, engine)
    logger.info(f"[userdocs] research job {job_id} of {profile} resumed from its checkpoint "
                f"(answers: {sorted(answers)})")
    return _view(restarted)


async def _wait_event(event: asyncio.Event, timeout: float) -> bool:
    if event.is_set():
        return True
    if timeout <= 0:
        return False
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        return False


async def wait_job(*, profile: str, job_id: str, timeout: float, collect: bool = False) -> JobView:
    """The job's view once it stops running, or after ``timeout`` seconds,
    whichever is first. Waits on the run's event, never on its task, so a
    caller that is cancelled or times out leaves the job running.

    ``collect``: the caller will show the outcome to the agent and claim it
    (:func:`mark_collected`) — the tool's long-poll. While it waits, a run
    that ends does not also inject its outcome as a turn; if the caller then
    fails to claim it, the turn's end still reports it."""
    # Hold before the first await: the run may reach its outcome during it.
    held = _runs.get(str(job_id or "")) if collect else None
    if held is not None and held.profile == profile:
        if held.prehold:
            held.prehold = False  # take over the hold start/continue took
        else:
            held.collectors += 1
    else:
        held = None
    try:
        row = await asyncio.to_thread(_get_row, profile, job_id)
        run = _runs.get(row["id"])
        if run is not None and run.profile == profile and row["status"] in ACTIVE and (timeout or 0) > 0:
            await _wait_event(run.done, float(timeout))
            row = await asyncio.to_thread(_get_row, profile, job_id)
        return _view(row)
    finally:
        if held is not None:
            held.collectors -= 1


async def _cancel_run(run: _Run, reason: str) -> None:
    """Ask a run to stop; if it has not within the grace period (it is in the
    middle of a model call), cancel its task outright."""
    run.cancel_reason = run.cancel_reason or reason
    run.cancel_event.set()
    task = run.task
    if task is None or task.done():
        return
    if not await _wait_event(run.done, CANCEL_GRACE_S):
        task.cancel()
        await _wait_event(run.done, 5.0)


async def cancel_job(*, profile: str, job_id: str) -> JobView:
    """Cancel a running or waiting job; a finished one is returned as it is.
    The dossier so far is kept."""
    row = await asyncio.to_thread(_get_row, profile, job_id)
    job_id = row["id"]
    run = _runs.get(job_id)
    if run is not None and run.profile == profile:
        await _cancel_run(run, "cancelled by the user")
    elif row["status"] in WAITING or row["status"] in ACTIVE:
        dossier = row.get("dossier")
        if isinstance(dossier, dict) and dossier:
            dossier = {**dossier, "clarification": None, "status": CANCELLED}
        await asyncio.to_thread(
            _storage().bump_rev, profile, job_id, quiet=True, expect=WAITING | ACTIVE,
            status=CANCELLED, phase=None, dossier=dossier, finished_at=_now_ms(),
        )
        logger.info(f"[userdocs] research job {job_id} of {profile} cancelled while {row['status']}")
    return await asyncio.to_thread(get_job, profile, job_id)


async def cancel_profile_jobs(profile: str, reason: str) -> int:
    """Stop every job this process runs for ``profile`` (the feature went
    off, the index is being deleted). Jobs waiting for an answer are left
    for the user to continue or cancel. Returns how many were stopped."""
    runs = [r for r in list(_runs.values()) if r.profile == profile]
    for run in runs:
        await _cancel_run(run, reason)
    if runs:
        logger.info(f"[userdocs] stopped {len(runs)} research job(s) of {profile}: {reason}")
    return len(runs)


def signal_cancel(profile: str, reason: str) -> int:
    """:func:`cancel_profile_jobs` for callers on a worker thread (the User
    Document Search engine): schedules the cancellation on the server loop
    and returns without waiting. Returns how many runs were signalled."""
    runs = [r for r in list(_runs.values()) if r.profile == profile]
    for run in runs:
        loop = run.loop or _loop
        if loop is None or loop.is_closed():
            run.cancel_reason = run.cancel_reason or reason
            run.cancel_event.set()  # not started yet: nothing awaits it
            continue
        try:
            asyncio.run_coroutine_threadsafe(_cancel_run(run, reason), loop)
        except RuntimeError:
            run.cancel_event.set()
    return len(runs)


def purge_profile(profile: str) -> int:
    """Purge set P: stop the profile's running jobs, delete every job row
    and every artifact the jobs wrote. Callable from any thread. A run that
    was mid-flight notices its row is gone and stops without a word."""
    signal_cancel(profile, "the document index was deleted")
    removed = 0
    try:
        removed = _storage().delete_profile(profile)
    finally:
        shutil.rmtree(artifacts_root(profile), ignore_errors=True)
    if removed:
        logger.info(f"[userdocs] {profile}: deleted {removed} research job(s)")
    return removed


# ── the runner ────────────────────────────────────────────────────────────


class _RunSink(ProgressSink):
    """The sink a pipeline writes to: forwards everything to the one the
    Research activity panel reads (or a plain one), and tells the runner when
    a phase ends — usage is written per phase.

    Holds no state of its own (``ProgressSink.__init__`` is deliberately not
    called): ``steps``/``phase``/``done``/``total`` read through, so the job's
    view and the panel can never disagree."""

    def __init__(self, inner: ProgressSink, on_phase_end: Callable[[str], None]) -> None:
        self._inner = inner
        self._on_phase_end = on_phase_end

    @property
    def steps(self) -> list[dict[str, Any]]:  # type: ignore[override]
        return getattr(self._inner, "steps", [])

    @property
    def phase(self) -> str | None:  # type: ignore[override]
        return getattr(self._inner, "phase", None)

    @property
    def done(self) -> int:  # type: ignore[override]
        return int(getattr(self._inner, "done", 0) or 0)

    @property
    def total(self) -> int:  # type: ignore[override]
        return int(getattr(self._inner, "total", 0) or 0)

    def set_phase(self, name: str) -> None:
        previous = self.phase
        self._inner.set_phase(name)
        if previous and previous != name:
            self._on_phase_end(previous)

    def add_step(self, *, kind: str, label: str, detail: str | None = None, status: str = "running") -> str:
        return self._inner.add_step(kind=kind, label=label, detail=detail, status=status)

    def resolve_step(self, step_id: str, *, status: str, detail_suffix: str | None = None) -> None:
        self._inner.resolve_step(step_id, status=status, detail_suffix=detail_suffix)

    def set_progress(self, done: int, total: int) -> None:
        self._inner.set_progress(done, total)


def _json_copy(value: Any) -> Any:
    """A deep, JSON-only copy taken on the loop: the checkpoint written from
    a worker thread must not be the dict the pipeline keeps mutating, and a
    value that cannot round-trip must fail here, loudly, not on resume."""
    return json.loads(json.dumps(value))


def _summary(d: Dossier) -> str:
    read = sum(1 for r in d.coverage if r.read == READ_FULL)
    parts = [f"{read}/{len(d.coverage)} files read"] if d.coverage else []
    findings = len(d.facts) + sum(len(i.findings) for i in d.issues)
    if findings:
        parts.append(f"{findings} findings")
    if d.compiled is not None:
        parts.append(f"{len(d.compiled.rows)} rows compiled")
    return ", ".join(parts)


def _clip(value: Any, n: int) -> str | None:
    return str(value)[:n] if value else None


async def _open_activity(run: _Run, row: dict[str, Any]) -> tuple[ProgressSink, Any]:
    """The panel (for a job with a conversation) and the sink feeding it."""
    if not run.conversation_id:
        return ProgressSink(), None
    mod = _activity_module()
    if mod is None:
        return ProgressSink(), None
    try:
        activity = await mod.ResearchActivity.start(
            conversation_id=run.conversation_id, profile=run.profile, job_id=run.job_id,
            title=(row.get("question") or "")[:140], mode=row.get("mode") or MODE_ANALYZE,
        )
        return mod.ActivityProgressSink(activity), activity
    except Exception as exc:  # noqa: BLE001 — the job runs without its panel
        logger.warning(f"[userdocs] research job {run.job_id}: activity panel failed to start: {exc}")
        return ProgressSink(), None


async def _runner(run: _Run, row: dict[str, Any], engine: Any) -> None:
    """One run of a job, from its checkpoint to a status. Whatever ends it —
    a cancel landing mid-write included — wakes its waiters and frees the
    profile's slot."""
    try:
        await _run_once(run, row, engine)
    finally:
        _forget(run)


async def _run_once(run: _Run, row: dict[str, Any], engine: Any) -> None:
    job_id, profile, conv_id = run.job_id, run.profile, run.conversation_id
    storage = _storage()
    mode = row.get("mode") or MODE_ANALYZE
    group = row.get("model_group") or DEFAULT_MODEL_GROUP
    budget = int(row.get("budget") or DEFAULT_BUDGET)
    base_elapsed = float(row.get("elapsed_s") or 0.0)
    started = time.monotonic()
    save_lock = asyncio.Lock()
    pending_usage: list[dict[str, int]] = []
    activity: Any = None

    def elapsed() -> float:
        return base_elapsed + (time.monotonic() - started)

    try:
        dossier = dossier_from_dict(row.get("dossier"))
    except Exception:  # noqa: BLE001
        dossier = None
    if dossier is None:
        dossier = Dossier(job_id=job_id, mode=mode, domain=row.get("domain") or DOMAIN_GENERAL,
                          question=row.get("question") or "", status=RUNNING)
    dossier.status, dossier.clarification = RUNNING, None
    state: dict[str, Any] = dict(row.get("state") or {})
    answers: dict[str, Any] = dict(row.get("answers") or {})

    def take_usage() -> dict[str, int]:
        # Synchronous, so a phase's record holds exactly that phase's calls.
        batch = pending_usage[:]
        pending_usage.clear()
        return {k: sum(int(u.get(k) or 0) for u in batch) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")}

    async def write_usage(sums: dict[str, int], conversation_id: str | None) -> None:
        if not any(sums.values()):
            return
        llm = run.llm
        try:
            from app.agent.usage import UsageRecord
            from app.storage import get_usage_storage

            record = UsageRecord(
                source_kind="userdocs", tool_id=TOOL_ID, label=f"Document research: {mode}",
                provider=llm.provider if llm else None, model=llm.model if llm else None,
                model_group=group, step_index=0, **sums,
            )
            await get_usage_storage().add_usage_records(
                conversation_id=conversation_id, profile=profile, records=[record.to_dict()], message_id=None,
            )
        except Exception:  # noqa: BLE001 — accounting never fails a job
            logger.exception(f"[userdocs] research job {job_id}: could not record usage")

    async def flush_usage(conversation_id: str | None = conv_id) -> None:
        await write_usage(take_usage(), conversation_id)

    def on_phase_end(_name: str) -> None:
        sums = take_usage()
        if any(sums.values()):
            spawn(write_usage(sums, conv_id), name=f"userdocs-research-usage:{job_id}")

    def on_usage(usage: dict[str, int]) -> None:
        pending_usage.append(dict(usage))
        llm = run.llm
        if activity is not None and llm is not None:
            try:
                activity.update_usage(llm.spent.tokens_in, llm.spent.tokens_out, llm.budget)
            except Exception:  # noqa: BLE001
                logger.debug("[userdocs] research activity usage update failed", exc_info=True)

    async def saver(c: ResearchContext) -> None:
        async with save_lock:
            values = {
                "state": _json_copy(c.state), "dossier": c.dossier.to_dict(), "phase": _clip(sink.phase, 64),
                "tokens_in": c.llm.spent.tokens_in, "tokens_out": c.llm.spent.tokens_out,
                "elapsed_s": elapsed(), "provider": _clip(c.llm.provider, 64), "model": _clip(c.llm.model, 128),
            }
            ok = await asyncio.to_thread(storage.update, profile, job_id, expect=ACTIVE, **values)
        if not ok:
            if not await asyncio.to_thread(storage.exists, job_id):
                run.cancel_event.set()
                raise _RowGone()
            raise Cancelled()

    ctx: ResearchContext | None = None
    status, error, note = FAILED, None, None
    sink: ProgressSink = ProgressSink()
    try:
        inner, activity = await _open_activity(run, row)
        sink = _RunSink(inner, on_phase_end)
        run.sink = sink
        if not await asyncio.to_thread(storage.update, profile, job_id, expect=ACTIVE, status=RUNNING):
            raise _RowGone()
        if run.cancel_event.is_set():
            raise Cancelled()
        try:
            raw_llm = await asyncio.to_thread(_create_llm, group, profile)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"no model is configured for research (model group {group!r}): {exc}") from exc
        run.llm = ResearchLLM(
            raw_llm, budget=budget, model_group=group, on_usage=on_usage,
            spent=Usage(int(row.get("tokens_in") or 0), int(row.get("tokens_out") or 0)),
        )
        ctx = ResearchContext(
            profile=profile, job_id=job_id,
            spec=ResearchSpec(question=row["question"], mode=mode, domain=row.get("domain") or DOMAIN_GENERAL,
                              scope=row.get("scope"), reference_scope=row.get("reference_scope")),
            engine=engine, llm=run.llm, dossier=dossier, state=state, answers=answers, progress_sink=sink,
            deadline=time.monotonic() + max(0.0, C.MAX_JOB_SECONDS - base_elapsed),
            cancel_event=run.cancel_event, saver=saver, artifacts_dir=artifacts_dir(profile, job_id),
        )
        pipeline = _pipeline_for(mode)
        result = await pipeline(ctx)
        if isinstance(result, Dossier):
            ctx.dossier = result
        status = ctx.dossier.status if ctx.dossier.status in (COMPLETE, PARTIAL) else COMPLETE
    except NeedsInput as ask:
        status = ask.status if ask.status in (NEEDS_CLARIFICATION, NEEDS_CONFIRMATION) else NEEDS_CLARIFICATION
        (ctx.dossier if ctx else dossier).clarification = ask.clarification
    except BudgetExceeded:
        status, note = PARTIAL, _REASON_BUDGET
    except TimeUp:
        status, note = PARTIAL, _REASON_TIME
    except _RowGone:
        await _end_quietly(run, activity, flush_usage)
        return
    except Cancelled:
        status, error = CANCELLED, run.cancel_reason
    except asyncio.CancelledError:
        if not run.cancel_event.is_set():
            # The server is shutting down under the job: leave the row running;
            # the next boot marks it interrupted and tells its conversation.
            raise
        # A cancel_job that could not wait for the pipeline: finish the job as
        # cancelled. The cancellation is consumed here, so un-count it.
        current = asyncio.current_task()
        if current is not None:
            current.uncancel()
        status, error = CANCELLED, run.cancel_reason
    except Exception as exc:  # noqa: BLE001 — a failed job keeps what it found
        logger.exception(f"[userdocs] research job {job_id} of {profile} failed")
        status, error = FAILED, f"{type(exc).__name__}: {exc}"

    d = ctx.dossier if ctx is not None else dossier
    d.status = status
    if status not in WAITING:
        d.clarification = None
    if note and note not in d.notes:
        d.notes.append(note)
    llm = run.llm
    if llm is not None:
        d.tokens_in, d.tokens_out = llm.spent.tokens_in, llm.spent.tokens_out
    final = status in FINAL
    values: dict[str, Any] = {
        "status": status, "phase": None if final else _clip(sink.phase, 64),
        "tokens_in": d.tokens_in, "tokens_out": d.tokens_out, "elapsed_s": elapsed(),
        "error": error, "finished_at": _now_ms() if final else None,
    }
    if llm is not None:
        values.update(provider=_clip(llm.provider, 64), model=_clip(llm.model, 128))
    # Nobody is owed news of a cancellation: whoever cancelled already knows.
    quiet = status == CANCELLED
    try:
        written = await asyncio.to_thread(
            storage.bump_rev, profile, job_id, quiet=quiet, expect=ACTIVE,
            state=_json_copy(ctx.state if ctx is not None else state), dossier=d.to_dict(), **values,
        )
    except Exception as exc:  # noqa: BLE001 — never leave a job "running" forever
        logger.exception(f"[userdocs] research job {job_id}: could not save its result")
        values.update(status=FAILED, error=f"could not save the result: {exc}", phase=None, finished_at=_now_ms())
        written = await asyncio.to_thread(storage.bump_rev, profile, job_id, expect=ACTIVE, **values)
        status = FAILED
    # Read before waking the waiters: a collector still counted here is about
    # to show this outcome to the agent.
    collected_by_waiter = run.collectors > 0
    _forget(run)
    await flush_usage()
    if activity is not None:
        try:
            await activity.finish(status=status, summary=_summary(d) or None, error=error)
        except Exception:  # noqa: BLE001
            logger.debug("[userdocs] research activity finish failed", exc_info=True)
    logger.info(f"[userdocs] research job {job_id} of {profile}: {status} "
                f"({d.tokens_in + d.tokens_out} tokens, {elapsed():.0f}s)")
    if written and not quiet and conv_id:
        # The row's conversation, not the one captured at start: a
        # conversation id renamed meanwhile repointed the row, not this run.
        current = await asyncio.to_thread(storage.get, profile, job_id)
        if current is not None and current.get("conversation_id"):
            if collected_by_waiter:
                # The tool's long-poll reports it; the turn's end catches the
                # case where it did not get to claim it.
                _pending_convs.add(current["conversation_id"])
            else:
                await _deliver_if_idle(current["conversation_id"], profile)


def _forget(run: _Run) -> None:
    """The run is over: wake its waiters, then free the profile's slot."""
    run.done.set()
    if _runs.get(run.job_id) is run:
        _runs.pop(run.job_id, None)


async def _end_quietly(run: _Run, activity: Any, flush_usage: Callable[..., Awaitable[None]]) -> None:
    _forget(run)
    # The tokens were spent; the conversation they would be filed under may
    # be what was deleted.
    await flush_usage(None)
    if activity is not None:
        try:
            await activity.finish(status=CANCELLED, summary=None, error=None)
        except Exception:  # noqa: BLE001
            logger.debug("[userdocs] research activity finish failed", exc_info=True)
    logger.info(f"[userdocs] research job {run.job_id} of {run.profile} stopped: its record was deleted")


# ── delivery ──────────────────────────────────────────────────────────────


def _render_safe(profile: str, view: JobView) -> tuple[str, list[Any]]:
    try:
        return _render(profile, view)
    except Exception as exc:  # noqa: BLE001 — a result must still arrive, even plain
        logger.warning(f"[userdocs] research job {view.job_id}: render failed ({exc}); sending a plain summary")
        return _plain_text(view), []


def _plain_text(view: JobView) -> str:
    lines = [f"[User Documents · research] job {view.job_id} · {view.status} · {view.mode}/{view.domain}",
             f"Question: {view.question}"]
    d = view.dossier
    if view.error:
        lines.append(f"Error: {view.error}")
    if d is not None:
        if d.clarification is not None:
            lines.append(f"Question for the user: {d.clarification.question}")
            if d.clarification.answer_keys:
                lines.append("Answer keys: " + ", ".join(f"{k} ({v})" for k, v in d.clarification.answer_keys.items()))
        lines += [f"Note: {n}" for n in d.notes] + [f"Gap: {g}" for g in d.gaps]
    lines.append(f"Read the full dossier with user_documents__research(continue_job=\"{view.job_id}\").")
    return "\n".join(lines)


def _issue(profile: str, conversation_id: str, citations: list[Any]) -> None:
    from app.userdocs import citations as registry

    registry.issue(profile, conversation_id, citations)


def build_delivery_messages(view: JobView, text: str, *, room: bool = False) -> tuple[str, dict[str, Any]]:
    """``(query, trigger_event)`` for the turn that reports a job back. Pure.

    ``query`` is what the model reads: the rendered job and what to do with
    it. The trigger bubble the user sees stays one line — the rendered
    dossier is long, and a channel in detail mode forwards the trigger as it
    is."""
    jid, status = view.job_id, view.status
    call = f'user_documents__research with continue_job="{jid}"'
    if status in (NEEDS_CLARIFICATION, NEEDS_CONFIRMATION):
        header = ("[Document research — input needed] The research job started earlier in this conversation "
                  "stopped to ask the user something before going on. The full conversation history is above.")
        tail = ("Ask the user the question above, in their language, with the choices it lists. Do not answer "
                f"it yourself or guess. When they answer, call {call} and answers={{…}} using the answer keys "
                "shown; the job resumes from where it stopped.")
        word = "needs input"
    elif status == INTERRUPTED:
        header = ("[Document research — interrupted] The research job started earlier in this conversation was "
                  "cut short by a server restart. Its progress was saved.")
        tail = (f"Resume it now by calling {call}: it continues from its checkpoint without re-reading what it "
                "already read. Tell the user it is resuming.")
        word = "interrupted"
    elif status in (COMPLETE, PARTIAL):
        header = ("[Document research result] The research job started earlier in this conversation has "
                  "finished. This turn delivers its results; the full conversation history is above.")
        tail = ("Present these research results to the user: answer their question from the findings, cite "
                "each claim with the [ud:…] tokens exactly as printed, and say which files were not read and "
                "what was not found. Do not add claims the results do not support.")
        if status == PARTIAL:
            tail += " The job is partial — say what it did not cover and why."
        tail += (f' Further dossier pages: user_documents__read(file="research:{jid}", page=n).')
        word = "complete" if status == COMPLETE else "partial"
    else:
        header = (f"[Document research — {status}] The research job started earlier in this conversation "
                  f"did not finish ({status}).")
        tail = ("Tell the user it did not finish and why, present whatever it did establish above (with its "
                "[ud:…] tokens exactly as printed), and offer to run it again.")
        word = status
    query = f"{header}\n\n{text}\n\n{tail}"
    if room:
        query += ("\n\nThis conversation is a room: your answer is posted to everyone in it. This is the "
                  "research the room asked for reporting back, so present it in your own words.")
    question = " ".join((view.question or "").split())
    label = question if len(question) <= 80 else question[:79] + "…"
    trigger_event = {
        "kind": "research_result",
        "event_type": f"document research {word}: {label}",
        "action": "",
        "content": f"Research job {jid} · {view.mode}/{view.domain} · {status}",
        "status": status,
        "job_id": jid,
        "label": label,
        # Not a hop in an event-task wait chain.
        "task_chain_depth": 0,
    }
    return query, trigger_event


async def _deliver(row: dict[str, Any]) -> bool:
    """Claim the job's current state and inject one turn reporting it.
    Returns whether this call delivered. On a failed enqueue the claim is
    released, so the next hook retries."""
    job_id, profile, conv_id = row["id"], row["profile"], row.get("conversation_id")
    if not conv_id:
        return False
    storage = _storage()
    claim = await asyncio.to_thread(storage.claim_delivery, job_id)
    if claim is None:
        return False
    adapter = None
    try:
        full = await asyncio.to_thread(storage.get, profile, job_id)
        if full is None:
            return False
        from app.events import runner as event_runner

        conversation_storage = event_runner.get_conversation_storage()
        if conversation_storage is None:
            raise RuntimeError("conversation storage not initialized")
        conv = await conversation_storage.get_conversation(conv_id)
        if conv is None or conv.get("kind") == "event_run":
            # Nowhere a person reads: the claim stands, nothing is sent.
            logger.info(f"[userdocs] research job {job_id}: conversation {conv_id} cannot take a report; "
                        f"closed out")
            return False
        view = _view(full)
        text, citations = await asyncio.to_thread(_render_safe, profile, view)
        if citations:
            await asyncio.to_thread(_issue, profile, conv_id, citations)

        from app.events.event_task_delivery import _load_history
        from app.events.user_message_delivery import is_room_conversation

        room = is_room_conversation(conv)
        history = await _load_history(conversation_storage, conv_id, profile)
        try:
            from app.events.run_dispatcher import _maybe_forward_to_channel

            adapter = await _maybe_forward_to_channel(conversation_storage, conv_id, conv_id)
        except Exception:  # noqa: BLE001
            logger.exception("[userdocs] research: channel forwarder setup failed")
        query, trigger_event = build_delivery_messages(view, text, room=room)
        metadata = {"source": "research_result", "status": view.status, "job_id": job_id}
        trigger_metadata = {**metadata, "trigger": True, "label": trigger_event["label"]}

        from app.agent.stream_runner import make_run_id
        from app.events import queue as event_queue

        await event_queue.enqueue_user_message(
            conversation_id=conv_id,
            run_id=make_run_id(conv_id, kind="research"),
            profile=profile,
            query=query,
            history_messages=history,
            reasoning=True,
            user_message_metadata=trigger_metadata,
            agent_message_metadata=metadata,
            push_user_message=False,
            trigger_event=trigger_event,
            update_title_from_query=False,
            publish_notification=not room,
        )
    except BaseException as exc:  # noqa: BLE001 — a claim must never outlive a failed hand-over
        _pending_convs.add(conv_id)
        if not isinstance(exc, Exception):
            # Cancelled mid-hand-over (shutdown): no await may run now, so the
            # claim is released inline, and the next boot delivers.
            try:
                storage.release_delivery(job_id, *claim)
            except Exception:  # noqa: BLE001
                logger.exception(f"[userdocs] research job {job_id}: could not release its delivery claim")
            raise
        logger.exception(f"[userdocs] research job {job_id}: delivery to {conv_id} failed; will retry")
        try:
            await asyncio.to_thread(storage.release_delivery, job_id, *claim)
        except Exception:  # noqa: BLE001
            logger.exception(f"[userdocs] research job {job_id}: could not release its delivery claim")
        if adapter is not None:
            try:
                await adapter.release_external_run(conv_id)
            except Exception:  # noqa: BLE001
                logger.exception(f"[userdocs] research: could not release the forwarder for {conv_id}")
        return False
    logger.info(f"[userdocs] research job {job_id} ({view.status}) reported to conversation {conv_id}")
    return True


async def _deliver_owed(conversation_id: str | None, profile: str | None = None) -> int:
    """Deliver every state owed (to one conversation, or to all). A
    conversation with a live turn is left to that turn's end."""
    from app.events import task_result_inbox

    rows = await asyncio.to_thread(_storage().list_undelivered, conversation_id)
    delivered = 0
    for row in rows:
        if profile is not None and row["profile"] != profile:
            continue
        conv = row["conversation_id"]
        if task_result_inbox.bound_run_for(conv) is not None:
            _pending_convs.add(conv)
            continue
        if await _deliver(row):
            delivered += 1
    return delivered


async def _deliver_if_idle(conversation_id: str, profile: str) -> None:
    """At a deliverable state: report now if the conversation is idle;
    otherwise the live turn may collect it itself, and its end reports it if
    it did not."""
    from app.events import task_result_inbox

    _pending_convs.add(conversation_id)
    if task_result_inbox.bound_run_for(conversation_id) is not None:
        return
    try:
        _pending_convs.discard(conversation_id)
        await _deliver_owed(conversation_id, profile)
    except Exception:  # noqa: BLE001
        _pending_convs.add(conversation_id)
        logger.exception(f"[userdocs] research delivery to {conversation_id} failed")


async def on_turn_end(*, conversation_id: str, profile: str) -> None:
    """A turn in ``conversation_id`` ended (called by the stream runner after
    it unbinds): report any job state it did not collect. Costs nothing for a
    conversation no job reported to."""
    if not conversation_id or conversation_id not in _pending_convs:
        return
    _pending_convs.discard(conversation_id)
    try:
        await _deliver_owed(conversation_id, profile)
    except Exception:  # noqa: BLE001
        _pending_convs.add(conversation_id)
        logger.exception(f"[userdocs] research turn-end delivery failed for {conversation_id}")


async def boot_recover() -> None:
    """Boot: jobs the last process was running become ``interrupted`` (their
    conversations are told, and ``continue_job`` resumes them), and every
    state still owed is delivered. Never raises."""
    _capture_loop()
    try:
        ids = await asyncio.to_thread(_storage().mark_active_interrupted, list(_runs))
        if ids:
            logger.info(f"[userdocs] {len(ids)} research job(s) interrupted by the restart: {', '.join(ids)}")
        delivered = await _deliver_owed(None)
        if delivered:
            logger.info(f"[userdocs] boot: reported {delivered} research result(s)")
    except Exception:  # noqa: BLE001
        logger.exception("[userdocs] research boot recovery failed")


def _reset_for_tests() -> None:
    """Forget every in-memory run and marker. Tests only."""
    _runs.clear()
    _pending_convs.clear()
    _background.clear()


__all__ = [
    "artifacts_dir", "artifacts_root", "boot_recover", "build_delivery_messages", "cancel_job",
    "cancel_profile_jobs", "continue_job", "get_job", "has_live_run", "list_jobs", "mark_collected",
    "on_turn_end", "purge_profile", "resolve_conversation_id", "signal_cancel", "spawn", "start_job",
    "wait_cap", "wait_job",
]
