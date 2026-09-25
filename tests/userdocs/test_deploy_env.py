"""deploy_env: mountinfo parsing, the Docker bind-mount check, and the watch-mode table.

The fixtures are real ``/proc/self/mountinfo`` shapes from the deployments
Cremind ships to: a legacy compose container whose documents folder is just a
directory in the overlay layer, a fresh one with an ext4 bind mount, and the
Docker Desktop / WSL mounts on which inotify never fires.
"""

from __future__ import annotations

import pytest

from app.userdocs import deploy_env as de

OVERLAY_ONLY = """\
575 520 0:52 / / rw,relatime master:204 - overlay overlay rw,lowerdir=/var/lib/docker/overlay2/l/ABC,upperdir=/x/diff,workdir=/x/work
576 575 0:55 / /proc rw,nosuid,nodev,noexec,relatime - proc proc rw
577 575 0:56 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755
583 575 8:1 /var/lib/docker/volumes/cremind-data/_data /root/.cremind rw,relatime - ext4 /dev/sda1 rw
584 575 8:1 /var/lib/docker/containers/abc/resolv.conf /etc/resolv.conf rw,relatime - ext4 /dev/sda1 rw
"""

EXT4_BIND = OVERLAY_ONLY + (
    "590 575 8:1 /home/lee/Documents /root/Documents rw,relatime - ext4 /dev/sda1 rw\n"
)
NINE_P = OVERLAY_ONLY + (
    "591 575 0:80 /c/Users/lee/Documents /root/Documents rw,noatime - 9p C:\\134Users rw,dirsync,aname=drvfs\n"
)
VIRTIOFS = OVERLAY_ONLY + "592 575 0:81 /Users/lee/Documents /root/Documents rw - virtiofs mount0 rw\n"
GRPCFUSE = OVERLAY_ONLY + "593 575 0:82 / /root/Documents rw - fuse.grpcfuse grpcfuse rw,user_id=0\n"
SPACES = OVERLAY_ONLY + "594 575 8:1 /x /root/My\\040Documents rw - ext4 /dev/sda1 rw\n"
OPTIONAL_FIELDS = "36 35 98:0 /mnt1 /mnt/parent rw,noatime master:1 shared:2 - ext3 /dev/root rw,errors=continue\n"

_REAL_MOUNT_FOR_PATH = de.mount_for_path


def test_parse_mountinfo_fields_and_escapes():
    mounts = de.parse_mountinfo(SPACES + OPTIONAL_FIELDS + "garbage line\n\n")
    by_mp = {m.mount_point: m for m in mounts}
    assert by_mp["/"].fstype == "overlay"
    assert by_mp["/root/My Documents"].fstype == "ext4"  # \040 decoded
    assert by_mp["/mnt/parent"] == de.MountInfo("/mnt/parent", "ext3", "/dev/root")
    assert len(mounts) == 7


@pytest.mark.parametrize("text,path,mount_point,fstype", [
    (OVERLAY_ONLY, "/root/Documents", "/", "overlay"),
    (OVERLAY_ONLY, "/root/.cremind/admin", "/root/.cremind", "ext4"),
    (EXT4_BIND, "/root/Documents/Reports", "/root/Documents", "ext4"),
    (EXT4_BIND, "/root/DocumentsX", "/", "overlay"),  # prefix is by path segment
    (NINE_P, "/root/Documents", "/root/Documents", "9p"),
    (VIRTIOFS, "/root/Documents/a", "/root/Documents", "virtiofs"),
    (GRPCFUSE, "/root/Documents", "/root/Documents", "fuse.grpcfuse"),
])
def test_mount_for_path_longest_prefix(text, path, mount_point, fstype):
    m = de.mount_for_path(path, text)
    assert m is not None and (m.mount_point, m.fstype) == (mount_point, fstype)


def test_later_mount_on_the_same_point_wins():
    text = EXT4_BIND + "595 590 0:90 / /root/Documents rw - tmpfs tmpfs rw\n"
    assert de.mount_for_path("/root/Documents", text).fstype == "tmpfs"


def test_mount_for_path_is_none_off_linux(monkeypatch):
    monkeypatch.setattr(de.sys, "platform", "win32")
    assert de.mount_for_path("/root/Documents") is None


def _status(monkeypatch, text: str, *, container: bool = True, **env) -> dict:
    # The real longest-prefix lookup, fed a fixture instead of /proc.
    monkeypatch.setattr(de, "mount_for_path", lambda path, t=None: _REAL_MOUNT_FOR_PATH(path, text))
    monkeypatch.setattr(de, "in_container", lambda: container)
    for key in ("CREMIND_DOCUMENTS_BIND", "CREMIND_HOST_DOCUMENTS_HINT", "CREMIND_COMPOSE_HOST_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return de.docker_root_status("/root/Documents")


def test_docker_root_status_legacy_overlay(monkeypatch):
    s = _status(monkeypatch, OVERLAY_ONLY, CREMIND_HOST_DOCUMENTS_HINT="C:/Users/lee/Documents")
    assert s["in_container"] is True
    assert s["root_mounted"] is False and s["persistent"] is False and s["fstype"] == "overlay"
    assert s["bind_expected"] is False
    assert s["snippet"] == '- "C:/Users/lee/Documents:/root/Documents"'


def test_docker_root_status_bound(monkeypatch):
    s = _status(monkeypatch, EXT4_BIND, CREMIND_DOCUMENTS_BIND="1", CREMIND_COMPOSE_HOST_DIR="/opt/cremind")
    assert s["root_mounted"] is True and s["persistent"] is True and s["fstype"] == "ext4"
    assert s["bind_expected"] is True and s["compose_host_dir"] == "/opt/cremind"
    assert s["snippet"] == '- "~/Documents:/root/Documents"'


def test_docker_root_status_tmpfs_is_mounted_but_not_persistent(monkeypatch):
    s = _status(monkeypatch, OVERLAY_ONLY + "596 575 0:91 / /root/Documents rw - tmpfs tmpfs rw\n")
    assert s["root_mounted"] is True and s["persistent"] is False


def test_docker_root_status_unknown_off_linux(monkeypatch):
    monkeypatch.setattr(de, "mount_for_path", lambda path, text=None: None)
    monkeypatch.setattr(de, "in_container", lambda: False)
    s = de.docker_root_status("C:/Users/lee/Documents")
    assert s["root_mounted"] is None and s["persistent"] is None and s["snippet"] is None


def test_in_container_signals(monkeypatch, tmp_path):
    from app.config import runtime_env

    monkeypatch.setattr(de, "_CONTAINERENV_MARKER", tmp_path / "no-containerenv")
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.setattr(runtime_env, "is_container", lambda: False)
    assert de.in_container() is False
    monkeypatch.setattr(runtime_env, "is_container", lambda: True)
    assert de.in_container() is True
    monkeypatch.setattr(runtime_env, "is_container", lambda: False)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    assert de.in_container() is True and de.in_kubernetes() is True
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    marker = tmp_path / ".containerenv"
    marker.write_text("", encoding="utf-8")
    monkeypatch.setattr(de, "_CONTAINERENV_MARKER", marker)
    assert de.in_container() is True


def test_inotify_max_user_watches(monkeypatch, tmp_path):
    f = tmp_path / "max_user_watches"
    f.write_text("8192\n", encoding="utf-8")
    monkeypatch.setattr(de, "_MAX_WATCHES_PATH", f)
    monkeypatch.setattr(de.sys, "platform", "linux")
    assert de.inotify_max_user_watches() == 8192
    monkeypatch.setattr(de.sys, "platform", "darwin")
    assert de.inotify_max_user_watches() is None


def test_sysctl_hint():
    assert de.inotify_sysctl_hint(10) == "sudo sysctl fs.inotify.max_user_watches=524288"
    assert de.inotify_sysctl_hint(400_000).endswith("=800000")


# ── choose_watch_mode ──────────────────────────────────────────────────────


@pytest.fixture
def linux(monkeypatch):
    """A Linux host with a given mountinfo and watch budget."""
    state = {"text": OVERLAY_ONLY, "watches": 65536, "wsl": False}
    monkeypatch.setattr(de, "mount_for_path", lambda path, text=None: _REAL_MOUNT_FOR_PATH(path, state["text"]))
    monkeypatch.setattr(de, "inotify_max_user_watches", lambda: state["watches"])
    monkeypatch.setattr(de, "_is_wsl", lambda: state["wsl"])
    return state


@pytest.mark.parametrize("fstype", [
    "9p", "virtiofs", "grpcfuse", "fuse.grpcfuse", "fakeowner", "nfs", "nfs4", "cifs", "smb3", "smbfs",
    "fuse.sshfs", "drvfs", "vboxsf",
])
def test_remote_filesystems_poll(linux, fstype):
    linux["text"] = OVERLAY_ONLY + f"600 575 0:99 / /data rw - {fstype} src rw\n"
    assert de.choose_watch_mode("/data/docs") == ("poll", "remote_fs")


@pytest.mark.parametrize("fstype", ["ext4", "xfs", "btrfs", "overlay", "zfs"])
def test_local_filesystems_watch_natively(linux, fstype):
    linux["text"] = OVERLAY_ONLY + f"600 575 0:99 / /data rw - {fstype} src rw\n"
    assert de.choose_watch_mode("/data/docs") == ("native", None)


def test_wsl_drive_polls_only_under_wsl(linux):
    linux["text"] = OVERLAY_ONLY  # no hint from mountinfo
    assert de.choose_watch_mode("/mnt/c/Users/lee/Documents") == ("native", None)
    linux["wsl"] = True
    assert de.choose_watch_mode("/mnt/c/Users/lee/Documents") == ("poll", "wsl_drive")
    assert de.choose_watch_mode("/mnt/data/docs") == ("native", None)  # not a drive letter


def test_too_many_directories_for_inotify(linux):
    linux["watches"] = 8192
    assert de.choose_watch_mode("/root/Documents", dirs_needed=4096) == ("native", None)
    assert de.choose_watch_mode("/root/Documents", dirs_needed=4097) == ("poll", "inotify_limit")
    linux["watches"] = None  # unknown budget: no limit applied
    assert de.choose_watch_mode("/root/Documents", dirs_needed=10**7) == ("native", None)


def test_requested_mode_is_honoured(linux):
    linux["text"] = NINE_P
    assert de.choose_watch_mode("/root/Documents", requested="native") == ("native", "requested")
    linux["text"] = EXT4_BIND
    assert de.choose_watch_mode("/root/Documents", requested="poll") == ("poll", "requested")
