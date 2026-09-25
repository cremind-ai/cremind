"""The per-profile index: the SQLite file behind User Document Search.

:mod:`.schema` defines the file (tables, FTS5 objects, forward migrations);
:mod:`.db` is the only code that opens it. Everything else in the sync and
query engines goes through :class:`IndexDB`, so the connection model (one
writer, per-thread readers, one short transaction per write) holds everywhere.
"""

from app.userdocs.index.db import (
    CITE_ALPHABET,
    IndexCorrupt,
    IndexDB,
    IndexDBError,
    IndexIncompatible,
    discard_backup,
    index_dir,
    index_path,
    is_corrupt_error,
    rebuild_file,
)
from app.userdocs.index.schema import LEXICAL_FTS5, LEXICAL_LIKE, SCHEMA_VERSION

__all__ = [
    "CITE_ALPHABET",
    "IndexCorrupt",
    "IndexDB",
    "IndexDBError",
    "IndexIncompatible",
    "LEXICAL_FTS5",
    "LEXICAL_LIKE",
    "SCHEMA_VERSION",
    "discard_backup",
    "index_dir",
    "index_path",
    "is_corrupt_error",
    "rebuild_file",
]
