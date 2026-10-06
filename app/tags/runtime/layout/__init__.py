"""Multilingual text layout for tags: plain text -> positioned glyphs -> GLYPHS commands.

See docs/tags/layout.md. Entry points:

- `plain_text` — card text (Markdown / HTML-ish) -> plain NFC text;
- `layout_text` — paragraph layout in a box (ICU graphemes, bidi, line breaks;
  script itemisation; font fallback; weight; HarfBuzz shaping and line-boundary
  reshaping; grid-fitted positions; ellipsis; alignment) -> `TextBlock` with
  `LineBox`es and `PositionedGlyph`s;
- `glyph_commands` / `TextBlock.commands` — GLYPHS commands with i8 deltas;
- `legible_size` — the smallest size a text's scripts stay legible at.
"""

from .commands import command_glyphs, glyph_commands
from .engine import LineBox, PositionedGlyph, TextBlock, layout_text, measure_text
from .fonts import FontContext
from .legibility import legible_size
from .plaintext import plain_text

__all__ = [
    "FontContext",
    "LineBox",
    "PositionedGlyph",
    "TextBlock",
    "command_glyphs",
    "glyph_commands",
    "layout_text",
    "legible_size",
    "measure_text",
    "plain_text",
]
