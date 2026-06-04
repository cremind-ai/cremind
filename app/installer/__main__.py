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

from app.installer import catalog, tui
from app.installer.output import TuiResult, write


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.installer")
    p.add_argument("--output", required=True, help="Write KEY=VALUE result here.")
    p.add_argument("--catalog", required=True, help="Path to install/_catalog.json.")

    # Pre-populated values forwarded from the shell front-end. Each is
    # treated as a default that short-circuits the matching screen.
    p.add_argument("--channel", default="", choices=["", "production", "test", "dev"])
    p.add_argument("--deployment", default="")
    p.add_argument("--mode", default="")
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
    return p


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
        custom_listen_host=args.custom_listen_host,
        custom_public_url=args.custom_public_url,
        custom_allowed_origins=args.custom_allowed_origins,
        custom_wizard_preset=args.custom_wizard_preset,
    )

    try:
        result = tui.run(
            catalog=cat,
            initial=initial,
            in_container=args.in_container == "1",
            has_docker=args.has_docker == "1",
            electron_version=args.electron_version,
        )
    except KeyboardInterrupt:
        print("installer: cancelled.", file=sys.stderr)
        return 1

    if result is None:
        return 1

    try:
        write(result, args.output)
    except OSError as exc:
        print(f"installer: failed to write output {args.output}: {exc}", file=sys.stderr)
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
