"""Research results in one turn: what they handed over, and what the turn
must do about it before the model writes an answer.

A research job reports back through the Documentation Search tool
(``research``, ``read`` of ``research:<id>``) or as a turn of its own. Each
result carries a :class:`~app.documents.research.handoff.ResearchDelivery`,
attached by the built-in tool path (never read out of text). The reasoning
agent keeps one :class:`ResearchHandoff` per turn and feeds it every record;
before each model request it asks two things:

- **Pages still owed.** A settled job's verified findings may sit on dossier
  pages the turn has not shown the model (page 1 is the summary; findings
  spill over under a small tool-result budget: a 112-finding dossier is ten
  3,900-token pages). Those pages are read first, as the agent's own calls,
  within a per-turn allowance (:func:`allowance_for`) — so the answer can
  rest on the evidence the job found, not on page 1 alone, without the model
  spending steps to fetch it.
- **Nothing to answer from.** A settled legal analysis with no usable
  verified evidence ends the turn with the insufficiency summary the server
  rendered — before another model request, because a check of the finished
  answer would come after it was streamed. Never for a job that is still
  running or waiting, never when another job of the same turn has evidence.

A research record is not passage-delivery evidence: a finding delivered does
not mean its source was read in full, so nothing here feeds the ordinary
document review (:mod:`app.agent.document_review`) — except that a *valid*
handoff (a job's state was shown) makes that review stand down, and a failed
research call does not.

The state lives on the agent run, one per turn — never keyed by conversation
in a process-wide registry — so profiles, conversations and turns share
nothing, and a later turn's successful result is judged on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.documents.research.handoff import ResearchDelivery

# The activity-trace origin of a dossier page the agent read itself.
ORIGIN = "research_handoff"
# The dossier text one turn reads automatically, at most: ten pages at the
# default page budget — never more than a quarter of the model's context
# window. A page whose record does not say its budget counts as this many.
AUTO_READ_TOKENS = 40_000
WINDOW_SHARE = 4
DEFAULT_PAGE_TOKENS = 4_000


def allowance_for(provider: str | None, model: str | None) -> int:
    """The turn's automatic-read allowance for the model that answers it."""
    from app.lib.llm.pricing import DEFAULT_CONTEXT_WINDOW, context_window_for

    try:
        window = context_window_for(provider, model) or DEFAULT_CONTEXT_WINDOW
    except Exception:  # noqa: BLE001 — an unknown model gets the default window
        window = DEFAULT_CONTEXT_WINDOW
    return min(AUTO_READ_TOKENS, window // WINDOW_SHARE)


@dataclass
class _Job:
    latest: ResearchDelivery
    pages: set[int] = field(default_factory=set)
    delivered: set[str] = field(default_factory=set)


class ResearchHandoff:
    """The research records of one turn (see the module docstring)."""

    def __init__(self, *, allowance: int = AUTO_READ_TOKENS) -> None:
        self.allowance = allowance
        self.jobs: dict[str, _Job] = {}
        self.auto_pages = 0
        self.auto_tokens = 0
        self.guarded: str | None = None

    def observe(self, delivery: Any) -> bool:
        """Take in a record; True when it is a valid handoff (a job's state
        was shown). A failed call's record, or anything that is not a
        record, changes nothing."""
        if not isinstance(delivery, ResearchDelivery) or not delivery.valid:
            return False
        job = self.jobs.get(delivery.job_id)
        if job is None or job.latest.status != delivery.status:
            # A new state of the job (it finished since): what was shown of
            # the old one says nothing about this one.
            job = _Job(latest=delivery)
            self.jobs[delivery.job_id] = job
        job.latest = delivery
        if delivery.settled:
            job.pages.add(int(delivery.page or 1))
        job.delivered |= set(delivery.delivered)
        return True

    def pending_pages(self) -> list[tuple[str, int]]:
        """``(job id, page)`` of the dossier pages that carry evidence the
        turn has not shown yet, in order, as many as the turn's allowance
        still holds (each page counted at its full budget)."""
        spent = self.auto_tokens
        out: list[tuple[str, int]] = []
        for jid, job in self.jobs.items():
            d = job.latest
            if not d.settled or not d.findings or not (set(d.evidence) - job.delivered):
                continue
            for p in d.evidence_pages:
                if p in job.pages:
                    continue
                if spent + self._page_tokens(jid) > self.allowance:
                    return out
                spent += self._page_tokens(jid)
                out.append((jid, p))
        return out

    def charge(self, pages: list[tuple[str, int]]) -> None:
        """Count ``pages`` — about to be read — against the allowance."""
        self.auto_pages += len(pages)
        self.auto_tokens += sum(self._page_tokens(jid) for jid, _page in pages)

    def _page_tokens(self, job_id: str) -> int:
        job = self.jobs.get(job_id)
        return (job.latest.page_tokens if job is not None else 0) or DEFAULT_PAGE_TOKENS

    def insufficient(self) -> ResearchDelivery | None:
        """The settled legal analysis this turn must end on, or None: one
        without usable evidence, when no job of the turn has any."""
        latest = [j.latest for j in self.jobs.values()]
        if any(d.settled and d.findings > 0 for d in latest):
            return None
        bad = [d for d in latest if d.settled and d.insufficiency]
        return bad[-1] if bad else None

    def summary(self) -> dict[str, Any] | None:
        """For the turn's log: the jobs seen, pages read, whether the guard
        ended the turn. None when the turn touched no research."""
        if not self.jobs:
            return None
        return {
            "jobs": {jid: {"status": j.latest.status, "outcome": j.latest.outcome, "findings": j.latest.findings,
                           "pages": sorted(j.pages)} for jid, j in self.jobs.items()},
            "auto_pages": self.auto_pages,
            "auto_tokens": self.auto_tokens,
            "guarded": self.guarded,
        }


def trusted_research(tool: Any, event: Any, *, tool_id: str) -> ResearchDelivery | None:
    """The research record on ``event`` when — and only when — the built-in
    Documentation Search group produced it (see
    ``document_review.trusted_evidence`` for why)."""
    from app.tools.base import ToolType

    record = getattr(event, "evidence", None)
    if not isinstance(record, ResearchDelivery):
        return None
    if getattr(tool, "tool_type", None) is not ToolType.BUILTIN or getattr(tool, "tool_id", None) != tool_id:
        return None
    return record


__all__ = ["AUTO_READ_TOKENS", "DEFAULT_PAGE_TOKENS", "ORIGIN", "ResearchHandoff", "WINDOW_SHARE", "allowance_for",
           "trusted_research"]
