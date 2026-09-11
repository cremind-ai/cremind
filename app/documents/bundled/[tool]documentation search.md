---
description: "The Documentation Search (documentation_search) built-in tool: how the agent finds a document (vector ranking plus an LLM relevance judge, or the judge alone when Vector Embedding is off), how a long document is delivered (its head, a table of contents with section sizes, and the matching sections, sized to the profile's tool_result.max_tokens), reading one section with read_documentation_section, and its DEFAULT_TOP_K variable. How to view and change the documentation_search top-k and toggle its two sub-tools per profile."
---

# Documentation Search Tool (documentation_search)

The **Documentation Search** tool (`tool_id` `documentation_search`) is how the
agent answers questions about Cremind, its skills, the `cremind` CLI, and any
documents the user has added. It is always on (locked) and visible in Settings.
It has two sub-tools: `search_documentation` finds a document, and
`read_documentation_section` reads one section of it.

## How a document is found

The agent's query is embedded and the vector store returns the `DEFAULT_TOP_K`
closest documents from the shared library and the active profile's own
documents. An internal LLM relevance judge — the profile's `low` model group —
then reads each candidate's name and description (never the body) and picks the
single one that answers the query, or none.

When **Vector Embedding is disabled** (or its store is unreachable, or the
document collection has not been built yet), the tool skips vector ranking and
gives the judge **every** document in the shared library plus the profile's own
documents (the profile's are capped at 50; a truncation is logged). Search still
works — it is just unranked — and `DEFAULT_TOP_K` has no effect in that mode.
Which path ran is logged once per change as `[documents] search mode -> …` and
on every search's summary line as `mode=…`: `vector`, `fallback-disabled`
(embedding off or not ready), `fallback-no-collection` (the collection has not
been built yet) or `fallback-error` (the store is unreachable or a query
failed).

Only the first 1,200 characters of a document's description are used, both for
ranking and by the judge.

## How a document is delivered

The reasoning agent cuts every tool result to the profile's
`tool_result.max_tokens` (4000 by default, see `cremind config`). So the tool
sizes its answer to fit:

- **A document that fits** is returned whole.
- **A longer document** is returned as an excerpt: a header naming the
  document, its size and the budget; the document's opening (its head); a
  **table of contents** listing every section with the number of tokens reading
  it costs; the sections whose headings match the query, in full, as far as
  they fit (matching sections that do not fit are named with their size); and a
  footer saying exactly how to read any other section.
- **A longer document with no `##`/`###` headings** has nothing to navigate, so
  it comes back as much of its beginning as fits.

Repeating the same search returns the same excerpt; to see more of the document,
read a section. The footer names the document the way the reader needs it —
normally its name, or a `scope/path` reference such as `admin/guide.md` when
another document shares that name.

When the document is a `cremind` CLI reference (a `[cli]…` doc), the tool
prepends a short agent directive telling the assistant to **run** the relevant
command through its Shell Executor and answer from the live output — rather
than paraphrasing the man page or copying its example tables (which are
illustrative, not live data). It also says where `--json` goes: it is a root
flag, `cremind --json <group> <command>`. The directive is omitted when the
Shell Executor's run-command leaf is disabled for the active profile, so the
assistant is never told to run a command it cannot.

## Reading one section (`read_documentation_section`)

Returns one section of a document by name. It involves no vector search and no
judge: the document is looked up by name among the shared library and the
active profile's own documents, and cut at its headings.

- `document` (required) — the document as the excerpt's footer wrote it: a name
  such as `[cli]cremind channels` (the bracketed tag, a trailing `.md` and case
  are optional), or a `scope/path` reference such as `admin/guide.md`. Either
  form is only ever compared with the documents this profile can see — it is
  never used as a file path, so it cannot reach another profile's documents.
- `section` (optional) — a heading from the table of contents, e.g.
  `cremind channels add`. Backticks, hyphens (`set args` finds `set-args`) and a
  leading `cremind` are optional and matching is case-insensitive; half of a
  combined heading such as `cremind channels enable` / `cremind channels
  disable` also works. `Introduction` returns the text before the first
  heading. Omit it to get the whole document when it fits, or its table of
  contents when it does not.

A section bigger than the budget comes back as its own text plus a list of its
subsections to read next (or, when it has none, its beginning). When nothing
matches, the result is an error carrying `candidates` to pick from:
`DocumentNotFound` (close document names), `AmbiguousDocument` (a name several
documents share — the candidates are their `scope/path` references),
`SectionNotFound` (the closest headings, best first), or `AmbiguousSection`
(every heading that matched).

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `DEFAULT_TOP_K` | number | `10` | Maximum number of documents the vector store returns to the relevance judge for each search call. Ignored when Vector Embedding is off. |

`documentation_search` has no Tool Arguments. The delivery budget has no
variable of its own — it follows `tool_result.max_tokens`.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Documentation Search. Its two sub-tools
  can be switched individually there.
- **CLI** — `cremind tools set-var documentation_search DEFAULT_TOP_K=20`;
  `cremind --json tools get documentation_search` to read the current value;
  `cremind tools leaves documentation_search` to list the two sub-tools and
  `cremind tools set-leaf documentation_search read_documentation_section=false`
  to switch one off. With the section reader off, a long document's excerpt
  says so: other sections can then only be reached by a search whose query
  names their heading.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
