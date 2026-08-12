"""Session-token authentication: minting, verification, and revocation.

Cremind issues one stateless HS256 JWT per profile, stored at
``<system_dir>/tokens/<profile>.token``. Revocation is a per-profile
``token_serial`` counter (:mod:`app.auth.serial`) stamped into every token as
the ``tsr`` claim and checked on every decode (:mod:`app.auth.tokens`), so
rotating one profile's token kills its old credentials without touching anyone
else's.

Every code path that decodes a session token must go through
:func:`verify_token` — a bare ``jwt.decode`` accepts revoked tokens.
"""

from app.auth.serial import (
    TOKEN_SERIAL_CLAIM,
    all_serials,
    bump_serial,
    current_serial,
    invalidate_serial_cache,
    serial_matches,
)
from app.auth.tokens import (
    delete_token_file,
    ensure_local_config_storage,
    rotate_profile_token,
    token_file_path,
    tokens_dir,
    verify_token,
    write_token_file,
)

__all__ = [
    "TOKEN_SERIAL_CLAIM",
    "all_serials",
    "bump_serial",
    "current_serial",
    "delete_token_file",
    "ensure_local_config_storage",
    "invalidate_serial_cache",
    "rotate_profile_token",
    "serial_matches",
    "token_file_path",
    "tokens_dir",
    "verify_token",
    "write_token_file",
]
