"""Is this folder a code project, and what is it made of?

"The Python robot project with motion tracking, from last year" is a question
about a *folder*, not a file: its language, its dependencies (``opencv``,
``mediapipe``), its README and when it was last worked on. The folder card
built from :func:`detect_project` is what makes that searchable.

Everything here is cheap by design — it runs for every folder in a scan:
only the folder's own entries are looked at (no recursion), each manifest file
is read up to a small cap, and nothing is read from a cloud placeholder.

``.git`` is pruned by the walker, so its one useful fact — when the project was
last committed to — is read here from ``.git/logs/HEAD`` before that happens.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from typing import Any

from app.userdocs.discovery.hashing import read_text_head, read_text_tail

_MAX_DEPS = 40
_MANIFEST_MAX_BYTES = 256 * 1024
_GIT_LOG_TAIL_BYTES = 8 * 1024

_MARKER_NAMES = frozenset({
    "pyproject.toml", "package.json", "setup.py", "cmakelists.txt", "cargo.toml",
    "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
})
_ENTRY_POINTS = frozenset({"main.py", "app.py", "index.js"})
# A folder with this many source files in one language is a project even
# without a manifest (a loose script collection is still what users call
# "my X project").
_IMPLICIT_MIN_FILES = 3

# Programming languages only: three Markdown files are notes, not a project.
_LANGUAGES = {
    ".py": "Python", ".pyw": "Python", ".ipynb": "Jupyter Notebook",
    ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".vue": "Vue", ".svelte": "Svelte",
    ".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".scala": "Scala", ".groovy": "Groovy",
    ".go": "Go", ".rs": "Rust", ".c": "C", ".h": "C",
    ".cpp": "C++", ".cc": "C++", ".cxx": "C++", ".hpp": "C++", ".hh": "C++", ".ino": "Arduino",
    ".cs": "C#", ".fs": "F#", ".vb": "Visual Basic", ".swift": "Swift", ".m": "Objective-C", ".mm": "Objective-C",
    ".rb": "Ruby", ".php": "PHP", ".pl": "Perl", ".lua": "Lua", ".r": "R", ".jl": "Julia",
    ".dart": "Dart", ".ex": "Elixir", ".exs": "Elixir", ".erl": "Erlang", ".hs": "Haskell",
    ".clj": "Clojure", ".sh": "Shell", ".bash": "Shell", ".ps1": "PowerShell",
    ".sql": "SQL", ".sol": "Solidity", ".zig": "Zig", ".nim": "Nim",
}

# A PEP 508 / npm-ish requirement's leading name.
_REQ_NAME_RE = re.compile(r"\s*([A-Za-z0-9@][A-Za-z0-9._/@-]*)")


def _req_name(spec: str) -> str | None:
    m = _REQ_NAME_RE.match(spec)
    return m.group(1) if m else None


def _deps_requirements(text: str) -> list[str]:
    out = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):  # -r other.txt, -e ., --index-url
            continue
        name = _req_name(line)
        if name:
            out.append(name)
    return out


def _deps_pyproject(text: str) -> list[str]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    out = [n for n in (_req_name(str(s)) for s in (data.get("project") or {}).get("dependencies") or []) if n]
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    if isinstance(poetry, dict):
        out.extend(k for k in poetry if k.lower() != "python")
    return out


def _deps_package_json(text: str) -> list[str]:
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    # Runtime dependencies first; dev tooling (vite, typescript) still says
    # what the project is, so it fills whatever the cap leaves.
    for key in ("dependencies", "devDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            out.extend(str(k) for k in section)
    return out


def _deps_cargo(text: str) -> list[str]:
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError):
        return []
    deps = data.get("dependencies") or {}
    return [str(k) for k in deps] if isinstance(deps, dict) else []


def _deps_go_mod(text: str) -> list[str]:
    out = []
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if in_block:
            if line == ")":
                in_block = False
            elif line:
                out.append(line.split()[0])
        elif line.startswith("require"):
            rest = line[len("require"):].strip()
            if rest == "(":
                in_block = True
            elif rest:
                out.append(rest.split()[0])
    return out


def _git_dir(dir_abs: str) -> str | None:
    """The repository directory for ``dir_abs/.git`` — a directory, or a
    ``gitdir: <path>`` file (worktrees, submodules)."""
    dot_git = os.path.join(dir_abs, ".git")
    if os.path.isdir(dot_git):
        return dot_git
    head = read_text_head(dot_git, 4096)
    if head and head.startswith("gitdir:"):
        lines = head[len("gitdir:"):].strip().splitlines()
        target = lines[0].strip() if lines else ""
        if target:
            path = target if os.path.isabs(target) else os.path.join(dir_abs, target)
            return os.path.normpath(path)
    return None


def git_last_commit_at(dir_abs: str) -> int | None:
    """Unix time of the last reflog entry of ``HEAD`` (a commit, checkout, pull…).

    A reflog line is ``<old> <new> <name> <email> <unix-time> <tz>\\t<message>``;
    the author name may contain spaces, so the time is read from the right.
    """
    git_dir = _git_dir(dir_abs)
    if not git_dir:
        return None
    tail = read_text_tail(os.path.join(git_dir, "logs", "HEAD"), _GIT_LOG_TAIL_BYTES)
    if not tail:
        return None
    for line in reversed(tail.splitlines()):
        head = line.split("\t", 1)[0].split()
        if len(head) >= 4:
            try:
                return int(head[-2])
            except ValueError:
                continue
    return None


def detect_project(dir_abs: str, file_names: list[str]) -> dict[str, Any] | None:
    """Project facts for one folder, or ``None`` when it is not a project.

    ``file_names``: the names of the folder's direct entries (the walker's
    view; ``.git`` is found on disk even though the walk prunes it).

    Returns ``{markers, languages, deps, readme, git_last_commit_at, implicit}``:
    ``markers`` the manifest/README/.git names present; ``languages`` source
    file counts per language; ``deps`` dependency names from the manifests
    (at most 40); ``readme`` the README's file name, relative to the folder
    (the caller reads its head); ``implicit`` True when there is no marker
    but the folder qualifies by its source files or an entry-point script.
    """
    markers: list[str] = []
    readme: str | None = None
    languages: dict[str, int] = {}
    manifests: list[tuple[str, str]] = []
    has_entry_point = False

    for name in sorted(file_names):
        low = name.lower()
        if low.startswith("readme"):
            markers.append(name)
            # Prefer README.md over README.txt over a bare README.
            if readme is None or (low.endswith(".md") and not readme.lower().endswith(".md")):
                readme = name
        elif low in _MARKER_NAMES or low.endswith(".sln") or (low.startswith("requirements") and low.endswith(".txt")):
            markers.append(name)
            manifests.append((name, low))
        if low in _ENTRY_POINTS:
            has_entry_point = True
        lang = _LANGUAGES.get(os.path.splitext(low)[1])
        if lang:
            languages[lang] = languages.get(lang, 0) + 1

    git_at = git_last_commit_at(dir_abs)
    if ".git" in file_names or _git_dir(dir_abs):
        markers.append(".git")

    implicit = False
    if not markers:
        implicit = has_entry_point or any(n >= _IMPLICIT_MIN_FILES for n in languages.values())
        if not implicit:
            return None

    deps: list[str] = []
    for name, low in manifests:
        if len(deps) >= _MAX_DEPS:
            break
        if low.startswith("requirements"):
            parser = _deps_requirements
        elif low == "pyproject.toml":
            parser = _deps_pyproject
        elif low == "package.json":
            parser = _deps_package_json
        elif low == "cargo.toml":
            parser = _deps_cargo
        elif low == "go.mod":
            parser = _deps_go_mod
        else:
            continue
        text = read_text_head(os.path.join(dir_abs, name), _MANIFEST_MAX_BYTES)
        if text:
            for dep in parser(text):
                if dep not in deps:
                    deps.append(dep)
    return {
        "markers": markers,
        "languages": dict(sorted(languages.items(), key=lambda kv: (-kv[1], kv[0]))),
        "deps": deps[:_MAX_DEPS],
        "readme": readme,
        "git_last_commit_at": git_at,
        "implicit": implicit,
    }


__all__ = ["detect_project", "git_last_commit_at"]
