"""Parity + contract guards for the installers' VNC password prompt.

The two installers are hand-maintained mirrors of each other, and nothing
executes either one in CI (they install Python, pull images and start
servers). So the couplings asserted here are exactly the ones that rot
silently:

- the flag exists and is documented in both scripts;
- the *same* validation regex lives in install.sh, install.ps1 AND
  app/installer/tui.py — three hand-written copies of one rule, with no
  compiler to notice when one drifts;
- that regex's charset stays compatible with how each script renders the
  value into the .env (a widened charset would corrupt the file instead of
  being rejected);
- install.ps1's TUI read-back names the key. That switch is a whitelist:
  a missing case drops the TUI's answer on the floor with no error;
- the TUI's output key is NOT ``VNC_PASSWORD`` — install.sh *sources* that
  file and owns that variable name for its own resolution chain;
- both scripts keep a generated-password fallback, so an unattended install
  never blocks on a password nobody can type.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PS1 = REPO_ROOT / "install" / "install.ps1"
SH = REPO_ROOT / "install" / "install.sh"
TUI = REPO_ROOT / "app" / "installer" / "tui.py"
OUTPUT_PY = REPO_ROOT / "app" / "installer" / "output.py"

# The single rule, written out here so a change to it has to be made
# deliberately in four places instead of drifting in one.
EXPECTED_RE = r"^[A-Za-z0-9@%_+=:,.-]{6,8}$"


def _ps1() -> str:
    return PS1.read_text(encoding="utf-8")


def _sh() -> str:
    return SH.read_text(encoding="utf-8")


# ── the flag itself ───────────────────────────────────────────────────────


def test_flag_declared_in_both_installers() -> None:
    assert "[string] $VncPassword" in _ps1()
    sh = _sh()
    assert "--vnc-password)" in sh and "--vnc-password=*)" in sh


def test_flag_documented_in_both_help_texts() -> None:
    assert ".PARAMETER VncPassword" in _ps1()
    # install.sh's --help prints its own header block verbatim.
    header = _sh().split("set -e", 1)[0]
    assert "--vnc-password PW" in header


# ── one rule, three hand-written copies ───────────────────────────────────


def test_the_same_regex_is_used_everywhere() -> None:
    """install.sh, install.ps1 and the TUI must agree on what is valid, or a
    password accepted by one front-end is rejected by the next."""
    assert f"VNC_PASSWORD_RE='{EXPECTED_RE}'" in _sh()
    assert f"$VncPasswordRe = '{EXPECTED_RE}'" in _ps1()
    assert f'VNC_PASSWORD_PATTERN = r"{EXPECTED_RE}"' in TUI.read_text(encoding="utf-8")


def test_the_charset_survives_how_each_script_renders_it() -> None:
    """install.sh renders the .env with ``sed -e "s|__VNC_PASSWORD__|$VNC_PASSWORD|g"``
    and install.ps1 with ``-replace``; the value then sits unquoted in a
    compose .env and inside a TOML basic string. Anything that terminates one
    of those would corrupt the file rather than fail loudly, so the accepted
    charset must exclude it."""
    # The template really is rendered with a '|'-delimited sed expression —
    # if that ever changes, this test's premise needs revisiting, not just
    # the assertion below.
    assert "s|__VNC_PASSWORD__|$VNC_PASSWORD|g" in _sh()

    for hostile in ["|", "&", "\\", "$", "#", " ", '"', "'", "\n"]:
        assert re.match(EXPECTED_RE, f"abc{hostile}12") is None, hostile


def test_the_length_bounds_are_vncs_own() -> None:
    """6 is TigerVNC's minimum; 8 is where the classic DES scheme stops
    reading. A 9th character would be silently ignored, so it is rejected
    rather than accepted-and-truncated."""
    assert re.match(EXPECTED_RE, "abc12") is None       # 5
    assert re.match(EXPECTED_RE, "abc123") is not None  # 6
    assert re.match(EXPECTED_RE, "abc12345") is not None  # 8
    assert re.match(EXPECTED_RE, "abc123456") is None   # 9


# ── plumbing the value from the TUI back into the shell ───────────────────


def test_tui_output_key_is_not_the_shell_variable_name() -> None:
    """install.sh SOURCES the TUI output file and separately assigns
    VNC_PASSWORD from its own flag/previous/generated chain. Emitting the key
    under that name would have the two clobber each other."""
    out = OUTPUT_PY.read_text(encoding="utf-8")
    assert '"VNC_PASSWORD_INPUT": self.vnc_password' in out
    assert '"VNC_PASSWORD":' not in out


def test_powershell_reads_the_key_back() -> None:
    """The TUI read-back is a switch WHITELIST — an absent case silently
    discards the user's answer with no error anywhere."""
    ps1 = _ps1()
    assert "'VNC_PASSWORD_INPUT'" in ps1
    # And it must actually assign the script-scope variable the install reads.
    assert re.search(
        r"'VNC_PASSWORD_INPUT'\s*\{[^}]*Set-Variable -Scope Script VncPassword",
        ps1,
    )


def test_both_scripts_forward_the_flag_to_the_tui() -> None:
    sh = _sh()
    # Both launch paths (dev-python and uv) must forward it, or the TUI
    # re-asks for a password the operator already supplied.
    assert sh.count('--vnc-password "$VNC_PASSWORD_INPUT"') == 2
    assert sh.count('--vnc-password-set "$vnc_pw_preset"') == 2
    assert "$tuiArgs.Add('--vnc-password')" in _ps1()
    assert "$tuiArgs.Add('--vnc-password-set')" in _ps1()


# ── the resolution chain ──────────────────────────────────────────────────


def test_precedence_is_chosen_then_previous_then_generated() -> None:
    """The middle step is the one that matters on a re-install: without it
    every re-run silently rotates a password the user wrote down."""
    assert (
        'VNC_PASSWORD="${VNC_PASSWORD_INPUT:-${PREV_VNC_PASSWORD:-$(gen_secret)}}"'
        in _sh()
    )
    ps1 = _ps1()
    assert re.search(
        r"\$VncPwd = if \(\$VncPassword\) \{ \$VncPassword \}\s*"
        r"elseif \(\$PrevVncPassword\) \{ \$PrevVncPassword \}\s*"
        r"else \{ New-Secret \}",
        ps1,
    )


def test_both_scripts_still_read_the_previous_password() -> None:
    assert "PREV_VNC_PASSWORD=\"$(sed -n 's/^VNC_PASSWORD=//p'" in _sh()
    assert "$PrevVncPassword = ($prevVncLine -replace '^VNC_PASSWORD=', '').Trim()" in _ps1()


def test_unattended_installs_never_prompt() -> None:
    """A scripted install must not block on a question nobody can answer —
    the generated fallback is what keeps that true."""
    sh = _sh()
    assert '[ "$UNATTENDED" -eq 0 ]' in sh
    assert "gen_secret" in sh
    ps1 = _ps1()
    assert "-not $Unattended" in ps1
    assert "New-Secret" in ps1


def test_the_prompt_is_only_asked_when_there_is_a_desktop_to_protect() -> None:
    """The basic image ships no VNC server; asking there would be noise."""
    assert '[ "$MODE" = "docker" ] && [ "$DESKTOP_UI" != "0" ]' in _sh()
    assert "$Mode -eq 'docker' -and $DesktopUi -ne '0'" in _ps1()


def test_the_retry_loop_is_bounded_in_both_scripts() -> None:
    """Every rejected entry re-reads. A tty that exists but only returns EOF
    would spin forever on an unbounded loop, hanging the install with no
    output — so both loops must have a ceiling that falls through to the
    generated-password path."""
    assert 'while [ "$vnc_tries" -lt 5 ]' in _sh()
    assert "$vncTries -lt 5" in _ps1()
