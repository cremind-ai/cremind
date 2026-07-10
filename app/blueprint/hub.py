"""Download a Blueprint from the Cremind Hub marketplace.

Mirrors the skill-side hub importer (:mod:`app.skills.importer`) but for the
``.cremind-blueprint`` archive: unlike a skill (which is extracted), a blueprint
archive is saved *as-is* and handed straight to the import wizard's
:func:`app.blueprint.plan.stage_upload`, which opens it as a gzipped tar.

Override the hub base URL with ``CREMIND_HUB_URL`` (e.g. ``http://localhost:8788``)
for local development. The download endpoint is public — no credentials are sent.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import httpx

from app.blueprint.manifest import BlueprintError
from app.utils.logger import logger

# Cap the download so a hostile/huge archive can't exhaust disk (matches the
# blueprint export cap and the skill importer's tarball cap).
_MAX_BYTES = 100 * 1024 * 1024  # 100 MiB

_HUB_DEFAULT_URL = "https://hub.cremind.io"
# A hub link is the blueprint's page URL (what users copy) or a bare canonical name.
_HUB_BP_PATH_RE = re.compile(
    r"^(?:https?://[^/\s]+)?/blueprints/(?P<name>[a-z0-9][a-z0-9._-]{0,63})/?$",
    re.IGNORECASE,
)
_HUB_BARE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$", re.IGNORECASE)


def hub_base() -> str:
    """The Cremind Hub base URL (``CREMIND_HUB_URL`` or the public default)."""
    return os.environ.get("CREMIND_HUB_URL", _HUB_DEFAULT_URL).rstrip("/")


def parse_hub_link(link: str) -> str:
    """Resolve a Cremind Hub link to a canonical blueprint name.

    Accepts the blueprint page URL (``https://hub.cremind.io/blueprints/<name>``
    or ``/blueprints/<name>``) or a bare ``<name>``. Returns the lowercased name.
    Raises :class:`BlueprintError` for anything unrecognizable.
    """
    raw = (link or "").strip()
    if not raw:
        raise BlueprintError("A Cremind Hub link or blueprint name is required")
    match = _HUB_BP_PATH_RE.match(raw)
    if match:
        return match.group("name").lower()
    if _HUB_BARE_NAME_RE.match(raw):
        return raw.lower()
    raise BlueprintError(
        "Not a recognizable Cremind Hub link "
        "(expected https://hub.cremind.io/blueprints/<name> or a blueprint name)"
    )


def download_hub_blueprint(link: str, dest_dir: Path) -> Path:
    """Download a blueprint's ``.cremind-blueprint`` archive into *dest_dir*.

    Returns the saved archive path (NOT extracted — the file *is* the archive the
    import wizard consumes). Raises :class:`BlueprintError` on any failure.
    """
    name = parse_hub_link(link)
    base = hub_base()
    url = f"{base}/api/blueprints/{name}/download"
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = dest_dir / f"{name}.cremind-blueprint"
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=60) as resp:
            if resp.status_code == 404:
                raise BlueprintError(f"Blueprint '{name}' was not found on Cremind Hub")
            if resp.status_code != 200:
                raise BlueprintError(f"Cremind Hub returned {resp.status_code} for '{name}'")
            total = 0
            with saved.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > _MAX_BYTES:
                        fh.close()
                        saved.unlink(missing_ok=True)
                        raise BlueprintError("Blueprint archive is too large")
                    fh.write(chunk)
    except httpx.HTTPError as exc:
        logger.warning(f"Hub blueprint download failed for '{name}': {exc}")
        raise BlueprintError(
            f"Could not reach Cremind Hub for '{name}'. Check the link and your connection."
        ) from exc
    return saved


def download_to_temp(link: str) -> tuple[Path, Path]:
    """Download a blueprint to a fresh temp dir. Returns (archive_path, temp_dir).

    The caller owns *temp_dir* and must remove it (e.g. ``shutil.rmtree``).
    """
    tmp = Path(tempfile.mkdtemp(prefix="cremind-bp-hub-"))
    saved = download_hub_blueprint(link, tmp)
    return saved, tmp
