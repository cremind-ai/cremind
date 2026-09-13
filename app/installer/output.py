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
    # Empty = not asked; keep = preserve inherited/existing TLS settings.
    # Kept separate from the shell's SSL_MODE / SSL_EXPLICIT flag variables.
    ssl_choice: str = ""
    # Docker desktop-UI choice: "" unset (let the shell decide), "1" desktop,
    # "0" basic. Only meaningful when mode == "docker".
    desktop: str = ""
    # The VNC password the user typed, or "" when they were not asked / chose
    # to keep the existing one. Written as VNC_PASSWORD_INPUT, deliberately
    # NOT as VNC_PASSWORD: install.sh sources this file and assigns its own
    # VNC_PASSWORD later from the flag/previous/generated chain, so reusing
    # that name would have the TUI's answer clobbered (or clobber it).
    vnc_password: str = ""
    custom_listen_host: str = ""
    custom_public_url: str = ""
    custom_allowed_origins: str = ""
    custom_wizard_preset: str = ""
    # Kubernetes mode. Every one of these is round-tripped through a shell
    # flag (--kube-context, --k8s-release-name, …): the shell passes its
    # current value in and the TUI echoes it back, so sourcing this file can
    # never clobber an answer that came from the command line.
    kube_context: str = ""
    # The kubeconfig file that context came from; "" for the one kubectl
    # reads on its own. Its shell variable is KUBE_CONFIG_FILE, never
    # KUBECONFIG: sourcing this file must not redirect kubectl itself.
    kube_config_file: str = ""
    kube_namespace: str = ""
    k8s_release_name: str = ""
    k8s_app_url: str = ""
    k8s_legacy_postgres_image: str = ""
    k8s_delete_postgres_data: str = ""
    k8s_extra_set: str = ""

    def as_env_dict(self) -> dict[str, str]:
        return {
            "CHANNEL": self.channel,
            "VERSION_SPEC": self.version_spec,
            "DEPLOYMENT": self.deployment,
            "APP_HOST": self.app_host,
            "MODE": self.mode,
            "SSL_CHOICE": self.ssl_choice,
            "DESKTOP_UI": self.desktop,
            "VNC_PASSWORD_INPUT": self.vnc_password,
            "CUSTOM_listen_host": self.custom_listen_host,
            "CUSTOM_public_url": self.custom_public_url,
            "CUSTOM_allowed_origins": self.custom_allowed_origins,
            "CUSTOM_wizard_preset": self.custom_wizard_preset,
            "KUBE_CONTEXT": self.kube_context,
            "KUBE_CONFIG_FILE": self.kube_config_file,
            "KUBE_NAMESPACE": self.kube_namespace,
            "K8S_release_name": self.k8s_release_name,
            "K8S_app_url": self.k8s_app_url,
            "K8S_legacy_postgres_image": self.k8s_legacy_postgres_image,
            "K8S_delete_postgres_data": self.k8s_delete_postgres_data,
            "K8S_extra_set": self.k8s_extra_set,
        }


_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    "_-./:@,+%="
)


def _sh_quote(value: str) -> str:
    # A newline would break the format outright: install.ps1 parses the file
    # line by line (dropping the tail of a multi-line value) and install.sh
    # greps it for the cancel sentinel before sourcing, so a pasted value
    # containing "CREMIND_TUI_CANCELLED=1" on its own line would abort the
    # install. No screen can produce one — prompt_toolkit's TextArea is
    # single-line — but a forwarded flag value can, so refuse it here rather
    # than writing a file the shell will misread.
    if "\n" in value or "\r" in value:
        raise ValueError(f"installer answers must be single-line: {value!r}")
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
