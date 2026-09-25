---
description: "Search the user's OWN indexed files from the terminal with `cremind userdocs search` (passages by meaning and keyword, with --folder, --type, --from/--to dates, --group-by folder, --thorough), `cremind userdocs find` (files, folders or code projects by name, type or date, --kind project), `cremind userdocs read` (a file's text by --pages, --lines, --section such as 'Điều 203', --sheet, --slide) and `cremind userdocs cite` (where a [ud:…] citation points). Same results the agent's user_documents tool gets. Needs User Document Search enabled. Not for Cremind's own documentation (that is documentation_search)."
---

# `cremind userdocs search` — searching your own files

These four subcommands query **User Document Search** the way the agent's
User Documents tool does (`user_documents__search`, `__find_files`,
`__read`), over the current profile's index only. Turning indexing on, the
folder and sync progress are in `cremind userdocs` (see `[cli]cremind
userdocs`).

Each prints the same text the agent reads: a header with the search mode and
index status, the results inside a marked data block, and a `[ud:…]` citation
token on every file and passage. Unlike the agent's copy it is not cut to a
token budget. `cremind --json userdocs …` prints the structured result
instead (`mode`, `items` with tokens, paging).

If the profile has no index yet, or the feature is off or disabled by the
admin, the command says why and exits 1.

## Global flags

All subcommands accept the root-level `--json` flag, right after `cremind`
(`cremind --json userdocs search "budget"`).

## Subcommands

### `cremind userdocs search`

**Purpose.** Find passages about something inside your files — by meaning
and by exact words.

```bash
cremind userdocs search QUERY [--folder NAME]... [--type TYPE]... [--from DATE] [--to DATE]
                              [--date-field any|modified|created|taken]
                              [--group-by file|folder|chunk] [--top-k N] [--thorough]
```

| Flag           | Default  | Meaning |
|----------------|----------|---------|
| `--folder`     | —        | Only inside this folder (loose match: case, accents, typos; subfolders included). Repeatable. |
| `--type`       | —        | `document`, `pdf`, `word`, `spreadsheet`, `presentation`, `text`, `code`, `image`, `audio`, `video`, `archive`, `executable`, `other`. Repeatable. |
| `--from`, `--to` | —      | Date window, `YYYY-MM-DD` (or `YYYY-MM`, `YYYY`), inclusive, in the profile's time zone. |
| `--date-field` | `any`    | Which date the window applies to; `any` = created, modified, photo taken, or first seen. |
| `--group-by`   | `file`   | `file` (two passages per file), `folder` (nearest project folder, else parent), `chunk`. |
| `--top-k`      | 8        | Results per page. |
| `--thorough`   | off      | Restore Vietnamese accents, translate the query, rerank the best 30 with the `low` model group. |

Identifiers such as `45/2013/QH13` or `Điều 203` are matched as exact
phrases. When a date window finds nothing, it is widened (±3 days, ±14 days,
then no date) and the output says so.

```bash
$ cremind userdocs search "AI challenges" --type document --from 2026-09-22 --to 2026-09-24
[User Documents · search · "AI challenges" · mode: hybrid · 1,240 files · 100% synced]
Results 1–1 of 1 (grouped by file; best first):
…
1. AI-challenges.docx [ud:k7m2xq9a] — Reports/AI-challenges.docx · word · 41 KB · modified 2026-09-23 · confidence high
   p. 2 [ud:k7m2xq9a#3f9c2e1b]: The main AI challenges we face are hallucination, …
```

Modes: `hybrid`, `hybrid_partial` (vectors still being built), `lexical_only`
(Vector Embedding off or unreachable), `vector_only`, `catalog_only`.

### `cremind userdocs find`

**Purpose.** Find files, folders or code projects by name, type, date or
topic — or list what a filter selects.

```bash
cremind userdocs find [QUERY] [--kind file|folder|project] [--folder NAME]... [--type TYPE]...
                              [--from DATE] [--to DATE] [--sort ORDER] [--limit N]
```

| Flag       | Default | Meaning |
|------------|---------|---------|
| `--kind`   | `file`  | `file`, `folder`, or `project` (folders with a README, a manifest, `.git`, or several source files). |
| `--folder`, `--type`, `--from`, `--to` | — | As for `search`. For folders and projects the date is their activity (changes beneath them, last commit). |
| `--sort`   | `relevance` with a query, else `newest` | `relevance`, `newest`, `oldest`, `name`, `largest`, `smallest`. |
| `--limit`  | 20      | Results per page. |

```bash
cremind userdocs find "python robot motion tracking" --kind project --from 2025 --to 2025
cremind userdocs find --folder "MKT-report" --sort newest
```

### `cremind userdocs read`

**Purpose.** Print a file's indexed text, or part of it, with a citation token
on every passage.

```bash
cremind userdocs read FILE [--pages 3-5] [--lines 40-80] [--section "Điều 203"] [--sheet NAME] [--slide N]
```

`FILE` is a `[ud:…]` token, a file id, a path inside the indexed folder, or a
file name (quote tokens in the shell: `'[ud:k7m2xq9a]'`).

| Flag        | Meaning |
|-------------|---------|
| `--pages`   | PDF pages, e.g. `3` or `3-5`. |
| `--lines`   | A line range of a text or code file. |
| `--section` | A heading, or a legal reference: `"Điều 203"`, `"khoản 2 Điều 5"`, `"Article 12"`. |
| `--sheet`   | A spreadsheet sheet. |
| `--slide`   | A slide number or range. |

Without a locator a short file prints whole; a long one prints its
beginning, a table of contents (each part with its size and the flag that
reads it) and nothing more. Errors list candidates: `NotFound`,
`AmbiguousFile`, `SectionNotFound`, `SheetNotFound`.

### `cremind userdocs cite`

**Purpose.** Show where a citation token points: the file, the place in it,
and the cited text — to check an answer's sources.

```bash
cremind userdocs cite '[ud:k7m2xq9a#3f9c2e1b]'
```

```text
[ud:k7m2xq9a#3f9c2e1b] · verified
  file: AI-challenges.docx (Reports/AI-challenges.docx)
  where: p. 2
  text: The main AI challenges we face are hallucination, …
```

A token from another profile, or one that was never printed by the tools, is
reported as not found (exit 1).

## Troubleshooting

**`User Document Search is off for this profile`** — `cremind userdocs enable`.

**`… has not been indexed yet`** — the first sync is running or waiting for
`cremind userdocs start`; follow it with `cremind userdocs status -f`.

**`mode: lexical_only`** — Vector Embedding is off or not ready: keyword
search still works; meaning-based matches return when it is back.
