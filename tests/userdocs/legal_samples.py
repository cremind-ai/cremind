"""Synthetic legal documents shared by the chunking tests.

Built in code rather than checked in as fixtures so the structure under test
(chapters, articles, clauses, points, cross-references, dates) is visible
next to the assertions that depend on it.
"""

from __future__ import annotations

from app.userdocs.types import Block

_FILLER_VI = (
    "Người sử dụng đất có trách nhiệm sử dụng đất đúng mục đích, đúng ranh giới thửa đất "
    "và tuân thủ các quy định về bảo vệ môi trường, không làm tổn hại đến lợi ích chính đáng "
    "của người sử dụng đất xung quanh"
)
_FILLER_EN = (
    "The competent authority shall ensure that land is allocated and used in accordance "
    "with the approved plan, with due regard to the rights and legitimate interests of "
    "the persons concerned"
)


def vietnamese_law_lines() -> list[str]:
    """A 12-article law in two chapters, one line per paragraph."""
    lines = [
        "QUỐC HỘI",
        "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM",
        "Độc lập - Tự do - Hạnh phúc",
        "Luật số: 31/2024/QH15",
        "LUẬT",
        "ĐẤT ĐAI",
        "Căn cứ Hiến pháp nước Cộng hòa xã hội chủ nghĩa Việt Nam;",
        "Quốc hội ban hành Luật Đất đai.",
        "Chương I",
        "QUY ĐỊNH CHUNG",
    ]
    for n in range(1, 13):
        if n == 7:
            lines += ["CHƯƠNG II", "QUYỀN VÀ NGHĨA VỤ CỦA NGƯỜI SỬ DỤNG ĐẤT"]
        lines.append(f"Điều {n}. Nội dung quy định số {n}")
        if n == 3:
            # A short article with no clauses: must stay a chunk of its own.
            lines.append("Luật này áp dụng đối với cơ quan nhà nước.")
            continue
        lines.append(f"1. {_FILLER_VI} theo khoản 2 Điều 5 và điểm a khoản 1 Điều 4.")
        lines.append(f"2. {_FILLER_VI}, trừ trường hợp quy định tại Điều 12 của Luật Đất đai quy định chi tiết.")
        lines.append("a) Trường hợp thứ nhất theo quy định tại Điều 3 của Luật này;")
        lines.append(f"b) {_FILLER_VI}.")
        if n == 12:
            lines.append("3. Luật này có hiệu lực thi hành từ ngày 01 tháng 8 năm 2024.")
            lines.append(
                "4. Luật Đất đai số 45/2013/QH13 đã được sửa đổi, bổ sung hết hiệu lực "
                "kể từ ngày Luật này có hiệu lực thi hành."
            )
    lines.append(
        "Luật này được Quốc hội nước Cộng hòa xã hội chủ nghĩa Việt Nam khóa XV, kỳ họp bất "
        "thường lần thứ 5 thông qua ngày 18 tháng 01 năm 2024."
    )
    return lines


def vietnamese_law_blocks() -> list[Block]:
    return [Block(text=t, locator={"line_start": i + 1, "line_end": i + 1})
            for i, t in enumerate(vietnamese_law_lines())]


def english_act_lines() -> list[str]:
    lines = [
        "THE LAND ACT 2020",
        "No. 12/2020/LA",
        "Adopted on March 3, 2020",
        "Chapter I",
        "GENERAL PROVISIONS",
    ]
    for n in range(1, 7):
        if n == 4:
            lines += ["Chapter II", "ALLOCATION"]
        lines.append(f"Article {n}. Provision number {n}")
        lines.append(f"(1) {_FILLER_EN}, subject to Article 3(2) of the Land Act.")
        lines.append(f"(2) {_FILLER_EN}, as provided in Article 2 of this Act and Section 4.2.")
        if n == 6:
            lines.append("(3) This Act shall take effect on January 1, 2021.")
    return lines


def english_act_blocks() -> list[Block]:
    return [Block(text=t, locator={"page": 1 + i // 12}) for i, t in enumerate(english_act_lines())]
