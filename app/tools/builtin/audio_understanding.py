"""Audio Understanding built-in tool.

Sends an audio clip to an audio-capable LLM so the agent can answer questions
about what an audio file *contains* — transcribe speech, summarize a recording,
identify sounds, answer questions about spoken content, etc. It is deliberately
scoped to *audio understanding*: routine file operations on an audio file
(duration, size, format, moving, converting) must use the ``system_file`` tools
instead, which do not spend an audio call.

The tool owns its own LLM via the dedicated ``audio`` model group (configured in
Settings → LLM Providers, falling back to the ``high`` group when unset). Before
any network call it checks ``model_supports_audio`` for the resolved model: if the
model cannot accept audio it returns a structured error that the Reasoning Agent
relays to the user — no audio is ever sent to a model that can't hear it (e.g. any
Anthropic model, which has no audio input).

Audio bytes are read from within the same allowed roots as ``system_file``
(reusing its ``_safe_resolve`` / ``_allowed_roots`` helpers), so this works on
uploaded temp files and on existing working/system files alike. Audio is sent as
an OpenAI-style ``input_audio`` content part (base64 payload + a ``format`` hint);
OpenAI-compatible providers (OpenAI, Google Gemini, xAI, Qwen, vLLM, …) accept it
directly.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, Optional, Tuple

from app.config import model_supports_audio
from app.constants import ChatCompletionTypeEnum
from app.lib.llm.base import done_chunk_token_usage
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.system_file import _allowed_roots, _guess_mime, _safe_resolve
from app.types import ToolConfig
from app.utils.logger import logger


SERVER_NAME = "Audio Understanding"

_DEFAULT_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # 25 MB (pre-base64)

# MIME type -> the ``format`` string audio-capable providers expect on the
# ``input_audio`` content part. Only these formats are accepted; anything else
# is rejected with actionable guidance (we do not transcode audio).
_AUDIO_MIME_FORMATS: Dict[str, str] = {
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/ogg": "ogg",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/webm": "webm",
}

# Fallback extension -> format when the MIME type is generic (e.g. octet-stream).
_AUDIO_EXT_FORMATS: Dict[str, str] = {
    ".wav": "wav",
    ".mp3": "mp3",
    ".mpeg": "mp3",
    ".mpga": "mp3",
    ".m4a": "m4a",
    ".mp4": "m4a",
    ".aac": "aac",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".flac": "flac",
    ".webm": "webm",
}

_AUDIO_SYSTEM_PROMPT = (
    "You are an audio assistant. You are given one audio clip and a question "
    "about it. Answer the question by listening to the audio only. When asked to "
    "transcribe speech, transcribe it faithfully and preserve speaker turns where "
    "possible. Be concise and do not speculate about anything not present in the "
    "audio."
)


class Var:
    MAX_AUDIO_BYTES = "MAX_AUDIO_BYTES"


TOOL_CONFIG: ToolConfig = {
    "name": "audio_understanding",
    "display_name": SERVER_NAME,
    "description": (
        "Sends an audio clip to an audio-capable model to answer questions about "
        "what it contains — transcribe speech, summarize a recording, or identify "
        "sounds. Use it only for audio understanding; use system_file for plain "
        "file operations on an audio file."
    ),
    "required_config": {
        Var.MAX_AUDIO_BYTES: {
            "description": (
                "Maximum audio size in bytes sent to the audio model. Larger "
                "files are rejected (audio is not transcoded or trimmed). "
                "Default: 26214400 (25 MB)."
            ),
            "type": "number",
            "default": _DEFAULT_MAX_AUDIO_BYTES,
        },
    },
}


def _int_var(variables: Dict[str, Any], key: str, default: int) -> int:
    try:
        return int(variables.get(key) or default)
    except (TypeError, ValueError):
        return default


def _resolve_audio_format(path: str, mime: str) -> Optional[str]:
    """Return the provider ``format`` string for an audio file, or None if the
    format isn't one we support sending."""
    fmt = _AUDIO_MIME_FORMATS.get(mime.lower())
    if fmt:
        return fmt
    _, ext = os.path.splitext(path)
    return _AUDIO_EXT_FORMATS.get(ext.lower())


def _prepare_audio_input(
    path: str, mime: str, max_bytes: int,
) -> Tuple[Optional[Tuple[str, str]], Optional[BuiltInToolResult]]:
    """Return ``((base64_data, format), None)`` or ``(None, error_result)``.

    Audio is base64-encoded as-is (no transcoding). The ``format`` hint is derived
    from the MIME type, falling back to the file extension.
    """
    fmt = _resolve_audio_format(path, mime)
    if not fmt:
        return None, BuiltInToolResult(structured_content={
            "error": "Unsupported audio format",
            "message": (
                f"'{os.path.basename(path)}' is {mime}, which is not a supported "
                "audio format. Provide a WAV, MP3, M4A, AAC, OGG, FLAC, or WebM file."
            ),
        })

    try:
        size = os.path.getsize(path)
    except OSError as e:
        return None, BuiltInToolResult(structured_content={
            "error": "OS error", "message": str(e)})

    if size > max_bytes:
        return None, BuiltInToolResult(structured_content={
            "error": "Audio too large",
            "message": (
                f"Audio is {size} bytes, over the {max_bytes}-byte limit. Use a "
                "shorter or more compressed clip, or raise MAX_AUDIO_BYTES in the "
                "tool's settings."
            ),
        })

    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return None, BuiltInToolResult(structured_content={
            "error": "OS error", "message": str(e)})

    return (base64.b64encode(data).decode("ascii"), fmt), None


class AnalyzeAudioTool(BuiltInTool):
    name: str = "analyze_audio"
    description: str = (
        "Listen to an audio clip and answer a question about its content — "
        "transcribe speech, summarize a recording, identify sounds, or answer "
        "questions about what is said. Sends the audio to an audio-capable model. "
        "Do NOT use for file metadata (duration/size/format → get_file_info) or "
        "moving/copying/renaming (→ move_file / copy_file)."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path to the audio file (relative to the working directory, or an absolute path within an allowed root such as an uploaded temp file).",
            },
            "query": {
                "type": "string",
                "description": "What to find out about the audio, e.g. 'transcribe this recording' or 'what is the speaker asking for?'.",
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
                "message": "No audio model is configured. Choose a Specialized Audio Model in Settings → LLM Providers.",
            })

        # Resolve the audio path within the same trust boundary as system_file.
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
        if not mime.startswith("audio/") and _resolve_audio_format(target, mime) is None:
            return BuiltInToolResult(structured_content={
                "error": "Not an audio file",
                "message": (
                    f"analyze_audio only accepts audio files; '{os.path.basename(target)}' "
                    f"is {mime}. Use read_file or convert_to_markdown for documents, or "
                    "analyze_image for images."
                ),
            })

        # Capability gate — before any network call. A model that can't accept
        # audio never receives audio data; the agent relays this error to the user.
        provider = getattr(llm, "provider_name", "") or ""
        model = getattr(llm, "model_name", "") or ""
        if not model_supports_audio(provider, model, profile=arguments.get("_profile")):
            return BuiltInToolResult(structured_content={
                "error": "AudioNotSupported",
                "model": getattr(llm, "model_label", model),
                "message": (
                    f"The configured audio model '{getattr(llm, 'model_label', model)}' "
                    "does not support audio input. Choose an audio-capable model for "
                    "the Specialized Audio Model in Settings → LLM Providers, or set the "
                    "CREMIND_AUDIO_MODELS env var if this model does in fact support audio."
                ),
            })

        max_bytes = _int_var(variables, Var.MAX_AUDIO_BYTES, _DEFAULT_MAX_AUDIO_BYTES)
        prepared, err = _prepare_audio_input(target, mime, max_bytes)
        if err is not None:
            return err
        audio_b64, audio_format = prepared

        messages = [
            {"role": "system", "content": _AUDIO_SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": query},
                {"type": "input_audio", "input_audio": {"data": audio_b64, "format": audio_format}},
            ]},
        ]

        answer = ""
        token_usage: Dict[str, int] = done_chunk_token_usage({})
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
                    token_usage = done_chunk_token_usage(response)
                    break
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[audio_understanding] audio call failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Audio call failed",
                "message": (
                    f"The audio model '{getattr(llm, 'model_label', model)}' failed to "
                    f"process the audio: {e}"
                ),
            })

        answer = answer.strip()
        if not answer:
            return BuiltInToolResult(structured_content={
                "error": "Empty response",
                "message": "The audio model returned no content for this audio.",
            }, token_usage=token_usage)

        # Single text content item → unwrapped to a plain-string observation.
        return BuiltInToolResult(
            content=[{"type": "text", "text": answer}],
            token_usage=token_usage,
        )


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    data_dir = config.get(
        "CREMIND_SYSTEM_DIR", os.path.join(os.path.expanduser("~"), ".cremind"),
    )
    return [AnalyzeAudioTool(data_dir=data_dir)]
