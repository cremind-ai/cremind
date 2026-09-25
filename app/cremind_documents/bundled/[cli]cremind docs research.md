---
description: "Deep research over the user's OWN indexed files with `cremind docs research`: `run` a background job that either ANALYZES a question (legal, financial or general: reads the case files in full, searches the law or policy folder from several angles, verifies every quote; `--domain legal` picks the edition of each law in force and stops to ask when it cannot tell) or COMPILES a folder exhaustively (every file read, one table, conflicting values kept side by side — e.g. compile the business results in MKT-report). The result is a dossier: a coverage table of every file read or unread and why, findings with verified [doc:…] citations, gaps. `--follow` prints progress until done; `continue JOB --answer edition=<fid>` answers a clarification; `status --all-pages`, `cancel`, `list`. Not for Cremind's own documentation (that is cremind_documentation_search); one quick lookup is `cremind docs search`."
---

# `cremind docs research` — deep research over your documents

A **research job** answers one question over many of your indexed files and
checks its own work: it lists every file in scope and whether it was read,
and every quote it relies on is matched against the source text before it is
kept. It is the job the agent runs through its Documentation Search tool
(`documentation_search__research`); these commands start, follow, answer and cancel
the same jobs from a terminal or a script.

**In a chat, the agent uses the tool, not this command.** Only the tool
registers the answer's `[doc:…]` citations for the conversation (so they
verify when the answer is saved) and shows the Research activity panel. A job
started here belongs to no conversation.

A job reads the text the index holds, so it first brings the index up to
date: it checks the folder for new files (a scan, when the folder is
polled), asks Google Drive for its latest changes when Drive is indexed,
compares every local file in scope with the disk, and re-indexes the changed
ones before reading. A changed file that could not be re-indexed in
time, or while sync is paused, is never read from its old text: it shows as
`not_indexed_yet` and the job asks before going on without it.

A job runs on the server, not in your terminal: Ctrl-C stops following it,
not the job. A profile runs one job at a time — a second `run` is refused
with `ResearchBusy`, which names the running job. A job stops at the
profile's token budget or after 30 minutes in total and reports what it
covered (`partial`). The budget and the model are the Documentation Search tool
variables `RESEARCH_TOKEN_BUDGET` (default 250000) and `RESEARCH_MODEL_GROUP`
(`high`, the main model, or `low`).

## Analyze and compile modes

**`--mode analyze`** (default) answers a question. It reads the primary
scope (`--folder`, `--file`: the case) in full, works out the issues it
raises, searches the reference scope (`--reference-folder`,
`--reference-file`: the law, the policy, the standard) for each issue from
several angles, including counter-evidence and exceptions, reads each cited
provision in full and follows its cross-references. Every finding carries
verified quotes.

**`--mode compile`** is exhaustive: every file in scope is read in full, the
values the question asks for are extracted from each, and merged into one
table. Values that disagree between files are kept side by side as
conflicts, each with its source — never averaged or silently picked. The
table is also saved as CSV and Markdown next to the job.

`--domain` is `general` (default), `financial`, or `legal`.

## Legal research and the edition of a law

With `--domain legal`, each legal document found in the reference scope is
an **authority** with its number, issue and effective dates, and whether it
is a consolidated text. The job chooses the edition it relies on explicitly
and says why (in force, superseded by another document in your files,
amended), judged only against the documents in your index. When it cannot
tell — two editions of the same law and nothing in the question or the
files decides — it stops with `needs_clarification`, prints the candidate
editions with their file ids, and waits for the file id (or the law's
number, e.g. `31/2024/QH15`) of the edition to use:

```bash
cremind docs research continue 3f9c2e1b7a40 --answer edition=k7m2xq9a --follow
```

A legal result ends with "Not legal advice": it is a reading of your
documents, not a lawyer's opinion.

## Clarifications and confirmations

A job stops to ask (exit code 2) instead of guessing. The printed text states
the question, the candidates and the answer keys; `continue` takes each as
`--answer KEY=VALUE`:

| Status | Asked when | Answer |
|--------|------------|--------|
| `needs_clarification` | a folder name matches several folders, or none | `--answer scope_folder=<folder path>` (the case/compile folder) or `--answer reference_folder=<folder path>` (the law/policy folder) — the question says which |
| `needs_clarification` | the edition of a law is ambiguous (legal) | `--answer edition=<fid>` |
| `needs_confirmation` | files in scope cannot be read (encrypted, photos awaiting the vision model, not indexed yet, …) | `--answer confirm=true` to go on without them (listed as gaps); `false` stops |
| `needs_confirmation` | the estimate exceeds the token budget | `--answer confirm_budget=true` (stops at the budget, `partial`), or raise this job's budget: `--answer budget=600000` |
| `interrupted` | the server restarted mid-job | `continue JOB` with no answer resumes from the checkpoint |

`true`/`false` become booleans; every other value is sent as text.

## What the dossier contains

- **Coverage** — every file in scope, `read`, `partial` or `unread`, with the
  reason (`encrypted`, `legacy_format`, `awaiting_vision`, `too_large`,
  `not_indexed_yet`, `budget`, `time`, …). A conclusion is only as good as the
  files actually read.
- **Authorities and the edition used** (legal), with why.
- **Findings per issue** — each with its provision and verified quotes, every
  quote followed by its `[doc:…]` citation token. Quotes that did not match
  their source are dropped and counted ("rejected quotes").
- **The compiled table** (compile) with its conflicts and artifact files.
- **Gaps and notes** — what could not be established, and why the job
  stopped early if it did.

Page 1 is the summary; long dossiers have more pages (`status JOB --page N`,
or `--all-pages`). Check any token with `cremind docs cite '[doc:…]'`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | `complete` or `partial` — or, without `--follow`, still running |
| 2 | waiting for you: `needs_clarification`, `needs_confirmation`, `interrupted` — the question is printed, answer with `continue` |
| 1 | `failed` or `cancelled`, or an error (feature off, job not found, busy, bad filter) |

## Global flags

All subcommands accept the root-level `--json` flag, right after `cremind`
(`cremind --json documents research status 3f9c2e1b7a40`): it prints the job
(status, progress, the whole dossier) and the rendered text. Progress lines
go to stderr, the result to stdout.

## Subcommands

### `cremind docs research run`

**Purpose.** Start a research job.

```bash
cremind docs research run QUESTION [--mode analyze|compile] [--domain general|legal|financial]
    [--folder F]... [--file FID]... [--reference-folder F]... [--reference-file FID]...
    [--follow/-f] [--wait N]
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--mode` | `analyze` | `analyze` a question, or `compile` every file in scope into a table. |
| `--domain` | `general` | `legal` adds edition selection; `financial` or `general`. |
| `--folder` | whole index | Primary scope: a folder, matched loosely (case, accents, typos), subfolders included. Repeatable. |
| `--file` | — | Primary scope: a file id or `[doc:…]` token. Repeatable. |
| `--reference-folder` | — | Where the law, policy or standard lives (analyze). Repeatable. |
| `--reference-file` | — | A reference file id or token (analyze). Repeatable. |
| `--follow`, `-f` | off | Wait for the result, printing a progress line per change (phase, files done, current step). |
| `--wait` | 0 | Seconds the start request may wait for the job (the server caps it, about 4 minutes). |

Without `--follow` it prints the job id and its first progress, and exits 0.

### `cremind docs research status`

**Purpose.** Show a job: its progress, the question it asked, or its result.

```bash
cremind docs research status JOB [--page N | --all-pages] [--follow/-f]
```

| Flag | Meaning |
|------|---------|
| `--page` | Dossier page of a finished job (default 1, the summary). |
| `--all-pages` | Print every page. |
| `--follow`, `-f` | Wait until the job finishes or asks something, printing progress. |

### `cremind docs research continue`

**Purpose.** Answer what a job asked, or resume an interrupted job.

```bash
cremind docs research continue JOB [--answer KEY=VALUE]... [--follow/-f]
```

`--answer` (`-a`) is repeatable; the keys are the ones the job printed. On a
running or finished job it just shows the job.

### `cremind docs research cancel`

**Purpose.** Stop a job. What it found so far stays readable with `status`.
Exits 0 once the job is stopped (or had already finished).

```bash
cremind docs research cancel JOB
```

### `cremind docs research list`

**Purpose.** This profile's recent jobs, newest first: id, status,
mode/domain, when, question. The newest 50 are kept.

```bash
cremind docs research list [--limit N]
```

## Examples

### Compile the MKT-report folder

```bash
$ cremind docs research run "Compile the business results: revenue, cost and profit per quarter" \
    --mode compile --domain financial --folder MKT-report --follow
Research job 3f9c2e1b7a40 started (compile/financial).
3f9c2e1b7a40: running · Reading files · 3/14 · Reading MKT-report/Q3-2025.xlsx (3/14)
…
[Documentation Search · research] job 3f9c2e1b7a40 · complete · compile/financial · …
```

If two files report different Q3 revenue, both values appear under
conflicts with their citations.

### Analyze client ABC's land dispute

```bash
cremind docs research run "Giải pháp cho tranh chấp đất đai của khách hàng ABC theo Luật Đất đai" \
    --domain legal --folder "Clients/ABC" --reference-folder "Luat" --follow
```

If `Luat` holds the 2013 land law and the 2024 law, the job asks which
edition applies (exit 2) and prints both with their file ids; answer with
`cremind docs research continue 3f9c2e1b7a40 --answer edition=<fid> --follow`.
If some case files are scanned PDFs not yet read, it asks first; answer
`--answer confirm=true` to go on without them (they are listed as gaps).

## Troubleshooting

**`ResearchBusy`** — another job is running for this profile. Follow it
(`status JOB --follow`) or `cancel JOB` first.

**`DocumentsUnavailable`** — Documentation search is off or has no index
yet: `cremind docs enable`, then `cremind docs status -f`.

**`InvalidFilter`** — a scope flag names something invalid; the message says
which.

**`partial` with "stopped at the token budget"** — narrow the scope, or raise
the budget: `cremind tools set-var documentation_search RESEARCH_TOKEN_BUDGET=500000`.

**Many `unread` files** — they are not indexed yet or need the vision model
(`cremind docs caption --consent-vision`); `cremind docs files --status error`
says why a file failed. Files edited just before the job show as
`not_indexed_yet` when sync is paused (`cremind docs resume`) or their
re-index took too long: wait for `cremind docs status` to show idle, then
`continue` the job.
