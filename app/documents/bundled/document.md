---
description: "How to write an Cremind documentation file: the required YAML frontmatter shape, how the `description` field drives retrieval in `documentation_search`, body conventions, and how to verify the document is indexed."
---

# Writing an Cremind Document

An *Cremind document* is a Markdown (`.md`) file that lives under one of the
two watched documentation roots:

- Shared: `<CREMIND_WORKING_DIR>/documents/*.md`
- Per-profile: `<CREMIND_WORKING_DIR>/<profile>/documents/*.md`

When a file appears in either tree, the documents watcher parses it and, if
it is valid, indexes its `description` into the Qdrant collection used by
the `documentation_search` built-in tool. The body is read from disk on
demand only after a query matches the description.

## File format

Every document must begin with a YAML frontmatter block. The parser is
strict — files that don't match this exact shape are silently skipped and
will never appear in search results.

Rules:

- The first non-empty line of the file must be exactly `---`.
- A YAML mapping (key/value pairs) follows.
- The frontmatter is closed by another `---` on its own line.
- The mapping must contain a `description` key whose value is a non-empty
  string. No other keys are required.
- Everything after the closing `---` is the body, in plain Markdown.

Skeleton:

```markdown
---
description: "<one-sentence summary of the whole file>"
---

# <Document Title>

<body...>
```

## The `description` field

This is the single most important field in the file. It is the **only**
text that gets embedded into the vector store — the body is not indexed.
Retrieval ranking for `documentation_search` is driven entirely by how
well a query matches the description.

Write the description as a **concise summary of the entire file's
content**: a single sentence (or short paragraph) that names the topic
and the scenarios in which the document is useful.

Guidelines:

- Front-load topic keywords. The description is matched semantically, but
  concrete nouns help (e.g. tool names, command names, artifact names).
- Name the *scenario*, not just the topic — "How to configure X when Y"
  beats "About X".
- Avoid vague phrases like "documentation about ..." or "notes on ...".
- Quote the value (`description: "..."`) so colons, hashes, and other YAML
  special characters parse cleanly.
- Keep it to one sentence when possible. If two sentences are needed,
  separate them with a period and a space inside the same quoted string.

Examples:

```yaml
description: "Step-by-step guide for writing a new Cremind skill, including the SKILL.md frontmatter format, the skill directory layout, and how to register the skill so the Reasoning Agent can discover it."
```

```yaml
description: "How to configure the `exec_shell` built-in tool to run long-running background apps, including the `metadata.long_running_app.command` key and autostart behavior."
```

## Body conventions

The body is shown to the agent only after retrieval, so write it for a
reader who already knows roughly why they're here.

- Use standard Markdown headings (`##` for top-level sections, `###` for
  subsections). Reserve a single `#` for the document title.
- Keep sections focused. Prefer concrete examples and code fences over
  long prose.
- Use fenced code blocks with language tags so syntax highlighting works:
  ` ```python `, ` ```bash `, ` ```yaml `, ` ```markdown `.
- Cross-reference other documents or skills by relative path.
- Don't repeat the description verbatim in the body; the body is for
  detail, the description is for discovery.

## Minimal example

The smallest valid document:

```markdown
---
description: "How to configure the foo widget for the bar workflow."
---

# Configuring the Foo Widget

To enable the foo widget in the bar workflow, set `foo.enabled = true`
in the profile config and restart the server.
```

## Verification

After saving a new document:

1. Place the file directly under one of the watched roots — `documents/`
   for shared docs, or `<profile>/documents/` for per-profile docs. It
   must not be nested in a subdirectory.
2. Wait ~1 second for the watcher's debounce to pick up the change. No
   server restart is required.
3. Call the `documentation_search` built-in tool with a query whose
   keywords appear in your description; the new file should be in the
   results.

If the file doesn't appear, the most common causes are:

1. Malformed frontmatter — confirm that the very first non-empty line is
   exactly `---` and that there is a matching closing `---`.
2. Missing or empty `description` — the parser silently rejects files
   without a non-empty description string.
3. Wrong location — the file must be directly under a `documents/`
   folder, not in a nested subdirectory.
4. YAML parse error — if the description contains colons, hashes, or
   other special characters, wrap the value in double quotes.
