# Docket-owned event outcome journal

This operation runs INSIDE the existing Docket daemon. There is no additional
workload server/service/manager or external side effect. Its sole authority is
recording a bounded summary in one new `docket_event_outcomes` table. Ordinary
Docket tables are not mutated. This is not a general file/exec/notification tool.

## Existing daemon integration

`serve` and `daemon` wire this operation when both event configs and
`--event-journal-config` are supplied. The source seam below is already integrated;
do not add a second server or duplicate the handler.

```python
from docket.event_journal import EventJournal, PATH
journal = EventJournal(server.store.path.resolve(), private_channel_config_ref)
```

Constructor requires an absolute SQLite path and owned0600 channel config beneath
an owned0700 parent; invalid config raises `ValueError`/`OSError`, unavailable
SQLite raises `sqlite3.Error`. No ambient path/token fallback. Initialize once.

Only for exact `PATH == "/v1/actions/record"`, BEFORE the ordinary admin/publisher
route and BEFORE body read:

```python
headers = dict(request.headers)
response = journal.preflight(request.command, request.path, headers,
                             request.headers.get("Content-Length"))
if response is None:
    # Existing daemon's finite socket read timeout; never read an unbounded body.
    raw = request.rfile.read(int(request.headers["Content-Length"]))
    response = journal.handle(request.command, request.path, headers, raw)
# Respond with response.status and JSON response.body; close rejected requests
# without draining unbounded/untrusted request data.
```

`preflight` returns `None` only when authorized/bounded; otherwise a frozen
`JournalResponse(status, body)`. `handle` always returns that response type.
Actual unmodified Tool Stack HttpRuntime posts this internal endpoint:

```text
POST /v1/actions/record
Content-Type: application/json
Content-Length: <bounded exact byte count>
X-Toolstack-Secret: <fresh own Docket channel from explicit private ToolState>
```

```json
{"arguments":{"source":"docket-event-summary-v1","text":"bounded summary"},
 "broker_request_id":123,"caller":{"name":"docket-<32 lowercase hex>"}}
```

The broker's agent-facing route `/v1/actions/docket.record` is a DIFFERENT hop.
The request ID is in the body, not an invented request-ID header. Caller names
are asserted by the authenticated HttpRuntime hop from ScopedBroker's per-instance
caller; no claimed `caller_id`, namespace, role, path or source authority accepted.
The fixed channel namespace scopes IDs to one persistent broker deployment. A
new broker DB deployment needs a new namespace/channel. Names only narrow a
one-row-per-handler quota within that scope; they do not grant read/admin rights.

Envelope keys and arguments are exact; request ID is a positive SQLite-safe int,
not bool/float. Text is 1–512 UTF-8 BYTES. Body max8192 bytes; JSON duplicate keys,
unknown keys, incorrect source/caller metadata and malformed JSON are refused.
Only the installed source is stored. All SQL values are bound parameters.
Intent reservation and row insertion commit in ONE SQLite transaction with
`BEGIN IMMEDIATE`, FULL synchronization and unique namespace/request/caller
constraints. No external I/O occurs within the effect. Same ID/same canonical
envelope returns the SAME receipt; changed body or new ID for the same handler
returns409. Concurrent/restarted requests cannot create duplicates. Other fresh
handler callers may append their own one row; this is not a global one-shot tool.

Success (including an identical replay):

```json
{"status":"recorded","row_id":1,"sha256":"<summary UTF-8 sha256>"}
```

No summary/database contents or credentials in the tool response. Errors are
fixed `{"error":"..."}` objects:401 wrong/missing channel;404 unknown path;
405 non-POST record;411 missing/invalid bounded Content-Length;413 oversized body;
415 non-JSON;400 invalid envelope/arguments/length/transfer encoding;409 ID/body
conflict, reserved handler quota or uncertain intent;503 SQLite unavailable.
No other Docket token authenticates this operation.

## Private channel and installed broker configuration

Generate a NEW random own Docket channel through trusted install code (e.g.
`secrets.token_urlsafe(32)`), never reuse test fixture/client/provider credentials.
Store the raw channel only as `{"docket":{"e_secret":"<fresh value>"}}` in the
explicit owned0600 ToolState file outside model mounts. The daemon config stores
ONLY its hash (never the raw value):

```json
{"version":1,"namespace":"docket-journal-broker-v1",
 "source":"docket-event-summary-v1","channel_token_sha256":"<SHA256 of fresh raw channel>"}
```

Both config and parent must be private. Changing namespace/source/channel is an
explicit deployment/reinitialization, not event input. Empty credentials refused.
Nothing here creates/writes live config or copies a fixture credential.

The normal broker_config_ref v2 contains `mode:"installed"`, explicit descriptor
roots/ToolState, normal timeout/rate limits, and:

```json
{"policy":{"tools":{"docket":{"record":"allow"}}},
 "fixed_args":{"docket.record":{"source":"docket-event-summary-v1"}},
 "data_fields":{"docket.record":["text"]}}
```

Derived native operation is ONLY `toolstack_docket__record`. Model schema excludes
source, paths and channel credentials. Registry port must match the existing
daemon. Freeze descriptor/config fingerprints normally; review mode is `refuse`
for this harmless own-journal-only class. Actual Nod support remains separately
configurable, with no fabricated issuer. No model/live event is authorized here.

## Admin inspection only

`journal.list_outcomes(limit=100, before_id=None)` returns at most100 own-table
rows (descending): `row_id, source, text, text_sha256, created_at_ms`. Invalid
limits/cursors raise `ValueError`. It uses a query-only SQLite connection.
The existing daemon exposes `GET /api/v1/events/outcomes` and
`bin/docket events outcomes --json` behind Docket admin authentication only, never
publisher/channel/anonymous auth. The GET accepts `limit` and `before_id` query
parameters. Treat summary text as untrusted display data. The journal module
itself exposes no channel HTTP read.
