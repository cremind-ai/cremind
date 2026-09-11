---
description: "How to write a Cremind documentation file: the required YAML frontmatter shape, how the `description` field drives retrieval in `documentation_search` and its 1,200-character limit, body conventions including the size budget and self-contained sections for long documents, and how to verify the document is indexed."
---

# Writing an Cremind Document

An *Cremind document* is a Markdown (`.md`) file that lives under one of the
two watched documentation roots:

- Shared: `<CREMIND_WORKING_DIR>/documents/*.md`
- Per-profile: `<CREMIND_WORKING_DIR>/<profile>/documents/*.md`

When a file appears in either tree, the documents watcher parses it and, if
it is valid, indexes it into the vector store (Qdrant or Chroma) used by the
`documentation_search` built-in tool. What gets embedded is the file's
identity (its filename, with any leading `[tag]` stripped) followed by its
`description` — the body is *not* indexed. Retrieval then runs in two
stages: vector search ranks candidates by that embedded text, and an
internal LLM judge picks the single best match from the top candidates'
names + descriptions (with Vector Embedding off, the judge reviews every
document instead). The body is read from disk on demand only after the
judge picks a document.

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

This is the single most important field in the file. Together with the
file's name it is the **only** text embedded into the vector store — the
body is not indexed — and it is also what the internal LLM judge reads to
pick the winning document. Retrieval quality is driven almost entirely by
how *discriminating* this one string is.

Write it to be **distinctive, not merely thorough.** Two consumers read it:
the embedder (which rewards specific, front-loaded keywords) and the judge
(which needs enough scenario detail to tell this doc apart from its
neighbors). A two-sentence shape serves both:

1. **Recall sentence** — lead with the topic's *distinctive* verbs and
   object noun, plus the natural-language synonyms someone would actually
   search: "Create, list, and delete profiles — make, add, or register a
   new profile…".
2. **Disambiguation sentence** — name the scenario and, where docs are
   easily confused, say what this one is *distinct from*: "…distinct from
   `cremind chat` (the interactive REPL)."

Guidelines:

- **No shared boilerplate.** Do NOT open with a generic prefix that many
  docs repeat (e.g. "Complete reference for the `cremind X` CLI command —
  the terminal-side counterpart to the **Y** page in the web UI — covering
  how to…"). Identical prefixes pull every doc's embedding toward the same
  point and flatten the ranking. Put the UI-counterpart fact in the body.
- **Front-load distinctive keywords.** The first ~10 words decide most of
  the match; spend them on what makes this doc unique, not on filler.
- **Bind common words to a unique object.** A token like "create" or
  "profile" that appears across many docs doesn't discriminate on its own —
  tie it to this doc's unique object ("create a profile", "create a
  conversation", "create a schedule event").
- **Include user-intent synonyms** (create / make / add / register / new)
  so natural-language queries find lexical anchors.
- **Aim for roughly two sentences (~250–350 characters).** Don't enumerate
  every subcommand — list only the ones that disambiguate; the body carries
  the rest.
- **Never exceed 1,200 characters.** Only the first 1,200 characters of a
  description are used — for ranking *and* by the judge — so anything past
  that point does nothing, and the server logs a warning naming the file.
  (The embedding model truncates silently at its own window; this limit
  makes the cut explicit.) Bundled docs are held to it by a test.
- **Quote the value** (`description: "..."`) so colons, hashes, and other
  YAML special characters parse cleanly; escape embedded double quotes as
  `\"`.
- **Avoid vague phrases** like "documentation about ..." or "notes on ...".

Examples:

```yaml
description: "Create, list, inspect, rename, and delete Cremind profiles — how to make, add, or register a new profile, remove one, and read or edit a profile's persona text and the assistant's agent name. Distinct from `cremind setup`, which mints the very first admin during install."
```

```yaml
description: "Manage conversations and stream agent replies from the terminal: create, list, fetch, rename, delete, and `send` a message to stream the response. Use this to script one-shot messages and manage threads — distinct from `cremind chat` (the interactive REPL)."
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
- **Aim for a body under about 3,500 tokens (roughly 14,000 bytes of
  English).** The agent's default tool-result budget
  (`tool_result.max_tokens`) is 4,000 tokens, and a `cremind` CLI reference
  arrives with an agent directive of about 300 tokens in front of it. A
  longer document still works, but a search then returns only its head, a
  table of contents with each section's size, and the sections whose headings
  match the query; the agent reads further sections one at a time with
  `read_documentation_section` (the text before the first heading is its
  `Introduction`).
- **Make every `##` and `###` section stand on its own.** In a long document
  a section may be the only part the agent reads, so don't lean on "as shown
  above". Headings must be unique within the file — they are how a section is
  asked for — and no single `###` should exceed about 3,000 tokens; split a
  bigger one into several `###` sections.
- `#` lines inside fenced code blocks (shell comments) are fine: headings
  inside fences are ignored. Outside a fence, the single `#` is the title and
  `####` or deeper is ordinary content, not a section of its own.

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
   keywords appear in your description; it should return your document —
   whole, or for a long one its head and table of contents, from which
   `read_documentation_section` reads any section by its heading.

> **System docs are different.** The watcher covers the two documentation
> roots above. The docs *bundled with Cremind* are mirrored into the shared
> root and re-embedded at server **boot**, so edits to a bundled doc take
> effect on the next restart — not via the live watcher.

If the file doesn't appear, the most common causes are:

1. Malformed frontmatter — confirm that the very first non-empty line is
   exactly `---` and that there is a matching closing `---`.
2. Missing or empty `description` — the parser silently rejects files
   without a non-empty description string.
3. Wrong location — the file must be directly under a `documents/`
   folder, not in a nested subdirectory.
4. YAML parse error — if the description contains colons, hashes, or
   other special characters, wrap the value in double quotes.
