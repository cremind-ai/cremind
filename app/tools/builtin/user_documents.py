"""User Documents built-in tool: the agent's way into the user's own files.

Three leaves over the per-profile index that User Document Search keeps in
sync (see :mod:`app.userdocs`), exposed as ``user_documents__<leaf>``:

- ``find_files`` — files, folders and projects by name, type, date, folder
  (the catalog), with counts by type, folder, month or extension;
- ``search`` — passages by meaning and by keyword (hybrid retrieval, fused),
  grouped by file, by folder or not at all;
- ``read`` — the text of one file, by pages, lines, section ("Điều 203"),
  sheet, rows, slide, or around a phrase or a passage token.

Every passage is printed with a citation token (``[ud:<file>#<chunk>]``) and
every token printed is registered for the conversation
(:func:`app.userdocs.citations.issue`), so the answer's citations can be
verified when it is saved. Every result fits the profile's tool-result budget
by construction (see :mod:`app.userdocs.query.render`), and everything taken
from the user's files is wrapped as untrusted data.

Not Cremind's own documentation — that is ``documentation_search`` — and not
a file-system browser: only what User Document Search has indexed for the
active profile is visible, and another profile's files do not exist here.

The tool's visibility is decided per run by
:func:`app.userdocs.gate.userdocs_tool_available` (admin gate, the profile's
opt-in, and where the conversation happens); this module only answers when
asked, and answers "not available, because …" rather than raising.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.tools.builtin.external_content import wrap_document_content
from app.types import ToolConfig
from app.userdocs.query.filters import DATE_FIELDS, IMAGE_ORIGINS, SOURCES, TYPE_NAMES
from app.utils.logger import logger

SERVER_NAME = "User Documents"
TOOL_ID = "user_documents"

LEAF_FIND = "find_files"
LEAF_SEARCH = "search"
LEAF_READ = "read"
# Deep research (PR6) will register as a fourth leaf of this group; the agent
# guidance mentions it only when a leaf of this name is registered.
LEAF_RESEARCH = "research"

DEFAULT_TOP_K = 8
MAX_TOP_K = 30


class Var:
    DEFAULT_TOP_K = "DEFAULT_TOP_K"
    RESEARCH_MODEL_GROUP = "RESEARCH_MODEL_GROUP"
    RESEARCH_TOKEN_BUDGET = "RESEARCH_TOKEN_BUDGET"


TOOL_CONFIG: ToolConfig = {
    "name": TOOL_ID,
    "display_name": SERVER_NAME,
    "description": (
        "Searches the user's OWN files indexed by User Document Search — their "
        "documents, notes, reports, spreadsheets, photos and project folders — "
        "by meaning, keyword, date, type and folder, reads any part of a file, "
        "and returns [ud:…] citation tokens to cite in the answer. Not for "
        "Cremind's own documentation (use documentation search for that)."
    ),
    # On by default; it only appears for a profile that turned User Document
    # Search on, where the conversation's origin is allowed (see gate.py).
    "default": True,
    "required_config": {
        Var.DEFAULT_TOP_K: {
            "description": (
                "How many results user_documents search returns per page when "
                "the agent does not ask for a number (1-30)."
            ),
            "type": "number",
            "default": DEFAULT_TOP_K,
        },
        Var.RESEARCH_MODEL_GROUP: {
            "description": (
                "Model group that runs deep research over the user's documents "
                "(verified legal/financial analysis, exhaustive folder "
                "compilations): 'high' for the main model, 'low' for the "
                "cheaper auxiliary model."
            ),
            "type": "string",
            "enum": ["high", "low"],
            "default": "high",
        },
        Var.RESEARCH_TOKEN_BUDGET: {
            "description": (
                "Maximum LLM tokens one deep-research job may spend before it "
                "stops and reports what it covered."
            ),
            "type": "number",
            "default": 250000,
        },
    },
}


# ── schemas ────────────────────────────────────────────────────────────────

FILTERS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Narrow the files considered. Hard filters: folder, path_glob, "
        "name_query, types, extensions, source, date_*, size_*, has_gps, "
        "file_ids. Soft (boost only, never exclude): author, taken_by, "
        "image_origin."
    ),
    "properties": {
        "folder": {
            "type": "array", "items": {"type": "string"},
            "description": "Folder names or paths, matched loosely (case, accents, typos); "
                           "includes everything beneath them. E.g. [\"MKT-report\"].",
        },
        "path_glob": {
            "type": "array", "items": {"type": "string"},
            "description": "Glob over the path inside the indexed folder, e.g. \"Clients/*/2025/**\" or \"*.pdf\".",
        },
        "name_query": {"type": "string", "description": "Words that must all appear in the file name."},
        "types": {
            "type": "array", "items": {"type": "string", "enum": list(TYPE_NAMES)},
            "description": "File types. 'document' = PDF, Word, text/Markdown, slides, e-books, e-mails.",
        },
        "extensions": {"type": "array", "items": {"type": "string"}, "description": "E.g. [\"pdf\", \".docx\"]."},
        "source": {"type": "string", "enum": list(SOURCES), "description": "Local folder, Google Drive, or both."},
        "date_field": {
            "type": "string", "enum": list(DATE_FIELDS),
            "description": "Which date date_from/date_to apply to. 'any' (default) matches the document's "
                           "creation date, the file's creation or modification time, or a photo's EXIF date.",
        },
        "date_from": {"type": "string", "description": "First day, YYYY-MM-DD (or YYYY-MM, YYYY), user's time zone."},
        "date_to": {"type": "string", "description": "Last day (inclusive), YYYY-MM-DD (or YYYY-MM, YYYY)."},
        "size_min": {"type": "integer", "description": "Minimum size in bytes."},
        "size_max": {"type": "integer", "description": "Maximum size in bytes."},
        "author": {"type": "string", "description": "'me' (the user's configured names) or a name. Soft."},
        "taken_by": {"type": "string", "description": "'me' (the user's cameras) or a camera make/model. Soft."},
        "image_origin": {"type": "string", "enum": list(IMAGE_ORIGINS), "description": "Soft."},
        "has_gps": {"type": "boolean", "description": "Only photos with (true) or without (false) a location."},
        "file_ids": {
            "type": "array", "items": {"type": "string"},
            "description": "Restrict to these files: [ud:…] tokens or file ids from earlier results.",
        },
    },
    "additionalProperties": False,
}


class UserDocumentsFindFilesTool(BuiltInTool):
    name: str = LEAF_FIND
    description: str = (
        "Find the user's own files, folders or project folders by name, type, date, folder or "
        "what they are about — e.g. last week's photos, the spreadsheets in MKT-report, "
        "the Python robot project from last year (kind=project). Without a query it lists "
        "what the filters select (newest first); `aggregate` counts them by type, folder, "
        "month or extension. Returns [ud:…] tokens; photos come back as thumbnails."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What the file or folder is called or is about (optional)."},
            "kind": {
                "type": "string", "enum": ["file", "folder", "project"],
                "description": "file (default), folder, or project (folders that are code projects).",
            },
            "filters": FILTERS_SCHEMA,
            "sort": {
                "type": "string", "enum": ["relevance", "newest", "oldest", "name", "largest", "smallest"],
                "description": "Default: relevance with a query, newest without.",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Results per page (default 20)."},
            "aggregate": {
                "type": "string", "enum": ["by_type", "by_folder", "by_month", "by_extension"],
                "description": "Also count all matching files by this key.",
            },
            "page": {"type": "integer", "minimum": 1, "description": "Page of results (default 1)."},
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return await _tool_result(LEAF_FIND, arguments)


class UserDocumentsSearchTool(BuiltInTool):
    name: str = LEAF_SEARCH
    description: str = (
        "Search inside the user's own files for passages about something — by meaning and "
        "by exact words (identifiers such as 45/2013/QH13 or 'Điều 203' match exactly). "
        "Returns the best files (or folders, or passages) with the matching passages, "
        "each with a [ud:…] citation token to copy into the answer. Use filters for "
        "dates, folders and types; if a date window finds nothing it is widened and "
        "the result says so."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for, in the user's words."},
            "filters": FILTERS_SCHEMA,
            "group_by": {
                "type": "string", "enum": ["file", "folder", "chunk"],
                "description": "file (default: up to 2 passages per file), folder (nearest project "
                               "folder, else parent folder), or chunk (every passage on its own).",
            },
            "top_k": {"type": "integer", "minimum": 1, "maximum": MAX_TOP_K,
                      "description": "Results per page (default from the tool settings, 8)."},
            "expand": {"type": "boolean",
                       "description": "Show surrounding context for the best results (default true)."},
            "thorough": {"type": "boolean",
                         "description": "Slower, better recall: restore Vietnamese accents, translate the "
                                        "query, and rerank the best 30 with a model."},
            "image_objects": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"label": {"type": "string"}, "count": {"type": "integer"}},
                    "required": ["label"],
                    "additionalProperties": False,
                },
                "description": "Objects a photo should show, e.g. [{\"label\": \"dog\", \"count\": 2}]. Soft.",
            },
            "verify_images": {"type": "boolean",
                              "description": "Re-check the top photos with the vision model (when available)."},
            "page": {"type": "integer", "minimum": 1, "description": "Page of results (default 1)."},
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return await _tool_result(LEAF_SEARCH, arguments)


class UserDocumentsReadTool(BuiltInTool):
    name: str = LEAF_READ
    description: str = (
        "Read one of the user's indexed files, rebuilt from the index with a [ud:…] token on "
        "every passage. Choose a part with pages, lines, section (a heading or a legal "
        "reference like 'Điều 203'), sheet/rows, slide, or around (a phrase). A long file "
        "without a locator comes back as its beginning, a table of contents with sizes, and "
        "the parts matching `query`."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "file": {"type": "string",
                     "description": "A [ud:…] token (a passage token opens around that passage), a file id, "
                                    "a path inside the indexed folder, or a file name."},
            "pages": {"type": "string", "description": "PDF pages, e.g. \"3\" or \"3-5\"."},
            "lines": {"type": "string", "description": "Line range in a text file, e.g. \"40-80\"."},
            "section": {"type": "string",
                        "description": "A heading, or a legal reference such as \"Điều 203\" or \"khoản 2 Điều 5\"."},
            "sheet": {"type": "string", "description": "Spreadsheet sheet name."},
            "rows": {"type": "string", "description": "Spreadsheet or CSV rows, e.g. \"2-40\"."},
            "slide": {"type": "string", "description": "Slide number or range."},
            "around": {"type": "string",
                       "description": "A phrase: return the passage containing it and its neighbours."},
            "query": {"type": "string", "description": "What you are looking for: ranks the parts of a long file."},
            "page": {"type": "integer", "minimum": 1,
                     "description": "Next part of a selection too long for one reply (default 1)."},
        },
        "required": ["file"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return await _tool_result(LEAF_READ, arguments)


# ── execution (shared with the REST query API) ─────────────────────────────


@dataclass
class LeafResult:
    """One leaf's answer, before it is shaped for the agent or for HTTP."""

    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    citations: list[Any] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, int] | None = None
    # {"error": code, "message": …, …} when the leaf could not answer.
    error: dict[str, Any] | None = None
    # unavailable | invalid | not_found — lets the API choose a status code.
    error_kind: str | None = None


def render_context(profile: Optional[str], *, budgeted: bool = True):
    """How results are sized and wrapped. ``budgeted`` sizes them to the
    profile's ``tool_result.max_tokens`` exactly as documentation_search
    does (synchronous: it reads the profile's config); the CLI asks for the
    whole result instead."""
    from app.tools.builtin.documentation_search import _cut_tokens, _delivery_budget, _fit_lines, _tokens
    from app.userdocs.query.render import RenderContext

    limit = _delivery_budget(profile or "admin", "") if budgeted else None
    return RenderContext(limit=limit, tokens=_tokens, fit_lines=_fit_lines, cut_tokens=_cut_tokens,
                         wrap=wrap_document_content)


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _str(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


async def execute(
    leaf: str,
    profile: str,
    args: Dict[str, Any],
    *,
    llm: Any = None,
    variables: Optional[Dict[str, Any]] = None,
    budgeted: bool = True,
) -> LeafResult:
    """Run one leaf for ``profile``. Every blocking step (index reads, the
    query embedding, rendering) runs in a worker thread. "Not available",
    a bad filter and an unknown file come back as ``LeafResult.error``;
    store and embedder failures degrade the search mode instead of raising
    (see :mod:`app.userdocs.query.engine`)."""
    from app.userdocs.query import FilterError, ReadError, open_engine
    from app.userdocs.query import render as R

    access = await asyncio.to_thread(open_engine, profile)
    if access.engine is None:
        return LeafResult(
            text=R.render_status(access.message or "not available", code=access.code),
            error={"error": "UserDocumentsUnavailable", "status": access.code, "message": access.message},
            error_kind="unavailable",
        )
    engine = access.engine
    variables = variables or {}
    try:
        if leaf == LEAF_SEARCH:
            return await _search(engine, profile, args, llm=llm, variables=variables, budgeted=budgeted)
        if leaf == LEAF_FIND:
            outcome = await asyncio.to_thread(
                engine.find, _str(args.get("query")),
                kind=_str(args.get("kind")) or "file", filters=args.get("filters"),
                sort=_str(args.get("sort")), limit=_int(args.get("limit"), 20, 1, 100),
                aggregate=_str(args.get("aggregate")), page=_int(args.get("page"), 1, 1, 10_000),
            )
            rendered = await asyncio.to_thread(
                lambda: R.render_find(outcome, render_context(profile, budgeted=budgeted)))
        elif leaf == LEAF_READ:
            outcome = await asyncio.to_thread(
                engine.read, str(args.get("file") or ""),
                pages=_str(args.get("pages")), lines=_str(args.get("lines")), section=_str(args.get("section")),
                sheet=_str(args.get("sheet")), rows=_str(args.get("rows")), slide=_str(args.get("slide")),
                around=_str(args.get("around")), query=_str(args.get("query")),
                page=_int(args.get("page"), 1, 1, 10_000),
            )
            rendered = await asyncio.to_thread(
                lambda: R.render_read(outcome, render_context(profile, budgeted=budgeted)))
        else:
            return LeafResult(error={"error": "UnknownLeaf", "message": f"unknown leaf {leaf!r}"},
                              error_kind="invalid")
    except FilterError as exc:
        return LeafResult(error={"error": "InvalidFilter", "message": str(exc)}, error_kind="invalid")
    except ReadError as exc:
        kind = "not_found" if exc.code in ("NotFound", "IsAFolder") else "invalid"
        return LeafResult(error={"error": exc.code, "message": exc.message, "candidates": exc.candidates},
                          error_kind=kind)
    return LeafResult(text=rendered.text, data=rendered.data, citations=rendered.citations, files=rendered.files)


async def _search(engine: Any, profile: str, args: Dict[str, Any], *, llm: Any, variables: Dict[str, Any],
                  budgeted: bool) -> LeafResult:
    from app.userdocs.query import render as R
    from app.userdocs.query.rerank import RERANK_TOP, add_usage, query_variants, rerank

    query = _str(args.get("query"))
    if not query:
        return LeafResult(error={"error": "MissingQuery", "message": "Pass `query`: what to look for."},
                          error_kind="invalid")
    default_k = _int(variables.get(Var.DEFAULT_TOP_K), DEFAULT_TOP_K, 1, MAX_TOP_K)
    top_k = _int(args.get("top_k"), default_k, 1, MAX_TOP_K) if args.get("top_k") is not None else default_k
    page = _int(args.get("page"), 1, 1, 10_000)
    group_by = _str(args.get("group_by")) or "file"
    expand = args.get("expand") is not False
    thorough = bool(args.get("thorough"))
    image_objects = args.get("image_objects") if isinstance(args.get("image_objects"), list) else None
    usage: dict[str, int] = {}
    variants: list[str] = []
    notes: list[str] = []
    if thorough and llm is None:
        notes.append("thorough mode needs a model (the 'low' group) and none is configured; ran a normal search.")
        thorough = False
    if thorough:
        variants, u = await query_variants(llm, query, db=engine.db)
        usage = add_usage(usage, u)

    outcome = await asyncio.to_thread(
        engine.search, query, filters=args.get("filters"), group_by=group_by,
        top_k=RERANK_TOP if thorough else top_k, page=1 if thorough else page,
        expand=expand and not thorough, variants=variants, image_objects=image_objects,
        verify_images=bool(args.get("verify_images")),
    )
    if thorough and outcome.groups:
        candidates = []
        for g in outcome.groups:
            owner = g.file if g.file is not None else g.folder
            title = (owner or {}).get("rel_path") or "(top level)"
            candidates.append((title, " ".join((g.best.chunk.get("text") or "").split())))
        order, u = await rerank(llm, query, candidates)
        usage = add_usage(usage, u)
        ranked = [outcome.groups[i] for i in order] if order else list(outcome.groups)
        start = (page - 1) * top_k
        outcome.groups = ranked[start:start + top_k]
        outcome.total = min(outcome.total, len(ranked))
        outcome.page, outcome.top_k = page, top_k
        if expand:
            await asyncio.to_thread(engine.expand, outcome.groups)
        notes.append(f"thorough: {'reranked the best ' + str(len(ranked)) if order else 'rerank unavailable'}"
                     + (f"; also searched: {', '.join(variants)}" if variants else ""))
    outcome.notes = notes + outcome.notes
    rendered = await asyncio.to_thread(lambda: R.render_search(outcome, render_context(profile, budgeted=budgeted)))
    return LeafResult(text=rendered.text, data=rendered.data, citations=rendered.citations, files=rendered.files,
                      token_usage=usage or None)


async def _tool_result(leaf: str, arguments: Dict[str, Any]) -> BuiltInToolResult:
    profile = arguments.get("_profile") or "admin"
    context_id = arguments.get("_context_id")
    args = {k: v for k, v in arguments.items() if not k.startswith("_")}
    try:
        result = await execute(leaf, profile, args, llm=arguments.get("_llm"),
                               variables=arguments.get("_variables") or {})
    except Exception as exc:  # noqa: BLE001 — a search failure is an observation, not a crash
        logger.exception(f"[userdocs] {leaf} failed for {profile}")
        return BuiltInToolResult(structured_content={
            "error": "SearchFailed", "message": f"User document {leaf} failed: {exc}"})
    if result.error is not None:
        if result.error_kind == "unavailable":
            return BuiltInToolResult(structured_content={**result.error, "text": result.text})
        return BuiltInToolResult(structured_content=result.error)
    if result.citations:
        await _issue(profile, context_id, result.citations)
    if result.files:
        return BuiltInToolResult(
            structured_content={"text": result.text, "_files": result.files},
            token_usage=result.token_usage,
        )
    return BuiltInToolResult(content=[{"type": "text", "text": result.text}], token_usage=result.token_usage)


async def _issue(profile: str, context_id: Optional[str], citations: list[Any]) -> None:
    """Register every printed token for this conversation, off the loop.
    The registry never raises into a tool; this guards against it anyway."""
    from app.userdocs import citations as registry

    try:
        await asyncio.to_thread(registry.issue, profile, context_id, citations)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[userdocs] could not register {len(citations)} citation(s): {exc}")


def get_tools(config: dict) -> list[BuiltInTool]:
    """The three leaves; research joins them in a later release."""
    return [UserDocumentsFindFilesTool(), UserDocumentsSearchTool(), UserDocumentsReadTool()]
