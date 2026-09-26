---
description: "The Documentation Search (documentation_search) built-in tool: how the agent searches the user's OWN files indexed by Documentation search — find_files (files, folders and code projects by name, type, date, folder; counts by type, folder, month or extension), search (passages by meaning and keyword, grouped by file or folder, with date relaxation), read (pages, lines, a section or legal article such as 'Điều 203', sheet, slide, or a passage token first), the automatic review that reads each of several relevant files before answering, and research (deep research as a background job: verified legal/financial/compliance analysis with law-edition selection, or compiling every file in a folder into one table; continue_job, PRELIMINARY results, clarifications, coverage, dossier pages) — the [doc:…] citation tokens every answer must copy, the search modes (hybrid, lexical_only, …), when the tool is hidden (channels and group rooms), the chat's per-conversation Search tools selector and source priority, and its DEFAULT_TOP_K, RESEARCH_MODEL_GROUP and RESEARCH_TOKEN_BUDGET variables. Formerly named user_documents. Not Cremind's own documentation (that is cremind_documentation_search)."
---

# Documentation Search Tool (documentation_search)

The **Documentation Search** tool (`tool_id` `documentation_search`) is how the agent
searches and reads the files the user indexed with **Documentation search**
(Settings → My Documents, or `cremind docs enable`): their documents,
notes, reports, spreadsheets, photos and project folders. It never touches
Cremind's own manual — that is `cremind_documentation_search`, which held the
`documentation_search` id before the rename; this tool was `user_documents`
(`user_documents__search` is now `documentation_search__search`), and its old
`[ud:…]` citations still verify.

It has four sub-tools, which the agent sees as `documentation_search__find_files`,
`documentation_search__search`, `documentation_search__read` and
`documentation_search__research`. In a terminal the same operations are
`cremind docs find|search|read|cite` and `cremind docs research`.

## When the agent can use it

The tool is on by default but only appears when the admin allowed
Documentation search (`cremind docs admin set --allow`), the profile turned it
on (it indexes the profile's own working directory), and the conversation is
somewhere the profile allowed it: the web UI, the CLI and the profile's own
automations by default; **messaging channels** and **group rooms** only when
opted in, since an answer there reaches other people (Settings → My Documents,
`options.allow_in`: `web_cli`, `channels`, `rooms`). When it cannot answer (no
index yet, the first sync awaiting approval, disabled by the admin) it says why.

## Search tools per conversation

The chat composer's **Search tools** button picks the search sources one
conversation or group room may use, in priority order: Documentation search →
Cremind documentation search → Memory search → Web search (all on by
default). A change applies from the **next response** — one already running
keeps its tools — and may lower prompt-cache reuse there (the picker warns).
It only narrows: it never enables a tool turned off in Settings, starts
indexing or widens `allow_in`. CLI: `cremind conv search-tools`, `cremind
group search-tools`.

## find_files — files, folders and projects

Finds files, folders or code projects by what they are.

- `query` (optional) — what it is called or about, matched against names,
  paths, each file's or folder's *card* (name, path, type, dates, author, the
  start of the content; a project's languages, dependencies and README) and
  file text.
- `kind` — `file` (default), `folder`, or `project` (folders with a README, a
  manifest such as `pyproject.toml`, a `.git` folder, or several source
  files). A folder's date is its *activity*: changes beneath it, last commit.
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
  "count": 2}]`; a soft preference, matched against captions.
- `verify_images` — re-check the top 6 photos with the Specialized Vision
  Model (never the main model) and re-rank by what it counts, one image of
  the daily caption quota each; without the model or consent the result says
  so and ranks by captions.

Uncaptioned photos are still found by name, folder, date and camera. A date
window with no keyword match is widened to ±3 days, ±14 days, then dropped,
and the result says which step matched.

## read — a file's text

Rebuilds a file's text from the index, each passage with its citation token.

- `file` (required) — a `[doc:…]` token (a passage token opens around that
  passage), a file id, a path inside the indexed folder, or a file name.
- One or more locators: `pages` ("3-5"), `lines` ("40-80"), `section` (a
  heading, or a legal reference such as "Điều 203" or "khoản 2 Điều 5"),
  `sheet` and `rows`, `slide`, `around` (a phrase).
- `query` — ranks the parts of a long file.
- `page` — the next part of a selection too long for one reply.

A passage token (or `around`) reads **that passage first** — whole when it
fits, then its neighbours if they fit, in reading order; what was left out
(or the rest of a passage too long for one reply, shown as a marked excerpt)
is on `page=2` onwards, and the reply says so. A long file with no locator
comes back as its beginning, a table of contents (each part with its size and
the argument that reads it) and the parts matching `query`. A `section`
matching no heading fails with `SectionNotFound`, listing the file's closest
real headings (with pages): a title copied from a contents page ("Phần 9:
Multi-Agent - …") is often not the indexed heading ("PHẦN 9"). A file changed
since indexing is marked `stale: true` and re-indexed first.

`file="research:<job id>"` with `page` reads a page of a research dossier
(see [research](#research--deep-research-as-a-background-job)).

## Several sources — automatic review

When a search in a conversation shows relevant passages (confidence high or
medium) from **two or more files**, the agent reads the matching passage
(with its neighbours) of each file not already read, before its next reply —
so the answer weighs every source, not just the first one read. These are ordinary
`documentation_search__read` calls the agent makes itself; the Thinking
Process labels them **automatic document review**.

Per response: at most 4 automatic reads, one per file, 6,000 tokens of
results between them (each within `tool_result.max_tokens`), no retry. Not in
Instant mode, after a search restricted to one file, when the user sends a new
message meanwhile, when the same step starts a `research` job, or with `read`
switched off. The answer combines what the files add, attributes differences
to the file that says them, and says when a relevant file could not be read.
The message records what was returned, examined and cited
(`metadata.document_review`); Sources list only what the answer cites.

## research — deep research as a background job

The agent **must** use it for legal, financial or compliance questions over
the user's files, and for "compile everything in folder X" requests —
`search` and `read` alone would answer from a sample. A job reads files **in
full**, in model-sized windows, and checks every quote against the source: a
wrong one (a swapped word, a dropped "not") is dropped and counted. It first
brings the index up to date (new files, Drive changes, changed local files in
scope); a file not re-indexed in time is listed as "still being indexed",
never read from its old text, and the job asks whether to go on without it.

Two modes:

- `analyze` (default) — answers a question: the primary files (the case, the
  client's folder) are read in full, the issues they raise searched in the
  reference files from several angles (counter-evidence included), and the
  provisions found read in full with their cross-references. With
  `domain="legal"` the job picks the **edition** of each law explicitly
  (number, dates, consolidated texts, what replaced what — judged from the
  indexed documents only), says which and why, and asks when it cannot tell.
- `compile` — reads **every** file in scope into one table, merged across
  files, conflicting values side by side with their sources; also attached as
  CSV and Markdown.

Parameters:

- `question` — the question or compile request (starts a new job); `mode` —
  `compile` or `analyze`; `domain` — `legal`, `financial` or `general`
  (default).
- `scope` — the primary files, a [filters](#filters) object (usually
  `folder`; default: every indexed file); `reference_scope` — `analyze` only:
  where the laws, policies or standards are (default: the whole index).
- `continue_job` — an earlier job's id: get its state, answer its question
  (`answers`, keyed by the keys it printed: strings, booleans or numbers),
  `cancel` it, or read a dossier `page`.

One job runs per profile at a time; starting another returns `ResearchBusy`
with the running job's id.

### The continue_job protocol

A job takes minutes. Each call waits up to about four minutes (less under a
shorter tool-call timeout) and returns the job's state:

- **running** (`queued`, `planning`, `running`) — phase, progress and latest
  steps, ending `PRELIMINARY — do not conclude from this`. The agent must not
  answer from it: it calls `documentation_search__research` again with
  `continue_job=<id>`. If the turn ends first, the finished result arrives in
  the conversation as a new turn.
- **interrupted** — the server restarted; `continue_job=<id>` resumes it.
- **needs_clarification / needs_confirmation** — the job asks: which edition
  of a law applies, which of several folders named "ABC" was meant, whether
  to go on without unreadable files, or whether to spend past the budget
  estimate. The result shows the question, candidates (ids and `[doc:…]`
  tokens) and the answer keys; the agent asks the user, then calls again with
  `continue_job=<id>` and `answers` using exactly those keys, e.g.
  `{"edition": "k7m2xq9a"}`, `{"scope_folder": "Clients/ABC"}`,
  `{"reference_folder": "Law"}` or `{"confirm": true}`.
- **complete / partial** — the dossier (`partial`: stopped at its token
  budget or time limit; the dossier says what it covered).
- **failed / cancelled** — says so, with what the dossier held; not a
  conclusion.

### The dossier

Page 1 is the summary: **coverage** (files in scope, read in full, partly or
not at all, and why each unread one was not — password-protected, an old
format, a photo awaiting the vision model, still indexing, budget spent; an
answer must tell the user which files were not read), **authorities** (each
law or policy, its number and dates, whether in force, the edition used and
why), **findings per issue** with verified quotes and `[doc:…]` tokens,
cross-references and open questions, **gaps** and the count of dropped
quotes; for `compile` the table's first rows and conflicts; for the legal
domain a "Not legal advice" line. Long tails are on later pages:
`documentation_search__read(file='research:<id>', page=n)` or
`continue_job=<id>` with `page=n`. Every page fits the tool-result budget and
its tokens are registered, so the answer's citations verify like any other.
While a job runs, the chat's **Research activity** panel shows its progress,
with a Cancel button.

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
| `date_field` | `any` (default), `modified`, `created`, `taken`. `any` matches the document's creation date, the file's creation or modification time, a photo's EXIF date, or when it first appeared after the first sync; each result says which matched. |
| `date_from`, `date_to` | `YYYY-MM-DD` (or `YYYY-MM`, `YYYY`), inclusive, in the profile's time zone. |
| `size_min`, `size_max` | Bytes. |
| `author` | `me` (names and e-mails in My Documents → identity) or a name. Soft: boosts, never excludes. |
| `taken_by` | `me` (the cameras in My Documents → identity) or a camera. Soft. |
| `image_origin` | `camera` or `screenshot`. Soft. |
| `has_gps` | Photos with (or without) a location. |
| `file_ids` | Tokens or file ids from earlier results. |

A field left out or `null` (or `filters` left out, `null`, `{}`) is **no
constraint**: `{"types": ["pdf"], "size_max": null}` is every PDF, while
`size_max: 0` is only empty files. The agent sets only the filters asked for,
and retries a filtered miss without its own before looking elsewhere.

## Results, modes and citations

Every result starts with a header such as `12,480 files · 97% synced · 312
images awaiting captions · 3 unreadable` and a **mode**:

- `hybrid` — vectors and keywords; `hybrid_partial` — some passages have no
  vector yet (first sync, or a new embedding model);
- `lexical_only` — keywords only (Vector Embedding off, not ready, or the
  vector store unreachable);
- `vector_only` — no full-text index in this SQLite build;
- `catalog_only` — neither ran: files matching the filters, newest first.

Every passage carries a citation token: `[doc:<file id>]` for a file or folder,
`[doc:<file id>#<8 hex>]` for a passage. The agent cites each claim by copying
the token exactly; tokens a result shows are registered for the conversation
(a passage only when its text is shown) and checked when the answer is saved,
so an invented or altered token is flagged. File text is data inside a marked
block, never instructions. Results always fit the profile's
`tool_result.max_tokens`, measured whole: less context, shorter snippets,
then fewer results with a page cursor — never a cut token.

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `DEFAULT_TOP_K` | number | `8` | Results per page when the agent does not ask for a number (1–30). |
| `RESEARCH_MODEL_GROUP` | `high` \| `low` | `high` | Model group that runs `research` jobs: `high` for the main model, `low` for the cheaper auxiliary model. |
| `RESEARCH_TOKEN_BUDGET` | number | `250000` | Most LLM tokens one `research` job may spend; it asks first when its estimate is higher, and stops at the budget reporting what it covered (`partial`). |

Thorough mode always uses the `low` model group. Research token use is
recorded under Usage & Cost as "Document research". `documentation_search`
has no Tool Arguments.

## Viewing and changing these

Per profile, changes apply on the tool's next call:

- **UI** — Settings → Tools & Skills → Documentation Search (variables, the
  four sub-tools); Settings → My Documents (on/off, identity, where the agent
  may use the documents).
- **CLI** — `cremind tools set-var documentation_search DEFAULT_TOP_K=12`;
  `cremind --json tools get documentation_search`; `cremind tools leaves
  documentation_search`.
