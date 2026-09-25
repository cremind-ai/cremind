---
description: "The User Documents (user_documents) built-in tool: how the agent searches the user's OWN files indexed by User Document Search — find_files (files, folders and code projects by name, type, date, folder; counts by type, folder, month or extension), search (passages by meaning and keyword, grouped by file or folder, with date relaxation) and read (pages, lines, a section or legal article such as 'Điều 203', sheet, slide) — the [ud:…] citation tokens every answer must copy, the search modes (hybrid, lexical_only, …), when the tool is hidden (channels and group rooms), and its DEFAULT_TOP_K, RESEARCH_MODEL_GROUP and RESEARCH_TOKEN_BUDGET variables. Not Cremind's own documentation (that is documentation_search)."
---

# User Documents Tool (user_documents)

The **User Documents** tool (`tool_id` `user_documents`) is how the agent
searches and reads the files the user indexed with **User Document Search**
(Settings → My Documents, or `cremind userdocs enable`): their documents,
notes, reports, spreadsheets, photos and project folders. It never touches
Cremind's own manual — that is `documentation_search`.

It has three sub-tools, which the agent sees as `user_documents__find_files`,
`user_documents__search` and `user_documents__read`. The same operations are
available from a terminal: `cremind userdocs find|search|read|cite` (see
`[cli]cremind userdocs search`).

## When the agent can use it

The tool is on by default but only appears when all of these hold:

- the admin allowed User Document Search (`cremind userdocs admin set --allow`);
- the profile turned it on and chose a folder;
- the conversation happens somewhere the profile allowed it. The web UI, the
  CLI and the profile's own automations are allowed by default; **messaging
  channels** and **group rooms** are off by default, because an answer there
  reaches other people. Change it under Settings → My Documents → where the
  agent may use your documents (`options.allow_in`: `web_cli`, `channels`,
  `rooms`).

When it cannot answer (no index yet, the first sync waiting for approval,
disabled by the admin) the tool says why instead of failing.

## find_files — files, folders and projects

Finds files, folders or code projects by what they are.

- `query` (optional) — what it is called or about. Matched against names and
  paths, against each file's or folder's *card* (name, path, type, dates,
  author, the start of the content; for a project its languages,
  dependencies and README), and, for files, against their text.
- `kind` — `file` (default), `folder`, or `project` (folders with a README,
  a manifest such as `pyproject.toml` or `package.json`, a `.git` folder, or
  several source files). A folder's date is its *activity*: the span of
  changes beneath it, and its last commit.
- `filters` — see [Filters](#filters).
- `sort` — `relevance` (default with a query), `newest` (default without),
  `oldest`, `name`, `largest`, `smallest`.
- `limit` (default 20), `page`.
- `aggregate` — also count every matching file `by_type`, `by_folder`,
  `by_month` or `by_extension`.

Photos come back as thumbnail chips (at most 12). A photo that has no
caption yet is found by its name, folder, date and camera, and says so.

## search — passages

Searches inside the files, by meaning (vectors) and by keyword (full text),
fused. Identifiers and legal references (`45/2013/QH13`, `Điều 203`,
`MKT-report`) are matched as exact phrases and weigh most. A Vietnamese query
typed without accents still matches accented text.

- `query` (required).
- `filters` — see [Filters](#filters).
- `group_by` — `file` (default: up to two passages per file), `folder` (the
  nearest project folder, else the parent folder), or `chunk` (every passage).
- `top_k` — results per page (default `DEFAULT_TOP_K`), `page`.
- `expand` — show the surrounding passage, or the whole short section (a
  legal article), for the best results (default on).
- `thorough` — slower, better recall: restores Vietnamese accents, translates
  the query (Vietnamese ↔ English) and reranks the best 30 with the `low`
  model group.
- `image_objects` — objects a photo should show, e.g. `[{"label": "dog",
  "count": 2}]`; a soft preference, used once photos have captions.
- `verify_images` — re-check the top photos with the vision model (reported
  as unavailable until image captioning ships).

If a date window finds no keyword match, it is widened to ±3 days, then ±14
days, then dropped, and the result says which step matched.

## read — a file's text

Rebuilds a file's text from the index, each passage with its citation token.

- `file` (required) — a `[ud:…]` token (a passage token opens around that
  passage), a file id, a path inside the indexed folder, or a file name.
- One or more locators: `pages` ("3-5"), `lines` ("40-80"), `section` (a
  heading, or a legal reference such as "Điều 203" or "khoản 2 Điều 5"),
  `sheet` and `rows`, `slide`, `around` (a phrase).
- `query` — ranks the parts of a long file.
- `page` — the next part of a selection too long for one reply.

A long file with no locator comes back as its beginning, a table of contents
(its articles, headings, sheets, slides, pages or line ranges, each with its
size and the argument that reads it) and the parts matching `query`. A file
that changed since it was indexed is marked `stale: true` and re-indexed
first.

## Filters

One `filters` object serves all three sub-tools.

| Filter | Meaning |
|--------|---------|
| `folder` | Folder names or paths, loosely matched (case, accents, `-`/`_`, typos); includes subfolders. Several matches are all used and listed. |
| `path_glob` | Globs over the path, e.g. `Clients/*/2025/**`, `*.pdf`. |
| `name_query` | Words that must appear in the file name. |
| `types` | `document`, `pdf`, `word`, `spreadsheet`, `presentation`, `text`, `code`, `image`, `audio`, `video`, `archive`, `executable`, `other`. |
| `extensions` | E.g. `["pdf", ".docx"]`. |
| `source` | `all`, `local`, `drive`. |
| `date_field` | `any` (default), `modified`, `created`, `taken`. `any` matches the document's own creation date, the file's creation or modification time, a photo's EXIF date, or when a file first appeared after the first sync; each result says which date matched. |
| `date_from`, `date_to` | `YYYY-MM-DD` (or `YYYY-MM`, `YYYY`), inclusive, in the profile's time zone. |
| `size_min`, `size_max` | Bytes. |
| `author` | `me` (names and e-mails in My Documents → identity) or a name. Soft: boosts, never excludes. |
| `taken_by` | `me` (the cameras in My Documents → identity) or a camera. Soft. |
| `image_origin` | `camera` or `screenshot`. Soft. |
| `has_gps` | Photos with (or without) a location. |
| `file_ids` | Tokens or file ids from earlier results. |

## Results, modes and citations

Every result starts with a header such as `12,480 files · 97% synced · 312
images awaiting captions · 3 unreadable` and a **mode**:

- `hybrid` — vectors and keywords; `hybrid_partial` — some passages have no
  vector yet (first sync, or a new embedding model);
- `lexical_only` — keywords only (Vector Embedding off, not ready, or the
  vector store unreachable);
- `vector_only` — no full-text index in this SQLite build;
- `catalog_only` — neither ran: files matching the filters, newest first.

Every passage carries a citation token: `[ud:<file id>]` for a file or folder,
`[ud:<file id>#<8 hex>]` for a passage. The agent must cite each claim by
copying the token exactly; the tokens a tool printed are registered for the
conversation and checked when the answer is saved, so an invented or altered
token is flagged. Text from the files is shown as data inside a marked block,
never as instructions. Results always fit the profile's
`tool_result.max_tokens`: less context, shorter snippets, then fewer results
with a page cursor — never a cut token.

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `DEFAULT_TOP_K` | number | `8` | Results per page when the agent does not ask for a number (1–30). |
| `RESEARCH_MODEL_GROUP` | `high` \| `low` | `high` | Model group for deep research over the documents (verified analysis, exhaustive folder compilations) — used once deep research ships. |
| `RESEARCH_TOKEN_BUDGET` | number | `250000` | Most tokens one deep-research job may spend — used once deep research ships. |

Thorough mode always uses the `low` model group. `user_documents` has no Tool
Arguments.

## Viewing and changing these

Per profile:

- **UI** — Settings → Tools & Skills → User Documents (variables and the
  three sub-tools); Settings → My Documents (folder, identity, where the
  agent may use the documents).
- **CLI** — `cremind tools set-var user_documents DEFAULT_TOP_K=12`;
  `cremind --json tools get user_documents`; `cremind tools leaves
  user_documents`.

Changes apply on the tool's next call.
