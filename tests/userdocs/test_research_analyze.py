"""Analyze mode over a real (hand-built) index with a scripted research model.

The golden case, in the user's words: "propose a solution for client ABC's
land dispute under the 2020 land law". The ABC folder holds a transfer
contract (2023) and a neighbour's complaint; the Law folder holds the Land
Law 45/2013/QH13, a consolidated text of it (VBHN) and the Land Law
31/2024/QH15 (effective 1 August 2024). Vietnam has no 2020 Land Law, so:

- the job must stop and ask which edition to use, listing the three;
- answered "31/2024", it must cite only that edition, find the exception
  ("trừ trường hợp…"), follow a cross-reference to an article no search hits,
  and reject misquotes (a swapped authority, a dropped "không");
- a newer edition already in force at the case date must also be asked about;
- a financial question uses the same machinery with no edition logic;
- a job stopped by the time limit resumes without re-reading the case;
- two profiles never see each other's documents.

The index is written directly (the real chunker and legal overlay, no sync
engine) and searched lexically; the model is a fake that answers each
function call from the prompt it is shown, so every token it cites is one
the job really printed.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from app.constants import ChatCompletionTypeEnum as CT
from app.userdocs.chunking import CHUNKER_VERSION, chunk_blocks, detect_legal_meta, diff_chunks, looks_legal
from app.userdocs.chunking import make_file_card
from app.userdocs.discovery.walker import path_hash
from app.userdocs.index import IndexDB
from app.userdocs.query.engine import QueryEngine
from app.userdocs.research import analyze as analyze_module
from app.userdocs.research import legal as L
from app.userdocs.research.analyze import run_analyze
from app.userdocs.research.context import NeedsInput, ProgressSink, ResearchContext, ResearchLLM, ResearchSpec, TimeUp
from app.userdocs.research.types import (
    COMPLETE,
    IN_FORCE,
    NEEDS_CLARIFICATION,
    NEEDS_CONFIRMATION,
    PARTIAL,
    READ_FULL,
    ROLE_PRIMARY,
    ROLE_REFERENCE,
    SUPERSEDED,
    Dossier,
    dossier_from_dict,
)
from app.userdocs.textnorm import fold
from app.userdocs.types import Block

UTC = _dt.timezone.utc
T0 = _dt.datetime(2026, 9, 20, 12, tzinfo=UTC).timestamp()

# ── the documents ──────────────────────────────────────────────────────────

CONTRACT = [
    "HỢP ĐỒNG CHUYỂN NHƯỢNG QUYỀN SỬ DỤNG ĐẤT",
    "Bên chuyển nhượng: ông Nguyễn Văn An.",
    "Bên nhận chuyển nhượng: Công ty TNHH ABC.",
    "Ngày 15 tháng 3 năm 2023, hai bên ký hợp đồng chuyển nhượng thửa đất số 12, tờ bản đồ số 5, xã Tân Phú.",
    "Giá chuyển nhượng là 2.000.000.000 đồng, đã thanh toán đủ.",
]
COMPLAINT = [
    "ĐƠN KHIẾU NẠI",
    "Ông Nguyễn Văn Bình, chủ thửa đất số 13 liền kề, cho rằng Công ty TNHH ABC đã lấn chiếm 2 mét đất dọc "
    "ranh giới thửa đất số 12.",
    "Hòa giải tại Ủy ban nhân dân xã Tân Phú ngày 10 tháng 6 năm 2023 không thành.",
    "Công ty ABC có Giấy chứng nhận quyền sử dụng đất đối với thửa đất số 12.",
]
XYZ_CONTRACT = [
    "HỢP ĐỒNG CHUYỂN NHƯỢNG QUYỀN SỬ DỤNG ĐẤT",
    "Bên nhận chuyển nhượng: Công ty XYZ.",
    "Ngày 15 tháng 9 năm 2024, hai bên ký hợp đồng chuyển nhượng thửa đất số 40 tại xã Bình An.",
    "Ông Lê Văn Chung khiếu nại về ranh giới thửa đất số 40.",
]

_GENERAL_ARTICLES = [
    "Điều 1. Phạm vi điều chỉnh",
    "Luật này quy định về chế độ sở hữu đất đai, quyền hạn và trách nhiệm của Nhà nước đại diện chủ sở hữu "
    "toàn dân về đất đai.",
    "Điều 2. Đối tượng áp dụng",
    "1. Cơ quan nhà nước thực hiện quyền hạn và trách nhiệm đại diện chủ sở hữu toàn dân về đất đai.",
    "2. Người sử dụng đất.",
    "Điều 3. Giải thích từ ngữ",
    "Tranh chấp đất đai là tranh chấp về quyền, nghĩa vụ của người sử dụng đất giữa hai hoặc nhiều bên trong "
    "quan hệ đất đai.",
]

LAW_2013 = [
    "QUỐC HỘI", "Luật số: 45/2013/QH13", "LUẬT", "ĐẤT ĐAI", "Chương I", "QUY ĐỊNH CHUNG",
    *_GENERAL_ARTICLES,
    "Chương XIII", "GIẢI QUYẾT TRANH CHẤP VỀ ĐẤT ĐAI",
    "Điều 202. Hòa giải tranh chấp đất đai",
    "1. Nhà nước khuyến khích các bên tranh chấp đất đai tự hòa giải hoặc giải quyết tranh chấp đất đai thông "
    "qua hòa giải ở cơ sở.",
    "2. Tranh chấp đất đai mà các bên tranh chấp không hòa giải được thì gửi đơn đến Ủy ban nhân dân cấp xã "
    "nơi có đất tranh chấp để hòa giải.",
    "Điều 203. Thẩm quyền giải quyết tranh chấp đất đai",
    "Tranh chấp đất đai đã được hòa giải theo quy định tại Điều 202 của Luật này mà không thành thì được giải "
    "quyết như sau:",
    "1. Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do Tòa án nhân dân giải quyết.",
    "2. Tranh chấp đất đai mà đương sự không có Giấy chứng nhận thì đương sự chỉ được lựa chọn một trong hai "
    "hình thức, trừ trường hợp quy định tại khoản 1 Điều này.",
    "Điều 212. Hiệu lực thi hành",
    "Luật này có hiệu lực thi hành từ ngày 01 tháng 7 năm 2014.",
    "Luật này đã được Quốc hội thông qua ngày 29 tháng 11 năm 2013.",
]

VBHN_2018 = [
    "VĂN PHÒNG QUỐC HỘI", "Số: 21/VBHN-VPQH", "Hà Nội, ngày 10 tháng 12 năm 2018", "VĂN BẢN HỢP NHẤT",
    "LUẬT", "ĐẤT ĐAI", "Chương I", "QUY ĐỊNH CHUNG",
    *_GENERAL_ARTICLES,
    "Chương XIII", "GIẢI QUYẾT TRANH CHẤP VỀ ĐẤT ĐAI",
    "Điều 202. Hòa giải tranh chấp đất đai",
    "1. Nhà nước khuyến khích các bên tranh chấp đất đai tự hòa giải hoặc giải quyết tranh chấp đất đai thông "
    "qua hòa giải ở cơ sở.",
    "Điều 203. Thẩm quyền giải quyết tranh chấp đất đai",
    "1. Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do Tòa án nhân dân giải quyết.",
    "Điều 212. Hiệu lực thi hành",
    "Luật này có hiệu lực thi hành từ ngày 01 tháng 7 năm 2014.",
]

LAW_2024 = [
    "QUỐC HỘI", "Luật số: 31/2024/QH15", "LUẬT", "ĐẤT ĐAI", "Chương I", "QUY ĐỊNH CHUNG",
    *_GENERAL_ARTICLES,
    # Reached only through the cross-reference in Điều 236: no search term of
    # the issues occurs in it.
    "Điều 101. Bảo đảm kinh phí",
    "Kinh phí bảo đảm cho hoạt động này do ngân sách nhà nước bố trí.",
    "Chương XV", "GIẢI QUYẾT TRANH CHẤP ĐẤT ĐAI",
    "Điều 235. Hòa giải tranh chấp đất đai",
    "1. Nhà nước khuyến khích các bên tranh chấp đất đai tự hòa giải hoặc giải quyết tranh chấp đất đai thông "
    "qua hòa giải ở cơ sở.",
    "2. Trước khi đề nghị giải quyết tranh chấp, các bên phải thực hiện hòa giải tại Ủy ban nhân dân cấp xã "
    "nơi có đất tranh chấp.",
    # Another instrument, not among the documents: an unresolved reference.
    "3. Hồ sơ hòa giải thực hiện theo Điều 12 của Nghị định 43/2014/NĐ-CP.",
    "Điều 236. Thẩm quyền giải quyết tranh chấp đất đai",
    "1. Tranh chấp đất đai mà các bên tranh chấp có Giấy chứng nhận thì do Tòa án giải quyết.",
    "2. Tranh chấp đất đai đã được hòa giải theo quy định tại Điều 235 của Luật này mà không thành thì các bên "
    "có quyền lựa chọn nơi giải quyết; kinh phí thực hiện theo Điều 101 của Luật này.",
    "3. Trường hợp lựa chọn giải quyết tại Ủy ban nhân dân cấp tỉnh thì áp dụng thủ tục hành chính, trừ trường "
    "hợp quy định tại khoản 1 Điều này.",
    "4. Trong thời gian tranh chấp, người sử dụng đất không được chuyển nhượng quyền sử dụng đất đang tranh chấp.",
    "Điều 252. Hiệu lực thi hành",
    "1. Luật này có hiệu lực thi hành từ ngày 01 tháng 8 năm 2024.",
    "2. Luật Đất đai số 45/2013/QH13 hết hiệu lực kể từ ngày Luật này có hiệu lực thi hành.",
    "Luật này đã được Quốc hội thông qua ngày 18 tháng 01 năm 2024.",
]

Q3_REPORT = [
    "Q3 2025 travel report",
    "Travel spending in Q3 2025 was 120 million VND.",
    "The sales team booked business class flights for three domestic trips to Da Nang.",
]
EXPENSE_POLICY = [
    "Expense policy",
    "Employees must book economy class for domestic flights.",
    "Business class is allowed only for international flights longer than six hours, unless the CEO approves "
    "in writing.",
]


class Index:
    """A profile's index written directly: the real chunker (legal overlay
    included), file cards, folder rows."""

    def __init__(self, path: Path, uid: str):
        self.db = IndexDB.open(str(path), profile_uid=uid)
        self.folders: dict[str, int] = {}
        self.rows: dict[str, dict] = {}

    def folder(self, rel: str) -> int | None:
        if not rel:
            return None
        if rel in self.folders:
            return self.folders[rel]
        parent = self.folder(rel.rsplit("/", 1)[0] if "/" in rel else "")
        name = rel.rsplit("/", 1)[-1]
        fid = self.db.upsert_folder("local", rel, path_hash(rel), name=name, name_folded=fold(name),
                                    parent_id=parent, depth=rel.count("/") + 1, status="live")
        self.folders[rel] = fid
        return fid

    def add(self, rel: str, lines: list[str], *, legal_meta: bool = True, status: str = "indexed",
            status_reason: str | None = None) -> dict:
        blocks = [Block(text=x, locator={"line_start": i + 1, "line_end": i + 1}) for i, x in enumerate(lines)]
        folder_id = self.folder(rel.rsplit("/", 1)[0] if "/" in rel else "")
        name = rel.rsplit("/", 1)[-1]
        doc_meta = {}
        if legal_meta and looks_legal(blocks):
            doc_meta["legal"] = detect_legal_meta(blocks)
        text = "\n".join(lines)
        row = self.db.insert_file("local", rel, path_hash(rel), name=name, name_folded=fold(name),
                                  ext="." + name.rsplit(".", 1)[-1].lower(), kind="text", folder_id=folder_id,
                                  status="dirty", size=len(text), mtime=T0, mtime_ns=int(T0 * 1e9),
                                  sha256=hashlib.sha256(text.encode()).hexdigest(),
                                  chunker_version=CHUNKER_VERSION, doc_meta=doc_meta or None)
        card = make_file_card(name=name, rel_path=rel, kind="text", size=len(text), mtime_iso="2026-09-20",
                              summary_text=lines[0])
        body = chunk_blocks(blocks) if status == "indexed" else []
        self.db.apply_chunks(file_id=row["id"], folder_id=folder_id, source="local",
                             diff=diff_chunks([], [card] + body),
                             file_fields={"status": status, "status_reason": status_reason})
        self.rows[rel] = self.db.get_file(row["id"])
        return self.rows[rel]

    def fid(self, rel: str) -> str:
        return self.rows[rel]["cite_id"]


LAW13 = "Law/LuatDatDai-45-2013-QH13.txt"
VBHN = "Law/VBHN-LuatDatDai-2018.txt"
LAW24 = "Law/LuatDatDai-31-2024-QH15.txt"


@pytest.fixture
def alice(tmp_path):
    ix = Index(tmp_path / "alice.db", "uid-alice")
    ix.add("Clients/ABC/HopDong.txt", CONTRACT)
    ix.add("Clients/ABC/DonKhieuNai.txt", COMPLAINT)
    ix.add("Clients/XYZ/HopDong.txt", XYZ_CONTRACT)
    ix.add(LAW13, LAW_2013)
    ix.add(VBHN, VBHN_2018)
    # No doc_meta: the job must read the number and dates from the text.
    ix.add(LAW24, LAW_2024, legal_meta=False)
    for i, line in enumerate(Q3_REPORT):
        ix.add(f"Finance/Q3/part{i}.txt", [line, Q3_REPORT[0]])
    ix.add("Policies/expense-policy.txt", EXPENSE_POLICY)
    yield ix
    ix.db.close()


class Spy:
    """Records every search the job runs."""

    def __init__(self, engine: QueryEngine):
        self.queries: list[str] = []
        real = engine.search

        def search(query, **kw):
            self.queries.append(query)
            return real(query, **kw)

        engine.search = search  # type: ignore[method-assign]


def _engine(ix: Index, profile: str = "alice") -> QueryEngine:
    return QueryEngine(profile, ix.db, tz=UTC, snapshot={}, vector_handles=lambda: None)


# ── the scripted model ─────────────────────────────────────────────────────

_PASSAGE_RE = re.compile(r"(\[ud:[0-9a-z]{8}#[0-9a-f]{8}\])[^\n]*\n(.*?)(?=\n\[ud:|\n###|\n<<<|\Z)", re.S)


def passages(user: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _PASSAGE_RE.finditer(user)}


def tok(user: str, phrase: str) -> str | None:
    return next((t for t, text in passages(user).items() if phrase in text), None)


ISSUES = [
    {"title": "Thẩm quyền giải quyết tranh chấp đất đai",
     "description": "Cơ quan nào có thẩm quyền giải quyết tranh chấp ranh giới giữa Công ty ABC và ông Bình.",
     "terms_vi": ["thẩm quyền", "Tòa án", "giải quyết tranh chấp"],
     "terms_en": ["competent authority", "land dispute"]},
    {"title": "Hòa giải tranh chấp đất đai",
     "description": "Thủ tục hòa giải bắt buộc trước khi khởi kiện.",
     "terms_vi": ["hòa giải", "Ủy ban nhân dân cấp xã"], "terms_en": ["mediation"]},
]


def record_question(_system: str, user: str) -> dict:
    m = re.search(r"Luật Đất đai (\d{4})", user)
    return {"instruments": [{"name": m.group(0) if m else "Luật Đất đai", "name_vi": "Luật Đất đai",
                             "name_en": "Land Law",
                             # A model that "knows" the number: must never be taken from it.
                             "number": "45/2013/QH13", "year": m.group(1) if m else ""}],
            "issues": ISSUES}


def record_case(_system: str, user: str) -> dict:
    facts, dates = [], []
    for token, text in passages(user).items():
        if "hai bên ký hợp đồng chuyển nhượng" in text:
            facts.append({"text": "Hai bên ký hợp đồng chuyển nhượng thửa đất.",
                          "evidence": [{"token": token, "quote": "hai bên ký hợp đồng chuyển nhượng thửa đất số"}]})
            # A misquoted amount: the digits differ, so it is rejected.
            facts.append({"text": "Giá chuyển nhượng 3 tỷ đồng.",
                          "evidence": [{"token": token, "quote": "Giá chuyển nhượng là 3.000.000.000 đồng"}]})
        if "lấn chiếm" in text:
            quote = "đã lấn chiếm 2 mét đất dọc ranh giới thửa đất số 12"
            facts.append({"text": "Ông Bình cho rằng Công ty ABC lấn chiếm 2 mét đất.",
                          "evidence": [{"token": token, "quote": quote}]})
        for m in re.finditer(r"ngày (\d{1,2}) tháng (\d{1,2}) năm (\d{4})", text, re.IGNORECASE):
            d, mo, y = (int(x) for x in m.groups())
            dates.append({"what": "sự kiện", "date": f"{y:04d}-{mo:02d}-{d:02d}"})
    shown = " ".join(passages(user).values())
    parties = [p for p in ("Nguyễn Văn An", "Công ty TNHH ABC", "Nguyễn Văn Bình", "Trần Thị Cúc") if p in shown]
    return {"facts": facts, "parties": parties, "dates": dates,
            "instruments": [{"name": "Luật Đất đai", "number": "", "year": ""}], "issues": ISSUES}


def record_findings(_system: str, user: str) -> dict:
    out = []
    P = passages(user)
    first = next(iter(P.items()), None)
    if first:
        line = next((ln for ln in first[1].splitlines() if len(ln.strip()) >= 20), "")
        if line:
            out.append({"stance": "definition", "provision": "?", "text": f"Nội dung: {line[:40]}",
                        "evidence": [{"token": first[0], "quote": line.strip()[:60]}]})
    t = tok(user, "thì do Tòa án giải quyết")
    if t:
        out.append({"stance": "supports", "provision": "Điều 208", "text": "Có Giấy chứng nhận thì Tòa án giải quyết.",
                    "evidence": [{"token": t, "quote": "Tranh chấp đất đai mà các bên tranh chấp có Giấy chứng nhận "
                                                       "thì do Tòa án giải quyết"}]})
        out.append({"stance": "contradicts", "provision": "Điều 236", "text": "UBND giải quyết.",
                    "evidence": [{"token": t, "quote": "Tranh chấp đất đai mà các bên tranh chấp có Giấy chứng nhận "
                                                       "thì do UBND giải quyết"}]})
    t = tok(user, "trừ trường hợp quy định tại khoản 1 Điều này")
    if t:
        out.append({"stance": "exception", "provision": "Điều 236 khoản 3", "text": "Ngoại lệ của khoản 3.",
                    "evidence": [{"token": t, "quote": "trừ trường hợp quy định tại khoản 1 Điều này"}]})
    t = tok(user, "không được chuyển nhượng")
    if t:
        # "không" dropped: reverses the provision, so it is rejected.
        out.append({"stance": "condition", "provision": "Điều 236 khoản 4", "text": "Được chuyển nhượng.",
                    "evidence": [{"token": t, "quote": "người sử dụng đất được chuyển nhượng quyền sử dụng đất "
                                                       "đang tranh chấp"}]})
    t = tok(user, "Kinh phí bảo đảm")
    if t:
        out.append({"stance": "condition", "provision": "Điều 101", "text": "Kinh phí do ngân sách bố trí.",
                    "evidence": [{"token": t, "quote": "Kinh phí bảo đảm cho hoạt động này do ngân sách nhà nước"}]})
    t = tok(user, "các bên phải thực hiện hòa giải")
    if t:
        out.append({"stance": "procedure", "provision": "Điều 235", "text": "Phải hòa giải tại UBND cấp xã trước.",
                    "evidence": [{"token": t, "quote": "các bên phải thực hiện hòa giải tại Ủy ban nhân dân cấp xã"}]})
    t = tok(user, "Employees must book economy class")
    if t:
        out.append({"stance": "contradicts", "provision": "Expense policy", "text": "Domestic flights must be economy.",
                    "evidence": [{"token": t, "quote": "Employees must book economy class for domestic flights"}]})
    t = tok(user, "unless the CEO approves")
    if t:
        out.append({"stance": "exception", "provision": "Expense policy", "text": "The CEO may approve.",
                    "evidence": [{"token": t, "quote": "unless the CEO approves in writing"}]})
    # A token the job never showed: rejected whatever the quote.
    out.append({"stance": "supports", "provision": "?", "text": "Invented.",
                "evidence": [{"token": "[ud:zzzzzzzz#00000000]", "quote": "anything at all goes here"}]})
    first_round = "Findings so far" not in user
    return {"findings": out, "open_questions": ["thời hiệu khởi kiện tranh chấp đất đai"] if first_round else []}


def record_case_finance(_system: str, user: str) -> dict:
    facts = []
    for token, text in passages(user).items():
        if "business class" in text:
            quote = "booked business class flights for three domestic trips"
            facts.append({"text": "Business class was booked for domestic trips.",
                          "evidence": [{"token": token, "quote": quote}]})
    return {"facts": facts, "parties": ["sales team"], "dates": [], "instruments": [{"name": "Expense policy",
                                                                                      "number": "", "year": ""}],
            "issues": [{"title": "Class of travel on domestic flights",
                        "description": "Whether business class on domestic flights complies with the policy.",
                        "terms_vi": [], "terms_en": ["economy class", "domestic flights", "business class"]}]}


class FakeLLM:
    """Answers each function call from its prompt; logs every call."""

    provider_name, model_name = "fake", "fake-research"

    def __init__(self, handlers: dict[str, Callable[[str, str], Any]] | None = None,
                 on_call: Callable[[str, str], None] | None = None):
        self.handlers = {"record_question": record_question, "record_case": record_case,
                         "record_findings": record_findings,
                         "query_variants": lambda s, u: {"restored": "", "translation": "hạng phổ thông nội địa"},
                         **(handlers or {})}
        self.on_call = on_call
        self.log: list[tuple[str, str]] = []
        self.inflight = 0
        self.max_inflight = 0

    async def chat_completion(self, *, messages, tools, **kw):
        name = tools[0]["function"]["name"]
        system, user = messages[0]["content"], messages[1]["content"]
        self.log.append((name, user))
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            await asyncio.sleep(0.005)
            args = self.handlers[name](system, user)
        finally:
            self.inflight -= 1
        if args is not None:
            yield {"type": CT.FUNCTION_CALLING,
                   "data": {"function": [{"name": name, "arguments": json.dumps(args, ensure_ascii=False)}]}}
        if self.on_call is not None:
            self.on_call(name, user)
        yield {"type": CT.DONE, "input_tokens": len(system + user) // 4, "output_tokens": 150}

    def names(self) -> list[str]:
        return [n for n, _ in self.log]


def make_ctx(engine, fake, *, question: str, domain: str = "legal", scope=None, reference_scope=None,
             answers=None, budget: int = 250_000, state=None, dossier=None, job_id: str = "job1",
             profile: str = "alice") -> ResearchContext:
    spec = ResearchSpec(question=question, mode="analyze", domain=domain, scope=scope, reference_scope=reference_scope)
    d = dossier or Dossier(job_id=job_id, mode="analyze", domain=domain, question=question, status="running")
    return ResearchContext(profile=profile, job_id=job_id, spec=spec, engine=engine,
                           llm=ResearchLLM(fake, budget=budget), dossier=d,
                           state=state if state is not None else {}, answers=answers or {},
                           progress_sink=ProgressSink())


def resume(ctx: ResearchContext, fake, *, answers=None) -> ResearchContext:
    """What the job runner does on continue_job / after a restart: the saved
    checkpoint (state and dossier, through JSON) and the merged answers."""
    state = json.loads(json.dumps(ctx.state))
    dossier = dossier_from_dict(json.loads(json.dumps(ctx.dossier.to_dict())))
    return make_ctx(ctx.engine, fake, question=ctx.spec.question, domain=ctx.spec.domain, scope=ctx.spec.scope,
                    reference_scope=ctx.spec.reference_scope, answers={**ctx.answers, **(answers or {})},
                    state=state, dossier=dossier, job_id=ctx.job_id, profile=ctx.profile)


Q_2020 = "Đề xuất giải pháp cho tranh chấp đất đai của khách hàng ABC theo Luật Đất đai 2020."
ABC = {"folder": ["ABC"]}
LAW = {"folder": ["Law"]}


def _cite(token: str) -> str:
    return token.split(":", 1)[1].split("#", 1)[0]


# ── (a) + (b): the golden case ─────────────────────────────────────────────


def test_a_2020_land_law_stops_to_ask_which_edition(alice):
    fake = FakeLLM()
    ctx = make_ctx(_engine(alice), fake, question=Q_2020, scope=ABC, reference_scope=LAW)
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    exc = ei.value
    assert exc.status == NEEDS_CLARIFICATION
    clar = exc.clarification
    assert clar.kind == "edition" and "edition" in clar.answer_keys
    assert "2020" in clar.question
    by_number = {c["number"]: c for c in clar.candidates}
    assert set(by_number) == {"45/2013/QH13", "21/VBHN-VPQH", "31/2024/QH15"}
    assert by_number["45/2013/QH13"]["effective"] == "2014-07-01"
    assert by_number["31/2024/QH15"]["effective"] == "2024-08-01"
    assert by_number["31/2024/QH15"]["fid"] == alice.fid(LAW24)
    assert by_number["21/VBHN-VPQH"]["consolidated"] is True
    # Status is judged against the corpus: the 2024 law replaces the 2013 one.
    assert by_number["45/2013/QH13"]["status"] == SUPERSEDED
    assert by_number["45/2013/QH13"]["replaced_by"] == alice.fid(LAW24)
    assert by_number["31/2024/QH15"]["status"] == IN_FORCE
    # The case was read in full first; nothing was concluded.
    assert fake.names().count("record_case") == 2 and "record_findings" not in fake.names()
    assert ctx.state["analyze"]["primary"]["done"] is True
    assert ctx.dossier.facts and not any(i.findings for i in ctx.dossier.issues)
    assert any(n.startswith("Not legal advice") for n in ctx.dossier.notes)


def test_b_answered_edition_gives_verified_findings_from_that_edition_only(alice):
    fake = FakeLLM()
    engine = _engine(alice)
    spy = Spy(engine)
    ctx = make_ctx(engine, fake, question=Q_2020, scope=ABC, reference_scope=LAW)
    with pytest.raises(NeedsInput):
        asyncio.run(run_analyze(ctx))
    read_before = fake.names().count("record_case")

    ctx2 = resume(ctx, fake, answers={"edition": alice.fid(LAW24)})
    d = asyncio.run(run_analyze(ctx2))
    assert d.status == COMPLETE
    # The case was not read again.
    assert fake.names().count("record_case") == read_before

    # Facts: verified against the case files; the misquoted amount is gone.
    abc = {alice.fid("Clients/ABC/HopDong.txt"), alice.fid("Clients/ABC/DonKhieuNai.txt")}
    assert d.facts and all(_cite(e.token) in abc for f in d.facts for e in f.evidence)
    assert not any("3.000.000.000" in e.quote for f in d.facts for e in f.evidence)

    # Findings cite only the chosen edition, with the source's wording.
    fid24 = alice.fid(LAW24)
    findings = [f for i in d.issues for f in i.findings]
    assert findings
    assert all(_cite(e.token) == fid24 for f in findings for e in f.evidence)
    court = [f for f in findings if f.stance == "supports" and "Tòa án" in f.evidence[0].quote]
    assert court and court[0].evidence[0].quote_status == "exact"
    # The provision is named from the evidence, not from the model's "Điều 208".
    assert court[0].provision.startswith("Điều 236") and "31/2024/QH15" in court[0].provision
    # Counter-evidence was searched for and found.
    assert any("trừ trường hợp" in q for q in spy.queries)
    assert any("thời hiệu" in q for q in spy.queries)
    issue1 = d.issues[0]
    assert any(f.stance == "exception" and "trừ trường hợp" in f.evidence[0].quote for f in issue1.findings)
    # Misquotes rejected and counted: "UBND" for "Tòa án", a dropped "không",
    # an invented token, the misquoted amount.
    assert not any("UBND" in e.quote for f in findings for e in f.evidence)
    assert not any(f.text == "Được chuyển nhượng." for f in findings)
    assert d.rejected_quotes >= 4
    # The cross-reference to Điều 101 was followed (no search reaches it);
    # one to a decree that is not among the documents is a gap.
    assert any(x.startswith("Điều 101") for i in d.issues for x in i.xrefs)
    assert any(f.provision.startswith("Điều 101") for f in findings)
    assert any("Điều 12 của Nghị định 43/2014/NĐ-CP" in g and "not among the documents" in g for g in d.gaps)

    # Authorities: the edition used and why; the others marked, not used.
    auth = {a.number: a for a in d.authorities}
    assert auth["31/2024/QH15"].used and auth["31/2024/QH15"].why == "chosen by the user"
    assert not auth["45/2013/QH13"].used and auth["45/2013/QH13"].status == SUPERSEDED
    assert not auth["21/VBHN-VPQH"].used
    notes = " ".join(d.version_notes)
    assert "judged only against" in notes and "31/2024/QH15" in notes
    # The case date (2023) is before the chosen law took effect: said so.
    assert "after the case date 2023-06-10" in notes

    # Coverage: the case read in full; the law searched, provisions read.
    cov = {c.rel_path: c for c in d.coverage}
    assert cov["Clients/ABC/HopDong.txt"].read == READ_FULL and cov["Clients/ABC/HopDong.txt"].role == ROLE_PRIMARY
    assert cov[LAW24].role == ROLE_REFERENCE and cov[LAW24].chunks_read > 0
    assert cov[LAW13].chunks_read == 0 and cov[LAW13].reason == "not_relevant"
    assert any(n.startswith("Not legal advice") for n in d.notes)
    json.dumps(ctx2.state)  # the checkpoint stays JSON


# ── (c): a newer edition in force at the case date ─────────────────────────


def test_c_newer_edition_in_force_at_the_case_date_is_asked_about(alice):
    fake = FakeLLM()
    ctx = make_ctx(_engine(alice), fake, question="Giải quyết tranh chấp của Công ty XYZ theo Luật Đất đai 2013.",
                   scope={"folder": ["XYZ"]}, reference_scope=LAW)
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    clar = ei.value.clarification
    assert clar.kind == "edition"
    assert "2024-08-01" in clar.question and "2024-09-15" in clar.question
    assert {c["number"] for c in clar.candidates} >= {"45/2013/QH13", "31/2024/QH15"}

    # Answered with the edition the question named, the job goes on with it.
    ctx2 = resume(ctx, fake, answers={"edition": alice.fid(LAW13)})
    d = asyncio.run(run_analyze(ctx2))
    assert d.status == COMPLETE
    used = [a.number for a in d.authorities if a.used]
    assert used == ["45/2013/QH13"]
    tokens = [e.token for i in d.issues for f in i.findings for e in f.evidence]
    assert tokens and all(_cite(x) == alice.fid(LAW13) for x in tokens)
    # Điều 203 refers to Điều 202 of the same law: followed.
    assert any(x.startswith("Điều 202") for i in d.issues for x in i.xrefs) or any(
        f.provision.startswith("Điều 202") for i in d.issues for f in i.findings)


# ── (d): a financial question: no edition logic ────────────────────────────


def test_d_financial_question_uses_the_same_machinery_without_editions(alice):
    fake = FakeLLM({"record_case": record_case_finance})
    ctx = make_ctx(_engine(alice), fake, domain="financial",
                   question="Was the Q3 travel spending compliant with the expense policy?",
                   scope={"folder": ["Q3"]}, reference_scope={"folder": ["Policies"]})
    d = asyncio.run(run_analyze(ctx))
    assert d.status == COMPLETE
    assert d.authorities == [] and d.version_notes == []
    assert "record_legal_meta" not in fake.names()
    assert not any(n.startswith("Not legal advice") for n in d.notes)
    # Every primary window was read, never more than four at a time.
    assert fake.names().count("record_case") == 3
    assert 1 < fake.max_inflight <= 4
    policy = alice.fid("Policies/expense-policy.txt")
    findings = [f for i in d.issues for f in i.findings]
    assert {f.stance for f in findings} >= {"contradicts", "exception"}
    assert all(_cite(e.token) == policy for f in findings for e in f.evidence)
    assert d.facts and "business class" in d.facts[0].evidence[0].quote
    # The findings prompt was in English (the question's language).
    findings_prompt = next(u for n, u in fake.log if n == "record_findings")
    assert "Research question: Was the Q3" in findings_prompt
    # No Vietnamese search terms: the issue query was also translated, and
    # that call counts against the job's budget like any other.
    assert fake.names().count("query_variants") == 1
    calls = len(fake.log)
    assert ctx.llm.spent.tokens_out == 150 * calls


# ── (e): resume after the time limit ───────────────────────────────────────


def test_e_resume_after_time_up_mid_issue_does_not_reread_the_case(alice, monkeypatch):
    # Few provisions per round, so the first issue needs a second round.
    monkeypatch.setattr(analyze_module, "MAX_UNITS_PER_ROUND", 3)
    holder: dict[str, ResearchContext] = {}

    def stop_after_first_findings(name: str, _user: str) -> None:
        if name == "record_findings" and "ctx" in holder:
            holder["ctx"].deadline = time.monotonic() - 1  # the time limit hits after this call

    fake = FakeLLM(on_call=stop_after_first_findings)
    ctx = make_ctx(_engine(alice), fake, question=Q_2020, scope=ABC, reference_scope=LAW,
                   answers={"edition": alice.fid(LAW24)})
    holder["ctx"] = ctx
    with pytest.raises(TimeUp):
        asyncio.run(run_analyze(ctx))
    assert fake.names().count("record_findings") == 1
    issues = ctx.state["analyze"]["issues"]
    assert issues[0]["round"] == 1 and not issues[0]["done"]
    first = [f for f in ctx.dossier.issues[0].findings]
    assert first

    fake2 = FakeLLM()
    ctx2 = resume(ctx, fake2)
    d = asyncio.run(run_analyze(ctx2))
    assert d.status == COMPLETE
    # Nothing before the issues ran again: no question, case or law reading.
    assert set(fake2.names()) == {"record_findings"}
    # It continued the first issue where it stopped (its second round).
    resumed = fake2.log[0][1]
    assert f"Issue: {ISSUES[0]['title']}" in resumed and "Findings so far" in resumed
    # The first round's findings are kept once, and both issues are done.
    texts = [f.text for f in d.issues[0].findings]
    assert len(texts) == len(set(texts))
    assert all(f.text in texts for f in first)
    assert d.issues[1].findings


# ── (f): two profiles ──────────────────────────────────────────────────────


def test_f_two_profiles_never_see_each_other(alice, tmp_path):
    bob = Index(tmp_path / "bob.db", "uid-bob")
    try:
        bob.add("Clients/ABC/ThueNha.txt", [
            "HỢP ĐỒNG THUÊ NHÀ",
            "Bên thuê: bà Trần Thị Cúc.",
            "Ngày 1 tháng 2 năm 2022, bà Cúc thuê nhà tại số 7 phố Huế.",
        ])
        fa, fb = FakeLLM(), FakeLLM()
        qa = "Tranh chấp đất đai của ABC theo luật đất đai?"
        ca = make_ctx(_engine(alice), fa, question=qa, domain="general", scope=ABC, reference_scope=LAW,
                      profile="alice", job_id="ja")
        cb = make_ctx(_engine(bob, "bob"), fb, question=qa, domain="general", scope=ABC, profile="bob", job_id="jb")

        async def both():
            return await asyncio.gather(run_analyze(ca), run_analyze(cb))

        da, db_ = asyncio.run(both())
        assert da.status == db_.status == COMPLETE
        bob_ids = {r["cite_id"] for r in bob.db.list_files(source="local", limit=100)}
        alice_ids = {r["cite_id"] for r in alice.db.list_files(source="local", limit=100)}
        tokens_b = [e.token for f in db_.facts for e in f.evidence] + \
            [e.token for i in db_.issues for f in i.findings for e in f.evidence]
        assert all(_cite(x) in bob_ids for x in tokens_b)
        assert {c.fid for c in db_.coverage} <= bob_ids
        # Nothing of alice's reached bob's prompts, and the reverse.
        assert not any("Nguyễn Văn An" in u or "Tòa án" in u for _n, u in fb.log)
        assert not any("Trần Thị Cúc" in u for _n, u in fa.log)
        assert all(_cite(x) in alice_ids for i in da.issues for f in i.findings for x in [e.token for e in f.evidence])
        # Each profile's cache lives in its own index.
        keys_a = {r["key"] for r in alice.db.read_sql("SELECT key FROM llm_cache")}
        keys_b = {r["key"] for r in bob.db.read_sql("SELECT key FROM llm_cache")}
        assert keys_a and keys_b and not keys_a & keys_b
    finally:
        bob.db.close()


# ── coverage first: scope, unread files, budget ────────────────────────────


def test_ambiguous_scope_folder_is_asked(alice):
    alice.add("Archive/ABC/cu.txt", ["Hồ sơ cũ của khách hàng ABC năm 2019."])
    ctx = make_ctx(_engine(alice), FakeLLM(), question=Q_2020, scope=ABC, reference_scope=LAW)
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    assert ei.value.clarification.kind == "scope"
    assert {c["rel_path"] for c in ei.value.clarification.candidates} == {"Archive/ABC", "Clients/ABC"}
    # The answer applies to the case scope only — the reference folder stays "Law".
    fake = FakeLLM()
    ctx2 = resume(ctx, fake, answers={"scope_folder": "Clients/ABC", "edition": alice.fid(LAW24)})
    d = asyncio.run(run_analyze(ctx2))
    assert d.status == COMPLETE
    assert {c.rel_path for c in d.coverage if c.role == ROLE_PRIMARY} == {
        "Clients/ABC/HopDong.txt", "Clients/ABC/DonKhieuNai.txt"}


def test_unreadable_files_need_confirmation_then_become_gaps(alice):
    alice.add("Clients/DEF/HopDong.txt", CONTRACT)
    alice.add("Clients/DEF/scan.pdf", ["x"], status="metadata_only", status_reason="encrypted")
    ctx = make_ctx(_engine(alice), FakeLLM({"record_findings": lambda s, u: {"findings": [], "open_questions": []}}),
                   question="Tranh chấp của DEF?", domain="general", scope={"folder": ["DEF"]})
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    assert ei.value.status == NEEDS_CONFIRMATION
    clar = ei.value.clarification
    assert clar.kind == "unread" and "confirm" in clar.answer_keys
    assert clar.candidates == [{"fid": alice.fid("Clients/DEF/scan.pdf"), "rel_path": "Clients/DEF/scan.pdf",
                                "role": ROLE_PRIMARY, "reason": "encrypted"}]
    d = asyncio.run(run_analyze(resume(ctx, ctx.llm._llm, answers={"confirm": "true"})))
    assert d.status == COMPLETE
    assert any("Clients/DEF/scan.pdf" in g and "encrypted" in g for g in d.gaps)
    cov = {c.rel_path: c for c in d.coverage}
    assert cov["Clients/DEF/scan.pdf"].reason == "encrypted" and cov["Clients/DEF/HopDong.txt"].read == READ_FULL


def test_an_estimate_over_the_budget_needs_confirmation(alice):
    fake = FakeLLM()
    ctx = make_ctx(_engine(alice), fake, question=Q_2020, scope=ABC, reference_scope=LAW, budget=20_000)
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    assert ei.value.status == NEEDS_CONFIRMATION and ei.value.clarification.kind == "budget"
    assert ei.value.clarification.candidates[0]["estimate"] > 20_000
    assert fake.log == []  # asked before spending anything


def test_near_the_budget_the_job_stops_starting_rounds_and_is_partial(alice):
    fake = FakeLLM()
    ctx = make_ctx(_engine(alice), fake, question=Q_2020, scope=ABC, reference_scope=LAW, budget=100_000,
                   answers={"edition": alice.fid(LAW24), "confirm_budget": "true"})
    ctx.llm.spent.tokens_in = 91_000  # past 90%: room to finish reading, none for new rounds
    d = asyncio.run(run_analyze(ctx))
    assert d.status == PARTIAL
    assert "record_findings" not in fake.names() and fake.names().count("record_case") == 2
    assert any("stopped at 90% of the token budget" in n for n in d.notes)
    assert any(g.startswith("Issue not fully researched") for g in d.gaps)
    assert d.facts and d.authorities  # what it had is kept


def test_without_a_reference_scope_the_named_law_is_searched_for(alice):
    ctx = make_ctx(_engine(alice), FakeLLM(), question=Q_2020, scope=ABC)
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    assert {c["number"] for c in ei.value.clarification.candidates} == {"45/2013/QH13", "21/VBHN-VPQH",
                                                                       "31/2024/QH15"}
    assert any(n.startswith("No reference scope was given") and "Luật Đất đai" in n for n in ctx.dossier.notes)
    # The policy and the case files are not authorities.
    assert {a.rel_path for a in ctx.dossier.authorities} == {LAW13, VBHN, LAW24}


# ── legal.py: identification, editions, cross-references ───────────────────


def _docs(alice) -> dict[str, L.LegalDoc]:
    out = {}
    for rel in (LAW13, VBHN, LAW24):
        row = alice.rows[rel]
        out[row["cite_id"]] = L.doc_from_index(row, alice.db.chunks_of_file(row["id"]))
    return out


def test_documents_are_identified_from_meta_or_their_text(alice):
    docs = {d.number: d for d in _docs(alice).values()}
    law13, vbhn, law24 = docs["45/2013/QH13"], docs["21/VBHN-VPQH"], docs["31/2024/QH15"]
    assert (law13.issued, law13.effective, law13.source) == ("2013-11-29", "2014-07-01", "index")
    assert vbhn.consolidated and vbhn.issued == "2018-12-10" and vbhn.effective == "2014-07-01"
    # No doc_meta: number, dates and repeals read from the stored chunks.
    assert (law24.issued, law24.effective, law24.source) == ("2024-01-18", "2024-08-01", "text")
    assert law24.repeals == ["45/2013/QH13"]
    assert law13.family == vbhn.family == law24.family == "luat dat dai"
    assert L.family_key("LUẬT SỬA ĐỔI, BỔ SUNG MỘT SỐ ĐIỀU CỦA LUẬT ĐẤT ĐAI") == ("luat dat dai", True)


def test_edition_rules(alice):
    docs = _docs(alice)
    by = {d.number: d.fid for d in docs.values()}
    today = "2026-09-25"

    def pick(question: str, case_day: str | None = None, answers=None):
        return L.select_editions(docs, L.named_from_text(question), case_day=case_day, today=today,
                                 answers=answers or {}, lang="en")

    # No year named: the edition in force on the case date — the consolidated
    # text published by then carries the amendments.
    sel = pick("land dispute under the Land Law", case_day="2023-06-10")
    assert sel.chosen["luat dat dai"] == [by["21/VBHN-VPQH"]] and sel.clarification is None
    assert pick("land dispute under Luật Đất đai", case_day="2025-01-10").chosen["luat dat dai"] == [by["31/2024/QH15"]]
    # A named year matches by file name/title first, then dates.
    assert pick("theo Luật Đất đai 2018").chosen["luat dat dai"] == [by["21/VBHN-VPQH"]]
    sel = pick("theo Luật Đất đai 2013")
    assert sel.chosen["luat dat dai"] == [by["45/2013/QH13"]] and "file name" in sel.why[by["45/2013/QH13"]]
    # A named number is taken as is.
    assert pick("Law No. 31/2024/QH15").chosen["luat dat dai"] == [by["31/2024/QH15"]]
    # No 2020 edition: ask. A number not in the corpus: ask.
    assert pick("theo Luật Đất đai 2020").clarification.kind == "edition"
    assert pick("theo Luật số 99/2020/QH14").clarification is not None
    # The answer decides, by fid or by number.
    assert pick("theo Luật Đất đai 2020", answers={"edition": "31/2024/QH15"}).chosen["luat dat dai"] == [
        by["31/2024/QH15"]]
    # A named edition superseded by one already in force at the case date: ask.
    sel = pick("theo Luật Đất đai 2013", case_day="2024-09-15")
    assert sel.clarification is not None and "2024-08-01" in sel.clarification.question
    # A model's "knowledge" of a number the question does not contain is ignored.
    named = L.named_from_plan("theo Luật Đất đai 2020", [{"name": "Luật Đất đai", "number": "45/2013/QH13",
                                                         "year": "2020"}])
    assert named[0].number is None and named[0].year == 2020


def test_cross_references_resolve_only_to_the_edition_used(alice):
    docs = _docs(alice)
    by = {d.number: d for d in docs.values()}
    chosen = {"luat dat dai": [by["31/2024/QH15"].fid]}
    cur = by["31/2024/QH15"]
    assert L.resolve_ref({"article": "235", "doc": "self"}, cur, docs, chosen) == (cur.fid, "")
    assert L.resolve_ref({"article": "12", "doc": "Luật Đất đai"}, cur, docs, chosen) == (cur.fid, "")
    fid, why = L.resolve_ref({"article": "203", "doc": "Luật Đất đai số 45/2013/QH13"}, cur, docs, chosen)
    assert fid is None and "not the edition" in why
    fid, why = L.resolve_ref({"article": "5", "doc": "Nghị định 43/2014/NĐ-CP"}, cur, docs, chosen)
    assert fid is None and "not among the documents" in why


def test_model_fallback_is_cached_and_trusted_only_where_the_text_agrees(alice):
    row = alice.add("Law/scan-luat.txt", [
        "LUẬT", "ĐẤT ĐAI", "Văn bản 45/2013/QH13 do Quốc hội ban hành.",
        "Điều 1. Phạm vi", "Nội dung một.", "Điều 2. Đối tượng", "Nội dung hai.", "Điều 3. Giải thích",
        "Nội dung ba.", "Điều 4. Nguyên tắc", "Nội dung bốn.", "Điều 5. Hiệu lực", "Bắt đầu áp dụng từ 1/7/2014.",
    ], legal_meta=False)
    chunks = alice.db.chunks_of_file(row["id"])
    doc = L.doc_from_index(row, chunks)
    assert L.needs_model(doc) and doc.number is None
    calls = []

    async def ask(**kw):
        calls.append(kw)
        return {"title": "Luật Đất đai", "number": "45/2013/QH13", "issued": "2013-11-29",
                "effective": "2014-07-01", "consolidated": True, "repeals": ["13/2003/QH11"]}

    store: dict[str, Any] = {}

    async def cache_get(k):
        return store.get(k)

    async def cache_put(k, v):
        store[k] = v

    filled = asyncio.run(L.model_meta(doc, row, chunks, ask=ask, cache_get=cache_get, cache_put=cache_put))
    # The number and 2014 appear in the text; 2013-11-29, "consolidated" and
    # the repealed number do not.
    assert doc.number == "45/2013/QH13" and doc.effective == "2014-07-01"
    assert doc.issued is None and not doc.consolidated and doc.repeals == []
    assert set(filled) == {"number", "effective"} and doc.source == "model"
    # Only the document's edges, delimited as untrusted content.
    assert "<<<" in calls[0]["user"] and "45/2013/QH13" in calls[0]["user"]
    doc2 = L.doc_from_index(row, chunks)
    asyncio.run(L.model_meta(doc2, row, chunks, ask=ask, cache_get=cache_get, cache_put=cache_put))
    assert len(calls) == 1 and doc2.number == "45/2013/QH13"


def test_named_instruments_in_a_question():
    assert [(n.names, n.year) for n in L.named_from_text("theo Luật Đất đai 2020")] == [(["Luật Đất đai"], 2020)]
    assert [(n.names, n.year) for n in L.named_from_text("under the 2020 land law")] == [(["land law"], 2020)]
    assert L.named_from_text("hợp đồng ký năm 2023") == []
    assert L.named_from_text("Luật sư tư vấn năm 2023") == []  # a lawyer, not a law
    assert L.named_from_text("Law No. 45/2013/QH13")[0].number == "45/2013/QH13"


def test_a_case_file_changed_since_indexing_is_not_read_from_its_old_text(alice, tmp_path):
    # The ABC contract was edited on disk after it was indexed; the complaint
    # is as indexed. Sync is paused, so the job cannot wait for the re-index:
    # it asks, rather than read the contract's old text.
    from types import SimpleNamespace

    from app.userdocs.runtime import ProfileRuntime

    root = tmp_path / "root"
    for rel, lines in (("Clients/ABC/HopDong.txt", CONTRACT + ["Phụ lục 2: điều khoản mới."]),
                       ("Clients/ABC/DonKhieuNai.txt", COMPLAINT)):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
    st = (root / "Clients/ABC/DonKhieuNai.txt").stat()
    alice.db.update_file(alice.rows["Clients/ABC/DonKhieuNai.txt"]["id"], size=st.st_size, mtime_ns=st.st_mtime_ns)
    rt = ProfileRuntime(SimpleNamespace(wake=lambda: None), "alice", "uid-alice")
    rt.db, rt.root, rt.active, rt.paused_user = alice.db, str(root), True, True
    engine = _engine(alice)
    engine.runtime = rt
    fake = FakeLLM()
    ctx = make_ctx(engine, fake, question="Tranh chấp của ABC?", domain="general", scope={"folder": ["ABC"]})
    with pytest.raises(NeedsInput) as ei:
        asyncio.run(run_analyze(ctx))
    assert ei.value.status == NEEDS_CONFIRMATION
    assert [(c["rel_path"], c["reason"]) for c in ei.value.clarification.candidates] == [
        ("Clients/ABC/HopDong.txt", "not_indexed_yet")]
    assert fake.names() == [], "nothing is read before the user answers"
    assert any("sync is paused" in n for n in ctx.dossier.notes), ctx.dossier.notes
