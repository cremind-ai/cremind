"""Data contracts of deep research over the user's documents.

A research job answers one question over a *scope* of the user's files and
returns a :class:`Dossier`: what was read (the coverage table — every file in
scope, read or not, and why not), and what was found, each claim carrying
evidence the job itself checked against the source text.

Two modes share the job machinery:

- ``compile`` — exhaustive: every file in scope is read in full and a table
  is extracted from it (map), then merged (reduce), conflicts kept side by
  side. "Compile the business results in MKT-report".
- ``analyze`` — question-driven: the primary scope is read in full, the
  issues it raises are searched in the reference scope from several angles
  (including counter-evidence), cited provisions are read in full and their
  cross-references followed, and every quote is verified. With
  ``domain="legal"`` the edition of each authority is chosen explicitly and
  the job stops to ask when it cannot tell. "A solution for client ABC's
  land dispute under the 2020 land law".

Everything here is JSON-serialisable: a job checkpoints its state to the main
database (``document_research_jobs``) and resumes from it after a restart.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# ── modes and domains ─────────────────────────────────────────────────────
MODE_COMPILE = "compile"
MODE_ANALYZE = "analyze"
MODES = (MODE_COMPILE, MODE_ANALYZE)

DOMAIN_LEGAL = "legal"
DOMAIN_FINANCIAL = "financial"
DOMAIN_GENERAL = "general"
DOMAINS = (DOMAIN_LEGAL, DOMAIN_FINANCIAL, DOMAIN_GENERAL)

# ── job status ────────────────────────────────────────────────────────────
QUEUED = "queued"
PLANNING = "planning"
RUNNING = "running"
# The job stopped to ask the user something (which edition of a law; which
# of several folders named "ABC"); answered with continue_job + answers.
NEEDS_CLARIFICATION = "needs_clarification"
# The job stopped before spending past its budget, or before concluding
# with files in scope it could not read; answered with continue_job + answers.
NEEDS_CONFIRMATION = "needs_confirmation"
# Every required stage finished and — analyze — every issue has verified
# findings. Never a job that found no evidence.
COMPLETE = "complete"
# Usable work, but not the whole answer: the evidence found does not cover
# every issue, a law the question named is not among the documents, or the
# token budget or the time limit stopped it. The dossier's ``outcome`` says
# which, and what was covered.
PARTIAL = "partial"
# An execution or provider failure prevented useful research.
FAILED = "failed"
CANCELLED = "cancelled"
# The server restarted mid-job; continue_job resumes from the checkpoint.
INTERRUPTED = "interrupted"

ACTIVE = frozenset({QUEUED, PLANNING, RUNNING})
WAITING = frozenset({NEEDS_CLARIFICATION, NEEDS_CONFIRMATION, INTERRUPTED})
FINAL = frozenset({COMPLETE, PARTIAL, FAILED, CANCELLED})

# ── coverage ──────────────────────────────────────────────────────────────
ROLE_PRIMARY = "primary"      # the case / the folder to compile
ROLE_REFERENCE = "reference"  # the law, the policy, the standard

READ_FULL = "read"
READ_PARTIAL = "partial"
READ_NONE = "unread"

# Why a file in scope was not (fully) read — the file's own index status
# (status_reason / caption_state), or the job's.
UNREAD_REASONS = (
    "legacy_format", "encrypted", "awaiting_vision", "awaiting_consent", "over_cap", "too_large",
    "error", "placeholder", "metadata_only", "awaiting_extractor", "not_indexed_yet",
    "budget", "time", "not_relevant",
)

# ── findings ──────────────────────────────────────────────────────────────
STANCES = ("supports", "contradicts", "exception", "condition", "procedure", "definition", "fact")

# Authority status, judged only against the documents in the corpus.
IN_FORCE = "in_force"
SUPERSEDED = "superseded"
AMENDED = "amended"
UNKNOWN = "unknown"

# ── outcome: why a job ended as it did ─────────────────────────────────────
# Every issue has verified findings.
OUTCOME_EVIDENCED = "evidenced"
# Some issues have verified findings, others none.
OUTCOME_INSUFFICIENT = "insufficient_evidence"
# A law the question named is not among the documents (a topical match does
# not stand in for it).
OUTCOME_UNRESOLVED_INSTRUMENT = "unresolved_instrument"
# The searches ran and found no candidate document.
OUTCOME_NO_CANDIDATES = "no_candidates"
# Candidates were found, none could be read (encrypted, not indexed yet, …).
OUTCOME_UNREADABLE = "candidates_unreadable"
# Candidates were found, none was a relevant legal document.
OUTCOME_REJECTED = "candidates_rejected"
# An explicit reference scope held no readable file.
OUTCOME_EMPTY_SCOPE = "empty_scope"
# Documents were read; no finding survived verification.
OUTCOME_NO_FINDINGS = "no_verified_findings"
# A search or the model's structured answers failed.
OUTCOME_RETRIEVAL_FAILED = "retrieval_failed"
OUTCOME_MODEL_FAILED = "model_failed"
# Any other failure of the job itself.
OUTCOME_FAILED = "execution_failed"
# The token budget or the time limit stopped the job.
OUTCOME_BUDGET = "budget"
OUTCOME_TIME = "time"
OUTCOME_CANCELLED = "cancelled"
# compile: every readable file in scope was read (or there was none).
OUTCOME_COMPILED = "compiled"
# Still working (a checkpoint's outcome).
OUTCOME_RUNNING = "running"

# Reasons that mean "the documents did not yield an evidenced answer" (as
# opposed to a job stopped by a limit or a failure).
INSUFFICIENT_OUTCOMES = frozenset({
    OUTCOME_INSUFFICIENT, OUTCOME_UNRESOLVED_INSTRUMENT, OUTCOME_NO_CANDIDATES, OUTCOME_UNREADABLE,
    OUTCOME_REJECTED, OUTCOME_EMPTY_SCOPE, OUTCOME_NO_FINDINGS,
})
OUTCOME_TEXT = {
    OUTCOME_EVIDENCED: "every issue has verified findings",
    OUTCOME_INSUFFICIENT: "insufficient evidence: some issues have no verified finding",
    OUTCOME_UNRESOLVED_INSTRUMENT: "a law or document the question names is not among the indexed documents",
    OUTCOME_NO_CANDIDATES: "the searches found no candidate document",
    OUTCOME_UNREADABLE: "candidate documents were found but none could be read",
    OUTCOME_REJECTED: "candidate documents were found but none was a relevant legal document",
    OUTCOME_EMPTY_SCOPE: "the reference scope held no readable file",
    OUTCOME_NO_FINDINGS: "documents were read but no finding could be verified against them",
    OUTCOME_RETRIEVAL_FAILED: "a search failed",
    OUTCOME_MODEL_FAILED: "the research model's answers failed",
    OUTCOME_FAILED: "the job failed",
    OUTCOME_BUDGET: "stopped at the token budget",
    OUTCOME_TIME: "stopped at the time limit",
    OUTCOME_CANCELLED: "cancelled",
    OUTCOME_COMPILED: "every readable file in scope was compiled",
    OUTCOME_RUNNING: "still running",
}


@dataclass
class Evidence:
    """One verified piece of source text behind a claim."""

    token: str                      # "[doc:k7m2xq9a#1f3c9a0b]"
    quote: str                      # the SOURCE's wording (canonical), never the model's
    quote_status: str               # exact | normalized | fuzzy (quotes.py)
    label: str = ""                 # "Điều 203, khoản 2" / "p. 3" / "sheet 'Q3' A2:F41"
    rel_path: str = ""


@dataclass
class CoverageRow:
    fid: str                        # the file's cite id
    rel_path: str
    kind: str
    role: str = ROLE_PRIMARY
    read: str = READ_NONE           # read | partial | unread
    reason: str | None = None       # one of UNREAD_REASONS when not fully read
    chunks_read: int = 0
    chunks_total: int = 0


@dataclass
class Authority:
    """A legal (or policy) document found in the reference scope."""

    fid: str
    title: str
    rel_path: str = ""
    number: str | None = None       # "31/2024/QH15"
    issued: str | None = None       # ISO date
    effective: str | None = None    # ISO date
    consolidated: bool = False      # a VBHN / consolidated text
    status: str = UNKNOWN           # in_force | superseded | amended | unknown
    replaced_by: str | None = None  # fid of the document that replaces it, if in the corpus
    used: bool = False              # the job relied on this edition
    why: str = ""                   # why it was (not) chosen


@dataclass
class Finding:
    issue: str                      # which issue it bears on ("" for a fact)
    stance: str                     # one of STANCES
    text: str                       # the statement, in the job's words
    provision: str = ""             # "Điều 203 Luật Đất đai 31/2024/QH15"
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Issue:
    title: str
    findings: list[Finding] = field(default_factory=list)
    xrefs: list[str] = field(default_factory=list)          # provisions followed ("Điều 236")
    open_questions: list[str] = field(default_factory=list)


@dataclass
class Cell:
    value: str
    tokens: list[str] = field(default_factory=list)          # evidence tokens for this value


@dataclass
class Conflict:
    key: str                        # "Q3 2025 revenue"
    values: list[Cell] = field(default_factory=list)          # every differing value, each with its source


@dataclass
class CompiledTable:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Cell]] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    # ``_files`` entries (the CSV and Markdown artifacts), for the tool result.
    artifacts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Clarification:
    """What a job stopped to ask. ``kind`` ``edition``: which edition of an
    authority to use (``candidates`` are Authority dicts); ``scope``: which
    of several matching folders/files; ``unread``: files in scope could not
    be read — continue anyway, or describe/OCR them now; ``budget``: the
    estimate exceeds the budget."""

    kind: str
    question: str
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # The answer keys continue_job accepts for this question, e.g.
    # {"edition": "a fid from candidates"} or {"confirm": "true|false"}.
    answer_keys: dict[str, str] = field(default_factory=dict)


@dataclass
class Outcome:
    """Why the job ended as it did, in counts — the part of the dossier that
    says whether its findings answer the question at all. Counts only (and
    the searches it ran, which the question and the planner wrote): never a
    document's text, so it may be shown outside the untrusted-content block.

    ``queries``/``candidates``/``selected`` are the authority discovery's
    (analyze without a reference scope; zero otherwise); ``files_read`` and
    ``provisions_read`` are what the model was actually shown; ``findings``
    counts verified findings (each with evidence the job checked);
    ``unresolved`` the issues without one plus the named instruments not
    found. ``stopped_early``: discovery or reading was cut short (a limit,
    the budget, the time). ``trace`` is the discovery's diagnostic record:
    each query with the candidates it found, why candidates were rejected,
    which were selected."""

    reason: str = OUTCOME_RUNNING
    detail: str = ""
    queries: int = 0
    candidates: int = 0
    selected: int = 0
    files_read: int = 0
    provisions_read: int = 0
    findings: int = 0
    unresolved: int = 0
    stopped_early: bool = False
    incomplete_provisions: list[str] = field(default_factory=list)
    trace: dict[str, Any] | None = None

    @property
    def insufficient(self) -> bool:
        return self.reason in INSUFFICIENT_OUTCOMES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def outcome_from_dict(d: Any) -> Outcome | None:
    """An :class:`Outcome` from its JSON form; None for a dossier saved
    before outcomes existed (or anything that is not one)."""
    if not isinstance(d, dict):
        return None
    known = set(Outcome.__dataclass_fields__)
    try:
        return Outcome(**{k: v for k, v in d.items() if k in known})
    except TypeError:
        return None


@dataclass
class Dossier:
    job_id: str
    mode: str
    domain: str
    question: str
    status: str
    coverage: list[CoverageRow] = field(default_factory=list)
    authorities: list[Authority] = field(default_factory=list)
    version_notes: list[str] = field(default_factory=list)
    facts: list[Finding] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    compiled: CompiledTable | None = None
    gaps: list[str] = field(default_factory=list)
    # Quotes the model produced that did not match their source — dropped,
    # counted so the reader knows the job checked.
    rejected_quotes: int = 0
    notes: list[str] = field(default_factory=list)
    clarification: Clarification | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    # Why it ended as it did (None in dossiers saved before it existed).
    outcome: Outcome | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def verified_findings(self) -> int:
        """Issue findings that carry verified evidence (facts from the case
        files are not an answer to the question)."""
        return sum(1 for i in self.issues for f in i.findings if f.evidence)


@dataclass
class JobView:
    """A job as callers see it (the tool leaf, the REST API, the CLI through
    REST): the row, the live progress, and the dossier so far."""

    job_id: str
    profile: str
    status: str
    mode: str
    domain: str
    question: str
    conversation_id: str | None = None
    phase: str | None = None
    created_at: float | None = None      # epoch ms
    updated_at: float | None = None
    finished_at: float | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    budget: int = 0
    error: str | None = None
    # {"phase", "done", "total", "steps": [{"id", "label", "detail", "status"}]} — the
    # last dozen steps; empty for a job that is not running in this process.
    progress: dict[str, Any] = field(default_factory=dict)
    dossier: Dossier | None = None

    @property
    def clarification(self) -> Clarification | None:
        return self.dossier.clarification if self.dossier else None

    def to_dict(self, *, with_dossier: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not with_dossier:
            d.pop("dossier", None)
        return d


def dossier_from_dict(d: dict[str, Any] | None) -> Dossier | None:
    """Rebuild a :class:`Dossier` from its JSON form (a checkpoint)."""
    if not d:
        return None
    ev = lambda xs: [Evidence(**e) for e in (xs or [])]  # noqa: E731
    finding = lambda f: Finding(**{**f, "evidence": ev(f.get("evidence"))})  # noqa: E731
    compiled = d.get("compiled")
    table = None
    if compiled:
        table = CompiledTable(
            columns=list(compiled.get("columns") or []),
            rows=[{k: Cell(**v) for k, v in (r or {}).items()} for r in compiled.get("rows") or []],
            conflicts=[Conflict(key=c["key"], values=[Cell(**v) for v in c.get("values") or []])
                       for c in compiled.get("conflicts") or []],
            artifacts=list(compiled.get("artifacts") or []),
        )
    clar = d.get("clarification")
    return Dossier(
        job_id=d["job_id"], mode=d["mode"], domain=d["domain"], question=d["question"], status=d["status"],
        coverage=[CoverageRow(**r) for r in d.get("coverage") or []],
        authorities=[Authority(**a) for a in d.get("authorities") or []],
        version_notes=list(d.get("version_notes") or []),
        facts=[finding(f) for f in d.get("facts") or []],
        issues=[Issue(title=i["title"], findings=[finding(f) for f in i.get("findings") or []],
                      xrefs=list(i.get("xrefs") or []), open_questions=list(i.get("open_questions") or []))
                for i in d.get("issues") or []],
        compiled=table,
        gaps=list(d.get("gaps") or []),
        rejected_quotes=int(d.get("rejected_quotes") or 0),
        notes=list(d.get("notes") or []),
        clarification=Clarification(**clar) if clar else None,
        tokens_in=int(d.get("tokens_in") or 0),
        tokens_out=int(d.get("tokens_out") or 0),
        outcome=outcome_from_dict(d.get("outcome")),
    )


__all__ = [
    "ACTIVE", "AMENDED", "Authority", "CANCELLED", "COMPLETE", "Cell", "Clarification", "CompiledTable",
    "Conflict", "CoverageRow", "DOMAINS", "DOMAIN_FINANCIAL", "DOMAIN_GENERAL", "DOMAIN_LEGAL", "Dossier",
    "Evidence", "FAILED", "FINAL", "Finding", "INSUFFICIENT_OUTCOMES", "INTERRUPTED", "IN_FORCE", "Issue", "JobView",
    "MODES", "MODE_ANALYZE", "MODE_COMPILE", "NEEDS_CLARIFICATION", "NEEDS_CONFIRMATION", "OUTCOME_BUDGET",
    "OUTCOME_CANCELLED", "OUTCOME_COMPILED", "OUTCOME_EMPTY_SCOPE", "OUTCOME_EVIDENCED", "OUTCOME_FAILED",
    "OUTCOME_INSUFFICIENT",
    "OUTCOME_MODEL_FAILED", "OUTCOME_NO_CANDIDATES", "OUTCOME_NO_FINDINGS", "OUTCOME_REJECTED",
    "OUTCOME_RETRIEVAL_FAILED", "OUTCOME_RUNNING", "OUTCOME_TEXT", "OUTCOME_TIME", "OUTCOME_UNREADABLE",
    "OUTCOME_UNRESOLVED_INSTRUMENT", "Outcome", "PARTIAL", "PLANNING", "QUEUED", "READ_FULL", "READ_NONE",
    "READ_PARTIAL", "ROLE_PRIMARY", "ROLE_REFERENCE", "RUNNING", "STANCES", "SUPERSEDED", "UNKNOWN",
    "UNREAD_REASONS", "WAITING", "dossier_from_dict", "outcome_from_dict",
]
