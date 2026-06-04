"""YAML-frontmatter parser for documentation `.md` files.

Eligible files declare a frontmatter block delimited by ``---`` lines with a
non-empty ``description`` field, e.g.

```
---
description: "..."
---
<body>
```

Files without that exact shape are silently ineligible — they're skipped
during sync and never appear in the vector store.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from app.utils.logger import logger


@dataclass(frozen=True)
class ParsedDocument:
    description: str
    body: str


def parse_document(path: Path) -> Optional[ParsedDocument]:
    """Parse ``path`` and return ``ParsedDocument`` or None if ineligible.

    Returns None when:
    - the file cannot be read,
    - the file lacks a ``---`` frontmatter delimiter pair,
    - the frontmatter is not a YAML mapping,
    - the ``description`` field is missing or empty.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.debug(f"[documents] unreadable: {path} ({e})")
        return None

    description, body = _split_frontmatter(raw)
    if description is None:
        return None

    return ParsedDocument(description=description, body=body)


def _split_frontmatter(raw: str) -> tuple[Optional[str], str]:
    """Return ``(description, body)`` if frontmatter is valid; else ``(None, "")``.

    Body excludes the frontmatter block and any single newline immediately
    after the closing ``---``.
    """
    # Strip a leading BOM but preserve trailing whitespace inside the body.
    text = raw.lstrip("﻿")

    # The first non-empty line must be exactly ``---``.
    lines = text.splitlines(keepends=True)
    idx = 0
    while idx < len(lines) and lines[idx].strip() == "":
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        return None, ""

    start = idx + 1
    end = None
    for j in range(start, len(lines)):
        if lines[j].strip() == "---":
            end = j
            break
    if end is None:
        return None, ""

    fm_text = "".join(lines[start:end])
    try:
        meta = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return None, ""

    if not isinstance(meta, dict):
        return None, ""

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, ""

    body = "".join(lines[end + 1:])
    # Drop a single leading newline after the closing ``---`` so callers see
    # the body verbatim without an awkward blank prefix.
    if body.startswith("\n"):
        body = body[1:]

    return description.strip(), body
