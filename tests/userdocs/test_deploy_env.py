"""deploy_env: mountinfo parsing, the Docker bind-mount check, and the watch-mode table.

The fixtures are real ``/proc/self/mountinfo`` shapes from the deployments
Cremind ships to: a legacy compose container whose documents folder is just a
directory in the overlay layer, a fresh one with an ext4 bind mount, and the
Docker Desktop / WSL mounts on which inotify never fires.
"""

from __future__ import annotations

from types import SimpleNamespace

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


def _status(monkeypatch, text: str, *, container: bool = True, pod: bool = False, **env) -> dict:
    # The real longest-prefix lookup, fed a fixture instead of /proc.
    monkeypatch.setattr(de, "mount_for_path", lambda path, t=None: _REAL_MOUNT_FOR_PATH(path, text))
    monkeypatch.setattr(de, "in_container", lambda: container)
    # Not the env var: CI itself may run in a pod.
    monkeypatch.setattr(de, "in_kubernetes", lambda: pod)
    for key in ("CREMIND_DOCUMENTS_BIND", "CREMIND_HOST_DOCUMENTS_HINT", "CREMIND_COMPOSE_HOST_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return de.docker_root_status("/root/Documents")


def test_docker_root_status_legacy_overlay(monkeypatch):
    s = _status(monkeypatch, OVERLAY_ONLY, CREMIND_HOST_DOCUMENTS_HINT="C:/Users/lee/Documents")
    assert s["in_container"] is True and s["kubernetes"] is False
    assert s["root_mounted"] is False and s["persistent"] is False and s["fstype"] == "overlay"
    assert s["bind_expected"] is False
    assert s["snippet"] == '- "C:/Users/lee/Documents:/root/Documents"'


def test_docker_root_status_in_a_pod_without_the_work_volume(monkeypatch):
    s = _status(monkeypatch, OVERLAY_ONLY, pod=True)
    assert s["in_container"] is True and s["kubernetes"] is True
    assert s["root_mounted"] is False and s["bind_expected"] is False


def test_docker_root_status_kubernetes_implies_a_container(monkeypatch):
    # The two flags never disagree: no container, no pod.
    s = _status(monkeypatch, OVERLAY_ONLY, container=False, pod=True)
    assert s["in_container"] is False and s["kubernetes"] is False


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
    assert s["kubernetes"] is False


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


# ── the snapshot's docker block (runtime.docker_view) ──────────────────────
#
# Old installs keep the documents folder in the container's own layer; the UI
# and `cremind userdocs status` warn from this block. It is read by configure
# (on the maintenance thread) and only served by the snapshot, which is built
# on every progress frame and must never block or raise.


LEGACY_STATUS = {
    "in_container": True, "kubernetes": False, "root_mounted": False, "persistent": False,
    "fstype": "overlay", "compose_host_dir": "/opt/cremind", "host_documents_hint": None,
    "bind_expected": False, "snippet": '- "~/Documents:/root/Documents"',
}


@pytest.fixture
def probe(monkeypatch):
    """docker_root_status as configure sees it, counting calls."""
    state = {"calls": [], "status": dict(LEGACY_STATUS), "error": None}

    def fake(root):
        state["calls"].append(root)
        if state["error"] is not None:
            raise state["error"]
        return dict(state["status"])

    monkeypatch.setattr(de, "docker_root_status", fake)
    return state


def _runtime(root: str | None = "/root/Documents", *, local_on: bool = True):
    from app.userdocs.runtime import ProfileRuntime

    rt = ProfileRuntime(SimpleNamespace(), "alice", "uid-alice")
    rt.settings = {"enabled": local_on}
    rt.root = root
    return rt


def test_snapshot_serves_the_docker_block_read_by_configure(probe):
    rt = _runtime()
    assert rt.runtime_snapshot()["docker"] is None  # not checked yet: no guess
    rt._refresh_docker_status("/root/Documents")
    docker = rt.runtime_snapshot()["docker"]
    assert docker == {
        "in_container": True, "kubernetes": False, "root_mounted": False, "persistent": False,
        "fstype": "overlay", "bind_expected": False, "snippet": '- "~/Documents:/root/Documents"',
    }
    for _ in range(5):
        rt.runtime_snapshot()
    assert probe["calls"] == ["/root/Documents"]  # cached: snapshots never probe


def test_docker_block_is_per_root(probe):
    rt = _runtime()
    rt._refresh_docker_status("/root/Documents")
    rt.root = "/data/docs"  # a new root that configure has not checked yet
    assert rt.docker_view() is None
    probe["status"] = {**LEGACY_STATUS, "root_mounted": True, "persistent": True, "fstype": "ext4"}
    rt._refresh_docker_status("/data/docs")
    assert rt.docker_view()["root_mounted"] is True


def test_docker_block_only_while_the_local_folder_is_on(probe):
    rt = _runtime(local_on=False)
    rt._refresh_docker_status("/root/Documents")
    assert rt.runtime_snapshot()["docker"] is None


def test_a_failing_probe_is_unknown_not_an_error(probe):
    probe["error"] = OSError("mountinfo vanished")
    rt = _runtime()
    rt._refresh_docker_status("/root/Documents")  # does not raise
    assert rt.runtime_snapshot()["docker"] is None


def test_configure_rechecks_the_container_mount(probe, monkeypatch, tmp_path):
    """Every configure that settles a root re-reads the mount, so the block
    follows a changed folder (and a restarted, fixed container)."""
    import app.storage.userdocs_storage as uds_storage
    from app.userdocs import settings as uds

    root = str(tmp_path)
    row = {"enabled": True, "root_mode": uds.ROOT_CUSTOM, "root_path": root, "first_sync_confirmed_at": 1.0}

    class _Storage:
        def get_source(self, profile, kind):
            return row if kind == uds.SOURCE_LOCAL else None

    class _DB:
        closed = False

        def get_source_state(self, source):
            return {}

    monkeypatch.setattr(uds_storage, "get_userdocs_storage", lambda: _Storage())
    monkeypatch.setattr(uds, "read_admin_policy", lambda: SimpleNamespace(allowed=True))
    monkeypatch.setattr(uds, "feature_effective", lambda policy: (True, None))
    monkeypatch.setattr(uds, "validate_root", lambda path, is_admin=False: SimpleNamespace(
        ok=True, path=root, locked_excludes=[], code=None, message=None))
    rt = _runtime(root=None)
    monkeypatch.setattr(rt, "ensure_db", lambda: _DB())
    monkeypatch.setattr(rt.drive, "configure", lambda r: None)
    monkeypatch.setattr(rt, "_start_watching", lambda opts: None)
    monkeypatch.setattr(rt, "request_scan", lambda reason: None)

    rt.configure()
    assert probe["calls"] == [root]
    assert rt.runtime_snapshot()["docker"]["root_mounted"] is False
    rt.configure()
    assert probe["calls"] == [root, root]
