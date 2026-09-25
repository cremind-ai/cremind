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
database (``userdoc_research_jobs``) and resumes from it after a restart.
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
COMPLETE = "complete"
# Stopped at the token budget or the time limit; the dossier says what was
# covered and what was not.
PARTIAL = "partial"
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


@dataclass
class Evidence:
    """One verified piece of source text behind a claim."""

    token: str                      # "[ud:k7m2xq9a#1f3c9a0b]"
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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
    )


__all__ = [
    "ACTIVE", "AMENDED", "Authority", "CANCELLED", "COMPLETE", "Cell", "Clarification", "CompiledTable",
    "Conflict", "CoverageRow", "DOMAINS", "DOMAIN_FINANCIAL", "DOMAIN_GENERAL", "DOMAIN_LEGAL", "Dossier",
    "Evidence", "FAILED", "FINAL", "Finding", "INTERRUPTED", "IN_FORCE", "Issue", "JobView", "MODES", "MODE_ANALYZE",
    "MODE_COMPILE", "NEEDS_CLARIFICATION", "NEEDS_CONFIRMATION", "PARTIAL", "PLANNING", "QUEUED",
    "READ_FULL", "READ_NONE", "READ_PARTIAL", "ROLE_PRIMARY", "ROLE_REFERENCE", "RUNNING", "STANCES",
    "SUPERSEDED", "UNKNOWN", "UNREAD_REASONS", "WAITING", "dossier_from_dict",
]
