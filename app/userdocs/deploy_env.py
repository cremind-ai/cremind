"""What the machine underneath a documents root can and cannot do.

Two questions the sync engine cannot answer from the root path alone:

1. **Is this folder really the user's?** In a Docker install the documents
   root (``/root/Documents``) only holds the user's files when the compose file
   bind-mounts a host folder there. Installs made before the bind mount existed
   have nothing mounted, and :func:`app.config.settings.get_user_working_directory`
   happily ``makedirs`` the path — so the root *exists*, is empty, and lives on
   the container's overlay filesystem, where anything written vanishes with the
   container. :func:`docker_root_status` spots that from ``/proc/self/mountinfo``
   so the UI can show the one-line compose fix instead of indexing nothing.

2. **Will change notifications arrive?** inotify only reports changes made
   *through this kernel*. On a Docker Desktop bind mount (9p, virtiofs,
   grpcfuse, fakeowner), a network share (nfs, cifs/smb, sshfs) or a WSL
   ``/mnt/c`` drive, edits made on the other side never produce an event, and
   a recursive watch over a large tree can exhaust ``max_user_watches``.
   :func:`choose_watch_mode` picks polling (our own scan-diff) for those.

Everything here is Linux-specific underneath and degrades to "unknown" (``None``)
elsewhere: macOS and Windows have no mountinfo and native change notifications
work on their local disks. Nothing here raises for a missing ``/proc`` file.

Import discipline: stdlib only at module level; the install-mode resolver is
imported inside :func:`in_container` so importing this module stays cheap.
"""

from __future__ import annotations

import os
import posixpath
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Module-level so tests can point them at fixtures instead of patching
# ``Path.exists``/``open`` globally (CI itself may run inside a container).
_MOUNTINFO_PATH = Path("/proc/self/mountinfo")
_MAX_WATCHES_PATH = Path("/proc/sys/fs/inotify/max_user_watches")
_OSRELEASE_PATH = Path("/proc/sys/kernel/osrelease")
# Podman writes this instead of ``/.dockerenv``; the shared install-mode
# resolver does not look for it because Cremind ships no Podman install, but a
# user running our image under Podman still has an overlay root.
_CONTAINERENV_MARKER = Path("/run/.containerenv")

# Filesystems whose changes are made outside this kernel (a VM host, another
# machine), so inotify never hears about them. Exact names, then prefixes for
# the families that come in versions (nfs4, smb3, smbfs).
_POLL_FSTYPES = frozenset({
    "9p",               # WSL2 /mnt/<x>, Docker Desktop (Windows) bind mounts
    "virtiofs",         # Docker Desktop (macOS, VirtioFS) bind mounts
    "grpcfuse",         # Docker Desktop (older macOS/Windows) bind mounts
    "fuse.grpcfuse",
    "fakeowner",        # Docker Desktop (macOS) bind mounts as seen in-container
    "osxfs", "fuse.osxfs",  # the pre-2020 Docker for Mac file sharing
    "cifs",
    "fuse.sshfs",
    "drvfs",            # WSL1 /mnt/<x>
    # Hypervisor shared folders: same "the host edited it" problem.
    "vboxsf", "prl_fs", "vmhgfs", "fuse.vmhgfs-fuse",
})
_POLL_FSTYPE_PREFIXES = ("nfs", "smb")

# Filesystems whose contents do not outlive the container or the machine.
_EPHEMERAL_FSTYPES = frozenset({"overlay", "tmpfs", "ramfs", "aufs"})

# ``/mnt/c``, ``/mnt/D/...``: how WSL exposes Windows drives.
_WSL_DRIVE_RE = re.compile(r"^/mnt/[A-Za-z](?:/|$)")

# mountinfo escapes space, tab, newline and backslash as 3-digit octal.
_OCTAL_ESCAPE_RE = re.compile(r"\\([0-7]{3})")


@dataclass(frozen=True)
class MountInfo:
    mount_point: str
    fstype: str
    source: str


def _unescape(field: str) -> str:
    return _OCTAL_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 8)), field)


def parse_mountinfo(text: str) -> list[MountInfo]:
    """Parse ``/proc/self/mountinfo`` text, in file order.

    Line shape (proc(5))::

        36 35 98:0 /mnt1 /mnt/parent rw,noatime master:1 - ext3 /dev/root rw

    The optional fields before the ``-`` separator vary in number, so the
    filesystem type and source are read relative to the separator, never by a
    fixed column. Malformed lines are skipped rather than failing the parse.
    """
    mounts: list[MountInfo] = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 7:
            continue
        try:
            sep = parts.index("-", 6)
        except ValueError:
            continue
        if sep + 1 >= len(parts):
            continue
        fstype = parts[sep + 1]
        source = parts[sep + 2] if sep + 2 < len(parts) else ""
        mounts.append(MountInfo(_unescape(parts[4]), fstype, _unescape(source)))
    return mounts


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def mount_for_path(path: str, text: str | None = None) -> MountInfo | None:
    """The mount that holds ``path``: the longest mount point that prefixes it.

    With ``text`` given (tests, or a caller that already read the file) the
    path is only normalised; with ``text=None`` this reads the live
    ``/proc/self/mountinfo`` and resolves symlinks first, because the mount
    that matters is the one the *target* lives on. Returns ``None`` off Linux
    or when the file cannot be read. When two mounts share a mount point the
    later line wins: it was mounted on top and is the one the path sees.
    """
    if text is None:
        if not sys.platform.startswith("linux"):
            return None
        text = _read_text(_MOUNTINFO_PATH)
        if text is None:
            return None
        path = os.path.realpath(path)
    target = posixpath.normpath(path.replace("\\", "/") or "/")
    best: MountInfo | None = None
    best_len = -1
    for mount in parse_mountinfo(text):
        mp = posixpath.normpath(mount.mount_point) if mount.mount_point else ""
        if not mp:
            continue
        if mp == "/":
            covered = target.startswith("/")
        else:
            covered = target == mp or target.startswith(mp + "/")
        if covered and len(mp) >= best_len:
            best, best_len = mount, len(mp)
    return best


def in_kubernetes() -> bool:
    """Running in a pod: the kubelet injects ``KUBERNETES_SERVICE_HOST``."""
    return bool((os.environ.get("KUBERNETES_SERVICE_HOST") or "").strip())


def in_container() -> bool:
    """Docker, Kubernetes or Podman, as the rest of Cremind decides it.

    Delegates to :func:`app.config.runtime_env.is_container` (``/.dockerenv``,
    ``INSTALL_MODE``, the pod markers) so this answer can never disagree with
    the Developer page's; the Podman marker is the only addition.
    """
    if in_kubernetes() or _CONTAINERENV_MARKER.exists():
        return True
    try:
        from app.config import runtime_env

        return bool(runtime_env.is_container())
    except Exception:  # noqa: BLE001 — a probe must never break a scan
        return False


def _compose_snippet(root: str, host_hint: str | None) -> str:
    host = (host_hint or "").strip() or "~/Documents"
    return f'- "{host}:{root}"'


def docker_root_status(root: str) -> dict:
    """Whether a container's documents root is backed by a real host folder.

    Keys:

    - ``in_container`` — see :func:`in_container`.
    - ``root_mounted`` — ``False`` when the longest-prefix mount of ``root``
      is ``/`` itself, i.e. the folder is just a directory in the container's
      own overlay layer (the legacy-compose case). ``None`` when unknown (not
      Linux, no mountinfo).
    - ``persistent`` — the root sits on a mount whose contents outlive the
      container (not the overlay root, not tmpfs). ``None`` when unknown.
    - ``fstype`` — the filesystem type of that mount, or ``None``.
    - ``compose_host_dir`` / ``host_documents_hint`` — what the installer
      recorded about the host side (``CREMIND_COMPOSE_HOST_DIR``,
      ``CREMIND_HOST_DOCUMENTS_HINT``), for the UI's instructions.
    - ``bind_expected`` — the compose file says a bind mount belongs here
      (``CREMIND_DOCUMENTS_BIND=1``). With this set and ``root_mounted``
      False, the mount went missing and the root guard holds the source.
    - ``snippet`` — the one ``volumes:`` line that fixes a missing mount, or
      ``None`` outside a container.
    """
    container = in_container()
    mount = mount_for_path(root)
    if mount is None:
        mounted: bool | None = None
        persistent: bool | None = None
    else:
        mounted = posixpath.normpath(mount.mount_point) != "/"
        persistent = mounted and mount.fstype not in _EPHEMERAL_FSTYPES
    hint = (os.environ.get("CREMIND_HOST_DOCUMENTS_HINT") or "").strip() or None
    return {
        "in_container": container,
        "root_mounted": mounted,
        "persistent": persistent,
        "fstype": mount.fstype if mount else None,
        "compose_host_dir": (os.environ.get("CREMIND_COMPOSE_HOST_DIR") or "").strip() or None,
        "host_documents_hint": hint,
        "bind_expected": (os.environ.get("CREMIND_DOCUMENTS_BIND") or "").strip() == "1",
        "snippet": _compose_snippet(root, hint) if container else None,
    }


def inotify_max_user_watches() -> int | None:
    """The kernel's per-user inotify watch budget, or ``None`` off Linux."""
    if not sys.platform.startswith("linux"):
        return None
    raw = _read_text(_MAX_WATCHES_PATH)
    try:
        return int((raw or "").strip())
    except ValueError:
        return None


def inotify_sysctl_hint(dirs_needed: int = 0) -> str:
    """The command that raises the watch budget, for the "too many folders" banner."""
    target = max(524288, 2 * int(dirs_needed or 0))
    return f"sudo sysctl fs.inotify.max_user_watches={target}"


def _is_wsl() -> bool:
    """Running under WSL (1 or 2). A function so tests can say yes on any host."""
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    if not sys.platform.startswith("linux"):
        return False
    return "microsoft" in (_read_text(_OSRELEASE_PATH) or "").lower()


def _polls(fstype: str) -> bool:
    return fstype in _POLL_FSTYPES or fstype.startswith(_POLL_FSTYPE_PREFIXES)


def choose_watch_mode(root: str, requested: str = "auto", dirs_needed: int = 0) -> tuple[str, str | None]:
    """``(mode, reason)``: ``native`` (a watchdog Observer) or ``poll``.

    ``requested`` is the profile's ``observer.mode``; ``native`` and ``poll``
    are honoured as given (reason ``requested``). For ``auto`` the reasons are:

    - ``remote_fs`` — the root's filesystem gets its changes from outside this
      kernel (see ``_POLL_FSTYPES``), so no event would ever arrive;
    - ``wsl_drive`` — a WSL ``/mnt/<letter>`` path (Windows drive);
    - ``inotify_limit`` — the tree needs more than half the per-user watch
      budget (the other half is left for everything else the user runs);
      :func:`inotify_sysctl_hint` gives the fix.

    ``(native, None)`` otherwise. The caller still falls back to polling when
    the watcher fails to start or is later seen to miss changes.
    """
    if requested in ("native", "poll"):
        return requested, "requested"
    mount = mount_for_path(root)
    if mount is not None and _polls(mount.fstype):
        return "poll", "remote_fs"
    if _WSL_DRIVE_RE.match(root.replace("\\", "/")) and _is_wsl():
        return "poll", "wsl_drive"
    limit = inotify_max_user_watches()
    if limit and dirs_needed > limit * 0.5:
        return "poll", "inotify_limit"
    return "native", None


__all__ = [
    "MountInfo",
    "choose_watch_mode",
    "docker_root_status",
    "in_container",
    "in_kubernetes",
    "inotify_max_user_watches",
    "inotify_sysctl_hint",
    "mount_for_path",
    "parse_mountinfo",
]
