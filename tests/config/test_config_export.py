"""The configuration file: what goes in it, and what must not.

This renderer replaced 700 lines of TypeScript that ran in the browser, and it
is the only producer now — the Setup Wizard, the Profile page card and
``cremind config export`` all download what it writes. Two properties are worth
pinning hard.

**The reduced file.** Any profile may download its own configuration file, which
is the point: a member who lost their token should not have to ask the admin for
it. But the file used to mix two kinds of fact, and half of them are install-wide
credentials — the Postgres password, the VNC password, the cluster's namespace
and a runnable ``kubectl`` line. A non-admin export that leaked any of those
would be worse than the admin-only card it replaced.

**The shapes.** A configuration file is read once, months later, by someone who
has lost the install — so a section that renders wrong is discovered at the worst
possible moment. The six cases below are the branches that actually differ:
Postgres vs SQLite, Qdrant vs persistent Chroma, Docker's own noVNC port vs
Kubernetes' proxied path vs its relay tunnels, and a custom deployment's fields.
"""

from __future__ import annotations

import json

import pytest

from app.config.config_export import (
    ConfigSnapshotSources,
    _env_line,
    _pg_connection_string,
    _quote_env_value,
    assemble_config_snapshot,
    export_filename,
    render_env,
    render_json,
    render_markdown,
)


def _sources(**overrides) -> ConfigSnapshotSources:
    base = dict(
        profile="li",
        token="eyJ-token",
        token_expires_at="2026-10-14T09:12:33Z",
        agent_url="http://localhost:1515",
        agent_url_pending_https=False,
        generated_at="2026-09-14T10:00:00Z",
        install_deployment="local",
        install_mode="native",
        install_custom_values={},
        install_secrets=None,
        db_provider="sqlite",
        user_working_dir="/home/li/Documents",
        system_dir="/home/li/.cremind",
        sqlite_db_path="/home/li/.cremind/storage/cremind.db",
        embedding_config={"enabled": False, "provider": "me5", "vectorstore": {}},
        channels=(),
        full=True,
    )
    base.update(overrides)
    return ConfigSnapshotSources(**base)


# ── identity: present in every file, both scopes ─────────────────────────


def test_the_file_always_says_where_and_how_to_sign_in():
    """The two things a user needs and cannot derive: a page to open, and the
    server-side copy of the token for when this file is the thing they lost."""
    snapshot = assemble_config_snapshot(_sources())
    assert snapshot["loginUrl"] == "http://localhost:1515/#/login/li"
    assert snapshot["tokenFile"] == "/home/li/.cremind/tokens/li.token"

    markdown = render_markdown(snapshot)
    assert "http://localhost:1515/#/login/li" in markdown
    assert "/home/li/.cremind/tokens/li.token" in markdown
    assert "eyJ-token" in markdown

    env = render_env(snapshot)
    # Quoted, because the login URL is hash-routed and an unquoted ``#`` starts
    # a comment — the rest of the address would vanish on the way back in.
    assert 'CREMIND_LOGIN_URL="http://localhost:1515/#/login/li"' in env
    assert "CREMIND_TOKEN=eyJ-token" in env
    assert "CREMIND_TOKEN_FILE=/home/li/.cremind/tokens/li.token" in env


def test_a_trailing_slash_on_the_agent_url_does_not_double_up():
    snapshot = assemble_config_snapshot(_sources(agent_url="https://cremind.example.com/"))
    assert snapshot["loginUrl"] == "https://cremind.example.com/#/login/li"


def test_a_pending_https_address_is_labelled_in_both_renderings():
    """Under after-setup TLS the wizard runs on plain HTTP and this address only
    starts answering after the restart. Handing it over unlabelled reads as a
    broken file."""
    snapshot = assemble_config_snapshot(
        _sources(agent_url="https://localhost:1515", agent_url_pending_https=True),
    )
    assert "becomes active once setup finishes" in render_markdown(snapshot)
    assert "# HTTPS address; active after the post-setup restart." in render_env(snapshot)


# ── the reduced file ─────────────────────────────────────────────────────


_INSTALL_WIDE_SECRETS = {
    "vnc_password": "hunter2",
    "app_url": "http://box.example:1515",
    "install_mode": "docker",
    "novnc_port": 6080,
    "vnc_port": 5900,
    "kubernetes": None,
    "pg_host": "db.internal",
    "pg_port": 5432,
    "pg_user": "cremind",
    "pg_password": "s3cret",
    "pg_database": "cremind",
}


def test_a_non_admin_file_carries_the_profile_and_nothing_about_the_install():
    """The exact key set, asserted as a whole. A field added to the snapshot
    later must be a deliberate decision about which scope it belongs to, not
    something that rides in because nothing was watching."""
    snapshot = assemble_config_snapshot(_sources(
        full=False,
        install_mode="docker",
        install_deployment="custom",
        install_custom_values={"listen_host": "0.0.0.0", "public_url": "http://box:1515"},
        install_secrets=_INSTALL_WIDE_SECRETS,
        db_provider="postgres",
        embedding_config={
            "enabled": True,
            "provider": "me5",
            "vectorstore": {"provider": "qdrant", "qdrant": {"host": "q", "port": 6333, "api_key": "qk"}},
        },
        channels=({"type": "telegram", "mode": "bot", "id": "ch_1"},),
    ))

    assert set(snapshot) == {
        "profile", "token", "tokenExpiresAt", "agentUrl", "agentUrlPendingHttps",
        "loginUrl", "tokenFile", "generatedAt", "workingDir", "systemDir",
        "embedding", "channels", "scope", "deployment",
    }
    assert snapshot["scope"] == "profile"
    # Its own channels are the profile's own business, and are already
    # readable on GET /api/channels.
    assert snapshot["channels"] == [{"type": "telegram", "mode": "bot", "id": "ch_1"}]
    # On/off is public (GET /api/config/embedding/status); WHICH model is
    # server configuration and is only readable by admin today.
    assert snapshot["embedding"] == {"enabled": True}
    # A custom deployment's fields name the listen host and public URL.
    assert "customFields" not in snapshot["deployment"]


@pytest.mark.parametrize("render", [render_markdown, render_env, render_json])
def test_no_rendering_of_a_reduced_file_leaks_an_install_wide_secret(render):
    snapshot = assemble_config_snapshot(_sources(
        full=False,
        install_mode="docker",
        install_secrets=_INSTALL_WIDE_SECRETS,
        db_provider="postgres",
        embedding_config={
            "enabled": True,
            "provider": "me5",
            "vectorstore": {"provider": "qdrant", "qdrant": {"host": "q", "port": 6333, "api_key": "qk"}},
        },
    ))
    text = render(snapshot)
    for secret in ("hunter2", "s3cret", "db.internal", "qk"):
        assert secret not in text, f"the reduced file leaked {secret!r}"
    for heading in ("## Database", "## Vector Store", "## VNC Desktop", "## Kubernetes"):
        assert heading not in text
    for prefix in ("PG_", "VNC_", "K8S_", "QDRANT_", "DB_PROVIDER", "SQLITE_DB_PATH"):
        assert prefix not in text


def test_the_reduced_markdown_says_why_it_is_short():
    """Otherwise it reads as a broken export rather than a scoped one."""
    snapshot = assemble_config_snapshot(_sources(full=False))
    assert "only in the admin profile's export" in render_markdown(snapshot)
    assert "only in the admin profile's export" in render_env(snapshot)


# ── admin: the install shapes ────────────────────────────────────────────


def test_postgres_and_qdrant_render_ready_to_paste_connection_strings():
    snapshot = assemble_config_snapshot(_sources(
        db_provider="postgres",
        install_secrets={
            "pg_host": "db.internal", "pg_port": 6543, "pg_user": "cre mind",
            "pg_password": "p@ss/word", "pg_database": "cremind", "pg_sslmode": "require",
        },
        embedding_config={
            "enabled": True, "provider": "me5",
            "vectorstore": {
                "provider": "qdrant",
                "qdrant": {"host": "q.internal", "port": 6333, "https": True, "api_key": "qk"},
            },
        },
    ))
    markdown = render_markdown(snapshot)
    assert "postgresql://cre%20mind:p%40ss%2Fword@db.internal:6543/cremind?sslmode=require" in markdown
    assert "https://q.internal:6333" in markdown
    env = render_env(snapshot)
    assert "PG_URL=postgresql://cre%20mind:p%40ss%2Fword@db.internal:6543/cremind?sslmode=require" in env
    assert "QDRANT_HTTPS=true" in env


def test_sqlite_names_the_real_database_path_not_a_guess():
    """The browser used to compose ``<system dir>/storage/cremind.db``; the
    server knows where the file actually is, which is the point of moving the
    renderer here."""
    snapshot = assemble_config_snapshot(_sources(sqlite_db_path="/mnt/data/cremind.db"))
    assert snapshot["database"]["sqlite"] == {"path": "/mnt/data/cremind.db"}
    assert "/mnt/data/cremind.db" in render_markdown(snapshot)


def test_a_native_chroma_install_is_a_persistent_file_not_an_endpoint():
    snapshot = assemble_config_snapshot(_sources(
        embedding_config={
            "enabled": True, "provider": "me5",
            "vectorstore": {
                "provider": "chroma",
                "chroma": {"deployment_mode": "native", "persist_path": "/var/chroma"},
            },
        },
    ))
    assert snapshot["vectorStore"]["chroma"]["mode"] == "persistent"
    markdown = render_markdown(snapshot)
    assert "- **Mode:** persistent" in markdown
    assert "/var/chroma" in markdown


def test_embedding_turned_off_leaves_no_vector_store_section():
    snapshot = assemble_config_snapshot(_sources(
        embedding_config={
            "enabled": False, "provider": "me5",
            "vectorstore": {"provider": "qdrant", "qdrant": {"host": "q", "port": 6333}},
        },
    ))
    assert snapshot["vectorStore"] == {"provider": "none"}
    assert "## Vector Store" not in render_markdown(snapshot)


def test_a_docker_desktop_names_its_own_published_novnc_port():
    """Always http and always the container's port: websockify's listener is
    not Cremind's, so it stays plain even where Cremind serves HTTPS."""
    snapshot = assemble_config_snapshot(_sources(
        install_mode="docker",
        agent_url="https://box.example:1515",
        install_secrets={
            "vnc_password": "hunter2", "app_url": "https://box.example:1515",
            "install_mode": "docker", "novnc_port": 6080, "vnc_port": 5900,
            "resolution": "1920x1080",
        },
    ))
    vnc = snapshot["vnc"]
    assert vnc["environment"] == "docker"
    assert vnc["novnc_url"] == "http://box.example:6080/vnc.html"
    assert vnc["vnc_endpoint"] == "box.example:5900"
    markdown = render_markdown(snapshot)
    assert "## VNC Desktop (Docker)" in markdown
    assert "- **VNC port:** `5900`" in markdown


def test_a_kubernetes_relay_carries_the_tunnels_that_have_to_exist_first():
    """The reader is offline from the cluster by the time they open this file,
    so the commands are the whole value of the section."""
    snapshot = assemble_config_snapshot(_sources(
        install_mode="kubernetes",
        install_deployment="kubernetes",
        install_secrets={
            "vnc_password": "hunter2",
            "app_url": "http://localhost:1515",
            "install_mode": "kubernetes",
            "vnc": {"port_forward_commands": [
                {"label": "Desktop", "command": "kubectl port-forward svc/cremind 6080:6080",
                 "open_url": "http://localhost:6080/vnc.html"},
            ]},
            "kubernetes": {
                "namespace": "cremind", "release": "cremind", "workload": "cremind",
                "service": "cremind", "service_port": 80, "source": "chart",
                "port_forward": "kubectl port-forward -n cremind svc/cremind 1515:80",
            },
        },
    ))
    assert snapshot["deployment"]["mode"] == "docker", "a pod is a container"
    assert snapshot["deployment"]["type"] == "kubernetes", "but the chart is named"
    assert snapshot["kubernetes"]["deployment"] == "cremind"
    markdown = render_markdown(snapshot)
    assert "kubectl port-forward -n cremind svc/cremind 1515:80" in markdown
    assert "kubectl port-forward svc/cremind 6080:6080" in markdown
    assert "1455:1455" in markdown, "the Codex OAuth callback hint"
    # noVNC is behind one proxied port there, so the raw client lines are wrong.
    assert "- **VNC client:**" not in markdown
    assert "K8S_PORT_FORWARD=" in render_env(snapshot)


def test_an_inferred_kubernetes_identity_says_which_name_is_a_guess():
    snapshot = assemble_config_snapshot(_sources(
        install_mode="kubernetes",
        install_secrets={"kubernetes": {"namespace": "ns", "workload": "cremind", "source": "inferred"}},
    ))
    assert "helm list --all-namespaces" in render_markdown(snapshot)


def test_a_custom_deployment_carries_the_installer_answers():
    snapshot = assemble_config_snapshot(_sources(
        install_deployment="custom",
        install_custom_values={"listen_host": "0.0.0.0", "public_url": "http://box:1515", "wizard_preset": ""},
    ))
    markdown = render_markdown(snapshot)
    assert "- `listen_host` = `0.0.0.0`" in markdown
    assert "wizard_preset" not in markdown, "an empty answer is not an answer"
    assert "CREMIND_LISTEN_HOST=0.0.0.0" in render_env(snapshot)


def test_channels_are_listed_with_their_ids():
    snapshot = assemble_config_snapshot(_sources(
        channels=({"type": "telegram", "mode": "bot", "id": "ch_1"},),
    ))
    assert "`telegram` (bot) — id: `ch_1`" in render_markdown(snapshot)
    assert "CHANNEL_0_ID=ch_1" in render_env(snapshot)


# ── the renderings themselves ────────────────────────────────────────────


def test_json_is_the_snapshot_verbatim_and_keeps_false_and_zero():
    """``None`` is dropped the way ``JSON.stringify`` drops ``undefined``, but
    ``false`` is an answer — an export claiming nothing about ``https`` reads
    very differently from one that says no."""
    snapshot = assemble_config_snapshot(_sources(
        embedding_config={
            "enabled": True, "provider": "me5",
            "vectorstore": {"provider": "qdrant", "qdrant": {"host": "q", "port": 6333, "https": False}},
        },
    ))
    parsed = json.loads(render_json(snapshot))
    assert parsed == snapshot
    assert parsed["vectorStore"]["qdrant"]["https"] is False
    assert "api_key" not in parsed["vectorStore"]["qdrant"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("", ""),
        ("plain", "plain"),
        ("with space", '"with space"'),
        ('has "quote"', '"has \\"quote\\""'),
        ("back\\slash", '"back\\\\slash"'),
        ("hash#mark", '"hash#mark"'),
        ("dollar$sign", '"dollar$sign"'),
        ("tick`mark", '"tick`mark"'),
    ],
)
def test_env_values_are_quoted_the_way_a_shell_reads_them_back(value, expected):
    assert _quote_env_value(value) == expected


def test_env_lines_skip_nothing_but_actual_absence():
    assert _env_line("K", None) is None
    assert _env_line("K", "") is None
    assert _env_line("K", False) == "K=false"
    assert _env_line("K", 0) == "K=0"
    assert _env_line("K", True) == "K=true"


def test_the_connection_string_escapes_like_encodeuricomponent():
    """Ported from the browser, where ``encodeURIComponent`` left exactly these
    unreserved marks alone. A password with a slash in it is the case that
    silently produced an unusable URL."""
    assert _pg_connection_string({
        "user": "a-b_c.d!e~f*g'h(i)", "password": "p/w", "host": "h", "port": 1, "database": "d",
    }) == "postgresql://a-b_c.d!e~f*g'h(i):p%2Fw@h:1/d"


def test_the_filename_is_the_one_both_producers_have_always_written():
    assert export_filename("li", "md") == "cremind-li-config.md"
    assert export_filename("li", "env") == "cremind-li-config.env"
