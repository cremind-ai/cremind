"""Two small stand-ins for the OpenClaw guides of the multi-agent incident.

A user's documents folder held two OpenClaw user guides; asked "what is the
multiple agent mode of OpenClaw?", the agent searched, got both (the English
one ranked first), read only the Vietnamese one and answered from it. These
fixtures keep what mattered to that, and nothing of the real PDFs:

- an English tutorial that describes running *multiple instances*, with
  ``ClawLite`` commands (pages 5, 9, 10 and 11);
- a Vietnamese guide that describes *independent agents within one Gateway*,
  with ``openclaw`` commands, whose headings sit under one title heading —
  ``1 THẾ GIỚI › PHẦN 9`` — while its table of contents prints the section
  as "Phần 9: Multi-Agent - Xây Dựng Đội AI" (so a section guessed from the
  contents is not a heading the index has).

The two describe the feature differently and their commands differ; an
answer must not merge them into one procedure.

:func:`build` writes both into an :class:`~app.documents.index.IndexDB` with
the real chunker, and :func:`engine` opens a query engine over it whose
vector list the caller chooses (so confidence is deterministic).
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from typing import Any

from app.documents.chunking import chunk_blocks, diff_chunks, make_file_card
from app.documents.discovery.walker import path_hash
from app.documents.index import IndexDB
from app.documents.query.engine import QueryEngine
from app.documents.textnorm import fold
from app.documents.types import ANCHOR_HARD, ANCHOR_NONE, Block

UTC = _dt.timezone.utc
T0 = _dt.datetime(2026, 9, 26, 9, tzinfo=UTC).timestamp()

EN_NAME = "OpenClaw-Tutorial-Beginner-to-Intermediate-English.pdf"
VI_NAME = "huong dan su dung - Openclaw - AIECOS.pdf"


def _page(text: str, page: int, *, anchor: int = ANCHOR_HARD) -> Block:
    # A hard anchor keeps each page its own passage, as in the incident's
    # index (short pages would otherwise merge into one chunk); the page right
    # after a heading stays with it (``ANCHOR_NONE``), as a PDF's would.
    return Block(text=text, anchor=anchor, locator={"page": page})


def _heading(text: str, level: int, page: int) -> Block:
    return Block(text=text, anchor=ANCHOR_HARD, level=level, role="heading", locator={"page": page})


# The passages the question is about are long, like a real PDF page: a search
# snippet shows only part of them.
EN_MULTI_AGENT = (
    "Step 11: Multi-Agent Management. You can run multiple OpenClaw instances, each with different "
    "configurations and use cases, and keep their settings apart. Each instance is an agent with its own "
    "model, its own skills and its own messaging platform, so a work agent never sees what a personal "
    "agent was told. Create Multiple Agents: run ClawLite create-agent work, then ClawLite config --agent "
    "work set ai.model claude-sonnet-4.6 to give the work agent its model; repeat for every agent you "
    "need. An instance started this way keeps running when you switch to another one, so several agents "
    "can answer at the same time on different platforms. Think of each instance as a separate assistant "
    "that happens to share the same installation."
)
EN_MULTI_AGENT_2 = (
    "Create a personal agent with ClawLite create-agent personal and give it its own model with ClawLite "
    "config --agent personal set ai.model gpt-5.3. Switch agent with ClawLite switch-agent work or "
    "ClawLite switch-agent personal, and list all agents with ClawLite list-agents. Use cases: a Work "
    "Agent connects company email and project management tools; a Personal Agent manages your schedule "
    "and family affairs; an Experiment Agent tests new features and new skills. Practice Task 11: "
    "Configure Multi-Agent — create at least 2 agents for different purposes, each with a different AI "
    "model, messaging platform, skill set and permission level."
)
VI_MULTI_AGENT = (
    "MULTI-AGENT - XÂY DỰNG ĐỘI AI. Multi-Agent là tính năng cho phép chạy nhiều Agent độc lập trong cùng "
    "một Gateway. Mỗi Agent có workspace riêng, bộ nhớ riêng, và có thể giao tiếp với nhau thông qua "
    "sessions. Khi nào cần Multi-Agent: cần cách ly dữ liệu, ví dụ Agent Marketing không được thấy dữ liệu "
    "của Agent HR; cần quyền truy cập khác nhau, ví dụ Agent Dev có quyền exec còn Agent Content thì không; "
    "cần bộ nhớ riêng biệt cho từng dự án hoặc khách hàng; và tăng throughput khi nhiều tác vụ chạy song "
    "song không phải chờ nhau. Sơ đồ đội AI ba Agent tiêu biểu: Agent Content viết bài, Agent Research tìm "
    "kiếm và phân tích, Agent Ops lo email và lịch trình."
)
VI_MULTI_AGENT_2 = (
    "CÁCH TẠO AGENT MỚI: openclaw agent create --name 'content-agent' --workspace ./workspace-content và "
    "openclaw agent create --name 'research-agent' --workspace ./workspace-research. Mỗi workspace có "
    "SOUL.md riêng và TOOLS.md riêng; có thể kết nối kênh riêng, agent 1 dùng Telegram, agent 2 dùng "
    "Slack. Các Agent có thể nhận và gửi task cho nhau thông qua sessions: Agent Marketing nhận yêu cầu "
    "từ bạn, chuyển cho Research Agent tìm dữ liệu, nhận kết quả về và viết nội dung hoàn chỉnh. Bắt đầu "
    "với một agent, thêm dần khi cần."
)


def english_blocks() -> list[Block]:
    return [
        _page("OpenClaw Tutorial - From Beginner to Intermediate. Gateway: the Message Gateway connects "
              "Telegram, Discord and WhatsApp; the API Gateway is an HTTP interface. Skills are OpenClaw's "
              "capability extensions, similar to plugins or apps. Memory keeps your preferences and "
              "previous conversation context.", 5),
        _page("Practice Task 10: Create Custom Skill. Create a custom Skill that implements a weather "
              "assistant, a task manager or a website monitor. Reload skills with openclaw skills reload "
              "and test one with openclaw skills test.", 9),
        _page(EN_MULTI_AGENT, 10),
        _page(EN_MULTI_AGENT_2, 10),
        _page("Step 12: Automation with cron jobs. Schedule a daily news summary and send it to your "
              "messaging platform every morning.", 11),
    ]


def vietnamese_blocks() -> list[Block]:
    return [
        _heading("HƯỚNG DẪN TOÀN DIỆN", 1, 1),
        _page("LÀM CHỦ OPENCLAW - AI Agent mã nguồn mở. Cài đặt, cấu hình, tích hợp, tự động hóa.", 1,
              anchor=ANCHOR_NONE),
        _heading("1 THẾ GIỚI", 1, 2),
        _page("MỤC LỤC. Phần 1: OpenClaw Là Gì? Phần 8: 21 Use Cases Thực Chiến. Phần 9: Multi-Agent - "
              "Xây Dựng Đội AI. Phần 10: Bảo Mật & Triển Khai Trên VPS. Phần 11: So Sánh OpenClaw vs Manus.", 2,
              anchor=ANCHOR_NONE),
        _heading("PHẦN 1", 2, 5),
        _page("OPENCLAW LÀ GÌ? OpenClaw là một AI Agent mã nguồn mở chạy trực tiếp trên máy tính của bạn.", 5,
              anchor=ANCHOR_NONE),
        _heading("PHẦN 9", 2, 43),
        _page(VI_MULTI_AGENT, 43, anchor=ANCHOR_NONE),
        _page(VI_MULTI_AGENT_2, 44),
        _heading("PHẦN 10", 2, 45),
        _page("BẢO MẬT & TRIỂN KHAI TRÊN VPS. OpenClaw chạy cục bộ với dữ liệu được mã hóa trên máy của bạn.", 45,
              anchor=ANCHOR_NONE),
        _heading("PHẦN 11", 2, 48),
        _page("SO SÁNH OPENCLAW & CÁC AI AGENT. Multi-Agent: có, tùy biến hoàn toàn; Manus AI hỗ trợ hạn chế.",
              49, anchor=ANCHOR_NONE),
    ]


def _add(db: IndexDB, rel: str, blocks: list[Block], *, kind: str = "pdf") -> dict[str, Any]:
    name = rel.rsplit("/", 1)[-1]
    row = db.insert_file("local", rel, path_hash(rel), name=name, name_folded=fold(name),
                         ext="." + name.rsplit(".", 1)[-1].lower(), kind=kind, folder_id=None,
                         status="dirty", size=12_000, mtime=T0, mtime_ns=int(T0 * 1e9))
    card = make_file_card(name=name, rel_path=rel, kind=kind, size=12_000, mtime_iso="2026-09-26",
                          summary_text=blocks[0].text if blocks else None)
    db.apply_chunks(file_id=row["id"], folder_id=None, source="local", diff=diff_chunks([], [card] + chunk_blocks(blocks)),
                    file_fields={"status": "indexed"})
    return db.get_file(row["id"])


def body(db: IndexDB, row: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in db.chunks_of_file(int(row["id"])) if c["ctype"] == "body"]


def build(path: str, *, profile_uid: str = "u-guides") -> SimpleNamespace:
    """Both guides in a fresh index at ``path``."""
    db = IndexDB.open(path, profile_uid=profile_uid)
    en = _add(db, EN_NAME, english_blocks())
    vi = _add(db, VI_NAME, vietnamese_blocks())
    return SimpleNamespace(db=db, en=en, vi=vi)


class Store:
    """A vector store answering with the hits the test chose."""

    def __init__(self, hits: list[tuple[int, float]]):
        self.hits = hits

    def query_vectors(self, name, vector, k, filt=None):
        return self.hits[:k]


class Embedder:
    model_key = "fake"
    dimension = 4

    def embed_search_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def activate_vectors(db: IndexDB) -> None:
    db.add_collection(1, "ud_guides", model_key="fake", dim=4, store_key="s", state="active")
    db.set_vec_gen([r["id"] for r in db.read_sql("SELECT id FROM chunks")], 1)


def engine(g: SimpleNamespace, *, vector_hits: list[tuple[int, float]] | None = None,
           profile: str = "p") -> QueryEngine:
    """A query engine over the guides; with ``vector_hits`` the vector list
    ranks those chunks first (hybrid mode), else keywords alone."""
    if vector_hits is None:
        return QueryEngine(profile, g.db, tz=UTC, snapshot={}, vector_handles=lambda: None)
    store = Store(vector_hits)
    return QueryEngine(profile, g.db, tz=UTC, snapshot={}, vector_handles=lambda: (Embedder(), store))


def page_chunk(db: IndexDB, row: dict[str, Any], page: int, *, contains: str | None = None) -> dict[str, Any]:
    """The body chunk of ``row`` on ``page`` (containing ``contains``, when given)."""
    for c in body(db, row):
        loc = c.get("locator") or {}
        if loc.get("page") == page and (contains is None or contains in (c.get("text") or "")):
            return c
    raise AssertionError(f"no chunk on page {page} of {row['rel_path']}")
