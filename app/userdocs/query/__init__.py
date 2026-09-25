"""The User Document Search query engine: what the agent tool and the query
API ask of a profile's index.

- :mod:`.terms` — a query → safe FTS5 expressions (quoted terms, the folded
  column for unaccented Vietnamese, a phrase list for identifiers);
- :mod:`.filters` — the one filter schema, the scope it selects, soft boosts;
- :mod:`.lexical` / :mod:`.vector` — the two retrieval lists;
- :mod:`.fusion` — reciprocal-rank fusion, dedupe, grouping;
- :mod:`.engine` — :class:`QueryEngine` and :func:`open_engine` (search);
- :mod:`.catalog` — ``find_files``; :mod:`.reader` — ``read``;
- :mod:`.rerank` — thorough mode's LLM steps;
- :mod:`.render` — budgeted text for the agent, with the tokens it printed.

All of it is synchronous except :mod:`.rerank`; callers run it through
``asyncio.to_thread``.
"""

from app.userdocs.query.engine import Access, QueryEngine, SearchOutcome, open_engine
from app.userdocs.query.filters import FilterError, Filters, parse_filters
from app.userdocs.query.reader import ReadError

__all__ = [
    "Access",
    "FilterError",
    "Filters",
    "QueryEngine",
    "ReadError",
    "SearchOutcome",
    "open_engine",
    "parse_filters",
]
