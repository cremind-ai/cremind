"""Automatic cross-source review of document search results, for one turn.

The failure this exists for: a search over the user's documents returned two
relevant guides, the agent read one of them and answered — the other, ranked
first, never reached the answer. Citation checks cannot catch that (every
citation was genuine); only knowing what the turn *delivered* can.

So the reasoning agent keeps one :class:`DocumentReview` per turn and feeds
it the typed delivery record (:class:`~app.documents.delivery.DocumentEvidence`)
of every Documentation Search result — the trusted built-in path only, never
text. When a search shows matching passages from at least two eligible files,
those files become candidates, and before the next model response the agent
reads, for each candidate whose matching passage no read has delivered whole
yet, that passage with the passages around it (``documentation_search__read``
with its token) — a search snippet showing a short passage whole does not
count, since the model can pass over a snippet line — within fixed limits:

- at most :data:`MAX_AUTO_READS` automatic reads per turn, one per file;
- at most :data:`TOKEN_ALLOWANCE` rendered tokens across them (their notes
  included), divided between the reads still to make — each read also stays
  within the profile's own tool-result budget;
- no retry after a partial or failed read, and an automatic read never makes
  candidates of its own.

Candidates are files whose displayed matching passage was rated ``high`` or
``medium`` confidence, in search-result order. Confidence is a retrieval
heuristic, not a verdict on what the document says.

What the record distinguishes, and the summary reports (``document_review``
on the answer's metadata, and a log line of counts):

- a file *returned* by a search, and *eligible* for review;
- a passage *delivered* (whole, or in part) — which is not the file read in
  full; a file *examined* (a passage asked for — a search match, a passage a
  read was for — delivered whole; a neighbour shown for context does not
  count) — which is not its claims used in the answer;
- a candidate *partial* (only part of it fit), *failed* (the read failed,
  the passage is gone, the file changed, only its details are indexed), or
  *unexamined*, with the reason it was left out (a limit, new user input, a
  research job taking over, Instant mode, …);
- the files the final answer cites.

"100% synced" in a result header is indexing status, not answer coverage;
nothing here reads it.

The state lives on the agent run — a new :class:`DocumentReview` per turn —
never in a process-wide registry keyed by conversation, so concurrent turns
and different profiles cannot share candidates, passages or state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.documents.delivery import (
    FOCUS_COMPLETE,
    FOCUS_PARTIAL,
    FOCUS_UNRESOLVED,
    OP_READ,
    OP_SEARCH,
    ROLE_MATCH,
    DocumentEvidence,
)

SUMMARY_VERSION = 1

# The activity-trace origin of an automatic read (``Origin`` on its step).
ORIGIN = "document_review"

MAX_AUTO_READS = 4
TOKEN_ALLOWANCE = 6000
ELIGIBLE_CONFIDENCE = frozenset({"high", "medium"})
# A read is not worth making with less room than this: a header, the passage
# asked for and little else.
MIN_READ_TOKENS = 500
# Room kept, within the allowance, for the line that introduces each
# automatic result and for the summary after the last one.
NOTE_RESERVE = 60
# The round note at its longest (four reads, each partial with its
# continuation, and four files left out) measures about 400 tokens.
SUMMARY_RESERVE = 450
# How many unreviewed files the summary names before "and N more".
_SUMMARY_LISTED = 4

# Why an eligible file was not examined automatically.
REASON_CALL_LIMIT = "call_limit"
REASON_TOKEN_BUDGET = "token_budget"
REASON_READ_UNAVAILABLE = "read_unavailable"
REASON_INSTANT = "instant_mode"
REASON_NEW_INPUT = "new_user_input"
REASON_RESEARCH = "research_handoff"
REASON_NO_STEP = "no_synthesis_step"
REASON_NOT_REVIEWED = "not_reviewed"

# A candidate's state.
_PENDING = "pending"
_COVERED = "covered"          # its passage was already delivered whole
_ATTEMPTED = "attempted"      # one automatic read made (its outcome in ``outcome``)
_SKIPPED = "skipped"          # left out, ``reason`` says why

# The outcome of an automatic read.
COMPLETE = "complete"
PARTIAL = "partial"
FAILED = "failed"


@dataclass
class _Source:
    """A file a search returned during the turn."""

    fid: str
    token: str
    order: int                     # first appearance across the turn's searches
    eligible: bool = False
    candidate: str | None = None   # the matching passage chosen for review
    confidence: str | None = None
    position: str = ""             # where that passage sits ("p. 49"), numbers only
    state: str = _PENDING
    outcome: str | None = None     # complete | partial | failed, for an attempted read
    reason: str | None = None


@dataclass
class Attempt:
    """One automatic read's result, as the summary note reports it."""

    token: str                     # the file's token
    candidate: str                 # the passage read
    outcome: str                   # complete | partial | failed
    reason: str | None = None
    continuation: dict[str, Any] | None = None
    position: str = ""


class DocumentReview:
    """The document evidence of one turn, and the automatic reads it calls
    for. Pure bookkeeping: the agent runs the reads and hands back what they
    delivered."""

    def __init__(self, *, max_calls: int = MAX_AUTO_READS, allowance: int = TOKEN_ALLOWANCE) -> None:
        self.max_calls = max_calls
        self.allowance = allowance
        self.sources: dict[str, _Source] = {}
        # passage token -> (file id, delivered whole) for every passage any
        # result showed; a stale read's passages are not counted.
        self._delivered: dict[str, tuple[str, bool]] = {}
        # Files with a passage delivered whole that was asked for — a search
        # match, or a passage a read was for. A neighbour a search printed for
        # context does not make its file examined, nor does the neighbour a
        # read centred on another passage showed.
        self._examined: set[str] = set()
        # The same, from reads only: what a failed automatic read is weighed
        # against (a search snippet does not make up for it).
        self._examined_by_read: set[str] = set()
        # Passages a READ delivered whole (the agent's own or an automatic
        # one). Only these cover a candidate: a search snippet that happened
        # to show a short passage whole still lacks what a read adds — the
        # passages around it — and a model can pass over a snippet line.
        self._read_whole: set[str] = set()
        self._stale: set[str] = set()
        self.searches = 0
        self.reads = 0
        self.calls = 0
        self.tokens = 0
        self.rounds = 0

    # ── what results delivered ──────────────────────────────────────────

    def observe(self, evidence: Any) -> None:
        """Take in a result the agent's own call produced."""
        if not isinstance(evidence, DocumentEvidence) or evidence.error:
            return
        if evidence.op == OP_SEARCH:
            self.searches += 1
            self._observe_search(evidence)
        elif evidence.op == OP_READ:
            self.reads += 1
        self._note_delivered(evidence)
        self._settle()

    def _note_delivered(self, evidence: DocumentEvidence) -> None:
        """Record the passages a result delivered. A read of a file that
        changed since it was indexed, or that has no text indexed, delivers
        nothing that counts."""
        if evidence.op == OP_READ and evidence.stale:
            if evidence.fid:
                self._stale.add(evidence.fid)
            return
        if evidence.op == OP_READ and evidence.metadata_only:
            return
        for p in evidence.passages:
            if not p.substantive:
                continue
            _fid, whole = self._delivered.get(p.token, (p.fid, False))
            self._delivered[p.token] = (p.fid, whole or p.complete)
            if not p.complete:
                continue
            if evidence.op == OP_READ:
                self._read_whole.add(p.token)
                # A read centred on a passage examined its file only if that
                # passage came through whole; any other read, by what it showed.
                if evidence.focus is None or p.token == evidence.focus:
                    self._examined.add(p.fid)
                    self._examined_by_read.add(p.fid)
            elif p.role == ROLE_MATCH:
                self._examined.add(p.fid)

    def _observe_search(self, evidence: DocumentEvidence) -> None:
        for s in evidence.sources:
            if s.fid not in self.sources:
                self.sources[s.fid] = _Source(fid=s.fid, token=s.token, order=len(self.sources) + 1)
        # The first displayed matching passage of each file, in rank order.
        matches: dict[str, Any] = {}
        for p in sorted(evidence.passages, key=lambda p: p.order):
            if (p.role == ROLE_MATCH and p.substantive and p.confidence in ELIGIBLE_CONFIDENCE
                    and p.fid in self.sources):
                matches.setdefault(p.fid, p)
        # Review is for a search that brought several sources: one restricted
        # to a single file, or that found only one relevant file, has nothing
        # to weigh against it.
        if len(matches) < 2:
            return
        for fid, p in matches.items():
            src = self.sources[fid]
            if src.eligible:
                continue  # its candidate is fixed at its first eligible search
            src.eligible = True
            src.candidate = p.token
            src.confidence = p.confidence
            src.position = p.position

    def _settle(self) -> None:
        """A pending candidate whose passage a read already delivered whole
        is covered."""
        for src in self.sources.values():
            if src.eligible and src.state == _PENDING and (src.candidate or "") in self._read_whole:
                src.state = _COVERED

    def _whole(self, token: str | None) -> bool:
        """Whether any result showed this passage whole."""
        got = self._delivered.get(token or "")
        return bool(got and got[1])

    # ── planning the automatic reads ────────────────────────────────────

    def has_pending(self) -> bool:
        return any(s.eligible and s.state == _PENDING for s in self.sources.values())

    def suppress(self, reason: str) -> None:
        """Leave every pending candidate unreviewed, for ``reason``."""
        for src in self.sources.values():
            if src.eligible and src.state == _PENDING:
                src.state, src.reason = _SKIPPED, reason

    def plan(self) -> list[_Source]:
        """The candidates to read now, in result order, within the turn's
        call and token limits. The ones left out are recorded as skipped,
        with the limit that stopped them."""
        self._settle()
        todo = sorted((s for s in self.sources.values() if s.eligible and s.state == _PENDING),
                      key=lambda s: s.order)
        room = max(0, self.max_calls - self.calls)
        chosen, over = todo[:room], todo[room:]
        for src in over:
            src.state, src.reason = _SKIPPED, REASON_CALL_LIMIT
        while chosen and self._share(len(chosen)) < MIN_READ_TOKENS:
            src = chosen.pop()
            src.state, src.reason = _SKIPPED, REASON_TOKEN_BUDGET
        if chosen:
            self.rounds += 1
        return chosen

    def _share(self, remaining: int) -> int:
        """Tokens each of ``remaining`` reads may render: what is left of the
        allowance, less the room kept for their notes and the summary."""
        left = self.allowance - self.tokens - SUMMARY_RESERVE - NOTE_RESERVE * remaining
        return left // remaining if remaining else 0

    def budget_for(self, remaining: int) -> int:
        """The budget of the next read, ``remaining`` reads (it included) to
        go. Read one at a time, so what one read did not use is left for the
        others."""
        return max(MIN_READ_TOKENS, self._share(remaining))

    def record_attempt(self, src: _Source, evidence: Any, *, note_tokens: int) -> Attempt:
        """Take in an automatic read's result; one attempt per file, never
        retried. A read counts as complete only when the passage asked for was
        delivered whole from a current copy of the file."""
        self.calls += 1
        src.state = _ATTEMPTED
        outcome, reason, continuation = FAILED, None, None
        if not isinstance(evidence, DocumentEvidence):
            reason = "no_result"
        elif evidence.error:
            reason = f"read_failed:{evidence.error}"
        elif evidence.focus_status == FOCUS_UNRESOLVED:
            reason = "passage_gone"
        elif evidence.stale:
            reason = "file_changed"
        elif evidence.metadata_only:
            reason = "metadata_only"
        elif evidence.focus_status == FOCUS_COMPLETE:
            outcome = COMPLETE
        elif evidence.focus_status == FOCUS_PARTIAL:
            outcome, reason, continuation = PARTIAL, "budget", evidence.continuation
        else:
            reason = "passage_not_shown"
        if isinstance(evidence, DocumentEvidence):
            self.tokens += int(evidence.rendered_tokens or 0)
            # A failed read (the passage gone, the file changed, only its
            # details indexed) delivers nothing that counts as examined.
            if outcome != FAILED:
                self._note_delivered(evidence)
        self.tokens += int(note_tokens or 0)
        src.outcome, src.reason = outcome, reason
        self._settle()
        return Attempt(token=src.token, candidate=src.candidate or "", outcome=outcome, reason=reason,
                       continuation=continuation, position=src.position)

    def add_tokens(self, n: int) -> None:
        self.tokens += int(n or 0)

    # ── what the model is told after a round ────────────────────────────

    def round_note(self, attempts: list[Attempt], read_fn: str) -> str:
        """The compact summary appended after a round's last automatic read.
        Names files by token only: titles and paths stay inside the
        results' document blocks."""
        eligible = [s for s in self.sources.values() if s.eligible]
        done = []
        for a in attempts:
            where = f" {a.position}" if a.position else ""
            if a.outcome == COMPLETE:
                done.append(f"{a.token}{where} complete")
            elif a.outcome == PARTIAL:
                # Not "page=N": an automatic read is laid out in a smaller
                # budget than the model's own call, and pages cut at different
                # places in different budgets — "page=2" would skip text. A
                # plain re-read starts from the passage, at the full budget.
                done.append(f"{a.token}{where} partial (for the rest, read file=\"{a.candidate}\" again)")
            else:
                done.append(f"{a.token}{where} could not be read ({a.reason or 'failed'})")
        left = [s for s in sorted(eligible, key=lambda s: s.order)
                if s.state == _SKIPPED and s.reason in (REASON_CALL_LIMIT, REASON_TOKEN_BUDGET)]
        parts = [
            f"[Automatic document review — the search returned {len(eligible)} relevant files; the matching "
            f"passage of each one not yet read in full was read for you above, with the passages around "
            f"it: {'; '.join(done)}."
        ]
        if left:
            named = ", ".join(s.token for s in left[:_SUMMARY_LISTED])
            more = f" and {len(left) - _SUMMARY_LISTED} more" if len(left) > _SUMMARY_LISTED else ""
            parts.append(f"Not reviewed (the automatic limit was reached): {named}{more} — read them with "
                         f"{read_fn} if they matter.")
        parts.append(
            "Each read shows the one passage the search ranked first in that file, which may only touch "
            "the subject (a comparison, a list, an overview): when a file's own section on the subject is "
            "not above, read it before answering — search within that file (filters.file_ids) or read its "
            "section or pages. Then assess what each file adds, whatever its language: combine complementary "
            "evidence, attribute any difference to the file it comes from, cite the passages that support "
            "each claim, and leave out a file that adds nothing. Never state that a file does not cover "
            "something unless you read that part of it; if a relevant file could not be reviewed and that "
            "limits the answer, say so.]"
        )
        return " ".join(parts)

    # ── the turn's record ───────────────────────────────────────────────

    def summary(self, final_text: str | None = None) -> dict[str, Any] | None:
        """The versioned ``document_review`` record of the turn, or None when
        it searched and read no documents."""
        if not (self.searches or self.reads or self.calls):
            return None
        from app.documents.cite import parse_tokens

        whole = (self._examined | {s.fid for s in self.sources.values()
                                   if s.eligible and self._whole(s.candidate)}) - self._stale
        by_read = self._examined_by_read - self._stale
        examined, partial, failed, unexamined = [], [], [], []
        reasons: dict[str, str] = {}
        for src in sorted(self.sources.values(), key=lambda s: s.order):
            # An automatic read that failed or came through in part is
            # reported as such unless a read delivered the file after all —
            # a whole search snippet does not hide it.
            if src.outcome == FAILED and src.fid not in by_read:
                failed.append(src.fid)
            elif src.outcome == PARTIAL and src.fid not in by_read:
                partial.append(src.fid)
            elif src.fid in whole:
                examined.append(src.fid)
                continue
            elif src.eligible:
                unexamined.append(src.fid)
            else:
                continue  # an ineligible file, not examined: nothing to report
            reason = src.reason or (REASON_NOT_REVIEWED if src.state == _PENDING else None)
            if src.fid in self._stale and not reason:
                reason = "file_changed"
            if reason:
                reasons[src.fid] = reason
        # Files read by the agent itself that no search returned.
        for fid in sorted(whole - set(self.sources)):
            examined.append(fid)
        cited = list(dict.fromkeys(t["cite_id"] for t in parse_tokens(final_text or "")))
        return {
            "v": SUMMARY_VERSION,
            "searches": self.searches,
            "reads": self.reads,
            "returned": len(self.sources),
            "eligible": sum(1 for s in self.sources.values() if s.eligible),
            "examined": examined,
            "partial": partial,
            "failed": failed,
            "unexamined": unexamined,
            "automatic": {"calls": self.calls, "tokens": self.tokens, "rounds": self.rounds},
            "reasons": reasons,
            "cited": cited,
        }


def count_tokens(text: str) -> int:
    """Tokens of ``text``, counted like the documentation results' own
    budget (the chars/4 estimate without tiktoken)."""
    if not text:
        return 0
    try:
        from app.utils.common import count_content_tokens

        return count_content_tokens(text)
    except Exception:  # noqa: BLE001
        return len(text) // 4


def log_line(summary: dict[str, Any], *, profile: str, conversation: Any, run: Any) -> str:
    """The INFO line of a turn's review: identifiers and counts, no document
    names or text."""
    auto = summary.get("automatic") or {}
    reasons: dict[str, int] = {}
    for r in (summary.get("reasons") or {}).values():
        reasons[r] = reasons.get(r, 0) + 1
    return (
        f"[document_review] profile={profile} conversation={conversation} run={run} "
        f"searches={summary.get('searches')} reads={summary.get('reads')} returned={summary.get('returned')} "
        f"eligible={summary.get('eligible')} examined={len(summary.get('examined') or [])} "
        f"partial={len(summary.get('partial') or [])} failed={len(summary.get('failed') or [])} "
        f"unexamined={len(summary.get('unexamined') or [])} cited={len(summary.get('cited') or [])} "
        f"auto_calls={auto.get('calls', 0)} auto_tokens={auto.get('tokens', 0)} "
        f"reasons={dict(sorted(reasons.items()))}"
    )


def trusted_evidence(tool: Any, event: Any, *, tool_id: str) -> DocumentEvidence | None:
    """The delivery record on ``event`` when — and only when — it came from
    the built-in Documentation Search group: the record is a typed object the
    built-in path attaches, so another kind of tool (MCP, a skill) or a
    same-named look-alike cannot supply one, and none is ever read out of
    text."""
    from app.tools.base import ToolType

    evidence = getattr(event, "evidence", None)
    if evidence is None or not isinstance(evidence, DocumentEvidence):
        return None
    if getattr(tool, "tool_type", None) is not ToolType.BUILTIN or getattr(tool, "tool_id", None) != tool_id:
        return None
    return evidence


__all__ = [
    "Attempt",
    "COMPLETE",
    "DocumentReview",
    "FAILED",
    "MAX_AUTO_READS",
    "MIN_READ_TOKENS",
    "NOTE_RESERVE",
    "ORIGIN",
    "PARTIAL",
    "REASON_CALL_LIMIT",
    "REASON_INSTANT",
    "REASON_NEW_INPUT",
    "REASON_NO_STEP",
    "REASON_NOT_REVIEWED",
    "REASON_READ_UNAVAILABLE",
    "REASON_RESEARCH",
    "REASON_TOKEN_BUDGET",
    "SUMMARY_RESERVE",
    "SUMMARY_VERSION",
    "TOKEN_ALLOWANCE",
    "count_tokens",
    "log_line",
    "trusted_evidence",
]
