"""Image Understanding built-in tool.

Sends an image to a vision-capable LLM so the agent can answer questions about
what an image *depicts* — extract/OCR text shown in it, describe its contents,
read a chart, etc. It is deliberately scoped to *visual understanding*: routine
file operations on an image (dimensions, size, format, moving, converting to
markdown) must use the ``system_file`` / ``convert_to_markdown`` tools instead,
which do not spend a vision call.

The tool owns its own LLM via the dedicated ``vision`` model group (configured
in Settings → LLM Providers, falling back to the ``high`` group when unset).
Before any network call it checks ``model_supports_vision`` for the resolved
model: if the model is not vision-capable it returns a structured error that the
Reasoning Agent relays to the user — no image is ever sent to a text-only model.

Image bytes are read from within the same allowed roots as ``system_file``
(reusing its ``_safe_resolve`` / ``_allowed_roots`` helpers), so this works on
uploaded temp files and on existing working/system files alike.
"""

from __future__ import annotations

import base64
import os
from io import BytesIO
from typing import Any, Dict, Optional, Tuple

from app.config import model_supports_vision
from app.constants import ChatCompletionTypeEnum
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.system_file import _allowed_roots, _guess_mime, _safe_resolve
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Image Understanding"

_DEFAULT_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8 MB (pre-base64)
_DEFAULT_MAX_IMAGE_DIMENSION = 2048         # longest side, px

# Formats that vision providers accept directly without transcoding.
_SAFE_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

_VISION_SYSTEM_PROMPT = (
    "You are a vision assistant. You are given one image and a question about "
    "it. Answer the question by looking at the image only. When asked to "
    "extract or read text, transcribe it faithfully and preserve its structure. "
    "Be concise and do not speculate about anything not visible in the image."
)

_IMAGE_TOOL_INSTRUCTIONS = (
    "Use the Image Understanding tool ONLY when the user wants to understand the "
    "visual content of an image — read or extract text shown in it (OCR), "
    "describe what it depicts, or answer a question about what is pictured. The "
    "image is sent to a vision model. Do NOT use it for operations that do not "
    "require seeing the picture: file dimensions / size / format / metadata (use "
    "the System File 'get_file_info' sub-tool), moving / copying / renaming / "
    "deleting (use System File), or converting an image to another format such as "
    "markdown (use Convert To Markdown). If the request is about the file rather "
    "than the picture, do not use this tool."
)


class Var:
    MAX_IMAGE_BYTES = "MAX_IMAGE_BYTES"
    MAX_IMAGE_DIMENSION = "MAX_IMAGE_DIMENSION"


TOOL_CONFIG: ToolConfig = {
    "name": "image_understanding",
    "display_name": SERVER_NAME,
    # Vision = a strong, capable model. Defaults to the dedicated "vision"
    # group, which itself falls back to "high" when the user hasn't picked a
    # vision model. A per-tool model override (Settings) still works.
    "default_model_group": "vision",
    "llm_parameters": {
        "tool_instructions": _IMAGE_TOOL_INSTRUCTIONS,
        # The vision answer is produced inside run(); the adapter's post-tool
        # reasoning pass would only paraphrase it.
        "full_reasoning": False,
    },
    "locked_llm_fields": ["full_reasoning"],
    "required_config": {
        Var.MAX_IMAGE_BYTES: {
            "description": (
                "Maximum image size in bytes sent to the vision model. Larger "
                "images are downscaled when Pillow is available, else rejected. "
                "Default: 8388608 (8 MB)."
            ),
            "type": "number",
            "default": _DEFAULT_MAX_IMAGE_BYTES,
        },
        Var.MAX_IMAGE_DIMENSION: {
            "description": (
                "Longest-side pixel cap for downscaling oversized images "
                "(requires Pillow). Default: 2048."
            ),
            "type": "number",
            "default": _DEFAULT_MAX_IMAGE_DIMENSION,
        },
    },
}


def _int_var(variables: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(variables.get(key) or default)
    except (TypeError, ValueError):
        return default


def _prepare_image_data_url(
    path: str, mime: str, max_bytes: int, max_dim: int,
) -> Tuple[Optional[str], Optional[BuiltInToolResult]]:
    """Return ``(data_url, None)`` or ``(None, error_result)``.

    Fast path: a small image in a directly-accepted format is base64-encoded
    as-is. Otherwise Pillow (lazy, optional) downscales / transcodes it; if
    Pillow is unavailable the file is rejected with actionable guidance.
    """
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, BuiltInToolResult(structured_content={
            "error": "OS error", "message": str(e)})

    if size <= max_bytes and mime in _SAFE_IMAGE_MIMES:
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return None, BuiltInToolResult(structured_content={
                "error": "OS error", "message": str(e)})
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", None

    try:
        from PIL import Image  # lazy; optional
    except ImportError:
        if mime not in _SAFE_IMAGE_MIMES:
            return None, BuiltInToolResult(structured_content={
                "error": "Unsupported image format",
                "message": (
                    f"'{os.path.basename(path)}' is {mime}, which needs Pillow to "
                    "transcode for the vision model. Install the imaging support "
                    "(pip install Pillow) and restart, or provide a PNG/JPEG/WebP/GIF."
                ),
            })
        return None, BuiltInToolResult(structured_content={
            "error": "Image too large",
            "message": (
                f"Image is {size} bytes, over the {max_bytes}-byte limit, and "
                "Pillow is not installed to downscale it. Install Pillow and "
                "restart, or use a smaller image."
            ),
        })

    try:
        with Image.open(path) as im:
            im.load()
            has_alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            )
            if max(im.size) > max_dim:
                im.thumbnail((max_dim, max_dim))
            buf = BytesIO()
            if has_alpha:
                im.convert("RGBA").save(buf, format="PNG", optimize=True)
                out_mime = "image/png"
            else:
                im.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
                out_mime = "image/jpeg"
            data = buf.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[image_understanding] could not process image {path}: {e}")
        return None, BuiltInToolResult(structured_content={
            "error": "Image error",
            "message": f"Could not read or process the image: {e}",
        })

    if len(data) > max_bytes:
        return None, BuiltInToolResult(structured_content={
            "error": "Image too large",
            "message": (
                f"Image is still {len(data)} bytes after downscaling, over the "
                f"{max_bytes}-byte limit. Use a smaller image or raise "
                "MAX_IMAGE_BYTES in the tool's settings."
            ),
        })
    return f"data:{out_mime};base64,{base64.b64encode(data).decode('ascii')}", None


class AnalyzeImageTool(BuiltInTool):
    name: str = "analyze_image"
    description: str = (
        "Look at an image and answer a question about its visual content — "
        "extract/OCR text shown in it, describe what it depicts, or read a chart. "
        "Sends the image to a vision model. Do NOT use for file metadata "
        "(dimensions/size/format → get_file_info), moving/renaming (→ system_file), "
        "or converting to markdown (→ convert_to_markdown)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the image file (relative to the working directory, or an absolute path within an allowed root such as an uploaded temp file).",
            },
            "query": {
                "type": "string",
                "description": "What to find out about the image, e.g. 'extract all the text' or 'what does this diagram show?'.",
            },
        },
        "required": ["path", "query"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        path = (arguments.get("path") or "").strip()
        query = (arguments.get("query") or "").strip()
        llm = arguments.get("_llm")
        variables = arguments.get("_variables") or {}

        if not path:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter", "message": "path is required."})
        if not query:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter", "message": "query is required."})
        if llm is None:
            return BuiltInToolResult(structured_content={
                "error": "No LLM configured",
                "message": "No vision model is configured. Choose a Vision model in Settings → LLM Providers.",
            })

        # Resolve the image path within the same trust boundary as system_file.
        try:
            target = _safe_resolve(data_dir, path, _allowed_roots(arguments, data_dir))
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied", "message": str(e)})
        if not os.path.isfile(target):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": f"'{path}' is not a file or does not exist.",
            })

        mime = _guess_mime(target)
        if not mime.startswith("image/"):
            return BuiltInToolResult(structured_content={
                "error": "Not an image",
                "message": (
                    f"analyze_image only accepts image files; '{os.path.basename(target)}' "
                    f"is {mime}. Use read_file or convert_to_markdown for documents."
                ),
            })

        # Capability gate — before any network call. A non-vision model never
        # receives image data; the agent relays this error to the user.
        provider = getattr(llm, "provider_name", "") or ""
        model = getattr(llm, "model_name", "") or ""
        if not model_supports_vision(provider, model):
            return BuiltInToolResult(structured_content={
                "error": "VisionNotSupported",
                "model": getattr(llm, "model_label", model),
                "message": (
                    f"The configured Vision model '{getattr(llm, 'model_label', model)}' "
                    "does not support image input. Choose a vision-capable model for "
                    "the Vision group in Settings → LLM Providers, or set the "
                    "CREMIND_VISION_MODELS env var if this model does in fact support vision."
                ),
            })

        max_bytes = _int_var(variables, Var.MAX_IMAGE_BYTES, _DEFAULT_MAX_IMAGE_BYTES)
        max_dim = _int_var(variables, Var.MAX_IMAGE_DIMENSION, _DEFAULT_MAX_IMAGE_DIMENSION)
        data_url, err = _prepare_image_data_url(target, mime, max_bytes, max_dim)
        if err is not None:
            return err

        messages = [
            {"role": "system", "content": _VISION_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": query},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]},
        ]

        answer = ""
        try:
            async for response in llm.chat_completion(
                messages=messages,
                tools=None,
                temperature=0,
                max_tokens=2048,
            ):
                rtype = response.get("type")
                if rtype == ChatCompletionTypeEnum.CONTENT:
                    chunk = response.get("data") or ""
                    if chunk:
                        answer += chunk
                elif rtype == ChatCompletionTypeEnum.DONE:
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[image_understanding] vision call failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Vision call failed",
                "message": (
                    f"The vision model '{getattr(llm, 'model_label', model)}' failed to "
                    f"process the image: {e}"
                ),
            })

        answer = answer.strip()
        if not answer:
            return BuiltInToolResult(structured_content={
                "error": "Empty response",
                "message": "The vision model returned no content for this image.",
            })

        # Single text content item → unwrapped to a plain-string observation.
        return BuiltInToolResult(content=[{"type": "text", "text": answer}])


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    data_dir = config.get(
        "CREMIND_SYSTEM_DIR", os.path.join(os.path.expanduser("~"), ".cremind"),
    )
    return [AnalyzeImageTool(data_dir=data_dir)]
