"""detect_project: what makes a folder a project, and the cheap facts read from it.

The facts feed the folder card that answers "the Python robot project with
motion tracking, last year": language, dependencies, README, last commit.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.userdocs.discovery.projects import detect_project, git_last_commit_at


def _files(d: Path, files: dict[str, str]) -> list[str]:
    for name, text in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return sorted(n for n in os.listdir(d))


def test_python_project_with_manifests(tmp_path):
    names = _files(tmp_path, {
        "README.md": "# Robot arm\n",
        "requirements.txt": "opencv-python>=4.8\n# comment\nmediapipe==0.10  # pose\n-r dev.txt\n\nnumpy\n",
        "pyproject.toml": '[project]\nname = "robot"\ndependencies = ["pyserial>=3", "numpy"]\n',
        "track.py": "",
        "servo.py": "",
    })
    p = detect_project(str(tmp_path), names)
    assert p is not None and p["implicit"] is False
    assert set(p["markers"]) == {"README.md", "requirements.txt", "pyproject.toml"}
    assert p["deps"] == ["pyserial", "numpy", "opencv-python", "mediapipe"]
    assert p["languages"] == {"Python": 2}
    assert p["readme"] == "README.md"
    assert p["git_last_commit_at"] is None


def test_package_json_and_go_mod_and_sln(tmp_path):
    names = _files(tmp_path, {
        "package.json": json.dumps({"dependencies": {"react": "^18", "three": "^0.160"}, "devDependencies": {"vite": "^5"}}),
        "go.mod": "module x\n\ngo 1.22\n\nrequire (\n\tgithub.com/a/b v1.0.0\n\tgolang.org/x/net v0.1.0 // indirect\n)\nrequire github.com/c/d v2.0.0\n",
        "App.sln": "",
    })
    p = detect_project(str(tmp_path), names)
    assert p["deps"] == ["github.com/a/b", "golang.org/x/net", "github.com/c/d", "react", "three", "vite"]
    assert "App.sln" in p["markers"]


def test_implicit_project_by_source_count_or_entry_point(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    p = detect_project(str(a), _files(a, {"x.js": "", "y.js": "", "z.js": "", "notes.md": ""}))
    assert p is not None and p["implicit"] is True and p["languages"] == {"JavaScript": 3}
    b = tmp_path / "b"
    b.mkdir()
    assert detect_project(str(b), _files(b, {"main.py": ""}))["implicit"] is True
    c = tmp_path / "c"
    c.mkdir()
    assert detect_project(str(c), _files(c, {"a.py": "", "b.py": "", "notes.md": "", "x.md": "", "y.md": ""})) is None


def test_not_a_project(tmp_path):
    assert detect_project(str(tmp_path), _files(tmp_path, {"invoice.pdf": "", "photo.jpg": ""})) is None


def test_git_last_commit_from_reflog(tmp_path):
    logs = tmp_path / ".git" / "logs"
    logs.mkdir(parents=True)
    (logs / "HEAD").write_text(
        "0000 1111 Lee Nguyen <lee@example.com> 1700000000 +0700\tcommit (initial): start\n"
        "1111 2222 Lee Van Nguyen <lee@example.com> 1720000000 +0700\tcommit: motion tracking\n",
        encoding="utf-8",
    )
    p = detect_project(str(tmp_path), [])
    assert p["markers"] == [".git"] and p["git_last_commit_at"] == 1720000000


def test_git_worktree_file_points_at_the_real_git_dir(tmp_path):
    real = tmp_path / "repo.git"
    (real / "logs").mkdir(parents=True)
    (real / "logs" / "HEAD").write_text("a b N <e> 1710000000 +0000\tcheckout\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../repo.git\n", encoding="utf-8")
    assert git_last_commit_at(str(wt)) == 1710000000


def test_deps_are_capped(tmp_path):
    names = _files(tmp_path, {"requirements.txt": "\n".join(f"pkg{i}" for i in range(100))})
    assert len(detect_project(str(tmp_path), names)["deps"]) == 40


def test_malformed_manifests_do_not_raise(tmp_path):
    names = _files(tmp_path, {"pyproject.toml": "[project\n", "package.json": "{not json", "Cargo.toml": "x = ["})
    p = detect_project(str(tmp_path), names)
    assert p is not None and p["deps"] == []
