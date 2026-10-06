# Text layout and screen composition

> Moved here from the cremind-tag firmware repository (`docs/layout.md`) with the hardware
> runtime (`app/tags/runtime/`). `cremind tags tools …` is the runtime's CLI (formerly
> `cremind-tag …`); the normative protocol documents are the pinned contract's
> (`app/tags/runtime/protocol/pinned/docs/`).

Tags contain no font engine: the runtime turns card text into positioned
glyph ids that bridges draw from the font pack ([`fonts.md`](fonts.md),
[`fontpack.md`](../../app/tags/runtime/protocol/pinned/docs/fontpack.md)) with the logical screen format of
[`protocol.md`](../../app/tags/runtime/protocol/pinned/docs/protocol.md) §4. This page covers how text is laid out, how a
tag's screen is composed from its active cards, how previews are rendered and
what is known not to work.

| Code (`app/tags/runtime/`) | What |
|---|---|
| `layout/plaintext.py` | card text (Markdown / HTML-ish) → plain NFC text |
| `layout/unicode.py` | ICU: graphemes, line breaks, bidi, scripts, emoji properties, NFC |
| `layout/fonts.py` | `FontContext`: HarfBuzz fonts, cmap coverage, face choice, weights, sizes, line boxes, pack glyphs |
| `layout/engine.py` | `layout_text` → `TextBlock` (`LineBox`es, `PositionedGlyph`s) |
| `layout/legibility.py` | `legible_size(text)`: the per-script legibility floors |
| `layout/commands.py` | positioned glyphs → `GLYPHS` commands |
| `compose/api.py` | the contract: `TagPanel`, `ActiveCard`, `ScreenSettings`, `ComposedScreen`, `Composer`; `COMPOSER_VERSION` |
| `compose/cards.py` | card policy: visibility, icon, body, tone and label, progress, QR link, order |
| `compose/timefmt.py` | ICU date/time formatting in the profile's language and time zone |
| `compose/tokens.py` | the design tokens: panel classes XS/S/M/L, type scale, spacing, row counts (`tokens_for`) |
| `compose/strings.py` | every English word the composer draws (labels, "Updated", "+N MORE", …) |
| `compose/canvas.py` | `Canvas`: the §4.3 budget, text roles resolved for the pack, RTL-mirrored shapes |
| `compose/components.py` | masthead, eyebrow, hero, list rows, "+N MORE", footer, empty state |
| `compose/screen.py` | `compose_screen` (the ladder `plans_for`), `compose_identify`, `compose_setup_code`, `compose_blank` |
| `compose/preview.py` | PNG previews through `render/reference.py` |
| `compose/samples.py` | the multilingual sample set, per-face samples, the screen gallery, `write_samples` |
| `cli/preview.py` | `cremind tags tools preview text|card|screen|identify|samples` |

## Pipeline

`layout_text(text, fonts, width=, size_px=, language=, direction=, align=,
max_lines=, ellipsis=, line_spacing=, weight=, leading=, tracking=, grid_fit=)`
lays out plain text (`\n` separates paragraphs) in a box `width` pixels wide;
`measure_text` is its one-line width (it takes `weight`, `tracking` and
`grid_fit` too). `size_px` must be a text size the pack has:
`FontContext.text_sizes()` lists them (12, 14, 16, 24, 32 on the full pack;
16/24 on the dev pack; 16/24/32 on packs built before contract 0.3.0) and
`FontContext.nearest_size(want)` picks the largest available ≤ `want`, else
the smallest. Per paragraph:

1. **Normalise.** C0/C1 controls are removed (a tab becomes a space), then NFC
   with ICU's data (ICU 77 = Unicode 16; Python's `unicodedata` is older).
2. **Grapheme clusters** (UAX #29, ICU character break iterator). Nothing
   below — face choice, breaking, ellipsis — ever splits a cluster.
3. **Paragraph direction.** `direction="ltr"`/`"rtl"` wins. With `"auto"`:
   an RTL language hint (`ar`, `he`, `fa`, `ur`, `ps`, `yi`, `dv`, `ug`, `ckb`,
   `sd`, `syr`, `nqo`, … or any `-Arab` tag) makes the paragraph RTL when it
   contains any strong RTL character (so "Google Drive: تم الحفظ" stays RTL);
   otherwise the first strong character decides (UAX #9 P2/P3); a paragraph
   without strong characters follows the hint. Embedding levels come from
   `icu.Bidi` at that fixed paragraph level.
4. **Script itemisation.** Each cluster takes the script (ICU `Script`) of its
   first character that is not Common/Inherited. Common and Inherited clusters
   take the preceding script (the following one at the paragraph start); an
   opening paired bracket remembers the script it was opened in and its
   closing bracket gets the same (UAX #24-style, 64-deep stack).
5. **Font fallback per cluster** — see "Font selection" below — then the
   **weight**: with `weight="bold"` a cluster is drawn with its face's bold
   sibling (Noto Sans Bold, face 172, for Noto Sans) when the pack has that
   strike and the sibling maps every character the cluster needs; any other
   cluster (Arabic, Han, emoji, …) and every cluster on a pack without a bold
   face stays regular, silently.
6. **Shaping** with uharfbuzz: every maximal run of clusters with the same
   (bidi level, face, script) is shaped with the **whole paragraph as
   context** (`add_codepoints(text, offset, length)`), direction from the
   level, script and language set explicitly, default features, `BOT`/`EOT`
   flags at the text ends. The font is the exact cached file the pack was
   rasterised from (glyph ids match the pack 1:1) with the face's variation
   coordinates (Noto Emoji `wght` 400), scaled to 26.6 at the strike size:
   `scale = (size·64, size·64)`, `ppem = (size, size)`. Each cluster's advance
   is the sum of its glyphs' `x_advance`.
7. **Line breaking.** Break opportunities from ICU's line break iterator for
   the language: dictionaries for Thai, Lao, Khmer and Myanmar (under any
   language hint), CJK rules, spaces and hyphens. Chinese and Japanese use
   `lb=strict` (no line starts with `ー`, small kana, `。`, `、`); Han text
   under a non-CJK hint is broken as `zh`. Greedy fitting on the cluster
   advances: the longest line whose width **without trailing spaces** fits;
   trailing spaces hang (not counted, not drawn) and a new line never starts
   with a space after a soft break. A word wider than the box is broken
   between grapheme clusters.
8. **Line-boundary reshaping.** Every line is shaped again with **only the
   line as context**, so joining, ligatures and kerning are recomputed at the
   break (a joining Arabic word broken across lines ends in a final form and
   restarts with an initial form). If the reshaped line no longer fits, it is
   broken at the previous opportunity (or cluster) and reshaped again.
9. **Max lines and ellipsis.** When `max_lines` cuts the text (in this
   paragraph or a later one) the last line keeps its natural content minus
   trailing spaces plus U+2026 `…` in whichever face covers it (face choice as
   for any Common character, so the run's face when it has one; `...` when no
   face has U+2026); clusters are dropped from the end, re-shaping each time,
   until it fits. The ellipsis is resolved by bidi with the line (at the
   logical end: the right end of an LTR paragraph, the left end of an RTL one).
10. **Visual order.** Line levels from `Bidi.setLine` (rule L1), then rule L2
    over the line's runs; HarfBuzz already returns each RTL run's glyphs left
    to right.
11. **Positions.** Left-to-right runs in a face whose regular face declares
    only Latin, Greek and Cyrillic (Noto Sans and Noto Sans Bold) are
    **grid-fitted** (`grid_fit=True`, the default): each spacing glyph
    advances by the **hinted advance stored in the pack** (what FreeType's
    autohinter gave the bitmap) plus HarfBuzz's kerning — its `x_advance`
    minus the glyph's nominal advance — rounded to whole pixels, so every
    position is an exact pixel and the same pair of letters always gets the
    same gap (on unhinted 26.6 positions a third to a half of the steps at
    12–16 px were a pixel off, and the same pair could get different gaps). A
    mark keeps HarfBuzz's offset from its base glyph's pen position, rounded
    once, so a combining sequence looks the same wherever it sits.
    `tracking` (0–8 px) is added between clusters of these runs, never after
    a line's last, and turns the `liga`/`clig`/`dlig` ligatures off ("fi" stays
    two letters); line fitting, placement and the ellipsis all use the same
    advances, and a width from `measure_text` fits the text exactly. Every
    other run — other scripts, right-to-left runs, and everything with
    `grid_fit=False` — keeps HarfBuzz's positions: the pen accumulates
    `x_advance`s in 26.6 and each glyph origin is `pen + x_offset` (and
    `baseline − y_offset`) rounded half up to whole pixels, `(v + 32) >> 6`.
    Nothing accumulates rounded values, so there is no drift
    (`test_positions_accumulate_in_26_6`); tracking does not apply there.
12. **Alignment.** `start`/`end` follow the paragraph direction (start = left
    in LTR, right in RTL), `center`, or absolute `left`/`right`; the line's
    left edge is a whole pixel.
13. **Line boxes.** Every line starts from an ascent and descent set by
    `leading` (`FontContext.line_box`) and grows to the ink of its glyphs
    (bitmap extents from the pack), so Arabic, Thai, Tibetan or stacked
    Vietnamese lines are taller only when their marks need it and lines never
    overlap. Lines stack with no gap (`line_spacing` adds one).
    - `"font"` (default): the base face's strike metrics (Noto Sans, face 1:
      13/4 px at 12 px, 15/5 at 14, 18/5 at 16, 26/8 at 24, 35/10 at 32).
    - `"tight"`: from Noto Sans's ink — one pixel above the tallest of
      `bdfhklHT0([` and down to the deepest of `gjpqy(),;[`, each capped at
      the strike value: 11/3 at 12 px, 12/4 at 14, 13/4 at 16, 19/6 at 24,
      26/8 at 32. No ASCII glyph of Noto Sans or its Bold reaches outside it,
      so an ASCII paragraph steps by exactly ascent + descent; accented
      capitals (Vietnamese `Ở` and `Ẫ` reach 13 px at 12 px) grow their line.
    - `(ascent, descent)`: used as given (still grown to the ink).

Glyphs without ink (spaces, zero-width marks) only move the pen and are not
emitted, which saves glyph budget. A `TextBlock` holds the normalised `text`,
`lines` (`LineBox`: code-point range, `x`, `width`, `top`, `baseline`,
`bottom`, `rtl`, `ellipsis`), `glyphs` (`PositionedGlyph`: `face_id`,
`size_px`, `glyph_id`, `x`, `y` = baseline origin, `cluster`, `line`),
`height`, `truncated`, `unsupported` (characters no face maps),
`unsupported_clusters` (clusters no single face maps completely), `notdef`
(glyph id 0 count on the visible lines) and `shaped`. Results are cached
(1024 entries, LRU) — `TextBlock` is immutable.

### Font selection

For each grapheme cluster, among faces that have a strike at the requested
size (the dev pack has no 32 px):

1. **Emoji presentation** — VS16, a keycap, a ZWJ sequence, a skin-tone
   modifier, a flag or an `Emoji_Presentation` base — picks the emoji face
   (171) when it maps the cluster. VS15 asks for text presentation; other
   `Extended_Pictographic` characters default to text.
2. The faces that **declare the cluster's resolved script**, ordered by
   `fonts.coverage.candidate_faces` (longest BCP-47 language match, then
   primary, CJK region, supplement, optional; Han without a matching language
   → `zh-Hans`, Noto Sans SC). The first that maps every character that needs
   a glyph wins. For Han, Hiragana, Katakana, Hangul and Bopomofo the language
   is the hint when it is Chinese/Japanese/Korean, else inferred from the
   paragraph: kana → `ja`, hangul → `ko` ("直す" under an `en` hint uses the JP
   face).
3. The **previous cluster's face** when it maps the cluster (digits and
   punctuation stay in the run's font; never the emoji face for non-emoji).
4. **Any face mapping the whole cluster**, by language match, Noto Sans
   first, then role (primary, CJK region, supplement, optional, emoji last
   unless emoji presentation was asked) and face id.
5. Otherwise the cluster is **unsupported**: drawn with the face that maps its
   base character (else the previous / base face), whose `.notdef` box shows
   the gap; the characters no face maps are reported in `unsupported` and the
   cluster in `unsupported_clusters`.

Controls, format characters (except visible prepended concatenation marks),
separators and variation selectors need no glyph and never count against a
face (`fonts.coverage.needs_glyph`).

**Weight siblings never take part in fallback.** A face with role `weight`
(`FaceInfo.regular_face_id` set: Noto Sans Bold → Noto Sans) is not a
candidate in any step above, ranks last if it ever were, and stands for its
regular face when it is the previous cluster's face; the regular choice is made
first and `FontContext.styled` swaps in the sibling only for `weight="bold"`
(step 5 of the pipeline). `FontContext.has_weight("bold", size)` tells whether
the pack can draw Noto Sans bold at a size; `regular_of(face)` maps a sibling
back to its regular face.

### Legibility floors

`layout.legible_size(text)` is the smallest text size every script of a text
stays readable at on a 1-bit panel: the largest floor over the scripts of its
code points, judged from renders of the multilingual samples at 12, 14 and
16 px with the full pack.

| Floor | Scripts |
|---|---|
| 12 px | Latin (Vietnamese included), Greek, Cyrillic, Armenian, Georgian, Hebrew, Cherokee |
| 14 px | Arabic, Syriac, Thaana, N'Ko, Adlam, Hanifi Rohingya, Ethiopic, Canadian syllabics, Thai, Lao, Hiragana, Katakana, Hangul (dots, vowel marks and small strokes crowd at 12 px) |
| 16 px | Han, Bopomofo, Devanagari, Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada, Malayalam, Sinhala, Khmer, Myanmar, Tibetan, Mongolian and every other script (conjuncts, stacks and dense ideographs clot below) |

Common and Inherited characters (digits, punctuation, symbols, combining
marks, emoji) and unassigned or private-use ones have no floor of their own
(12 px). The engine never applies the floors: the composer takes the strike
`nearest_size` gives for the wanted size and, when that is below
`legible_size(text)` (localised dates and times included), the smallest
strike at or above the floor (`Canvas.size`). Emoji are legible only from
about 16 px (monochrome blobs at 12), which the floor deliberately ignores.

### GLYPHS commands

`glyph_commands(glyphs, color)` groups consecutive glyphs with the same face,
size and colour into one `GLYPHS` command: `origin` = the first glyph's origin
(its entry has `dx = dy = 0`), every later entry the i8 delta from the
previous origin. A run is split when a delta leaves −128…127 or at 255 glyphs.
Lines of a block are emitted alternately left-to-right and right-to-left
("serpentine"), so the step to the next line is a short diagonal instead of a
jump back across the box and a paragraph in one face usually needs one
command; glyphs of one colour never depend on drawing order.

### Plain text

`plain_text(text, keep_newlines=False)` (titles: one paragraph; bodies keep
single line breaks): HTML comments and `<script>`/`<style>` bodies are
dropped, `<br>` and block-closing tags become line breaks, other tags are
removed and entities decoded; Markdown fences and inline code keep their
text, links and images keep label / alt text, emphasis, headings, block
quotes, bullets, rules, table pipes and reference definitions go, backslash
escapes are resolved; controls go, runs of ASCII spaces collapse (no-break
and ideographic spaces stay), blank lines collapse, NFC.

## Screen model

`compose_screen(panel, cards, fonts, settings, now) -> ComposedScreen`
implements `compose.api.Composer` in the "Editorial" design: one bold title
is the focal point, everything else is small, and hierarchy comes from size
and weight rather than boxes. The logical canvas is the native panel turned
by `panel.rotation` (rotation 1 or 3 swaps the sides: the Hema's native
128×250 panel with rotation 3 is read 250×128); its short side picks the
**panel class**, and every size comes from that class's tokens
(`compose/tokens.py`) resolved against the pack (tested: 250×128 both ways,
250×122 portrait, 296×128, 264×176, 400×300 at all four rotations, 800×480).

The example cards (`compose.samples.example_cards`) on the Hema 2.13"
(250×128, black/white/red); y is the first pixel row of each part, of each
slot for the rows:

```
  y ┌──────────────────────────────────────────────────┐
  4 │ HEMA                         Updated Sun 2:05 PM │ masthead: name 12B caps, time 12R
 17 │ ══════════════ accent rule, 2 px ═══════════════ │ red
 22 │ ▐NEEDS YOU▌                              2:02 PM │ eyebrow: 13 px chip, label in white
 39 │ Approve deployment of                            │ hero title: 16B (24B when the whole
 59 │ release 2.4 to production?                       │   title fits two lines)
 77 │ ──────────────────────────────────────────────── │ hairline
 78 │ ■ Telegram channel stopped                Sep 26 │ rows, pitch 16: a 5 px square (red,
 94 │ □ Reply ready: Tóm tắt cuộc họ…          1:35 PM │   filled: alert), 12R title (bold:
110 │   +4 MORE                                        │   alert) and time; "+N MORE" last
    └──────────────────────────────────────────────────┘
```

and on 400×300, where all seven fit:

```
  y ┌──────────────────────────────────────────────────────────┐
  8 │ DESK                        Updated Sun, Sep 27, 2:05 PM │ masthead: 12B caps / 12R
 22 │ ══════════════════ accent rule, 2 px ═══════════════════ │
 29 │ [?] ▐NEEDS YOU▌                                  2:02 PM │ eyebrow: 16 px icon, 15 px chip
 50 │ Approve deployment of                                    │ hero title: 24B, ≤ 2 lines
 79 │ release 2.4 to production?                               │
109 │ ──────────────────────────────────────────────────────── │ hairline
110 │ [!] Telegram channel stopped                      Sep 26 │ rows, pitch 22: 16 px icon (red:
132 │ [▤] Reply ready: Tóm tắt cuộc họp sáng nay       1:35 PM │   alert), 14R title, 12R time
154 │ [▤] Weekly summary                               1:20 PM │
176 │ [♪] (an Arabic title, at its 14 px floor)       12:35 PM │
198 │ [▦] (a Japanese title, at its 16 px floor)       2:00 PM │
220 │ [↻] Nightly backup                                  7/12 │ progress: done/total
    └──────────────────────────────────────────────────────────┘
```

With excerpts and QR links on, the 400×300 hero title drops to 16B beside a
QR code at the end side and the body follows in 14R; the rows are the same.

**Panel classes and type scale** (B bold, R regular, caps = uppercased by ICU
in the UI language with 1 px letter-spacing; line pitch 16 px for 12 px text,
19 for 14, 20 for 16, 29 for 24, 38 for 32):

| | XS | S | M | L |
|---|---|---|---|---|
| short side | < 160 | 160–239 | 240–399 | ≥ 400 |
| panels | 2.13" 250×122/128, 2.9" 296×128 | 2.7" 264×176, 1.54" 200×200 | 4.2" 400×300 | 7.5" 800×480 |
| margin | 6 | 8 | 12 | 16 |
| masthead: name / time / rule | 12B caps / 12R / 2 px | 12B caps / 12R / 2 px | 12B caps / 12R / 2 px | 14B caps / 14R / 3 px |
| eyebrow: icon, chip | none, 13 px | 16 px, 13 px | 16 px, 15 px | 24 px, 18 px (14B label) |
| hero title | 24B ≤ 2 lines if whole, else 16B ≤ 2 (portrait ≤ 6) | 24B ≤ 2 if whole, else 16B ≤ 3 | 24B ≤ 2 if whole, else 16B ≤ 3 | 32B ≤ 2 if whole, else 24B ≤ 3 |
| body | 12R, 1–2 lines (portrait 1–4) | 12R, 1–4 | 14R, 2–6 | 16R, 3–14 |
| row: title / time, pitch | 12R / 12R, 16 (portrait: no time, ≤ 2 lines) | 14R / 12R, 20 | 14R / 12R, 22 (portrait ≤ 2 lines) | 16R + a 12 px meta line, 44 |
| row marker | 5 px square | 16 px icon | 16 px icon | 16 px icon |
| rows at most | 4 (portrait 8) | 6 | 8 (portrait 12) | 12 |
| example cards shown | 3 of 7 (portrait 6) | 4 | 7 | 7 |

An L panel at least 600 wide and 1.4 times as wide as high (800×480) splits
into two columns: the hero in the start column (9/16 of the width), a 1 px
divider, the rows in the end column.

Sizes in the tokens are **wanted** sizes. `Canvas.resolve` draws a text at the
largest size ≤ the wanted one that the pack has (else its smallest), raised to
the text's [legibility floor](#legibility-floors) — card texts, the tag's
name and localised dates and times alike: an Arabic title stays at 14 px and
a Han one at 16 px, its row taking as many slots as its ink needs, while Latin
rows on XS use 12 px. Bold is used only where the pack has Noto Sans Bold at
that size (below 14 px not for text with stacked marks, "Known limitations"),
letter-spacing only for Latin, Greek and Cyrillic. On a pack without 12/14 px
or bold text (packs built before contract 0.3.0, the dev pack) the same design
comes out at 16 px regular ("Known limitations").

- **Order**: displayable cards (`resolved` and `clear` are instructions, never
  shown) by priority descending, then newest `created_at`, then highest
  delivery id. The first is the **hero**, the next ones are list rows — as
  many as the panel holds — and "+N MORE" counts the rest.
- **Masthead**: the tag's name, uppercased in the UI language ("TAG 1A2B3C4D"
  without a name), at the start side; "Updated {time}" at the end side, the
  first of `MMMEdjm` ("Sun, Sep 27, 2:05 PM"), `Ejm` ("Sun 2:05 PM"), `jm`
  with "Updated", then the bare time, that leaves the name whole (XS starts at
  `Ejm`). The time is in `settings.timezone` and `settings.language`
  ("Updated 14:05 CN"; an unknown time zone falls back to UTC) and is when the
  screen was composed: a screen is composed again when its cards change, not
  when the clock moves. The accent rule runs under the real ink of both. XS
  panels on a pack without 12 px text have no masthead: a footer row under a
  hairline at the bottom carries the time ("Updated 2:05 PM", else "2:05 PM")
  and "+N MORE" (else "+N").
- **Eyebrow**, over the hero title: the card's icon (S/M/L), its chip or
  label, and the card time at the end side ("2:02 PM" today; "Sep 26, 1:35 PM",
  else "Sep 26", on another day; dropped when it would come within 8 px of the
  label). The **tone** decides the chip: **alert** (`needs_input`, or severity
  `error`) a filled accent chip with the label knocked out in white;
  **caution** (`warning`, `attention`) a 1 px black outline chip; anything
  else the bold label alone. Labels (`compose/strings.py`): NEEDS YOU
  (`needs_input`); FAILED (an error of `task_outcome`, `automation`,
  `progress`) or ERROR; WARNING; IMPORTANT (`attention`); DONE (`success`);
  else the kind's word: NOTICE (`notification`), REPLY (`excerpt`), UPDATE
  (`task_outcome`, unknown kinds), CALENDAR, AUTOMATION, USAGE, RUNNING
  (`progress`), NOTE (`pinned_note`), HEALTH, FILES (`indexing_problem`), TAG
  (`tag_diagnostics`).
- **Icon** (eyebrow and rows): `card.icon` when it names a built-in icon, else
  the kind's default (notification → notifications, task_outcome → task,
  needs_input → help, excerpt → chat, progress → sync, health → warning,
  indexing_problem → folder, calendar → event, automation → schedule, usage →
  bar_chart, pinned_note → push_pin, tag_diagnostics → battery_low; unknown
  kinds by severity, else info).
- **Hero**: the title in bold, in the card's `lang` (else the profile
  language), on the first rung of the class's title ladder that leaves room
  for the rows the class aims at (2 on XS, 3 on XS portrait and S, 4 on M, 5
  on L) — the larger size only when the whole title fits its lines; a
  `PROGRESS` bar and a
  localised "7/12" for `progress` cards with real counts (integers,
  `done ≥ 0`, `total > 0`, `done` clamped; totals above 65535 are scaled into
  u16); the body; a QR code. A lone card is centred in the white under the
  rule.
- **Body**: only when `settings.show_excerpts` is on and the kind is one whose
  body Cremind sanitises (`excerpt`, `needs_input`, `task_outcome`,
  `notification`, `health`, `indexing_problem`, `calendar`, `automation`,
  `usage`, `tag_diagnostics`, `progress`); a `pinned_note` body is text the
  owner wrote for this tag and is always shown. Unknown kinds never show a
  body. The body gets the height and the budget left after everything else.
- **QR**: only with `settings.qr_links` and a link that is a short, token-free
  `https` URL: printable ASCII, ≤ `LAYOUT_QR_MAX_TEXT` bytes, a host, no user
  info, no query (not even `?`), no `=`, `&` or `;`, no 24+-character
  `[A-Za-z0-9_-]` run once UUID record ids are set aside, none of `token`,
  `secret`, `passw`, `apikey`, `api_key`, `api-key`, `access_key`,
  `signature`, `jwt`, `bearer`. ECC LOW, with the class's modules (XS 2 px,
  S and M 3/2, L 4/3) — the largest whose ink and 2-module quiet zone fit a
  third of the hero column and its height — at the end side of the hero, level
  with the eyebrow; the title and body wrap beside it.
- **Rows**: one card per slot of a fixed pitch: the marker (XS: a 5 px square,
  filled in the accent colour for an alert, filled black for a caution, hollow
  otherwise; S/M/L: the card's 16 px icon, in the accent colour for an alert),
  the title (bold for an alert) at the start side and the card time — or
  "7/12" — at the end side. XS portrait rows have no time. A row title that
  would show fewer than 8 characters takes a second line where the class
  allows one (XS and M portrait, where room left over also lets cut titles
  wrap, in display order), else waits under "+N MORE". L rows add a meta line
  under the title: the label (in a mini chip for alert and caution cards) and
  the time.
- **"+N MORE"** (12B caps) takes the last slot when cards are left over; N is
  `pending_count`.
- **Empty**: no displayable card → a check-circle icon over "No updates" in
  bold, centred under the masthead.
- **Colour**: everything is black except the **accent** — RED on
  black/white/red panels — for the masthead rule, alert chips (the L mini
  chip too), alert row markers and icons, and the identify frame: never text,
  never a 1 px line, never touching black (a white pixel between, diagonals
  included). On a black/white panel the composer emits BLACK for the accent,
  so its layout is exactly the black/white/red one with RED drawn as BLACK
  (only the layout's uses-red flag differs), and every red signal keeps a
  shape twin: the filled chip, the filled square, the bold row title. On a
  calm screen (no alert) the one red is the rule.
- **Right-to-left profiles** (`settings.language` RTL) mirror the chrome: the
  name, icons, chips and markers on the right; times, "7/12", the QR code and
  "Updated …" on the left; a progress bar fills from the right (drawn as a
  frame and a fill RECT, since PROGRESS fills left to right). Every card text
  keeps its own paragraph direction (an Arabic title is right-aligned in an
  English UI; rows align to the UI's start side). The composer's own strings
  are English left-to-right paragraphs; times, dates and numbers are
  localised.

**Changing the look.** `COMPOSER_VERSION` (`compose/api.py`) names the design
the screens are drawn in. Bump it in the same commit as any change to how
screens look — layout, type, spacing, colour or wording, the tokens and
strings included: every tag then redraws once, by itself, after the upgrade
([runtime.md](runtime.md#5-from-a-job-to-a-screen) "Redraw once after a
design or font-pack change"); without it every tag keeps the old look until
its next card. What such an upgrade means for tags in use:
[upgrade-tags-screen-design.md](../upgrade-tags-screen-design.md).

### Delivery ids

`ComposedScreen.delivery_ids` lists **only the cards whose title is drawn**
(the hero first, then the rows; a cut row title counts when it shows at least
8 characters). Cards counted under "+N MORE" are in `pending_delivery_ids`
(`len == pending_count`) and are **not** part of the revision. Reason:
connector-api.md receipts every delivery a displayed revision includes as
`displayed`; a card only counted in "+4 MORE" has not been seen by anyone,
and a `needs_input` card reported displayed while hidden would mislead
Cremind. The pending cards stay active in the daemon's store and are shown
(and receipted) by a later revision when higher-priority cards resolve or
expire — or they end as `expired`/`cancelled` without ever being reported
displayed. The daemon must therefore not mark a pending card `superseded`
when a newer revision stops showing it: a newer revision keeps every
still-active card (docs/tags/connector-api.md "Screen model") either by
showing it or by counting it.

### Limits and degradation

A screen must stay within **`min(LAYOUT_HARD_MAX, LAYOUT_SERIAL_MAX)` =
4000 bytes** (DELIVER_LAYOUT's CBOR envelope has to fit one 4 KiB serial
frame; bridges accept up to 4096), `LAYOUT_MAX_GLYPHS` (512) glyphs and
`LAYOUT_MAX_COMMANDS` (256) commands, and within the render-cost bounds of
protocol.md §4.3 (`LAYOUT_MAX_QR` = 4 QR codes, `LAYOUT_MAX_LINE_STEPS` =
16384 line steps, line endpoints in `[−W, 2W) × [−H, 2H)`). The composer
cannot come near the last three: it draws at most one QR code and no `LINE`
(rules, hairlines, the divider, chips, markers and frames are RECTs inside the
canvas); its budget (`canvas.Limits`, read from `compose.screen`'s module
globals on every call) still counts QR codes and line steps, so a future part
that drew more would degrade like any other overflow
(`test_render_cost_bounds_on_the_largest_canvases` composes the worst cards
on 800×480, 2048×2048 and 480×2048; `test_composer_budget_counts_render_cost`
drops the QR code when the QR budget is 0).

Within a plan the masthead, the hero's fixed parts (eyebrow, title, progress,
QR) and the rows are committed first; the body is laid out last with as many
lines as fit the height **and** the budget left (down to none). When a fixed
part does not fit, the composer walks its class's ladder (`plans_for(tokens)`)
and takes the first plan that fits:

1. the full design: up to the class's most rows, with the ornaments (chip
   corners cut, the eyebrow icon, separators between L rows);
2. the same without ornaments;
3. one row fewer at a time, down to the class's floor (XS and S 1, M 2, L 3);
4. from the floor down to no rows, without reserving body lines;
5. no rows, the title on its smaller rung, the masthead with the bare time;
6. the same with the title on at most 2 lines;
7. the smaller title on one line without masthead, body or QR — always fits.

A plan that must fail the way the last one did (the same hero after the hero
did not fit; only more rows than fitted) is skipped. Every step is a pure
function of the inputs (`now` included): the same cards give the same bytes
(`test_same_input_same_bytes`, `test_plans_degrade_deterministically`).

Measured with the full pack: every gallery screen uses the full design. So do
the worst cases — 20 cards with 400-character titles and 1200-character
bodies, excerpts and QR on, in the 13 scripts and stress texts of the tests —
on every XS and S panel (at most 1606 bytes and 260 glyphs) and on 400×300,
where the largest is a title alternating six scripts per character (3783
bytes and 224 commands: a script and face change at every character, so a
GLYPHS command per glyph) and the densest a run of narrow letters (510
glyphs). The glyph budget binds on the panels with room for more: on 300×400
the narrow text steps down to 4 rows, on 800×480 the alternating, Arabic and
Thai texts to 7 rows and the narrow one to 6, while the other scripts fill up
to all 512 glyphs with the full design. The ladder is tested by tightening
each limit to what a later plan uses (`test_degradation_ladder_under_tight_limits`).

`ComposedScreen.unsupported_chars` lists the characters of the **drawn**
texts that no face maps.

### Identify, setup code and blank

- `compose_identify(panel, fonts, tag_id=None)`: a frame round the whole
  screen in the accent colour (3 px on XS and S, 4 on M, 6 on L), "CREMIND
  TAG" (12B caps, 14B on L), the tag id (`panel.tag_id` as 8 hex digits, or
  the string given) in bold at the largest size up to 32 px the pack has (24
  px regular on the dev pack) and the tag's name (16 px on XS and S, 24 px
  on M and L; 2 lines, else 1, else left out when it does not fit the
  frame), centred. The daemon delivers it as a new revision for the
  `identify` command; `delivery_ids` is empty.
- `compose_setup_code(panel, fonts, code, qr_text)`: a removed tag's last
  screen ([connect-setup.md](../../app/tags/runtime/protocol/pinned/docs/connect-setup.md)
  §5.1 `RELEASE`): the fresh setup code as a QR code (`qr_text`, the `CTAG:`
  text; ECC LOW, the largest module of 6…1 px that fits) and as text, so the
  next owner can add the tag (its printed label no longer works). Landscape
  panels put the QR code at the start side and, beside it, "ADD THIS TAG", the
  code in bold two groups per line ("4ED6Q-W6H00" / "0G40R-40M30" / "E2091")
  and where to type it ("Cremind › Settings › Tags › Add tag", 12 px, up to 2
  lines); portrait panels put the QR code on top. When that does not fit, the
  hint loses lines, then the label goes, then the code gets smaller (down to
  the pack's smallest size), then the QR code's modules. It fits every tested
  panel down to 250×128 and 128×250, on the dev pack too.
- `compose_blank(panel)`: an all-white screen with no commands (12 bytes), for
  `clear` jobs and ownership changes.

## Previews

`compose.preview.preview_png(screen, panel, fonts, scale=1)` renders the
layout through `render/reference.py` — the normative renderer, the same pack,
the same plane encoding a bridge produces — and decodes the planes back to a
picture: a true 1-bit PNG for black/white panels, a 2-bit palette PNG (white,
black, red) for black/white/red panels, turned back to the **logical**
orientation (`orientation="native"` keeps panel rows). A scaled image over the
limit falls back to scale 1; over `MAX_PREVIEW_BYTES` (64 KiB, the connector's
limit) it raises `PreviewTooLarge`. Measured at scale 1, worst cases
included: 1–2 KiB on the Hema, 2–5 KiB on 400×300, up to 12 KiB on 800×480.
`render_image` / `render_png` take any layout (bytes or
`protocol.layout.Layout`), `panel=None` for a free-standing canvas, and keep
the scale asked for (the sample gallery uses them).

The Web UI shows these PNGs (one pixel per panel pixel, in the orientation the
tag is read) at the largest **whole-number zoom** that fits, up to 4×,
nearest-neighbour, so every panel pixel becomes the same square and the 1 px
stems of 12 px text survive (a fractional scale drops or doubles some); only
a thumbnail that does not fit at 1× is scaled down, smoothly. Clicking a
preview opens it at the largest whole zoom the window holds (`previewZoom` in
`ui/src/utils/tagsFormat.ts`, `TagPreviewImage.vue`).

## API for the daemon

| Call | Cost (full pack, this PC) |
|---|---|
| `FontSet.load(pack, cache)` | ≈ 0.4 s warm, ≈ 2 s cold (46 MB pack, SHA-256 of every font) — once |
| first `compose_screen` on a FontSet | ≈ 1 s: pack parse, 171 cmaps, HarfBuzz faces as used (cached per FontSet, weakly) |
| `compose_screen` after warm-up | ≈ 15–20 ms with every text shaped again, ≈ 5 ms when the texts are cached (400×300, the example cards); worst cases ≤ 120 ms |
| `compose_identify`, `compose_setup_code`, `compose_blank` | < 5 ms / < 5 ms / < 1 ms |
| `preview_png` | 10–50 ms at scale 1 |

`ComposedScreen.layout` is already validated (§4.3 structure,
`check_strikes` against the pack, `check_panel` for the rotation). The
composer never mutates its inputs and is thread-safe (break iterators per
thread, formatter and cache locks).

## `cremind tags tools preview`

```sh
cremind tags tools preview text "Tiếng Việt مرحبا 123 שלום" --size 24 --width 300 --lang vi --out t.png --scale 2
cremind tags tools preview text "NEEDS YOU" --size 12 --weight bold --tracking 1 --leading tight --out chip.png
cremind tags tools preview card job.json --panel bwr --out card.png            # one job or bare card
cremind tags tools preview screen cards.json --panel bwr --rotation 1 --out s.png   # [jobs] or {jobs|cards, settings}
cremind tags tools preview screen cards.json --panel hema213 --scale 2 --out hema.png  # the Hema 2.13": 250×128
cremind tags tools preview identify --tag-id 1A2B3C4D --name Desk --out id.png
cremind tags tools preview samples --out dist/tag-design/samples [--scale 2]
```

`--pack` defaults to `<repo>/fonts/out/full/fontpack.ctfp` (else the dev
pack), `--cache` to the font cache. `card`, `screen` and `identify` take the
panel: `--panel bw` or `bwr` (black/white, black/white/red) is 400×300
unturned unless `--width`, `--height` (native pixels) and `--rotation`
(quarter turns, 0–3) say otherwise; any hardware panel name or number of
`enroll/hardware.py` (`hema213`, `uc8176_bwr`, `ssd1619_213_bwr`, …) brings
its native size, colour planes and the rotation a new tag of it gets, each
still overridable (`--panel hema213 --rotation 0` previews the Hema
portrait). `text` takes the engine's typography:
`--size` (a text size the pack has), `--weight regular|bold` (it notes when
the pack has no bold face at that size), `--leading font|tight|ASCENT,DESCENT`,
`--tracking 0..8` and `--no-grid-fit` (HarfBuzz's unhinted positions, to
compare). It prints every line (range, x,
width, baseline, direction), the faces used and unsupported characters;
`card`/`screen` print the layout size, glyph and command counts, shown and
pending delivery ids. `screen` files take the connector job shape (or bare
cards) and optional `settings` (`language`, `timezone`, `show_excerpts`,
`qr_links`); `--lang`, `--tz`, `--excerpts/--no-excerpts`, `--qr/--no-qr`,
`--now` override.

### Sample gallery

`preview samples --out DIR` writes:

- `multilingual-NN.png` — the hand-written set (`compose.samples.MULTILINGUAL`):
  English, Vietnamese, French, German, Polish, Turkish, Greek, Russian,
  Ukrainian, Arabic, Persian, Urdu, Hebrew, Thai, Lao, Khmer, Myanmar, Hindi,
  Bengali, Tamil, Telugu, Kannada, Malayalam, Gujarati, Punjabi, Odia,
  Sinhala, Tibetan, Georgian, Armenian, Amharic, Chinese (SC/TC/HK), Japanese,
  Korean, Mongolian, Cherokee, emoji, mixed directions, combining marks;
- `faces-NN.png` — one automatic sample per text face (see below);
- `screen-<name>.png` — the screen gallery (`compose.samples.example_screens`
  and `special_screens`, composed at 07:05 UTC on Sunday 27 September 2026:
  2:05 PM in Ho Chi Minh City, the time zone of most of them), rendered at
  `--scale` whatever its size:
  - 400×300: `landscape-bw`, `landscape-bwr-excerpts-qr`, `portrait-bwr`
    (Vietnamese UI), `landscape-arabic-ui`, `portrait-progress` (German, one
    progress card), `empty`, `landscape-bwr-dense` (15 cards);
  - the Hema 2.13" (250×128): `hema-bwr`, `hema-bw`, `hema-portrait-bwr`
    (128×250), `hema-test-card` (the "Hello from Cremind" note *Send test*
    pins on a new tag), `hema-error` (a failed automation on top),
    `hema-dense`, `hema-vietnamese`;
  - the other classes: `s-264x176-bwr`, `small-296x128-bw`,
    `large-800x480-bwr` (two columns, excerpts and a QR code);
  - no cards: `identify` (400×300), `identify-hema`, `setup-code-hema`,
    `setup-code-landscape` (400×300);
- `summary.json` — per sample: faces used, unsupported characters, `.notdef`
  count; per screen: the panel (native size, rotation, planes, logical size,
  class), bytes, commands, glyphs, the text sizes and faces used, whether it
  uses red, shown delivery ids, the pending count, unsupported characters and
  the PNG's size.

It exits 1 when a face's sample is not drawn by that face, is unsupported or
has `.notdef`. Nothing of the gallery is committed (packs built on other
systems differ by a few pixels): render it where you need it (`dist/` is
ignored).

## Tests

`tests/tags/runtime/layout` and `tests/tags/runtime/compose` (tests that need the
packs are marked `fonts` and skip when `fonts/out/<profile>` or
`fonts/cache` is missing):

- plain text, ICU helpers (graphemes, Thai/Lao dictionary breaks, CJK strict
  breaks, paragraph direction, L1/L2, emoji properties);
- engine: Arabic contextual forms, lam-alef, RTL reordering of Arabic + Latin
  + digits, Hebrew, mixed directions on one line, Thai dictionary line breaks
  at word boundaries, CJK kinsoku, Han regional faces by language (and inferred
  from kana/hangul, with differing bitmaps), line-boundary reshaping, trailing
  spaces, long words, Vietnamese NFC = NFD and stacked marks, Devanagari /
  Bengali / Tamil conjuncts and reordering, emoji presentation, unsupported
  characters and clusters, ellipsis (LTR at the right, RTL at the left), max
  lines across paragraphs, alignment, 26.6 rounding without drift, line boxes
  grown to the ink, determinism (every weight, leading and tracking), the dev
  pack;
- spacing (`test_spacing.py`): Latin steps = the pack's hinted advance plus
  HarfBuzz's kerning rounded per pair at every size and weight, one gap per
  letter pair over a long text, no overlapping ink in tight pairs, marks at
  the same offset from their base wherever they sit, Devanagari / Arabic /
  Thai / Hebrew positions unchanged, tight leading (exact pitch for ASCII,
  grown and never overlapping for stacked capitals), tracking only between
  clusters (exact `measure_text`, no ligatures), end/centre-aligned ink inside
  the box, bold on Noto Sans Bold for Latin/Greek/Cyrillic only and regular on
  a pack without it, cache keys; `test_legibility.py`: the floor table;
- GLYPHS: grouping, i8 overflow splits, 255-glyph runs, serpentine order,
  real blocks encoding and validating with `protocol/layout.py`;
- **every face** (acceptance, "cover every installed script with automated
  samples"): for each of the **170 text faces** of the full pack (and Noto
  Sans Bold, laid out bold) a sample is
  generated from the face's cmap ∩ the pinned `Scripts.txt` — letters of the
  face's first declared script (numbers and symbols for music, numerals,
  symbols and emoji faces) that the engine draws **with that face**, evenly
  spread over the repertoire, grouped in four-letter words — laid out and
  rendered: the face is used, nothing is unsupported, HarfBuzz returns no
  `.notdef`, the rendering has ink. The 41 multilingual samples are checked
  the same way (and never use the bold face);
- composer (`test_screen.py`): every panel and rotation validates, shown
  cards are a prefix of the display order with the rest pending, red only on
  black/white/red panels, bodies only with excerpts, QR rules, progress (u16
  scaling, filled from the right in an RTL UI), identify (accent frame, the id
  at the largest size ≤ 32 px) and blank, the setup code on seven panels from
  250×128 up on both packs (one QR code with the `CTAG:` text, the code
  drawn), the dev pack, worst cases (20 cards, 400-character titles and
  1200-character bodies in 13 scripts/styles incl. a title switching script
  every character, on seven panels from the Hema to 800×480) within 4000
  bytes / 512 glyphs / 256 commands, render cost on the largest canvases, the
  ladder under tightened limits (the composer takes exactly the later plan),
  determinism, a new minute changes the bytes, < 200 ms per screen;
- design rules (`test_design.py`, read back from the rendered layouts over
  the gallery's card sets, identify and the setup code, on one
  black/white/red panel per class and orientation, in English, with excerpts
  and QR, and in an Arabic UI): the black/white screen is the black/white/red
  one with RED drawn as BLACK; dark text and icons only over white, white text
  only inside a fill with a pixel to spare, red never next to black; an alert
  keeps a shape cue on black/white (a filled chip; a filled square on XS, a
  bold row title elsewhere) that a calm card lacks; a calm screen's only red
  is the masthead rule; no two texts, icons, QR codes or bars share a pixel,
  no dark text on a rule, marker or chip, the QR code keeps its quiet zone,
  all ink stays on the canvas; row titles show at least 8 characters or wait;
  the density targets of `design_checks.DENSITY` (example cards shown: 3 on
  the Hema, 7 on 400×300, …) on both packs; at most 35 % ink; a lone card
  centred; "+N MORE" in its caps; no text below its script's floor; no
  exception and only the pack's sizes on every class, LTR and RTL, on the dev
  pack and the pack under test; the Hema's masthead (or footer row) and a new
  minute changing its screen; the panel-class tokens;
- previews: 1-bit and 3-colour PNGs match the reference frame, logical
  orientation, plane polarity independence, ≤ 64 KiB;
- the CLI (hardware panels by name, the sample gallery).

`CREMIND_TAG_TEST_PACK=<asset dir>` (a directory with `fontpack.ctfp` and
`cache/`, e.g. a gateway computer's
`<system dir>/.tag-runtime/assets/fonts/<pack id>`) runs the composer tests
on that pack instead of the full one — how the fallbacks are checked against
an older pack a computer really has; a pack named that way that cannot be
loaded fails the run instead of skipping it.

## Known limitations

- **Wide glyphs cropped at 32 px**: the ten glyphs wider than 255 px at 32 px
  (U+FDFD ARABIC LIGATURE BISMILLAH and nine Dives Akuru stacked ligatures,
  [`fonts.md`](fonts.md)) are cropped to 255 columns in the pack; titles that
  use 32 px show them cut (24/16 px are complete).
- **Nastaliq off**: Urdu is drawn with Noto Sans Arabic (Naskh-style); Noto
  Nastaliq Urdu is an optional face outside the `full` pack.
- **Spacing** (resolved for Latin, Greek and Cyrillic): grid-fitted runs use
  the pack's hinted advances, so letter pairs get one fixed gap. Other scripts
  (and right-to-left runs) are still placed at HarfBuzz's unhinted 26.6
  positions while their bitmaps are auto-hinted 1-bpp, so their spacing can
  look uneven by a pixel; tracking does not apply to them. Hinted widths
  differ from HarfBuzz's: on composer-like text +1.4 % at 12 px, −0.2 % at
  14, −2.8 % at 16 (Regular), +6.4 % / +3.7 % / 0 % (Bold).
- **Bold Vietnamese at 12 px**: Noto Sans Bold's 12 px bitmap of `ắ` merges
  the acute into the breve (the font's hinting), so the composer draws no
  text with stacked marks bold below 14 px: such a 12 px label or row title
  falls back to regular (an alert row on XS keeps its filled square), and the
  masthead's name moves up to 14 px bold instead.
- **Unicode versions**: ICU 77 knows Unicode 16; characters new in the pinned
  Unicode 17 data are treated as Common (their face is still found by cmap).
- **No hyphenation or justification**; no vertical text (Mongolian is shown
  horizontally, as Noto Sans Mongolian draws it).
- **English chrome**: the labels (NEEDS YOU, FAILED, DONE, NOTICE, REPLY, …),
  "Updated …", "+N MORE", "No updates", "TAG …", "CREMIND TAG", "ADD THIS
  TAG" and the setup hint are English, all in `compose/strings.py` (times,
  dates and numbers are localised); in a right-to-left UI they stay
  left-to-right, so "Updated" stands left of an Arabic date.
- **The masthead's time is when the screen was composed**: a screen is
  composed again when its cards change, not when the clock does, so on a
  quiet tag "Updated 2:05 PM" can be hours old — it says how fresh the screen
  is, not what time it is.
- **Older font packs**: a pack without 12/14 px or bold text (one built
  before contract 0.3.0 — what a gateway computer draws with until it is
  prepared after an update — or the dev pack) draws the same design at 16 px
  regular: titles are cut sooner, an XS panel shows a footer row ("+4 MORE …
  Updated 2:05 PM") instead of the masthead, and with nothing bold an alert
  row on a black/white S, M or L panel stands out only by its icon.
- **Emoji are monochrome** (Noto Emoji); flags show as boxed letters, skin
  tones are not distinguished, and at 12 px they are blobs (the floors leave
  emoji out).
- **Coverage gaps** of the pack (CJK Extensions B–J, Cyrillic Extended-D, …,
  [`fonts.md`](fonts.md) "Coverage") show as `.notdef` boxes and are reported
  in `unsupported_chars`.
- Line heights follow the ink: a line with tall stacks (Tibetan, Myanmar,
  Arabic marks) is taller than its neighbours, and a list row whose ink (or
  legibility floor) needs it takes more than one slot of the grid.
