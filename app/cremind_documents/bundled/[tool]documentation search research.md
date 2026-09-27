---
description: "documentation_search__research, the Documentation Search tool's deep research over the user's own files as a background job: when the agent must use it (rights, obligations, applicability, conflicting provisions, law editions; compiling a folder) and when to find/search/read instead (summarizing what a document covers); analyze and compile modes; scope (case records) vs reference_scope (governing laws); documents named by number ('Decree 165') identified before any search; a scanned document awaiting OCR reported as found but unreadable; the continue_job protocol (running, PRELIMINARY, interrupted, clarifications such as which edition or which document, confirmations); outcomes (evidenced, insufficient_evidence, content_unavailable, partly_readable, document_changed, no_verified_findings…); and the dossier pages."
---

# Documentation Search: research

`documentation_search__research` is the **research** sub-tool of the
Documentation Search tool (`documentation_search`, see its own reference for
find_files, search, read, filters and variables). In a terminal the same jobs
run with `cremind docs research`.

## When to use it

The agent **must** use it for questions of rights, obligations,
applicability, conflicting provisions or which edition of a law applies —
legal, financial or compliance — and for "compile everything in folder X"
requests: `search` and `read` alone would answer from a sample. To
**summarize or explain what a document covers**, the agent instead finds it
(`find_files`), searches and reads it, and answers with its citations.

A job reads in model-sized windows and checks every quote against the source:
a wrong one (a swapped word, a dropped "not") is dropped and counted. It first
brings the index up to date; a file not re-indexed in time is listed as
"still being indexed", never read from its old text, and the job asks whether
to go on without it.

## Modes and parameters

- `analyze` (default) — answers a question: the primary files (the case) are
  read in full, the issues searched in the reference files from several
  angles (counter-evidence included), the provisions found read in full with
  their cross-references. With `domain="legal"` the job picks the **edition**
  of each law explicitly (judged from the indexed documents only), says which
  and why, and asks when it cannot tell.
- `compile` — reads **every** file in scope into one table, merged across
  files, conflicting values side by side with their sources; also attached as
  CSV and Markdown.

- `question` — the question or compile request (starts a new job); `mode`;
  `domain` — `legal`, `financial` or `general` (default).
- `scope` — the primary files (a filters object): `analyze`'s **case
  records** (default: none — the question is the case), `compile`'s folder
  (default: every indexed file). `reference_scope` — `analyze` only: the
  **governing laws or policies**; left out, the job finds them across the
  whole index. One file may be both: a decree given as the case is also read
  as its own authority.
- `continue_job` — an earlier job's id: get its state, answer its question
  (`answers`, keyed by the keys it printed), `cancel` it, or read a dossier
  `page`.

One job runs per profile at a time; another start returns `ResearchBusy`.

## Documents named by number

A document the question names by number — "Decree 165", "Nghị định 165",
"165/2024/NĐ-CP" — is **identified before any search**, from the index's
legal metadata, titles, headers and file names: "Decree 165" matches
165/2024/NĐ-CP, never 1650/…; a year, kind or issuer given must agree. Its
appendix comes with it. The identified document is read whatever a topical
relevance check says of it; several documents under one number stop the job
to ask (`{"document": "165/2024/NĐ-CP"}` or a file id). A document found but
not readable yet — a scan whose pages wait for OCR, a file still indexing —
is reported as such (`content_unavailable`), never as missing or "not a
legal document". Before giving up on a named document the job looks once
more at the index.

## The continue_job protocol

Each call waits up to about four minutes and returns the job's state:

- **running** — phase, progress, latest steps, ending `PRELIMINARY — do not
  conclude from this`: call again with `continue_job=<id>`. If the turn ends
  first, the finished result arrives as a new turn.
- **interrupted** — the server restarted; `continue_job=<id>` resumes it.
- **needs_clarification / needs_confirmation** — which edition applies,
  which document or folder was meant, whether to go on without unreadable
  files, or whether to spend past the budget estimate. Ask the user, then
  call again with `answers` using exactly the printed keys, e.g.
  `{"edition": "k7m2xq9a"}`, `{"document": "k7m2xq9a"}`,
  `{"scope_folder": "Clients/ABC"}`, `{"reference_folder": "Law"}` or
  `{"confirm": true}`.
- **complete** — every issue has verified findings. **partial** — the
  outcome says why not: `insufficient_evidence`, `unresolved_instrument` (no
  document matches a law named), `content_unavailable` (found, not readable
  yet), `partly_readable`, `document_changed` (a document changed or left the
  index while the job ran — never described as never indexed),
  `no_verified_findings` (read, nothing verified), `candidates_rejected`, or
  the budget or time. Answer only from verified findings; never tell the user
  an indexed file is missing.
- **failed / cancelled** — says so, with what the dossier held.

## The dossier

Page 1 opens with the status, the **outcome** (reason, counts and a trace of
identities, candidates, rejections and revisions) and the coverage totals,
then: unread files and why, **authorities** (number, dates, in force or not,
edition used and why), **findings per issue** with verified quotes and
`[doc:…]` tokens, cross-references, open questions, **gaps**; for `compile`
the table's head and conflicts. Later pages:
`documentation_search__read(file='research:<id>', page=n)` or
`continue_job=<id>` with `page=n`. Unseen pages with verified findings are
read for the agent before it answers (labelled **automatic**). The chat's
**Research activity** panel tracks a running job (with Cancel).
