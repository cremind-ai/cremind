"""Build the Cremind Tag font asset bundle a release ships, and pin it.

Tag screens are composed on the gateway computer with the same fonts the
bridges draw from, so every gateway computer installs this bundle
(``cremind tags hosts prepare`` on the server, ``cremind tags host prepare``
on a desktop gateway computer). People never build it: a release publishes it
and ``app/tags/runtime/fonts/bundle.json`` pins it by pack id and SHA-256,
which ``app/tags/hosting/fonts.py`` checks before installing anything.

    uv run python scripts/tags/build_font_bundle.py                # dist/fonts/cremind-tag-fonts-<pack>.tar.gz
    uv run python scripts/tags/build_font_bundle.py --write-lock   # ... and pin it in bundle.json (Linux only)
    uv run python scripts/tags/build_font_bundle.py --check        # rebuild: it must match the pin exactly

The pack must be built first (``cremind tags tools fonts fetch`` and
``cremind tags tools fonts build --profile full``). The archive is
reproducible — entries sorted, times and owners zeroed, modes normalised,
text with LF line ends, gzip without a timestamp — so the same pack always
gives the same SHA-256. That is what lets CI prove a pin and a gateway
computer verify a download.

The canonical pack is the **Linux** build (what the release workflows run):
the FreeType/HarfBuzz builds inside the Windows and macOS wheels round a
handful of glyph pixels differently, so a pack built there has another pack
id. ``--write-lock`` therefore refuses to run anywhere but Linux; pin from a
container mirroring CI (from Git Bash on Windows, prefix ``MSYS_NO_PATHCONV=1``)::

    docker run --rm -v "$PWD:/src" -w /src python:3.13-slim bash -c \\
      "pip install uv && UV_PROJECT_ENVIRONMENT=/venv uv sync --all-groups && \\
       uv run --no-sync cremind tags tools fonts fetch && \\
       uv run --no-sync cremind tags tools fonts build --profile full && \\
       uv run --no-sync python scripts/tags/build_font_bundle.py --write-lock"

A checkout whose own pack has another id installs the pinned bundle instead
of its own (app/tags/hosting/fonts.py), so every gateway computer and bridge
shares one pack.

The bundle is published once per pack, as the asset of the GitHub release
``fonts-<pack_id>`` (the release workflows create it when it is missing), so
a new Cremind release reuses the same URL until the fonts change.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LOCK = REPO / "app" / "tags" / "runtime" / "fonts" / "bundle.json"
SCHEMA = "cremind/tag-font-bundle@1"
URL_BASE = "https://github.com/cremind-ai/cremind/releases/download"


def archive_name(pack_id: str) -> str:
    return f"cremind-tag-fonts-{pack_id}.tar.gz"


def release_tag(pack_id: str) -> str:
    return f"fonts-{pack_id}"


TEXT_SUFFIXES = (".json", ".txt", ".md", ".yaml")


def _is_text(path: Path) -> bool:
    return path.suffix.lower() in TEXT_SUFFIXES or path.name.upper().startswith(("NOTICE", "LICENSE", "README"))


def _tar_bytes(root: Path) -> bytes:
    """A reproducible tar.gz of everything under ``root`` (paths relative to it). Text files (the sidecar, the
    notices and licences) go in with LF line ends, so a checkout on Windows builds the same bytes as CI."""
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
                name = path.relative_to(root).as_posix()
                info = tarfile.TarInfo(name)
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if path.is_dir():
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                else:
                    data = path.read_bytes()
                    if _is_text(path):
                        data = data.replace(b"\r\n", b"\n")
                    info.size = len(data)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def build(profile: str, out_dir: Path) -> dict:
    """Assemble and verify the pack's assets, then write the archive; returns its pin."""
    sys.path.insert(0, str(REPO))
    from app.tags.runtime.resources import make_font_assets

    pack_dir, cache = REPO / "fonts" / "out" / profile, REPO / "fonts" / "cache"
    if not (pack_dir / "fontpack.ctfp").is_file() or not cache.is_dir():
        raise SystemExit(f"{pack_dir} or {cache} is missing: run `cremind tags tools fonts fetch` and "
                         f"`cremind tags tools fonts build --profile {profile}` first")
    with tempfile.TemporaryDirectory(prefix="ctag-font-bundle-") as tmp:
        assets = make_font_assets(pack_dir, cache, Path(tmp) / "assets")
        data = _tar_bytes(Path(tmp) / "assets")
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / archive_name(assets.pack_id)
    archive.write_bytes(data)
    return {"schema": SCHEMA, "pack_id": assets.pack_id, "profile": profile,
            "url": f"{URL_BASE}/{release_tag(assets.pack_id)}/{archive_name(assets.pack_id)}",
            "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", default="full", help="the font pack profile to ship (default: full)")
    parser.add_argument("--out", type=Path, default=REPO / "dist" / "fonts", help="where the archive goes")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--write-lock", action="store_true", help=f"pin the archive in {LOCK.relative_to(REPO)}")
    group.add_argument("--check", action="store_true", help="the rebuilt archive must match the pin exactly")
    args = parser.parse_args(argv)
    if args.write_lock and not sys.platform.startswith("linux"):
        print(f"--write-lock runs on Linux only: the release pins the Linux build of the pack, and a {sys.platform} "
              "build has another pack id. Pin from a Linux container (the docker command in this script's "
              "docstring); --check and a plain build work here.", file=sys.stderr)
        return 1
    pin = build(args.profile, args.out)
    print(json.dumps(pin, indent=2))
    if args.write_lock:
        LOCK.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
        print(f"pinned in {LOCK.relative_to(REPO)}")
    elif args.check:
        try:
            pinned = json.loads(LOCK.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"no usable pin at {LOCK}: {exc}", file=sys.stderr)
            return 1
        drift = [key for key in ("pack_id", "sha256", "size", "url") if pinned.get(key) != pin[key]]
        if drift:
            print(f"the rebuilt bundle does not match the pin ({', '.join(drift)}): fonts changed? "
                  "re-pin with --write-lock", file=sys.stderr)
            return 1
        print("the rebuilt bundle matches the pin")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
