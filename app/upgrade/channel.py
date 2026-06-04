"""Release-channel helpers.

Cremind ships three release channels: ``production`` (PyPI + GitHub
``/releases/latest``), ``test`` (Test PyPI + GitHub prereleases tagged
``v*-rc.N``), and ``dev`` (editable install rooted at the bind-mounted
source checkout — Docker dev mode and ``uv sync`` native dev mode). The
channel is decided at install time and written to ``<CREMIND_SYSTEM_DIR>/.env`` as
``CREMIND_UPGRADE_CHANNEL``; switching channels is a deliberate reinstall,
not a runtime toggle.

Dev exists so the feature installer can tell "we have local source"
apart from "we have a PyPI install" — pinning ``cremind==X.Y.Z`` for a
not-yet-published version only works by accident in dev (the installed
editable happens to match the pinned version), so :func:`pip_spec` drops
the pin in dev to make that explicit.

This module is the single owner of:

  - reading the channel from the environment;
  - the tag-format regex that distinguishes release candidates. The
    canonical RC tag is ``v<X.Y.Z>[.W]rc<N>.dev<M>`` — e.g.
    ``v0.2.1rc3.dev2`` (or the four-segment hotfix form
    ``v0.0.2.1rc1.dev1``). The ``.dev<M>`` suffix is mandatory; bare
    ``v<X.Y.Z>rc<N>`` and the older hyphenated form
    ``v<X.Y.Z>-rc.<N>[.dev.<M>]`` are both rejected;
  - the translation between the GitHub tag (``v0.2.1rc3.dev2``) and the
    PEP 440 release-candidate version pip uses (``0.2.1rc3.dev2``) —
    which is just stripping the leading ``v``, since the tag already
    carries PEP 440 canonical form. release-rc.yml uses the same rule
    when building, so we round-trip trivially.
  - PEP 440 rc-version ordering, since stdlib has no parser for it
    and we don't want to take a runtime dep on ``packaging`` just for
    the upgrader.
  - the channel-shape rules (``matches_channel`` / ``matches_electron_line``
    / ``validate`` / ``filter_same_line``) the install scripts and the
    SetupWizard use to decide "is this version allowed for (channel,
    Electron build)?". ``version_filter`` re-exports them for callers
    that still import from there.

Keeping these here means manifest.py, runner.py, and the install
scripts don't grow tag / version-format knowledge. The install scripts
run this file *standalone* via its ``__main__`` CLI (``resolve`` /
``validate``) during bootstrap — before the cremind wheel is installed —
so this module must stay import-light (stdlib only).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from typing import Literal


Channel = Literal["production", "test", "dev"]


def get_channel() -> Channel:
    """Return the active release channel.

    Defaults to ``production`` when the env var is unset, so an
    existing prod install that hasn't yet had its ``.env`` rewritten
    keeps querying ``releases/latest`` exactly as before.
    """
    value = os.environ.get("CREMIND_UPGRADE_CHANNEL", "").strip().lower()
    if value == "test":
        return "test"
    if value == "dev":
        return "dev"
    return "production"


_RC_TAG = re.compile(r"^v(\d+\.\d+\.\d+(?:\.\d+)?)rc(\d+)\.dev(\d+)$")


def is_rc_tag(tag: str) -> bool:
    """``True`` iff ``tag`` matches the canonical RC shape.

    Canonical shape: ``v<X.Y.Z>[.W]rc<N>.dev<M>``. The ``.dev<M>``
    counter is mandatory; the legacy hyphen/dot variant
    ``v<X.Y.Z>-rc.<N>[.dev.<M>]`` and the bare ``v<X.Y.Z>rc<N>`` (no
    dev) are intentionally rejected.
    """
    return _RC_TAG.match(tag or "") is not None


def tag_to_pep440(tag: str) -> str:
    """Convert an RC tag to its PEP 440 form by stripping the leading ``v``.

    ``v0.2.1rc3.dev2`` → ``0.2.1rc3.dev2``. The rest of the tag is
    already PEP 440 canonical, so there is no rewriting to do. Raises
    ``ValueError`` for tags that don't match the canonical RC shape —
    callers should gate with :func:`is_rc_tag` first.
    """
    if not _RC_TAG.match(tag or ""):
        raise ValueError(f"Not an RC tag: {tag!r}")
    return tag[1:]


_PEP440 = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:rc(\d+)(?:\.dev(\d+))?)?(?:\+([\w.]+))?$")


def parse_pep440(version: str) -> tuple[int, int, int, int, int, int, int, str]:
    """Parse ``MAJOR.MINOR.PATCH[rcN[.devM]][+LOCAL]`` into a sortable tuple.

    Slots:
    ``(major, minor, patch, is_final, rc_counter, is_not_dev, dev_counter, local)``.

    - ``is_final`` is ``0`` for ``rcN`` releases and ``1`` for finals so
      that ``0.2.1rcN < 0.2.1`` regardless of N. ``rc_counter`` orders
      multiple rcs against each other.
    - ``is_not_dev`` is ``0`` for ``rcN.devM`` dev releases and ``1``
      otherwise, so a dev release sorts *before* its rc
      (``0.2.1rc3.dev2 < 0.2.1rc3``) while still sorting above the
      previous patch (``0.2.0 < 0.2.1rc1.dev1``). ``dev_counter`` orders
      multiple dev iterations of the same rc. A bare ``X.Y.Z.devM`` (no
      ``rc``) is intentionally *not* accepted — the release process only
      produces dev releases paired with an rc.
    - ``local`` is the optional PEP 440 *local version* suffix (the part
      after ``+``). Kept last and compared lexically as a string — empty
      sorts lowest, so a version with a local segment is always strictly
      newer than the same version without one. We use this for the
      dev-channel forced-available synth: ``X.Y.Z[rcN[.devM]]+devforced``
      is reliably newer than the working-copy install at the same version.
    """
    m = _PEP440.match(version or "")
    if not m:
        raise ValueError(f"Not a PEP 440 version we recognize: {version!r}")
    major, minor, patch, rc, dev, local = m.groups()
    local_str = local or ""
    if rc is None:
        return (int(major), int(minor), int(patch), 1, 0, 1, 0, local_str)
    if dev is None:
        return (int(major), int(minor), int(patch), 0, int(rc), 1, 0, local_str)
    return (int(major), int(minor), int(patch), 0, int(rc), 0, int(dev), local_str)


def is_newer(latest: str, current: str) -> bool:
    """``True`` iff ``latest`` is strictly newer than ``current`` under PEP 440."""
    return parse_pep440(latest) > parse_pep440(current)


# ── Channel-shape rules ─────────────────────────────────────────────────────
# "Is this version allowed for (channel, Electron build)?" — shared by the
# install scripts (via the CLI below) and the SetupWizard. Re-exported from
# version_filter.py for backwards compatibility.


def matches_channel(version: str, channel: Channel) -> bool:
    """``True`` iff ``version`` (PEP 440 form, no leading ``v``) is shape-valid.

    Production accepts a plain final ``X.Y.Z`` only — an ``rcN``/``devM`` is a
    Test PyPI artifact and a ``+local`` is the dev-channel synth, neither of
    which resolves on PyPI. Test accepts ``X.Y.ZrcN`` and the per-PR dev form
    ``X.Y.ZrcN.devM``. Dev has no shape constraint (the editable install
    ignores the version string entirely).
    """
    if channel == "dev":
        return True
    try:
        _maj, _min, _pat, is_final, _rc, _not_dev, _dev, local = parse_pep440(version)
    except ValueError:
        return False
    if channel == "production":
        return is_final == 1 and local == ""
    # test: a release candidate, with or without the .devM suffix
    return is_final == 0 and local == ""


def matches_electron_line(
    version: str,
    channel: Channel,
    electron_version: str,
) -> bool:
    """``True`` iff ``version`` is allowed for ``channel`` on this Electron build.

    Production: must equal ``electron_version`` exactly. The Electron app and
    cremind package are released together under the same tag, and letting the
    user install a different ``X.Y.Z`` would silently desync the UI from the
    backend.

    Test: **no line constraint** — any published RC (``X.Y.ZrcN[.devM]`` on any
    line) is installable. The test channel exists to validate arbitrary per-PR
    release candidates, so pinning a tester to the shell's own ``X.Y.Z`` line
    would defeat the purpose; a test shell auto-updates independently, so any
    temporary UI/backend skew is short-lived. (``electron_version`` is ignored
    on test — ``filter_same_line`` therefore keeps every test RC.)

    Dev: no constraint — the editable install satisfies whatever is asked.
    """
    if not matches_channel(version, channel):
        return False
    if channel == "production":
        return version == electron_version
    # test / dev: shape already validated by matches_channel; no line lock.
    return True


def validate(
    version: str,
    channel: Channel,
    *,
    electron_version: str | None = None,
) -> tuple[bool, str]:
    """Return ``(ok, error)``. ``error`` is empty when ``ok`` is True.

    When ``electron_version`` is ``None`` (standalone script invocation),
    only the channel-shape constraint is applied — the script doesn't know
    which Electron build the user intends to pair this install with, so we
    don't enforce the line. The Electron-invoked install *always* passes
    ``electron_version`` and gets the strict check.
    """
    if electron_version is not None:
        if matches_electron_line(version, channel, electron_version):
            return True, ""
        if channel == "production":
            return False, (
                f"Invalid version: {version!r} is not a valid production release "
                f"for this Electron build (v{electron_version}). Production "
                f"requires an exact version match — use the in-app update "
                f"flow to install a different version."
            )
        if channel == "test":
            return False, (
                f"Invalid version: {version!r} does not look like a test "
                f"prerelease (expected X.Y.ZrcN[.devM])."
            )
        return False, f"Invalid version: {version!r} is not valid for the dev channel."

    if matches_channel(version, channel):
        return True, ""
    if channel == "production":
        return False, (
            f"Invalid version: {version!r} does not look like a production "
            f"release (expected X.Y.Z)."
        )
    if channel == "test":
        return False, (
            f"Invalid version: {version!r} does not look like a test "
            f"prerelease (expected X.Y.ZrcN[.devM])."
        )
    return False, f"Invalid version: {version!r}."


def filter_same_line(
    versions: list[str],
    channel: Channel,
    electron_version: str,
) -> list[str]:
    """Return the subset of ``versions`` allowed for this Electron build.

    Order of input is preserved (callers typically sort by PEP 440 first
    via :func:`parse_pep440`). Note: on the ``test`` channel there is no
    line constraint, so every shape-valid RC is kept regardless of
    ``electron_version``; the filter only narrows on ``production``.
    """
    return [v for v in versions if matches_electron_line(v, channel, electron_version)]


# ── Standalone CLI ──────────────────────────────────────────────────────────
# The install scripts fetch this single file and run it with the system
# python3 during bootstrap (before the cremind wheel exists). Two subcommands:
#
#   resolve   — read a PyPI / Test PyPI *simple index* HTML on stdin and print
#               the newest matching cremind wheel URL (or version). Replaces the
#               PowerShell ``[version]`` sort and the bash ``sort -V`` hacks,
#               which couldn't order the ``rcN.devM`` form correctly.
#   validate  — exit 0 (and echo the version) when a version string is valid
#               for a channel, else exit 2 with a message on stderr.

# Matches an absolute cremind wheel href in a simple-index listing, capturing the
# URL (up to ``.whl``, dropping any ``#sha256=…`` fragment) and the version.
_WHEEL_HREF = re.compile(
    r'href="(?P<url>https://[^"#]+/cremind-(?P<ver>[^"/]+)-py3-none-any\.whl)(?:#[^"]*)?"'
)


def _resolve_latest_wheel(
    index_html: str,
    *,
    channel: Channel,
    line: str | None,
) -> tuple[str, str] | None:
    """Return ``(version, url)`` of the newest matching wheel, or ``None``."""
    best: tuple[tuple[int, int, int, int, int, int, int, str], str, str] | None = None
    for m in _WHEEL_HREF.finditer(index_html):
        url, ver = m.group("url"), m.group("ver")
        keep = (
            matches_electron_line(ver, channel, line)
            if line
            else matches_channel(ver, channel)
        )
        if not keep:
            continue
        try:
            key = parse_pep440(ver)
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, ver, url)
    if best is None:
        return None
    return best[1], best[2]


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="channel.py",
        description="Cremind version resolver / validator (single source of truth).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser(
        "resolve",
        help="pick the newest matching cremind wheel from a simple-index HTML on stdin",
    )
    pr.add_argument("--channel", required=True, choices=["production", "test"])
    pr.add_argument("--line", default=None, help="restrict to this X.Y.Z release line")
    pr.add_argument(
        "--print", dest="emit", choices=["url", "version"], default="url",
        help="what to print for the chosen wheel (default: url)",
    )

    pv = sub.add_parser("validate", help="validate a version string for a channel")
    pv.add_argument("--channel", required=True, choices=["production", "test", "dev"])
    pv.add_argument("--version", required=True)
    pv.add_argument("--electron-version", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "validate":
        ok, err = validate(
            args.version, args.channel, electron_version=args.electron_version
        )
        if not ok:
            print(err, file=sys.stderr)
            return 2
        print(args.version)
        return 0

    # resolve
    chosen = _resolve_latest_wheel(
        sys.stdin.read(), channel=args.channel, line=args.line
    )
    if chosen is None:
        scope = f" for line {args.line}" if args.line else ""
        print(
            f"No matching cremind {args.channel} wheel found{scope} in the index.",
            file=sys.stderr,
        )
        return 1
    version, url = chosen
    print(url if args.emit == "url" else version)
    return 0


__all__ = [
    "Channel",
    "filter_same_line",
    "get_channel",
    "is_newer",
    "is_rc_tag",
    "matches_channel",
    "matches_electron_line",
    "parse_pep440",
    "tag_to_pep440",
    "validate",
]


if __name__ == "__main__":
    raise SystemExit(_cli())
