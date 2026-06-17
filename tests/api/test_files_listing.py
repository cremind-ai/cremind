"""Tests for directory-listing entry filtering in ``app.api.files``.

The right-hand file tree lists the working directory via
``GET /api/files/list``. On Windows the profile's ``Documents`` folder holds
legacy ``My Music``/``My Pictures``/``My Videos`` *junctions* — they carry the
SYSTEM + HIDDEN attributes and reparse to directories *outside* the working
tree, so opening one 403s. They must never appear in a listing, regardless of
the show-hidden toggle. These tests pin ``_entry_hidden`` (the pure predicate
behind that filter) without touching the filesystem.
"""

from __future__ import annotations

import stat

from app.api.files import _entry_hidden

_SYSTEM = stat.FILE_ATTRIBUTE_SYSTEM
_HIDDEN = stat.FILE_ATTRIBUTE_HIDDEN
_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT


def test_system_junction_always_hidden():
    # ``My Pictures`` & co.: SYSTEM | HIDDEN | REPARSE_POINT.
    attrs = _SYSTEM | _HIDDEN | _REPARSE
    assert _entry_hidden("My Pictures", attrs, show_hidden=False) is True
    # Still hidden even with "show hidden files" on — they only 403 on open.
    assert _entry_hidden("My Pictures", attrs, show_hidden=True) is True


def test_system_only_entry_always_hidden():
    assert _entry_hidden("desktop.ini", _SYSTEM, show_hidden=False) is True
    assert _entry_hidden("desktop.ini", _SYSTEM, show_hidden=True) is True


def test_windows_hidden_attribute_respects_toggle():
    assert _entry_hidden("AppData", _HIDDEN, show_hidden=False) is True
    assert _entry_hidden("AppData", _HIDDEN, show_hidden=True) is False


def test_dotfile_respects_toggle():
    assert _entry_hidden(".git", 0, show_hidden=False) is True
    assert _entry_hidden(".git", 0, show_hidden=True) is False


def test_plain_entry_always_visible():
    assert _entry_hidden("projects", 0, show_hidden=False) is False
    assert _entry_hidden("notes.txt", 0, show_hidden=True) is False


def test_posix_no_attributes_falls_back_to_dotfiles():
    # POSIX: ``st_file_attributes`` is absent → attrs == 0; only the dotfile
    # convention applies, never the Windows attribute bits.
    assert _entry_hidden("Documents", 0, show_hidden=False) is False
    assert _entry_hidden(".bashrc", 0, show_hidden=False) is True
