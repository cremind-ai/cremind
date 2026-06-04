"""Auth client storage backend for session tokens.

Provides storage for OAuth tokens against the active database provider.
Schema (``auth_tokens`` table) is owned by
:class:`app.storage.conversation_storage.ConversationStorage`; this module
only reads/writes existing rows.

Storage table: auth_tokens
Columns: id, agent_name, profile, agent_type, token_kind, token, created_at
"""
import base64
import json
import time
import uuid
from typing import Optional, Protocol

from sqlalchemy import text

from app.storage._sync_base import SyncStorageBase, dialect_upsert
from app.utils.logger import logger

# Default token kind
ACCESS_TOKEN = "access_token"
REFRESH_TOKEN = "refresh_token"
DCR_CLIENT_ID = "dcr_client_id"
DCR_CLIENT_SECRET = "dcr_client_secret"

# Special profile for server-level (profile-independent) data like DCR credentials
SERVER_PROFILE = "__server__"


def _sanitize_name(name: str) -> str:
    """Sanitize a name for use in storage keys."""
    return "".join([c for c in name if c.isalnum() or c in (' ', '-', '_')]).strip()


def _decode_jwt_exp(token: str) -> Optional[int]:
    """Decode JWT token and extract the 'exp' claim.

    Returns:
        The exp timestamp (seconds since epoch) or None if unable to decode
    """
    try:
        parts = token.split('.')
        if len(parts) != 3:
            return None

        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += '=' * padding

        decoded_bytes = base64.urlsafe_b64decode(payload)
        payload_dict = json.loads(decoded_bytes)

        return payload_dict.get('exp')
    except Exception as e:
        logger.warning(f"Failed to decode JWT exp claim: {e}")
        return None


class ClientStorageBackend(Protocol):
    """Protocol for client storage backends."""

    def save_token(self, agent_name: str, profile: str, token: str,
                   agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> None:
        """Save a token for the given agent, profile, and kind."""
        ...

    def get_token(self, agent_name: str, profile: str,
                  agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> str:
        """Retrieve a token for the given agent, profile, and kind."""
        ...

    def delete_token(self, agent_name: str, profile: str,
                     agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> bool:
        """Delete a token for the given agent, profile, and kind."""
        ...


class DatabaseClientStorage(SyncStorageBase):
    """Provider-backed client storage for auth tokens.

    Reads/writes the ``auth_tokens`` table through the active
    :class:`app.databases.DatabaseProvider`. The table is created by
    :class:`ConversationStorage`; this class assumes it exists.

    The ``__server__`` pseudo-profile row is also created by ConversationStorage's
    profile-handling code on first need (DCR credentials are stored against it).
    """

    def __init__(self):
        super().__init__(None)
        self._ensure_server_profile()

    def _ensure_server_profile(self) -> None:
        """Insert the ``__server__`` pseudo-profile row if missing.

        Some token kinds (DCR client_id, client_secret) are server-wide and
        not tied to a user profile. They reference ``__server__`` via FK.
        """
        try:
            with self._engine.begin() as conn:
                exists = conn.execute(
                    text("SELECT 1 FROM profiles WHERE name = :name"),
                    {"name": SERVER_PROFILE},
                ).fetchone()
                if not exists:
                    now = time.time() * 1000
                    conn.execute(
                        text(
                            "INSERT INTO profiles (id, name, created_at, updated_at) "
                            "VALUES (:id, :name, :created_at, :updated_at)"
                        ),
                        {
                            "id": SERVER_PROFILE, "name": SERVER_PROFILE,
                            "created_at": now, "updated_at": now,
                        },
                    )
        except Exception as exc:  # noqa: BLE001
            # Non-fatal — the row may not be needed yet (no DCR registration
            # happens at boot). Logging keeps the issue diagnosable.
            logger.debug(f"Could not pre-create __server__ profile row: {exc}")

    def save_token(self, agent_name: str, profile: str, token: str,
                   agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> None:
        safe_agent = _sanitize_name(agent_name)
        now = time.time() * 1000
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM auth_tokens WHERE agent_name = :agent_name "
                    "AND profile = :profile AND agent_type = :agent_type AND token_kind = :token_kind"
                ),
                {"agent_name": safe_agent, "profile": profile,
                 "agent_type": agent_type, "token_kind": token_kind},
            )
            conn.execute(
                text(
                    "INSERT INTO auth_tokens "
                    "(id, agent_name, profile, agent_type, token_kind, token, created_at) "
                    "VALUES (:id, :agent_name, :profile, :agent_type, :token_kind, :token, :created_at)"
                ),
                {
                    "id": str(uuid.uuid4()), "agent_name": safe_agent, "profile": profile,
                    "agent_type": agent_type, "token_kind": token_kind, "token": token,
                    "created_at": now,
                },
            )
        logger.info(f"Token saved ({token_kind}): agent={safe_agent}, profile={profile}")

    def get_token(self, agent_name: str, profile: str,
                  agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> str:
        safe_agent = _sanitize_name(agent_name)
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT token FROM auth_tokens WHERE agent_name = :agent_name "
                    "AND profile = :profile AND agent_type = :agent_type AND token_kind = :token_kind"
                ),
                {"agent_name": safe_agent, "profile": profile,
                 "agent_type": agent_type, "token_kind": token_kind},
            ).fetchone()
            return row[0] if row else ""

    def delete_token(self, agent_name: str, profile: str,
                     agent_type: str = "a2a", token_kind: str = ACCESS_TOKEN) -> bool:
        safe_agent = _sanitize_name(agent_name)
        with self._engine.begin() as conn:
            cursor = conn.execute(
                text(
                    "DELETE FROM auth_tokens WHERE agent_name = :agent_name "
                    "AND profile = :profile AND agent_type = :agent_type AND token_kind = :token_kind"
                ),
                {"agent_name": safe_agent, "profile": profile,
                 "agent_type": agent_type, "token_kind": token_kind},
            )
            if cursor.rowcount > 0:
                logger.info(f"Token deleted ({token_kind}): agent={safe_agent}, profile={profile}")
                return True
            return False


# Backward-compatible alias name in case other code imports it.
SQLiteClientStorage = DatabaseClientStorage


# Singleton storage instance
_storage_instance: Optional[ClientStorageBackend] = None


def get_auth_client_storage() -> ClientStorageBackend:
    """Get the auth client storage backend instance (singleton)."""
    global _storage_instance

    if _storage_instance is not None:
        return _storage_instance

    _storage_instance = DatabaseClientStorage()
    logger.info("Using database-backed client storage")
    return _storage_instance


def _reset_auth_client_storage_singleton() -> None:
    """Used by the wizard hot-swap to drop the cached engine reference."""
    global _storage_instance
    _storage_instance = None


# Backward-compatible alias
get_client_storage = get_auth_client_storage
