---
description: "The System File (system_file) built-in tool and its Tool Variables: max readable tokens returned inline, max markitdown file size, and the caps on list entries, search results, grep results, grep file size, and grep match line length. How to view and change each system_file limit per profile."
---

# System File Tool (system_file)

The **System File** tool (`tool_id` `system_file`) reads, lists, searches, and
greps files for the agent. It is a core tool: always on (locked) and visible in
Settings so its limits can be tuned per profile. All its variables are numeric
caps that bound how much data a single call returns.

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `MAX_READABLE_TOKENS` | number | `10000` | Maximum tokens of file content returned inline before falling back to a metadata-only response. |
| `MAX_MARKITDOWN_FILE_SIZE` | number | `10485760` (10 MB) | Maximum file size in bytes that markitdown will attempt to convert; larger files return metadata only. |
| `MAX_LIST_ENTRIES` | number | `100` | Maximum entries returned by `list_files` in one call. |
| `MAX_SEARCH_RESULTS` | number | `100` | Hard cap on results returned by `search_files` (the per-call `max_results` is clamped to this). |
| `MAX_GREP_RESULTS` | number | `100` | Hard cap on match entries returned by `grep_files` (the per-call `max_results` is clamped to this). |
| `MAX_GREP_FILE_SIZE` | number | `5242880` (5 MB) | Maximum file size in bytes that `grep_files` will read; larger files are skipped. |
| `MAX_GREP_MATCH_LINE_LENGTH` | number | `1000` | Maximum characters of a single matched or context line returned by `grep_files`; longer lines are truncated. |

`system_file` has no Tool Arguments.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → System File.
- **CLI** — `cremind tools set-var system_file MAX_LIST_ENTRIES=250`;
  `cremind tools get system_file --json` to read the current values.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
