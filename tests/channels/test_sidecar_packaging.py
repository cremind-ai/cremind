"""Guard the packaging invariants the channel sidecars depend on.

The Zalo/WhatsApp sidecars need their ``package-lock.json`` inside the
installed package: hatchling ships only VCS-tracked files, so the moment a
lockfile stops being tracked it vanishes from the wheel and the channel
cannot start on Docker/Kubernetes — while every test that only imports
Python keeps passing. Same spirit as
``tests/storage/test_migrations_graph.py`` guarding revision-id length.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from app.channels.sidecars.bootstrap import discover_sidecars

REPO_ROOT = Path(__file__).resolve().parents[2]

# Bare module specifiers from `require("x")` / `from "x"` — enough to catch an
# undeclared dependency without parsing JavaScript.
_SPECIFIER = re.compile(
    r"""(?:require\(\s*|\bfrom\s+)["']([^"'./][^"']*)["']""",
)

# Node builtins the sidecars use. Anything prefixed `node:` is skipped outright.
_NODE_BUILTINS = {
    "assert", "buffer", "child_process", "crypto", "events", "fs", "http",
    "https", "net", "os", "path", "process", "readline", "stream", "timers",
    "tls", "url", "util", "worker_threads", "zlib",
}


def _sidecars():
    found = discover_sidecars()
    assert found, "no sidecars discovered — has the layout moved?"
    return found


@pytest.mark.parametrize("sidecar", _sidecars(), ids=lambda p: p.name)
def test_lockfile_is_git_tracked(sidecar):
    """A lockfile that isn't tracked never reaches the wheel."""
    if not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout (e.g. running from an sdist)")

    rel = sidecar.joinpath("package-lock.json").relative_to(REPO_ROOT).as_posix()
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", rel],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert tracked.returncode == 0, (
        f"{rel} is not tracked by git, so hatchling will drop it from the "
        "wheel and the sidecar's `npm ci` will have no lockfile on "
        "Docker/K8S installs. Check the lockfile block in .gitignore."
    )


@pytest.mark.parametrize("sidecar", _sidecars(), ids=lambda p: p.name)
def test_lockfile_matches_its_manifest(sidecar):
    """``npm ci`` refuses a lockfile whose identity drifted from package.json.

    This caught a real one: the whatsapp lockfile still said
    ``openpa-whatsapp-sidecar`` after the project was renamed.
    """
    lock_path = sidecar / "package-lock.json"
    if not lock_path.exists():
        pytest.skip(f"{sidecar.name}: no lockfile checked out")

    pkg = json.loads((sidecar / "package.json").read_text(encoding="utf-8"))
    lock = json.loads(lock_path.read_text(encoding="utf-8"))

    assert lock.get("name") == pkg["name"], (
        f"{sidecar.name}: lockfile name {lock.get('name')!r} != package.json "
        f"{pkg['name']!r}. Regenerate it with `npm install` in {sidecar}."
    )
    assert lock.get("version") == pkg["version"]

    root = lock.get("packages", {}).get("", {})
    assert root.get("dependencies", {}) == pkg.get("dependencies", {}), (
        f"{sidecar.name}: lockfile dependencies are out of sync with "
        f"package.json. Regenerate it with `npm install` in {sidecar}."
    )


@pytest.mark.parametrize("sidecar", _sidecars(), ids=lambda p: p.name)
def test_imports_are_declared_dependencies(sidecar):
    """Relying on a transitive dep works until the tree gets deduped differently."""
    pkg = json.loads((sidecar / "package.json").read_text(encoding="utf-8"))
    declared = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))

    source = (sidecar / "index.js").read_text(encoding="utf-8")
    for spec in _SPECIFIER.findall(source):
        if spec.startswith("node:"):
            continue
        # Scoped packages: @scope/name; everything else: first path segment.
        parts = spec.split("/")
        name = "/".join(parts[:2]) if spec.startswith("@") else parts[0]
        if name in _NODE_BUILTINS:
            continue
        assert name in declared, (
            f"{sidecar.name}/index.js imports {name!r}, which is not in "
            "package.json dependencies — it only resolves as a transitive "
            "dependency today."
        )
