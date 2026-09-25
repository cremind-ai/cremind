"""The Docker documents-folder question in the installer TUI.

The answer travels a long way: typed here, written to the file install.sh
sources and install.ps1 parses, then appended unquoted to the compose .env as
CREMIND_HOST_DOCUMENTS. So these tests pin the output keys and their quoting
(Windows paths with apostrophes and spaces included), the validation rule the
compose .env forces, and the screen's skip / Back behaviour.

The catalog text comes from install/_catalog.json's ``[docker_documents]``
section. Assertions compare against whatever the loaded catalog holds, so
they hold with the section present or with the loader's fallback text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.installer import __main__ as installer_main
from app.installer import catalog, tui
from app.installer.output import TuiResult, write


REPO_ROOT = Path(__file__).resolve().parents[2]
CATALOG_PATH = REPO_ROOT / "install" / "_catalog.json"

WINDOWS_PATH = r"C:\Users\John's Docs"
POSIX_PATH = "/home/ann/My Documents"
# Absolute on the platform running the tests, so normalization leaves it be.
NATIVE_ABS_PATH = WINDOWS_PATH if os.name == "nt" else POSIX_PATH


# ── output keys and quoting ──────────────────────────────────────────────


def _parse_like_install_ps1(text: str) -> dict[str, str]:
    """Parse the TUI output the way install.ps1's read-back does.

    Strip the outer single quotes, then turn each ``'\\''`` back into ``'``
    — the sequence output._sh_quote writes for an apostrophe.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        quoted = re.match(r"^'(.*)'$", value)
        if quoted:
            value = quoted.group(1).replace("'\\''", "'")
        out[key] = value
    return out


def _working_bash() -> str | None:
    """A bash that actually runs, or None (a WSL stub without a distro fails)."""
    bash = shutil.which("bash")
    if not bash:
        return None
    try:
        probe = subprocess.run(
            [bash, "-c", "printf ok"], capture_output=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bash if probe.returncode == 0 and probe.stdout == b"ok" else None


@pytest.mark.parametrize(
    ("value", "line"),
    [
        (WINDOWS_PATH, "DOCUMENTS_DIR_INPUT='C:\\Users\\John'\\''s Docs'"),
        (POSIX_PATH, "DOCUMENTS_DIR_INPUT='/home/ann/My Documents'"),
        # The forward-slash form a previous .env holds needs no quoting.
        ("C:/Users/x/Documents", "DOCUMENTS_DIR_INPUT=C:/Users/x/Documents"),
    ],
)
def test_documents_dir_round_trips(value: str, line: str, tmp_path: Path) -> None:
    out = tmp_path / "tui.out"
    write(TuiResult(mode="docker", documents_dir=value, documents_access="ro"), out)
    text = out.read_text(encoding="utf-8")
    assert line in text.splitlines()
    parsed = _parse_like_install_ps1(text)
    assert parsed["DOCUMENTS_DIR_INPUT"] == value
    assert parsed["DOCUMENTS_ACCESS_INPUT"] == "ro"


@pytest.mark.parametrize("value", [WINDOWS_PATH, POSIX_PATH])
def test_documents_dir_survives_a_real_bash_source(value: str, tmp_path: Path) -> None:
    """install.sh runs ``. "$tui_out"``; a real bash must read the value back."""
    bash = _working_bash()
    if bash is None:
        pytest.skip("no working bash on this machine")
    out = tmp_path / "tui.out"
    write(TuiResult(mode="docker", documents_dir=value, documents_access="rw"), out)
    # Text mode folds Windows' CRLF: install.sh only ever sources this file on
    # a POSIX host, where write() emits plain LF.
    script = out.read_text(encoding="utf-8")
    proc = subprocess.run(
        [
            bash,
            "-c",
            'eval "$(cat)"; printf "%s|%s" "$DOCUMENTS_DIR_INPUT" "$DOCUMENTS_ACCESS_INPUT"',
        ],
        input=script.encode("utf-8"),
        capture_output=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode("utf-8") == f"{value}|rw"


def test_documents_keys_never_shadow_a_shell_variable(tmp_path: Path) -> None:
    """Sourcing must not set what install.sh resolves itself: the .env key,
    the env-var fallbacks, or a bare DOCUMENTS_DIR."""
    out = tmp_path / "tui.out"
    write(TuiResult(documents_dir="/x", documents_access="rw"), out)
    keys = [line.split("=", 1)[0] for line in out.read_text(encoding="utf-8").splitlines()]
    assert "DOCUMENTS_DIR_INPUT" in keys
    assert "DOCUMENTS_ACCESS_INPUT" in keys
    assert not [k for k in keys if k.startswith("CREMIND_")]
    assert "DOCUMENTS_DIR" not in keys
    assert "DOCUMENTS_ACCESS" not in keys


def test_unset_documents_keys_are_written_empty(tmp_path: Path) -> None:
    out = tmp_path / "tui.out"
    write(TuiResult(mode="native"), out)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert "DOCUMENTS_DIR_INPUT=" in lines
    assert "DOCUMENTS_ACCESS_INPUT=" in lines


# ── validation and normalization ─────────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [WINDOWS_PATH, POSIX_PATH, "C:/Users/x/Documents", "~/Documents", "~", "docs"],
)
def test_validate_documents_dir_accepts(value: str, tmp_path: Path) -> None:
    assert tui.validate_documents_dir(value, home=str(tmp_path)) is None


@pytest.mark.parametrize(
    ("value", "named"),
    [
        ("/data/a$b", "$"),
        ("/data/a#b", "#"),
        ('/data/a"b', '"'),
        ("/data/a\nb", "line break"),
        ("/data/a\rb", "carriage return"),
    ],
)
def test_validate_documents_dir_names_the_character(value: str, named: str) -> None:
    err = tui.validate_documents_dir(value)
    assert err is not None
    assert named in err


@pytest.mark.parametrize("value", [" /data/x", "/data/x ", "\t/data/x", "/data/x\t"])
def test_validate_documents_dir_rejects_surrounding_whitespace(value: str) -> None:
    err = tui.validate_documents_dir(value)
    assert err is not None
    assert "starts or ends" in err


@pytest.mark.parametrize("value", ["", "   "])
def test_validate_documents_dir_requires_a_value(value: str) -> None:
    assert tui.validate_documents_dir(value) is not None


def test_validate_documents_dir_checks_the_resolved_path(tmp_path: Path) -> None:
    """``~`` can bring in a home that the .env cannot carry either."""
    home = str(tmp_path / "a#b")
    err = tui.validate_documents_dir("~/Docs", home=home)
    assert err is not None
    assert "#" in err and home in err


def test_normalize_documents_dir(tmp_path: Path) -> None:
    home = str(tmp_path)
    assert tui.normalize_documents_dir("~", home=home) == home
    assert tui.normalize_documents_dir("~/Docs", home=home) == home + "/Docs"
    assert tui.normalize_documents_dir("~\\Docs", home=home) == home + "\\Docs"
    # Only a bare ~ is a home: ~bob is resolved as an ordinary relative path.
    assert tui.normalize_documents_dir("~bob/x", home=home) == os.path.abspath("~bob/x")
    assert tui.normalize_documents_dir("docs") == os.path.abspath("docs")
    # An absolute path comes back untouched — separators included.
    assert tui.normalize_documents_dir(NATIVE_ABS_PATH) == NATIVE_ABS_PATH
    if os.name == "nt":
        assert tui.normalize_documents_dir("C:/Users/x/Documents") == "C:/Users/x/Documents"


def test_read_env_value_tolerates_a_bom_and_quotes(tmp_path: Path) -> None:
    """install.ps1 writes .env with ``Set-Content -Encoding utf8`` — a BOM on
    Windows PowerShell 5.1."""
    env = tmp_path / ".env"
    env.write_bytes(
        "CREMIND_HOST_DOCUMENTS=C:/Users/John's Docs\n"
        "# CREMIND_DOCUMENTS_READ_ONLY=false\n"
        'CREMIND_DOCUMENTS_READ_ONLY="true"\n'.encode("utf-8-sig")
    )
    assert tui._read_env_value(str(env), "CREMIND_HOST_DOCUMENTS") == "C:/Users/John's Docs"
    assert tui._read_env_value(str(env), "CREMIND_DOCUMENTS_READ_ONLY") == "true"
    assert tui._read_env_value(str(env), "MISSING") == ""
    assert tui._read_env_value(str(tmp_path / "nope"), "X") == ""
    assert tui._read_env_value("", "X") == ""


# ── catalog ──────────────────────────────────────────────────────────────


_DOC_KEYS = (
    "prompt",
    "hint",
    "access_prompt",
    "rw_label",
    "ro_label",
    "rw_disclosure",
    "ro_disclosure",
    "linux_owner_note",
    "macos_privacy_note",
    "wsl_note",
)


def test_catalog_reads_every_documents_key() -> None:
    """The real catalog yields text for every key, from the section or the
    fallback, so the screen never shows a blank prompt."""
    dd = catalog.load(CATALOG_PATH).docker_documents
    for key in _DOC_KEYS:
        assert getattr(dd, key), key


def test_catalog_documents_section_overrides_the_fallback(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text(
        json.dumps({"docker_documents": {"prompt": "Pick one", "ro_label": "RO"}}),
        encoding="utf-8",
    )
    dd = catalog.load(path).docker_documents
    assert dd.prompt == "Pick one"
    assert dd.ro_label == "RO"
    # A key the section does not carry falls back on its own.
    assert dd.rw_label == catalog.DockerDocuments.rw_label


def test_catalog_without_the_documents_section_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "catalog.json"
    path.write_text("{}", encoding="utf-8")
    assert catalog.load(path).docker_documents == catalog.DockerDocuments()


# ── the screen ───────────────────────────────────────────────────────────


@pytest.fixture()
def loaded_catalog() -> catalog.Catalog:
    return catalog.load(CATALOG_PATH)


def _ctx(cat: catalog.Catalog, **overrides: object) -> tui.Context:
    defaults = dict(catalog=cat, in_container=False, has_docker=True, electron_version="")
    defaults.update(overrides)
    return tui.Context(**defaults)  # type: ignore[arg-type]


class _Dialogs:
    """Scripted ``_text`` / ``_radio`` stand-ins that record their kwargs."""

    def __init__(self, texts=(), radios=()) -> None:
        self.texts = list(texts)
        self.radios = list(radios)
        self.text_calls: list[dict] = []
        self.radio_calls: list[dict] = []

    def text(self, **kwargs):
        self.text_calls.append(kwargs)
        if not self.texts:
            pytest.fail("unexpected folder prompt")
        return self.texts.pop(0)

    def radio(self, **kwargs):
        self.radio_calls.append(kwargs)
        if not self.radios:
            pytest.fail("unexpected access prompt")
        return self.radios.pop(0)


@pytest.fixture()
def dialogs(monkeypatch: pytest.MonkeyPatch) -> _Dialogs:
    d = _Dialogs()
    monkeypatch.setattr(tui, "_text", d.text)
    monkeypatch.setattr(tui, "_radio", d.radio)
    monkeypatch.setattr(tui, "_message", lambda **_k: pytest.fail("opened a message"))
    monkeypatch.setattr(tui, "_host_platform", lambda: "windows")
    return d


@pytest.mark.parametrize("mode", ["native", "kubernetes", ""])
def test_screen_documents_is_docker_only(
    mode: str, loaded_catalog: catalog.Catalog, dialogs: _Dialogs
) -> None:
    state = TuiResult(mode=mode)
    new_state, action = tui.screen_documents(state, _ctx(loaded_catalog))
    assert (new_state, action) == (state, "skip")


def test_screen_documents_skips_when_both_came_by_flag(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs
) -> None:
    state = TuiResult(mode="docker", documents_dir=WINDOWS_PATH, documents_access="rw")
    new_state, action = tui.screen_documents(state, _ctx(loaded_catalog))
    assert (new_state, action) == (state, "skip")


def test_screen_documents_asks_folder_then_access(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs
) -> None:
    dialogs.texts = [(NATIVE_ABS_PATH, "advance")]
    dialogs.radios = [("ro", "advance")]
    ctx = _ctx(loaded_catalog, documents_default="/srv/shared docs", can_go_back=True)
    new_state, action = tui.screen_documents(TuiResult(mode="docker"), ctx)

    assert action == "advance"
    assert new_state.documents_dir == NATIVE_ABS_PATH
    assert new_state.documents_access == "ro"

    dd = loaded_catalog.docker_documents
    (text_call,) = dialogs.text_calls
    assert text_call["default"] == "/srv/shared docs"  # the shell's pick
    assert text_call["validator"] is tui.validate_documents_dir
    assert text_call["allow_back"] is True
    assert dd.prompt in text_call["text"] and dd.hint in text_call["text"]

    (radio_call,) = dialogs.radio_calls
    assert radio_call["default"] == "rw"
    assert radio_call["values"] == [("rw", dd.rw_label), ("ro", dd.ro_label)]
    # Each disclosure is labelled with the choice it explains.
    assert f"{dd.rw_label}: {dd.rw_disclosure}" in radio_call["text"]
    assert f"{dd.ro_label}: {dd.ro_disclosure}" in radio_call["text"]
    # Back from the access step goes to the folder step, so it is always offered.
    assert radio_call["allow_back"] is True


def test_screen_documents_expands_home(
    loaded_catalog: catalog.Catalog,
    dialogs: _Dialogs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tui.Path, "home", lambda: tmp_path)
    dialogs.texts = [("~/Docs", "advance")]
    dialogs.radios = [("rw", "advance")]
    new_state, _ = tui.screen_documents(TuiResult(mode="docker"), _ctx(loaded_catalog))
    assert new_state.documents_dir == str(tmp_path) + "/Docs"


def test_screen_documents_defaults_come_from_the_previous_env(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_bytes(
        "CREMIND_HOST_DOCUMENTS=C:/Users/John's Docs\n"
        "CREMIND_DOCUMENTS_READ_ONLY=true\n".encode("utf-8-sig")
    )
    dialogs.texts = [("C:/Users/John's Docs", "advance")]
    dialogs.radios = [("ro", "advance")]
    tui.screen_documents(TuiResult(mode="docker"), _ctx(loaded_catalog, docker_env=str(env)))
    assert dialogs.text_calls[0]["default"] == "C:/Users/John's Docs"
    assert dialogs.radio_calls[0]["default"] == "ro"


def test_screen_documents_shell_default_beats_the_previous_env(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs, tmp_path: Path
) -> None:
    env = tmp_path / ".env"
    env.write_text("CREMIND_HOST_DOCUMENTS=/old\n", encoding="utf-8")
    dialogs.texts = [(None, "back")]
    ctx = _ctx(loaded_catalog, docker_env=str(env), documents_default="/new")
    tui.screen_documents(TuiResult(mode="docker"), ctx)
    assert dialogs.text_calls[0]["default"] == "/new"


def test_screen_documents_falls_back_to_home_documents(
    loaded_catalog: catalog.Catalog,
    dialogs: _Dialogs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tui.Path, "home", lambda: tmp_path)
    dialogs.texts = [(None, "back")]
    tui.screen_documents(TuiResult(mode="docker"), _ctx(loaded_catalog))
    assert dialogs.text_calls[0]["default"] == str(tmp_path / "Documents")


def test_screen_documents_back_from_access_reasks_the_folder(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs, tmp_path: Path
) -> None:
    first, second = str(tmp_path / "one"), str(tmp_path / "two")
    dialogs.texts = [(first, "advance"), (second, "advance")]
    dialogs.radios = [(None, "back"), ("rw", "advance")]
    new_state, action = tui.screen_documents(
        TuiResult(mode="docker"), _ctx(loaded_catalog, documents_default="/d")
    )
    assert action == "advance"
    # The folder step comes back showing what was typed, not the default.
    assert [c["default"] for c in dialogs.text_calls] == ["/d", first]
    assert new_state.documents_dir == second
    assert new_state.documents_access == "rw"


@pytest.mark.parametrize("action", ["back", "cancel"])
def test_screen_documents_leaves_on_the_folder_step(
    action: str, loaded_catalog: catalog.Catalog, dialogs: _Dialogs
) -> None:
    state = TuiResult(mode="docker")
    dialogs.texts = [(None, action)]
    new_state, got = tui.screen_documents(state, _ctx(loaded_catalog, can_go_back=True))
    assert (new_state, got) == (state, action)


def test_screen_documents_cancel_on_the_access_step(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs, tmp_path: Path
) -> None:
    state = TuiResult(mode="docker")
    dialogs.texts = [(str(tmp_path), "advance")]
    dialogs.radios = [(None, "cancel")]
    new_state, action = tui.screen_documents(state, _ctx(loaded_catalog))
    assert (new_state, action) == (state, "cancel")


def test_screen_documents_folder_by_flag_asks_only_access(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs
) -> None:
    """With the folder settled, Back on the access step leaves the screen."""
    state = TuiResult(mode="docker", documents_dir=WINDOWS_PATH)
    dialogs.radios = [(None, "back")]
    new_state, action = tui.screen_documents(state, _ctx(loaded_catalog, can_go_back=True))
    assert (new_state, action) == (state, "back")
    assert dialogs.radio_calls[0]["allow_back"] is True

    dialogs.radios = [("ro", "advance")]
    new_state, action = tui.screen_documents(state, _ctx(loaded_catalog))
    assert action == "advance"
    # A flag value is echoed back as given; the shell already resolved it.
    assert new_state.documents_dir == WINDOWS_PATH
    assert new_state.documents_access == "ro"
    assert dialogs.radio_calls[-1]["allow_back"] is False
    assert dialogs.text_calls == []


def test_screen_documents_access_by_flag_asks_only_the_folder(
    loaded_catalog: catalog.Catalog, dialogs: _Dialogs
) -> None:
    dialogs.texts = [(NATIVE_ABS_PATH, "advance")]
    new_state, action = tui.screen_documents(
        TuiResult(mode="docker", documents_access="ro"), _ctx(loaded_catalog)
    )
    assert action == "advance"
    assert new_state.documents_dir == NATIVE_ABS_PATH
    assert new_state.documents_access == "ro"
    assert dialogs.radio_calls == []


@pytest.mark.parametrize(
    ("platform", "folder_notes", "access_notes"),
    [
        ("windows", (), ()),
        ("darwin", ("macos_privacy_note",), ()),
        ("linux", (), ("linux_owner_note",)),
        ("wsl", ("wsl_note",), ("linux_owner_note",)),
    ],
)
def test_screen_documents_platform_notes(
    platform: str,
    folder_notes: tuple[str, ...],
    access_notes: tuple[str, ...],
    loaded_catalog: catalog.Catalog,
    dialogs: _Dialogs,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(tui, "_host_platform", lambda: platform)
    dialogs.texts = [(str(tmp_path), "advance")]
    dialogs.radios = [("rw", "advance")]
    tui.screen_documents(TuiResult(mode="docker"), _ctx(loaded_catalog))

    dd = loaded_catalog.docker_documents
    notes = ("linux_owner_note", "macos_privacy_note", "wsl_note")
    folder_text = dialogs.text_calls[0]["text"]
    access_text = dialogs.radio_calls[0]["text"]
    for note in notes:
        assert (getattr(dd, note) in folder_text) is (note in folder_notes), note
        assert (getattr(dd, note) in access_text) is (note in access_notes), note


def test_screen_documents_sits_after_the_vnc_password() -> None:
    screens = tui._SCREENS
    assert screens.index(tui.screen_documents) == screens.index(tui.screen_vnc_password) + 1
    assert screens.index(tui.screen_documents) < screens.index(tui.screen_confirm)


# ── confirm screen ───────────────────────────────────────────────────────


def _confirm_text(
    state: TuiResult, cat: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> str:
    seen: dict[str, object] = {}

    def fake_choice(**kwargs: object) -> tuple[None, str]:
        seen.update(kwargs)
        return None, "advance"

    monkeypatch.setattr(tui, "_choice", fake_choice)
    tui.screen_confirm(state, _ctx(cat))
    return str(seen["text"])


@pytest.mark.parametrize(("access", "label"), [("rw", "read-write"), ("ro", "read-only")])
def test_confirm_shows_the_documents_row(
    access: str,
    label: str,
    loaded_catalog: catalog.Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = TuiResult(
        mode="docker", desktop="1", documents_dir=WINDOWS_PATH, documents_access=access
    )
    text = _confirm_text(state, loaded_catalog, monkeypatch)
    row = next(line for line in text.splitlines() if line.strip().startswith("Documents"))
    assert WINDOWS_PATH in row and f"({label})" in row


@pytest.mark.parametrize("mode", ["native", "kubernetes"])
def test_confirm_has_no_documents_row_outside_docker(
    mode: str, loaded_catalog: catalog.Catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = _confirm_text(TuiResult(mode=mode, desktop="1"), loaded_catalog, monkeypatch)
    assert not [line for line in text.splitlines() if line.strip().startswith("Documents")]


# ── __main__ plumbing ────────────────────────────────────────────────────


def test_main_plumbs_the_documents_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two values pre-answer their steps; the default is context only."""
    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> TuiResult:
        seen.update(kwargs)
        return kwargs["initial"]  # type: ignore[return-value]

    monkeypatch.setattr(installer_main.tui, "run", fake_run)
    out = tmp_path / "tui.out"
    rc = installer_main.main(
        [
            "--output", str(out), "--catalog", str(CATALOG_PATH),
            "--mode", "docker",
            "--documents-dir", WINDOWS_PATH,
            "--documents-access", "ro",
            "--documents-default", POSIX_PATH,
        ]
    )
    assert rc == 0
    initial: TuiResult = seen["initial"]  # type: ignore[assignment]
    assert initial.documents_dir == WINDOWS_PATH
    assert initial.documents_access == "ro"
    assert seen["documents_default"] == POSIX_PATH

    parsed = _parse_like_install_ps1(out.read_text(encoding="utf-8"))
    assert parsed["DOCUMENTS_DIR_INPUT"] == WINDOWS_PATH
    assert parsed["DOCUMENTS_ACCESS_INPUT"] == "ro"
    # Context only: the default is never written back.
    assert POSIX_PATH not in parsed.values()


def test_main_defaults_leave_the_documents_screen_to_ask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        installer_main.tui, "run", lambda **k: seen.update(k) or k["initial"]
    )
    rc = installer_main.main(
        ["--output", str(tmp_path / "tui.out"), "--catalog", str(CATALOG_PATH)]
    )
    assert rc == 0
    initial: TuiResult = seen["initial"]  # type: ignore[assignment]
    assert (initial.documents_dir, initial.documents_access) == ("", "")
    assert seen["documents_default"] == ""


def test_main_rejects_an_unknown_access(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        installer_main.main(
            [
                "--output", str(tmp_path / "tui.out"), "--catalog", str(CATALOG_PATH),
                "--documents-access", "readonly",
            ]
        )
    assert exc.value.code == 2
