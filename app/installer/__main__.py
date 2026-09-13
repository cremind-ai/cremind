"""Entry point for the Cremind installer TUI.

Invoked by install.sh / install.ps1 via ``uv run --with prompt_toolkit
--with rich python -m app.installer --output <path> --catalog <path>
[pre-populated flag values...]``. Walks the interactive prompts the
shell installers used to ask one-by-one, then writes a ``KEY=VALUE``
file the shell sources to continue the install.

Exit codes:
  0 — TUI exited cleanly; output file written.
  1 — user cancelled (Ctrl-C / q); shell falls back / exits.
  2 — bad arguments / catalog load failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.installer import catalog, tui
from app.installer.output import TuiResult, write

# Written into the --output file when the user cancels. ``uv run`` does not
# reliably propagate the child's exit code on Ctrl+C (Windows: it treats the
# interrupt as a clean stop and returns 0), so the shell front-ends cannot
# depend on exit code 1 to detect a cancel. They inspect the output file for
# this sentinel instead — see tui_run_bootstrap (install.sh) /
# Invoke-InstallerTuiBootstrap (install.ps1).
CANCEL_MARKER = "CREMIND_TUI_CANCELLED=1"


def _write_cancel_marker(path: str) -> None:
    """Best-effort: record cancellation for the shell regardless of exit code."""
    try:
        Path(path).write_text(CANCEL_MARKER + "\n", encoding="utf-8")
    except OSError:
        pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.installer")
    p.add_argument("--output", required=True, help="Write KEY=VALUE result here.")
    p.add_argument("--catalog", required=True, help="Path to install/_catalog.json.")

    # Pre-populated values forwarded from the shell front-end. Each is
    # treated as a default that short-circuits the matching screen.
    p.add_argument("--channel", default="", choices=["", "production", "test", "dev"])
    p.add_argument("--deployment", default="")
    p.add_argument("--mode", default="")
    p.add_argument("--ssl", default="", choices=["", "none", "auto", "after-setup"])
    p.add_argument("--ssl-inherited", default="0", choices=["0", "1"])
    p.add_argument("--native-env", default="", help="Previous native installation's .env path.")
    p.add_argument("--docker-env", default="", help="Previous Docker installation's .env path.")
    p.add_argument(
        "--desktop",
        default="",
        choices=["", "1", "0"],
        help="Docker desktop-UI choice: 1 desktop, 0 basic, empty = ask.",
    )
    p.add_argument(
        "--vnc-password",
        default="",
        dest="vnc_password",
        help="Pre-supplied VNC Desktop password; empty = ask when applicable.",
    )
    p.add_argument(
        "--vnc-password-set",
        default="0",
        choices=["0", "1"],
        help="1 if a previous install already has a VNC password, which makes "
             "an empty answer mean 'keep the existing one'.",
    )
    p.add_argument("--version", default="", dest="version_spec")
    p.add_argument("--host", default="", dest="app_host")
    p.add_argument("--listen-host", default="", dest="custom_listen_host")
    p.add_argument("--public-url", default="", dest="custom_public_url")
    p.add_argument("--allowed-origins", default="", dest="custom_allowed_origins")
    p.add_argument("--wizard-preset", default="", dest="custom_wizard_preset")
    p.add_argument("--electron-version", default="")
    p.add_argument(
        "--in-container",
        default="0",
        choices=["0", "1"],
        help="1 if the installer detected it's running inside a container.",
    )
    p.add_argument(
        "--has-docker",
        default="0",
        choices=["0", "1"],
        help="1 if Docker is detected and usable.",
    )

    # Kubernetes mode. The value flags mirror TuiResult slots (a non-empty
    # value short-circuits its screen); the capability/context flags are
    # context only and are never written back.
    p.add_argument("--kube-context", default="", dest="kube_context")
    p.add_argument("--kubeconfig", default="", dest="kube_config_file")
    p.add_argument("--kube-namespace", default="", dest="kube_namespace")
    p.add_argument("--k8s-release-name", default="", dest="k8s_release_name")
    p.add_argument("--k8s-app-url", default="", dest="k8s_app_url")
    p.add_argument(
        "--k8s-legacy-postgres-image",
        default="",
        choices=["", "yes", "no"],
        dest="k8s_legacy_postgres_image",
    )
    p.add_argument(
        "--k8s-delete-postgres-data",
        default="",
        choices=["", "yes", "no"],
        dest="k8s_delete_postgres_data",
    )
    p.add_argument("--k8s-extra-set", default="", dest="k8s_extra_set")
    p.add_argument(
        "--has-kubectl",
        default="0",
        choices=["0", "1"],
        help="1 if kubectl is on PATH with at least one kubeconfig context.",
    )
    p.add_argument(
        "--has-helm",
        default="0",
        choices=["0", "1"],
        help="1 if helm 3 is on PATH.",
    )
    p.add_argument(
        "--kube-contexts-file",
        default="",
        help="File of kubeconfig contexts, one per line: "
             "name<TAB>server<TAB>namespace<TAB>kubeconfig<TAB>current.",
    )
    return p


def _read_kube_contexts(path: str) -> tuple[tui.KubeContext, ...]:
    """Parse the contexts file the shell wrote, tolerating its absence.

    A missing or unreadable file is not fatal: the context screen shows its
    own "no contexts" message, and the shell refuses a kubernetes install
    without contexts long before this point.
    """
    if not path:
        return ()
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        print(f"installer: could not read {path}: {exc}", file=sys.stderr)
        return ()
    return tui.parse_kube_contexts(text)


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    try:
        cat = catalog.load(args.catalog)
    except (FileNotFoundError, ValueError, KeyError) as exc:
        print(f"installer: failed to load catalog {args.catalog}: {exc}", file=sys.stderr)
        return 2

    initial = TuiResult(
        channel=args.channel,
        version_spec=args.version_spec,
        deployment=args.deployment,
        app_host=args.app_host,
        mode=args.mode,
        ssl_choice=args.ssl,
        desktop=args.desktop,
        vnc_password=args.vnc_password,
        custom_listen_host=args.custom_listen_host,
        custom_public_url=args.custom_public_url,
        custom_allowed_origins=args.custom_allowed_origins,
        custom_wizard_preset=args.custom_wizard_preset,
        kube_context=args.kube_context,
        kube_config_file=args.kube_config_file,
        kube_namespace=args.kube_namespace,
        k8s_release_name=args.k8s_release_name,
        k8s_app_url=args.k8s_app_url,
        k8s_legacy_postgres_image=args.k8s_legacy_postgres_image,
        k8s_delete_postgres_data=args.k8s_delete_postgres_data,
        k8s_extra_set=args.k8s_extra_set,
    )

    try:
        result = tui.run(
            catalog=cat,
            initial=initial,
            in_container=args.in_container == "1",
            has_docker=args.has_docker == "1",
            has_kubectl=args.has_kubectl == "1",
            has_helm=args.has_helm == "1",
            kube_contexts=_read_kube_contexts(args.kube_contexts_file),
            electron_version=args.electron_version,
            vnc_password_preset=args.vnc_password_set == "1",
            ssl_inherited=args.ssl_inherited == "1",
            native_env=args.native_env,
            docker_env=args.docker_env,
        )
    except KeyboardInterrupt:
        _write_cancel_marker(args.output)
        print("installer: cancelled.", file=sys.stderr)
        return 1

    if result is None:
        _write_cancel_marker(args.output)
        return 1

    try:
        write(result, args.output)
    except (OSError, ValueError) as exc:
        print(f"installer: failed to write output {args.output}: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
