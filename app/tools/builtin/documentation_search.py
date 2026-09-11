"""Documentation Search built-in tool.

Vector-searches Markdown documentation kept under ``<CREMIND_SYSTEM_DIR>/documents``
(shared) and ``<CREMIND_SYSTEM_DIR>/<profile>/documents`` (per-profile),
then runs an internal LLM-as-judge to pick the single most accurate
candidate before loading that one document's body and returning it to the
Reasoning Agent.

Why the internal LLM step is required
-------------------------------------
Vector search ranks candidates by cosine similarity, which is approximate.
An LLM judge reads the user's query alongside each candidate's name and
one-line description and picks the document that *actually* answers the
query (or none, if nothing is on-topic). The result is the body of that
single document -- reliable enough to hand back to the Reasoning Agent
verbatim.

Token-frugal contract
---------------------
- The judge only sees ``name`` + ``description`` for each candidate.
  Document bodies are NOT sent to the judge.
- The judge uses **tool calling** (``select_document(index)`` /
  ``no_relevant_result()``) rather than parsing free-form JSON, so its
  output is structurally guaranteed.
- Only AFTER the judge picks does the tool open and read the chosen
  ``.md`` file's body from disk.

Delivery budget
---------------
The reasoning agent head-clips every tool result to the profile's
``tool_result.max_tokens`` (4000 by default). A whole document longer than that
used to arrive as its preamble plus a truncation notice, every single time: in
one logged conversation the judge picked the right 23k-token CLI reference ten
times in a row and the agent never once saw the subcommand it asked for, because
its only knobs -- ``query`` and ``top_k`` -- can re-select a document but never
reach further into one. So this tool owns its delivery budget. A document that
fits is returned whole, exactly as before. One that does not comes back as an
*envelope*: its head, a table of contents with each section's token size, and
the sections whose headings match the query. A second leaf,
``read_documentation_section``, returns any single section by name -- no
embedding, no judge, just a deterministic slice of the file.

Invocation
----------
The reasoning model calls ``search_documentation`` directly via native
function calling, filling the ``query`` argument; there is no per-group
routing LLM. The tool's INTERNAL judge LLM (below) is the only LLM call it makes.

The leaf ``description`` (what the reasoning model sees) frames this as a
*document search tool*. The tool's INTERNAL judge LLM, by contrast, is told
its sole job is to pick the most accurate candidate -- not to reason about
the user's request more broadly.
"""

from __future__ import annotations

import difflib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config.user_config import resolve_group
from app.constants import ChatCompletionTypeEnum
from app.documents import get_service
from app.documents.sections import (
    Section,
    SectionSizes,
    children,
    find_section,
    normalize_heading,
    open_fence_at_end,
    query_words,
    rank_sections_for_query,
    render_toc,
    section_own_text,
    section_text,
    size_sections,
    split_sections,
)
from app.documents.sync import DESCRIPTION_MAX_CHARS, SHARED_SCOPE, _clean_name
from app.lib.llm.base import done_chunk_token_usage
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.common import count_content_tokens
from app.utils.logger import logger
from app.utils.message_tokens import resolve_system_var_tokens


SERVER_NAME = "Documentation Search"

DEFAULT_TOP_K = 10

NO_RESULT_MESSAGE = "no relevant result found"

# Bundled `cremind` CLI man pages are named ``[cli]cremind <feature>.md``; the
# stem keeps the bracketed tag in the vector-store payload (``_clean_name`` only
# strips it from the embedded text, not the ``name`` field), so a simple prefix
# test identifies a CLI doc in both the vector and degraded full-scan paths.
_CLI_DOC_PREFIX = "[cli]"

# The group's tool id and the exposed function names of its two leaves. The
# names mirror ``app.tools.base.make_leaf_name`` (``<tool_id>__<leaf>``), which
# cannot be imported at module load because of the builtin <-> registry import
# cycle; a test pins that the two agree.
_TOOL_ID = "documentation_search"
SEARCH_LEAF_NAME = "search_documentation"
SECTION_LEAF_NAME = "read_documentation_section"
SEARCH_LEAF_FN = f"{_TOOL_ID}__{SEARCH_LEAF_NAME}"
SECTION_LEAF_FN = f"{_TOOL_ID}__{SECTION_LEAF_NAME}"

# Prepended to a returned CLI-reference body so the reasoning model RUNS the
# documented command via exec_shell instead of paraphrasing the man page (the
# incident: the model listed providers from the doc's example table and told the
# user to run the command). Rides the tool RESULT, not the system prompt —
# matching the skill-load precedent in reasoning_agent.py, which keeps such
# contextual guidance out of the (prompt-cached) system prompt. Prepended, not
# appended, because long tool results are head-truncated. ``{fn}`` is the
# exposed exec_shell run-leaf function name, derived at serve time.
#
# The ``--json`` bullet exists because the agent kept appending ``--json`` to
# the end of a command: it is a ROOT option of the CLI (app/cli/main.py), so a
# trailing one is rejected, and on a few commands it is instead that command's
# own JSON-payload flag. Logged conversations show every such command failing
# and being re-run with the flag moved.
_CLI_EXECUTION_DIRECTIVE = (
    "[Agent directive — run the command, don't recite the doc]\n"
    "The document below is a `cremind` CLI reference. If the user's question can "
    "be answered by running one of its commands, do NOT answer from the text: run "
    "the command yourself with the `{fn}` function and report its REAL output. The "
    "shell already has CREMIND_SERVER and CREMIND_TOKEN set for the active profile, "
    "so the command works as-is.\n"
    "- Machine-readable output: `--json` is a GLOBAL flag and goes right after "
    "`cremind`, before the command group — `cremind --json <group> <command>`. A "
    "trailing `--json` is rejected as an unknown option, and on a few commands "
    "(`channels add`, `channels edit`, `channels notify-filter`, `embedding set`, "
    "`llm providers configure`, `setup complete`, `tools set-args`) a `--json` "
    "AFTER the command is that command's own JSON-payload option, not the output "
    "switch.\n"
    "- Read-only commands (list / show / get / status / options): run them "
    "immediately, without asking.\n"
    "- State-changing commands (configure / set / enable / disable / create / "
    "delete): run them when the user asked for that change; confirm first only if "
    "the action is destructive or ambiguous.\n"
    "- Shell sessions and tables inside the document are illustrative EXAMPLES, not "
    "live data — never present them as this server's current state. Only real "
    "command output is live.\n"
    "- Operating the `cremind` CLI is NOT a coding task — never delegate it to a "
    "coding agent.\n"
)


_JUDGE_SYSTEM_PROMPT = (
    "You are the Documentation Search relevance judge. You are given a numbered "
    "list of candidate documents -- ranked by vector similarity when semantic "
    "search is available, otherwise every document in the library. Each "
    "candidate has a short name and a description -- you do NOT see the body.\n"
    "\n"
    "Your sole job is to pick the SINGLE candidate that best answers the "
    "user's query. You MUST decide by calling exactly one of the provided "
    "tools:\n"
    "- `select_document(index)` -- when one candidate clearly matches.\n"
    "- `no_relevant_result()`   -- when every candidate is off-topic or "
    "only tangentially related.\n"
    "\n"
    "Do not write any prose, do not invent indices outside the list, and "
    "do not call any other tool."
)

_SELECT_TOOL_NAME = "select_document"
_NO_MATCH_TOOL_NAME = "no_relevant_result"


# ── Delivery budget ─────────────────────────────────────────────────────────

# Used only when the profile's agent config cannot be resolved; mirrors the
# ``[tool_result].max_tokens`` default in app/config/settings.toml.
_FALLBACK_CLAMP_TOKENS = 4000
# Slack for newline joins and tokenizer boundary effects between our count and
# the agent's own clamp, which measures the joined text.
_BUDGET_MARGIN_TOKENS = 100
# The body budget never goes below this, even when the clamp minus the
# directive would. A result is only guaranteed to survive the agent's clamp
# when the clamp leaves at least this much after the directive and margin
# (roughly clamp >= 720 for a CLI reference); below that the operator has
# chosen a clamp too small for any excerpt, and the agent's clamp cuts it.
_MIN_BUDGET_TOKENS = 300
# Envelope layout: the head may use this share of the budget, the table of
# contents this share; matched sections get what is left.
_HEAD_SHARE = 0.35
_TOC_SHARE = 0.25
# At most this many ranked sections are tried for the envelope.
_MAX_SECTION_ATTEMPTS = 12
# An oversized section with subsections keeps at least this much room for its
# own opening text after the (capped) subsection list.
_MIN_OWN_TEXT_TOKENS = 150
# At most this many matching-but-not-shown sections are named in an envelope.
_MAX_UNSHOWN_MATCHES = 5
# The text before a document's first heading has no heading of its own; the
# reader answers to this name (and the aliases) for it, so a head the envelope
# had to cut is still reachable.
_INTRO_SECTION = "Introduction"
_INTRO_ALIASES = frozenset({"introduction", "intro", "head", "preamble"})
# When a single line does not fit, cut inside it only if at least this much
# room is left; below that a fragment says nothing useful.
_MIN_PARTIAL_LINE_TOKENS = 40

# Query words that say nothing about WHICH section is wanted. English function
# words, their Vietnamese counterparts (most real queries are Vietnamese), and
# words every document shares. The document's own name is added per call.
_STOP_WORDS = frozenset({
    "a", "an", "and", "any", "are", "as", "at", "be", "by", "can", "could",
    "do", "does", "for", "from", "how", "i", "if", "in", "into", "is", "it",
    "its", "me", "my", "of", "on", "or", "our", "should", "so", "that", "the",
    "their", "them", "then", "there", "these", "this", "to", "via", "was",
    "we", "what", "when", "where", "which", "who", "why", "will", "with",
    "would", "you", "your",
    "và", "của", "cho", "trên", "để", "là", "có", "không", "các", "những",
    "trong", "với", "một", "khi", "nào", "gì", "thế", "như", "được", "này",
    "đó", "bằng", "cách",
    "cremind", "doc", "docs", "document", "documentation", "section", "sections",
})


class Var:
    DEFAULT_TOP_K_KEY = "DEFAULT_TOP_K"


TOOL_CONFIG: ToolConfig = {
    "name": _TOOL_ID,
    "display_name": SERVER_NAME,
    "description": (
        "Semantically searches the user's local Markdown documentation library "
        "(skills, how-to guides, `cremind` CLI manual pages, user-added docs) and "
        "returns the single most relevant document, chosen by an internal LLM "
        "judge — whole when it is short, otherwise its head, a table of contents "
        "and the sections matching the query, so any other section can be read "
        "on demand. Try it first for any factual or how-to lookup before "
        "searching the public web. For a `cremind` CLI manual page, run the "
        "command it documents with the Shell Executor to get live answers "
        "instead of quoting the page."
    ),
    # Visible in Settings (so its top-k can be configured) but locked on —
    # the agent must always be able to search its own documentation.
    "locked": True,
    "required_config": {
        Var.DEFAULT_TOP_K_KEY: {
            "description": (
                "Maximum number of documents the vector store returns to "
                "the relevance judge for each search call. Ignored when Vector "
                "Embedding is off: the judge then reviews the whole shared "
                "library plus up to 50 of the profile's own documents."
            ),
            "type": "number",
            "default": DEFAULT_TOP_K,
        },
    },
}


class DocumentationSearchTool(BuiltInTool):
    name: str = SEARCH_LEAF_NAME
    description: str = (
        "Search Cremind's documentation and knowledge base by semantic query — "
        "skills, how-to guides, `cremind` CLI usage, and any documents the user "
        "has added. Given a natural-language question, this returns the single "
        f"most relevant document, or \"{NO_RESULT_MESSAGE}\" when nothing matches. "
        "A long document comes back as its head, a table of contents and the "
        "sections matching the question; its closing line says how to read any "
        "other section — follow it rather than repeating the search, which "
        "returns the same excerpt. When the result is a `cremind` CLI reference, "
        "follow its embedded agent directive: run the relevant command with the "
        "Exec Shell tool and report its live output instead of paraphrasing the "
        "document."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Natural-language description of what the user wants "
                    "to learn or build. Example: 'how to write a sample "
                    "skill for Cremind'."
                ),
            },
            "top_k": {
                "type": "integer",
                "description": (
                    "Maximum number of vector-search candidates the LLM "
                    "judge considers. Defaults to 10 and is capped at 20. "
                    "Ignored when semantic search is off."
                ),
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        # Searches the general (shared + per-profile) documentation corpus via
        # the shared vector-search + LLM-judge engine below.
        return await run_doc_search(arguments)


class ReadDocumentationSectionTool(BuiltInTool):
    name: str = SECTION_LEAF_NAME
    description: str = (
        "Read one section of a documentation document by name. Use it after "
        f"`{SEARCH_LEAF_FN}` returned a document's head and table of contents, to "
        "fetch the part you need instead of searching again. Pass the document "
        "exactly as the search result's closing line wrote it (e.g. \"[cli]cremind "
        "channels\", or a scope/path reference such as \"admin/guide.md\" when two "
        "documents share a name) and a heading from its table of contents; "
        "backticks, hyphens and a leading \"cremind\" are optional and matching is "
        "case-insensitive. Pass section=\"Introduction\" for the text before the "
        "first heading. Omit `section` to get the table of contents, or the whole "
        "document when it is short."
    )
    parameters: Dict[str, Any] = {
        "type": "object",
        "properties": {
            "document": {
                "type": "string",
                "description": (
                    "The document as the search result's closing line wrote it: "
                    "a name such as \"[cli]cremind channels\" (the bracketed tag "
                    "is optional), or a scope/path reference such as "
                    "\"admin/guide.md\"."
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    "A heading from the document's table of contents, e.g. "
                    "\"cremind channels add\". Omit it to get the table of "
                    "contents."
                ),
            },
        },
        "required": ["document"],
        "additionalProperties": False,
    }

    async def run(self, arguments: Dict[str, Any]) -> BuiltInToolResult:
        return await run_read_section(arguments)


async def run_doc_search(
    arguments: Dict[str, Any],
    *,
    scopes: Optional[List[str]] = None,
    log_label: str = "documentation_search",
) -> BuiltInToolResult:
    """Shared vector-search + LLM-judge pipeline for documentation-style tools.

    ``documentation_search`` calls this with the default scopes (shared + the
    active profile). ``scopes`` is a generic filter so callers can narrow the
    corpus if needed; ``log_label`` tags the diagnostic log lines and does not
    affect behaviour.
    """
    tag = f"[{log_label}]"
    query = (arguments.get("query") or "").strip()
    profile = arguments.get("_profile") or "admin"
    llm = arguments.get("_llm")

    variables = arguments.get("_variables") or {}
    try:
        default_top_k = int(variables.get(Var.DEFAULT_TOP_K_KEY) or DEFAULT_TOP_K)
    except (TypeError, ValueError):
        default_top_k = DEFAULT_TOP_K

    top_k = arguments.get("top_k") or default_top_k
    try:
        top_k = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        top_k = default_top_k

    if not query:
        return _no_result()

    service = get_service()
    if service is None:
        logger.warning(
            f"{tag} sync service not initialized; vector store unavailable"
        )
        return _no_result()

    try:
        hits = service.search(query=query, profile=profile, limit=top_k, scopes=scopes)
        logger.debug(f"vector search hits: {hits}")
    except Exception:  # noqa: BLE001
        logger.exception(f"{tag} vector search failed")
        return _no_result()

    # Which path ``search`` took (vector / one of the fallbacks). Read off the
    # service rather than returned, so ``search`` keeps its list-of-dicts shape.
    mode = getattr(service, "last_search_mode", None) or "unknown"

    if not hits:
        logger.info(
            f"{tag} query={query!r} ranked=[] decision=no-candidates mode={mode}"
        )
        return _no_result()

    # Lightweight candidates: name + description + file_path only. We
    # deliberately do NOT load bodies here -- the judge picks based on
    # description alone, and we read the body of the winner only.
    candidates: List[Dict[str, Any]] = []
    for hit in hits:
        file_path = hit.get("file_path")
        description = hit.get("text") or ""
        if not file_path or not description:
            continue
        candidates.append({
            "name": hit.get("name") or "",
            "description": description,
            "file_path": file_path,
            "scope": hit.get("scope"),
            "relpath": hit.get("relpath"),
            "score": hit.get("score"),
        })

    if not candidates:
        logger.info(
            f"{tag} query={query!r} ranked=[] decision=no-usable-candidates "
            f"mode={mode}"
        )
        return _no_result()

    if llm is None:
        logger.warning(
            f"{tag} no internal LLM available; cannot judge relevance, "
            "returning no-result"
        )
        return _no_result()

    chosen_index, judge_usage, judge_errored = await _select_best_candidate(
        llm=llm, query=query, candidates=candidates, log_label=log_label, mode=mode,
    )
    if chosen_index is None:
        # A judge *error* (e.g. the configured judge model is incompatible with
        # the provider's auth method) is reported distinctly from a genuine
        # no-match, so the reasoning agent doesn't read a broken judge as "no doc
        # matched". Never returns a document on error (the judge exists precisely
        # to reject off-topic top hits, so a blind top-1 fallback would be wrong).
        if judge_errored:
            return _judge_unavailable(token_usage=judge_usage)
        return _no_result(token_usage=judge_usage)

    chosen = candidates[chosen_index]
    body = service.read_body(Path(chosen["file_path"]))
    if body is None:
        # Source file disappeared between vector search and read.
        logger.warning(f"{tag} chosen file missing on disk: {chosen['file_path']}")
        return _no_result(token_usage=judge_usage)

    # Resolve $VAR system-variable tokens in the body for the active
    # profile (e.g. $CREMIND_SERVER, $CREMIND_PROFILE) — same syntax as
    # chat; `$$NAME` escapes to a literal `$NAME`. Done here at serving
    # time only, never during indexing, so resolved values never enter
    # the vector store or content hash.
    body = resolve_system_var_tokens(body, profile)

    # CLI reference docs describe live `cremind` commands. Prepend an execution
    # directive so a weak model runs the command via exec_shell and answers from
    # real output instead of paraphrasing the man page (and mistaking its example
    # tables for live data). Gated on exec_shell being callable for this profile
    # so we never instruct the impossible; prepended because long results are
    # head-truncated.
    name = chosen.get("name") or ""
    directive = _directive_for(name, profile)

    # A body over the delivery budget would be head-clipped by the agent, so it
    # is delivered as an envelope the agent can navigate instead. Under budget
    # it goes out exactly as before.
    budget = _delivery_budget(profile, directive)
    if budget is not None:
        body_tokens = _tokens(body)
        if body_tokens > budget:
            scope = chosen.get("scope") or SHARED_SCOPE
            body = _build_envelope(
                name=name,
                reference=_document_reference(
                    service, name=name, scope=scope,
                    relpath=chosen.get("relpath"), profile=profile,
                ),
                scope=scope,
                body=body,
                body_tokens=body_tokens,
                budget=budget,
                query=query,
                profile=profile,
                log_label=log_label,
            )

    # Kept as a single text item so the adapter's _extract_tool_result unwraps
    # it to the plain string the Reasoning Agent should see.
    return BuiltInToolResult(
        content=[{"type": "text", "text": directive + body}],
        token_usage=judge_usage,
    )


async def run_read_section(arguments: Dict[str, Any]) -> BuiltInToolResult:
    """Return one section of a document, resolved by document NAME.

    No embedding and no judge: the document is looked up by name within the
    caller's own ``[shared, profile]`` scopes and sliced deterministically at
    its headings. Never touches a path the agent supplied.
    """
    tag = "[documentation_search]"
    profile = arguments.get("_profile") or "admin"
    document = (arguments.get("document") or "").strip()
    wanted = (arguments.get("section") or "").strip()

    if not document:
        return _section_error(
            "MissingDocument",
            "Pass `document`: the document name exactly as the search result "
            "showed it, e.g. \"[cli]cremind channels\".",
        )

    service = get_service()
    if service is None:
        return _section_error(
            "ServiceUnavailable",
            "The documentation service is not initialized yet; try again shortly.",
        )

    scopes = [SHARED_SCOPE, profile]
    rows: Optional[List[Dict[str, Any]]] = None
    resolve = getattr(service, "resolve_document", None)
    if resolve is not None:
        rows = service.list_document_names(scopes)
        matches = resolve(document, scopes, rows=rows)
    else:  # a minimal service without ambiguity support
        found = service.find_document(document, scopes)
        matches = [found] if found else []

    if len(matches) > 1:
        references = [service.qualified_reference(r) for r in matches]
        logger.info(
            f"{tag} section doc={document!r} decision=ambiguous-document "
            f"matches={references}"
        )
        return _section_error(
            "AmbiguousDocument",
            f"{document!r} names {len(matches)} documents. Call again with "
            "`document` set to one of the candidates.",
            document=document,
            candidates=references,
        )
    if not matches:
        if rows is None:
            rows = service.list_document_names(scopes)
        names = [row["name"] for row in rows]
        logger.info(f"{tag} section doc={document!r} decision=document-not-found")
        return _section_error(
            "DocumentNotFound",
            f"No document named {document!r} in this profile's documentation. "
            "Use a name exactly as the search result showed it, or search again.",
            document=document,
            candidates=_close_names(document, names),
        )
    doc = matches[0]

    name = doc.get("name") or document
    doc_scope = doc.get("scope") or SHARED_SCOPE
    body = service.read_body(Path(doc["file_path"]))
    if body is None:
        return _section_error(
            "DocumentNotFound",
            f"Document {name!r} is no longer readable; search again.",
            document=name,
            candidates=[],
        )

    # Resolved BEFORE splitting, so headings, sizes and matching all see the
    # same text the agent will receive.
    body = resolve_system_var_tokens(body, profile)
    directive = _directive_for(name, profile)
    budget = _delivery_budget(profile, directive)
    lines, head_lines, sections = split_sections(body)
    sizes = size_sections(lines, sections, _tokens)

    if not wanted:
        text, decision = _render_document_overview(
            name=name, scope=doc_scope, body=body, lines=lines, sections=sections,
            sizes=sizes, budget=budget,
        )
        logger.info(
            f"{tag} section doc={name!r} scope={doc_scope} decision={decision} "
            f"budget={budget}"
        )
        return _text_result(directive + text)

    wants_intro = normalize_heading(wanted) in _INTRO_ALIASES
    if not sections:
        if wants_intro:
            return _text_result(directive + _render_intro(
                name=name, scope=doc_scope, head_lines=head_lines, budget=budget,
            ))
        return _section_error(
            "SectionNotFound",
            f"{name!r} has no sections; call again without `section` to read it.",
            document=name,
            section=wanted,
            candidates=[],
        )

    matches, suggestions = find_section(sections, wanted)
    if not matches and wants_intro:
        logger.info(f"{tag} section doc={name!r} scope={doc_scope} decision=introduction")
        return _text_result(directive + _render_intro(
            name=name, scope=doc_scope, head_lines=head_lines, budget=budget,
        ))
    if not matches:
        logger.info(
            f"{tag} section doc={name!r} section={wanted!r} decision=not-found"
        )
        return _section_error(
            "SectionNotFound",
            f"No section of {name!r} matches {wanted!r}. Call again with "
            "`section` set to one of the candidates, or omit it for the table "
            "of contents.",
            document=name,
            section=wanted,
            candidates=suggestions,
        )

    if len({normalize_heading(m.title) for m in matches}) > 1:
        logger.info(
            f"{tag} section doc={name!r} section={wanted!r} decision=ambiguous "
            f"matches={len(matches)}"
        )
        return _section_error(
            "AmbiguousSection",
            f"{wanted!r} matches several sections of {name!r}. Call again with "
            "`section` set to one of the candidates.",
            document=name,
            section=wanted,
            candidates=[m.title for m in matches],
        )

    target = matches[0]
    total = sizes[target.index].total
    note = (
        f"\n[note: {len(matches)} sections share this heading; showing the first]"
        if len(matches) > 1
        else ""
    )
    oversized = budget is not None and total > budget
    if oversized:
        text = _render_oversized_section(
            name=name, scope=doc_scope, target=target, lines=lines,
            sections=sections, sizes=sizes, budget=budget,
        )
    else:
        text = (
            f"[Section \"{target.title}\" of \"{name}\" ({doc_scope}) — "
            f"{total} tokens]{note}\n\n{section_text(lines, target)}"
        )
    logger.info(
        f"{tag} section doc={name!r} scope={doc_scope} section={target.title!r} "
        f"tokens={total} budget={budget} oversized={oversized}"
    )
    return _text_result(directive + text)


# ── Delivery helpers ────────────────────────────────────────────────────────


def _tokens(text: str) -> int:
    """Token count in the same encoding the agent's clamp uses.

    ``count_content_tokens`` needs tiktoken (an optional extra) and raises
    without it; fall back to the usual chars/4 estimate rather than failing a
    search over bookkeeping.
    """
    if not text:
        return 0
    try:
        return count_content_tokens(text)
    except Exception:  # noqa: BLE001
        return len(text) // 4


@dataclass(frozen=True)
class _ClampConfig:
    """The two ``tool_result`` settings the delivery budget depends on."""

    tool_result_enabled: bool
    tool_result_max_tokens: int


def _resolve_clamp(profile: str) -> _ClampConfig:
    """Read the profile's tool-result clamp — and only that.

    ``resolve_agent_config`` would read every agent setting, one synchronous
    config-store query each, on the event loop, for every search and section
    read; and a malformed setting that has nothing to do with delivery would
    make it raise. The ``tool_result`` group is the same source the reasoning
    agent's clamp reads.
    """
    group = resolve_group("tool_result", profile)
    return _ClampConfig(
        tool_result_enabled=bool(group["enabled"]),
        tool_result_max_tokens=int(group["max_tokens"]),
    )


def _agent_clamp_is_active() -> bool:
    """Whether the reasoning agent's tool-result clamp can cut anything at all.

    The clamp is ``truncate_to_tokens``, which hands text back UNCHANGED when
    tiktoken is not installed (it ships only in some optional extras). On such
    an install nothing is ever clipped, so enveloping a document would deliver
    less than the agent would have received whole.
    """
    return importlib.util.find_spec("tiktoken") is not None


def _delivery_budget(profile: str, reserved: str) -> Optional[int]:
    """Tokens a result body may use so the WHOLE result survives the clamp.

    Derived from the same per-profile ``tool_result.max_tokens`` the reasoning
    agent clamps with; ``reserved`` is the text that rides in front of the body
    (the CLI directive), measured rather than guessed. ``None`` means there is
    no effective clamp — the profile turned it off, or tiktoken is missing so
    the agent's clamp is a no-op — and bodies are delivered whole, exactly as
    the agent would have received them. The body budget is floored at
    :data:`_MIN_BUDGET_TOKENS`; see there for when that floor lets a result
    exceed a very small clamp.
    """
    if not _agent_clamp_is_active():
        return None
    try:
        cfg = _resolve_clamp(profile)
        if not cfg.tool_result_enabled:
            return None
        clamp = int(cfg.tool_result_max_tokens)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[documentation_search] could not resolve the agent config for "
            f"profile {profile!r}; assuming a {_FALLBACK_CLAMP_TOKENS}-token clamp"
        )
        clamp = _FALLBACK_CLAMP_TOKENS
    return max(_MIN_BUDGET_TOKENS, clamp - _tokens(reserved) - _BUDGET_MARGIN_TOKENS)


def _directive_for(name: str, profile: str) -> str:
    """The CLI execution directive for ``name``, or "" when it does not apply."""
    if not (name or "").startswith(_CLI_DOC_PREFIX):
        return ""
    fn = _exec_shell_fn(profile)
    if not fn:
        return ""
    return _CLI_EXECUTION_DIRECTIVE.format(fn=fn) + "\n"


def _stop_words_for(name: str) -> set[str]:
    """Generic stop words plus the document's own name, singular and plural.

    ``rank_sections_for_query`` matches stop words exactly, and every heading
    of ``[cli]cremind channels`` repeats "channels": without both forms, a
    query that says "channel" would match the entire table of contents. A
    hyphenated name contributes its parts too — every heading of
    ``[cli]cremind skill-events`` contains "skill-events", which a query
    saying "skill events" would otherwise prefix-match through "skill".
    """
    words = set(_STOP_WORDS)
    for word in _clean_name(name).casefold().split():
        for part in {word, *word.split("-")}:
            if not part:
                continue
            words.add(part)
            if part.endswith("s") and len(part) > 3:
                words.add(part[:-1])
            else:
                words.add(part + "s")
    return words


def _cut_tokens(text: str, max_tokens: int) -> str:
    """The longest prefix of ``text`` measuring at most ``max_tokens``.

    Measured with :func:`_tokens` — the one measure every budget in this
    module uses — by binary search over the character length, so a cut can
    never disagree with the budget it is meant to satisfy (whether tiktoken is
    installed or the chars/4 fallback is in force). Backs off to a word break
    when one is close.
    """
    if _tokens(text) <= max_tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tokens(text[:mid]) <= max_tokens:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    space = cut.rfind(" ")
    return cut[:space] if space > len(cut) - 40 else cut


def _fit_lines(text: str, cap: int, *, split_long_line: bool = True) -> Tuple[str, bool]:
    """Keep whole lines of ``text`` while they fit in ``cap`` tokens.

    Cutting at a line boundary keeps a table row or a command from being split
    mid-way. But when the first line that does not fit leaves real room behind
    (a paragraph written as one long line, a document with no line breaks),
    stopping there would deliver nothing at all, so that line is cut inside
    instead — unless ``split_long_line`` is False (a table of contents, where a
    half entry is worse than none). A code fence left open by the cut is
    closed, so whatever the caller appends does not read as part of it.
    Returns ``(text, was_cut)``.
    """
    if _tokens(text) <= cap:
        return text, False
    kept: List[str] = []
    used = 0
    for line in text.split("\n"):
        cost = _tokens(line) + 1
        if used + cost > cap:
            room = cap - used - 4  # the fence closer and the "…" marker
            if split_long_line and room >= _MIN_PARTIAL_LINE_TOKENS:
                kept.append(_cut_tokens(line, room).rstrip() + " …")
            break
        kept.append(line)
        used += cost
    closer = open_fence_at_end(kept)
    if closer:
        kept.append(closer)
    return "\n".join(kept).rstrip(), True


def _heading_label(title: str) -> str:
    """A heading as the agent should type it back: backticks dropped."""
    return title.replace("`", "")


def _render_toc_block(
    sections: List[Section], sizes: Dict[int, SectionSizes], cap: int,
) -> Tuple[str, str]:
    """The table of contents, shrunk to ``cap``. Returns ``(toc, note)``."""
    toc = render_toc(sections, sizes)
    note = ""
    if _tokens(toc) > cap:
        h2 = [s for s in sections if s.level == 2]
        if h2 and len(h2) < len(sections):
            toc = render_toc(h2, sizes)
            note = (
                f"({len(sections) - len(h2)} subsections not listed — read an "
                "H2 section to see its subsections.)"
            )
    if _tokens(toc) > cap:
        full_entries = toc.count("\n") + 1
        toc, _ = _fit_lines(toc, cap, split_long_line=False)
        shown = toc.count("\n") + 1 if toc else 0
        cut_note = (
            f"(table of contents cut to fit: {full_entries - shown} more entries "
            "not listed.)"
        )
        note = f"{note} {cut_note}" if note else cut_note
    return toc, note


def _document_reference(
    service, *, name: str, scope: str, relpath: Optional[str], profile: str,
) -> str:
    """How the envelope tells the agent to name THIS document to the reader.

    Its bare name normally; its ``scope/relpath`` reference when that name is
    shared with another document the reader would otherwise have to choose
    between (a profile doc named like a bundled one, two nested files with the
    same stem).
    """
    reference_for = getattr(service, "reference_for", None)
    if reference_for is None or not relpath:
        return name
    try:
        return reference_for(scope=scope, relpath=relpath, scopes=[SHARED_SCOPE, profile])
    except Exception:  # noqa: BLE001
        logger.exception("[documentation_search] could not build a document reference")
        return name


def _ranking_query(query: str, name: str) -> str:
    """``query`` without the words that only restate the document's own name.

    Stop words are matched exactly, but ranking prefix-matches, so
    "conversation" would still hit every `` `cremind conv …` `` heading through
    "conv" and fill the envelope with unrelated sections. Drop any query word
    that extends, or is extended by, a name word of four or more characters.
    """
    name_words = [w for w in query_words(_clean_name(name)) if len(w) >= 4]
    kept = [
        w for w in query_words(query)
        if not any(
            w.startswith(n) or (len(w) >= 4 and n.startswith(w)) for n in name_words
        )
    ]
    return " ".join(kept)


def _render_beginning(
    *, name: str, scope: str, body: str, body_tokens: int, budget: int,
) -> str:
    """A document over budget with no sections: as much of its beginning as
    fits. There is no table of contents to offer, so none is pretended."""
    header = (
        f"[Document \"{name}\" ({scope}) — {body_tokens} tokens, over the "
        f"{budget}-token budget and without sections, so only its beginning is "
        "shown]"
    )
    marker = "[… document truncated]"
    text, _ = _fit_lines(body, max(1, budget - _tokens(header) - _tokens(marker) - 8))
    return f"{header}\n\n{text}\n{marker}"


def _build_envelope(
    *,
    name: str,
    reference: str,
    scope: str,
    body: str,
    body_tokens: int,
    budget: int,
    query: str,
    profile: str,
    log_label: str,
) -> str:
    """Head + table of contents + query-matched sections, within ``budget``.

    ``reference`` is what the footer tells the agent to pass as ``document`` —
    see :func:`_document_reference`.
    """
    lines, head_lines, sections = split_sections(body)
    if not sections:
        logger.info(
            f"[{log_label}] envelope doc={name!r} scope={scope} "
            f"body_tokens={body_tokens} budget={budget} sections=0 -> beginning"
        )
        return _render_beginning(
            name=name, scope=scope, body=body, body_tokens=body_tokens, budget=budget,
        )

    sizes = size_sections(lines, sections, _tokens)
    section_fn = _section_leaf_fn(profile)
    ranked = rank_sections_for_query(
        sections, _ranking_query(query, name), _stop_words_for(name),
    )

    head_text, head_cut = _fit_lines(
        "\n".join(head_lines).strip("\n"), int(budget * _HEAD_SHARE),
    )
    toc_text, toc_note = _render_toc_block(sections, sizes, int(budget * _TOC_SHARE))

    header = (
        f"[Document \"{name}\" ({scope}) — {body_tokens} tokens, too long to "
        f"deliver whole within the {budget}-token budget. Shown below: its head, "
        "a table of contents with the token size of each section, and the "
        "section(s) whose heading matches the query.]"
    )
    head_marker = (
        f"[… head truncated — read all of it with section=\"{_INTRO_SECTION}\"]"
        if section_fn else "[… head truncated]"
    )

    def footer(included: List[Section]) -> str:
        if section_fn is None:
            return (
                f"[End of excerpt from \"{name}\". The section reader "
                f"(`{SECTION_LEAF_NAME}`) is disabled for this profile, so other "
                "sections cannot be fetched directly — a search whose query names "
                "another heading returns that section instead. The reader can be "
                "enabled in Settings → Tools & Skills → Documentation Search, or "
                f"with `cremind tools set-leaf {_TOOL_ID} {SECTION_LEAF_NAME}=true`.]"
            )
        example = _example_section(sections, ranked, included)
        example_hint = f", e.g. section=\"{example}\"" if example else ""
        return (
            f"[End of excerpt from \"{name}\". To read another section, call "
            f"`{section_fn}` with document=\"{reference}\" and section set to a "
            f"heading from the table of contents{example_hint}. Omit section to "
            "get the table of contents again. Repeating this search returns this "
            "same excerpt.]"
        )

    def listing(matches: List[Section]) -> str:
        return ", ".join(
            f"{_heading_label(s.title)} ({sizes[s.index].total})" for s in matches
        )

    def assemble(included: List[Section]) -> str:
        shown = {s.index for s in included}
        unshown = [s for s in ranked if s.index not in shown][:_MAX_UNSHOWN_MATCHES]
        parts = [header, "", head_text]
        if head_cut:
            parts.append(head_marker)
        parts += ["", "## Table of contents (section → tokens when read)", toc_text]
        if toc_note:
            parts.append(toc_note)
        parts += ["", "## Sections matching the query", ""]
        if included:
            for section in included:
                parts += [section_text(lines, section), ""]
            if unshown:
                parts += [f"(Also matching, not shown here: {listing(unshown)}.)", ""]
        elif ranked:
            # A heading DID match; it just does not fit. Saying "nothing
            # matched" here would contradict the footer's own suggestion.
            parts += [
                "(The section(s) matching the query are too long to include in "
                f"this excerpt: {listing(unshown)}. Read one on its own.)",
                "",
            ]
        else:
            parts += [
                "(No section heading matched the query. Pick one from the table "
                "of contents above.)",
                "",
            ]
        parts.append(footer(included))
        return "\n".join(parts)

    included: List[Section] = []
    text = assemble(included)
    for section in ranked[:_MAX_SECTION_ATTEMPTS]:
        if sizes[section.index].total > budget:
            continue
        candidate = assemble(included + [section])
        if _tokens(candidate) <= budget:
            included.append(section)
            text = candidate

    logger.info(
        f"[{log_label}] envelope doc={name!r} scope={scope} "
        f"body_tokens={body_tokens} budget={budget} "
        f"head_tokens={_tokens(head_text)} head_cut={head_cut} "
        f"toc_entries={len(sections)} "
        f"matched={[s.title for s in included]} delivered={_tokens(text)}"
    )
    return text


def _example_section(
    sections: List[Section], ranked: List[Section], included: List[Section],
) -> str:
    """A heading to show in the footer as the argument format: not already
    shown, preferably the next-best match, then a command-reference heading
    (one that opens with a backticked command), then any subsection."""
    shown = {s.index for s in included}
    pools = (
        ranked,
        [s for s in sections if s.level == 3 and s.title.startswith("`")],
        [s for s in sections if s.level == 3],
        sections,
    )
    for pool in pools:
        for section in pool:
            if section.index not in shown:
                return _heading_label(section.title)
    return ""


def _render_intro(
    *, name: str, scope: str, head_lines: List[str], budget: Optional[int],
) -> str:
    """The text before a document's first heading — the part no heading names,
    and the one an envelope may have had to cut."""
    head = "\n".join(head_lines).strip("\n")
    head_tokens = _tokens(head)
    header = (
        f"[{_INTRO_SECTION} of \"{name}\" ({scope}) — the text before its first "
        f"section — {head_tokens} tokens]"
    )
    if budget is not None and head_tokens > budget:
        head, _ = _fit_lines(head, max(1, budget - _tokens(header) - 20))
        head += "\n[… introduction truncated]"
    return f"{header}\n\n{head}"


def _render_document_overview(
    *,
    name: str,
    scope: str,
    body: str,
    lines: List[str],
    sections: List[Section],
    sizes: Dict[int, SectionSizes],
    budget: Optional[int],
) -> Tuple[str, str]:
    """``read_documentation_section`` without a section: the whole document
    when it fits, else its table of contents. Returns ``(text, decision)``."""
    body_tokens = _tokens(body)
    if budget is None or body_tokens <= budget:
        return (
            f"[Document \"{name}\" ({scope}) — {body_tokens} tokens, complete]"
            f"\n\n{body}",
            "whole",
        )
    if not sections:
        return (
            _render_beginning(
                name=name, scope=scope, body=body, body_tokens=body_tokens,
                budget=budget,
            ),
            "truncated",
        )
    header = (
        f"[Table of contents of \"{name}\" ({scope}) — {body_tokens} tokens in "
        f"total. Read a section with `{SECTION_LEAF_FN}` and section set to one "
        "of these headings; the number is what reading it costs.]"
    )
    toc, note = _render_toc_block(sections, sizes, budget - _tokens(header) - 40)
    parts = [header, "", toc]
    if note:
        parts.append(note)
    return "\n".join(parts), "toc"


def _render_oversized_section(
    *,
    name: str,
    scope: str,
    target: Section,
    lines: List[str],
    sections: List[Section],
    sizes: Dict[int, SectionSizes],
    budget: int,
) -> str:
    """A section too big for the budget: its own text plus its subsections,
    or — when it has none — its beginning."""
    total = sizes[target.index].total
    kids = children(sections, target)
    if kids:
        header = (
            f"[Section \"{target.title}\" of \"{name}\" ({scope}) — {total} tokens, "
            f"over the {budget}-token budget. Showing its own text and a list of "
            f"its subsections; read any of them with `{SECTION_LEAF_FN}`.]"
        )
        # The subsection list is what the agent navigates by, so it gets its
        # room first — capped, like every other list here, so a section with
        # hundreds of children cannot push the result past the clamp — and the
        # section's own prose gets what is left.
        sub_toc, sub_note = _render_toc_block(
            kids, sizes, max(1, budget - _tokens(header) - _MIN_OWN_TEXT_TOKENS),
        )
        subsections = "## Subsections (section → tokens when read)\n" + sub_toc
        if sub_note:
            subsections += "\n" + sub_note
        room = budget - _tokens(header) - _tokens(subsections) - 40
        own, cut = _fit_lines(section_own_text(lines, target), max(0, room))
        own = own + ("\n[… own text truncated]" if cut else "")
        return f"{header}\n\n{own}\n\n{subsections}"

    header = (
        f"[Section \"{target.title}\" of \"{name}\" ({scope}) — {total} tokens, "
        f"over the {budget}-token budget and without subsections, so only its "
        "beginning is shown]"
    )
    text, _ = _fit_lines(
        section_text(lines, target), max(1, budget - _tokens(header) - 60),
    )
    return (
        f"{header}\n\n{text}\n[… section truncated. Ask the user to narrow the "
        "question, or read a neighbouring section.]"
    )


def _close_names(query: str, names: List[str], limit: int = 5) -> List[str]:
    """Document names close to ``query``, by raw and by tag-stripped name."""
    if not names:
        return []
    by_raw = {n.casefold(): n for n in names}
    by_clean = {_clean_name(n).casefold(): n for n in names}
    out: List[str] = []
    for key in difflib.get_close_matches(query.casefold(), list(by_raw), n=limit, cutoff=0.4):
        out.append(by_raw[key])
    for key in difflib.get_close_matches(
        _clean_name(query).casefold(), list(by_clean), n=limit, cutoff=0.4,
    ):
        if by_clean[key] not in out:
            out.append(by_clean[key])
    return out[:limit] if out else names[:20]


def _text_result(text: str) -> BuiltInToolResult:
    return BuiltInToolResult(content=[{"type": "text", "text": text}], token_usage=None)


def _section_error(code: str, message: str, **extra: Any) -> BuiltInToolResult:
    """Structured error for the section reader (see ``base.py``: error + message)."""
    return BuiltInToolResult(
        structured_content={"error": code, "message": message, **extra},
        token_usage=None,
    )


def _section_leaf_fn(profile: str) -> Optional[str]:
    """Exposed name of the section-reader leaf, or ``None`` when the profile
    disabled it (so the envelope never points at a function that is gone).

    Unlike :func:`_exec_shell_fn` this fails OPEN when the registry cannot be
    consulted (early boot, unit tests): the reader ships in this very group, so
    whenever search is running it is registered, and only an explicit per-leaf
    disable — which needs a registry — can take it away.
    """
    try:
        from app.tools.registry import get_tool_registry

        payload = get_tool_registry().leaves_for_profile(profile, _TOOL_ID)
    except Exception:  # noqa: BLE001 - registry unavailable / group unregistered
        return SECTION_LEAF_FN
    for leaf in payload.get("leaves", []):
        if leaf.get("leaf_name") == SECTION_LEAF_NAME:
            return SECTION_LEAF_FN if leaf.get("enabled") else None
    return SECTION_LEAF_FN


def _exec_shell_fn(profile: str) -> Optional[str]:
    """Exposed function name of the exec_shell run-command leaf, or ``None`` when
    it isn't callable for ``profile`` (so we never tell the model to run a command
    it can't).

    exec_shell is ``locked`` (its group can't be disabled), but registration can
    fail (leaving a stub group with no real leaf) and the per-leaf API path can
    disable the ``exec_shell`` leaf — both are reflected by
    ``leaves_for_profile``'s per-leaf ``enabled`` flags. Lazy imports avoid a
    builtin<->registry cycle and make the registry easy to monkeypatch in tests;
    any failure (registry not initialized in unit tests / early boot) degrades to
    ``None`` silently. The name is derived via ``make_leaf_name`` — the same way
    the reasoning agent derives it — so a rename re-derives instead of drifting.
    """
    try:
        from app.tools.base import make_leaf_name
        from app.tools.registry import get_tool_registry

        registry = get_tool_registry()
        payload = registry.leaves_for_profile(profile, "exec_shell")
    except Exception:  # noqa: BLE001 - registry unavailable / group unregistered
        return None
    leaf_enabled = any(
        leaf.get("leaf_name") == "exec_shell" and leaf.get("enabled")
        for leaf in payload.get("leaves", [])
    )
    if not leaf_enabled:
        return None
    return make_leaf_name("exec_shell", "exec_shell")


def _no_result(token_usage: Optional[Dict[str, int]] = None) -> BuiltInToolResult:
    return BuiltInToolResult(
        structured_content={
            "message": NO_RESULT_MESSAGE,
            "relevant": False,
        },
        token_usage=token_usage,
    )


def _judge_unavailable(token_usage: Optional[Dict[str, int]] = None) -> BuiltInToolResult:
    """Distinct result for when the relevance judge LLM *errored* (as opposed to a
    genuine no-match). Carries ``error: True`` so the reasoning agent treats it as
    a transient/config failure to surface or retry — not as a definitive "no
    documentation matched". Never returns a document body.
    """
    return BuiltInToolResult(
        structured_content={
            "message": (
                "Documentation search is temporarily unavailable: the relevance "
                "judge could not run (check the 'low' model group is compatible "
                "with the active LLM provider auth method)."
            ),
            "relevant": False,
            "error": True,
        },
        token_usage=token_usage,
    )


async def _select_best_candidate(
    *,
    llm,
    query: str,
    candidates: List[Dict[str, Any]],
    log_label: str = "documentation_search",
    mode: str = "unknown",
) -> Tuple[Optional[int], Dict[str, int], bool]:
    """Run the LLM judge over ``candidates`` and return ``(index, token_usage,
    errored)``.

    The judge MUST decide by calling one of two function tools:
    ``select_document(index)`` or ``no_relevant_result()``. The first element is
    the chosen 0-based index, or ``None`` for no-match / parse-failure / LLM-error
    cases (the caller turns ``None`` into the standard "no relevant result found"
    response). The second element is the four-way token usage the judge call
    consumed — captured off the terminal ``DONE`` chunk and returned in every case
    (even no-match / error) so the caller can attribute the cost; all-zero when the
    call errored before producing usage. The third element is ``True`` only when
    the judge LLM call itself *errored* (e.g. the configured judge model is
    incompatible with the provider's auth method) — as opposed to a legitimate
    no-match — so the caller can tell a broken judge apart from "no doc matched"
    instead of reporting both as the same "no relevant result found".

    ``mode`` is the retrieval path the candidates came from (``vector`` or a
    fallback); it only rides the summary log line.
    """
    judge_tools = _build_judge_tools(num_candidates=len(candidates))
    user_message = _format_judge_prompt(query=query, candidates=candidates)
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    logger.debug(f"judge_tools: {judge_tools}")
    logger.debug(f"user_message: {user_message}")
    function_calls: List[Dict[str, Any]] = []
    token_usage: Dict[str, int] = done_chunk_token_usage({})

    # One always-on INFO summary per search, emitted at every return path below.
    # Lets us tell a ranking miss (target doc never reached top-K) from a judge
    # miss (target ranked but rejected) without dumping bodies or descriptions,
    # and — via mode — a ranked search from a degraded full scan.
    def _fmt_score(v: Any) -> str:
        return f"{v:.4f}" if isinstance(v, (int, float)) else "n/a"

    ranked = ", ".join(
        f"{c.get('name', '')}={_fmt_score(c.get('score'))}" for c in candidates
    )

    def _log(decision: str) -> None:
        logger.info(
            f"[{log_label}] query={query!r} ranked=[{ranked}] "
            f"decision={decision} mode={mode}"
        )

    try:
        async for response in llm.chat_completion(
            messages=messages,
            tools=judge_tools,
            tool_choice="auto",
            # Relevance judging is a deterministic single-pick classification;
            # sampling variance (temperature > 0) is pure downside here — it can
            # flip a borderline-correct pick to no_relevant_result() on retry.
            temperature=0,
        ):
            logger.debug(f"judge response: {response}")
            rtype = response.get("type")
            if rtype == ChatCompletionTypeEnum.FUNCTION_CALLING:
                data = response.get("data")
                if isinstance(data, dict) and data.get("function"):
                    function_calls = data["function"]
            elif rtype == ChatCompletionTypeEnum.DONE:
                token_usage = done_chunk_token_usage(response)
                break
    except Exception:  # noqa: BLE001
        logger.exception("[documentation_search] judge LLM call failed")
        _log("error:judge-llm-failed")
        return None, token_usage, True

    if not function_calls:
        logger.warning(
            "[documentation_search] judge produced no tool call; "
            "treating as no-match"
        )
        _log("no-match:no-tool-call")
        return None, token_usage, False

    call = function_calls[0]
    name = call.get("name") or ""
    args = call.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            args = {}

    if name == _NO_MATCH_TOOL_NAME:
        _log("no_relevant_result")
        return None, token_usage, False

    if name == _SELECT_TOOL_NAME:
        raw_index = args.get("index") if isinstance(args, dict) else None
        try:
            idx = int(raw_index)
        except (TypeError, ValueError):
            logger.warning(
                f"[documentation_search] judge passed non-integer index: "
                f"{raw_index!r}"
            )
            _log(f"no-match:non-integer-index:{raw_index!r}")
            return None, token_usage, False
        if 0 <= idx < len(candidates):
            _log(f"select:{candidates[idx].get('name', '')}[{idx}]")
            return idx, token_usage, False
        logger.warning(
            f"[documentation_search] judge index {idx} out of range "
            f"(have {len(candidates)} candidates)"
        )
        _log(f"no-match:index-out-of-range:{idx}")
        return None, token_usage, False

    logger.warning(
        f"[documentation_search] judge called unknown tool {name!r}; "
        "treating as no-match"
    )
    _log(f"no-match:unknown-tool:{name!r}")
    return None, token_usage, False


def _build_judge_tools(*, num_candidates: int) -> List[Dict[str, Any]]:
    """Construct the OpenAI-style function-calling schema for the judge.

    Two tools, mutually exclusive: ``select_document(index)`` for picking a
    candidate by its 0-based index, and ``no_relevant_result()`` for the
    no-match case. The ``index`` parameter is range-bounded to keep the
    LLM honest.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": _SELECT_TOOL_NAME,
                "description": (
                    "Select the candidate document that best answers the "
                    "user's query."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": (
                                f"Zero-based index of the chosen candidate "
                                f"(0 to {max(0, num_candidates - 1)})."
                            ),
                            "minimum": 0,
                            "maximum": max(0, num_candidates - 1),
                        },
                    },
                    "required": ["index"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": _NO_MATCH_TOOL_NAME,
                "description": (
                    "Call this when no candidate plausibly answers the "
                    "user's query."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        },
    ]


def _judge_description(description: str) -> str:
    """A candidate's description as the judge sees it: at most
    :data:`DESCRIPTION_MAX_CHARS`, the same cap the embedder uses.

    Bounds the judge prompt in fallback mode, where EVERY document is a
    candidate and user-authored descriptions have no size limit of their own.
    Bundled descriptions all fit (a test pins it), so this only ever trims a
    profile's own over-long docs.
    """
    if len(description) <= DESCRIPTION_MAX_CHARS:
        return description
    return description[:DESCRIPTION_MAX_CHARS].rstrip() + "…"


def _format_judge_prompt(*, query: str, candidates: List[Dict[str, Any]]) -> str:
    """Render the judge's user prompt with the query and numbered candidates.

    Bodies are intentionally NOT included -- the judge picks on name +
    description only. Each description is capped by :func:`_judge_description`.
    """
    lines = [f"User query: {query}", "", "Candidates:"]
    for i, cand in enumerate(candidates):
        lines.append(f"[{i}] name: {cand.get('name', '')}")
        lines.append(f"    description: {_judge_description(cand.get('description', ''))}")
    lines.append("")
    lines.append(
        f"Decide by calling `{_SELECT_TOOL_NAME}` with the chosen index, "
        f"or `{_NO_MATCH_TOOL_NAME}` if nothing is on-topic."
    )
    return "\n".join(lines)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server: search, and the section reader."""
    return [DocumentationSearchTool(), ReadDocumentationSectionTool()]
