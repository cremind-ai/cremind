"""textnorm: the three notions of "same text" and the token estimator."""

from __future__ import annotations

import random
import unicodedata

import pytest

from app.documents import textnorm as tn

NBSP = chr(0xA0)
SOFT_HYPHEN = chr(0xAD)
ZWSP = chr(0x200B)


# ── normalize_ws ───────────────────────────────────────────────────────────


def test_normalize_ws_rules() -> None:
    raw = "a  \t b" + NBSP + "c   \r\n\r\n\r\n\r\nd \n  e\rf  "
    assert tn.normalize_ws(raw) == "a b c\n\nd\n e\nf"


def test_normalize_ws_is_nfc() -> None:
    nfd = unicodedata.normalize("NFD", "Luật Đất đai")
    assert nfd != "Luật Đất đai"
    assert tn.normalize_ws(nfd) == "Luật Đất đai"


def test_normalize_ws_idempotent() -> None:
    s = "x \t y\r\n\r\n\r\n z  "
    once = tn.normalize_ws(s)
    assert tn.normalize_ws(once) == once


# ── fold ───────────────────────────────────────────────────────────────────


def test_fold_vietnamese_including_d_stroke() -> None:
    assert tn.fold("Luật Đất đai") == "luat dat dai"
    assert tn.fold("ĐIỀU 12. Quyền và nghĩa vụ") == "dieu 12. quyen va nghia vu"
    assert tn.fold("Nguyễn Thị Hường") == "nguyen thi huong"


def test_fold_decomposed_input_matches_precomposed() -> None:
    s = "Tiếng Việt có dấu"
    assert tn.fold(unicodedata.normalize("NFD", s)) == tn.fold(s) == "tieng viet co dau"


def test_fold_if_needed_skips_plain_english() -> None:
    assert tn.fold_if_needed("The Land Act, Article 3(2)") is None
    assert tn.fold_if_needed("") is None
    assert tn.fold_if_needed("Điều 12") == "dieu 12"
    # Non-ASCII that folds to the same thing beyond case stores nothing.
    assert tn.fold_if_needed("ÜBER") == "uber"
    assert tn.fold_if_needed("Москва") is None


# ── normalize_for_match ────────────────────────────────────────────────────


def test_normalize_for_match_unifies_typography_keeps_diacritics() -> None:
    quoted = "“Người sử dụng đất” — có quyền…  ‘tạm thời’ – 5 − 3"
    assert tn.normalize_for_match(quoted) == '"người sử dụng đất" - có quyền... \'tạm thời\' - 5 - 3'
    # Diacritics are meaning in Vietnamese: "đất" (land) is not "dat".
    assert tn.normalize_for_match("đất") != tn.normalize_for_match("dat")


def test_normalize_for_match_drops_invisible_chars_and_collapses_ws() -> None:
    s = "hiệu" + SOFT_HYPHEN + "lực" + ZWSP + "\n\n  thi   hành"
    assert tn.normalize_for_match(s) == "hiệulực thi hành"
    assert tn.normalize_for_match("ABC\tdef") == "abc def"


# ── estimate_tokens ────────────────────────────────────────────────────────


def test_estimate_tokens_basic_counts() -> None:
    assert tn.estimate_tokens("") == 0
    assert tn.estimate_tokens("word") == 2          # 1.4 → 2
    assert tn.estimate_tokens("one two three") == 5  # 4.2 → 5
    assert tn.estimate_tokens("中文字符") == 4        # one per CJK character
    assert tn.estimate_tokens("ภาษาไทย") == 7        # Thai: one per character
    assert tn.estimate_tokens("a\n\n\nb") == 4       # 2.8 + one newline run


def test_estimate_tokens_at_least_word_count() -> None:
    rng = random.Random(3)
    words = "Người sử dụng đất land law quyền nghĩa vụ article clause".split()
    for _ in range(200):
        text = " ".join(rng.choice(words) for _ in range(rng.randint(1, 80)))
        assert tn.estimate_tokens(text) >= len(text.split())


def test_estimate_tokens_prefix_monotonic() -> None:
    text = (
        "Điều 12. Nguyên tắc sử dụng đất — getUserById(0987654321) “quoted” text; "
        "中文 mixed with English and ALL CAPS HEADERS.\n\n1. Khoản một…"
    )
    prev = 0
    for i in range(len(text) + 1):
        cur = tn.estimate_tokens(text[:i])
        assert cur >= prev, (i, text[:i])
        prev = cur


def test_estimate_tokens_additive_over_blank_line_joins() -> None:
    rng = random.Random(5)
    pool = ["Luật", "đất", "đai", "The", "Act", "getUser", "0987654321", "“x”", "中文", "(1)", "–"]
    for _ in range(300):
        a = " ".join(rng.choice(pool) for _ in range(rng.randint(1, 30)))
        b = " ".join(rng.choice(pool) for _ in range(rng.randint(1, 30)))
        assert tn.estimate_tokens(a + "\n\n" + b) <= tn.estimate_tokens(a) + tn.estimate_tokens(b) + 1


def test_estimate_tokens_charges_hard_shapes() -> None:
    # Shapes the e5 tokenizer splits into many pieces cost more than 1.4/word.
    assert tn.estimate_tokens("CỘNG HÒA XÃ HỘI") > tn.estimate_tokens("Cộng hòa xã hội")
    assert tn.estimate_tokens("getUserById") > tn.estimate_tokens("getuserbyid")
    assert tn.estimate_tokens("0987654321") >= 5


# ── text_hash ──────────────────────────────────────────────────────────────


def test_text_hash_ignores_whitespace_and_normal_form_noise() -> None:
    h = tn.text_hash("Chương II › Điều 12", "Người sử dụng đất\n\nkhoản 2")
    assert h == tn.text_hash("Chương II › Điều 12  ", "Người sử dụng đất  \r\n\r\n\r\nkhoản 2\n")
    assert h == tn.text_hash(
        unicodedata.normalize("NFD", "Chương II › Điều 12"),
        unicodedata.normalize("NFD", "Người sử dụng đất\n\nkhoản 2"),
    )
    assert len(h) == 32 and int(h, 16) >= 0


def test_text_hash_depends_on_heading_and_text() -> None:
    base = tn.text_hash("", "body")
    assert base == tn.text_hash("", "body")
    assert base != tn.text_hash("Heading", "body")
    assert base != tn.text_hash("", "body!")
    assert tn.text_hash("A", "b") != tn.text_hash("A b", "")


# ── Optional: calibration against the real e5 tokenizer ────────────────────


def _load_e5_tokenizer():
    transformers = pytest.importorskip("transformers")
    try:
        return transformers.AutoTokenizer.from_pretrained(
            "intfloat/multilingual-e5-base", local_files_only=True
        )
    except Exception as exc:  # not cached on this machine
        pytest.skip(f"e5 tokenizer not cached locally: {exc}")


def _mixed_paragraphs(n: int) -> list[str]:
    vi = ("Nhà nước thống nhất quản lý đất đai theo quy hoạch và pháp luật người sử dụng đất "
          "có quyền nghĩa vụ theo quy định tại Luật này thu hồi đất bồi thường hỗ trợ tái định "
          "cư giá đất ủy ban nhân dân cấp tỉnh báo cáo kết quả kinh doanh doanh thu lợi nhuận "
          "quý năm tăng trưởng thị trường khách hàng").split()
    en = ("the land law of the republic shall apply to all users of land including households "
          "organizations compensation resettlement business results revenue profit quarter "
          "growth market artificial intelligence challenges robot tracking object motion "
          "notwithstanding jurisdiction indemnification").split()
    extra = ["31/2024/QH15", "Điều 12", "khoản 2", "(1)", "2024-08-01", "12,5%", "“trích dẫn”", "CHƯƠNG II"]
    rng = random.Random(2024)
    out = []
    for _ in range(n):
        words = []
        for _ in range(rng.randint(8, 120)):
            r = rng.random()
            w = rng.choice(vi) if r < 0.5 else rng.choice(en) if r < 0.93 else rng.choice(extra)
            if rng.random() < 0.1:
                w = w.capitalize()
            if rng.random() < 0.08:
                w += rng.choice([",", ".", ";", ":"])
            words.append(w)
        out.append(" ".join(words) + ".")
    return out


def test_estimate_is_at_least_e5_token_count() -> None:
    tok = _load_e5_tokenizer()
    paras = _mixed_paragraphs(300)
    under = [
        p for p in paras
        if tn.estimate_tokens(p) < len(tok(p, add_special_tokens=False)["input_ids"])
    ]
    # Measured 0/300 when written; the requirement is ≥ 99%.
    assert len(under) <= 3, under[:3]
