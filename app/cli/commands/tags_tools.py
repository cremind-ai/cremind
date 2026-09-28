"""`cremind tags tools ...` — Cremind Tag hardware tools for developers and
factory stations.

These are the host-side utilities that used to ship as the ``cremind-tag``
command of the firmware repository: flash released firmware, enroll a tag over
SWD, look after a bridge's maintenance port (font packs), provision a mesh by
hand, build and size font packs, render previews, run the simulator, inspect a
local delivery queue, run a manual (legacy) runtime daemon, and collect
diagnostics. People setting up hardware never need them: Settings → Tags (or
`cremind tags devices`) does that.

Everything after ``tools`` goes to :mod:`app.tags.runtime.cli` unchanged
(``cremind tags tools --help`` lists the commands). The tools run on THIS
computer against devices attached to it; most need the ``tags`` components
(``cremind features install tags``). The module is imported only when the
command runs, so the rest of the CLI never loads the runtime.
"""

from __future__ import annotations

import sys

import typer

_PASSTHROUGH = {"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []}


def register(tags_app: typer.Typer) -> None:
    @tags_app.command("tools", context_settings=_PASSTHROUGH, add_help_option=False)
    def tools(ctx: typer.Context) -> None:
        """Hardware tools on this computer: firmware, enrollment, bridge fonts, mesh, simulator, diagnostics."""
        try:
            from app.tags.runtime.cli.main import app as tools_app
        except ImportError as exc:
            sys.stderr.write(f"The Cremind Tag hardware tools cannot start ({exc}).\n"
                             "Install their components first: cremind features install tags\n")
            raise typer.Exit(1) from None
        tools_app(args=list(ctx.args), prog_name="cremind tags tools")


__all__ = ["register"]
