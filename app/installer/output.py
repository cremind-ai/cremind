"""Write the TUI's decisions as a key=value file the install scripts source.

install.sh runs ``. "$TUI_OUT"`` after the TUI exits; install.ps1 parses
the same file with a regex. We keep this format intentionally trivial:
``KEY=VALUE``, one per line, no quoting tricks — values are shell-quoted
by escaping single quotes and wrapping in single quotes when they
contain anything other than the unquoted-safe charset.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class TuiResult:
    """Decisions the TUI gathered, written to disk for the shell to source.

    Every key mirrors a shell variable name in install.sh / install.ps1
    so sourcing the file is a no-op when a value was already supplied
    via flag. Empty strings are written for fields the TUI skipped so
    the shell's existing ``if [ -z "$X" ]`` guards keep working.
    """

    channel: str = ""
    version_spec: str = ""
    deployment: str = ""
    app_host: str = ""
    mode: str = ""
    # Docker desktop-UI choice: "" unset (let the shell decide), "1" desktop,
    # "0" basic. Only meaningful when mode == "docker".
    desktop: str = ""
    custom_listen_host: str = ""
    custom_public_url: str = ""
    custom_allowed_origins: str = ""
    custom_wizard_preset: str = ""

    def as_env_dict(self) -> dict[str, str]:
        return {
            "CHANNEL": self.channel,
            "VERSION_SPEC": self.version_spec,
            "DEPLOYMENT": self.deployment,
            "APP_HOST": self.app_host,
            "MODE": self.mode,
            "DESKTOP_UI": self.desktop,
            "CUSTOM_listen_host": self.custom_listen_host,
            "CUSTOM_public_url": self.custom_public_url,
            "CUSTOM_allowed_origins": self.custom_allowed_origins,
            "CUSTOM_wizard_preset": self.custom_wizard_preset,
        }


_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "_-./:@,+%="
)


def _sh_quote(value: str) -> str:
    if value == "" or all(c in _SAFE for c in value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def write(result: TuiResult, path: str | Path) -> None:
    """Serialise ``result`` to ``path`` as ``KEY=VALUE`` lines.

    Values are single-quote-shell-escaped when they contain anything
    outside the unquoted-safe set so ``. "$path"`` works in bash even
    for URLs that contain ``?`` / ``&`` or hostnames with unusual
    characters. PowerShell's regex-based parser strips the surrounding
    quotes the same way.
    """
    out = Path(path)
    lines = [
        f"{key}={_sh_quote(value)}" for key, value in result.as_env_dict().items()
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
