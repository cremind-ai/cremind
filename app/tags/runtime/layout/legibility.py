"""Per-script legibility floors: the smallest text size each script stays readable at on a 1-bit tag.

Judged from renders of the multilingual samples (`compose.samples.MULTILINGUAL`)
with the full pack at 12, 14 and 16 px (FreeType mono autohint, docs/tags/layout.md
"Legibility floors"):

- **12 px** — Latin, Greek, Cyrillic (Vietnamese included), Armenian, Georgian,
  Hebrew and Cherokee are clean;
- **14 px** — Arabic and the scripts drawn like it (Syriac, Thaana, N'Ko,
  Adlam, Hanifi Rohingya), Ethiopic, Canadian syllabics, Thai, Lao, kana and
  Hangul: at 12 px their dots, vowel marks and small strokes crowd;
- **16 px** — Han, Bopomofo, the Indic scripts, Sinhala, Khmer, Myanmar,
  Tibetan, Mongolian and every script not measured: broken or clotted below.

Common and Inherited characters (digits, punctuation, symbols, combining marks,
emoji) and unassigned or private-use ones (drawn as ``.notdef`` boxes) have no
floor of their own. The engine never applies the floors; the composer raises a
text's size to `legible_size(text)` (`FontContext.nearest_size` then picks the
strike).
"""

from __future__ import annotations

from .unicode import script_of

MIN_LEGIBLE_PX: dict[str, int] = {
    **dict.fromkeys(("Latn", "Grek", "Cyrl", "Armn", "Geor", "Hebr", "Cher"), 12),
    **dict.fromkeys(("Arab", "Syrc", "Thaa", "Nkoo", "Adlm", "Rohg", "Ethi", "Cans", "Thai", "Laoo", "Hira", "Kana",
                     "Hang"), 14),
    **dict.fromkeys(("Hani", "Bopo", "Deva", "Beng", "Guru", "Gujr", "Orya", "Taml", "Telu", "Knda", "Mlym", "Sinh",
                     "Khmr", "Mymr", "Tibt", "Mong"), 16),
}
"""ISO 15924 script -> smallest legible text size in px."""
NEUTRAL_MIN_PX = 12
"""Common, Inherited and unknown characters."""
DEFAULT_MIN_PX = 16
"""Any script not in `MIN_LEGIBLE_PX`."""
_NEUTRAL = frozenset({"Zyyy", "Zinh", "Zzzz"})


def legible_size(text: str) -> int:
    """The smallest text size (px) at which every script of ``text`` stays legible.

    The largest floor over the scripts of its code points; Common, Inherited and
    unknown characters count as `NEUTRAL_MIN_PX` (so emoji never raise it), and
    an empty text needs 12 px.
    """
    floor = NEUTRAL_MIN_PX
    for script in {script_of(ord(c)) for c in text}:
        if script not in _NEUTRAL:
            floor = max(floor, MIN_LEGIBLE_PX.get(script, DEFAULT_MIN_PX))
    return floor
