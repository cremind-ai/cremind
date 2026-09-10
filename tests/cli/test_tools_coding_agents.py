"""CLI: `cremind tools coding-agents` — the listing, plus CLI-owned sign-in.

The commands do function-body imports from ``app.cli.client.tools``, so the
client calls are patched there and nothing reaches the network.

``login`` is the odd one out in the whole CLI: it is the only command that runs
*another program* in the user's own terminal. What matters about it is not its
output but the decisions it makes before the spawn — whether the CLI it was told
about is on THIS machine (signing in on the wrong host writes a credential the
server will never read), whether the server says its CPU can run that CLI at all
(where no copy of the binary would work and the fix is on the hypervisor), and
which home the child is pointed at — so those are what these tests pin. Each of
those the CLI *learns*, never computes: modules under ``app/cli`` must not import
``app.tools``, so a descriptor without the key has to behave exactly as an older
server made it behave.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner


# ── fakes ──────────────────────────────────────────────────────────────────

_AGENTS = [
    {
        "tool_id": "claude_code",
        "display_name": "Claude Code",
        "sdk_installed": True,
        "enabled": True,
        "credential_source": "profile_claude_login",
        "credential_scope": "profile",
        "message": "Claude Code is installed, enabled, and using profile_claude_login.",
    },
    {
        "tool_id": "codex",
        "display_name": "Codex",
        "sdk_installed": False,
        "enabled": False,
        "credential_source": None,
        "credential_scope": None,
        "message": "Codex is not installed on this server.",
    },
]

_CPU_BLOCKER = {
    "code": "cpu_features",
    "cpu_model": "QEMU Virtual CPU version 2.5+",
    "missing": ["ssse3", "sse4_1", "sse4_2", "popcnt"],
    "hypervisor": True,
    "message": (
        "The Claude Code CLI cannot run on this server's CPU: it is a Bun "
        "single-file executable built for the x86-64-v2 instruction level, and "
        'this virtual CPU "QEMU Virtual CPU version 2.5+" advertises none of '
        "ssse3, sse4_1, sse4_2, popcnt."
    ),
    "remedy": (
        "The fix is on the hypervisor: in Proxmox, set the VM's Hardware -> "
        "Processors -> Type to 'host'. The node has to be shut down and started "
        "again afterwards."
    ),
}

_SIGNED_IN = {
    "tool_id": "claude_code",
    "logged_in": True,
    "account": {
        "email": "dev@example.com",
        "subscription_type": "max",
        "auth_method": "claude_ai_oauth",
    },
}


def _patch_listing(monkeypatch, agents=None, probe=None):
    """Patch the two listing-side client calls; return what they were asked."""
    captured: dict = {"probes": []}

    async def fake_agents(client):
        return [dict(a) for a in (agents if agents is not None else _AGENTS)]

    async def fake_probe(client, tool_id, fresh=False):
        captured["probes"].append((tool_id, fresh))
        return dict(probe or _SIGNED_IN)

    import app.cli.client.tools as tools_client

    monkeypatch.setattr(tools_client, "get_coding_agents", fake_agents)
    monkeypatch.setattr(tools_client, "probe_coding_agent", fake_probe)
    return captured


def _patch_cli_descriptor(monkeypatch, payload, probe=None):
    """Patch `GET /cli` + the post-login probe; return what they were asked."""
    captured: dict = {"probes": []}

    async def fake_cli(client, tool_id):
        captured["cli_tool_id"] = tool_id
        return dict(payload)

    async def fake_probe(client, tool_id, fresh=False):
        captured["probes"].append((tool_id, fresh))
        return dict(probe or _SIGNED_IN)

    import app.cli.client.tools as tools_client

    monkeypatch.setattr(tools_client, "get_coding_agent_cli", fake_cli)
    monkeypatch.setattr(tools_client, "probe_coding_agent", fake_probe)
    return captured


def _patch_subprocess(monkeypatch, exit_code=0):
    """Replace `subprocess.call` so no real login binary is ever spawned."""
    captured: dict = {}

    def fake_call(argv, env=None, **kwargs):
        captured["argv"] = list(argv)
        captured["env"] = dict(env or {})
        return exit_code

    import subprocess

    monkeypatch.setattr(subprocess, "call", fake_call)
    return captured


def _local_descriptor(tmp_path, **overrides):
    """A `/cli` payload whose binary and system dir really exist here.

    Both have to exist for the command to accept this host, so the fixture
    creates both; a test that wants the "wrong host" branch overrides one.
    """
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    system_dir = tmp_path / "sysdir"
    system_dir.mkdir()
    payload = {
        "tool_id": "claude_code",
        "binary": str(binary),
        "binary_source": "bundled",
        "login_argv": [str(binary), "auth", "login"],
        "logout_argv": [str(binary), "auth", "logout"],
        "status_argv": [str(binary), "auth", "status", "--json"],
        "profile_env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "sysdir" / "admin" / "claude")},
        "shared_env": {"CLAUDE_CONFIG_DIR": str(tmp_path / "sysdir" / "coding-cli" / "claude")},
        "server_hostname": "cremind-7d9f4c",
        "system_dir": str(system_dir),
        "platform": "linux",
    }
    payload.update(overrides)
    return payload


# ── the listing ────────────────────────────────────────────────────────────


def test_bare_command_still_renders_the_table(monkeypatch: pytest.MonkeyPatch) -> None:
    # Turning the command into a group must not cost anyone the table it was.
    from app.cli.main import app

    captured = _patch_listing(monkeypatch)
    result = CliRunner().invoke(app, ["--token", "t", "tools", "coding-agents"])
    assert result.exit_code == 0, result.output
    assert "claude_code" in result.output
    assert "codex" in result.output
    assert "profile_claude_login" in result.output
    # No --probe: nothing was run live.
    assert captured["probes"] == []


def test_probe_asks_for_a_fresh_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    # Someone who typed --probe wants the live answer, not the one cached from
    # a burst seconds ago — which is exactly the answer a probe run right after
    # a sign-in would otherwise get.
    from app.cli.main import app

    captured = _patch_listing(monkeypatch)
    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "--probe"],
    )
    assert result.exit_code == 0, result.output
    # Only the installed agent is worth a round-trip.
    assert captured["probes"] == [("claude_code", True)]
    assert "yes" in result.output


def test_listing_prints_the_remedy_for_a_blocked_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one row whose fix is not something Cremind can do.

    The server keeps the remedy out of ``message`` because it is a paragraph
    about hypervisor CPU models rather than a clause, so the listing has to
    render it from ``cli_blocked`` - otherwise the table says the CLI cannot run
    and never says what to do about it.
    """
    from app.cli.main import app

    blocked = [
        {**_AGENTS[0], "message": _CPU_BLOCKER["message"], "cli_blocked": _CPU_BLOCKER},
        {**_AGENTS[1], "cli_blocked": None},
    ]
    _patch_listing(monkeypatch, agents=blocked)
    result = CliRunner().invoke(app, ["--token", "t", "tools", "coding-agents"])
    assert result.exit_code == 0, result.output
    assert "QEMU Virtual CPU version 2.5+" in result.output
    assert "Proxmox" in result.output
    # The unblocked row contributes no second remedy line.
    assert result.output.count("Proxmox") == 1


def test_listing_json_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    _patch_listing(monkeypatch)
    result = CliRunner().invoke(
        app, ["--json", "--token", "t", "tools", "coding-agents"],
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [r["tool_id"] for r in rows] == ["claude_code", "codex"]


# ── login ──────────────────────────────────────────────────────────────────


def test_login_refuses_when_the_cli_is_not_on_this_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The paths in the descriptor are the SERVER's. Running the login here would
    # sign in against a home directory on this machine that the server never
    # reads — a silent failure the user only discovers later — so the command
    # refuses and names where the CLI actually is.
    from app.cli.main import app

    _patch_cli_descriptor(
        monkeypatch,
        _local_descriptor(tmp_path, system_dir=str(tmp_path / "not-here")),
    )
    spawned = _patch_subprocess(monkeypatch)

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 1, result.output
    assert "cremind-7d9f4c" in result.output
    assert "Settings -> Tools & Skills -> Coding Agents -> Sign in" in result.output
    assert spawned == {}


def test_login_refuses_when_the_server_has_no_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # A missing binary is a different problem with a different fix, and saying
    # "run it on the server" would send the user to a host that has nothing to
    # run either.
    from app.cli.main import app

    _patch_cli_descriptor(monkeypatch, _local_descriptor(tmp_path, binary=None))
    spawned = _patch_subprocess(monkeypatch)

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 1, result.output
    assert "features install claude_code" in result.output
    assert spawned == {}


def test_login_refuses_on_a_server_whose_cpu_cannot_run_the_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The refusal the other two cannot express.

    Here the binary is present, it is on this very host, and it still cannot
    run: the bundled Claude Code executable on a CPU without the instructions it
    was built for spins at 100% CPU forever instead of failing. So neither
    "install the feature" nor "run this on the server" is the fix, nothing is
    spawned, and the CPU model and the hypervisor setting go to stderr instead.
    The CLI cannot work this out itself - it must not import ``app.tools`` - so
    it obeys the descriptor, exactly as it does for ``drop_env``.
    """
    from app.cli.main import app

    _patch_cli_descriptor(
        monkeypatch, _local_descriptor(tmp_path, cli_blocked=_CPU_BLOCKER),
    )
    spawned = _patch_subprocess(monkeypatch)

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 1, result.output
    assert "QEMU Virtual CPU version 2.5+" in result.output
    assert "Proxmox" in result.output
    # Named, so a user with two servers knows which one this is about.
    assert "cremind-7d9f4c" in result.output
    # And neither of the other two fixes is offered, because neither works.
    assert "features install" not in result.output
    assert spawned == {}


def test_login_on_an_older_server_without_the_blocker_key_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A server that predates the check sends no ``cli_blocked`` at all, and
    that has to read as "nothing known to be wrong" - a missing key must never
    become a refusal, or an upgrade would take the sign-in away from every host
    where it works."""
    from app.cli.main import app

    descriptor = _local_descriptor(tmp_path)
    descriptor.pop("cli_blocked", None)
    _patch_cli_descriptor(monkeypatch, descriptor)
    spawned = _patch_subprocess(monkeypatch)

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 0, result.output
    assert spawned["argv"] == descriptor["login_argv"]


def test_login_runs_the_argv_with_the_profile_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from app.cli.main import app

    descriptor = _local_descriptor(tmp_path)
    captured = _patch_cli_descriptor(monkeypatch, descriptor)
    spawned = _patch_subprocess(monkeypatch)
    monkeypatch.setenv("CREMIND_TEST_AMBIENT", "kept")

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 0, result.output
    assert spawned["argv"] == descriptor["login_argv"]
    # The child is pointed at THIS profile's CLI home...
    assert spawned["env"]["CLAUDE_CONFIG_DIR"] == descriptor["profile_env"]["CLAUDE_CONFIG_DIR"]
    # ...on top of the real environment, not instead of it: the login needs PATH,
    # HOME and whatever proxy variables the user has set.
    assert spawned["env"]["CREMIND_TEST_AMBIENT"] == "kept"
    # And the verdict afterwards is the live one, not a cached "not signed in".
    assert captured["probes"] == [("claude_code", True)]
    assert "dev@example.com" in result.output


def test_login_drops_the_environment_the_server_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A shell sign-in inside our image must not inherit the desktop's display.

    The desktop flavour exports ``DISPLAY=:0`` for the VNC session, and a login
    that can see a display opens a browser on that desktop instead of printing
    the URL the user's terminal is waiting for - the same failure the built-in
    terminal was fixed for. The server decides (only it knows it is in a
    container) and this command obeys, so both sign-in doors behave alike.
    """
    from app.cli.main import app

    descriptor = _local_descriptor(tmp_path)
    descriptor["drop_env"] = ["DISPLAY", "WAYLAND_DISPLAY", "BROWSER"]
    _patch_cli_descriptor(monkeypatch, descriptor)
    spawned = _patch_subprocess(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("CREMIND_TEST_AMBIENT", "kept")

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 0, result.output
    assert "DISPLAY" not in spawned["env"]
    # Only the named variables go: the login still needs the rest of the
    # environment it was started from.
    assert spawned["env"]["CREMIND_TEST_AMBIENT"] == "kept"
    assert spawned["env"]["CLAUDE_CONFIG_DIR"] == descriptor["profile_env"]["CLAUDE_CONFIG_DIR"]


def test_login_keeps_the_display_when_the_server_names_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """On a native install the browser opening is the point, and an older server
    omits the key entirely - both mean "take nothing away"."""
    from app.cli.main import app

    descriptor = _local_descriptor(tmp_path)
    descriptor.pop("drop_env", None)
    _patch_cli_descriptor(monkeypatch, descriptor)
    spawned = _patch_subprocess(monkeypatch)
    monkeypatch.setenv("DISPLAY", ":0")

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 0, result.output
    assert spawned["env"]["DISPLAY"] == ":0"


def test_login_shared_targets_the_server_wide_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from app.cli.main import app

    descriptor = _local_descriptor(tmp_path)
    _patch_cli_descriptor(monkeypatch, descriptor)
    spawned = _patch_subprocess(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["--token", "t", "tools", "coding-agents", "login", "claude_code", "--shared"],
    )
    assert result.exit_code == 0, result.output
    assert spawned["env"]["CLAUDE_CONFIG_DIR"] == descriptor["shared_env"]["CLAUDE_CONFIG_DIR"]


def test_login_reports_the_binary_exit_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # A cancelled or failed `claude auth login` must not be followed by a
    # cheerful "Signed in".
    from app.cli.main import app

    captured = _patch_cli_descriptor(monkeypatch, _local_descriptor(tmp_path))
    _patch_subprocess(monkeypatch, exit_code=2)

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 2, result.output
    assert captured["probes"] == []


def test_login_says_so_when_the_server_still_sees_no_sign_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # The usual cause is scope: a --shared login while this profile has a home
    # of its own, or the reverse. Claiming success would hide it.
    from app.cli.main import app

    _patch_cli_descriptor(
        monkeypatch,
        _local_descriptor(tmp_path),
        probe={"tool_id": "claude_code", "logged_in": False, "probe_detail": "no credential"},
    )
    _patch_subprocess(monkeypatch)

    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "login", "claude_code"],
    )
    assert result.exit_code == 1, result.output
    assert "does not report a sign-in" in result.output


# ── logout ─────────────────────────────────────────────────────────────────


def _patch_logout(monkeypatch, payload=None):
    captured: dict = {}

    async def fake_logout(client, tool_id, scope="profile"):
        captured["tool_id"] = tool_id
        captured["scope"] = scope
        return dict(payload or {"ok": True, "scope": scope, "detail": "Signed out."})

    import app.cli.client.tools as tools_client

    monkeypatch.setattr(tools_client, "logout_coding_agent", fake_logout)
    return captured


def test_logout_posts_the_profile_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    # No binary check anywhere in here: the server runs the logout, so this
    # works from a laptop pointed at a remote Cremind.
    from app.cli.main import app

    captured = _patch_logout(monkeypatch)
    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "logout", "codex"],
    )
    assert result.exit_code == 0, result.output
    assert captured == {"tool_id": "codex", "scope": "profile"}
    assert "Signed out of codex" in result.output


def test_logout_shared_asks_for_the_shared_login(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    captured = _patch_logout(monkeypatch)
    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "logout", "codex", "--shared"],
    )
    assert result.exit_code == 0, result.output
    assert captured["scope"] == "shared"


def test_logout_failure_exits_non_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.cli.main import app

    _patch_logout(
        monkeypatch,
        {"ok": False, "scope": "profile", "detail": "This profile has no login of its own."},
    )
    result = CliRunner().invoke(
        app, ["--token", "t", "tools", "coding-agents", "logout", "codex"],
    )
    assert result.exit_code == 1, result.output
    assert "no login of its own" in result.output
