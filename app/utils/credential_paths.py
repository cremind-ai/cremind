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

CREDENTIAL_DIR_NAMES = frozenset({"coding-cli", "codex-home", "cli-wizards"})
