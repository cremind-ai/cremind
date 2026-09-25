"""Settings for Documentation search: the admin gate and each profile's own.

Two layers, deliberately in two places:

**The admin gate** (``server_config`` keys ``documents.*``) is system-wide
because the embedding model and vector store it rides on are system-wide. It
is written only through ``PUT /api/documentation-search/admin`` — never through
``PUT /api/config/embedding``, which always forces a rebuild that refuses every
chat while it runs.

**Each profile's settings** live in the ``document_sources`` table, not in
``user_config``. ``user_config`` accepts any value its schema's type allows and
publishes nothing, so a folder path written there would skip the checks in
:func:`validate_root`. Blueprints also export ``user_config`` verbatim, which
would carry a machine-specific path to another install.

Every per-profile read here names its profile explicitly. ``get_dynamic(...,
profile=None)`` falls back to the admin's row without saying so.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

from app.config.settings import BaseConfig, _bool, _dynaconf_get, get_dynamic, get_user_working_directory

SOURCE_LOCAL = "local"
SOURCE_DRIVE = "drive"
SOURCE_KINDS = (SOURCE_LOCAL, SOURCE_DRIVE)

ROOT_INHERIT = "inherit"
ROOT_CUSTOM = "custom"

# ── Admin gate ─────────────────────────────────────────────────────────────

# (server_config key, AdminPolicy field, type, min, max). The TOML default for
# each lives in ``[documentation_search]`` of ``app/config/settings.toml``.
_ADMIN_FIELDS: tuple[tuple[str, str, type, float | None, float | None], ...] = (
    ("documentation_search.allowed", "allowed", bool, None, None),
    ("documentation_search.storage_budget_mb", "storage_budget_mb", int, 256, 10_000_000),
    ("documentation_search.per_profile_budget_mb", "per_profile_budget_mb", int, 0, 10_000_000),
    ("documentation_search.vision_daily_cap_default", "vision_daily_cap_default", int, 0, 1_000_000),
    ("documentation_search.max_file_mb", "max_file_mb", int, 1, 4096),
    ("documentation_search.workers", "workers", int, 1, 8),
    ("documentation_search.vector_capacity_mb", "vector_capacity_mb", int, 0, 100_000_000),
    ("documentation_search.db_capacity_mb", "db_capacity_mb", int, 0, 100_000_000),
)


@dataclass(frozen=True)
class AdminPolicy:
    allowed: bool = False
    storage_budget_mb: int = 10240
    per_profile_budget_mb: int = 0
    vision_daily_cap_default: int = 1000
    max_file_mb: int = 100
    workers: int = 2
    # 0 means "measure it" (statvfs), unless the matching environment variable
    # (set by the Helm chart from the PVC size) says otherwise.
    vector_capacity_mb: int = 0
    db_capacity_mb: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coerce(raw: Any, typ: type) -> Any:
    if typ is bool:
        return _bool(raw)
    return int(float(raw))


def read_admin_policy() -> AdminPolicy:
    """The admin gate as it stands now. ``server_config`` wins over the TOML."""
    defaults = AdminPolicy()
    values: dict[str, Any] = {}
    for key, attr, typ, _lo, _hi in _ADMIN_FIELDS:
        raw = get_dynamic("server_config", key)
        if raw is None:
            raw = _dynaconf_get(key, getattr(defaults, attr))
        try:
            values[attr] = _coerce(raw, typ)
        except (TypeError, ValueError):
            values[attr] = getattr(defaults, attr)
    return AdminPolicy(**values)


class PolicyValidationError(ValueError):
    """A rejected admin-gate write. ``details`` maps field name -> message."""

    def __init__(self, details: dict[str, str]):
        super().__init__("; ".join(f"{k}: {v}" for k, v in details.items()))
        self.details = details


def write_admin_policy(patch: dict[str, Any], config_storage) -> AdminPolicy:
    """Validate and persist a partial admin-gate update; return the new policy.

    Unknown keys are rejected rather than ignored, so a typo in a CLI flag or
    an API client cannot look like a successful save.
    """
    by_attr = {attr: (key, typ, lo, hi) for key, attr, typ, lo, hi in _ADMIN_FIELDS}
    errors: dict[str, str] = {}
    staged: list[tuple[str, str]] = []
    for attr, raw in (patch or {}).items():
        spec = by_attr.get(attr)
        if spec is None:
            errors[attr] = "unknown setting"
            continue
        key, typ, lo, hi = spec
        try:
            value = _coerce(raw, typ)
        except (TypeError, ValueError):
            errors[attr] = f"must be {'true/false' if typ is bool else 'a number'}"
            continue
        if typ is not bool:
            if lo is not None and value < lo:
                errors[attr] = f"must be at least {int(lo)}"
                continue
            if hi is not None and value > hi:
                errors[attr] = f"must be at most {int(hi)}"
                continue
        staged.append((key, "true" if value is True else "false" if value is False else str(value)))
    if errors:
        raise PolicyValidationError(errors)
    for key, value in staged:
        config_storage.set("server_config", key, value)
    return read_admin_policy()


def feature_effective(policy: AdminPolicy | None = None) -> tuple[bool, str | None]:
    """Whether the feature can run at all on this server, and if not, why.

    Reasons: ``admin_gate_off`` (the admin has not allowed it),
    ``embedding_disabled`` (Vector Embedding is off). Whether the optional
    extraction extras are installed is a separate question — plain-text files
    still index without them, so it does not switch the feature off.
    """
    policy = policy or read_admin_policy()
    if not policy.allowed:
        return False, "admin_gate_off"
    if not BaseConfig.is_embedding_enabled():
        return False, "embedding_disabled"
    return True, None


# ── Per-profile options ────────────────────────────────────────────────────

# Where the agent may use a profile's documents. Channels and group rooms are
# off by default: a channel in ``open`` mode answers anyone who messages it,
# and a group room fans every answer out to other people's conversations.
DEFAULT_ALLOW_IN = {"web_cli": True, "channels": False, "rooms": False}

OBSERVER_MODES = ("auto", "native", "poll")


def default_options() -> dict[str, Any]:
    return {
        "caption": {"enabled": True, "daily_cap": None, "min_px": 256, "min_kb": 20},
        # Recorded when the user agrees to send photos/scans to the vision
        # provider; captioning never runs without it (see app.documents vision).
        "caption_consent": None,
        "identity": {"author_names": [], "emails": [], "camera_devices": []},
        "allow_in": dict(DEFAULT_ALLOW_IN),
        "observer": {"mode": "auto"},
        "reconcile_interval_min": 360,
        # Drive only: folders to index for an account that holds whole-Drive
        # access. Empty means "only what was granted file-by-file".
        "include_folders": [],
    }


def _str_list(raw: Any, *, limit: int = 50, max_len: int = 256) -> list[str]:
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if s and len(s) <= max_len and s not in out:
            out.append(s)
        if len(out) >= limit:
            break
    return out


def normalize_options(raw: Any, base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge ``raw`` onto ``base`` (or the defaults), dropping anything invalid.

    Lenient by design — a malformed field keeps its previous value rather than
    failing the whole save — except that nothing outside the known shape ever
    reaches the database.
    """
    out = json.loads(json.dumps(base)) if base else default_options()
    for k, v in default_options().items():
        out.setdefault(k, v)
    if not isinstance(raw, dict):
        return out

    cap = raw.get("caption")
    if isinstance(cap, dict):
        c = out["caption"]
        if "enabled" in cap:
            c["enabled"] = _bool(cap["enabled"])
        if "daily_cap" in cap:
            dc = cap["daily_cap"]
            c["daily_cap"] = None if dc in (None, "") else max(0, int(float(dc)))
        for k, lo, hi in (("min_px", 16, 4096), ("min_kb", 0, 10240)):
            if k in cap:
                try:
                    c[k] = min(hi, max(lo, int(float(cap[k]))))
                except (TypeError, ValueError):
                    pass

    if "caption_consent" in raw:
        cc = raw["caption_consent"]
        if cc is None:
            out["caption_consent"] = None
        elif isinstance(cc, dict) and cc.get("provider") and cc.get("model"):
            out["caption_consent"] = {
                "at": float(cc.get("at") or 0) or None,
                "provider": str(cc["provider"])[:64],
                "model": str(cc["model"])[:128],
            }

    ident = raw.get("identity")
    if isinstance(ident, dict):
        for k in ("author_names", "emails", "camera_devices"):
            if k in ident:
                out["identity"][k] = _str_list(ident[k])

    allow = raw.get("allow_in")
    if isinstance(allow, dict):
        for k in DEFAULT_ALLOW_IN:
            if k in allow:
                out["allow_in"][k] = _bool(allow[k])

    obs = raw.get("observer")
    if isinstance(obs, dict) and obs.get("mode") in OBSERVER_MODES:
        out["observer"]["mode"] = obs["mode"]

    if "reconcile_interval_min" in raw:
        try:
            out["reconcile_interval_min"] = min(10080, max(15, int(float(raw["reconcile_interval_min"]))))
        except (TypeError, ValueError):
            pass

    if "include_folders" in raw:
        out["include_folders"] = _str_list(raw["include_folders"], limit=200)
    return out


EXCLUDE_TYPES = ("glob", "dir", "ext")
EXCLUDE_MODES = ("skip", "metadata_only")


def normalize_excludes(raw: Any) -> list[dict[str, str]]:
    """A clean, de-duplicated exclude-rule list. Invalid rules are dropped."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rule in raw[:500]:
        if isinstance(rule, str):
            rule = {"pattern": rule}
        if not isinstance(rule, dict):
            continue
        pattern = str(rule.get("pattern") or "").strip()
        if not pattern or len(pattern) > 512 or "\x00" in pattern:
            continue
        typ = rule.get("type") if rule.get("type") in EXCLUDE_TYPES else "glob"
        mode = rule.get("mode") if rule.get("mode") in EXCLUDE_MODES else "skip"
        if typ == "ext":
            pattern = pattern.lower().lstrip("*").lstrip(".")
            if not pattern:
                continue
        key = (typ, pattern)
        if key in seen:
            continue
        seen.add(key)
        out.append({"pattern": pattern, "type": typ, "mode": mode})
    return out


# ── Root validation ────────────────────────────────────────────────────────

# Never a root for anyone, admin included: indexing any of these reads the
# operating system, not documents, and several are unbounded pseudo-files.
_FORBIDDEN_POSIX = (
    "/", "/proc", "/sys", "/dev", "/etc", "/usr", "/bin", "/sbin", "/lib",
    "/lib64", "/boot", "/run", "/var/lib/docker", "/var/run",
)
_FORBIDDEN_WINDOWS_PREFIXES = (
    r"c:\windows", r"c:\program files", r"c:\program files (x86)", r"c:\programdata",
)


@dataclass
class RootCheck:
    ok: bool
    path: str | None = None
    code: str | None = None
    message: str | None = None
    # Paths under the root that must never be indexed (the system directory,
    # when it happens to sit inside the chosen folder).
    locked_excludes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def real_path(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path)))


def _norm(path: str) -> str:
    return os.path.normcase(path).rstrip("\\/") or os.path.normcase(path)


def is_inside(child: str, parent: str) -> bool:
    """``child`` equals or sits inside ``parent`` (both already realpath'd)."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    try:
        return os.path.commonpath([c, p]) == p
    except ValueError:  # different drives on Windows
        return False


def _is_forbidden(real: str) -> bool:
    n = _norm(real)
    if os.name == "nt":
        drive, tail = os.path.splitdrive(real)
        if drive and tail.strip("\\/") == "":
            return True  # a bare drive root, "C:\"
        low = n.lower()
        return any(low == p or low.startswith(p + "\\") for p in _FORBIDDEN_WINDOWS_PREFIXES)
    if n in ("", "/"):
        return True
    return any(n == p or (p != "/" and n.startswith(p + "/")) for p in _FORBIDDEN_POSIX)


def system_dir() -> str:
    return real_path(BaseConfig.CREMIND_SYSTEM_DIR)


def working_dir() -> str:
    """The server-wide User Working Directory, resolved. Note that
    :func:`get_user_working_directory` creates it, so "it exists" never
    proves a folder is mounted — the sync engine's root guard knows that."""
    return real_path(get_user_working_directory())


def validate_root(raw: str | None, *, is_admin: bool) -> RootCheck:
    """Decide whether ``raw`` may be indexed by this profile.

    ``raw=None`` means "inherit the working directory". Checked in order:
    resolves (symlinks included, so a link cannot escape); exists and is a
    readable directory; is not inside the Cremind system directory (tokens,
    every profile's data); is not an OS location; and, for non-admin profiles,
    sits inside the working directory. When the system directory is *inside*
    the root it is returned in ``locked_excludes`` so the walker prunes it.
    """
    inherited = raw is None or not str(raw).strip()
    real = working_dir() if inherited else real_path(str(raw).strip())
    sysdir = system_dir()

    if not os.path.exists(real):
        return RootCheck(False, real, "not_found", "That folder does not exist.")
    if not os.path.isdir(real):
        return RootCheck(False, real, "not_directory", "That path is a file, not a folder.")
    if not os.access(real, os.R_OK | os.X_OK):
        return RootCheck(False, real, "not_readable", "Cremind cannot read that folder.")
    if is_inside(real, sysdir):
        msg = (
            "The working directory is Cremind's own system folder, which holds "
            "credentials and every profile's data — choose a different folder."
            if inherited else
            "That folder is inside Cremind's system folder, which holds credentials "
            "and every profile's data. Choose a different folder."
        )
        return RootCheck(False, real, "inside_system_dir", msg)
    if _is_forbidden(real):
        return RootCheck(False, real, "forbidden_system_path",
                         "That is an operating-system location, not a documents folder.")
    if not is_admin and not inherited and not is_inside(real, working_dir()):
        return RootCheck(False, real, "outside_working_dir",
                         f"Choose a folder inside the working directory ({working_dir()}).")
    locked = [sysdir] if is_inside(sysdir, real) else []
    return RootCheck(True, real, None, None, locked)


# ── Confirm-before-destroy change plans ────────────────────────────────────

@dataclass
class Effect:
    """One consequence of a settings change the user should see first."""

    # purge_out_of_scope | purge_all | purge_drive | reembed_all | reextract_all
    kind: str
    files: int = 0
    bytes: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DESTRUCTIVE_EFFECTS = frozenset({"purge_out_of_scope", "purge_all", "purge_drive"})


@dataclass
class ChangePlan:
    effects: list[Effect]
    token: str

    @property
    def destructive(self) -> bool:
        return any(e.kind in DESTRUCTIVE_EFFECTS for e in self.effects)

    def to_dict(self) -> dict[str, Any]:
        return {"effects": [e.to_dict() for e in self.effects], "destructive": self.destructive}


def _bucket(n: int) -> int:
    """Coarse bucket for a count, so a file or two arriving while the dialog is
    open does not invalidate the user's confirmation, but a big change does."""
    if n <= 0:
        return 0
    return max(1, n.bit_length())


def plan_token(profile: str, patch: dict[str, Any], version: Any, effects: Iterable[Effect]) -> str:
    """A short, deterministic confirmation token for exactly this change.

    Covers the profile, the requested change, the row's version and the rough
    size of each effect. If any of those move between the preview and the
    confirm, the old token no longer matches and the server asks again.
    """
    payload = json.dumps(
        {
            "p": profile,
            "patch": patch,
            "v": version,
            "e": sorted((e.kind, _bucket(e.files)) for e in effects),
        },
        sort_keys=True, default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# Supplied by the index layer once it exists: given (profile, kind, what),
# return the Effect it would cause. Until then plans report no counts, which is
# correct — with no index there is nothing to destroy.
EffectCounter = Callable[[str, str, str, dict[str, Any]], Effect | None]
_effect_counter: EffectCounter | None = None


def set_effect_counter(fn: EffectCounter | None) -> None:
    global _effect_counter
    _effect_counter = fn


def _count(profile: str, kind: str, what: str, ctx: dict[str, Any]) -> Effect:
    if _effect_counter is not None:
        try:
            eff = _effect_counter(profile, kind, what, ctx)
            if eff is not None:
                return eff
        except Exception:  # noqa: BLE001 — a failed count must not block the preview
            pass
    return Effect(what)


def _narrows_folders(cur: dict[str, Any], patch: dict[str, Any]) -> bool:
    """Whether a Drive patch narrows ``include_folders``: a folder chosen
    before is no longer listed (a subfolder instead of its parent, say), or
    a chosen set replaces "everything granted". Only a count can tell whether
    anything indexed actually falls outside — the effect counter's job."""
    opts = patch.get("options")
    if not isinstance(opts, dict) or "include_folders" not in opts:
        return False
    old = set(normalize_options(cur.get("options")).get("include_folders") or [])
    new = set(normalize_options({"include_folders": opts["include_folders"]})["include_folders"])
    return bool(new) and (not old or bool(old - new))


def plan_source_change(
    profile: str,
    kind: str,
    current: dict[str, Any] | None,
    patch: dict[str, Any],
) -> ChangePlan:
    """What saving ``patch`` over ``current`` would do, before doing it.

    Destructive effects: turning the source off *and* deleting its index,
    moving the root (files outside the new folder leave the index), adding
    excludes that drop indexed files, turning Drive off (its index goes),
    and narrowing Drive's ``include_folders`` (files outside the folders
    now chosen leave the index). Turning a source off while keeping the
    index is not destructive.
    """
    effects: list[Effect] = []
    cur = current or {}
    ctx = {"current": cur, "patch": patch}
    purging = False
    if patch.get("delete_index"):
        effects.append(_count(profile, kind, "purge_all" if kind == SOURCE_LOCAL else "purge_drive", ctx))
        purging = True
    elif kind == SOURCE_DRIVE and cur.get("enabled") and patch.get("enabled") is False:
        effects.append(_count(profile, kind, "purge_drive", ctx))
        purging = True
    if kind == SOURCE_DRIVE and cur.get("enabled") and not purging and _narrows_folders(cur, patch):
        effects.append(_count(profile, kind, "purge_out_of_scope", ctx))
    if kind == SOURCE_LOCAL and cur.get("root_path") and "root_path" in patch:
        if _norm(str(patch.get("root_path") or "")) != _norm(str(cur.get("root_path") or "")):
            effects.append(_count(profile, kind, "purge_out_of_scope", ctx))
    if "excludes" in patch and cur.get("enabled"):
        new_rules = {(r["type"], r["pattern"]) for r in normalize_excludes(patch["excludes"])}
        old_rules = {(r["type"], r["pattern"]) for r in normalize_excludes(cur.get("excludes"))}
        if new_rules - old_rules:
            effects.append(_count(profile, kind, "purge_out_of_scope", ctx))
    if _effect_counter is not None:
        # With real counts available, a destructive effect that would remove
        # nothing is not worth a confirmation dialog.
        effects = [e for e in effects if e.kind not in DESTRUCTIVE_EFFECTS or e.files > 0]
    token = plan_token(profile, patch, cur.get("updated_at"), effects)
    return ChangePlan(effects, token)
