"""Turning an image (or a scanned page) into searchable text.

A caption is the only way a photo becomes findable by what it shows ("two
puppies in a park"), so the prompt asks for *structure*, not prose: objects
with counts, animals with counts, people count, the kind of place, any visible
text, and tags. That JSON is kept (it answers "how many dogs") and rendered
into one caption chunk together with what EXIF already knows (when and with
which camera the photo was taken), so a single chunk carries both halves of a
question like "photos of my dog I took last summer".

Everything here is synchronous from the caller's point of view: it runs on a
pipeline worker thread, and the async LLM client is driven by a private event
loop per call (never the server's loop, which must not wait on a vision API).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import re
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any

from app.utils.logger import logger

PROMPT_VERSION = 1
# Long side of what is sent. Enough for counting animals and reading a sign;
# small enough to bound the per-image cost and upload time.
CAPTION_MAX_SIDE = 1024
OCR_MAX_SIDE = 1600

# Path segments that mark UI assets rather than photographs.
_ASSET_DIRS = {"icons", "icon", "assets", "sprites", "favicon", "favicons", "emoji", "emojis"}

_CAPTION_SYSTEM = (
    "You describe images so they can be found later by search. Answer with ONE JSON "
    "object and nothing else, in this shape:\n"
    '{"kind": "photo|screenshot|scan|document|diagram|chart|graphic|other",\n'
    ' "summary": "one or two plain sentences describing what is shown",\n'
    ' "objects": [{"label": "dog", "count": 2}],\n'
    ' "animals": [{"species": "dog", "count": 2}],\n'
    ' "people_count": 0,\n'
    ' "scene": {"setting": "outdoor|indoor|unknown", "place_type": "park"},\n'
    ' "text": "any text visible in the image, verbatim, max 500 characters",\n'
    ' "tags": ["up to 12 short English search tags"],\n'
    ' "tags_local": ["the same tags in {lang}, or [] if none"]}\n'
    "Count carefully. Never identify real people by name. If unsure, say so in the summary."
)

_OCR_SYSTEM = (
    "You transcribe scanned document pages. Return the page's text exactly as written, "
    "in reading order, preserving headings, numbered articles and clauses, and line "
    "breaks between paragraphs. Do not translate, summarise or add anything. If the "
    "page has no text, return an empty string."
)


@dataclass
class CaptionResult:
    text: str
    data: dict[str, Any] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    provider: str | None = None
    model: str | None = None


def eligible(
    *, width: int | None, height: int | None, size_bytes: int, rel_path: str, options: dict[str, Any],
) -> tuple[bool, str | None]:
    """Whether an image is worth a vision call: not an icon, not tiny."""
    cap = (options or {}).get("caption") or {}
    if not cap.get("enabled", True):
        return False, "captions_off"
    name = rel_path.rsplit("/", 1)[-1].lower()
    if name.endswith(".ico") or name.startswith("favicon"):
        return False, "skipped_small"
    parts = {p.lower() for p in rel_path.split("/")[:-1]}
    if parts & _ASSET_DIRS:
        return False, "skipped_small"
    if size_bytes < int(cap.get("min_kb", 20)) * 1024:
        return False, "skipped_small"
    min_px = int(cap.get("min_px", 256))
    if width and height and min(int(width), int(height)) < min_px:
        return False, "skipped_small"
    return True, None


def daily_cap(options: dict[str, Any]) -> int:
    """Images + scanned pages this profile may send per day: its own
    ``caption.daily_cap``, else the admin default."""
    cap = (options.get("caption") or {}).get("daily_cap")
    if cap is not None:
        return int(cap)
    from app.documents import settings as uds

    return int(uds.read_admin_policy().vision_daily_cap_default)


def local_day(profile: str) -> str:
    """Today in the profile's own timezone — the daily cap resets at the
    user's midnight, not UTC's."""
    try:
        from app.config.timezone import resolve_tzinfo

        tz = resolve_tzinfo(profile)
        return _dt.datetime.now(tz).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return _dt.datetime.now().strftime("%Y-%m-%d")


def language_hint(profile: str) -> str | None:
    """A second language for tags, so a Vietnamese query ("chó", "công viên")
    finds an English-captioned photo by keyword too. Taken from the profile's
    timezone — a proxy, but a cheap one that needs no extra setting."""
    try:
        from app.config.timezone import resolve_tzinfo

        key = getattr(resolve_tzinfo(profile), "key", "") or ""
    except Exception:  # noqa: BLE001
        return None
    return {"Asia/Ho_Chi_Minh": "Vietnamese", "Asia/Saigon": "Vietnamese"}.get(key)


def prepare_jpeg(*, path: str | None = None, data: bytes | None = None, max_side: int = CAPTION_MAX_SIDE) -> bytes:
    """Decode (HEIC included), apply the EXIF rotation, shrink, re-encode as
    JPEG — in memory. Raises on an undecodable image."""
    from PIL import Image, ImageOps

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: BLE001 — HEIC simply stays unsupported
        pass
    src = BytesIO(data) if data is not None else path
    with Image.open(src) as im:
        im = ImageOps.exif_transpose(im)
        im.thumbnail((max_side, max_side))
        out = BytesIO()
        im.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
        return out.getvalue()


def _data_url(jpeg: bytes) -> str:
    import base64

    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def parse_caption_json(text: str) -> dict[str, Any]:
    """The first JSON object in the model's answer (models sometimes wrap it
    in a code fence or a sentence)."""
    if not text:
        return {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"summary": text.strip()[:1000]}
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"summary": text.strip()[:1000]}
    return data if isinstance(data, dict) else {}


def render_caption_text(data: dict[str, Any], exif: dict[str, Any] | None = None) -> str:
    """One searchable caption paragraph from the structured answer + EXIF."""
    lines: list[str] = []
    exif = exif or {}
    kind = str(data.get("kind") or "image")
    when = str(exif.get("taken_at") or "")[:16].replace("T", " ")
    camera = " ".join(x for x in (exif.get("make"), exif.get("model")) if x)
    head = kind.capitalize()
    if when:
        head += f" taken {when}"
    if camera:
        head += f" with {camera}"
    summary = str(data.get("summary") or "").strip()
    lines.append(f"{head} — {summary}" if summary else head)
    counts = []
    for item in (data.get("animals") or []) + (data.get("objects") or []):
        if not isinstance(item, dict):
            continue
        label = item.get("species") or item.get("label")
        if not label:
            continue
        n = item.get("count")
        # "2 dogs" — the form the search engine's object-count check reads
        # (app/documents/query/engine.py _image_object_factor).
        if isinstance(n, int) and n > 0:
            plural = n > 1 and not str(label).endswith("s")
            counts.append(f"{n} {label}{'s' if plural else ''}")
        else:
            counts.append(str(label))
    if counts:
        lines.append("Shows: " + ", ".join(dict.fromkeys(counts)))
    people = data.get("people_count")
    if isinstance(people, int) and people > 0:
        lines.append(f"People: {people}")
    scene = data.get("scene") or {}
    if isinstance(scene, dict) and (scene.get("place_type") or scene.get("setting")):
        lines.append("Scene: " + ", ".join(str(v) for v in (scene.get("place_type"), scene.get("setting")) if v))
    visible = str(data.get("text") or "").strip()
    if visible:
        lines.append(f"Text in image: {visible[:500]}")
    tags = [str(t) for t in (data.get("tags") or []) if t][:12]
    local = [str(t) for t in (data.get("tags_local") or []) if t][:12]
    if tags or local:
        lines.append("Tags: " + ", ".join(dict.fromkeys(tags + local)))
    gps = exif.get("gps") if isinstance(exif.get("gps"), dict) else None
    if gps and gps.get("lat") is not None and gps.get("lon") is not None:
        lines.append(f"Location: {float(gps['lat']):.4f}, {float(gps['lon']):.4f}")
    return "\n".join(lines)


async def _complete(llm: Any, messages: list[dict[str, Any]], max_tokens: int) -> tuple[str, dict[str, int]]:
    from app.constants import ChatCompletionTypeEnum
    from app.lib.llm.base import done_chunk_token_usage

    out = ""
    usage: dict[str, int] = {}
    async for resp in llm.chat_completion(messages=messages, tools=None, temperature=0, max_tokens=max_tokens):
        rtype = resp.get("type")
        if rtype == ChatCompletionTypeEnum.CONTENT:
            out += resp.get("data") or ""
        elif rtype == ChatCompletionTypeEnum.DONE:
            usage = done_chunk_token_usage(resp)
            break
    return out, usage


async def _record_usage(llm: Any, usage: dict[str, int], profile: str, label: str) -> None:
    if not usage or not any(usage.values()):
        return
    try:
        from app.agent.usage import UsageRecord
        from app.storage import get_usage_storage

        record = UsageRecord(
            source_kind="documents",
            tool_id="documentation_search",
            label=label,
            provider=getattr(llm, "provider_name", None),
            model=getattr(llm, "model_name", None),
            model_group="vision",
            step_index=0,
            input_tokens=int(usage.get("input_tokens") or 0),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
        )
        await get_usage_storage().add_usage_records(
            conversation_id=None, profile=profile, records=[record.to_dict()], message_id=None,
        )
    except Exception:  # noqa: BLE001 — accounting must never fail a caption
        logger.exception("[documents] failed to record vision usage")


def run_vision(
    llm: Any, profile: str, jpeg: bytes, *, mode: str = "image", exif: dict[str, Any] | None = None,
) -> CaptionResult:
    """One vision call (``mode`` ``image`` → caption, ``ocr`` → page text),
    on a private event loop. Raises on a failed call; the caller refunds the
    quota slot."""
    if mode == "ocr":
        system, max_tokens, label = _OCR_SYSTEM, 4096, "Document indexing: page OCR"
        user_text = "Transcribe this page."
    else:
        lang = language_hint(profile) or "English"
        system, max_tokens, label = _CAPTION_SYSTEM.replace("{lang}", lang), 700, "Document indexing: image caption"
        user_text = "Describe this image as specified."
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": _data_url(jpeg)}},
        ]},
    ]

    async def _go() -> tuple[str, dict[str, int]]:
        text, usage = await _complete(llm, messages, max_tokens)
        await _record_usage(llm, usage, profile, label)
        return text, usage

    text, usage = asyncio.run(_go())
    result = CaptionResult(
        text="", tokens_in=int(usage.get("input_tokens") or 0), tokens_out=int(usage.get("output_tokens") or 0),
        provider=getattr(llm, "provider_name", None), model=getattr(llm, "model_name", None),
    )
    if mode == "ocr":
        result.text = (text or "").strip()
        result.data = {"ocr": True}
    else:
        result.data = parse_caption_json(text)
        result.text = render_caption_text(result.data, exif)
    return result


_VERIFY_SYSTEM = (
    "You check whether an image matches a search. Answer with ONE JSON object and "
    'nothing else: {"counts": {"<label>": <number visible>}, "matches": true|false, '
    '"why": "one short sentence"}. Count only what is clearly visible.'
)


def run_verify(
    llm: Any, profile: str, jpeg: bytes, *, labels: list[str], query: str,
) -> dict[str, Any]:
    """Ask the vision model to count ``labels`` and judge whether the image
    matches ``query`` — the check behind ``verify_images`` (a caption can be
    wrong; this looks again when the user asked for something specific)."""
    ask = f"Search: {query!r}. Count these: {', '.join(labels) or '(none)'}."
    messages = [
        {"role": "system", "content": _VERIFY_SYSTEM},
        {"role": "user", "content": [
            {"type": "text", "text": ask},
            {"type": "image_url", "image_url": {"url": _data_url(jpeg)}},
        ]},
    ]

    async def _go() -> str:
        text, usage = await _complete(llm, messages, 200)
        await _record_usage(llm, usage, profile, "Document search: image check")
        return text

    data = parse_caption_json(asyncio.run(_go()))
    counts = data.get("counts") if isinstance(data.get("counts"), dict) else {}
    return {
        "counts": {str(k).lower(): v for k, v in counts.items() if isinstance(v, int)},
        "matches": data.get("matches") if isinstance(data.get("matches"), bool) else None,
        "why": str(data.get("why") or "")[:200],
    }


def image_dims(meta: dict[str, Any] | None, exif: dict[str, Any] | None) -> tuple[int | None, int | None]:
    for src in (meta or {}, exif or {}):
        w, h = src.get("width"), src.get("height")
        if w and h:
            return int(w), int(h)
    return None, None


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()
