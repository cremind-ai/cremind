from typing import Any, Dict, List, Optional, Tuple
from a2a.types import DataPart, FilePart, Part, TaskArtifactUpdateEvent, TaskStatusUpdateEvent, TextPart
from app.utils.formatting import dict_to_text


def parse_agent_events(events: List[Any]) -> Tuple[str, Dict[str, int], List[Part]]:
    """Parses a list of agent events (TaskArtifactUpdateEvent, TaskStatusUpdateEvent) into
    observation text, token usage, and structured observation parts.

    Args:
        events: List of events to process.

    Returns:
        A tuple of (observation_text, token_usage_dict, observation_parts).
        - observation_text: flattened text representation for LLM prompts
        - token_usage_dict: contains 'input_tokens' and 'output_tokens' if available
        - observation_parts: list[Part] preserving the original structure (TextPart, FilePart, DataPart)
    """
    text_parts: List[str] = []
    observation_parts: List[Part] = []
    token_usage: Dict[str, int] = {}

    for event in events:
        if isinstance(event, TaskArtifactUpdateEvent):
            # Check if this artifact is a token_usage artifact — extract separately
            if event.artifact and event.artifact.name == "token_usage" and event.artifact.parts:
                for part in event.artifact.parts:
                    if hasattr(part, 'root') and hasattr(part.root, 'data'):
                        data = getattr(part.root, 'data', {})
                        if isinstance(data, dict) and "token_usage" in data:
                            token_usage = data["token_usage"]
                continue  # Skip adding token_usage artifact to observation text

            if event.artifact and event.artifact.parts:
                for part in event.artifact.parts:
                    # Collect the raw Part for structured storage
                    observation_parts.append(part)
                    # Also build flattened text for LLM prompt
                    if hasattr(part, 'root'):
                        if hasattr(part.root, 'text'):
                            text = getattr(part.root, 'text', '')
                            if text:
                                text_parts.append(text)
                        elif hasattr(part.root, 'data'):
                            data = getattr(part.root, 'data', {})
                            if data:
                                try:
                                    text_parts.append(dict_to_text(data) if isinstance(data, dict) else str(data))
                                except Exception:
                                    text_parts.append(str(data))
                        elif hasattr(part.root, 'file'):
                            # FilePart — include a placeholder in text
                            file_obj = part.root.file
                            name = getattr(file_obj, 'name', None) or 'file'
                            mime = getattr(file_obj, 'mime_type', None) or 'unknown'
                            text_parts.append(f"[File: {name} ({mime})]")
        elif isinstance(event, TaskStatusUpdateEvent):
            if hasattr(event, "status") and event.status:
                message = getattr(event.status, "message", None)
                if message:
                    if isinstance(message, str):
                        text_parts.append(message)
                        observation_parts.append(Part(root=TextPart(text=message)))
                    elif hasattr(message, 'parts') and message.parts:
                        for part in message.parts:
                            observation_parts.append(part)
                            if hasattr(part, 'root') and hasattr(part.root, 'text'):
                                text = getattr(part.root, 'text', '')
                                if text:
                                    text_parts.append(text)

    return "\n".join(text_parts), token_usage, observation_parts
