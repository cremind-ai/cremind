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
    inject_ids: bool = True,
) -> list[ChatCompletionMessageParam]:
    """Convert database message dicts to ChatCompletionMessageParam format.

    When inject_ids is True, each message's content is suffixed with
    ``\\nid: <uuid>`` so the LLM can reference it via the message_detail tool.
    """
    messages: list[ChatCompletionMessageParam] = []
    for m in db_messages:
        role = "assistant" if m["role"] == "agent" else m["role"]
        content = m.get("content") or ""
        if not content.strip():
            continue
        if inject_ids:
            content += f"\nmessage_id: {m['id']}"
        summary = m.get("summary")
        if summary:
            content += f"\nsummary: {summary}"
        messages.append({
            "role": role,
            "content": content,
        })
    return messages


def limit_messages(messages: list[ChatCompletionMessageParam], max_length: int,
                   model: str = "gpt-4o") -> list[ChatCompletionMessageParam]:
    """Limit messages by token length, keeping the most recent messages.

    This function iterates from the end of the messages list (most recent) and
    keeps including messages until the total token length exceeds max_length.

    Args:
        messages: List of chat completion messages to limit
        max_length: Maximum number of tokens allowed
        model: Model name for tiktoken encoder (default: "gpt-4o")

    Returns:
        List of messages within the token limit, preserving order
    """
    from tiktoken import encoding_for_model
    encoder = encoding_for_model(model)
    llm_messages: list[ChatCompletionMessageParam] = []

    for i in range(len(messages) - 1, -1, -1):
        # Get messages from index i to end
        slice_messages = messages[i:len(messages)]

        # Join all content into a single string
        total_messages = " ".join(
            str(msg.get("content", "")) for msg in slice_messages
        )

        # Calculate token length
        token_length = len(encoder.encode(total_messages))

        # Stop if we exceed max_length
        if token_length > max_length:
            break

        # Add message to the beginning of the result list
        llm_messages.insert(0, messages[i])

    return llm_messages


def truncate_messages(
    messages: list[ChatCompletionMessageParam],
    max_tokens_per_message: int,
    preserve_recent: int = 2,
    model: str = "gpt-4o",
) -> list[ChatCompletionMessageParam]:
    """Truncate older messages to a maximum token length per message.

    The most recent `preserve_recent` messages are kept at full length.
    All older messages that exceed `max_tokens_per_message` tokens are
    truncated and suffixed with '...' to indicate omitted content.

    Args:
        messages: List of chat completion messages
        max_tokens_per_message: Maximum tokens allowed per older message
        preserve_recent: Number of most recent messages to preserve in full (default: 2)
        model: Model name for tiktoken encoder (default: "gpt-4o")

    Returns:
        A new list of messages with older ones truncated as needed
    """
    if not messages:
        return []

    from tiktoken import encoding_for_model
    encoder = encoding_for_model(model)
    result: list[ChatCompletionMessageParam] = []
    protected_start = max(len(messages) - preserve_recent, 0)

    for i, msg in enumerate(messages):
        if i >= protected_start:
            result.append(msg)
            continue

        content = msg.get("content", "")
        if not isinstance(content, str):
            result.append(msg)
            continue

        tokens = encoder.encode(content)
        if len(tokens) > max_tokens_per_message:
            truncated_text = encoder.decode(tokens[:max_tokens_per_message]) + '...'
            result.append({**msg, "content": truncated_text})
        else:
            result.append(msg)

    return result


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
