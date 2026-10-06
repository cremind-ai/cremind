"""A tag enrolled over SWD on this computer (``cremind tags tools tag enroll``,
protocol v1), as a gateway worker on the same computer imports it into its
connection: the hardware tools' inventory row and the tag's secret.

Read only: the tools' inventory is opened only when it exists, and nothing is
written to the tools' data directory or secret store (opening the inventory
runs its own schema migrations, as the tools would).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Config
    from ..store.db import TagRecord


@dataclass(frozen=True, slots=True)
class LocalEnrollment:
    record: TagRecord
    secret: bytes


def local_enrollment(tag_id: int, config: Config | None = None) -> LocalEnrollment | None:
    """The hardware tools' enrollment of ``tag_id`` here, or ``None`` (not enrolled on this computer, or
    its secret is gone)."""
    from ..config import load_config
    from ..daemon.schema import open_database
    from ..secrets import SecretStore, SecretStoreError

    config = config or load_config()
    if not config.db_path.is_file():
        return None
    db = open_database(config.db_path)
    try:
        record = db.find_tag(tag_id)
    finally:
        db.close()
    if record is None:
        return None
    try:
        secret = SecretStore.open(config.data_dir, config.secrets.backend).get_tag_secret(tag_id, record.secret_ref)
    except SecretStoreError:
        return None
    return LocalEnrollment(record, secret)


__all__ = ["LocalEnrollment", "local_enrollment"]
