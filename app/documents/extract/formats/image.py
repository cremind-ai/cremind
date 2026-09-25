"""Images: dimensions and EXIF only. No blocks.

An image's searchable text comes later, from the Specialized Vision Model's
caption. What this worker contributes is what the file itself records: when
and with what the photo was taken, and where. That is enough to answer "the
photos from my trip last May" by date and place before any caption exists.

Only headers are read: ``Image.open`` parses metadata lazily and nothing here
calls ``load()``, so a 100-megapixel photo costs no pixel memory. HEIC/HEIF
(every iPhone photo) needs pillow-heif; without it those files are reported
``heic_unsupported`` rather than ``corrupt``, so they can be retried once the
extra is installed.
"""

from __future__ import annotations

import math
import re
import threading
from datetime import datetime
from typing import Any

from ._base import Ctx

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_ORIENTATION = 0x0112
_TAG_SOFTWARE = 0x0131
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_OFFSET_TIME_ORIGINAL = 0x9011

_HEIF_MIMES = frozenset({"image/heic", "image/heif", "image/heic-sequence",
                         "image/heif-sequence", "image/avif"})

_EXIF_DATE_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})")
_OFFSET_RE = re.compile(r"^[+-]\d{2}:\d{2}$")

_heif_lock = threading.Lock()
_heif_registered = False


def _register_heif() -> bool:
    """Register pillow-heif's opener once per process; False when missing."""
    global _heif_registered
    try:
        import pillow_heif
    except ImportError:
        return False
    with _heif_lock:
        if not _heif_registered:
            pillow_heif.register_heif_opener()
            _heif_registered = True
    return True


def _text(value: Any) -> str | None:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    value = value.replace("\x00", "").strip()
    return value or None


def exif_datetime(value: Any, offset: Any = None) -> str | None:
    """EXIF ``YYYY:MM:DD HH:MM:SS`` (+ an ``OffsetTimeOriginal``) as ISO 8601.
    Cameras with an unset clock write zeros; those are no date at all."""
    text = _text(value)
    if not text:
        return None
    m = _EXIF_DATE_RE.match(text)
    if not m:
        return None
    try:
        stamp = datetime(*(int(g) for g in m.groups()))
    except ValueError:
        return None
    if stamp.year < 1900:
        return None
    iso = stamp.isoformat()
    offset_text = _text(offset)
    if offset_text and _OFFSET_RE.match(offset_text):
        iso += offset_text
    return iso


def _rational(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return number if math.isfinite(number) else None


def _dms(value: Any) -> float | None:
    if not isinstance(value, (tuple, list)) or len(value) != 3:
        return None
    parts = [_rational(v) for v in value]
    if any(p is None for p in parts):
        return None
    degrees, minutes, seconds = parts  # type: ignore[misc]
    return degrees + minutes / 60.0 + seconds / 3600.0


def gps_coordinates(gps: dict[int, Any]) -> dict[str, float] | None:
    """Decimal ``{lat, lon}`` from a GPS IFD, or None when absent or nonsense."""
    lat, lon = _dms(gps.get(2)), _dms(gps.get(4))
    if lat is None or lon is None:
        return None
    if (_text(gps.get(1)) or "N").upper().startswith("S"):
        lat = -lat
    if (_text(gps.get(3)) or "E").upper().startswith("W"):
        lon = -lon
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None  # 0,0 is what a phone writes when it had no fix
    return {"lat": round(lat, 6), "lon": round(lon, 6)}


def extract_image(ctx: Ctx) -> None:
    from PIL import Image, UnidentifiedImageError

    from ..detect import sniff

    _kind, mime = sniff(ctx.head(256), b"", ctx.ext)
    heif = mime in _HEIF_MIMES
    if heif and not _register_heif() and mime != "image/avif":
        ctx.metadata_only("heic_unsupported")
        return

    fh = ctx.open()
    try:
        try:
            im = Image.open(fh)
        except UnidentifiedImageError:
            if heif:
                ctx.metadata_only("heic_unsupported")
                return
            raise
        except Image.DecompressionBombError:
            ctx.metadata_only("too_large")
            return
        with im:
            width, height = im.size
            fmt = im.format
            exif = im.getexif()
            sub = exif.get_ifd(_EXIF_IFD) if exif else {}
            gps_ifd = exif.get_ifd(_GPS_IFD) if exif else {}
    finally:
        fh.close()

    make, model = _text(exif.get(_TAG_MAKE)), _text(exif.get(_TAG_MODEL))
    taken_at = exif_datetime(sub.get(_TAG_DATETIME_ORIGINAL), sub.get(_TAG_OFFSET_TIME_ORIGINAL))
    found: dict[str, Any] = {
        "taken_at": taken_at,
        "make": make,
        "model": model,
        "software": _text(exif.get(_TAG_SOFTWARE)),
        "orientation": exif.get(_TAG_ORIENTATION) if isinstance(exif.get(_TAG_ORIENTATION), int) else None,
        "gps": gps_coordinates(gps_ifd) if gps_ifd else None,
    }
    info = {key: value for key, value in found.items() if value is not None}
    info["width"], info["height"] = width, height
    ctx.result.exif = info
    ctx.result.image = {
        "width": width,
        "height": height,
        "format": fmt,
        "is_camera_photo": bool(make or model) and bool(taken_at),
    }
    if fmt and not ctx.result.mime:
        ctx.result.mime = Image.MIME.get(fmt)
