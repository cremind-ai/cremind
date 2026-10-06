"""Per-`FontSet` state layout needs: HarfBuzz fonts, cmap coverage, face choice, pack glyphs.

`FontContext.for_fontset(fonts)` is cached per FontSet (weakly), so the
daemon's first composition pays for loading and every later one reuses:

- HarfBuzz faces built from the **exact cached file bytes** the pack was
  rasterised from (glyph ids match the pack 1:1), with the face's variation
  coordinates applied, one ``hb.Font`` per (face, size) scaled to 26.6 at the
  strike's pixel size;
- cmap coverage (`app.tags.runtime.fonts.coverage.CmapIndex`);
- the font pack itself (`FontPack`) for ink extents, to skip ink-less glyphs
  and for the hinted advances of grid-fitted runs.

Face choice for one grapheme cluster (docs/tags/layout.md "Font selection"):

1. emoji presentation -> the emoji face when it maps the cluster;
2. the faces declaring the cluster's resolved script, best first by language
   (`candidate_faces`: longest BCP-47 match, then primary, CJK region,
   supplement), the first that maps every character that needs a glyph;
3. the previous cluster's face, when it maps the cluster (keeps punctuation and
   digits in the run's font);
4. every face that maps the cluster, by language match, role (primary, CJK
   region, supplement, optional, emoji last unless asked) and face id;
5. a face mapping the base character only; else the previous / base face, whose
   ``.notdef`` then shows the gap and the characters are reported unsupported.

Only faces with a strike at the requested size take part. Weight siblings
(``FaceInfo.regular_face_id`` set, e.g. Noto Sans Bold) never do: `styled`
swaps a chosen regular face for its sibling afterwards, when bold is asked for
and the sibling has the strike and maps the cluster.
"""

from __future__ import annotations

import struct
import threading
import weakref
from collections.abc import Sequence
from functools import lru_cache
from typing import Literal

import uharfbuzz as hb

from app.tags.runtime.fontpack.format import FontPack, GlyphBitmap
from app.tags.runtime.fonts.coverage import CmapIndex, _lang_match, candidate_faces, needs_glyph
from app.tags.runtime.fonts.fontset import FaceInfo, FontSet
from app.tags.runtime.protocol.ids import FONT_SIZES

Weight = Literal["regular", "bold"]
Leading = Literal["font", "tight"]
WEIGHTS: dict[str, int] = {"regular": 400, "bold": 700}
"""Engine weight names -> design weights on the CSS scale (`FaceInfo.weight`)."""
GRID_FIT_SCRIPTS = frozenset({"Latn", "Grek", "Cyrl"})
"""Scripts whose faces are drawn on the pack's hinted advances (docs/tags/layout.md "Pipeline", positions)."""
_TIGHT_TOP = "bdfhklHT0(["
"""Characters whose ink sets the "tight" ascent (+1 px above the tallest)."""
_TIGHT_BOTTOM = "gjpqy(),;["
"""Characters whose ink sets the "tight" descent."""
_GLYPH_ENTRY = struct.Struct("<IBBbbHH")
"""A pack glyph-index entry (docs/fontpack.md §2): offset, width, height, bearings, advance, reserved."""

_ROLE_RANK = {"primary": 0, "cjk-region": 1, "supplement": 2, "optional": 3, "emoji": 4, "weight": 9}
_HAN_FAMILY = frozenset({"Hani", "Hira", "Kana", "Hang", "Bopo", "Hrkt"})
_contexts: weakref.WeakKeyDictionary[FontSet, FontContext] = weakref.WeakKeyDictionary()
_contexts_lock = threading.Lock()


@lru_cache(maxsize=8)
def _load_pack(path: str, pack_id: bytes) -> FontPack:
    with open(path, "rb") as fh:
        pack = FontPack(fh.read(), verify_content=False)
    if pack.pack_id != pack_id:
        raise ValueError(f"{path} changed on disk: pack id {pack.pack_id.hex()}, the FontSet has {pack_id.hex()}")
    return pack


class FontContext:
    """Shaping and fallback state for one `FontSet` (see the module docstring)."""

    def __init__(self, fonts: FontSet) -> None:
        self.fonts = fonts
        self.shapeable: dict[int, FaceInfo] = {f.face_id: f for f in fonts.faces if f.path is not None}
        """Every face HarfBuzz can shape with (weight siblings included)."""
        self.text_faces: dict[int, FaceInfo] = {fid: f for fid, f in self.shapeable.items()
                                                if f.regular_face_id is None}
        """The faces fallback chooses from: shapeable faces that are not a weight sibling."""
        if not self.text_faces:
            raise ValueError("the font set has no text faces")
        self._weights: dict[tuple[int, int], int] = {
            (f.regular_face_id, f.weight): f.face_id for f in self.shapeable.values()
            if f.regular_face_id is not None and f.regular_face_id in self.text_faces}
        """(regular face, weight) -> its weight sibling."""
        self.index = CmapIndex.for_fontset(fonts)
        self.emoji_face = next((f.face_id for f in fonts.faces if f.role == "emoji"), None)
        self._hb_faces: dict[int, hb.Face] = {}
        self._hb_fonts: dict[tuple[int, int], hb.Font] = {}
        self._covering: dict[int, tuple[int, ...]] = {}
        self._candidates: dict[tuple[str, str, int], tuple[int, ...]] = {}
        self._fallback: dict[tuple[tuple[int, ...], str, int, bool], tuple[int, ...]] = {}
        self._ink: dict[tuple[int, int, int], tuple[int, int, int, int] | None] = {}
        self._advances: dict[tuple[int, int], tuple[int, ...]] = {}
        self._nominal: dict[tuple[int, int], tuple[int, ...]] = {}
        self._grid_fit: dict[int, bool] = {}
        self._tight: dict[tuple[int, int], tuple[int, int]] = {}
        self._lock = threading.RLock()

    @classmethod
    def for_fontset(cls, fonts: FontSet) -> FontContext:
        with _contexts_lock:
            ctx = _contexts.get(fonts)
            if ctx is None:
                ctx = _contexts[fonts] = cls(fonts)
            return ctx

    # ------------------------------------------------------------------ pack and strikes

    @property
    def pack(self) -> FontPack:
        return _load_pack(str(self.fonts.pack_path), self.fonts.pack_id)

    def has_strike(self, face_id: int, size_px: int) -> bool:
        return self.fonts.has_strike(face_id, size_px)

    def base_face(self, size_px: int) -> int:
        """The face whose metrics set the minimum line box (Noto Sans, face 1, when present)."""
        if 1 in self.text_faces and self.has_strike(1, size_px):
            return 1
        for fid in sorted(self.text_faces):
            if self.has_strike(fid, size_px):
                return fid
        sizes = ", ".join(map(str, self.text_sizes())) or "no"
        raise ValueError(f"no text face has a {size_px} px strike: this pack has {sizes} px text strikes; "
                         "use FontContext.nearest_size()")

    def text_sizes(self) -> tuple[int, ...]:
        """FONT_SIZES the base face has strikes for (the dev pack has no 32 px; older packs no 12/14 px)."""
        base = 1 if 1 in self.text_faces else min(self.text_faces)
        return tuple(s for s in FONT_SIZES if self.has_strike(base, s))

    def nearest_size(self, want: int) -> int:
        """The text size to use for ``want`` px: the largest available ≤ ``want``, else the smallest."""
        sizes = self.text_sizes()
        if not sizes:
            raise ValueError("the pack has no text strikes")
        fitting = [s for s in sizes if s <= want]
        return max(fitting) if fitting else min(sizes)

    def line_box(self, size_px: int, leading: Leading | tuple[int, int] = "font") -> tuple[int, int]:
        """(ascent, descent) every line of a text starts from before it grows to its ink.

        ``"font"``: the base face's strike metrics (18/5 at 16 px). ``"tight"``: from the base face's ink,
        one pixel above its tallest ascender / digit / bracket and down to its deepest descender (13/4 at
        16 px), each capped at the strike value. A tuple is used as given.
        """
        base = self.base_face(size_px)  # also rejects a size the pack lacks
        if isinstance(leading, tuple):
            if len(leading) != 2 or not all(isinstance(v, int) and v >= 0 for v in leading):
                raise ValueError(f"leading (ascent, descent) must be two whole pixels >= 0, not {leading!r}")
            return leading
        strike = self.fonts.strike(base, size_px)
        if leading == "font":
            return strike.ascent, strike.descent
        if leading != "tight":
            raise ValueError(f"leading must be 'font', 'tight' or (ascent, descent), not {leading!r}")
        key = (base, size_px)
        box = self._tight.get(key)
        if box is None:
            font = self.hb_font(base, size_px)

            def inks(chars: str) -> list[tuple[int, int, int, int]]:
                gids = (font.get_nominal_glyph(ord(c)) for c in chars)
                return [ink for gid in gids if gid and (ink := self.ink(base, size_px, gid)) is not None]

            tops = [-ink[1] + 1 for ink in inks(_TIGHT_TOP)]
            bottoms = [ink[3] for ink in inks(_TIGHT_BOTTOM)]
            box = self._tight[key] = (min(strike.ascent, max(tops, default=strike.ascent)),
                                      min(strike.descent, max(bottoms, default=strike.descent)))
        return box

    def glyph(self, face_id: int, size_px: int, glyph_id: int) -> GlyphBitmap | None:
        return self.pack.glyph(face_id, size_px, glyph_id)

    def advances(self, face_id: int, size_px: int) -> tuple[int, ...]:
        """Whole-pixel advance of every glyph of a strike, as the pack stores it (hinted, by glyph id)."""
        key = (face_id, size_px)
        out = self._advances.get(key)
        if out is None:
            pack = self.pack
            strike = pack.strike(face_id, size_px)
            if strike is None:
                raise ValueError(f"the pack has no ({face_id}, {size_px}) strike")
            index = pack.data[strike.index_off:strike.index_off + strike.glyph_count * _GLYPH_ENTRY.size]
            out = self._advances[key] = tuple(entry[5] for entry in _GLYPH_ENTRY.iter_unpack(index))
        return out

    def nominal_advances(self, face_id: int, size_px: int) -> tuple[int, ...]:
        """HarfBuzz's unhinted advance (26.6, ``x_advance`` before kerning) of every glyph of the face at
        ``size_px``, by glyph id."""
        key = (face_id, size_px)
        out = self._nominal.get(key)
        if out is None:
            font = self.hb_font(face_id, size_px)
            out = self._nominal[key] = tuple(font.get_glyph_h_advance(gid) for gid in range(font.face.glyph_count))
        return out

    def nominal_advance(self, face_id: int, size_px: int, glyph_id: int) -> int:
        """HarfBuzz's unhinted advance of one glyph in 26.6 (``x_advance`` before kerning)."""
        return self.nominal_advances(face_id, size_px)[glyph_id]

    # ------------------------------------------------------------------ weights and grid fitting

    def regular_of(self, face_id: int) -> int:
        """The regular face of a weight sibling (Noto Sans Bold -> Noto Sans); any other face itself."""
        info = self.shapeable.get(face_id)
        return info.regular_face_id if info is not None and info.regular_face_id is not None else face_id

    def styled(self, face_id: int, weight: Weight | int, size_px: int, cluster: Sequence[int]) -> int:
        """``face_id``'s weight sibling for one cluster, when it has the strike and maps every character the
        cluster needs (an empty need is fine); otherwise ``face_id`` (the regular weight is drawn)."""
        sibling = self._weights.get((face_id, _weight(weight)))
        if sibling is None or not self.has_strike(sibling, size_px):
            return face_id
        return sibling if self.maps(sibling, [cp for cp in cluster if needs_glyph(chr(cp))]) else face_id

    def has_weight(self, weight: Weight | int, size_px: int, face_id: int = 1) -> bool:
        """Whether ``face_id`` (Noto Sans by default) can be drawn at ``weight`` and ``size_px``."""
        value = _weight(weight)
        regular = self.regular_of(face_id)
        if value == WEIGHTS["regular"]:
            return regular in self.text_faces and self.has_strike(regular, size_px)
        sibling = self._weights.get((regular, value))
        return sibling is not None and self.has_strike(sibling, size_px)

    def grid_fit(self, face_id: int) -> bool:
        """Whether runs in ``face_id`` are placed on the pack's hinted advances: its regular face declares
        only Latin, Greek and Cyrillic (Noto Sans and its Bold)."""
        out = self._grid_fit.get(face_id)
        if out is None:
            info = self.shapeable.get(self.regular_of(face_id))
            out = self._grid_fit[face_id] = bool(info is not None and info.scripts
                                                 and set(info.scripts) <= GRID_FIT_SCRIPTS)
        return out

    def ink(self, face_id: int, size_px: int, glyph_id: int) -> tuple[int, int, int, int] | None:
        """(left, top, right, bottom) of the glyph's bitmap relative to its origin, or None without ink."""
        key = (face_id, size_px, glyph_id)
        box = self._ink.get(key, False)
        if box is False:
            g = self.pack.glyph(face_id, size_px, glyph_id)
            if g is None or g.empty or not g.bitmap or not any(g.bitmap):
                box = None
            else:
                box = (g.bearing_x, -g.bearing_y, g.bearing_x + g.width, -g.bearing_y + g.height)
            self._ink[key] = box
        return box  # type: ignore[return-value]

    # ------------------------------------------------------------------ HarfBuzz

    def hb_font(self, face_id: int, size_px: int) -> hb.Font:
        key = (face_id, size_px)
        font = self._hb_fonts.get(key)
        if font is None:
            with self._lock:
                face = self._hb_faces.get(face_id)
                if face is None:
                    info = self.shapeable[face_id]
                    assert info.path is not None
                    face = self._hb_faces[face_id] = hb.Face(hb.Blob(info.path.read_bytes()))
                font = hb.Font(face)
                font.scale = (size_px * 64, size_px * 64)
                font.ppem = (size_px, size_px)
                variations = self.shapeable[face_id].variations
                if variations:
                    font.set_variations(dict(variations))
                self._hb_fonts[key] = font
        return font

    # ------------------------------------------------------------------ coverage and choice

    def maps(self, face_id: int, cps: Sequence[int]) -> bool:
        cmap = self.index.cmaps.get(face_id)
        return cmap is not None and all(cp in cmap for cp in cps)

    def covering(self, cp: int) -> tuple[int, ...]:
        faces = self._covering.get(cp)
        if faces is None:
            faces = self._covering[cp] = self.index.faces_for(cp)
        return faces

    def covers(self, cp: int) -> bool:
        return self.index.covers(cp)

    def candidates(self, script: str, language: str, size_px: int) -> tuple[int, ...]:
        key = (script, language, size_px)
        out = self._candidates.get(key)
        if out is None:
            out = tuple(f.face_id for f in candidate_faces(self.fonts, script, language)
                        if f.face_id in self.text_faces and self.has_strike(f.face_id, size_px))
            self._candidates[key] = out
        return out

    def _ranked(self, faces: tuple[int, ...], language: str, size_px: int, emoji: bool) -> tuple[int, ...]:
        key = (faces, language, size_px, emoji)
        out = self._fallback.get(key)
        if out is None:
            def rank(fid: int) -> tuple[int, int, int, int]:
                info = self.text_faces[fid]
                role = _ROLE_RANK.get(info.role, 5)
                if emoji and info.role == "emoji":
                    role = -1
                return (-_lang_match(language, info.languages) if language else 0, 0 if fid == 1 else 1, role, fid)
            out = tuple(sorted((f for f in faces if f in self.text_faces and self.has_strike(f, size_px)), key=rank))
            self._fallback[key] = out
        return out

    def choose(self, cluster: Sequence[int], script: str, language: str, size_px: int, previous: int | None,
               emoji: bool | None) -> tuple[int, bool]:
        """(face id, fully mapped) for one grapheme cluster (rules in the module docstring).

        Always a regular face: a weight sibling given as ``previous`` stands for its regular face.
        """
        need = [cp for cp in cluster if needs_glyph(chr(cp))]
        if previous is not None:
            previous = self.regular_of(previous)
        if not need:
            fid = previous if previous is not None else self.base_face(size_px)
            return fid, True
        if emoji and self.emoji_face is not None and self.has_strike(self.emoji_face, size_px) \
                and self.maps(self.emoji_face, need):
            return self.emoji_face, True
        if script not in ("Zyyy", "Zinh", "Zzzz"):
            for fid in self.candidates(script, language, size_px):
                if self.maps(fid, need):
                    return fid, True
        if previous is not None and self.maps(previous, need) and not (previous == self.emoji_face and not emoji):
            return previous, True
        common = set(self.covering(need[0]))
        for cp in need[1:]:
            common &= set(self.covering(cp))
        ranked = self._ranked(tuple(sorted(common)), language, size_px, bool(emoji))
        if emoji is False or emoji is None:
            text_first = [f for f in ranked if f != self.emoji_face]
            ranked = tuple(text_first + [f for f in ranked if f == self.emoji_face])
        if ranked:
            return ranked[0], True
        base_faces = self._ranked(self.covering(need[0]), language, size_px, bool(emoji))
        if base_faces:
            return base_faces[0], False
        return (previous if previous is not None else self.base_face(size_px)), False


def _weight(weight: Weight | int) -> int:
    """A weight name ("regular", "bold") or a CSS weight -> the CSS weight."""
    if isinstance(weight, int):
        return weight
    try:
        return WEIGHTS[weight]
    except KeyError:
        raise ValueError(f"weight must be one of {', '.join(WEIGHTS)}, not {weight!r}") from None


def han_language(language: str, scripts: set[str]) -> str:
    """Language used to pick CJK faces: the hint, or ``ja``/``ko`` inferred from kana/hangul in the text."""
    primary = (language or "").split("-", 1)[0].lower()
    if primary in ("zh", "ja", "ko", "yue"):
        return language
    if scripts & {"Hira", "Kana", "Hrkt"}:
        return "ja"
    if "Hang" in scripts:
        return "ko"
    return language


def is_han_family(script: str) -> bool:
    return script in _HAN_FAMILY
