"""Credential-free system transport announcements shared by every profile."""
from app.events.embedding_state_bus import EmbeddingStateStreamBus

_bus = EmbeddingStateStreamBus()


def get_transport_state_bus() -> EmbeddingStateStreamBus:
    return _bus
