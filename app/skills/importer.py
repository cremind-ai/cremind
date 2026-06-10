"""Skill import: from uploaded archives and public GitHub repositories.

Two entry points are exposed to the API layer, both **synchronous** (the API
wraps them in ``asyncio.to_thread`` so the event loop is never blocked):

- :func:`install_archive`  -- extract an archive (``.zip``/``.tar.gz``/...) and
  install every valid skill it contains.
- :func:`install_github`   -- fetch a public GitHub repo (``git clone`` when git
  is available, else the codeload tarball via ``httpx``) and install its skills.

Both ultimately funnel through :func:`install_skills_from_dir`, which discovers
skills with :func:`app.skills.scanner.find_skill_dirs`, rejects name collisions
(never overwriting an existing skill or a built-in), and copies the validated
directories into ``<CREMIND_SYSTEM_DIR>/<profile>/skills``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import httpx

from app.skills.scanner import find_skill_dirs
from app.skills.sync import (
    builtin_skill_dir_names,
    profile_skills_dir,
)
from app.utils.logger import logger

# Windows: don't pop a console window when shelling out to git.
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Branches to try, in order, when downloading a repo tarball.
_DEFAULT_BRANCHES = ("main", "master")

# Cap the GitHub tarball download so a hostile/huge repo can't exhaust disk.
_MAX_TARBALL_BYTES = 100 * 1024 * 1024  # 100 MiB

_GITHUB_URL_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s#?]+)",
    re.IGNORECASE,
)
_OWNER_REPO_RE = re.compile(r"^(?P<owner>[^/\s]+)/(?P<repo>[^/\s#?]+)$")


class SkillImportError(Exception):
    """Raised when an import cannot be completed (bad input, no skills, etc.)."""


# ── installation ────────────────────────────────────────────────────────────


def install_skills_from_dir(source_root: Path, profile: str) -> dict:
    """Discover and install every valid skill found under *source_root*.

    Rejects (skips) a skill whose directory name collides with an existing skill
    in the profile or with a shipped built-in -- imports never overwrite. Raises
    :class:`SkillImportError` when no valid skill is found, or when every
    discovered skill was skipped due to collisions.

    Returns ``{"installed": [names], "skipped": [{"name", "reason"}]}``.
    """
    discovered = find_skill_dirs(source_root)
    if not discovered:
        raise SkillImportError(
            "No valid skill found — an importable skill must contain a SKILL.md "
            "with 'name' and 'description'."
        )

    skills_root = profile_skills_dir(profile)
    skills_root.mkdir(parents=True, exist_ok=True)
    builtins = builtin_skill_dir_names()

    installed: list[str] = []
    skipped: list[dict] = []

    for info in discovered:
        dir_name = info.dir_path.name
        dest = skills_root / dir_name
        if dir_name in builtins:
            skipped.append({"name": info.name, "reason": "matches a built-in skill"})
            continue
        if dest.exists():
            skipped.append({"name": info.name, "reason": "a skill with this name already exists"})
            continue
        # Guard: the basename copy must land strictly inside the skills dir.
        if dest.resolve().parent != skills_root.resolve():
            skipped.append({"name": info.name, "reason": "invalid skill directory name"})
            continue
        shutil.copytree(info.dir_path, dest)
        installed.append(info.name)
        logger.info(f"Imported skill '{info.name}' into profile '{profile}'")

    if not installed:
        reasons = "; ".join(f"{s['name']}: {s['reason']}" for s in skipped)
        raise SkillImportError(f"Nothing imported — {reasons}")

    return {"installed": installed, "skipped": skipped}


# ── archive import ──────────────────────────────────────────────────────────


def extract_archive(archive_path: Path, dest: Path) -> None:
    """Extract *archive_path* into *dest* using :func:`shutil.unpack_archive`.

    Supports every format ``shutil`` registers (zip, tar, tar.gz/tgz, tar.bz2,
    tar.xz). Raises :class:`SkillImportError` on an unknown/corrupt archive.
    """
    dest.mkdir(parents=True, exist_ok=True)
    try:
        # ``filename`` carries the suffix shutil needs to pick the unpacker.
        shutil.unpack_archive(str(archive_path), str(dest))
    except (shutil.ReadError, ValueError) as exc:
        raise SkillImportError(f"Could not read archive: {exc}") from exc


def install_archive(archive_bytes_path: Path, original_filename: str, profile: str) -> dict:
    """Install skills from an already-saved archive file.

    *archive_bytes_path* is a temp file holding the uploaded bytes;
    *original_filename* preserves the suffix so ``shutil`` can pick the
    unpacker. Extraction happens in an isolated temp dir; only validated skill
    directories are copied (by basename) into the profile skills dir, so a
    zip-slip attempt cannot place files outside it.
    """
    suffix_name = os.path.basename(original_filename) or archive_bytes_path.name
    with tempfile.TemporaryDirectory(prefix="cremind-skill-archive-") as tmp:
        tmp_path = Path(tmp)
        # Give the archive its original name so unpack_archive infers the format.
        named = tmp_path / "__upload__" / suffix_name
        named.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(archive_bytes_path, named)
        extract_dir = tmp_path / "extracted"
        extract_archive(named, extract_dir)
        return install_skills_from_dir(extract_dir, profile)


# ── GitHub import ───────────────────────────────────────────────────────────


def parse_github_url(url: str) -> tuple[str, str]:
    """Parse *url* into ``(owner, repo)``; accepts full URL or ``owner/repo``.

    Strips a trailing ``.git`` and ``/`` from the repo. Raises
    :class:`SkillImportError` for anything that isn't a recognizable GitHub repo.
    """
    raw = (url or "").strip()
    if not raw:
        raise SkillImportError("A GitHub repository URL is required")
    match = _GITHUB_URL_RE.match(raw) or _OWNER_REPO_RE.match(raw)
    if not match:
        raise SkillImportError(
            "Not a recognizable GitHub repository URL "
            "(expected https://github.com/<owner>/<repo>)"
        )
    owner = match.group("owner")
    repo = match.group("repo")
    if repo.endswith(".git"):
        repo = repo[:-4]
    repo = repo.rstrip("/")
    if not owner or not repo:
        raise SkillImportError("Could not determine owner/repo from the URL")
    return owner, repo


def _git_clone(owner: str, repo: str, dest: Path) -> bool:
    """Try ``git clone --depth 1``. Returns True on success, False otherwise."""
    git = shutil.which("git")
    if not git:
        return False
    clone_url = f"https://github.com/{owner}/{repo}.git"
    try:
        result = subprocess.run(
            [git, "clone", "--depth", "1", clone_url, str(dest)],
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"git clone failed for {owner}/{repo}: {exc}")
        return False
    if result.returncode != 0:
        logger.warning(
            f"git clone exited {result.returncode} for {owner}/{repo}: "
            f"{result.stderr.strip()[:300]}"
        )
        return False
    return True


def _download_tarball(owner: str, repo: str, dest: Path) -> bool:
    """Download + extract a repo tarball via httpx. Returns True on success."""
    for branch in _DEFAULT_BRANCHES:
        codeload = f"https://codeload.github.com/{owner}/{repo}/tar.gz/refs/heads/{branch}"
        try:
            with httpx.stream("GET", codeload, follow_redirects=True, timeout=60) as resp:
                if resp.status_code != 200:
                    continue
                with tempfile.NamedTemporaryFile(
                    prefix="cremind-gh-", suffix=".tar.gz", delete=False
                ) as fh:
                    tar_path = Path(fh.name)
                    total = 0
                    for chunk in resp.iter_bytes(1 << 20):
                        total += len(chunk)
                        if total > _MAX_TARBALL_BYTES:
                            fh.close()
                            tar_path.unlink(missing_ok=True)
                            raise SkillImportError("Repository tarball is too large")
                        fh.write(chunk)
            try:
                extract_archive(tar_path, dest)
            finally:
                tar_path.unlink(missing_ok=True)
            return True
        except httpx.HTTPError as exc:
            logger.warning(f"Tarball download failed for {owner}/{repo}@{branch}: {exc}")
            continue
    return False


def fetch_github_repo(url: str, dest: Path) -> None:
    """Fetch a public GitHub repo into *dest* (git clone, else tarball).

    Raises :class:`SkillImportError` if neither method succeeds (private repo,
    typo'd URL, no ``main``/``master`` branch, network failure, ...).
    """
    owner, repo = parse_github_url(url)
    dest.mkdir(parents=True, exist_ok=True)
    if _git_clone(owner, repo, dest):
        return
    if _download_tarball(owner, repo, dest):
        return
    raise SkillImportError(
        f"Could not fetch '{owner}/{repo}'. Make sure the repository is public "
        "and the URL is correct."
    )


def install_github(url: str, profile: str) -> dict:
    """Fetch a public GitHub repo and install every valid skill it contains."""
    with tempfile.TemporaryDirectory(prefix="cremind-skill-gh-") as tmp:
        repo_dir = Path(tmp) / "repo"
        fetch_github_repo(url, repo_dir)
        return install_skills_from_dir(repo_dir, profile)
