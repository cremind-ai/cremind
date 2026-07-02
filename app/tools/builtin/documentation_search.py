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

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.constants import ChatCompletionTypeEnum
from app.documents import get_service
from app.lib.llm.base import done_chunk_token_usage
from app.tools.builtin.base import BuiltInTool, BuiltInToolResult
from app.types import ToolConfig
from app.utils.logger import logger
from app.utils.message_tokens import resolve_system_var_tokens


SERVER_NAME = "Documentation Search"

DEFAULT_TOP_K = 10

NO_RESULT_MESSAGE = "no relevant result found"


_JUDGE_SYSTEM_PROMPT = (
    "You are the Documentation Search relevance judge. Vector search has "
    "returned a numbered list of candidate documents. Each candidate has a "
    "short name and a one-sentence description -- you do NOT see the body.\n"
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


class Var:
    DEFAULT_TOP_K_KEY = "DEFAULT_TOP_K"


TOOL_CONFIG: ToolConfig = {
    "name": "documentation_search",
    "display_name": SERVER_NAME,
    # Visible in Settings (so its top-k can be configured) but locked on —
    # the agent must always be able to search its own documentation.
    "locked": True,
    "required_config": {
        Var.DEFAULT_TOP_K_KEY: {
            "description": (
                "Maximum number of documents the vector store returns to "
                "the relevance judge for each search call."
            ),
            "type": "number",
            "default": DEFAULT_TOP_K,
        },
    },
}


class DocumentationSearchTool(BuiltInTool):
    name: str = "search_documentation"
    description: str = (
        "Search Cremind's documentation and knowledge base by semantic query — "
        "skills, how-to guides, `cremind` CLI usage, and any documents the user "
        "has added. Given a natural-language question, this returns the body of "
        f"the single most relevant document, or \"{NO_RESULT_MESSAGE}\" when "
        "nothing matches. Use it to look things up or answer questions about how "
        "Cremind and its skills work."
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
                    "judge considers. Defaults to 10 and is capped at 20."
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

    if not hits:
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
            "score": hit.get("score"),
        })

    if not candidates:
        return _no_result()

    if llm is None:
        logger.warning(
            f"{tag} no internal LLM available; cannot judge relevance, "
            "returning no-result"
        )
        return _no_result()

    chosen_index, judge_usage = await _select_best_candidate(
        llm=llm, query=query, candidates=candidates, log_label=log_label,
    )
    if chosen_index is None:
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

    # Return the body of the chosen document. The adapter's
    # _extract_tool_result unwraps a single text content item to a
    # plain string, which is what the Reasoning Agent should see.
    return BuiltInToolResult(
        content=[{"type": "text", "text": body}],
        token_usage=judge_usage,
    )


def _no_result(token_usage: Optional[Dict[str, int]] = None) -> BuiltInToolResult:
    return BuiltInToolResult(
        structured_content={
            "message": NO_RESULT_MESSAGE,
            "relevant": False,
        },
        token_usage=token_usage,
    )


async def _select_best_candidate(
    *,
    llm,
    query: str,
    candidates: List[Dict[str, Any]],
    log_label: str = "documentation_search",
) -> Tuple[Optional[int], Dict[str, int]]:
    """Run the LLM judge over ``candidates`` and return ``(index, token_usage)``.

    The judge MUST decide by calling one of two function tools:
    ``select_document(index)`` or ``no_relevant_result()``. The first element is
    the chosen 0-based index, or ``None`` for no-match / parse-failure / LLM-error
    cases (the caller turns ``None`` into the standard "no relevant result found"
    response). The second element is the four-way token usage the judge call
    consumed — captured off the terminal ``DONE`` chunk and returned in every case
    (even no-match / error) so the caller can attribute the cost; all-zero when the
    call errored before producing usage.
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
    # miss (target ranked but rejected) without dumping bodies or descriptions.
    def _fmt_score(v: Any) -> str:
        return f"{v:.4f}" if isinstance(v, (int, float)) else "n/a"

    ranked = ", ".join(
        f"{c.get('name', '')}={_fmt_score(c.get('score'))}" for c in candidates
    )

    def _log(decision: str) -> None:
        logger.info(
            f"[{log_label}] query={query!r} ranked=[{ranked}] "
            f"decision={decision}"
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
        return None, token_usage

    if not function_calls:
        logger.warning(
            "[documentation_search] judge produced no tool call; "
            "treating as no-match"
        )
        _log("no-match:no-tool-call")
        return None, token_usage

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
        return None, token_usage

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
            return None, token_usage
        if 0 <= idx < len(candidates):
            _log(f"select:{candidates[idx].get('name', '')}[{idx}]")
            return idx, token_usage
        logger.warning(
            f"[documentation_search] judge index {idx} out of range "
            f"(have {len(candidates)} candidates)"
        )
        _log(f"no-match:index-out-of-range:{idx}")
        return None, token_usage

    logger.warning(
        f"[documentation_search] judge called unknown tool {name!r}; "
        "treating as no-match"
    )
    _log(f"no-match:unknown-tool:{name!r}")
    return None, token_usage


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


def _format_judge_prompt(*, query: str, candidates: List[Dict[str, Any]]) -> str:
    """Render the judge's user prompt with the query and numbered candidates.

    Bodies are intentionally NOT included -- the judge picks based on the
    description only. Truncating descriptions is unnecessary because they
    are already a single sentence per the `.md` frontmatter contract.
    """
    lines = [f"User query: {query}", "", "Candidates:"]
    for i, cand in enumerate(candidates):
        lines.append(f"[{i}] name: {cand.get('name', '')}")
        lines.append(f"    description: {cand.get('description', '')}")
    lines.append("")
    lines.append(
        f"Decide by calling `{_SELECT_TOOL_NAME}` with the chosen index, "
        f"or `{_NO_MATCH_TOOL_NAME}` if nothing is on-topic."
    )
    return "\n".join(lines)


def get_tools(config: dict) -> list[BuiltInTool]:
    """Return tool instances for this server."""
    return [DocumentationSearchTool()]
