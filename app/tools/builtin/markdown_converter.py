"""Markdown Converter built-in tool.

Converts documents (PDF, DOCX, XLSX, PPTX, HTML, CSV, etc.) to Markdown
using the markitdown library. Output files are written to
CREMIND_SYSTEM_DIR/<profile>/markdown_files/ with timestamped names.
"""

import os
import re
from datetime import datetime, timezone
from typing import Any, Dict

from app.tools.builtin.base import (
    BuiltInTool,
    BuiltInToolResult,
    missing_dependency_result,
)
from app.types import ToolConfig, ToolResultFile, ToolResultWithFiles
from app.utils.logger import logger


SERVER_NAME = "Markdown Converter"

MAX_MARKITDOWN_FILE_SIZE = 50 * 1024 * 1024  # 50 MB — fallback


class Var:
    MAX_FILE_SIZE = "MAX_FILE_SIZE"


TOOL_CONFIG: ToolConfig = {
    "name": "markdown_converter",
    "display_name": "Markdown Converter",
    "hidden": True,
    # Requires the ``documents`` feature (markitdown + pandas). See
    # app.features.manifest.FEATURES.
    "requires_feature": "documents",
    "required_config": {
        Var.MAX_FILE_SIZE: {
            "description": (
                "Maximum file size in bytes that the converter will accept. "
                "Default: 52428800 (50 MB)."
            ),
            "type": "number",
            "default": MAX_MARKITDOWN_FILE_SIZE,
        },
    },
}

# ---------------------------------------------------------------------------
# Lazy markitdown converter
# ---------------------------------------------------------------------------

_markitdown = None


def _get_markitdown():
    """Return a MarkItDown instance, importing lazily to keep startup fast.

    Raises ``ImportError`` if the ``documents`` feature isn't installed so
    the caller can surface a structured ``MissingDependency`` result to the
    Reasoning Agent instead of silently disabling the tool.
    """
    global _markitdown
    if _markitdown is None:
        from markitdown import MarkItDown  # ImportError → caller handles
        _markitdown = MarkItDown()
    return _markitdown


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

def _safe_resolve(data_dir: str, relative_path: str) -> str:
    """Resolve *relative_path* inside data_dir.  Raises ValueError on traversal."""
    base = os.path.realpath(data_dir)
    os.makedirs(base, exist_ok=True)
    relative_path = relative_path.lstrip("/\\")
    target = os.path.realpath(os.path.join(base, relative_path))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError(f"Path traversal detected: {relative_path}")
    return target


def _validate_profile(profile: str) -> None:
    """Reject profile names that could escape data_dir."""
    if not profile or not re.match(r'^[\w\-. ]+$', profile):
        raise ValueError(f"Invalid profile name: {profile!r}")


def _format_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


# ---------------------------------------------------------------------------
# Tool
# ---------------------------------------------------------------------------

class ConvertToMarkdownTool(BuiltInTool):
    name: str = "convert_to_markdown"
    description: str = (
        "Convert a file (PDF, DOCX, XLSX, PPTX, HTML, CSV, JSON, XML, etc.) "
        "to Markdown format. The converted file is saved with a timestamped "
        "filename. If an output path is specified, the file is saved there; "
        "otherwise it defaults to <profile>/markdown_files/. "
        "E.g. 'Convert cremind.pdf to markdown'"
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "source_path": {
                "type": "string",
                "description": (
                    "Relative path to the source file within the data directory. "
                    "E.g. 'documents/report.pdf' or 'default/uploads/cremind.pdf'."
                ),
            },
            "profile": {
                "type": "string",
                "description": (
                    "Profile name. The output file will be saved under "
                    "<profile>/markdown_files/."
                ),
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Optional relative path (within the data directory) where "
                    "the converted file should be saved. Can be a directory "
                    "path or a full file path ending in .md. "
                    "If omitted, defaults to <profile>/markdown_files/. "
                    "E.g. 'myprofile/docs' or 'myprofile/docs/report.md'."
                ),
            },
        },
        "required": ["source_path", "profile"],
        "additionalProperties": False,
    }

    def __init__(self, data_dir: str):
        self._data_dir = data_dir

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        data_dir = arguments.pop("_working_directory", None) or self._data_dir
        source_path = arguments.get("source_path", "").strip()
        profile = arguments.get("profile", "").strip()
        output_path = arguments.get("output_path", "").strip()

        # -- Validate required params --
        if not source_path:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "source_path is required.",
            })
        if not profile:
            return BuiltInToolResult(structured_content={
                "error": "Missing parameter",
                "message": "profile is required.",
            })

        # -- Validate profile --
        try:
            _validate_profile(profile)
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        # -- Resolve and validate source --
        try:
            source_abs = _safe_resolve(data_dir, source_path)
        except ValueError as e:
            return BuiltInToolResult(structured_content={
                "error": "Access denied",
                "message": str(e),
            })

        if not os.path.isfile(source_abs):
            return BuiltInToolResult(structured_content={
                "error": "Not found",
                "message": f"'{source_path}' is not a file or does not exist.",
            })

        variables = arguments.get("_variables") or {}
        try:
            max_size = int(variables.get(Var.MAX_FILE_SIZE) or MAX_MARKITDOWN_FILE_SIZE)
        except (TypeError, ValueError):
            max_size = MAX_MARKITDOWN_FILE_SIZE

        file_size = os.path.getsize(source_abs)
        if file_size > max_size:
            return BuiltInToolResult(structured_content={
                "error": "File too large",
                "message": (
                    f"File exceeds {_format_size(max_size)} limit "
                    f"(actual: {_format_size(file_size)})."
                ),
            })

        # -- Get converter --
        try:
            converter = _get_markitdown()
        except ImportError as e:
            return missing_dependency_result(
                tool="convert_to_markdown",
                feature_key="documents",
                extras=("documents",),
                detail=str(e),
            )

        # -- Convert --
        source_name = os.path.basename(source_abs)
        try:
            result = converter.convert(source_abs)
            md_content = result.text_content if result and result.text_content else None
        except Exception as e:
            logger.error(f"[ConvertToMarkdownTool] conversion failed for {source_name}: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Conversion failed",
                "message": f"markitdown could not convert '{source_name}': {e}",
            })

        if not md_content:
            return BuiltInToolResult(structured_content={
                "error": "Conversion failed",
                "message": f"Conversion produced no content for '{source_name}'.",
            })

        # -- Build output path --
        base = os.path.realpath(data_dir)
        source_stem = os.path.splitext(source_name)[0]

        if output_path:
            try:
                resolved_output = _safe_resolve(data_dir, output_path)
            except ValueError as e:
                return BuiltInToolResult(structured_content={
                    "error": "Access denied",
                    "message": str(e),
                })

            if output_path.endswith(".md"):
                output_dir = os.path.dirname(resolved_output)
                out_name = os.path.basename(resolved_output)
            else:
                output_dir = resolved_output
                out_name = f"{source_stem}.md"
        else:
            output_dir = os.path.join(data_dir, "markdown_files")
            out_name = f"{source_stem}.md"

        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, out_name)

        # If file already exists, append a timestamp to avoid overwriting
        if os.path.exists(out_path):
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            stem = os.path.splitext(out_name)[0]
            out_name = f"{stem}_{timestamp}.md"
            out_path = os.path.join(output_dir, out_name)

        # -- Write output --
        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(md_content)
        except OSError as e:
            logger.error(f"[ConvertToMarkdownTool] write failed: {e}")
            return BuiltInToolResult(structured_content={
                "error": "Write error",
                "message": f"Failed to write output file: {e}",
            })

        # -- Build result --
        rel_output = os.path.relpath(out_path, base).replace(os.sep, "/")
        file_uri = out_path.replace(os.sep, "/")

        file_entry: ToolResultFile = {
            "uri": file_uri,
            "name": out_name,
            "mime_type": "text/markdown",
        }

        payload: ToolResultWithFiles = {
            "text": (
                f"Converted '{source_name}' to markdown.\n"
                f"Output: {rel_output} ({len(md_content)} characters)"
            ),
            "_files": [file_entry],
        }

        logger.info(
            f"[ConvertToMarkdownTool] converted {source_name} -> {rel_output} "
            f"({len(md_content)} chars)"
        )

        return BuiltInToolResult(structured_content=payload)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    data_dir = config.get("CREMIND_SYSTEM_DIR", os.path.join(os.path.expanduser("~"), ".cremind"))
    return [ConvertToMarkdownTool(data_dir=data_dir)]
