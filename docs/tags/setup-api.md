# Setup API (Cremind ↔ browser, Cremind ↔ Connect)

> Moved here from the cremind-tag firmware repository (`docs/setup-api.md`) with the hardware
> runtime (`app/tags/runtime/`). `cremind tags tools …` is the runtime's CLI (formerly
> `cremind-tag …`); the normative protocol documents are the pinned contract's
> (`app/tags/runtime/protocol/pinned/docs/`).

The wire contract for simple device setup ([connect-setup.md](../../app/tags/runtime/protocol/pinned/docs/connect-setup.md)).
Three groups: the **profile API** the Settings page and `cremind tags` use (JWT),
the **bootstrap API** Connect uses during a setup session (setup capability),
and the **connector additions** a worker uses once it has its credentials
(`CremindTag`, [connector-api.md](connector-api.md)).

Conventions: timestamps are ISO 8601 UTC with milliseconds; byte strings are
lower-case hex; ids are strings. Errors are Cremind's
`{"error": <code>, "message": <sentence>, "detail": <sentence>, …}`. Every
mutation accepts `Idempotency-Key: <uuid>` (or `"idempotency_key"` in the
body): a retry with the same key returns the first answer (409
`idempotency_key_reused` if the body differs).

## 1. Profile API (JWT, the caller's profile only)

### 1.1 Objects

**Device**

```json
{"id": "d0c1…", "binding_id": "b3a9…", "kind": "gateway|bridge|tag", "name": "Kitchen",
 "device_id": "8a9f…(32 hex)", "short_id": "1A2B3C4D",
 "state": "pairing|paired|ready|offline|recovery_pending|removal_pending|reconciling",
 "paused": false, "generation": 1, "fw": "0.2.0", "board": 19,
 "last_contact_at": "…"|null, "battery_mv": 2900|null, "rssi": -61|null,
 "capacity": {"max_tags": 20, "assigned": 3}|null,           // bridges; a gateway that serves tags itself
 "serves_tags": true|false|null,                             // gateways (null for bridges and tags)
 "fontpack_ok": true|null,                                   // bridges
 "bridge_id": "d0c2…"|null,                                  // tags: the bridge, or the gateway, it connects through
 "delivery": {"pending_count": 2, "displayed_revision": 17, "desired_revision": 18,
              "clear_required": false, "status": "ok|pending|clear_pending|failed"}|null,  // tags
 "affected_tag_ids": ["d0c3…"]|null}                         // bridges: tags assigned to it
```

`state` `offline` is derived: paired or ready, but no contact for 2 minutes
(gateway: no worker heartbeat; bridge/tag: no report).

**Where a tag lives.** A tag's parent (`bridge_id`) is a bridge, or its own
gateway: a gateway whose worker reports `tag_links` > 0 in its inventory
(protocol.md §11, the nRF52840 gateways) connects to tags on its own radio —
its worker runs the tag's session itself — so it `serves_tags` and carries a
`capacity` (`max_tags`: how many tags it serves itself; `assigned`: how many
tags have it as their parent). Bridges then only extend the range. A gateway
that reported `tag_links` 0 (older firmware; the nRF52832 gateway), or never
reported it, has `serves_tags: false` and `capacity: null`: every tag it
serves needs a bridge. An inventory without `tag_links` (the worker has not
talked to the gateway yet) keeps the last known values. A gateway's
`affected_tag_ids` stays `null`: removing it removes its whole connection.

**Connection** (one per gateway = one worker)

```json
{"id": "<companion id>", "name": "Desk gateway",
 "status": "setting_up|connected|offline|paused|recovery_pending|removal_pending",
 "paused": false,
 "computer": {"installation_id": "…", "name": "DESKTOP-ABC", "platform": "windows|macos|linux",
              "version": "0.2.0", "last_seen_at": "…"}|null,
 "gateway": Device, "bridges": [Device], "tags": [Device],
 "last_seen_at": "…"|null, "created_at": "…"}
```

**Setup session**

```json
{"id": "…", "operation": "connect_gateway|recover|probe",
 "state": "waiting_for_connect|waiting_for_approval|waiting_for_confirmation|redeeming|connecting|completed|cancelled|expired|failed",
 "expires_at": "…", "created_at": "…",
 "computer": {"installation_id", "name", "platform", "version"}|null,
 "verification_phrase": "amber orbit lantern tidal"|null,
 "gateway": {"device_id", "short_id", "fw", "usable": true}|null,
 "native_approved": false, "browser_confirmed": false,
 "companion_id": null, "operation_id": null,
 "error": {"code", "message"}|null}
```

Order: `waiting_for_connect` → (Connect binds) `waiting_for_approval` → (native
approval with the gateway) `waiting_for_confirmation` → (browser confirms the
computer, gateway and phrase) `redeeming` → (Connect redeems) `connecting` →
(claim done + first heartbeat) `completed`. A `probe` session completes as soon
as Connect binds (it only tells the page which Connect answers on this
computer). Sessions expire 5 minutes after creation unless redeemed.

**Operation** (discovery, pairing, unpair, recovery…)

```json
{"id": "…", "kind": "discovery|pair_bridge|pair_tag|unpair|recover_gateway|release_gateway|claim_gateway",
 "state": "queued|running|succeeded|failed|cancelled|pending_device",
 "stage": "…", "stage_detail": "Waiting for the tag to wake"|null,
 "device": Device|null, "error": {"code", "message"}|null,
 "created_at": "…", "updated_at": "…"}
```

### 1.2 Endpoints

| Method | Path | Body → answer |
|---|---|---|
| GET | `/api/tags/connections` | → `{simple_setup, connections: [Connection], computers: [computer], active: {sessions: [Setup session], operations: [Operation]}}` |
| POST | `/api/tags/setup-sessions` | `{operation, server_url, companion_id?}` → 201 `{session, launch_url}` (`launch_url` only here) |
| GET | `/api/tags/setup-sessions/{id}` | → `{session}` |
| POST | `/api/tags/setup-sessions/{id}/confirm` | `{}` → `{session}`; 409 `not_approved` before native approval |
| DELETE | `/api/tags/setup-sessions/{id}` | → `{session}` (cancelled); 409 `already_redeemed` |
| POST | `/api/tags/discovery` | `{role: bridge|tag, setup_code, gateway_id?, duration_s?}` → 201 `{discovery}` |
| GET | `/api/tags/discovery/{id}` | → `{discovery}` |
| POST | `/api/tags/pairings` | `{discovery_id, candidate_id, name?}` → 201 `{pairing}` |
| GET / DELETE | `/api/tags/pairings/{id}` | → `{pairing}` (also an import's) |
| POST | `/api/tags/imports` | `{tag_id, name?, gateway_id?}` → 201 `{pairing}`: a tag enrolled with the hardware tools (see **Import**) |
| POST | `/api/tags/devices/{id}/unpair` | `{}` → `{operation, device}` |
| POST | `/api/tags/devices/{id}/pause` / `resume` | `{}` → `{device}` (gateway: the whole connection) |
| POST | `/api/tags/devices/{id}/move` | `{bridge_id}` → `{operation}`: a ready tag onto its own gateway (while that `serves_tags`) or a ready bridge of that gateway; 409 `not_movable`, `candidate_not_eligible`, `bridge_full` |
| POST | `/api/tags/devices/{id}/test` | `{}` → 201 `{delivery}` (a test card) |
| POST | `/api/tags/recoveries` | `{companion_id, server_url}` → 201 `{recovery, session, launch_url}` |
| GET | `/api/tags/recoveries/{id}` | → `{recovery}` |

**Discovery**

```json
{"id": "…", "role": "tag", "short_id": "1A2B3C4D",
 "state": "scanning|found|not_found|failed|cancelled", "started_at": "…", "expires_at": "…",
 "candidates": [{"id": "c1", "gateway_id": "<companion id>", "bridge_id": "<device id>"|null,
                 "bridge_kind": "gateway|bridge", "bridge_name": "Hall"|null, "rssi": -61, "seen_at": "…",
                 "capacity": {"max_tags": 20, "assigned": 3}|null, "eligible": true, "reason": null|"bridge_full"}],
 "recommended": "c1"|null, "error": {"code", "message"}|null}
```

For a bridge a candidate is the gateway that heard its beacon (`bridge_kind`
`gateway`, `bridge_id` null). For a tag, a candidate is what heard it in setup
mode: a ready bridge of the profile (`bridge_kind` `bridge`), or the gateway's
own radio when the gateway serves tags (`bridge_kind` `gateway`, `bridge_id`
the gateway's device id, `bridge_name` its name — the connection title the page
shows, `capacity` the gateway's). The search listens on every such place: the
worker's discovery arguments list the gateway's own `gw-…` hardware id among
the `bridges`, and the worker reports `{tag_id, bridge_hw_id, rssi}` with that
id for a tag its radio heard. A candidate whose device serves all the tags it
can is listed with `eligible: false`, `reason: "bridge_full"`. `recommended` is
the strongest recent signal among eligible candidates. Pairing onto the gateway
gives the worker `{tag_id, bridge_hw_id: <gateway hw id>, bridge_id: <gateway
device id>}` and makes the gateway the tag's parent.

**Pairing**: an Operation plus `"role"`, `"first_tag": bool` (the profile's
first tag: the page offers "Send this profile's activity", on by default).

**Import** (`import_tag`): a tag enrolled over SWD with the hardware tools
(`cremind tags tools tag enroll`, protocol v1) has no setup label and cannot
take a pairing grant. The import names it by its tag id (8 hex digits); the
gateway's worker takes the tag's secret and panel from the hardware tools on
its own computer, keeps the secret (and a sealed copy in the vault, entry
`{role: tag, proto: 1, secret, tag_id, bridge, epoch, board, panel, …}`),
assigns the tag's v1 `K_epoch` on the gateway's own radio (or the first ready
bridge with room) and clears it — the clear proves the secret is the tag's.
Only the owner of the gateway's computer may import (the admin on the server's
own computer, the enrolling profile on its desktop): 403 `import_not_allowed`.
The binding exists from the start, keyed by a stand-in device id (the tag id,
little-endian, then 12 bytes of `SHA-256("cremind-tag/v1-tag" ‖ tag id)`;
`identity_pub` all zeros, `info.proto` 1), so a tag binds once per server
(409 `already_paired`); a failed or cancelled import removes it. Followed with
`GET /api/tags/pairings/{id}` like a pairing; worker failures `not_enrolled_here`
(the tools there have no such tag) and `panel_unsupported` (its firmware drives
no display). Removing an imported tag has no release (the worker drops the key;
the tag keeps its enrollment), and a recovery restores it from the vault.

**Recovery**: an Operation plus `"companion_id"` and
`"devices": [{"id", "kind", "name", "state": "pending|rekeyed|recovery_pending|failed"}]`.

Error codes: `simple_setup_disabled` (403), `setup_code_invalid`,
`setup_code_wrong_role` (422), `no_gateway`, `gateway_required`,
`gateway_offline`, `no_ready_bridge` (409), `device_owned` (409),
`candidate_not_eligible` (409), `bridge_full` (409), `not_found` (404),
`session_expired` (410), `not_approved`, `already_redeemed` (409),
`invalid_tag_id` (422), `import_not_allowed` (403), `already_paired` (409).

`no_ready_bridge` (a tag's search): nothing can take a tag — no unpaused
gateway serves tags itself and none has a ready bridge. The message says which:
"Your gateway cannot reach tags itself: add a bridge first, and wait until it
shows Ready." or "Your gateway is paused: resume it first." (`no_gateway`:
no gateway is connected at all). `bridge_full`: the chosen gateway or bridge
holds as many tags as it can (a pairing still running onto it counts).

## 2. Bootstrap API `/api/tag-setup/v1/` (Connect, during a session)

`Authorization: CremindSetup <session_id>.<token>` on every call (the token
from the launch link). After `bind`, every call also carries a **proof**: in
the JSON body (`"proof"`) for POST, in `X-Cremind-Connect-Proof` for GET:

```
proof = Ed25519(installation_sk, "cremind-connect/v1/" ‖ action ‖ 0x00 ‖ session_id ‖ 0x00 ‖
                server_nonce ‖ 0x00 ‖ SHA-256(canonical JSON of the body without "proof"))
```

`action` ∈ `bind, poll, approve, redeem, fail`; `bind` uses an empty
`server_nonce` (it is created by the bind). Canonical JSON: keys sorted, no
spaces, UTF-8 (`json.dumps(body, sort_keys=True, separators=(",", ":"))`).

| Method | Path | Body → answer |
|---|---|---|
| POST | `sessions/{id}/bind` | `{installation: {id, public_key, computer, platform, version}, proof}` → `{session: {id, operation, state, expires_at}, server: {installation_id, name, origin, authority_pub, authority_id}, profile: {name, id}, verification_phrase, server_nonce, recover: {companion_id, gateway_device_id, gateway_name}|null}` |
| GET | `sessions/{id}` | → `{state, native_approved, browser_confirmed, error}` |
| POST | `sessions/{id}/approve` | `{gateway: {device_id, ik, fw, proto, board, owner_state, gen, authority_id|null, challenge}, proof}` → `{state}` |
| POST | `sessions/{id}/redeem` | `{idempotency_key, controller_pub, credentials: {hardware_sha256, content_sha256}, proof}` → `{companion_id, credentials: {hardware_id, content_id}, operation_id, profile, server}` |
| POST | `sessions/{id}/fail` | `{code, message, proof}` → `{state}` |

- A second `bind` from the same installation key returns the same answer; from
  another key 409 `already_bound`.
- `approve` refuses `proto < 2` (422 `v1_firmware`), a gateway owned by another
  authority or bound to another profile (409 `device_owned`), a recover session
  whose gateway `device_id` differs (409 `wrong_gateway`).
- `redeem` needs both approvals (409 `not_confirmed`); repeated with the same
  `idempotency_key` it returns the same ids. Cremind stores only the SHA-256 of
  the two credential secrets Connect generated; the credential ids come back.

## 3. Connector additions `/api/tag-connector/v1/` (worker credentials)

| Method | Path | Kind | Body → answer |
|---|---|---|---|
| GET | `whoami` | any | + `api_version: 2, capabilities, mode, worker: {generation, state, paused}` |
| POST | `lease` | hardware | `{}` → `{lease_id, expires_at, ttl_s: 60, renew_s: 20, state, paused, generation}` |
| GET | `state` | hardware | → `{generation, state, paused, bindings: [{device_id, role, hw_id, generation, state, paused}], revoked: [device_id], operations: [{id, kind, state}]}` |
| GET | `operations/{id}` | hardware | → `{operation: {id, kind, state, stage, args, setup_secret|null}}` |
| POST | `operations/{id}/progress` | hardware | `{stage?, detail?, state?, candidates?, device?, result?, error?}` → `{operation}` |
| POST | `grants` | hardware | `{operation_id, op, device_id, role, ik?, gen_from, challenge}` → `{grant, sig, authority_pub}` |
| PUT | `vault/{device_id}` | hardware | `{expected_version|null, stage: pending|committed, generation, state}` → `{version}`; 409 `version_conflict {version}` |
| GET | `vault` | hardware | → `{entries: [{device_id, version, generation, stage, state}]}` (recovering worker only) |

- `state` of a worker: `active`, `paused`, `recovering`, `removing`. A worker
  whose lease has not been renewed for 60 s starts no new work.
- Operations arrive as commands `run_operation {operation_id, kind}` on the
  existing `commands` long-poll; the worker reports the command's result when
  the operation ends.
- `op` ∈ `claim, recover, pair, rekey, release, maint`. Cremind signs only a
  grant that an open operation of this worker needs, for the binding's current
  generation, with `controller` = this worker's controller key.
- `vault` entries are JSON objects of the worker's own design (roots, `mk`,
  assignments, epoch floors; never a controller key);
  Cremind encrypts them at rest (connect-setup.md §10) and never interprets them.
