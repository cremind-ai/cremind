"""The compose fix for a lost documents mount, now that each profile has its
own working directory.

A profile's documents root is its working directory: in the Docker bundle
``/root/Documents/cremind-workspaces/<profile>`` by default, ``/root/Documents``
for the admin of an install that predates per-profile folders. Both live under the ONE
bind mount the bundle defines, so the line the UI suggests must restore that
mount — never bind the host folder over a single workspace, which would hide
every other profile's folder from the host.
"""

from __future__ import annotations

import app.documents.deploy_env as de

_REAL_MOUNT_FOR_PATH = de.mount_for_path

OVERLAY_ONLY = "575 1 0:52 / / rw,relatime - overlay overlay rw\n"
EXT4_BIND = OVERLAY_ONLY + "590 575 8:1 /home/lee/Documents /root/Documents rw - ext4 /dev/sda1 rw\n"


def _status(monkeypatch, root: str, text: str, **env) -> dict:
    monkeypatch.setattr(de, "mount_for_path", lambda path, t=None: _REAL_MOUNT_FOR_PATH(path, text))
    monkeypatch.setattr(de, "in_container", lambda: True)
    monkeypatch.setattr(de, "in_kubernetes", lambda: False)
    for key in ("CREMIND_DOCUMENTS_BIND", "CREMIND_HOST_DOCUMENTS_HINT", "CREMIND_COMPOSE_HOST_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return de.docker_root_status(root)


def test_a_workspace_root_is_fixed_by_the_documents_mount(monkeypatch):
    s = _status(monkeypatch, "/root/Documents/cremind-workspaces/bob", OVERLAY_ONLY,
                CREMIND_HOST_DOCUMENTS_HINT="C:/Users/lee/Documents")
    assert s["root_mounted"] is False
    assert s["snippet"] == '- "C:/Users/lee/Documents:/root/Documents"'


def test_a_workspace_inside_the_bound_folder_counts_as_mounted(monkeypatch):
    for profile in ("admin", "bob"):
        s = _status(monkeypatch, f"/root/Documents/cremind-workspaces/{profile}", EXT4_BIND,
                    CREMIND_DOCUMENTS_BIND="1")
        assert s["root_mounted"] is True and s["persistent"] is True
        assert s["snippet"] == '- "~/Documents:/root/Documents"'


def test_the_legacy_admin_root_is_unchanged(monkeypatch):
    s = _status(monkeypatch, "/root/Documents", OVERLAY_ONLY)
    assert s["snippet"] == '- "~/Documents:/root/Documents"'


def test_a_folder_outside_the_documents_mount_keeps_its_own_target(monkeypatch):
    """A folder the admin chose elsewhere (``/data/shared``) is not under the
    bundle's mount; the suggestion binds it where it is, as before."""
    s = _status(monkeypatch, "/data/shared", OVERLAY_ONLY)
    assert s["snippet"] == '- "~/Documents:/data/shared"'
    # A sibling whose name merely starts the same is not inside the mount.
    s = _status(monkeypatch, "/root/Documents-old", OVERLAY_ONLY)
    assert s["snippet"] == '- "~/Documents:/root/Documents-old"'
