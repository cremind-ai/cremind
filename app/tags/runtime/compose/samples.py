"""Sample texts, sample pages and the screen gallery: the multilingual set, one sample per face, example screens.

`MULTILINGUAL` is a hand-written set covering the scripts people write cards
in (plus mixed directions, combining marks and emoji). `face_sample` builds a
sample for **any** face from its cmap and the pinned Unicode ``Scripts.txt``
(`app.tags.runtime.fonts.coverage.load_unicode`): letters of the face's first
declared script that the layout engine would draw **with that face** (so the
sample exercises the face, not a fallback), evenly spread over the script's
repertoire and grouped into four-letter "words"; faces without letters (music,
numerals, symbols, emoji) use their numbers and symbols. A weight sibling
(Noto Sans Bold) is sampled at its weight (``FaceSample.weight``), the only way
the engine draws it.

The gallery: `example_screens` (the card sets `example_cards` — also the
composer tests' fixture — and `gallery_cards`, on every panel class from the
2.13" Hema to 800x480) and `special_screens` (identify, the setup code).

`write_samples` renders all of it through the reference renderer into PNG
pages and screens at the scale asked for (the ``cremind tags tools preview
samples`` command).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.tags.runtime.fonts.coverage import UnicodeScripts, load_unicode, needs_glyph
from app.tags.runtime.fonts.fontset import FaceInfo, FontSet
from app.tags.runtime.layout.engine import TextBlock, layout_text
from app.tags.runtime.layout.fonts import WEIGHTS, FontContext
from app.tags.runtime.layout.unicode import emoji_presentation, general_category, script_of
from app.tags.runtime.protocol.ids import Color
from app.tags.runtime.protocol.layout import Command, Glyphs, Layout, Line, decode_layout

MULTILINGUAL: tuple[tuple[str, str, str], ...] = (
    ("English", "en", "The quick brown fox jumps over the lazy dog. Approve deployment?"),
    ("Vietnamese", "vi", "Tiếng Việt có dấu: Chào buổi sáng, hôm nay trời đẹp quá! Người, những, Ở, Ữ."),
    ("French", "fr", "Où est la bibliothèque ? Voilà, ça coûte 15 € — «déjà vu»."),
    ("German", "de", "Größenwahn: Fünf Bücher über Äpfel und Übermut."),
    ("Polish", "pl", "Zażółć gęślą jaźń."),
    ("Turkish", "tr", "İstanbul'da güzel bir gün; ığdır, şöyle, çiçek."),
    ("Greek", "el", "Γειά σου Κόσμε! Καλημέρα, ψυχή."),
    ("Russian", "ru", "Съешь же ещё этих мягких французских булок, да выпей чаю."),
    ("Ukrainian", "uk", "Привіт, світе! Ґанок, їжак, єнот."),
    ("Arabic", "ar", "السلام عليكم، كيف حالك اليوم؟ لا بأس ١٢٣"),
    ("Persian", "fa", "سلام دنیا! امروز هوا خوب است. ۱۴۰۵"),
    ("Urdu", "ur", "آپ کیسے ہیں؟ یہ اردو متن ہے۔"),
    ("Hebrew", "he", "שלום עולם! מה שלומך היום?"),
    ("Thai", "th", "สวัสดีครับ วันนี้อากาศดีมากไปเที่ยวกันไหม"),
    ("Lao", "lo", "ສະບາຍດີ ພາສາລາວ"),
    ("Khmer", "km", "សួស្តី ភាសាខ្មែរ"),
    ("Myanmar", "my", "မင်္ဂလာပါ မြန်မာဘာသာ"),
    ("Hindi", "hi", "नमस्ते दुनिया! क्षत्रिय स्त्री हिन्दी।"),
    ("Bengali", "bn", "হ্যালো বিশ্ব! ক্ষমা করবেন।"),
    ("Tamil", "ta", "வணக்கம் உலகம்! ஸ்ரீ க்ஷ கொ கோ"),
    ("Telugu", "te", "హలో ప్రపంచం"),
    ("Kannada", "kn", "ನಮಸ್ಕಾರ ಜಗತ್ತು"),
    ("Malayalam", "ml", "ഹലോ ലോകം"),
    ("Gujarati", "gu", "નમસ્તે દુનિયા"),
    ("Punjabi", "pa", "ਸਤ ਸ੍ਰੀ ਅਕਾਲ ਦੁਨੀਆ"),
    ("Odia", "or", "ନମସ୍କାର ଦୁନିଆ"),
    ("Sinhala", "si", "ආයුබෝවන් ලෝකය"),
    ("Tibetan", "bo", "བཀྲ་ཤིས་བདེ་ལེགས།"),
    ("Georgian", "ka", "გამარჯობა მსოფლიო"),
    ("Armenian", "hy", "Բարեւ աշխարհ"),
    ("Amharic", "am", "ሰላም ልዑል"),
    ("Chinese (Simplified)", "zh-Hans", "你好，世界！今天天气很好。直骨"),
    ("Chinese (Traditional)", "zh-TW", "你好，世界！今天天氣很好。直骨"),
    ("Chinese (Hong Kong)", "zh-HK", "你好，世界！今日天氣好好。直骨"),
    ("Japanese", "ja", "こんにちは世界！今日は良い天気ですね。東京タワー。直骨"),
    ("Korean", "ko", "안녕하세요 세계! 오늘 날씨가 좋네요."),
    ("Mongolian", "mn", "Сайн байна уу ᠮᠣᠩᠭᠣᠯ"),
    ("Cherokee", "chr", "ᏣᎳᎩ ᎦᏬᏂᎯᏍᏗ"),
    ("Emoji", "en", "Emoji 😀 👍🏽 ❤️ 🎉 🇻🇳 1️⃣ ☺ ☺️"),
    ("Mixed directions", "en", "Hello שלום 123 مرحبا world (עברית) 4.5%"),
    ("Combining marks", "vi", "x̂́ q̣̇ e̊ Å — Tiếng Việt"),
)
"""(label, BCP-47 language, text)."""

_PSEUDO_SCRIPTS = frozenset({"Hans", "Hant", "Jpan", "Kore", "Zsym", "Zsye", "Zmth", "Hrkt"})
_LETTER = ("Lu", "Ll", "Lt", "Lm", "Lo")
_OTHER = ("Nd", "Nl", "No", "So", "Sm", "Sk", "Sc", "Po", "Ps", "Pe", "Pd", "Pi", "Pf", "Pc")


@dataclass(frozen=True)
class FaceSample:
    face_id: int
    key: str
    script: str
    language: str
    text: str
    """Empty when no character of the face is drawn by it (every one is taken by a better face)."""
    weight: str = "regular"
    """The `layout_text` weight the sample is drawn at ("bold" for Noto Sans Bold)."""


def default_unicode() -> UnicodeScripts:
    """The pinned UCD (``Scripts.txt`` …) from the font cache (`cremind tags tools fonts fetch`)."""
    from app.tags.runtime.fonts.manifest import default_cache_dir, load_manifest

    version = load_manifest().unicode.version
    return _unicode(str(default_cache_dir() / "unicode" / version), version)


@lru_cache(maxsize=2)
def _unicode(directory: str, version: str) -> UnicodeScripts:
    return load_unicode(Path(directory), version)


def _script_for_choice(cp: int) -> str:
    s = script_of(cp)
    return "Zyyy" if s in ("Zinh", "Zzzz") else s


def face_sample(fonts: FontSet, face: FaceInfo, unicode: UnicodeScripts, *, count: int = 16,
                size_px: int = 24) -> FaceSample:
    """An automatic sample for ``face`` (module docstring)."""
    ctx = FontContext.for_fontset(fonts)
    cmap = ctx.index.cmaps[face.face_id]
    language = face.languages[0] if face.languages else ""
    scripts = [s for s in face.scripts if s not in _PSEUDO_SCRIPTS and s in unicode.scripts]
    if not scripts:
        scripts = ["Zyyy"]
    weight = next((name for name, value in WEIGHTS.items() if value == face.weight), "regular")

    def drawn_here(cp: int) -> bool:
        if not needs_glyph(chr(cp)):
            return False
        emoji = emoji_presentation([cp])
        fid, ok = ctx.choose([cp], _script_for_choice(cp), language, size_px, None, emoji)
        if face.regular_face_id is not None:  # a weight sibling draws what its regular face would, at its weight
            fid = ctx.styled(fid, face.weight, size_px, [cp])
        return ok and fid == face.face_id

    for script in scripts:
        repertoire = sorted(cmap & unicode.scripts.get(script, frozenset()))
        if script == "Zyyy" and face.role == "emoji":
            repertoire = sorted(cp for cp in cmap if emoji_presentation([cp]))
        for categories in (_LETTER, _OTHER):
            pool = [cp for cp in repertoire if general_category(cp) in categories and drawn_here(cp)]
            if pool:
                picks = [pool[i * len(pool) // min(count, len(pool))] for i in range(min(count, len(pool)))]
                words = ["".join(chr(cp) for cp in picks[i:i + 4]) for i in range(0, len(picks), 4)]
                return FaceSample(face.face_id, face.key, script, language, " ".join(words), weight)
    return FaceSample(face.face_id, face.key, scripts[0], language, "", weight)


def face_samples(fonts: FontSet, unicode: UnicodeScripts | None = None, **kw: int) -> list[FaceSample]:
    """`face_sample` for every text face of ``fonts``, by face id."""
    unicode = unicode or default_unicode()
    return [face_sample(fonts, f, unicode, **kw) for f in sorted(fonts.faces, key=lambda f: f.face_id)
            if f.path is not None]


# --------------------------------------------------------------------------- pages


@dataclass
class _Page:
    width: int
    commands: list[Command]
    y: int


def _pages(entries: Sequence[tuple[str, TextBlock]], fonts: FontSet, width: int, max_height: int) -> list[Layout]:
    """Stack (label, block) pairs into pages ``width`` wide, at most ``max_height`` tall."""
    pages: list[Layout] = []
    page = _Page(width, [], 8)

    def close() -> None:
        if page.commands:
            pages.append(Layout(width, page.y + 4, 0, Color.WHITE, tuple(page.commands)))

    for label, block in entries:
        head = layout_text(label, fonts, width=width - 16, size_px=16, language="en", max_lines=1)
        need = head.height + block.height + 12
        if page.commands and page.y + need > max_height:
            close()
            page = _Page(width, [], 8)
        page.commands.extend(head.commands(8, page.y, Color.RED))
        page.y += head.height + 2
        page.commands.extend(block.commands(8, page.y, Color.BLACK))
        page.y += block.height + 6
        page.commands.append(Line(8, page.y, width - 9, page.y, 1, Color.BLACK))
        page.y += 4
    close()
    return pages


def write_samples(fonts: FontSet, out: Path, *, scale: int = 1, width: int = 800, now: datetime | None = None,
                  progress: Callable[[str], None] = lambda _m: None) -> dict:
    """Render the sample set, every face's sample and the screen gallery into ``out``; returns a summary."""
    from app.tags.runtime.compose.preview import render_image

    out.mkdir(parents=True, exist_ok=True)
    size = max(s for s in FontContext.for_fontset(fonts).text_sizes() if s <= 24)
    summary: dict = {"pack_id": fonts.pack_id.hex(), "multilingual": [], "faces": [], "screens": []}

    # Multilingual set.
    entries = []
    for label, lang, text in MULTILINGUAL:
        block = layout_text(text, fonts, width=width - 16, size_px=size, language=lang)
        entries.append((f"{label} ({lang})", block))
        summary["multilingual"].append({"label": label, "language": lang, "lines": len(block.lines),
                                        "faces": list(block.faces), "unsupported": list(block.unsupported),
                                        "notdef": block.notdef})
    for n, page in enumerate(_pages(entries, fonts, width, 1600), start=1):
        path = out / f"multilingual-{n:02d}.png"
        render_image(page, fonts, scale=scale).save(path, optimize=True)
        progress(f"wrote {path}")

    # One sample per face.
    try:
        unicode = default_unicode()
    except (FileNotFoundError, ValueError) as exc:
        progress(f"skipping per-face samples: {exc}")
        unicode = None
    if unicode is not None:
        face_entries = []
        for sample in face_samples(fonts, unicode, size_px=size):
            info = fonts.face(sample.face_id)
            block = layout_text(sample.text or "(no characters drawn by this face)", fonts, width=width - 16,
                                size_px=size, language=sample.language, weight=sample.weight)  # type: ignore[arg-type]
            face_entries.append((f"{sample.face_id} {info.key} [{sample.script}]", block))
            summary["faces"].append({"face_id": sample.face_id, "key": sample.key, "script": sample.script,
                                     "weight": sample.weight, "text": sample.text, "faces": list(block.faces),
                                     "unsupported": list(block.unsupported), "notdef": block.notdef})
        for n, page in enumerate(_pages(face_entries, fonts, width, 1600), start=1):
            path = out / f"faces-{n:02d}.png"
            render_image(page, fonts, scale=scale).save(path, optimize=True)
            progress(f"wrote {path}")

    # The screen gallery: example screens, then identify and the setup code. render_png keeps the scale asked
    # for (the connector's preview_png would drop a large one to scale 1 above 64 KiB).
    from app.tags.runtime.compose.preview import render_png
    from app.tags.runtime.compose.screen import compose_identify, compose_screen, compose_setup_code
    from app.tags.runtime.protocol.ids import NodeRole
    from app.tags.runtime.secure.codes import SetupPayload

    now = now or datetime(2026, 9, 27, 7, 5, tzinfo=UTC)
    gallery = [(name, panel, compose_screen(panel, cards, fonts, settings, now))
               for name, panel, settings, cards in example_screens(now)]
    setup = SetupPayload(NodeRole.TAG, HEMA_ID, bytes(range(10)))
    for name, panel, what in special_screens():
        gallery.append((name, panel, compose_identify(panel, fonts) if what == "identify"
                        else compose_setup_code(panel, fonts, setup.code(), setup.qr_text())))
    for name, panel, screen in gallery:
        path = out / f"screen-{name}.png"
        path.write_bytes(render_png(screen.layout, fonts, panel=panel, scale=scale))
        summary["screens"].append(_screen_summary(name, panel, screen, path.stat().st_size))
        progress(f"wrote {path}")
    (out / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary


def _screen_summary(name: str, panel: Any, screen: Any, png: int) -> dict:
    """``summary.json``'s entry for one gallery screen: the panel, the layout's cost, the type it uses, red."""
    from app.tags.runtime.compose.screen import logical_size
    from app.tags.runtime.compose.tokens import panel_class

    layout = decode_layout(screen.layout)
    runs = [c for c in layout.commands if isinstance(c, Glyphs)]
    width, height = logical_size(panel)
    return {"name": name,
            "panel": {"width": panel.width, "height": panel.height, "rotation": panel.rotation,
                      "planes": panel.planes, "logical": f"{width}x{height}", "class": panel_class(width, height)},
            "bytes": len(screen.layout), "commands": len(layout.commands), "glyphs": sum(len(c.glyphs) for c in runs),
            "sizes": sorted({c.size_px for c in runs}), "faces": sorted({c.face for c in runs}),
            "uses_red": any(getattr(c, "color", None) == Color.RED for c in layout.commands),
            "delivery_ids": list(screen.delivery_ids), "pending": screen.pending_count,
            "unsupported": list(screen.unsupported_chars), "png": png}


def example_cards(now: datetime) -> list:
    """A realistic card set: needs-input with a link, progress, outcomes in several languages."""
    from app.tags.runtime.compose.api import ActiveCard

    def card(did: int, kind: str, title: str, prio: int, minutes: int, **extra: object) -> ActiveCard:
        ts = now - timedelta(minutes=minutes)
        body = {"v": 1, "kind": kind, "title": title, "lang": extra.pop("lang", "en"),
                "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **extra}
        return ActiveCard(did, kind, prio, ts, body)

    return [
        card(501, "needs_input", "Approve deployment of release 2.4 to production?", 90, 3, severity="attention",
             icon="approval", body="Chat: Release planning",
             link="https://cremind.example.com/#/alice/c/0f8c2b1e-5a3d-4b8e-9c21-7d9f0e1a2b3c"),
        card(502, "progress", "Nightly backup", 30, 10, icon="sync", progress={"done": 7, "total": 12},
             body="Running"),
        card(503, "task_outcome", "Reply ready: Tóm tắt cuộc họp sáng nay", 50, 30, severity="success", icon="chat",
             lang="vi"),
        card(504, "health", "Telegram channel stopped", 75, 60 * 20, severity="error", icon="error",
             body="Bot token revoked by the provider"),
        card(505, "notification", "رسالة جديدة من أحمد: هل أنت متاح غداً؟", 40, 90, lang="ar"),
        card(506, "calendar", "今日 10:00 定例ミーティング", 35, 5, lang="ja"),
        card(507, "excerpt", "Weekly summary", 50, 45, icon="chat",
             body="**Highlights**: shipped the *tag* companion; fixed 3 bugs in [the queue](https://x.example/q). "
                  "Next week: bridges, fonts, and the Vietnamese UI (Tiếng Việt)."),
    ]


def _card(now: datetime, did: int, kind: str, title: str, prio: int, minutes: int, **extra: object) -> Any:
    """An active card ``minutes`` old in the connector's card shape (``lang`` defaults to English)."""
    from app.tags.runtime.compose.api import ActiveCard

    ts = now - timedelta(minutes=minutes)
    body = {"v": 1, "kind": kind, "title": title, "lang": extra.pop("lang", "en"),
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), **extra}
    return ActiveCard(did, kind, prio, ts, body)


def gallery_cards(now: datetime) -> dict[str, list]:
    """The gallery's other card sets: a pinned test note, a dense list, a failure on top, Vietnamese titles."""
    dense = ["Build #482 passed", "New comment on Q4 roadmap", "Invoice INV-2291 paid", "Backup verified: 14 GB",
             "Lan shared 3 files with you", "Deploy window opens at 16:00", "Disk usage at 81% on lab-02",
             "Weekly report is ready"]
    return {
        "test-card": [_card(now, 601, "pinned_note", "Hello from Cremind", 55, 0, icon="check_circle",
                            body="This tag is set up and receiving updates.")],
        "dense": example_cards(now) + [_card(now, 801 + i, "notification", title, 40, 2 + 7 * i)
                                       for i, title in enumerate(dense)],
        "error": [
            _card(now, 701, "automation", "Morning briefing automation failed", 70, 4, severity="error",
                  icon="error", body="The model provider returned an error: rate limit reached."),
            _card(now, 702, "indexing_problem", "Document indexing: paused", 45, 25, severity="warning",
                  icon="folder", body="The Documents folder is not reachable."),
            _card(now, 703, "task_outcome", "Reply ready: Budget review", 50, 12, severity="success", icon="chat"),
            _card(now, 704, "calendar", "Next: 14:00 Product sync", 35, 1, icon="event"),
            _card(now, 705, "tag_diagnostics", "Battery low", 20, 60, severity="warning", icon="battery_low",
                  body="2410 mV"),
        ],
        "vietnamese": [
            _card(now, 901, "needs_input", "Duyệt triển khai bản 2.4 lên môi trường production?", 90, 3,
                  severity="attention", icon="approval", lang="vi", body="Trò chuyện: Kế hoạch phát hành"),
            _card(now, 902, "health", "Kênh Zalo bị ngắt kết nối", 75, 40, severity="warning", icon="link_off",
                  lang="vi"),
            _card(now, 903, "task_outcome", "Đã có trả lời: Tóm tắt cuộc họp sáng nay", 50, 20, severity="success",
                  icon="chat", lang="vi"),
            _card(now, 904, "notification", "Lan đã chia sẻ 3 tệp với bạn", 40, 55, lang="vi"),
            _card(now, 905, "calendar", "Tiếp theo: 14:00 Họp nhóm sản phẩm", 35, 2, icon="event", lang="vi"),
        ],
    }


HEMA_ID = 0xD1F06B9A
"""The 2.13" Hema tag of the gallery: native 128x250 black/white/red, rotation 3 (logical 250x128)."""


def example_screens(now: datetime) -> list:
    """(name, panel, settings, cards) of every example screen (``screen-<name>.png``)."""
    from app.tags.runtime.compose.api import ScreenSettings, TagPanel

    cards = example_cards(now)
    sets = gallery_cards(now)
    en = ScreenSettings(False, False, "Asia/Ho_Chi_Minh", "en")
    hema = TagPanel(HEMA_ID, 128, 250, 2, 3, 3, "Hema")
    desk_bwr = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 0, "Desk")
    return [
        ("landscape-bw", TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 0, "Desk"),
         ScreenSettings(False, False, "Asia/Ho_Chi_Minh", "en"), cards),
        ("landscape-bwr-excerpts-qr", TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 0, "Desk"),
         ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "en"), cards),
        ("portrait-bwr", TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 1, "Bếp"),
         ScreenSettings(True, False, "Asia/Ho_Chi_Minh", "vi"), cards),
        ("landscape-arabic-ui", TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 0, "المكتب"),
         ScreenSettings(True, True, "Asia/Riyadh", "ar"), cards),
        ("portrait-progress", TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 3, "Lab"),
         ScreenSettings(False, False, "Europe/Berlin", "de"), [c for c in cards if c.kind == "progress"]),
        ("empty", TagPanel(0x1A2B3C4D, 400, 300, 1, 1, 0, "Desk"), ScreenSettings(), []),
        ("hema-bwr", hema, en, cards),
        ("hema-bw", TagPanel(HEMA_ID, 128, 250, 1, 1, 3, "Hema"), en, cards),
        ("hema-portrait-bwr", TagPanel(HEMA_ID, 128, 250, 2, 3, 0, "Hema"), en, cards),
        ("hema-test-card", hema, en, sets["test-card"]),
        ("hema-error", hema, en, sets["error"]),
        ("hema-dense", hema, en, sets["dense"]),
        ("hema-vietnamese", hema, ScreenSettings(False, False, "Asia/Ho_Chi_Minh", "vi"), sets["vietnamese"]),
        ("s-264x176-bwr", TagPanel(0x3C4D5E6F, 264, 176, 2, 3, 0, "Door"), en, cards),
        ("landscape-bwr-dense", desk_bwr, en, sets["dense"]),
        ("small-296x128-bw", TagPanel(0x2B3C4D5E, 296, 128, 1, 1, 0, "Shelf"), en, cards),
        ("large-800x480-bwr", TagPanel(0x4D5E6F70, 800, 480, 2, 3, 0, "Wall"),
         ScreenSettings(True, True, "Asia/Ho_Chi_Minh", "en"), cards),
    ]


def special_screens() -> list:
    """(name, panel, ``identify`` | ``setup-code``) of the gallery's screens that show no cards."""
    from app.tags.runtime.compose.api import TagPanel

    hema = TagPanel(HEMA_ID, 128, 250, 2, 3, 3, "Hema")
    desk = TagPanel(0x1A2B3C4D, 400, 300, 2, 3, 0, "Desk")
    return [("identify", desk, "identify"), ("identify-hema", hema, "identify"),
            ("setup-code-hema", hema, "setup-code"), ("setup-code-landscape", desk, "setup-code")]
