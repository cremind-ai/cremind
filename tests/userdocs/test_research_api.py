"""`/api/userdocs/research` — the REST face of deep-research jobs.

The routes are thin over :mod:`app.userdocs.research.jobs` and
:mod:`app.userdocs.research.render`, so both are replaced by fakes (through
the module's own ``_jobs`` / ``_render_mod`` seams) and what is pinned is the
contract around them: the body is validated before a job starts, errors keep
their status and fields, another profile's job does not exist, ``page`` and
``wait`` are clamped rather than trusted, delivery is claimed only for a
state the caller was actually shown, and the list never carries dossiers.
"""

from __future__ import annotations

import inspect
from dataclasses import replace
from types import SimpleNamespace

import pytest

pytest.importorskip("a2a")

from starlette.applications import Starlette  # noqa: E402
from starlette.authentication import AuthCredentials, AuthenticationBackend, SimpleUser  # noqa: E402
from starlette.middleware import Middleware  # noqa: E402
from starlette.middleware.authentication import AuthenticationMiddleware  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app.api import userdocs_research as api  # noqa: E402
from app.userdocs.research import types as T  # noqa: E402
from app.userdocs.research.errors import JobNotFound, ResearchBusy, ResearchUnavailable  # noqa: E402

CAP = 30.0


class FakeJobs:
    """The jobs contract, in memory: jobs keyed by id, each owned by a profile."""

    def __init__(self) -> None:
        self.views: dict[str, T.JobView] = {}
        self.calls: list[tuple] = []
        self.collected: list[tuple[str, str]] = []
        self.raise_on_start: Exception | None = None
        # The status a job has once wait_job returns (None: unchanged).
        self.after_wait: str | None = None

    def add(self, job_id: str, profile: str = "alice", status: str = T.RUNNING, **kw) -> T.JobView:
        dossier = kw.pop("dossier", None) or T.Dossier(
            job_id=job_id, mode=T.MODE_ANALYZE, domain=T.DOMAIN_GENERAL, question="q", status=status)
        view = T.JobView(job_id=job_id, profile=profile, status=status, mode=T.MODE_ANALYZE,
                         domain=T.DOMAIN_GENERAL, question="q", budget=250000, dossier=dossier, **kw)
        self.views[job_id] = view
        return view

    def _own(self, profile: str, job_id: str) -> T.JobView:
        view = self.views.get(job_id)
        if view is None or view.profile != profile:
            raise JobNotFound(f"No research job {job_id!r}.")
        return view

    def wait_cap(self) -> float:
        return CAP

    async def start_job(self, *, profile, spec, conversation_id, run_id=None, variables=None):
        self.calls.append(("start", profile, spec, conversation_id, variables))
        if self.raise_on_start is not None:
            raise self.raise_on_start
        view = self.add("job1", profile, T.QUEUED)
        view.mode, view.domain, view.question = spec.mode, spec.domain, spec.question
        return view

    async def wait_job(self, *, profile, job_id, timeout):
        self.calls.append(("wait", profile, job_id, timeout))
        view = self._own(profile, job_id)
        if self.after_wait:
            view = replace(view, status=self.after_wait)
            self.views[job_id] = view
        return view

    async def continue_job(self, *, profile, job_id, answers=None, variables=None):
        self.calls.append(("continue", profile, job_id, answers, variables))
        view = replace(self._own(profile, job_id), status=T.RUNNING)
        self.views[job_id] = view
        return view

    async def cancel_job(self, *, profile, job_id):
        self.calls.append(("cancel", profile, job_id))
        view = replace(self._own(profile, job_id), status=T.CANCELLED)
        self.views[job_id] = view
        return view

    def get_job(self, profile, job_id):
        self.calls.append(("get", profile, job_id))
        return self._own(profile, job_id)

    def list_jobs(self, profile, *, limit=20, conversation_id=None):
        self.calls.append(("list", profile, limit))
        return [v for v in self.views.values() if v.profile == profile][:limit]

    def mark_collected(self, profile, job_id):
        self.collected.append((profile, job_id))

    def named(self, name: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == name]


class FakeRender:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def render_job(self, view, *, ctx, db, page=1, leaf="research"):
        self.calls.append({"job": view.job_id, "status": view.status, "page": page, "limit": ctx.limit, "db": db})
        return SimpleNamespace(text=f"[research] job {view.job_id} · {view.status} · page {page}",
                               citations=[], files=[], data={})

    def dossier_pages(self, view):
        return ["summary", "findings", "coverage"]


class _HeaderAuth(AuthenticationBackend):
    async def authenticate(self, conn):
        user = conn.headers.get("x-user")
        if not user:
            return None
        return AuthCredentials(["authenticated"]), SimpleUser(user)


@pytest.fixture
def env(monkeypatch):
    jobs, render = FakeJobs(), FakeRender()
    monkeypatch.setattr(api, "_jobs", lambda: jobs)
    monkeypatch.setattr(api, "_render_mod", lambda: render)
    app = Starlette(routes=api.get_userdocs_research_routes(),
                    middleware=[Middleware(AuthenticationMiddleware, backend=_HeaderAuth())])
    client = TestClient(app)
    return SimpleNamespace(jobs=jobs, render=render, client=client)


def _as(user: str) -> dict:
    return {"x-user": user}


# ── start ──────────────────────────────────────────────────────────────────


def test_start_is_a_202_for_the_callers_profile_with_no_conversation(env):
    resp = env.client.post("/api/userdocs/research", headers=_as("alice"), json={
        "question": "  Compile the business results  ", "mode": "COMPILE", "domain": "financial",
        "scope": {"folder": ["MKT-report"]}, "profile": "bob",
    })
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["job"]["job_id"] == "job1" and body["job"]["status"] == T.QUEUED
    assert body["text"] == "[research] job job1 · queued · page 1"
    [(_, profile, spec, conversation_id, variables)] = env.jobs.named("start")
    # A "profile" in the body is never the target; REST jobs have no conversation.
    assert profile == "alice" and conversation_id is None
    assert spec.question == "Compile the business results"
    assert (spec.mode, spec.domain) == (T.MODE_COMPILE, T.DOMAIN_FINANCIAL)
    assert spec.scope == {"folder": ["MKT-report"]} and spec.reference_scope is None
    # None: the runner reads the profile's saved budget and model group itself.
    assert variables is None
    # No wait asked: none done, and a queued job is not a delivered state.
    assert env.jobs.named("wait") == [] and env.jobs.collected == []


def test_start_defaults_to_analyze_general(env):
    resp = env.client.post("/api/userdocs/research", headers=_as("alice"), json={"question": "Why?"})
    assert resp.status_code == 202
    spec = env.jobs.named("start")[0][2]
    assert (spec.mode, spec.domain, spec.scope) == (T.MODE_ANALYZE, T.DOMAIN_GENERAL, None)


@pytest.mark.parametrize("body", [
    {},
    {"question": "   "},
    {"question": 42},
    {"question": "x" * (api.MAX_QUESTION_CHARS + 1)},
    {"question": "q", "mode": "summarize"},
    {"question": "q", "domain": "medical"},
    {"question": "q", "wait": "soon"},
    {"question": "q", "wait": True},
])
def test_a_bad_body_is_a_400_and_starts_nothing(env, body):
    resp = env.client.post("/api/userdocs/research", headers=_as("alice"), json=body)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "InvalidRequest"
    assert env.jobs.named("start") == []


@pytest.mark.parametrize("field,value", [
    ("scope", {"folder": ["ABC"], "colour": "red"}),
    ("scope", ["ABC"]),
    ("reference_scope", {"date_from": "yesterday"}),
    ("reference_scope", {"types": ["law"]}),
])
def test_a_bad_scope_is_an_invalid_filter_naming_the_scope(env, field, value):
    resp = env.client.post("/api/userdocs/research", headers=_as("alice"), json={"question": "q", field: value})
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"] == "InvalidFilter" and body["message"].startswith(f"{field}:")
    assert env.jobs.named("start") == []


def test_busy_is_a_409_that_names_the_running_job(env):
    env.jobs.raise_on_start = ResearchBusy("abc123", "the earlier question")
    resp = env.client.post("/api/userdocs/research", headers=_as("alice"), json={"question": "q"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "ResearchBusy"
    assert body["job_id"] == "abc123" and body["question"] == "the earlier question"


def test_unavailable_is_a_503(env):
    env.jobs.raise_on_start = ResearchUnavailable("User Document Search is off.", status_code="disabled")
    resp = env.client.post("/api/userdocs/research", headers=_as("alice"), json={"question": "q"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "UserDocumentsUnavailable"


def test_wait_is_capped_and_a_finished_job_is_collected(env):
    env.jobs.after_wait = T.COMPLETE
    resp = env.client.post("/api/userdocs/research", headers=_as("alice"), json={"question": "q", "wait": 999})
    assert resp.status_code == 202
    assert env.jobs.named("wait") == [("wait", "alice", "job1", CAP)]
    assert resp.json()["job"]["status"] == T.COMPLETE
    assert env.jobs.collected == [("alice", "job1")]


def test_unauthenticated_is_a_401(env):
    assert env.client.post("/api/userdocs/research", json={"question": "q"}).status_code == 401
    assert env.client.get("/api/userdocs/research").status_code == 401
    assert env.jobs.calls == []


# ── get ────────────────────────────────────────────────────────────────────


def test_another_profiles_job_does_not_exist(env):
    env.jobs.add("j-alice", "alice", T.COMPLETE)
    for method, path in (("get", "/api/userdocs/research/j-alice"),
                         ("post", "/api/userdocs/research/j-alice/continue"),
                         ("post", "/api/userdocs/research/j-alice/cancel")):
        resp = getattr(env.client, method)(path, headers=_as("bob"), **({"json": {}} if method == "post" else {}))
        assert resp.status_code == 404, path
        assert resp.json()["error"] == "JobNotFound"
    assert env.jobs.collected == []
    assert env.jobs.views["j-alice"].status == T.COMPLETE  # bob's cancel touched nothing


def test_get_renders_the_page_whole_and_counts_the_pages(env):
    env.jobs.add("j1", "alice", T.COMPLETE)
    resp = env.client.get("/api/userdocs/research/j1?page=3", headers=_as("alice"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pages"] == 3 and body["text"].endswith("page 3")
    assert body["job"]["dossier"]["job_id"] == "j1"
    # Unbudgeted (a person reads it whole) and no index: nothing is issued.
    assert env.render.calls[-1]["limit"] is None and env.render.calls[-1]["db"] is None


@pytest.mark.parametrize("query,page", [("page=0", 1), ("page=-4", 1), ("", 1), ("page=2", 2)])
def test_page_is_clamped_to_at_least_one(env, query, page):
    env.jobs.add("j1", "alice", T.COMPLETE)
    assert env.client.get(f"/api/userdocs/research/j1?{query}", headers=_as("alice")).status_code == 200
    assert env.render.calls[-1]["page"] == page


def test_non_numeric_params_are_a_400(env):
    env.jobs.add("j1", "alice", T.RUNNING)
    for query in ("page=last", "wait=long", "page=nan", "wait=inf"):
        resp = env.client.get(f"/api/userdocs/research/j1?{query}", headers=_as("alice"))
        assert resp.status_code == 400 and resp.json()["error"] == "InvalidRequest"


@pytest.mark.parametrize("query,expected", [("wait=100", [CAP]), ("wait=12.5", [12.5]), ("wait=-3", []),
                                             ("wait=0", []), ("", [])])
def test_wait_on_a_running_job_is_clamped(env, query, expected):
    env.jobs.add("j1", "alice", T.RUNNING)
    env.client.get(f"/api/userdocs/research/j1?{query}", headers=_as("alice"))
    assert [c[3] for c in env.jobs.named("wait")] == expected


def test_a_finished_job_is_never_waited_on(env):
    env.jobs.add("j1", "alice", T.PARTIAL)
    env.client.get("/api/userdocs/research/j1?wait=20", headers=_as("alice"))
    assert env.jobs.named("wait") == []


@pytest.mark.parametrize("status", sorted(T.ACTIVE | T.WAITING | T.FINAL))
def test_delivery_is_claimed_only_for_a_deliverable_state(env, status):
    env.jobs.add("j1", "alice", status)
    resp = env.client.get("/api/userdocs/research/j1", headers=_as("alice"))
    assert resp.status_code == 200
    deliverable = status in T.WAITING or status in T.FINAL
    assert env.jobs.collected == ([("alice", "j1")] if deliverable else [])


def test_looking_at_a_chat_job_from_the_terminal_does_not_steal_its_delivery(env):
    """A job the agent started in a conversation owes that conversation its
    result; ``cremind userdocs research status`` on it must not claim it."""
    env.jobs.add("j1", "alice", T.COMPLETE, conversation_id="conv-1")
    resp = env.client.get("/api/userdocs/research/j1", headers=_as("alice"))
    assert resp.status_code == 200
    assert env.jobs.collected == []


def test_a_long_job_id_is_not_found_without_a_lookup(env):
    resp = env.client.get("/api/userdocs/research/" + "a" * 100, headers=_as("alice"))
    assert resp.status_code == 404
    assert env.jobs.named("get") == []


# ── list ───────────────────────────────────────────────────────────────────


def test_list_is_the_callers_jobs_without_dossiers(env):
    env.jobs.add("a1", "alice", T.COMPLETE)
    env.jobs.add("a2", "alice", T.RUNNING)
    env.jobs.add("b1", "bob", T.COMPLETE)
    resp = env.client.get("/api/userdocs/research?limit=500", headers=_as("alice"))
    assert resp.status_code == 200
    jobs = resp.json()["jobs"]
    assert [j["job_id"] for j in jobs] == ["a1", "a2"]
    assert all("dossier" not in j for j in jobs)
    assert env.jobs.named("list") == [("list", "alice", api.MAX_LIST_LIMIT)]
    # A list shows no result, so it claims no delivery.
    assert env.jobs.collected == []


def test_list_limit_defaults_and_rejects_words(env):
    env.client.get("/api/userdocs/research", headers=_as("alice"))
    assert env.jobs.named("list")[-1][2] == 20
    assert env.client.get("/api/userdocs/research?limit=all", headers=_as("alice")).status_code == 400


# ── continue / cancel ──────────────────────────────────────────────────────


def test_continue_passes_the_answers_and_waits_while_running(env):
    env.jobs.add("j1", "alice", T.NEEDS_CLARIFICATION)
    env.jobs.after_wait = T.COMPLETE
    resp = env.client.post("/api/userdocs/research/j1/continue", headers=_as("alice"), json={
        "answers": {"edition": "k7m2xq9a", "confirm": True, "rank": 2}, "wait": 10,
    })
    assert resp.status_code == 200, resp.text
    [(_, profile, job_id, answers, variables)] = env.jobs.named("continue")
    assert (profile, job_id) == ("alice", "j1")
    assert answers == {"edition": "k7m2xq9a", "confirm": True, "rank": 2}
    assert variables is None
    assert [c[3] for c in env.jobs.named("wait")] == [10.0]
    assert resp.json()["job"]["status"] == T.COMPLETE and "text" in resp.json()
    assert env.jobs.collected == [("alice", "j1")]


def test_continue_without_answers_resumes(env):
    env.jobs.add("j1", "alice", T.INTERRUPTED)
    resp = env.client.post("/api/userdocs/research/j1/continue", headers=_as("alice"), json={})
    assert resp.status_code == 200
    assert env.jobs.named("continue")[0][3] is None
    # Still running and not waited on: nothing delivered yet.
    assert env.jobs.collected == []


@pytest.mark.parametrize("answers", ["edition=k7m2xq9a", ["k7m2xq9a"], {"edition": {"fid": "x"}},
                                     {"edition": None}, {"": "x"}, {f"k{i}": "v" for i in range(21)},
                                     {"note": "x" * 2001}])
def test_bad_answers_are_a_400(env, answers):
    env.jobs.add("j1", "alice", T.NEEDS_CLARIFICATION)
    resp = env.client.post("/api/userdocs/research/j1/continue", headers=_as("alice"), json={"answers": answers})
    assert resp.status_code == 400
    assert resp.json()["error"] == "InvalidRequest"
    assert env.jobs.named("continue") == []


def test_cancel_answers_the_cancelled_job_and_collects_it(env):
    env.jobs.add("j1", "alice", T.RUNNING)
    resp = env.client.post("/api/userdocs/research/j1/cancel", headers=_as("alice"))
    assert resp.status_code == 200
    assert resp.json()["job"]["status"] == T.CANCELLED
    assert "cancelled" in resp.json()["text"]
    assert env.jobs.collected == [("alice", "j1")]


# ── registration ───────────────────────────────────────────────────────────


def test_the_routes_and_their_registration_order():
    routes = {(r.path, m) for r in api.get_userdocs_research_routes() for m in (r.methods or ()) if m != "HEAD"}
    assert routes == {
        ("/api/userdocs/research", "POST"),
        ("/api/userdocs/research", "GET"),
        ("/api/userdocs/research/{job_id}", "GET"),
        ("/api/userdocs/research/{job_id}/continue", "POST"),
        ("/api/userdocs/research/{job_id}/cancel", "POST"),
    }
    import app.api as api_pkg

    source = inspect.getsource(api_pkg.get_api_routes)
    assert "get_userdocs_research_routes()" in source
    # Registered with the query routes, ahead of the settings/engine routes.
    assert source.index("get_userdocs_research_routes()") < source.index("get_userdocs_routes()")


@pytest.mark.parametrize("status", [T.RUNNING, T.NEEDS_CLARIFICATION, T.INTERRUPTED, T.COMPLETE, T.FAILED])
def test_the_real_renderer_serves_every_state(status):
    """No fakes: the route's render call against the real renderer."""
    clar = T.Clarification(kind="edition", question="Which edition of the Land Law?",
                           candidates=[{"fid": "k7m2xq9a", "title": "Luật Đất đai 2024"}],
                           answer_keys={"edition": "a fid from the candidates"})
    dossier = T.Dossier(
        job_id="j1", mode=T.MODE_ANALYZE, domain=T.DOMAIN_LEGAL, question="q?", status=status,
        coverage=[T.CoverageRow(fid="k7m2xq9a", rel_path="ABC/HopDong.pdf", kind="pdf", read=T.READ_FULL,
                                chunks_read=3, chunks_total=3)],
        clarification=clar if status == T.NEEDS_CLARIFICATION else None,
    )
    view = T.JobView(job_id="j1", profile="alice", status=status, mode=T.MODE_ANALYZE, domain=T.DOMAIN_LEGAL,
                     question="q?", budget=250000, dossier=dossier,
                     progress={"phase": "Reading files", "done": 1, "total": 3,
                               "steps": [{"id": "s1", "label": "Reading ABC/HopDong.pdf", "status": "running"}]})
    text, pages = api._render("alice", view, 1)
    assert "job j1" in text and status in text
    assert pages >= 1
    if status == T.NEEDS_CLARIFICATION:
        assert "Which edition of the Land Law?" in text


def test_a_job_gone_before_its_claim_still_answers(env, monkeypatch):
    env.jobs.add("j1", "alice", T.COMPLETE)

    def gone(profile, job_id):
        raise JobNotFound("No research job 'j1' for this profile.")

    monkeypatch.setattr(env.jobs, "mark_collected", gone)
    resp = env.client.get("/api/userdocs/research/j1", headers=_as("alice"))
    assert resp.status_code == 200
    assert resp.json()["job"]["status"] == T.COMPLETE
