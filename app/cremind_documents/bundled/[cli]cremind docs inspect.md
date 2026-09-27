---
description: "Check what Documentation search actually indexed from ONE of the user's own files with `cremind docs inspect FILE` (a path, a file id or a [doc:…] citation token): whether its text was read or only its file details (metadata only), scanned PDF pages waiting for OCR, transcribed or failed, image descriptions, embedding readiness, and why content is missing with how to fix it (vision model, consent, reindex); `--text` / `--all` print the stored passages in source order. Use it when a search misses a file you know is there, or the agent says a file has no readable text. Not for Cremind's own documentation (that is cremind_documentation_search)."
---

# `cremind docs inspect` — what the index holds for one file

A file can be indexed and still give search nothing to read: a scanned PDF
whose pages wait for OCR is indexed by its name, path and dates only.
`cremind docs inspect` shows what **Documentation search** actually stored
for one file — the summary the web UI shows as a file's **Indexed content** —
and, on request, the stored text itself. It only reads the index: nothing is
extracted, transcribed, embedded or sent to a model. Turning indexing on is
in `cremind docs`; searching in `cremind docs search`.

## Usage

```bash
cremind docs inspect FILE [--text | --all]
cremind --json docs inspect FILE [--text | --all]
```

`FILE` is one of:

- a **path**, absolute or relative to the current directory (made absolute,
  then looked up in the index; the server checks it, so when the CLI runs on
  another machine give the server's path);
- a **file id** such as `k7m2xq9a` (from `cremind docs files`, `find` or
  `search`);
- a **citation token**, `'[doc:k7m2xq9a]'` or `'[doc:k7m2xq9a#3f9c2e1b]'`
  (quote it in the shell; the old `[ud:…]` form works too). The whole file
  is shown, not only the cited passage.

| Flag     | Meaning |
|----------|---------|
| `--text` | Also print the first page of stored passages (30), in source order. |
| `--all`  | Print every stored passage, paging through the whole file. |

## Reading the summary

```text
$ cremind docs inspect Laws/decree-45.pdf
decree-45.pdf · Blocked
  Only file details were indexed. 8 pages are waiting for OCR: no Specialized Vision Model is chosen (Settings → LLM Providers).
  file:      Laws/decree-45.pdf
  path:      /home/ann/Documents/Laws/decree-45.pdf
  id:        k7m2xq9a (cite it as [doc:k7m2xq9a])
  kind:      pdf · file status indexed
  readable:  0 passages · 0 characters
  segments:  0 text · 0 OCR · 0 image descriptions · 1 metadata
  pages:     8 total · 0 native text · 8 scanned · 0 OCR done · 0 blank · 8 waiting for OCR · 0 OCR failed · 0 unreadable
  embedding: ready · 1 of 1 chunk embedded
  indexed:   2026-09-27 14:03
  revision:  0f3a9c1d2b4e5f60
  why:
    - 8 pages are waiting for OCR: no Specialized Vision Model is chosen (Settings → LLM Providers).
      fix: choose a Specialized Vision Model in Settings → LLM Providers (cremind llm model-groups set --vision PROVIDER/MODEL --vision-enabled)
```

The first line is the file's status:

| Status | Meaning |
|--------|---------|
| Indexed content | Everything was read (every page read or transcribed). |
| Partly indexed | Some text is stored; `why` says what is missing (pages waiting for OCR, a size limit, damaged pages). |
| Metadata only | Only name, path, type and dates, on purpose: video, encrypted, a cloud placeholder, a secret-looking file. |
| Blocked | Nothing readable yet, for a reason you can fix: no vision model, no consent, the daily quota, a reader not installed. |
| Waiting to be indexed, Indexing… | Not read yet. |
| Failed | Extraction failed; `why` gives the error. |
| Index status unknown | A scanned PDF indexed before page coverage was recorded; it is re-read. |
| Not available | Nothing readable. |

`readable` counts text, OCR and image-description passages — never the
metadata card. `pages` (PDFs) counts pages with native text, scanned pages
and what became of each. `embedding` other than `ready` means meaning-based
search does not find every passage yet; keyword search already does.
`revision` changes when the stored text changes.

A reason with something to do has a `fix`: choose the vision model
(Settings → LLM Providers), consent to it
(`cremind docs caption --consent-vision`), re-read the file
(`cremind docs reindex FID` — it also resumes unfinished OCR), or install a
missing reader (`cremind features install documentation_search`, then
reindex). A reason without a `fix`
resolves itself (a quota that resets, a later OCR batch) or is about the file
itself (damaged pages, a size limit).

## Stored text: --text and --all

```text
── stored text: passages 1–30 of 212 ──

[1] p. 1 · Chapter 1 · [doc:k7m2xq9a#3f9c2e1b]
The main AI challenges we face are hallucination, …

[2] p. 2 · OCR · [doc:k7m2xq9a#77a01c4e]
Điều 12. …

── 182 more passages: cremind docs inspect k7m2xq9a --all prints the whole file ──
```

Each passage shows its number, its place (a page, lines, `Điều 12, khoản 2`,
…), its heading, its type when it is not body text (`OCR`, `image
description`) and its citation token. A passage longer than a page is joined
back into one. With no passages, the file's metadata card is printed. If the
file is re-indexed while `--all` reads it, reading starts over once (a
warning on stderr); a second re-index exits 1.

## When the file is not in the index

Each of these exits 1 with a one-line reason on stderr:

| Message | What to do |
|---------|------------|
| `Not in the indexed folder` | Only your working directory is indexed — never another profile's folder or Cremind's system folder. |
| `Excluded from indexing` | An exclude rule skips it: `cremind docs excludes list`. |
| `Not indexed yet` | Follow the sync: `cremind docs status -f`. |
| `Removed from the index` | The file was deleted or moved. |
| `Search my documents is off for this profile` | `cremind docs enable`. |
| `k7m2xq9a: No such file.` | The id or token is not in this profile's index. |

A folder is refused: inspect one file at a time.

## JSON output

`cremind --json docs inspect FILE` prints `fid`, `name`, `rel_path`, `kind`,
`source`, `status`, `path` (for a path), `summary`, `metadata` (the card and
the document's title, author, pages, …), `revision` and `total_segments`;
with `--text` or `--all` also `segments` (`token`, `index`, `type`,
`heading`, `locator_label`, `text`) and `next_cursor`, and with `--all`
`restarted`. `summary` holds `state` (`complete`, `partial`,
`metadata_only`, `unavailable`, `unknown`), `badge`, `headline`, `readable`,
`segments`, `pages`, `reasons` (`code`, `message`, `action`) and
`embedding`. A failure prints its `state` or `error` and `message` instead,
and exits 1.
