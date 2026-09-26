"""Synthetic regulations for the research discovery regressions.

The primary fixtures are English: a decree on foreign workers whose header is
the generic heading "DECREE" (its subject is on the next line, as in the
reported incident), with a notification rule in Article 22(5), reissuance
cases in Article 23 and a reissuance dossier in Article 24 whose clause 3
wraps a reference to Article 23(2) onto a line of its own. Around it: a
background note about work permits (not a legal document), an unrelated
regulation, two editions of an act, and two decrees whose only title is
"DECREE".

A small Vietnamese set mirrors the reported decree (ND-219-2025-CP): the
same generic heading with its subject line, Điều 22 khoản 5, Điều 23 and the
wrapped "khoản 2 / Điều 23 Nghị định này." inside Điều 24.

Every line is one block, as a plain-text extraction delivers it.
"""

from __future__ import annotations

DECREE_EN = [
    "GOVERNMENT",
    "No. 12/2030/GOV",
    "Capital City, 5 March 2030",
    "DECREE",
    "On the employment of foreign workers",
    "Pursuant to the Labour Code;",
    "Chapter I",
    "GENERAL PROVISIONS",
    "Article 1. Scope",
    "This Decree governs the employment of foreign workers, including the issuance, reissuance and extension "
    "of work permits.",
    "Article 2. Definitions",
    "(1) A work permit is the document that allows a foreign worker to work for an employer.",
    "Chapter II",
    "WORK PERMITS",
    "Article 22. Procedure for issuing a work permit",
    "(1) The employer shall submit the application at least 15 days before the foreign worker starts work.",
    "(5) Where a foreign worker holding a valid work permit is to work for the same employer in several "
    "provinces, the employer shall notify the labour authority of each province at least 3 days before the "
    "work starts; no new work permit is required.",
    "Article 23. Cases for reissuing a work permit",
    "(1) The work permit is lost or damaged.",
    "(2) A particular stated in the work permit changes: the name, the nationality, the passport number or the "
    "name of the employer.",
    "Article 24. Reissuance dossier",
    "(1) The application for reissuance, signed by the employer.",
    "(2) Two colour photographs.",
    "(3) The documents proving the change referred to in",
    "Article 23(2) of this Decree.",
    "(4) The existing work permit, except where it was lost under Article 23(1) of this Decree.",
    "Article 25. Reissuance procedure",
    "(1) The employer shall submit the reissuance dossier to the labour authority that issued the work permit.",
    "(2) Within 3 working days of receiving a complete dossier, the labour authority shall reissue the work "
    "permit.",
    "Article 40. Entry into force",
    "This Decree takes effect on 1 May 2030.",
]

# Mentions work permits throughout, but is no legal instrument.
NEWS_NOTE = [
    "Work permit tips for foreign workers",
    "Many foreign workers ask how long a work permit takes and whether moving to another province changes it.",
    "This note collects advice from forum posts; it is not legal advice.",
]

# A regulation on another subject that shares ordinary words (notify,
# authority, change, province).
FOOD_REG = [
    "MINISTRY OF HEALTH",
    "No. 7/2029/MOH",
    "REGULATION ON FOOD SAFETY",
    "Article 1. Scope",
    "This Regulation governs food premises and food handlers.",
    "Article 2. Licensing of premises",
    "A food business must hold a licence for each of its premises.",
    "Article 3. Change of premises",
    "An operator that moves its premises to another province shall notify the food authority of that province.",
    "Article 4. Inspections",
    "The food authority inspects licensed premises every year.",
    "Article 5. Entry into force",
    "This Regulation takes effect on 1 January 2029.",
]


def act_edition(year: int, number: str, effective: str, *, extra: str = "") -> list[str]:
    """One edition of the Labour Migration Act."""
    return [
        "NATIONAL ASSEMBLY",
        f"Law No. {number}",
        "LABOUR MIGRATION ACT",
        "Article 1. Scope",
        "This Act governs workers who move between provinces for work.",
        "Article 2. Registration",
        f"A worker who moves to another province shall register within 30 days.{extra}",
        "Article 3. Employers",
        "An employer shall keep a register of the workers it moves between provinces.",
        "Article 4. Inspections",
        "The labour inspectorate may inspect the register.",
        "Article 5. Entry into force",
        f"This Act takes effect on {effective}.",
    ]


def generic_decree(number: str, subject_words: str, effective: str) -> list[str]:
    """A decree whose only title is "DECREE": its header names nothing else
    (the legal basis follows at once)."""
    return [
        "GOVERNMENT",
        f"No. {number}",
        "DECREE",
        "Pursuant to the Law on Government Organisation;",
        "Article 1. Scope",
        f"This Decree governs {subject_words}.",
        "Article 2. Duties",
        f"Operators shall comply with the rules on {subject_words}.",
        "Article 3. Fines",
        f"Breaches of the rules on {subject_words} are fined.",
        "Article 4. Authority",
        "The provincial authority enforces this Decree.",
        "Article 5. Entry into force",
        f"This Decree takes effect on {effective}.",
    ]


def long_article_decree() -> list[str]:
    """A regulation whose Article 7 is longer than one reading."""
    clauses = [
        f"({i}) The employer shall keep record {i} of the foreign worker's assignment, including the "
        f"province, the branch, the start date and the duties performed there, and shall make record {i} "
        "available to the labour authority on request during an inspection of the branch." for i in range(1, 15)
    ]
    return [
        "GOVERNMENT",
        "No. 55/2031/GOV",
        "DECREE",
        "On assignment records for foreign workers",
        "Pursuant to the Labour Code;",
        "Article 1. Scope",
        "This Decree governs assignment records for foreign workers.",
        "Article 2. Definitions",
        "An assignment record is a record of where a foreign worker works.",
        "Article 3. Retention",
        "Records are kept for five years.",
        "Article 4. Language",
        "Records are kept in the official language.",
        "Article 7. Assignment records",
        *clauses,
        "Article 9. Entry into force",
        "This Decree takes effect on 1 June 2031.",
    ]


# ── Vietnamese: shaped like the reported decree ────────────────────────────

DECREE_VI = [
    "CHÍNH PHỦ",
    "Số: 219/2025/NĐ-CP",
    "Hà Nội, ngày 07 tháng 8 năm 2025",
    "NGHỊ ĐỊNH",
    "Quy định về người lao động nước ngoài làm việc tại Việt Nam",
    "Căn cứ Bộ luật Lao động;",
    "Chương I",
    "NHỮNG QUY ĐỊNH CHUNG",
    "Điều 1. Phạm vi điều chỉnh",
    "Nghị định này quy định về cấp, cấp lại, gia hạn giấy phép lao động cho người lao động nước ngoài.",
    "Điều 2. Đối tượng áp dụng",
    "1. Người lao động nước ngoài làm việc tại Việt Nam.",
    "Chương III",
    "GIẤY PHÉP LAO ĐỘNG",
    "Điều 22. Trình tự cấp giấy phép lao động",
    "1. Người sử dụng lao động nộp hồ sơ đề nghị cấp giấy phép lao động.",
    "5. Trường hợp người lao động nước ngoài đã được cấp giấy phép lao động có nhu cầu làm việc cho người sử "
    "dụng lao động đó tại nhiều tỉnh, thành phố, trước ít nhất 3 ngày dự kiến làm việc, người sử dụng lao "
    "động phải thông báo cho cơ quan có thẩm quyền nơi người lao động nước ngoài đến làm việc.",
    "Điều 23. Các trường hợp cấp lại giấy phép lao động",
    "1. Giấy phép lao động còn thời hạn bị mất hoặc bị hư hỏng.",
    "2. Thay đổi một trong các nội dung ghi trong giấy phép lao động còn thời hạn: họ và tên; quốc tịch; số "
    "hộ chiếu.",
    "Điều 24. Hồ sơ đề nghị cấp lại giấy phép lao động",
    "1. Văn bản đề nghị cấp lại giấy phép lao động.",
    "2. 02 ảnh màu.",
    "3. Giấy tờ chứng minh thay đổi nội dung theo quy định tại khoản 2",
    "Điều 23 Nghị định này.",
    "4. Giấy phép lao động còn thời hạn, trừ trường hợp bị mất theo quy định tại khoản 1 Điều 23 Nghị định "
    "này.",
    "Điều 25. Trình tự cấp lại giấy phép lao động",
    "1. Người sử dụng lao động nộp hồ sơ đề nghị cấp lại giấy phép lao động.",
    "Điều 40. Hiệu lực thi hành",
    "Nghị định này có hiệu lực thi hành từ ngày 07 tháng 8 năm 2025.",
]
