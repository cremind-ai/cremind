from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, List

from a2a.types import (
    Role,
    Message
)

from app.lib.embedding import LocalEmbeddings
from app.types import EmbeddingTable, ToolEmbeddingRecord
from app.utils import logger

# Heavy SDK imports are deferred: openai (LLM SDK), pandas
# (`embeddings-me5`/`embeddings-gemma` extras), tiktoken (`tokenization`
# extras), and numpy (transitively pulled by sentence-transformers in
# the embeddings extras) are not in the core install. Only modules that
# actually call into them need the import.
if TYPE_CHECKING:
    import numpy as np
    from openai.types.chat import ChatCompletionMessageParam


EMBEDDING_TABLE_COLUMNS = ['id', 'text', 'embeddings', 'tool_id', 'name', 'tool_type', 'enabled']


def generate_embeddings(embedding_vendor: LocalEmbeddings, text):
    """Generates embeddings for the given text using the in-process embedding model.

    Args:
        text (str): The input text to generate embeddings for.
    Returns:
        List[float]: A list of floats representing the generated embeddings.
    """
    embedding = embedding_vendor.embed_query(text)
    return embedding


def build_table_embeddings(
    embedding_vendor: LocalEmbeddings,
    data: dict[str, ToolEmbeddingRecord],
) -> EmbeddingTable:
    """Generates embeddings for the given record dictionary and returns an EmbeddingTable.

    Args:
        data: Dict keyed by ``tool_id``; each value is a ``ToolEmbeddingRecord``
            carrying ``text`` plus filter metadata (``tool_type``, ``name``,
            ``enabled``).  Callers that only have ``{id: text}`` should normalise
            upstream (see ``app/vectorstores/cache.py``).

    Returns:
        EmbeddingTable wrapping a DataFrame with columns
        ``id, text, embeddings, tool_id, name, tool_type, enabled``.
    """
    # pandas ships with the ``embeddings-me5`` / ``embeddings-gemma``
    # extras groups, which the Setup Wizard installs whenever the user
    # enables Vector Embedding. Lazy-imported so the core install
    # doesn't drag in the wheel.
    import pandas as pd

    logger.info('Generating Embeddings for provided data')
    try:
        if data:
            rows = [
                {
                    'id': key,
                    'text': rec['text'],
                    'tool_id': rec['tool_id'],
                    'name': rec['name'],
                    'tool_type': rec['tool_type'],
                    'enabled': bool(rec['enabled']),
                }
                for key, rec in data.items()
            ]
            df = pd.DataFrame(rows)
            df['embeddings'] = df.apply(
                lambda row: generate_embeddings(embedding_vendor, row['text']),
                axis=1,
            )
            logger.info('Done generating embeddings for provided data')
            return EmbeddingTable(df)
        else:
            logger.info('No data provided, returning empty EmbeddingTable')
            empty_df = pd.DataFrame(columns=EMBEDDING_TABLE_COLUMNS)
            return EmbeddingTable(empty_df)
    except Exception as e:
        logger.error(f'An unexpected error occurred : {e}.', exc_info=True)
        empty_df = pd.DataFrame(columns=EMBEDDING_TABLE_COLUMNS)
        return EmbeddingTable(empty_df)


def find_similar_item(query: str, embedding_vendor: LocalEmbeddings, embedding_table: EmbeddingTable) -> str:
    """Find the most similar item to the query in the embedding table.

    Args:
        query: The search query text
        embedding_vendor: The embedding model to use
        embedding_table: The EmbeddingTable to search in

    Returns:
        The ID of the most similar item
    """
    import numpy as np
    df = embedding_table.dataframe
    query_embedding = embedding_vendor.embed_query(query)
    dot_products = np.dot(
        np.stack(df['embeddings'].tolist()), query_embedding
    )
    best_match_index = np.argmax(dot_products)
    logger.debug(
        f'Found best match at index {best_match_index} with score {dot_products[best_match_index]}'
    )
    return df.iloc[best_match_index]['id']


def find_similar_items(
        query: str,
        embedding_vendor: LocalEmbeddings,
        embedding_table: EmbeddingTable,
        limit: int = 5) -> list[str]:
    """Find the top N most similar items to the query in the embedding table.

    Args:
        query: The search query text
        embedding_vendor: The embedding model to use
        embedding_table: The EmbeddingTable to search in
        limit: Maximum number of results to return (default: 5)

    Returns:
        List of IDs of the most similar items, ordered by similarity
    """
    import numpy as np
    df = embedding_table.dataframe
    query_embedding = embedding_vendor.embed_query(query)
    dot_products = np.dot(
        np.stack(df['embeddings'].tolist()), query_embedding
    )
    # Get indices sorted by highest dot product (best matches first)
    sorted_indices = np.argsort(dot_products)[::-1]
    # Limit the number of results
    top_indices = sorted_indices[:limit]

    logger.debug(
        f'Found {len(top_indices)} matches with scores: {[dot_products[i] for i in top_indices]}'
    )
    return [df.iloc[i]['id'] for i in top_indices]


def convert_task_history_to_messages(task_history: list[Message]) -> list[ChatCompletionMessageParam]:
    """Convert task history to ChatCompletionMessageParam format"""
    # ChatCompletionMessageParam is a TypedDict at the openai-SDK level —
    # at runtime it's just a dict, so no SDK import is needed here.
    messages: list[ChatCompletionMessageParam] = []

    for message in task_history:
        # Extract text content from message parts
        content_parts = []
        if hasattr(message, 'parts') and message.parts:
            for part in message.parts:
                if hasattr(part, 'root') and hasattr(part.root, 'text'):
                    content_parts.append(part.root.text)

        content = " ".join(content_parts) if content_parts else ""

        # Convert role: agent -> assistant, keep user as user
        if hasattr(message, 'role'):
            if message.role == Role.agent:
                role = "assistant"
            elif message.role == Role.user:
                role = "user"
            else:
                role = "user"  # fallback
        else:
            role = "user"  # fallback

        if content.strip():  # Only add messages with content
            if role == "assistant":
                messages.append({
                    "role": "assistant",
                    "content": content,
                })
            else:  # user role
                messages.append({
                    "role": "user",
                    "content": content,
                })

    return messages


def _is_silent_answer(entry: object) -> bool:
    """Whether one replayed trace entry is a group turn's ``[silent]`` decision.

    Only a plain assistant answer counts: a tool call is work the turn really
    did, and the trace has to keep it even when the turn ended up saying nothing.
    """
    if not isinstance(entry, dict):
        return False
    if entry.get("role") != "assistant" or entry.get("tool_calls"):
        return False
    from app.groups.render import is_silent

    content = entry.get("content")
    return isinstance(content, str) and is_silent(content)


def convert_db_messages_to_history(
    db_messages: list[dict],
    *,
    include_reasoning: bool = False,
) -> list[ChatCompletionMessageParam]:
    """Convert database message dicts to ChatCompletionMessageParam format.

    The model receives each message's ORIGINAL content verbatim — no injected
    ``message_id``/``conversation_id``/``summary`` suffixes (those were ReAct-era
    aids for the now-removed ``message_detail`` tool and trace summarizer, and
    they would also bust the prompt cache).

    When ``include_reasoning`` is set and a message carries a stored ``llm_messages``
    trace (assistant ``tool_calls`` + ``role:"tool"`` results + the final-answer
    assistant message), that native trace is spliced in verbatim **in place of** the
    single content message — so later turns resume the real tool-use transcript and
    the prompt-cache prefix covers the prior reasoning. The trace already ends with
    the final answer, so it is not duplicated. Messages without a trace (older rows,
    or turns with no tool calls) fall back to the content-only form.

    A user message that arrived mid-turn carries ``metadata.mid_turn.state`` and is
    filtered on it, so that exactly one copy of it reaches the model:

    ``pending``
        Persisted and parked; the turn that will speak for it has not finished.
        Excluded UNCONDITIONALLY — including it here would double-feed the message
        to a turn that is also about to receive it by injection.
    ``consumed``
        Injected into a turn whose trace was persisted. Excluded when the trace
        replays (it is in there verbatim, cache-alignedly); included when reasoning
        replay is off, since then nothing else carries it.
    ``released``
        Never injected (or the turn died first); it runs as an ordinary user row.

    A **group-chat seat** adds one rule, keyed off ``metadata.group`` so no caller
    has to opt in and ordinary conversations are untouched: *turns that chose to
    stay silent are dropped.* In a group every member is asked about every
    message, so most turns end in the ``[silent]`` sentinel. A history of
    ``user / "[silent]" / user / "[silent]"`` teaches the model that silence is
    the house style and it stops answering altogether — so the rows stay in the
    database (the room's own record of who was asked) but never reach the model.

    Consecutive group posts are left as SEPARATE messages. They used to be joined
    into one, on the belief that back-to-back user turns are a shape some
    providers reject — which is not true of any provider this code talks to, and
    the joining cost a seat its prompt cache. Merging rewrote a message that had
    already been sent: dropping a silent turn leaves the posts on either side
    adjacent, so the merged block grew on every turn and the cached prefix
    diverged at it. Since silence is the *common* outcome in a room, a mostly
    quiet seat re-paid for its whole transcript every turn. (Anthropic already
    receives consecutive ``user`` messages on every parallel-tool step — each
    ``tool_result`` is its own user message — and Gemini goes through Google's
    OpenAI-compatible shim, which folds roles server-side.)
    """
    messages: list[ChatCompletionMessageParam] = []
    for m in db_messages:
        # UI-only messages (e.g. rejected skill-event triggers the matching gate
        # filtered out) are shown in the conversation but must never enter the
        # model's context — the agent has no knowledge of them. This is the single
        # chokepoint every history-building path routes through.
        metadata = m.get("metadata") or {}
        if metadata.get("ui_only"):
            continue
        mid_turn_state = (metadata.get("mid_turn") or {}).get("state")
        if mid_turn_state == "pending":
            continue
        if mid_turn_state == "consumed" and include_reasoning:
            continue
        trace = m.get("llm_messages") if include_reasoning else None
        # Either kind of room stamps the outcome of a turn that said nothing:
        # ``group`` for a Cremind seat, ``channel_group`` for a platform group.
        room_stamp = metadata.get("group") or metadata.get("channel_group") or {}
        if room_stamp.get("kind") == "silent":
            # Drop the sentinel, KEEP the rest of the turn. A turn that stayed
            # silent may still have absorbed group posts mid-flight, and those
            # rows are marked ``consumed`` — meaning this trace is the only copy
            # of them left. Dropping the row wholesale would erase what the agent
            # was told, which in a room is most of what it hears.
            kept = [t for t in (trace or []) if not _is_silent_answer(t)]
            if kept:
                messages.extend(kept)
            continue
        if trace:
            messages.extend(trace)
            continue
        role = "assistant" if m["role"] == "agent" else m["role"]
        content = m.get("content") or ""
        if role == "user":
            content = _append_file_part_paths(content, m.get("parts"))
        if not content.strip():
            continue
        messages.append({
            "role": role,
            "content": content,
        })
    return messages


def _append_file_part_paths(content: str, parts: Any) -> str:
    """Name each file attached to a past user message by its absolute path.

    A file only ever reaches the model as a path it can hand to a tool, and
    that path is appended to the query of the turn the file arrived on
    (``_append_attachments_note``). A message that never ran a turn — a group
    post the relevance judge stayed quiet on — never produced that note, and a
    later turn asking "what is that image?" saw only the row's text, which
    names the file but not where it is. The agent then hunted the filename
    across the disk.

    So the path is restored here, from the row's own file parts. Content-only
    and deterministic: no filesystem check (a stat would make the prompt vary
    with the temp-dir pruner and cost the cache), and the tool reports a
    missing file perfectly well on its own. Malformed parts are skipped rather
    than raised on — history assembly must not die on one bad row.
    """
    if not isinstance(parts, list):
        return content
    lines: list[str] = []
    for part in parts:
        if not isinstance(part, dict) or part.get("kind") != "file":
            continue
        file_info = part.get("file")
        if not isinstance(file_info, dict):
            continue
        uri = str(file_info.get("uri") or "").strip()
        if not uri:
            continue
        name = str(file_info.get("name") or "").strip() or os.path.basename(uri)
        lines.append(f'[attached file "{name}" — absolute path: {uri}]')
    if not lines:
        return content
    return "\n".join([content, *lines]) if content else "\n".join(lines)


_CONTENT_TOKEN_ENCODER = None


def count_content_tokens(text: str, model: str = "gpt-4o") -> int:
    """Count tokens in a single piece of message *content*.

    Used by the memory feature to size extraction windows and decide when to
    trigger extraction. Counts only the text it is given (callers pass
    ``MessageModel.content``, never reasoning ``thinking_steps``). The encoder
    is cached process-wide; tiktoken lives in the ``tokenization`` extra and is
    imported lazily, so importing this module stays cheap on the thin core.
    """
    if not text:
        return 0
    global _CONTENT_TOKEN_ENCODER
    if _CONTENT_TOKEN_ENCODER is None:
        from tiktoken import encoding_for_model
        _CONTENT_TOKEN_ENCODER = encoding_for_model(model)
    return len(_CONTENT_TOKEN_ENCODER.encode(text))


def truncate_to_tokens(text: str, max_tokens: int, model: str = "gpt-4o") -> str:
    """Return ``text`` clipped to at most ``max_tokens`` tokens (best-effort).

    Used to enforce the memory entry size caps (≤300 short-term, ≤50 long-term).
    Falls back to the original text if tiktoken is unavailable for any reason.
    """
    if not text or max_tokens <= 0:
        return text or ""
    try:
        global _CONTENT_TOKEN_ENCODER
        if _CONTENT_TOKEN_ENCODER is None:
            from tiktoken import encoding_for_model
            _CONTENT_TOKEN_ENCODER = encoding_for_model(model)
        tokens = _CONTENT_TOKEN_ENCODER.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _CONTENT_TOKEN_ENCODER.decode(tokens[:max_tokens]).rstrip()
    except Exception:  # noqa: BLE001
        return text


def truncate_to_tokens_tail(text: str, max_tokens: int, model: str = "gpt-4o") -> str:
    """Like :func:`truncate_to_tokens` but keeps the **tail** (last ``max_tokens``).

    Used to clip an over-budget running summary while preserving its most recent
    sections (Next Steps / Critical Context) rather than head-keeping and dropping
    them. Falls back to the original text if tiktoken is unavailable.
    """
    if not text or max_tokens <= 0:
        return text or ""
    try:
        global _CONTENT_TOKEN_ENCODER
        if _CONTENT_TOKEN_ENCODER is None:
            from tiktoken import encoding_for_model
            _CONTENT_TOKEN_ENCODER = encoding_for_model(model)
        tokens = _CONTENT_TOKEN_ENCODER.encode(text)
        if len(tokens) <= max_tokens:
            return text
        return _CONTENT_TOKEN_ENCODER.decode(tokens[-max_tokens:]).lstrip()
    except Exception:  # noqa: BLE001
        return text


# Mirror of the markers defined in app.agent.reasoning_agent. Re-declared here
# to avoid a circular import (reasoning_agent imports from this module).
_OBSERVATION_START_MARKER = "------------OBS-START------------"
_OBSERVATION_END_MARKER = "------------OBS-END------------"
_OBSERVATION_TRUNCATION_NOTICE = (
    "[... content truncated, full result available in stored conversation ...]"
)


def truncate_old_observations(
    steps: List[str],
    *,
    max_tokens: int,
    preserve_recent: int = 1,
    head_tokens: int = 200,
    tail_tokens: int = 200,
    model: str = "gpt-4o",
) -> List[str]:
    """Shorten older Observation blocks inside ReAct step strings.

    Each step string may contain one or more Observation blocks delimited by
    ``_OBSERVATION_START_MARKER`` / ``_OBSERVATION_END_MARKER``. The most recent
    ``preserve_recent`` blocks (across the whole step list) are kept verbatim.
    Any earlier block whose body exceeds ``max_tokens`` is rewritten to the
    first ``head_tokens`` tokens, a truncation notice, then the last
    ``tail_tokens`` tokens. The input list is not mutated.
    """
    if not steps:
        return []

    spans: list[tuple[int, int, int, str]] = []
    for step_idx, step in enumerate(steps):
        cursor = 0
        while True:
            start = step.find(_OBSERVATION_START_MARKER, cursor)
            if start < 0:
                break
            body_start = start + len(_OBSERVATION_START_MARKER)
            end = step.find(_OBSERVATION_END_MARKER, body_start)
            if end < 0:
                break
            body = step[body_start:end]
            spans.append((step_idx, body_start, end, body))
            cursor = end + len(_OBSERVATION_END_MARKER)

    if not spans:
        return list(steps)

    keep_from = max(len(spans) - preserve_recent, 0)
    from tiktoken import encoding_for_model
    encoder = encoding_for_model(model)

    rewritten_bodies: dict[tuple[int, int, int], str] = {}
    for idx, (step_idx, body_start, end, body) in enumerate(spans):
        if idx >= keep_from:
            continue
        tokens = encoder.encode(body)
        if len(tokens) <= max_tokens:
            continue
        head = encoder.decode(tokens[:head_tokens]) if head_tokens > 0 else ""
        tail = encoder.decode(tokens[-tail_tokens:]) if tail_tokens > 0 else ""
        rewritten_bodies[(step_idx, body_start, end)] = (
            f"{head}\n{_OBSERVATION_TRUNCATION_NOTICE}\n{tail}"
        )

    if not rewritten_bodies:
        return list(steps)

    by_step: dict[int, list[tuple[int, int, str]]] = {}
    for (step_idx, body_start, end), new_body in rewritten_bodies.items():
        by_step.setdefault(step_idx, []).append((body_start, end, new_body))

    result: List[str] = []
    for step_idx, step in enumerate(steps):
        edits = by_step.get(step_idx)
        if not edits:
            result.append(step)
            continue
        edits.sort(key=lambda e: e[0], reverse=True)
        rewritten = step
        for body_start, end, new_body in edits:
            rewritten = rewritten[:body_start] + new_body + rewritten[end:]
        result.append(rewritten)

    return result
