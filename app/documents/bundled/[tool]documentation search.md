---
description: "The Documentation Search (documentation_search) built-in tool and its DEFAULT_TOP_K variable — the maximum number of documents the vector store returns to the relevance judge per search. How to view and change the documentation_search top-k per profile."
---

# Documentation Search Tool (documentation_search)

The **Documentation Search** tool (`tool_id` `documentation_search`) is how the
agent answers questions about Cremind, its skills, the `cremind` CLI, and any
documents the user has added. It is always on (locked) and visible in Settings.

## Tool Variables

| Variable | Type | Default | Meaning |
|----------|------|---------|---------|
| `DEFAULT_TOP_K` | number | `10` | Maximum number of documents the vector store returns to the relevance judge for each search call. |

`documentation_search` has no Tool Arguments.

## Viewing and changing these

Per-profile, three equivalent ways:

- **UI** — Settings → Tools & Skills → Documentation Search.
- **CLI** — `cremind tools set-var documentation_search DEFAULT_TOP_K=20`;
  `cremind tools get documentation_search --json` to read the current value.
- **Agent** — the assistant can run those commands via its Shell Executor.

Changes take effect on the tool's next call — no restart. See `cremind tools`
for the full configuration CLI.
