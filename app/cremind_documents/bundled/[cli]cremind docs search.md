---
description: "Search the user's OWN indexed files (local folder and Google Drive) from the terminal with `cremind docs search` (passages by meaning and keyword, with --folder, --type, --from/--to dates, --source local|drive, --group-by folder, --thorough), `cremind docs find` (files, folders or code projects by name, type or date, --kind project), `cremind docs read` (a file's text by --pages, --lines, --section such as 'Điều 203', --sheet, --slide) and `cremind docs cite` (where a [doc:…] citation points). Same results the agent's documentation_search tool gets; --json adds a delivery object saying which passages the text shows whole. Needs Documentation search enabled. Not for Cremind's own documentation (that is cremind_documentation_search)."
---

# `cremind docs search` — searching your own files

These four subcommands query **Documentation search** the way the agent's
Documentation Search tool does (`documentation_search__search`, `__find_files`,
`__read`), over the current profile's index only. Turning indexing on, the
folder and sync progress are in `cremind docs` (see `[cli]cremind docs`).

Each prints the same text the agent reads: a header with the search mode and
index status, the results inside a marked data block, and a `[doc:…]` citation
token on every file and passage. Unlike the agent's copy it is not cut to a
token budget. `cremind --json docs …` prints the structured result
instead (`mode`, `items` with tokens, paging) plus a `delivery` object: what
the printed text actually shows.

```json
"delivery": {"v": 1, "rendered_tokens": 812, "truncated": false, "continuation": null,
             "passages": [{"token": "[doc:k7m2xq9a#3f9c2e1b]", "role": "match", "complete": false}],
             "focus": {"token": "[doc:k7m2xq9a#3f9c2e1b]", "status": "complete"}}
```

`passages` lists each passage token the text shows — `role` is `match` or
`context` for a search, `body` for a read — and whether its whole text is
shown (`complete`) or only a snippet or excerpt. `truncated` says something
was left out to fit a budget, and `continuation` how to get it (`{"page":
2}`). A read also reports its `focus` (the passage a passage token asked
for: `complete`, `partial`, `omitted` on this page, or `unresolved` when the
file no longer has it), `stale` and `metadata_only`. Search results'
`items[].passages[]` gain `visible`. Indexing `coverage` keeps its meaning.

These commands only search and read. The automatic review a conversation
makes when a search returns several relevant files (see `[tool]documentation
search`) is the agent's; `cremind docs search` never reads files on its own.

If the profile has no index yet, or the feature is off or disabled by the
admin, the command says why and exits 1.

## Global flags

All subcommands accept the root-level `--json` flag, right after `cremind`
(`cremind --json docs search "budget"`).

## Subcommands

### `cremind docs search`

**Purpose.** Find passages about something inside your files — by meaning
and by exact words.

```bash
cremind docs search QUERY [--folder NAME]... [--type TYPE]... [--from DATE] [--to DATE]
                              [--date-field any|modified|created|taken] [--source local|drive|all]
                              [--group-by file|folder|chunk] [--top-k N] [--thorough]
```

| Flag           | Default  | Meaning |
|----------------|----------|---------|
| `--folder`     | —        | Only inside this folder (loose match: case, accents, typos; subfolders included). Repeatable. |
| `--type`       | —        | `document`, `pdf`, `word`, `spreadsheet`, `presentation`, `text`, `code`, `image`, `audio`, `video`, `archive`, `executable`, `other`. Repeatable. |
| `--from`, `--to` | —      | Date window, `YYYY-MM-DD` (or `YYYY-MM`, `YYYY`), inclusive, in the profile's time zone. |
| `--date-field` | `any`    | Which date the window applies to; `any` = created, modified, photo taken, or first seen. |
| `--source`     | `all`    | `local` (the indexed folder), `drive` (Google Drive files, paths `Drive/…`), or `all`. |
| `--group-by`   | `file`   | `file` (two passages per file), `folder` (nearest project folder, else parent), `chunk`. |
| `--top-k`      | 8        | Results per page. |
| `--thorough`   | off      | Restore Vietnamese accents, translate the query, rerank the best 30 with the `low` model group. |

Identifiers such as `45/2013/QH13` or `Điều 203` are matched as exact
phrases. When a date window finds nothing, it is widened (±3 days, ±14 days,
then no date) and the output says so.

Every flag is optional, and a flag you leave off is no constraint: the
command sends only the filters you give. Size and location filters have no
flags; the endpoint behind these commands (`POST
/api/documentation-search/query/search`, or `…/query/find`) takes the agent's
whole `filters` object, where a field left out or `null` is also no
constraint and a value (`"size_max": 0`, `"has_gps": true`) always restricts:

```bash
cremind docs search "OpenClaw installation" --type pdf
# the same search over the endpoint — null size and GPS limits change nothing:
#   {"query": "OpenClaw installation", "filters": {"types": ["pdf"], "size_max": null, "has_gps": null}}
```

```bash
$ cremind docs search "AI challenges" --type document --from 2026-09-22 --to 2026-09-24
[Documentation Search · search · "AI challenges" · mode: hybrid · 1,240 files · 100% synced]
Results 1–1 of 1 (grouped by file; best first):
…
1. AI-challenges.docx [doc:k7m2xq9a] — Reports/AI-challenges.docx · word · 41 KB · modified 2026-09-23 · confidence high
   p. 2 [doc:k7m2xq9a#3f9c2e1b]: The main AI challenges we face are hallucination, …
```

Modes: `hybrid`, `hybrid_partial` (vectors still being built), `lexical_only`
(Vector Embedding off or unreachable), `vector_only`, `catalog_only`.

### `cremind docs find`

**Purpose.** Find files, folders or code projects by name, type, date or
topic — or list what a filter selects.

```bash
cremind docs find [QUERY] [--kind file|folder|project] [--folder NAME]... [--type TYPE]...
                              [--from DATE] [--to DATE] [--source local|drive|all] [--sort ORDER] [--limit N]
```

| Flag       | Default | Meaning |
|------------|---------|---------|
| `--kind`   | `file`  | `file`, `folder`, or `project` (folders with a README, a manifest, `.git`, or several source files). |
| `--folder`, `--type`, `--from`, `--to`, `--source` | — | As for `search`. For folders and projects the date is their activity (changes beneath them, last commit). |
| `--sort`   | `relevance` with a query, else `newest` | `relevance`, `newest`, `oldest`, `name`, `largest`, `smallest`. |
| `--limit`  | 20      | Results per page. |

```bash
cremind docs find "python robot motion tracking" --kind project --from 2025 --to 2025
cremind docs find --folder "MKT-report" --sort newest
```

### `cremind docs read`

**Purpose.** Print a file's indexed text, or part of it, with a citation token
on every passage.

```bash
cremind docs read FILE [--pages 3-5] [--lines 40-80] [--section "Điều 203"] [--sheet NAME] [--slide N]
```

`FILE` is a `[doc:…]` token, a file id, a path inside the indexed folder, or a
file name (quote tokens in the shell: `'[doc:k7m2xq9a]'`).

| Flag        | Meaning |
|-------------|---------|
| `--pages`   | PDF pages, e.g. `3` or `3-5`. |
| `--lines`   | A line range of a text or code file. |
| `--section` | A heading, or a legal reference: `"Điều 203"`, `"khoản 2 Điều 5"`, `"Article 12"`. |
| `--sheet`   | A spreadsheet sheet. |
| `--slide`   | A slide number or range. |

Without a locator a short file prints whole; a long one prints its
beginning, a table of contents (each part with its size and the flag that
reads it) and nothing more. A passage token as `FILE` prints that passage
with its neighbours — the passage first when a budget would not fit them all.
Errors list candidates: `NotFound`, `AmbiguousFile`, `SectionNotFound`,
`SheetNotFound`. For `SectionNotFound` the candidates are the file's closest
real headings, each a value `--section` accepts, and the message gives the
first one's pages: a title copied from a document's contents page
(`"Phần 9: Multi-Agent - Xây Dựng Đội AI"`) is often indexed as a shorter
heading (`"PHẦN 9"`) — pass that, or `--pages`.

```bash
$ cremind docs read '[doc:k7m2xq9a]' --section "Phần 9: Multi-Agent - Xây Dựng Đội AI"
No part of this file matches section='Phần 9: Multi-Agent - Xây Dựng Đội AI'. The candidates are the
closest headings this file has (the first is on p. 43–44): pass one of them exactly as section. …
  PHẦN 9
```

### `cremind docs cite`

**Purpose.** Show where a citation token points: the file, the place in it,
and the cited text — to check an answer's sources.

```bash
cremind docs cite '[doc:k7m2xq9a#3f9c2e1b]'
```

```text
[doc:k7m2xq9a#3f9c2e1b] · verified
  file: AI-challenges.docx (Reports/AI-challenges.docx)
  where: p. 2
  text: The main AI challenges we face are hallucination, …
```

A token from another profile, or one that was never printed by the tools, is
reported as not found (exit 1). Answers written before the rename carry
`[ud:…]` tokens; `cite` and `read` accept them and print the `[doc:…]` form.

## Troubleshooting

**`Documentation search is off for this profile`** — `cremind docs enable`.

**`… has not been indexed yet`** — the first sync is running or waiting for
`cremind docs start`; follow it with `cremind docs status -f`.

**`mode: lexical_only`** — Vector Embedding is off or not ready: keyword
search still works; meaning-based matches return when it is back.
