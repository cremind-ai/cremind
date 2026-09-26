"""What a research result hands the chat agent: :class:`ResearchDelivery`.

A research dossier is rendered for the agent in pages sized to its tool-result
budget, so the verified findings of a long dossier can sit on page 2 while the
agent reads page 1 — and a job can finish with no verified finding at all. In
the incidents behind this record, the agent went on to answer anyway: with
procedural advice no document supported, or by asking the user to upload a
decree the index already held.

So every research result the built-in tool returns (``research``, and ``read``
of ``research:<id>``), and every result the job delivers on its own into a
conversation, carries a typed record of what it delivered — built by the
server from the job and the rendered page, never read out of text a model or
a document could write:

- the job, its status and its outcome's reason;
- how many verified findings the dossier holds, their evidence tokens, and
  which pages carry them; the tokens printed on *this* page. Research findings
  are the job's reading, not the agent's: a finding delivered does not mean
  the agent read its source in full, so this record is kept apart from the
  passage-delivery evidence of ordinary searches and reads
  (:mod:`app.documents.delivery`);
- for a settled legal analysis with no usable verified evidence, the
  insufficiency summary the agent ends the turn with instead of letting the
  model write legal conclusions from nothing (:func:`insufficiency_summary`).

The response contract (:data:`RESPONSE_CONTRACT`) is the same text wherever a
settled job reaches the agent: the tool's page, a dossier page read later, and
the delivery turn.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.documents.research.types import (
    COMPLETE,
    DOMAIN_LEGAL,
    FAILED,
    FINAL,
    MODE_ANALYZE,
    OUTCOME_BUDGET,
    OUTCOME_MODEL_FAILED,
    OUTCOME_NO_CANDIDATES,
    OUTCOME_RETRIEVAL_FAILED,
    OUTCOME_TIME,
    OUTCOME_UNREADABLE,
    OUTCOME_UNRESOLVED_INSTRUMENT,
    PARTIAL,
    JobView,
)

DELIVERY_VERSION = 1
# The evidence tokens a record carries at most.
MAX_TOKENS = 200

RESPONSE_CONTRACT = (
    "How to answer: base every conclusion on the verified findings above and cite their [doc:…] tokens exactly "
    "as printed; state the unresolved issues, the gaps and the files not read in full. Never say an indexed "
    "document is missing or ask the user to upload it again. With no verified finding, say the research found "
    "no evidence in the documents — do not give legal conclusions from general knowledge instead."
)


@dataclass(frozen=True)
class ResearchDelivery:
    """What one research result delivered to the agent (see the module
    docstring). ``error`` is set, and nothing else is meaningful, when the
    call failed before any job state was shown."""

    job_id: str = ""
    status: str = ""
    mode: str = ""
    domain: str = ""
    outcome: str | None = None
    # Verified findings in the dossier, and their evidence tokens.
    findings: int = 0
    evidence: tuple[str, ...] = ()
    # The dossier page this result showed, of how many, and the tokens it
    # printed; the pages whose text carries evidence tokens; the token budget
    # each page was cut to (0: unknown).
    page: int = 1
    pages: int = 1
    delivered: tuple[str, ...] = ()
    evidence_pages: tuple[int, ...] = ()
    page_tokens: int = 0
    # A settled legal analysis without usable evidence: what the turn ends
    # with (server-rendered, in the question's language).
    insufficiency: str | None = None
    error: str | None = None
    version: int = DELIVERY_VERSION

    @property
    def settled(self) -> bool:
        return self.status in FINAL

    @property
    def valid(self) -> bool:
        """A job's state was shown: the research took the question over."""
        return not self.error and bool(self.job_id)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        for key in ("evidence", "delivered", "evidence_pages"):
            d[key] = list(d[key])
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "ResearchDelivery | None":
        if not isinstance(d, dict):
            return None
        known = set(cls.__dataclass_fields__)
        kw = {k: v for k, v in d.items() if k in known}
        for key in ("evidence", "delivered"):
            kw[key] = tuple(str(x) for x in kw.get(key) or ())
        kw["evidence_pages"] = tuple(int(x) for x in kw.get("evidence_pages") or () if str(x).isdigit())
        tokens = str(kw.get("page_tokens") or 0)
        kw["page_tokens"] = int(tokens) if tokens.isdigit() else 0
        try:
            return cls(**kw)
        except TypeError:
            return None


def failed_delivery(code: str) -> ResearchDelivery:
    """The record of a research call that failed (bad arguments, no such
    job, busy, unavailable): nothing was handed over."""
    return ResearchDelivery(error=code or "Error")


def evidence_tokens(view: JobView) -> list[str]:
    """The evidence tokens of the dossier's verified findings, in order."""
    d = view.dossier
    if d is None:
        return []
    out: list[str] = []
    for issue in d.issues:
        for f in issue.findings:
            for e in f.evidence:
                if e.token and e.token not in out:
                    out.append(e.token)
    return out[:MAX_TOKENS]


def needs_insufficiency(view: JobView) -> bool:
    """A settled legal analysis with no verified finding: the chat must not
    turn it into an answer."""
    d = view.dossier
    return (view.domain == DOMAIN_LEGAL and view.mode == MODE_ANALYZE and view.status in (COMPLETE, PARTIAL, FAILED)
            and (d is None or d.verified_findings() == 0))


def build_delivery(view: JobView, *, text: str, page: int, pages: int,
                   evidence_pages: list[int] | None = None, page_tokens: int | None = None) -> ResearchDelivery:
    """The record of ``view`` shown as ``text`` (page ``page`` of
    ``pages``, each cut to ``page_tokens``)."""
    from app.documents.cite import TOKEN_RE

    evidence = evidence_tokens(view)
    printed = list(dict.fromkeys(m.group(0) for m in TOKEN_RE.finditer(text or "")))
    d = view.dossier
    return ResearchDelivery(
        job_id=view.job_id, status=view.status, mode=view.mode, domain=view.domain,
        outcome=d.outcome.reason if d is not None and d.outcome is not None else None,
        findings=d.verified_findings() if d is not None else 0, evidence=tuple(evidence),
        page=page, pages=pages, delivered=tuple(t for t in printed if t in set(evidence))[:MAX_TOKENS],
        evidence_pages=tuple(sorted(set(evidence_pages or []))), page_tokens=max(0, int(page_tokens or 0)),
        insufficiency=insufficiency_summary(view) if needs_insufficiency(view) else None,
    )


# ── the insufficiency summary ──────────────────────────────────────────────


def _lang(view: JobView) -> str:
    from app.documents.query.terms import analyze

    return "vi" if analyze(view.question or "").lang in ("vi", "vi_nodiac") else "en"


def _clip(text: str, n: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= n else flat[:n - 1].rstrip() + "…"


_NEXT_EN = {
    OUTCOME_NO_CANDIDATES: "None of the indexed documents matched these searches. If a document that governs this "
                           "exists and is not among them, add it to your documents folder or name it (its number "
                           "helps), and ask again.",
    OUTCOME_UNREADABLE: "The documents found could not be read (see the gaps above): make them readable — describe "
                        "or OCR them, or wait for indexing to finish — and ask me to continue the research.",
    OUTCOME_BUDGET: "The job stopped before it could verify anything: ask me to continue it, or raise the research "
                    "token budget.",
    OUTCOME_TIME: "The job reached its time limit before it could verify anything: ask me to continue it.",
    OUTCOME_MODEL_FAILED: "The research model failed; ask again to retry.",
    OUTCOME_RETRIEVAL_FAILED: "The document search failed; ask again to retry.",
    OUTCOME_UNRESOLVED_INSTRUMENT: "Name the document that holds the law you mean (its number helps), or point me "
                                   "at the folder it is in.",
}
_NEXT_EN_DEFAULT = ("The documents found did not yield a provision that could be verified. Name the specific "
                    "provision or document to rely on, narrow the question, or point me at the folder to use, "
                    "and ask again.")
_NEXT_VI = {
    OUTCOME_NO_CANDIDATES: "Không tài liệu nào đã lập chỉ mục khớp với các lượt tìm kiếm này. Nếu có văn bản điều "
                           "chỉnh vấn đề này mà chưa nằm trong số đó, hãy thêm vào thư mục tài liệu hoặc nêu tên "
                           "(kèm số hiệu) rồi hỏi lại.",
    OUTCOME_UNREADABLE: "Các tài liệu tìm được chưa đọc được (xem phần thiếu sót ở trên): hãy làm cho chúng đọc "
                        "được — mô tả hoặc OCR, hoặc chờ lập chỉ mục xong — rồi yêu cầu tiếp tục nghiên cứu.",
    OUTCOME_BUDGET: "Tác vụ dừng trước khi kiểm chứng được điều gì: hãy yêu cầu tiếp tục, hoặc tăng ngân sách "
                    "token cho nghiên cứu.",
    OUTCOME_TIME: "Tác vụ hết thời gian trước khi kiểm chứng được điều gì: hãy yêu cầu tiếp tục.",
    OUTCOME_MODEL_FAILED: "Mô hình nghiên cứu gặp lỗi; hãy hỏi lại để thử lại.",
    OUTCOME_RETRIEVAL_FAILED: "Tìm kiếm tài liệu gặp lỗi; hãy hỏi lại để thử lại.",
    OUTCOME_UNRESOLVED_INSTRUMENT: "Hãy nêu văn bản chứa quy định bạn muốn dùng (kèm số hiệu), hoặc chỉ thư mục "
                                   "chứa văn bản đó.",
}
_NEXT_VI_DEFAULT = ("Các tài liệu tìm được không cho ra điều khoản nào kiểm chứng được. Hãy nêu cụ thể điều "
                    "khoản hoặc văn bản cần dựa vào, thu hẹp câu hỏi, hoặc chỉ thư mục cần dùng, rồi hỏi lại.")


def insufficiency_summary(view: JobView) -> str:
    """The answer to a legal question whose research found no verified
    evidence: what was searched and read, why nothing was established, and
    what would let the research succeed — rendered here, never by the model,
    so no legal conclusion can slip in. Plain text; issue titles and gaps
    come from the job's own dossier."""
    d = view.dossier
    o = d.outcome if d is not None else None
    reason = o.reason if o is not None else None
    vi = _lang(view) == "vi"
    issues = [i.title for i in (d.issues if d is not None else [])][:6]
    gaps = [g for g in (d.gaps if d is not None else []) if not g.startswith("No provision with verified evidence")]
    lines: list[str] = []
    if vi:
        lines.append(f"Tôi không thể trả lời câu hỏi này dựa trên tài liệu của bạn: nghiên cứu (tác vụ {view.job_id}, "
                     f"{view.status}) không tìm thấy bằng chứng nào được kiểm chứng trong các tài liệu đã lập chỉ "
                     "mục, nên tôi không đưa ra kết luận pháp lý.")
        if o is not None:
            lines.append(f"Đã thực hiện: {o.queries} lượt tìm kiếm, {o.candidates} tài liệu ứng viên, {o.selected} "
                         f"tài liệu được chọn; đã đọc {o.files_read} tệp và {o.provisions_read} điều khoản.")
            if o.detail:
                lines.append(f"Lý do: {_clip(o.detail, 300)}.")
        if issues:
            lines.append("Chưa xác lập được:")
            lines += [f"- {_clip(t, 200)}" for t in issues]
        if gaps:
            lines.append("Thiếu sót đã ghi nhận:")
            lines += [f"- {_clip(g, 240)}" for g in gaps[:5]]
        lines.append(f"Bước tiếp theo: {_NEXT_VI.get(reason or '', _NEXT_VI_DEFAULT)}")
    else:
        lines.append(f"I could not answer this from your documents: the research (job {view.job_id}, {view.status}) "
                     "found no verified evidence in the indexed documents, so I am not giving legal conclusions.")
        if o is not None:
            lines.append(f"What was done: {o.queries} search(es) found {o.candidates} candidate document(s), "
                         f"{o.selected} selected; {o.files_read} file(s) and {o.provisions_read} provision(s) read.")
            if o.detail:
                lines.append(f"Why: {_clip(o.detail, 300)}.")
        if issues:
            lines.append("Not established:")
            lines += [f"- {_clip(t, 200)}" for t in issues]
        if gaps:
            lines.append("Gaps recorded:")
            lines += [f"- {_clip(g, 240)}" for g in gaps[:5]]
        lines.append(f"What would help: {_NEXT_EN.get(reason or '', _NEXT_EN_DEFAULT)}")
    return "\n".join(lines)


__all__ = [
    "DELIVERY_VERSION", "RESPONSE_CONTRACT", "ResearchDelivery", "build_delivery", "evidence_tokens",
    "failed_delivery", "insufficiency_summary", "needs_insufficiency",
]
