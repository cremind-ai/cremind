"""Deep research over the user's documents: verified analysis and exhaustive
compilation, run as background jobs.

- :mod:`.types` — modes, statuses, the dossier and the job view;
- :mod:`.context` — what a pipeline runs inside (budgeted LLM, checkpoint);
- :mod:`.windows` / :mod:`.evidence` / :mod:`.coverage` — reading files in
  model-sized windows, checking claimed evidence, the coverage table;
- :mod:`.compile` / :mod:`.analyze` (+ :mod:`.legal`) — the two pipelines;
- :mod:`.jobs` — the job runner, long-poll, delivery, restart recovery;
- :mod:`.activity` — the live Research activity panel;
- :mod:`.render` — the dossier as text for the agent, REST and the CLI.

Modules are imported directly; this package exports nothing, so importing
one piece never drags in the others.
"""
