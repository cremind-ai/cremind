"""Names of directories that hold credentials and must never be served or read.

Shared by the file API (:mod:`app.api.files`, which refuses to list or serve
them) and User Document Search (:mod:`app.userdocs`, which never indexes them).
It lives here rather than in the API module so the indexer does not have to
import the web layer to learn the rule.

``coding-cli`` holds long-lived OAuth refresh tokens for a user's Claude and
ChatGPT accounts, ``codex-home`` the ``auth.json`` written for an API-key
credential, and ``cli-wizards`` plaintext keys until a wizard finishes. They
are kept as a name set because there is one store per profile, created on
demand, and the same names appear both per profile and at the shared root —
there is no list of live paths to enumerate, so the name is the rule.
"""

import os

CREDENTIAL_DIR_NAMES = frozenset({"coding-cli", "codex-home", "cli-wizards"})


def userdocs_index_root(system_dir: str) -> str:
    """Where User Document Search keeps every profile's index."""
    return os.path.join(os.path.realpath(system_dir), "storage", "userdocs")


def is_userdocs_index_path(target: str, system_dir: str) -> bool:
    """Is ``target`` User Document Search's index store, or inside it?

    Every profile's index lives there (one directory per profile uid) and an
    index holds the text of that profile's files, so generic file access —
    the file API, the agent's file tool — refuses it for every profile,
    admin included. Its content reaches clients only through the
    profile-scoped ``/api/userdocs`` routes.
    """
    store = os.path.normcase(userdocs_index_root(system_dir))
    real = os.path.normcase(os.path.realpath(target))
    return real == store or real.startswith(store + os.sep)
