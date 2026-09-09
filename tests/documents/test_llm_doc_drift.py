"""Doc/code drift pin for `[cli]cremind llm.md`.

Two things this doc said that the code under it had stopped doing, both of
which cost a user a working Codex sign-in.

First, the Kubernetes fix for "sign-in just waits forever" was a hard-coded
``kubectl -n <ns> port-forward svc/cremind 1515:80 1455:1455``. That literal was
wrong twice over: the namespace was a blank the reader had to fill in, and
``cremind`` is only the Service name when the Helm release happens to be called
that (the chart names the Service after the release), so pasting it returns
``services "cremind" not found`` with nothing saying the command - not the
cluster - was at fault. ``app.api.llm_codex_flow._kubernetes_hint`` now renders
the pod's own namespace and Service through
:func:`app.config.runtime_env.kubernetes_port_forward`, falling back to the
HTTPS runbook's placeholders only when the chart is too old to state them, so
the doc must describe that rather than reprint the retired literal.

Second, "Sign in with ChatGPT" now names two different logins. The one this doc
documents is the OpenAI *provider's* - tokens in ``llm_config``
(``openai.oauth_*``) for this profile. The Codex coding delegate has its own,
in that profile's ``CODEX_HOME``, and the bridge that let this login serve that
tool (credential source ``profile_chatgpt_login``) has been removed. Only the
frontmatter ``description`` is embedded into the vector store, so the
distinction has to live there too: a "sign in to Codex" query that lands here
otherwise walks the user through the wrong sign-in entirely.
"""

from __future__ import annotations

from pathlib import Path

DOC = (
    Path(__file__).resolve().parents[2]
    / "app" / "documents" / "bundled" / "[cli]cremind llm.md"
)


def _doc_text() -> str:
    assert DOC.exists(), f"missing bundled doc: {DOC.name}"
    return DOC.read_text(encoding="utf-8")


def _description() -> str:
    text = _doc_text().lstrip()
    end = text.find("---", 3)
    assert end != -1, "frontmatter is not closed"
    return text[3:end]


def test_no_retired_hard_coded_port_forward_survives_anywhere():
    """The old literal is a command that fails for everyone but one release."""
    text = _doc_text()
    for retired in ("svc/cremind", "-n <ns>"):
        assert retired not in text, (
            f"{retired!r} is the retired hard-coded hint: the namespace is a "
            "blank and the Service is named after the Helm release, so a reader "
            'who pastes it gets \'services "cremind" not found\''
        )


def test_the_doc_prints_the_line_the_pod_itself_renders():
    """Same builder as the hint, so the doc cannot drift from what users see."""
    from app.config import runtime_env

    text = _doc_text()
    fallback = (
        runtime_env.kubernetes_port_forward(
            "<namespace>", "<release>", runtime_env.PORT_FORWARD_LOCAL_PORT, 80
        )
        + " 1455:1455"
    )
    assert fallback in text, (
        f"the doc must show the older-chart fallback exactly as rendered: {fallback!r}"
    )
    assert "older chart" in text, (
        "the placeholder form must be labelled as the fallback, or the reader "
        "reads it as the command everybody gets"
    )
    for expected in ("kubectl --namespace", "<namespace>", "<release>"):
        assert expected in text, f"the rendered command never shows {expected!r}"


def test_the_doc_says_cremind_fills_the_two_names_in():
    """The point of the change: the reader copies, they do not compose."""
    text = _doc_text()
    assert "capture_hint" in text
    assert "namespace and Service" in text, (
        "the doc must say which two names Cremind resolves from the pod"
    )


def test_this_chatgpt_login_is_told_apart_from_the_codex_tools():
    """Both flows are called 'Sign in with ChatGPT'; only one is this doc's."""
    text = _doc_text()
    description = _description()
    assert "cremind tools coding-agents login codex" in description, (
        "only the description is embedded, so the pointer to the coding "
        "delegate's own sign-in has to be in it"
    )
    assert "cremind tools coding-agents login codex" in text
    assert "llm_config" in text, (
        "where this login is stored (llm_config, not a CODEX_HOME) is the fact "
        "that separates it from the Codex tool's"
    )
