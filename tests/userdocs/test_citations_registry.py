"""The citation registry and finalization: what a saved answer's tokens are worth.

The registry exists so that a citation chip means "a tool really showed the
agent this passage". What is pinned here:

- **all six statuses**, each from the situation that produces it;
- **quotes** before a token are checked against the cited chunk — numbers and
  diacritics included, the two changes a fuzzy match must never absorb;
- **tolerant parsing, strict verification**: a sloppily copied token still
  resolves, and numbering is by first appearance;
- **profile isolation**: a token resolves only in the registry and index of
  the profile that was issued it — bob citing alice's token gets "invalid";
- **issue() never raises into a tool**, re-issuing is idempotent, and the
  tool's context id finds the conversation (web and channel alike);
- the A2A path, whose first message has no conversation yet, binds its
  registrations at finalization;
- purge set P deletes the registry.
"""

from __future__ import annotations

import shutil

import pytest

pytest.importorskip("a2a")

from sqlalchemy import text  # noqa: E402

from app.userdocs import citations as cit  # noqa: E402
from app.userdocs.cite import IssuedCitation  # noqa: E402
from app.userdocs.index import index_dir  # noqa: E402

from ._citations_env import LAW_1, LAW_2, build, close  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = build(tmp_path, monkeypatch)
    cit._pending.clear()
    yield e
    cit._pending.clear()
    close(e)


def _item(meta, token):
    return next(i for i in meta["items"] if i["token"] == token)


def test_an_answer_without_citations_costs_nothing(env):
    assert cit.finalize_citations("alice", "c-web", "No documents were needed.") is None
    assert cit.finalize_citations("alice", "c-web", "") is None


def test_all_six_statuses(env):
    law, c1, c2 = env.law, env.law_chunks[0], env.law_chunks[1]
    note, n1 = env.note, env.note_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1), env.issued(law, c2)])
    cit.issue("alice", "c-other", [env.issued(note, n1)])

    # A token whose chunk has since changed: issued here, gone from the index.
    stale = "[ud:" + law["cite_id"] + "#deadbeef]"
    cit.issue("alice", "c-web", [IssuedCitation(
        token=stale, cite_id=law["cite_id"], ref_id=law["id"], text_hash="deadbeef" + "0" * 24,
        locator={"page": 9}, label="p. 9", rel_path=law["rel_path"], snippet="the old wording",
        leaf="search",
    )])
    # A file that was issued and then deleted from the index.
    gone = env.db.insert_file("local", "Old/gone.txt", "h-gone", name="gone.txt", kind="text", status="indexed")
    gone_token = env.token(gone)
    cit.issue("alice", "c-web", [env.issued(gone)])
    env.db.delete_file(gone["id"])

    answer = (
        f"Courts decide {env.token(law, c1)}. Mediation first {env.token(law, c2)}. "
        f"Q3 revenue grew {env.token(note, n1)}. The file itself {env.token(law)}. "
        f"Old wording {stale}. Removed {gone_token}. Invented [ud:zzzzzzzz#00000000]."
    )
    meta = cit.finalize_citations("alice", "c-web", answer)

    assert meta["v"] == 1
    statuses = {i["token"]: i["status"] for i in meta["items"]}
    assert statuses[env.token(law, c1)] == "verified"
    assert statuses[env.token(law, c2)] == "verified"
    assert statuses[env.token(note, n1)] == "verified_elsewhere"
    assert statuses[env.token(law)] == "unissued"  # resolves, but no tool printed it
    assert statuses[stale] == "stale"
    assert statuses[gone_token] == "removed"
    assert statuses["[ud:zzzzzzzz#00000000]"] == "invalid"
    assert meta["unverified"] == 4

    # Numbered by first appearance, the numbering every renderer shares.
    assert [i["n"] for i in meta["items"]] == [1, 2, 3, 4, 5, 6, 7]
    # A stale citation shows the snapshot taken when it was issued.
    s = _item(meta, stale)
    assert s["snippet"] == "the old wording" and s["locator_label"] == "p. 9"
    assert s["file"]["name"] == "luat-dat-dai.pdf"
    # A verified one carries what the viewer needs.
    v = _item(meta, env.token(law, c1))
    assert v["file"] == {
        "fid": law["cite_id"], "name": "luat-dat-dai.pdf", "rel_path": "Luat/luat-dat-dai.pdf",
        "source": "local", "kind": "pdf", "web_link": None,
    }
    assert v["locator"] == {"page": 1} and v["locator_label"] == "p. 1"
    assert v["snippet"].startswith("Điều 203")
    r = _item(meta, gone_token)
    assert r["file"]["name"] == "gone.txt"


def test_a_chunk_token_on_a_live_file_with_an_invented_hash_is_invalid(env):
    law = env.law
    meta = cit.finalize_citations("alice", "c-web", f"See [ud:{law['cite_id']}#0badc0de].")
    assert meta["items"][0]["status"] == "invalid"


def test_folder_tokens_resolve(env):
    folder = env.folder
    token = f"[ud:{folder['cite_id']}]"
    cit.issue("alice", "c-web", [env.issued(folder, target="folder", leaf="find_files")])
    meta = cit.finalize_citations("alice", "c-web", f"The Luat folder {token}.")
    item = meta["items"][0]
    assert item["status"] == "verified"
    assert item["file"]["kind"] == "folder" and item["file"]["name"] == "Luat"


def test_quote_statuses(env):
    law, c1, c2 = env.law, env.law_chunks[0], env.law_chunks[1]
    cit.issue("alice", "c-web", [env.issued(law, c1), env.issued(law, c2)])
    t1, t2 = env.token(law, c1), env.token(law, c2)

    def status(answer):
        return cit.finalize_citations("alice", "c-web", answer)["items"][0]["quote_status"]

    exact = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do Tòa án nhân dân giải quyết"
    assert status(f"The law says “{exact}” {t1}.") == "exact"
    assert status(f'The law says "{exact.upper()}", {t1}.') == "normalized"
    # A dropped word is a paraphrase inside quotation marks — close enough.
    dropped = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận thì do Tòa án giải quyết"
    assert status(f"It says “{dropped}” {t1}.") == "fuzzy"
    # A changed number is not: "45/2013" became "45/2014".
    wrong_number = (
        "lựa chọn một trong hai hình thức giải quyết tranh chấp đất đai theo Luật số 45/2014/QH13"
    )
    assert status(f"It says “{wrong_number}” {t2}.") == "mismatch"
    # Nor is a diacritics-only change: "chứng nhận" (certificate) → "chứng nhân".
    wrong_marks = "Tranh chấp đất đai mà đương sự có Giấy chứng nhân thì do Tòa án nhân dân giải quyết"
    assert status(f"It says «{wrong_marks}» {t1}.") == "mismatch"
    # A quote running into the next chunk is checked against both.
    across = LAW_1[-40:] + " " + LAW_2[:40]
    assert status(f"“{across}” {t1}") in ("exact", "normalized")
    # No quote (or one too short to check): no status.
    assert status(f"Courts decide {t1}.") is None
    assert status(f"“Tòa án” {t1}") is None

    meta = cit.finalize_citations("alice", "c-web", f"“{wrong_number}” {t2}")
    assert meta["unverified"] == 1  # a mismatched quote counts against the answer


def test_a_quote_before_a_run_of_tokens_is_checked_against_each(env):
    law, c1, c2 = env.law, env.law_chunks[0], env.law_chunks[1]
    cit.issue("alice", "c-web", [env.issued(law, c1), env.issued(law, c2)])
    quote = "Tranh chấp đất đai mà đương sự có Giấy chứng nhận"
    meta = cit.finalize_citations(
        "alice", "c-web", f"“{quote}” {env.token(law, c1)}{env.token(law, c2)}",
    )
    assert [i["quote_status"] for i in meta["items"]] == ["exact", "exact"]


def test_tolerant_parsing_strict_verification(env):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    canonical = env.token(law, c1)
    sloppy = canonical.replace("[ud:", "【ud: ").replace("]", "】").upper().replace("UD:", "ud:")
    meta = cit.finalize_citations("alice", "c-web", f"First {sloppy} then {canonical} again.")
    assert len(meta["items"]) == 1
    assert meta["items"][0]["token"] == canonical
    assert meta["items"][0]["status"] == "verified"

    # Several tokens in one bracket, numbered in order of appearance.
    note, n1 = env.note, env.note_chunks[0]
    both = f"[{canonical[1:-1]}; {env.token(note, n1)[1:-1]}]"
    meta = cit.finalize_citations("alice", "c-web", f"Both {both}.")
    assert [(i["n"], i["token"]) for i in meta["items"]] == [(1, canonical), (2, env.token(note, n1))]

    # A token with a letter outside the cite alphabet parses, and is invalid.
    meta = cit.finalize_citations("alice", "c-web", "Bad [ud:iiiiiiii#00000000].")
    assert meta["items"][0]["status"] == "invalid"


def test_the_registry_is_profile_scoped(env):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    token = env.token(law, c1)
    # bob has no index and no registration: alice's token means nothing to him.
    meta = cit.finalize_citations("bob", "c-bob", f"Stolen {token}.")
    assert meta["items"][0]["status"] == "invalid"
    assert meta["items"][0]["file"] is None
    # Resolving through the API helper stays inside the caller's profile too.
    assert cit.resolve_tokens("bob", None, [token])[token]["status"] == "invalid"
    # A tool of bob's cannot register into alice's conversation: the context
    # id is looked up within bob's own conversations.
    cit.issue("bob", "c-web", [env.issued(law, c1)])
    assert env.cit.count("bob") == 1
    assert env.cit.count("bob", "c-web") == 0
    assert env.cit.count("alice") == 1
    # And even a registration of bob's never makes alice's passage verifiable
    # for him: the chunk must exist in *his* index.
    status = cit.resolve_tokens("bob", None, [token])[token]["status"]
    assert status not in cit.TRUSTED


def test_issue_is_idempotent_and_finds_the_conversation(env):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1), env.issued(law, c1)])
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    assert env.cit.count("alice", "c-web") == 1
    # A channel conversation: the tool's context id is the platform's chat id.
    cit.issue("alice", "tg:12345", [env.issued(law, c1)])
    assert env.cit.count("alice", "c-chan") == 1
    meta = cit.finalize_citations("alice", "c-chan", f"x {env.token(law, c1)}")
    assert meta["items"][0]["status"] == "verified"


def test_issue_never_raises_into_the_tool(env, monkeypatch):
    import app.storage.userdocs_citations_storage as cit_storage

    class Broken:
        def resolve_conversation(self, *a, **k):
            raise RuntimeError("database is gone")

    monkeypatch.setattr(cit_storage, "_instance", Broken())
    cit.issue("alice", "c-web", [env.issued(env.law, env.law_chunks[0])])  # no exception


def test_non_canonical_tokens_are_not_registered(env):
    law = env.law
    cit.issue("alice", "c-web", [IssuedCitation(token="[ud:NOT-A-TOKEN]", cite_id=law["cite_id"])])
    cit.issue("alice", "c-web", [IssuedCitation(token=f"[ud:{law['cite_id']}]", cite_id="00000000")])
    assert env.cit.count("alice") == 0


def test_the_a2a_path_binds_registrations_made_before_the_conversation(env):
    law, c1 = env.law, env.law_chunks[0]
    # The first A2A message: its tools run before the conversation row exists.
    cit.issue("alice", "a2a-ctx-new", [env.issued(law, c1)])
    assert env.cit.count("alice") == 1 and env.cit.count("alice", "c-other") == 0
    meta = cit.finalize_citations(
        "alice", "c-other", f"x {env.token(law, c1)}", context_id="a2a-ctx-new",
    )
    assert meta["items"][0]["status"] == "verified"
    assert env.cit.count("alice", "c-other") == 1


def test_finalization_failure_saves_the_message_without_citations(env, monkeypatch):
    monkeypatch.setattr(cit, "_resolve", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert cit.finalize_citations("alice", "c-web", "x [ud:zzzzzzzz]") is None


def test_no_index_means_issued_tokens_are_removed(env):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    token = env.token(law, c1)
    # The index is deleted (purge P) but the registry row is still there.
    env.svc.runtimes.pop("alice")
    env.db.close()
    shutil.rmtree(index_dir("uid-alice"))
    meta = cit.finalize_citations("alice", "c-web", f"x {token}")
    assert meta["items"][0]["status"] == "removed"
    assert meta["items"][0]["snippet"].startswith("Điều 203")


def test_purge_deletes_the_registry(env, monkeypatch):
    law, c1 = env.law, env.law_chunks[0]
    cit.issue("alice", "c-web", [env.issued(law, c1)])
    cit.issue("bob", "c-bob", [env.issued(law, c1)])
    assert cit.purge_profile("alice") == 1
    assert env.cit.count("alice") == 0
    assert env.cit.count("bob") == 1  # another profile's registry is untouched


def test_clean_profile_deletes_the_registry(env, monkeypatch):
    from app.userdocs import service as svc_module

    cit.issue("alice", "c-web", [env.issued(env.law, env.law_chunks[0])])
    env.svc.runtimes.pop("alice").db.close()
    monkeypatch.setattr(svc_module, "_service", None)
    svc_module.clean_profile("alice")
    assert env.cit.count("alice") == 0


def test_cascades_with_the_conversation(env):
    cit.issue("alice", "c-web", [env.issued(env.law, env.law_chunks[0])])
    cit.issue("alice", "c-other", [env.issued(env.law, env.law_chunks[0])])
    with env.provider.sync_engine().begin() as c:
        c.execute(text("DELETE FROM conversations WHERE id='c-web'"))
    assert env.cit.count("alice", "c-web") == 0
    assert env.cit.count("alice") == 1


def test_a_held_drive_citation_shows_its_snapshot_not_the_live_index(env):
    """After Google access was revoked (or the token vanished), Drive is held
    and hidden from search; a citation into it must not read the live index
    either — it shows what was cited, marked stale, until Google is re-linked."""
    from app.userdocs.cite import make_token

    db = env.db
    doc = db.insert_file("drive", "Drive/Hop dong.md", "h-drive", name="Hop dong.md", kind="markdown",
                         status="indexed", drive_file_id="1AbC", drive_web_link="https://drive.google.com/x")
    chunks = [c for c in env.note_chunks]
    from app.userdocs.types import ChunkDiff

    db.apply_chunks(file_id=doc["id"], folder_id=None, source="drive", diff=ChunkDiff(add=chunks))
    tok = make_token(doc["cite_id"], chunks[0].text_hash)
    cit.issue("alice", "c-web", [IssuedCitation(
        token=tok, cite_id=doc["cite_id"], ref_id=doc["id"], text_hash=chunks[0].text_hash,
        source_kind="drive", rel_path=doc["rel_path"], snippet="what was cited", leaf="search")])

    live = cit.resolve_tokens("alice", "c-web", [tok])[tok]
    assert live["status"] == "verified"

    db.update_source_state("drive", state="hold", reason="auth_revoked")
    held = cit.resolve_tokens("alice", "c-web", [tok])[tok]
    assert held["status"] == "stale"
    assert held["snippet"] == "what was cited"

    db.update_source_state("drive", state="hold", reason="drive_unreachable")  # stale, not hidden
    assert cit.resolve_tokens("alice", "c-web", [tok])[tok]["status"] == "verified"
