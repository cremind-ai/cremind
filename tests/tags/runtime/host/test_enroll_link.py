"""The ``cremind://tags/setup`` link and a desktop gateway computer's enrollment files.

The link reaches the Cremind app from a browser, so it is parsed strictly:
the exact action, one of each parameter, a bare http(s) origin, a canonical
session id, a URL-safe token, a certificate pin only with https. The
enrollment's secret lives only in an owner-only file, never in the public one.
"""

from __future__ import annotations

import json
import sys

import pytest

from app.tags.runtime.connect.links import LinkError
from app.tags.runtime.host.enroll import (
    EnrollError, Enrollment, forget_enrollment, load_enrollment, parse_host_link, save_enrollment,
)

SESSION = "6fb5bffe-fdb4-4192-860d-341115d9d060"
TOKEN = "A" * 43
GOOD = f"cremind://tags/setup?v=1&server=https%3A%2F%2Fcremind.example.org%3A1180&session={SESSION}&token={TOKEN}"


def test_a_good_link() -> None:
    link = parse_host_link(GOOD)
    assert (link.server, link.session, link.token, link.pin) == ("https://cremind.example.org:1180", SESSION, TOKEN,
                                                                 None)
    assert TOKEN not in repr(link), "the token is a credential"
    pinned = parse_host_link(GOOD + "&pin=" + "AB" * 32)
    assert pinned.pin == "ab" * 32
    assert parse_host_link(GOOD.replace("https%3A%2F%2Fcremind.example.org%3A1180", "http%3A%2F%2F192.168.1.9%3A1515")
                           ).server == "http://192.168.1.9:1515"


@pytest.mark.parametrize("url, code", [
    ("", "empty"),
    (GOOD.replace("cremind://", "cremind-connect://"), "wrong_scheme"),
    (GOOD.replace("cremind://", "https://"), "wrong_scheme"),
    (GOOD.replace("tags/setup", "tags/remove"), "unknown_action"),
    (GOOD.replace("tags/setup", "files/setup"), "unknown_action"),
    (GOOD + "#frag", "malformed"),
    (GOOD + "&token=" + TOKEN, "malformed"),
    (GOOD.replace("v=1", "v=2"), "unsupported_version"),
    (GOOD.replace("https%3A%2F%2Fcremind.example.org%3A1180", "https%3A%2F%2Fcremind.example.org%2Fpath"), "bad_server"),
    (GOOD.replace("https%3A%2F%2Fcremind.example.org%3A1180", "https%3A%2F%2Fuser%40cremind.example.org"), "bad_server"),
    (GOOD.replace("https%3A%2F%2Fcremind.example.org%3A1180", "ftp%3A%2F%2Fcremind.example.org"), "bad_server"),
    (GOOD.replace(SESSION, "not-a-uuid"), "bad_session"),
    (GOOD.replace(SESSION, SESSION.replace("-", "")), "bad_session"),
    (GOOD.replace(SESSION, "{" + SESSION + "}"), "bad_session"),
    (GOOD.replace(SESSION, SESSION.upper()), None),  # a canonical id in capitals is the same session
    (GOOD.replace(TOKEN, "short"), "bad_token"),
    (GOOD.replace(TOKEN, "bad%20token" + "A" * 20), "bad_token"),
    (GOOD + "&pin=zz", "bad_pin"),
    (GOOD.replace("https%3A", "http%3A") + "&pin=" + "ab" * 32, "bad_pin"),
    (GOOD + "\n", None),  # trailing whitespace is trimmed, not an error
    ("cremind://tags/setup?v=1&server=https%3A%2F%2Fx.org&session=" + SESSION + "&token=" + TOKEN + "\x00", "malformed"),
])
def test_bad_links_are_refused(url: str, code: str | None) -> None:
    if code is None:
        assert parse_host_link(url).session == SESSION
        return
    with pytest.raises(LinkError) as info:
        parse_host_link(url)
    assert info.value.code == code


def _enrollment(**extra) -> Enrollment:
    return Enrollment(server="https://cremind.example.org", host_id="h-1", credential_id="tagh_abc", secret="s" * 43,
                      profile="anna", profile_id="pid-anna", host_name="LAPTOP-9", server_installation_id="inst",
                      enrolled_at="2026-09-28T10:00:00Z", **extra)


def test_the_enrollment_keeps_its_secret_apart(tmp_path) -> None:
    from app.tags.hosting.paths import RuntimePaths

    paths = RuntimePaths(tmp_path / ".tag-runtime")
    assert load_enrollment(paths) is None
    save_enrollment(paths, _enrollment(ca_pem="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n"))
    public = paths.enrollment_file.read_text(encoding="utf-8")
    assert "s" * 43 not in public and json.loads(public)["ca_pinned"] is True
    loaded = load_enrollment(paths)
    assert loaded == _enrollment(ca_pem="-----BEGIN CERTIFICATE-----\nx\n-----END CERTIFICATE-----\n")
    assert loaded.authorization == "CremindHost tagh_abc." + "s" * 43
    assert "s" * 43 not in repr(loaded)
    if sys.platform != "win32":
        assert (paths.enrollment_key.stat().st_mode & 0o077) == 0, "the key is owner-only"

    # Files that do not belong together are refused, not half-used.
    paths.enrollment_key.write_text(json.dumps({"credential_id": "tagh_other", "secret": "x"}), encoding="utf-8")
    with pytest.raises(EnrollError):
        load_enrollment(paths)
    forget_enrollment(paths)
    assert load_enrollment(paths) is None and not paths.enrollment_key.exists()
