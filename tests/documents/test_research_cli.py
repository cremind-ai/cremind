"""`cremind docs research …` — deep-research jobs from the terminal.

The commands import their client functions inside the body, so the client is
patched in ``app.cli.client.docs`` and nothing reaches the network. What
is pinned: the flags become exactly the request the REST API takes, `--follow`
long-polls until the job finishes or asks, printing one progress line per
change, the exit code says how it ended (0 done, 2 waiting for you, 1 failed),
`--answer KEY=VALUE` parses as documented, and the CLI stays free of server
modules.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

pytest.importorskip("typer")

from typer.testing import CliRunner  # noqa: E402


def _job(status: str = "queued", job_id: str = "job1", **kw) -> dict:
    return {"job_id": job_id, "profile": "alice", "status": status, "mode": "analyze", "domain": "general",
            "question": "q", "budget": 250000, "tokens_in": 0, "tokens_out": 0, **kw}


def _answer(status: str = "queued", text: str | None = None, **kw) -> dict:
    out = {"job": _job(status, **kw), "text": text or f"[research] {status}"}
    return out


@pytest.fixture
def client(monkeypatch):
    """Scripted client: ``gets`` is the queue of GET answers (the last one
    repeats); every call is recorded."""
    import app.cli.client.docs as c
    import app.cli.commands.docs as cmd

    monkeypatch.setattr(cmd, "_FOLLOW_MIN_INTERVAL", 0.0)
    state = {"calls": [], "start": _answer("queued"), "gets": [_answer("complete")],
             "continue": _answer("running"), "cancel": _answer("cancelled"), "list": {"jobs": []}}

    async def research_start(client, body):
        state["calls"].append(("start", body))
        return state["start"]

    async def research_get(client, job_id, *, page=None, wait=None):
        state["calls"].append(("get", job_id, page, wait))
        return state["gets"].pop(0) if len(state["gets"]) > 1 else state["gets"][0]

    async def research_continue(client, job_id, *, answers=None, wait=None):
        state["calls"].append(("continue", job_id, answers))
        return state["continue"]

    async def research_cancel(client, job_id):
        state["calls"].append(("cancel", job_id))
        return state["cancel"]

    async def research_list(client, *, limit=20):
        state["calls"].append(("list", limit))
        return state["list"]

    for fn in (research_start, research_get, research_continue, research_cancel, research_list):
        monkeypatch.setattr(c, fn.__name__, fn)
    return state


def _run(*args):
    from app.cli.main import app

    return CliRunner().invoke(app, ["--token", "t", *args])


def _named(state, name):
    return [c for c in state["calls"] if c[0] == name]


# ── run ────────────────────────────────────────────────────────────────────


def test_run_flags_become_the_request_body(client):
    result = _run("docs", "research", "run", "Compile the business results",
                  "--mode", "compile", "--domain", "financial",
                  "--folder", "MKT-report", "--folder", "Sales", "--file", "k7m2xq9a",
                  "--reference-folder", "Luat", "--reference-file", "[doc:abcd2345]", "--wait", "30")
    assert result.exit_code == 0, result.output
    [(_, body)] = _named(client, "start")
    assert body == {
        "question": "Compile the business results", "mode": "compile", "domain": "financial",
        "scope": {"folder": ["MKT-report", "Sales"], "file_ids": ["k7m2xq9a"]},
        "reference_scope": {"folder": ["Luat"], "file_ids": ["[doc:abcd2345]"]},
        "wait": 30,
    }
    assert "[research] queued" in result.stdout
    # Not followed: the job id and how to follow it, on stderr.
    assert "Research job job1 started" in result.stderr
    assert "research status job1 --follow" in result.stderr
    assert _named(client, "get") == []


def test_run_without_flags_leaves_the_defaults_to_the_server(client):
    result = _run("docs", "research", "run", "Why?")
    assert result.exit_code == 0, result.output
    body = _named(client, "start")[0][1]
    assert body == {"question": "Why?", "mode": None, "domain": None, "scope": None,
                    "reference_scope": None, "wait": None}


@pytest.mark.parametrize("status,code", [
    ("complete", 0), ("partial", 0),
    ("needs_clarification", 2), ("needs_confirmation", 2), ("interrupted", 2),
    ("failed", 1), ("cancelled", 1),
])
def test_follow_prints_progress_then_the_text_and_exits_by_outcome(client, status, code):
    step = lambda i, label: {"id": f"s{i}", "label": label, "status": "running"}  # noqa: E731
    running = lambda done, steps, tin: _answer(  # noqa: E731
        "running", progress={"phase": "Reading files", "done": done, "total": 3, "steps": steps}, tokens_in=tin)
    client["gets"] = [
        running(1, [step(1, "Reading A.pdf (1/3)")], 100),
        running(1, [step(1, "Reading A.pdf (1/3)")], 900),   # only tokens moved: no new line
        running(2, [step(1, "Reading A.pdf (1/3)"), step(2, "Reading B.pdf (2/3)")], 1500),
        _answer(status, text=f"FINAL {status}"),
    ]
    result = _run("docs", "research", "run", "q", "--follow")
    assert result.exit_code == code, result.output
    assert result.stdout.strip() == f"FINAL {status}"
    lines = [line for line in result.stderr.splitlines() if line.startswith("job1: ")]
    assert lines == [
        "job1: queued",
        "job1: running · Reading files · 1/3 · Reading A.pdf (1/3) · 100/250,000 tokens",
        "job1: running · Reading files · 2/3 · Reading B.pdf (2/3) · 1,500/250,000 tokens",
        f"job1: {status}",
    ]
    # Every poll long-polls the server.
    assert {c[3] for c in _named(client, "get")} == {20.0}
    assert len(_named(client, "get")) == 4


def test_a_question_prints_how_to_answer_it(client):
    clar = {"kind": "edition", "question": "Which edition of the Land Law?",
            "candidates": [], "answer_keys": {"edition": "a fid from the candidates"}}
    client["gets"] = [_answer("needs_clarification", text="Which edition of the Land Law?",
                              dossier={"clarification": clar})]
    result = _run("docs", "research", "status", "job1")
    assert result.exit_code == 2
    assert "Which edition of the Land Law?" in result.stdout
    assert "research continue job1 --answer edition=... --follow" in result.stderr
    assert "edition: a fid from the candidates" in result.stderr


def test_an_interrupted_job_says_how_to_resume(client):
    client["gets"] = [_answer("interrupted")]
    result = _run("docs", "research", "status", "job1")
    assert result.exit_code == 2
    assert "research continue job1 --follow" in result.stderr


def test_busy_names_the_running_job(client, monkeypatch):
    import app.cli.client.docs as c
    from app.cli.client._base import APIError

    async def busy(client, body):
        raw = json.dumps({"error": "ResearchBusy", "message": "A research job is already running.",
                          "job_id": "abc123", "question": "earlier"}).encode()
        raise APIError(409, raw.decode(), raw)

    monkeypatch.setattr(c, "research_start", busy)
    result = _run("docs", "research", "run", "q")
    assert result.exit_code == 1
    assert "already running" in result.stderr
    assert "research status abc123 --follow" in result.stderr and "research cancel abc123" in result.stderr


# ── status ─────────────────────────────────────────────────────────────────


def test_status_prints_one_page(client):
    client["gets"] = [{**_answer("complete", text="page two"), "pages": 3}]
    result = _run("docs", "research", "status", "job1", "--page", "2")
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == "page two"
    assert _named(client, "get") == [("get", "job1", 2, None)]
    assert "The dossier has 3 pages" in result.stderr


def test_status_all_pages_prints_every_page(client):
    client["gets"] = [{**_answer("complete", text="summary"), "pages": 3},
                      {**_answer("complete", text="findings"), "pages": 3},
                      {**_answer("complete", text="coverage"), "pages": 3}]
    result = _run("docs", "research", "status", "job1", "--all-pages")
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.index("summary") < out.index("── page 2 of 3 ──") < out.index("findings") \
        < out.index("── page 3 of 3 ──") < out.index("coverage")
    assert [c[2] for c in _named(client, "get")] == [None, 2, 3]


def test_page_and_all_pages_are_exclusive(client):
    result = _run("docs", "research", "status", "job1", "--page", "2", "--all-pages")
    assert result.exit_code == 1
    assert client["calls"] == []


def test_status_follow_keeps_the_page(client):
    client["gets"] = [_answer("running"), {**_answer("complete", text="p2"), "pages": 2}]
    result = _run("docs", "research", "status", "job1", "--page", "2", "--follow")
    assert result.exit_code == 0, result.output
    assert [(c[2], c[3]) for c in _named(client, "get")] == [(2, None), (2, 20.0)]
    assert result.stdout.strip() == "p2"


def test_json_prints_the_whole_answer(client):
    client["gets"] = [{**_answer("partial", text="t"), "pages": 1}]
    result = _run("--json", "docs", "research", "status", "job1")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["job"]["status"] == "partial" and out["text"] == "t" and out["pages"] == 1


# ── continue / cancel / list ───────────────────────────────────────────────


def test_continue_parses_answers(client):
    client["continue"] = _answer("complete", text="done")
    result = _run("docs", "research", "continue", "job1", "--answer", "edition=k7m2xq9a",
                  "-a", "confirm=TRUE", "--answer", "confirm_budget=false", "--answer", "note= a=b ",
                  "--answer", "rank=2")
    assert result.exit_code == 0, result.output
    [(_, job_id, answers)] = _named(client, "continue")
    assert job_id == "job1"
    assert answers == {"edition": "k7m2xq9a", "confirm": True, "confirm_budget": False, "note": "a=b",
                       "rank": "2"}
    assert result.stdout.strip() == "done"


def test_continue_without_answers_resumes(client):
    result = _run("docs", "research", "continue", "job1", "--follow")
    assert result.exit_code == 0, result.output
    assert _named(client, "continue") == [("continue", "job1", None)]
    assert len(_named(client, "get")) == 1  # followed from running to complete


@pytest.mark.parametrize("bad", ["edition", "=k7m2xq9a"])
def test_a_malformed_answer_sends_nothing(client, bad):
    result = _run("docs", "research", "continue", "job1", "--answer", bad)
    assert result.exit_code == 1
    assert "KEY=VALUE" in result.stderr
    assert client["calls"] == []


def test_cancel_prints_the_text_and_succeeds(client):
    client["cancel"] = _answer("cancelled", text="job1 cancelled")
    result = _run("docs", "research", "cancel", "job1")
    assert result.exit_code == 0, result.output
    assert _named(client, "cancel") == [("cancel", "job1")]
    assert "job1 cancelled" in result.stdout


def test_list_prints_one_row_per_job(client):
    client["list"] = {"jobs": [
        _job("complete", job_id="3f9c2e1b7a40", question="Compile the business results\nper quarter",
             mode="compile", domain="financial", created_at=1_790_000_000_000),
        _job("needs_clarification", job_id="0a1b2c3d4e5f", question="x" * 120),
    ]}
    result = _run("docs", "research", "list", "--limit", "5")
    assert result.exit_code == 0, result.output
    assert _named(client, "list") == [("list", 5)]
    rows = result.stdout.splitlines()
    assert rows[0].startswith("3f9c2e1b7a40  complete") and "compile/financial" in rows[0]
    assert "Compile the business results per quarter" in rows[0]
    assert rows[1].startswith("0a1b2c3d4e5f  needs_clarification") and rows[1].endswith("…")


def test_list_empty(client):
    result = _run("docs", "research", "list")
    assert result.exit_code == 0
    assert "(no research jobs)" in result.stdout
    assert _named(client, "list") == [("list", 20)]


# ── contracts ──────────────────────────────────────────────────────────────


def test_the_status_sets_mirror_the_server():
    import app.cli.commands.docs as cmd
    from app.documents.research import types as T

    assert cmd._RESEARCH_ACTIVE == T.ACTIVE
    assert cmd._RESEARCH_WAITING == T.WAITING
    assert cmd._RESEARCH_DONE | {"failed", "cancelled"} == T.FINAL


def test_the_client_builds_the_documented_requests(monkeypatch):
    """The wrappers themselves: paths, query params, and None left out."""
    import asyncio

    import app.cli.client.docs as c

    seen = []

    class _Client:
        async def get_json(self, path, *, params=None):
            seen.append(("GET", path, params))
            return {}

        async def post_json(self, path, body=None, *, params=None):
            seen.append(("POST", path, body))
            return {}

    async def go():
        cl = _Client()
        await c.research_start(cl, {"question": "q", "mode": None, "scope": None, "wait": 5})
        await c.research_get(cl, "job1", page=2, wait=20)
        await c.research_get(cl, "job1")
        await c.research_continue(cl, "job1", answers={"edition": "k7m2xq9a"})
        await c.research_cancel(cl, "a/b")
        await c.research_list(cl, limit=7)

    asyncio.run(go())
    assert seen == [
        ("POST", "/api/documentation-search/research", {"question": "q", "wait": 5}),
        ("GET", "/api/documentation-search/research/job1", {"page": 2, "wait": 20}),
        ("GET", "/api/documentation-search/research/job1", None),
        ("POST", "/api/documentation-search/research/job1/continue", {"answers": {"edition": "k7m2xq9a"}}),
        ("POST", "/api/documentation-search/research/a%2Fb/cancel", {}),
        ("GET", "/api/documentation-search/research", {"limit": 7}),
    ]


def test_the_cli_imports_no_server_module():
    """CLAUDE.md's import discipline: the slim `pip install cremind` has no
    server dependencies, so no CLI module may import one at top level."""
    code = (
        "import sys, app.cli.main, app.cli.commands.docs, app.cli.client.docs\n"
        "bad = ('app.server', 'app.api', 'app.tools', 'app.agent', 'app.skills', 'app.events', "
        "'app.cremind_documents', 'app.channels', 'app.databases', 'app.storage', 'app.documents')\n"
        "print(sorted(m for m in sys.modules if m.startswith(bad)))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip().splitlines()[-1] == "[]"
