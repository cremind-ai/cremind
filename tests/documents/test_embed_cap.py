"""The embedded text is capped, and an oversized description is reported.

Only ``<identity>\\n\\n<description>`` is embedded, and the default model
(multilingual-e5-base) silently truncates at 512 tokens. A 5,000-character
description therefore looked comprehensive to its author while most of it never
reached the ranker. The cap makes that cut explicit and logged. The same cap
bounds what the relevance judge is shown (tested with the tool), so the
authoring rule is one number.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from app.documents import sync as sync_module
from app.documents.sync import (
    DESCRIPTION_MAX_CHARS,
    DocumentSyncService,
    _embedding_text,
)
from app.utils.logger import logger

BUNDLED = Path(__file__).resolve().parents[2] / "app" / "documents" / "bundled"


def test_short_description_is_embedded_unchanged():
    assert _embedding_text("[cli]cremind profile", "Short.") == "cremind profile\n\nShort."


def test_long_description_is_cut_on_a_word_boundary():
    words = " ".join(f"word{i}" for i in range(2000))
    text = _embedding_text("[cli]cremind channels", words)

    identity, _, desc = text.partition("\n\n")
    assert identity == "cremind channels"
    assert len(desc) <= DESCRIPTION_MAX_CHARS
    assert words.startswith(desc)
    # Never a half word: the cut lands right before a separator.
    assert words[len(desc)] == " "


def test_description_without_spaces_is_hard_sliced():
    blob = "x" * (DESCRIPTION_MAX_CHARS + 50)
    assert _embedding_text("", blob) == "x" * DESCRIPTION_MAX_CHARS


def test_payload_keeps_the_full_description_only_the_vector_text_is_capped(tmp_path):
    """The stored payload is the source of truth; the cap is applied where the
    text is consumed (the embedder here, the judge prompt in the tool)."""
    long_desc = " ".join(f"keyword{i}" for i in range(600))
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "big.md").write_text(f'---\ndescription: "{long_desc}"\n---\n\nbody\n', encoding="utf-8")
    svc = DocumentSyncService(working_dir=tmp_path)

    (payload,) = svc._scan_scope("shared").values()

    assert payload["text"] == long_desc
    assert len(payload["_embed_text"]) < len(long_desc)


def test_oversized_description_warns_once_per_document(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_module, "_OVERSIZED_DESCRIPTIONS_WARNED", set())
    long_desc = "z " * DESCRIPTION_MAX_CHARS
    docs = tmp_path / "documents"
    docs.mkdir(parents=True)
    (docs / "big.md").write_text(f'---\ndescription: "{long_desc.strip()}"\n---\n\nbody\n', encoding="utf-8")
    svc = DocumentSyncService(working_dir=tmp_path)

    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    try:
        svc._scan_scope("shared")
        svc._scan_scope("shared")
    finally:
        logger.remove(sink_id)

    warnings = [m for m in messages if "only the first" in m and "big.md" in m]
    assert len(warnings) == 1


def _bundled_descriptions() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(BUNDLED.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\r?\n(.*?)\r?\n---\r?\n", text, re.DOTALL)
        assert match, f"{path.name}: no frontmatter"
        out[path.stem] = (yaml.safe_load(match.group(1)) or {}).get("description") or ""
    return out


def test_every_bundled_description_fits_the_embedding_window():
    """Ratchet: a bundled description that outgrows the cap is embedded only
    partially and would log a warning on every boot. Trim it instead — see the
    description guidance in document.md."""
    oversized = {
        stem: len(desc)
        for stem, desc in _bundled_descriptions().items()
        if len(desc) > DESCRIPTION_MAX_CHARS
    }
    assert not oversized, (
        f"bundled descriptions over {DESCRIPTION_MAX_CHARS} chars: {oversized}"
    )
