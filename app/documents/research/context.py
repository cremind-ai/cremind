"""What a research pipeline runs inside: :class:`ResearchContext`.

The job runner (:mod:`.jobs`) builds one context per run of a job and hands
it to a pipeline (:func:`.compile.run_compile` or
:func:`.analyze.run_analyze`). The pipeline never touches the job table, the
event bus or the usage table itself; everything goes through the context:

- ``ctx.engine`` — the profile's :class:`~app.documents.query.engine.QueryEngine`.
  Its calls are blocking (SQLite, the query embedding): run them with
  ``await ctx.io(fn, *args)``, which also checks for cancellation first.
- ``ctx.llm`` — a :class:`ResearchLLM`: function-calling completions that
  count every token against the job's budget and raise
  :class:`BudgetExceeded` *before* a call that would overrun it.
- ``ctx.state`` — the pipeline's checkpoint, a JSON-able dict the pipeline
  owns (its own keys). ``await ctx.save()`` persists it with the dossier.
  After a restart, or after the job stopped to ask something, the pipeline
  is called again with the saved ``state`` and must skip work already done.
- ``ctx.dossier`` — the result being built; saved with the checkpoint, so a
  failed or cancelled job still shows what it had.
- ``ctx.step(...)`` / ``ctx.phase(...)`` / ``ctx.progress(...)`` — what the
  user sees in the Research activity panel (and the REST/CLI status).
- ``ctx.answers`` — what the user answered to a clarification (continue_job).

A pipeline stops early by raising :class:`NeedsInput` (after ``save()``), and
the runner turns it into ``needs_clarification`` / ``needs_confirmation``.
:class:`Cancelled`, :class:`TimeUp` and :class:`BudgetExceeded` are raised by
the context itself; the runner maps them to ``cancelled`` / ``partial``.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable

from app.documents.research.types import (
    DOMAIN_GENERAL,
    MODE_ANALYZE,
    NEEDS_CLARIFICATION,
    Clarification,
    Dossier,
)
from app.utils.logger import logger

# A job never runs longer than this in total (across resumes).
MAX_JOB_SECONDS = 1800
# Rough characters per token for budgeting before a call (the provider's own
# count replaces it after). Deliberately low: overestimating stops early,
# underestimating overruns the budget.
CHARS_PER_TOKEN = 3.2
# The job stops starting new rounds at this fraction of its budget, keeping
# the rest for writing up what it has.
SOFT_BUDGET_FRACTION = 0.9


class Cancelled(Exception):
    """The job was cancelled (by the user, or because the feature went off)."""


class TimeUp(Exception):
    """The job reached its time limit."""


class BudgetExceeded(Exception):
    """The next LLM call would take the job past its token budget."""


class NeedsInput(Exception):
    """The pipeline stopped to ask the user; ``status`` is
    ``needs_clarification`` or ``needs_confirmation``."""

    def __init__(self, clarification: Clarification, status: str = NEEDS_CLARIFICATION):
        super().__init__(clarification.question)
        self.clarification = clarification
        self.status = status


class StructuredOutputError(Exception):
    """The model answered, but not with a usable call of the function it was
    asked for: no call, another function, arguments that are not JSON, or
    arguments that miss a required field or have the wrong type. Never an
    empty result: a caller retries it, then reports it."""


@dataclass
class ResearchSpec:
    """What the user asked for. ``scope`` / ``reference_scope`` are filter
    dicts in the tool's FILTERS_SCHEMA shape (folder, path_glob, file_ids,
    types, date_*, …)."""

    question: str
    mode: str = MODE_ANALYZE
    domain: str = DOMAIN_GENERAL
    scope: dict[str, Any] | None = None
    reference_scope: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out


def estimate_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,), "array": (list,), "string": (str,), "boolean": (bool,),
    "integer": (int,), "number": (int, float), "null": (type(None),),
}


def _type_error(schema: dict[str, Any], value: Any, path: str) -> str | None:
    types = schema.get("type")
    allowed = [types] if isinstance(types, str) else list(types or [])
    if not allowed:
        return None
    for name in allowed:
        if isinstance(value, bool) and name in ("integer", "number"):
            continue  # a boolean is not a number
        if isinstance(value, _JSON_TYPES.get(name, ())):
            return None
    return f"{path}: expected {'/'.join(allowed)}, got {type(value).__name__}"


def schema_errors(schema: dict[str, Any], value: Any, path: str = "$", *, root: bool = True) -> list[str]:
    """Where ``value`` breaks ``schema`` (the subset of JSON Schema the
    research tools use): the root's required fields, and the type of every
    field and of every array item. Deeper than that — one fact without its
    evidence, one finding with an odd field — is left to the pipeline, which
    drops such an item on its own; one bad item must not cost the whole
    answer. Extra fields are tolerated. Empty when it fits."""
    err = _type_error(schema, value, path)
    if err:
        return [err]
    errors: list[str] = []
    if isinstance(value, dict):
        if root:
            errors += [f"{path}.{key}: missing" for key in schema.get("required") or [] if key not in value]
        for key, sub in (schema.get("properties") or {}).items():
            if key in value and isinstance(sub, dict):
                errors += schema_errors(sub, value[key], f"{path}.{key}", root=False)
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            err = _type_error(schema["items"], item, f"{path}[{i}]")
            if err:
                errors.append(err)
                break
    return errors


class ResearchLLM:
    """The research model, with a token budget.

    ``call`` is one function-calling completion: the model is asked to call
    ``tool`` (an OpenAI-style ``{"type": "function", "function": {...}}``)
    by name, and its parsed arguments come back — checked against the
    tool's schema. Anything else (no call, another function, arguments that
    are not JSON or break the schema) raises :class:`StructuredOutputError`:
    a malformed answer must never pass for an empty one. Every call's usage
    is added to ``spent`` and reported to ``on_usage`` (the runner turns it
    into usage records), whatever the call's outcome.
    """

    def __init__(
        self,
        llm: Any,
        *,
        budget: int,
        spent: Usage | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
        model_group: str = "high",
    ) -> None:
        self._llm = llm
        self.budget = max(1000, int(budget))
        self.spent = spent or Usage()
        self._on_usage = on_usage
        self.model_group = model_group
        self.calls = 0
        # What calls in flight may still spend: checked in :meth:`call` so
        # concurrent calls cannot each pass on their own and overrun together.
        self._reserved = 0

    @property
    def provider(self) -> str | None:
        return getattr(self._llm, "provider_name", None)

    @property
    def model(self) -> str | None:
        return getattr(self._llm, "model_name", None)

    @property
    def raw(self) -> Any:
        """The underlying provider, for helpers that make their own calls
        (e.g. ``rerank.query_variants``); report their usage with
        :meth:`add_usage`, and check :meth:`can_afford` first."""
        return self._llm

    def add_usage(self, usage: dict[str, int] | None) -> None:
        """Count a call made outside :meth:`call` against the budget."""
        if usage:
            self._count(usage)

    @property
    def remaining(self) -> int:
        return max(0, self.budget - self.spent.total)

    @property
    def fraction(self) -> float:
        return self.spent.total / self.budget if self.budget else 1.0

    def soft_limit_reached(self) -> bool:
        """Past the point where new rounds should start (keep the rest for
        the write-up)."""
        return self.fraction >= SOFT_BUDGET_FRACTION

    def can_afford(self, prompt_tokens: int, answer_tokens: int = 2000) -> bool:
        """Against what is already spent; calls in flight are not counted
        here (a pipeline running several at once tracks its own), but
        :meth:`call` counts them before every call."""
        return self.spent.total + prompt_tokens + answer_tokens <= self.budget

    async def call(
        self,
        *,
        system: str,
        user: str,
        tool: dict[str, Any],
        max_tokens: int | None = 4000,
    ) -> dict[str, Any]:
        from app.constants import ChatCompletionTypeEnum
        from app.lib.llm.base import done_chunk_token_usage

        prompt = estimate_tokens(system) + estimate_tokens(user) + estimate_tokens(json.dumps(tool))
        need = prompt + (max_tokens or 2000)
        if self.spent.total + self._reserved + need > self.budget:
            raise BudgetExceeded(f"budget {self.budget} tokens; spent {self.spent.total}"
                                 + (f", {self._reserved} held by calls in flight" if self._reserved else ""))
        name = tool["function"]["name"]
        calls: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        # The function is named, not merely offered: a model left to choose
        # may answer in prose, which is no answer here.
        kwargs: dict[str, Any] = {"tools": [tool], "tool_choice": {"type": "function", "function": {"name": name}},
                                  "temperature": 0}
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        self.calls += 1
        self._reserved += need
        try:
            async for response in self._llm.chat_completion(
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}], **kwargs,
            ):
                rtype = response.get("type")
                if rtype == ChatCompletionTypeEnum.FUNCTION_CALLING:
                    data = response.get("data")
                    if isinstance(data, dict) and data.get("function"):
                        calls = data["function"]
                elif rtype == ChatCompletionTypeEnum.DONE:
                    usage = done_chunk_token_usage(response)
                    break
        finally:
            self._reserved -= need
            if usage and any(int(v or 0) for v in usage.values()):
                self._count(usage)
            else:
                # No usage reported — a failed call, or a provider that
                # reports zeros: count the estimate, so neither makes a job
                # free.
                self._count({"input_tokens": prompt, "output_tokens": 0})
        return _parse_call(name, calls, tool["function"].get("parameters") or {})

    def _count(self, usage: dict[str, int]) -> None:
        tin = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0) \
            + int(usage.get("cache_creation_input_tokens") or 0)
        tout = int(usage.get("output_tokens") or 0)
        self.spent.tokens_in += tin
        self.spent.tokens_out += tout
        if self._on_usage is not None:
            try:
                self._on_usage(usage)
            except Exception:  # noqa: BLE001 — accounting never fails a job
                logger.debug("[documents] research usage callback failed", exc_info=True)


def _parse_call(name: str, calls: list[dict[str, Any]], parameters: dict[str, Any]) -> dict[str, Any]:
    """The arguments of the call to ``name``, checked against its schema;
    :class:`StructuredOutputError` for anything else."""
    wanted = [c for c in calls if isinstance(c, dict) and c.get("name") == name]
    if not wanted:
        other = sorted({str(c.get("name")) for c in calls if isinstance(c, dict)})
        raise StructuredOutputError(f"the model did not call {name}"
                                    + (f" (it called {', '.join(other)})" if other else ""))
    args = wanted[0].get("arguments")
    if args is None or args == "":
        args = {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError) as exc:
            raise StructuredOutputError(f"{name}: the arguments are not valid JSON ({exc})") from None
    if not isinstance(args, dict):
        raise StructuredOutputError(f"{name}: the arguments are a {type(args).__name__}, not an object")
    errors = schema_errors(parameters, args)
    if errors:
        raise StructuredOutputError(f"{name}: malformed arguments: " + "; ".join(errors[:5]))
    return args


class ProgressSink:
    """Where a pipeline's progress goes. The runner supplies one wired to
    the Research activity panel and to the job's live status; tests use the
    default, which only remembers."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.phase: str | None = None
        self.done = 0
        self.total = 0

    def set_phase(self, name: str) -> None:
        self.phase = name

    def add_step(self, *, kind: str, label: str, detail: str | None = None, status: str = "running") -> str:
        sid = f"s{len(self.steps) + 1}"
        self.steps.append({"id": sid, "kind": kind, "label": label, "detail": detail, "status": status})
        return sid

    def resolve_step(self, step_id: str, *, status: str, detail_suffix: str | None = None) -> None:
        for s in self.steps:
            if s["id"] == step_id:
                s["status"] = status
                if detail_suffix:
                    s["detail"] = f"{s.get('detail') or ''}{detail_suffix}"

    def set_progress(self, done: int, total: int) -> None:
        self.done, self.total = done, total


@dataclass
class ResearchContext:
    profile: str
    job_id: str
    spec: ResearchSpec
    engine: Any
    llm: ResearchLLM
    dossier: Dossier
    state: dict[str, Any] = field(default_factory=dict)
    answers: dict[str, Any] = field(default_factory=dict)
    progress_sink: ProgressSink = field(default_factory=ProgressSink)
    # time.monotonic() after which TimeUp is raised.
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_JOB_SECONDS)
    cancel_event: asyncio.Event | None = None
    # Persists (state, dossier); supplied by the runner. None in tests.
    saver: Callable[["ResearchContext"], Awaitable[None]] | None = None
    # Where the job writes artifacts (the compile table's CSV/Markdown): a
    # directory under Cremind's system folder, never the user's folder.
    artifacts_dir: str | None = None

    # ── control ───────────────────────────────────────────────────────────

    def check(self) -> None:
        """Raise if the job must stop now. Call between steps."""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise Cancelled()
        if time.monotonic() >= self.deadline:
            raise TimeUp()

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    async def io(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking engine/index call off the event loop."""
        self.check()
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def save(self) -> None:
        self.dossier.tokens_in = self.llm.spent.tokens_in
        self.dossier.tokens_out = self.llm.spent.tokens_out
        if self.saver is not None:
            await self.saver(self)

    # ── progress ──────────────────────────────────────────────────────────

    def phase(self, name: str) -> None:
        self.progress_sink.set_phase(name)

    def step(self, label: str, detail: str | None = None, *, kind: str = "step") -> str:
        return self.progress_sink.add_step(kind=kind, label=label, detail=detail)

    def done_step(self, step_id: str, *, ok: bool = True, suffix: str | None = None) -> None:
        self.progress_sink.resolve_step(step_id, status="done" if ok else "failed", detail_suffix=suffix)

    def progress(self, done: int, total: int) -> None:
        self.progress_sink.set_progress(done, total)


__all__ = [
    "BudgetExceeded", "CHARS_PER_TOKEN", "Cancelled", "MAX_JOB_SECONDS", "NeedsInput", "ProgressSink",
    "ResearchContext", "ResearchLLM", "ResearchSpec", "SOFT_BUDGET_FRACTION", "StructuredOutputError", "TimeUp",
    "Usage", "estimate_tokens", "schema_errors",
]
