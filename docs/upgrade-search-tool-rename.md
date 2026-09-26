# Upgrading: the search-tool rename

Applies to the first release after **v0.0.18** that ships Documentation search
(the index of the user's own files), and to anyone who ran a pre-release build
of it (then called "User Document Search" / `userdocs`).

## In one paragraph

Two search tools traded names. The tool id **`documentation_search` used to
mean Cremind's own manual**; it now means **the user's own documents**. The
manual moved to **`cremind_documentation_search`**. Because an old id still
exists with a new meaning, an outdated client would not fail — it would quietly
configure the wrong tool. So the server, the web UI and the CLI are upgraded
together: stored data migrates on the server's first boot, open web UI tabs
must be reloaded, and every machine that runs `cremind` against the server
needs `pip install -U cremind`. Outdated clients are refused where it matters
(426 / 410, below) instead of being allowed to guess.

## Tool ids

| Searches | Before (v0.0.18) | Pre-release builds | Now |
|---|---|---|---|
| Cremind's own manuals (features, settings, the `cremind` CLI) | `documentation_search` | `documentation_search` | **`cremind_documentation_search`** |
| The user's own indexed files (local folder, Google Drive) | — | `user_documents` | **`documentation_search`** |

Display names: *Documentation Search* → *Cremind Documentation Search* for the
manual; *User Documents* → *Documentation Search* for the user's files.

## Function names the model sees

| Before | Now |
|---|---|
| `documentation_search__search_documentation` | `cremind_documentation_search__search_documentation` |
| `documentation_search__read_documentation_section` | `cremind_documentation_search__read_documentation_section` |
| `user_documents__find_files` | `documentation_search__find_files` |
| `user_documents__search` | `documentation_search__search` |
| `user_documents__read` | `documentation_search__read` |
| `user_documents__research` | `documentation_search__research` |

Older conversations still hold the old names in their history. A model that
copies one is dispatched to today's function (dispatch only — no schema is sent
under an old name, and only when that tool is exposed in the run, so an alias
never bypasses the conversation's Search tools choice or a gate).

## Everything else that was renamed

Items marked *pre-release* existed only in unreleased builds of Documentation
search; a v0.0.18 install has nothing to rename there.

| What | Before | Now |
|---|---|---|
| REST API (*pre-release*) | `/api/userdocs/*` | `/api/documentation-search/*` (path for path) |
| CLI group (*pre-release*) | `cremind userdocs …` | `cremind docs …` |
| Optional feature (*pre-release*) | `cremind features install userdocs` | `cremind features install documentation_search` |
| pip extra (*pre-release*) | `pip install "cremind[userdocs]"` | `pip install "cremind[documentation-search]"` |
| Server settings (*pre-release*) | `[userdocs]` in `settings.toml`, keys `userdocs.*` | `[documentation_search]`, keys `documentation_search.*` |
| Live-update topic on the profile event stream (*pre-release*) | `userdocs` | `documentation_search` |
| Usage & Cost `source_kind` (*pre-release*) | `userdocs` | `documents` |
| Database tables (*pre-release*) | `userdoc_sources`, `userdoc_captions`, `userdoc_vision_usage`, `userdoc_citations`, `userdoc_research_jobs` | `document_sources`, `document_captions`, `document_vision_usage`, `document_citations`, `document_research_jobs` |
| Index files (*pre-release*) | `<system dir>/storage/userdocs/<profile uid>/` | `<system dir>/storage/documents/<profile uid>/` |
| Vector collections of the personal index (*pre-release*) | `ud_*` | `doc_*` |
| Citation tokens in answers (*pre-release*) | `[ud:k7m2xq9a#3f9c2e1b]` | `[doc:k7m2xq9a#3f9c2e1b]` |
| Vector collection of the manual | `documentation_search` | `cremind_documentation_search` |
| Manual pages a profile wrote itself | `<system dir>/<profile name>/documents/` | `<system dir>/storage/cremind_documents/profiles/<profile uuid>/` |
| Mirror of the bundled manual | `<system dir>/documents/` | `<system dir>/storage/cremind_documents/shared/` |
| `cremind clean` component for the profile's own Cremind documentation | `documents` / `--documents` | `cremind_documents` / `--cremind-documents` |
| `cremind clean` component for the personal index (*pre-release*) | `user_documents` / `--user-documents` | `documentation_search` / `--documentation-search` |
| Bundled reference for the manual's tool | `[tool]documentation search.md` | `[tool]cremind documentation search.md` |
| Bundled reference for the personal tool (*pre-release*) | `[tool]user documents.md` | `[tool]documentation search.md` |
| Bundled CLI references (*pre-release*) | `[cli]cremind userdocs*.md` | `[cli]cremind docs*.md` |
| Python packages (contributors) | `app.documents` (manual), `app.userdocs` | `app.cremind_documents`, `app.documents` |

## What migrates by itself

On the upgraded server's first boot (Alembic `upgrade head` plus the boot-time
file moves), with no action from anyone:

- **Per-profile tool settings follow their tool**, not their old id: the
  enabled state, `DEFAULT_TOP_K` and the two sub-tool switches that a profile
  set on the manual under `documentation_search` are now on
  `cremind_documentation_search`; pre-release `user_documents` settings are now
  on `documentation_search`. Usage records keep pointing at the tool that ran.
- *Pre-release*: the `userdoc_*` tables, the `[userdocs]` server settings and
  the `userdocs` usage kind are renamed; the index files move from
  `storage/userdocs/` to `storage/documents/`; vector collections created
  under the old `ud_` prefix stay recognised as the index's own.
- The manual's collection is built under its new name from the documents on
  disk, the way the manual is re-embedded on every boot. Manual pages a
  profile wrote move from `<profile name>/documents/` to
  `storage/cremind_documents/profiles/<profile uuid>/`, and the old mirror of
  the bundled manual (`<system dir>/documents/`) is retired.
- **Backups and blueprints made before the upgrade** restore and import with
  the old ids mapped to the new ones.
- **Saved answers keep their sources.** `[ud:…]` tokens in older messages are
  read, numbered and verified exactly like `[doc:…]`; only new answers print
  `[doc:…]`. `cremind docs cite '[ud:k7m2xq9a#3f9c2e1b]'` still resolves.

Nothing needs to be re-indexed and no setting has to be re-entered.

## Deploy server, web UI and CLI together

1. **Upgrade the server first** (installer, `pip install -U cremind` +
   restart, Docker image, or Helm chart). A new CLI pointed at an old server
   would use ids and endpoints that server does not have yet.
2. **Reload every open web UI tab** (and restart the desktop app). The web UI is
   served by the server, so a reload picks up the matching version; a tab left
   open from before the upgrade is an outdated client.
3. **Upgrade every other `cremind` install** that talks to this server —
   laptops, CI runners, cron hosts: `pip install -U cremind`. The CLI inside the
   server's own environment (the one the agent's shell uses) is upgraded with the
   server.
4. **Update scripts** that name either tool id (examples below). Scripts that
   call the REST API directly must also send the client-protocol header on the
   gated writes.

## What an outdated client sees

The server accepts a write whose meaning changed only from a client that sends
`X-Cremind-Client-Protocol: 2` (the updated web UI and CLI do, on every
request). Reads are never blocked.

| An old client… | Gets |
|---|---|
| writes tool settings: any `POST`/`PUT`/`PATCH`/`DELETE` under `/api/tools/…`, or `PUT /api/agents/{tool_id}/enabled` / `PUT /api/agents/{tool_id}/config` | **426** `ClientUpgradeRequired` |
| runs setup: `POST /api/config/setup` | **426** `ClientUpgradeRequired` |
| cleans data: `POST /api/clean` | **426** `ClientUpgradeRequired` |
| calls anything under `/api/userdocs` (any method) | **410** `EndpointRenamed`, with the replacement path |
| reads (`GET /api/tools`, `GET /api/tools/{id}`, …) | the normal answer — in the new vocabulary |

The 426 body:

```json
{
  "error": "ClientUpgradeRequired",
  "message": "This Cremind client is older than the server: tool ids changed meaning (documentation_search is now the user's own documents; Cremind's manual is cremind_documentation_search). Update the web UI / CLI (pip install -U cremind) and retry.",
  "required_protocol": 2,
  "client_protocol": null
}
```

which an old CLI prints as
`server returned 426: ClientUpgradeRequired: This Cremind client is older than the server: …`,
and an old web UI tab as a failed save ("… Upgrade Required"). Nothing was
written.

The 410 body, for `GET /api/userdocs/files/k7m2xq9a/text`:

```json
{
  "error": "EndpointRenamed",
  "message": "/api/userdocs/files/k7m2xq9a/text moved to /api/documentation-search/files/k7m2xq9a/text — update the client (reload the web UI / pip install -U cremind); the CLI command is now `cremind docs`.",
  "replacement": "/api/documentation-search/files/k7m2xq9a/text"
}
```

The setup route is gated because its `tool_configs` payload is keyed by tool
id. Its only callers are the web UI's Setup Wizard and the CLI (`cremind setup
complete`, `cremind profile wizard`); the installer scripts, the Helm chart and
the desktop app's main process never call it, so an install or a Helm upgrade
is not affected.

## Scripts: before and after

Cremind's manual (v0.0.18 scripts):

```bash
# Before
cremind tools set-var documentation_search DEFAULT_TOP_K=20
cremind tools set-leaf documentation_search read_documentation_section=false
cremind clean components --documents --yes

# After
cremind tools set-var cremind_documentation_search DEFAULT_TOP_K=20
cremind tools set-leaf cremind_documentation_search read_documentation_section=false
cremind clean components --cremind-documents --yes
```

Run unchanged, the "before" lines would now tune the **user's own documents**
(the first two) — which is exactly why an outdated CLI is refused.

The user's own documents (pre-release scripts):

```bash
# Before                                         # After
pip install "cremind[userdocs]"                  pip install "cremind[documentation-search]"
cremind features install userdocs                cremind features install documentation_search
cremind userdocs enable --root ~/Documents       cremind docs enable --root ~/Documents
cremind userdocs search "Q3 budget"              cremind docs search "Q3 budget"
cremind userdocs research run "…" --follow       cremind docs research run "…" --follow
cremind tools set-var user_documents \           cremind tools set-var documentation_search \
  RESEARCH_TOKEN_BUDGET=500000                     RESEARCH_TOKEN_BUDGET=500000
cremind clean components --user-documents        cremind clean components --documentation-search
```

Direct REST calls:

```bash
# Before
curl -X PUT "$CREMIND_SERVER/api/tools/documentation_search/variables" \
  -H "Authorization: Bearer $CREMIND_TOKEN" -H "Content-Type: application/json" \
  -d '{"variables": {"DEFAULT_TOP_K": "20"}}'
curl "$CREMIND_SERVER/api/userdocs/status" -H "Authorization: Bearer $CREMIND_TOKEN"

# After
curl -X PUT "$CREMIND_SERVER/api/tools/cremind_documentation_search/variables" \
  -H "Authorization: Bearer $CREMIND_TOKEN" -H "Content-Type: application/json" \
  -H "X-Cremind-Client-Protocol: 2" \
  -d '{"variables": {"DEFAULT_TOP_K": "20"}}'
curl "$CREMIND_SERVER/api/documentation-search/status" -H "Authorization: Bearer $CREMIND_TOKEN"
```

## New with this release: Search tools per conversation

The chat composer has a **Search tools** button that chooses which of the four
search sources the agent may use in one conversation or group room, in a fixed
priority order: Documentation search → Cremind documentation search → Memory
search → Web search. All four are on by default, including in conversations
from before the upgrade. A change applies from the next response and may lower
prompt-cache reuse on it. It only narrows what a profile already has enabled.
From the CLI: `cremind conv search-tools` and `cremind group search-tools`.

## For contributors: the client-protocol marker

- The header is `X-Cremind-Client-Protocol`, the version `2`. The server's
  constants and the gated routes live in
  [`app/middleware/client_protocol.py`](../app/middleware/client_protocol.py)
  (`requires_current_client`); the CLI sends it from
  [`app/cli/client/_base.py`](../app/cli/client/_base.py), which spells the
  constants out rather than importing server code; the web UI stamps it on
  every request to its own backend from
  [`ui/src/services/clientProtocol.ts`](../ui/src/services/clientProtocol.ts)
  (except the cross-origin `/api/tls/*` hand-off, which is never gated).
  `tests/api/test_client_protocol.py` pins the three copies equal.
- Retired URL prefixes answer 410 from
  [`app/api/retired.py`](../app/api/retired.py) (`RENAMED_PREFIXES`).
- The next time an identifier changes meaning, bump the version in all three
  clients' copies, extend `requires_current_client` to the routes that carry
  the identifier, and write a note like this one.
