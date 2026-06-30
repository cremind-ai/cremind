from __future__ import annotations

from typing import TYPE_CHECKING, List

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
    """
    messages: list[ChatCompletionMessageParam] = []
    for m in db_messages:
        # UI-only messages (e.g. rejected skill-event triggers the matching gate
        # filtered out) are shown in the conversation but must never enter the
        # model's context — the agent has no knowledge of them. This is the single
        # chokepoint every history-building path routes through.
        if (m.get("metadata") or {}).get("ui_only"):
            continue
        trace = m.get("llm_messages") if include_reasoning else None
        if trace:
            messages.extend(trace)
            continue
        role = "assistant" if m["role"] == "agent" else m["role"]
        content = m.get("content") or ""
        if not content.strip():
            continue
        messages.append({
            "role": role,
            "content": content,
        })
    return messages


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
