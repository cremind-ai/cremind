"""Per-script legibility floors (`legible_size`): ICU's script data only, no font pack needed."""

from __future__ import annotations

import pytest

from app.tags.runtime.layout import legible_size
from app.tags.runtime.layout.legibility import DEFAULT_MIN_PX, MIN_LEGIBLE_PX, NEUTRAL_MIN_PX
from app.tags.runtime.layout.unicode import script_of


@pytest.mark.parametrize(("text", "size"), [
    # 12 px: Latin (Vietnamese too), Greek, Cyrillic, Armenian, Georgian, Hebrew, Cherokee.
    ("Approve deployment?", 12), ("Tóm tắt cuộc họp sáng nay", 12), ("Γειά σου", 12), ("Привет", 12),
    ("Բարեւ", 12), ("გამარჯობა", 12), ("שלום", 12), ("ᏣᎳᎩ", 12),
    # 14 px: Arabic and its relatives, Ethiopic, Canadian syllabics, Thai, Lao, kana, Hangul.
    ("مرحبا", 14), ("ܫܠܡܐ", 14), ("ސަލާމް", 14), ("ߒߞߏ", 14), ("𞤀𞤁𞤂", 14), ("𐴀𐴁", 14), ("ሰላም", 14),
    ("ᓀᐦᐃᔭᐍᐏᐣ", 14), ("สวัสดี", 14), ("ສະບາຍດີ", 14), ("こんにちは", 14), ("カタカナ", 14), ("안녕하세요", 14),
    # 16 px: Han, Bopomofo, Indic, Sinhala, Khmer, Myanmar, Tibetan, Mongolian, anything unmeasured.
    ("你好", 16), ("ㄅㄆㄇ", 16), ("नमस्ते", 16), ("হ্যালো", 16), ("ਸਤ ਸ੍ਰੀ", 16), ("નમસ્તે", 16), ("ନମସ୍କାର", 16),
    ("வணக்கம்", 16), ("హలో", 16), ("ನಮಸ್ಕಾರ", 16), ("ഹലോ", 16), ("ආයුබෝවන්", 16), ("សួស្តី", 16),
    ("မင်္ဂလာပါ", 16), ("བཀྲ་ཤིས", 16), ("ᠮᠣᠩᠭᠣᠯ", 16), ("ꦱꦸꦒꦼꦁ", 16),
    # Mixed: the largest floor wins; Common, Inherited and unknown characters never raise it.
    ("Hello 你好", 16), ("Hello مرحبا 123", 14), ("今日 10:00 定例ミーティング", 16), ("2:05 PM", 12),
    ("Done 🎉 👍🏽 ❤️ 1️⃣ 🇻🇳", 12), ("x̂́ q̃", 12), ("\U0010fffd", 12), ("", 12), ("…—«»", 12),
])
def test_legible_size(text: str, size: int) -> None:
    assert legible_size(text) == size


def test_floor_table_uses_icu_script_codes() -> None:
    samples = {"Latn": "a", "Grek": "α", "Cyrl": "д", "Armn": "ա", "Geor": "ა", "Hebr": "ש", "Cher": "Ꮳ",
               "Arab": "ب", "Syrc": "ܫ", "Thaa": "ސ", "Nkoo": "ߒ", "Adlm": "𞤀", "Rohg": "𐴀", "Ethi": "ሰ",
               "Cans": "ᓀ", "Thai": "ส", "Laoo": "ສ", "Hira": "こ", "Kana": "カ", "Hang": "안", "Hani": "你",
               "Bopo": "ㄅ", "Deva": "न", "Beng": "হ", "Guru": "ਸ", "Gujr": "ન", "Orya": "ନ", "Taml": "வ",
               "Telu": "హ", "Knda": "ನ", "Mlym": "ഹ", "Sinh": "ආ", "Khmr": "ស", "Mymr": "မ", "Tibt": "བ",
               "Mong": "ᠮ"}
    assert set(samples) == set(MIN_LEGIBLE_PX)
    for script, ch in samples.items():
        assert script_of(ord(ch)) == script
        assert legible_size(ch) == MIN_LEGIBLE_PX[script]
    assert NEUTRAL_MIN_PX == min(MIN_LEGIBLE_PX.values()) == 12 and DEFAULT_MIN_PX == max(MIN_LEGIBLE_PX.values())
