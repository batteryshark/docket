# Delivery status and conservative recovery

Docket wakes an agent with a message; it does not execute the message's work,
run arbitrary commands, or notify the principal. Watches remain built-in and
read-only. The panel lists/checks/stops work and requests core-authorized recovery;
it is not a delivery adapter. [Service maintenance](services.md) is separate from
message recovery. The instructions here do not authorize live checks on a held host.

## Read lifecycle and delivery separately

`status` is local schedule lifecycle. `delivery.outcome` is evidence about one
intended wake. Neither a successful API call nor a terminal schedule proves that
an agent performed the requested work.

- `pending`: no stronger delivery outcome is established; not a receipt of execution.
- `admitted`: positive provider acceptance, not transcript consumption.
- `delivered`: matching saved intake. Current OpenCode requests require
  the exact frozen request ID, `type: user` and exact text in the bound target's
  transcript. ID alone, a mismatched message, an inbox listing or shared-database
  visibility alone is not that proof. Historical evidence is marked `legacy`.
  For the optional [Pi companion adapter](pi.md), only a valid consumed receipt
  with `saved_exact` intake earns delivered; admission/claim/injection do not.
- `failed`: a failure without established consumption/possible-dispatch uncertainty;
  not permission to create a replacement wake.
- `uncertain`: possible acceptance, lost visibility or insufficient legacy evidence.
  Missing status does not prove non-delivery.
- `cancelled`: delivery projection records cancellation/local unsent stop where
  supported. Inspect consumption facts and retraction separately; this is not undo.
- `not_applicable`: recurring parent with no open occurrence. Inspect its separate
  `delivery_summary` and occurrences instead of treating the parent as consumed.

A recurring `completed` parent does not mean every occurrence was consumed.
`coalesced_slots` counts missed slots not separately delivered, not successful
deliveries. Summary counts are bounded views, not an exhaustive audit. An
unresolved admitted/uncertain occurrence holds the series; recovery does not
reanchor, relatch a watch, add count, restart a series or replay old occurrences.

The shared `delivery` projection includes `unit_id`, `request_id`, `outcome`,
`phase`, `facts` (`admitted`, `claimed`, `injected`, `consumed`, `cancelled`),
`execution`, `evidence`, safe `issue`, `check_issue`, `receipt_issue`,
`last_checked_at`, `stale`, `namespace_verified`, `same_daemon_recovery` and
advisory `recovery` eligibility/refusal. Positive facts are retained; null means
unknown, not false. Claim/injection are unknown where the adapter cannot prove
them. **Execution is always `unknown`**; an assistant ACK is separate evidence.
An unavailable check does not erase earlier admission or consumption.

Public request/admission/inbox identity display accepts only Docket's canonical
`msg_` plus 32 lowercase hexadecimal digits, for example
`msg_0123456789abcdef0123456789abcdef`. This is a local display rule, not a claim
about the provider's full ID grammar. Opaque legacy IDs stay private for supported
status-only reconciliation; hiding one never replaces it or permits resend.
Missing/malformed projections or receipts mean unknown/unavailable, never proven
non-delivery. CLI text displays `unknown` for a missing outcome.

## Inspect first

Use returned IDs from your own installation. The following syntactically valid
IDs are examples, not existing records. On Unix, from the source root:

```sh
bin/docket list --status all --json
bin/docket list --status all --outcome uncertain --json
bin/docket occurrences dkt_0123456789abcdef --json
bin/docket occurrences --status undeliverable --json
```

On Windows, use the recorded source root and venv console interpreter described
in [services](services.md), replacing `bin/docket` with `& $console -B -m docket`.
The default `list`/`status=pending` includes `pending`, `firing`, `admitted` and
`ambiguous`; it does not hide claimed or uncertain work. `outcome` is an additive
CLI/HTTP filter, not a lifecycle alias.

## Reconcile: status only

Reconcile reads the configured provider and updates local receipt/schedule
evidence; it does **not** send or replace the wake. It is a POST because local
evidence can change. Use the exact one-shot item, or the identified occurrence
for a watch/recurring item (the parent item recovery route refuses these).
For actionable occurrence recovery, use only the parent's current
`open_occurrence.id`, not a historical row chosen by guesswork.

```sh
bin/docket reconcile dkt_0123456789abcdef --json
bin/docket reconcile occ_0123456789abcdef --occurrence --json
```

If a send may have begun, including a timeout, conflict, malformed reply, sending
crash or provider error after the durable begin-send marker, **status only** is
safe. Even a crash between that marker and actual transport is conservative
possible dispatch. Absence from inbox/transcript cannot reset it. Do not resend
with a new ID, force, another target or another client. No automated client loop.

## Retry: one core-authorized pre-dispatch attempt

Retry is not a general resend button. The core must prove eligibility: no
possible dispatch, no admitted/consumed/cancelled fact, intact original identity
and (once prepared) frozen envelope, original target validation, available budget/capacity, and
the exact one-shot or current open occurrence. Eligibility in a projection is
advisory; the mutation rechecks it.

Only after inspecting that evidence and obtaining operator authorization:

```sh
bin/docket retry dkt_0123456789abcdef --json
bin/docket retry occ_0123456789abcdef --occurrence --json
```

The scheduler later performs at most **one additional pre-dispatch attempt per
unit lifetime**, using the same immutable ID, body, caller, urgency, target,
directory and binding. It does not reset the bounded automatic attempt budget
(eight pre-dispatch attempts); duplicate pending authorizations coalesce. A spent
explicit attempt does not gain another automatic budget.

Current OpenCode and Pi bindings do not establish a verified durable provider namespace:
`namespace_verified` is false, including paired mode. A prepared request can
retry only while its preparing daemon still owns the unchanged binding snapshot.
Restart, ownership loss or binding change makes prepared-request resend unsafe
(`backend_unverifiable`); use status only. A reserved request that never reached
binding/envelope preparation may still prepare its original ID after restart
where core evidence proves no send began. Attempted legacy rows without a complete
saved envelope cannot retry. Never edit state to change these decisions.

### HTTP contract and refusal handling

Reads remain loopback-only. Mutations require the existing Docket bearer token;
let the CLI/private client discover it without echoing it or placing it in argv.
Do not borrow another client's credentials. All recovery requests have an empty
JSON object (or empty body); no ID/body/target/schedule override is accepted.

```text
GET  /api/v1/items?status=all&outcome=uncertain
GET  /api/v1/items/{id}
GET  /api/v1/items/{id}/occurrences
GET  /api/v1/occurrences?status=undeliverable
POST /api/v1/items/{id}/reconcile
POST /api/v1/items/{id}/retry
POST /api/v1/occurrences/{id}/reconcile
POST /api/v1/occurrences/{id}/retry
POST /api/v1/items/{id}/cancel
```

Recovery body:

```json
{}
```

Reconcile returns 200 on a completed check, or structured 409/503 refusal/check
failure with current evidence where available. Retry **202 means authorization,
not delivery**. Preserve `error.code` and the returned `item`/`occurrence` projection,
including on 409/503; do not turn these into success or discard them. CLI `--json`
preserves structured error payloads and exits 1 on refusal (0 on success).
Invalid bodies get 400; missing units 404; missing authentication 401.

Typical reasons and safe interpretation:

- `acceptance_unknown`, `already_admitted`, `already_consumed`: check status;
  never resend. Consumed still does not establish execution.
- `backend_unverifiable`, `backend_changed`: original binding is not safe for retry.
- `legacy_envelope_unknown`, `legacy_identity_unknown`: do not reconstruct a wake.
- `cancelled_identity`, `schedule_advanced`, `schedule_finished`: do not restart
  or replay a stopped/advanced schedule.
- `in_flight`: do not overlap operations; timeout is not permission to resend.
- `retry_budget`, `attempt_budget`, `limits`, `not_ready`: do not reset counters
  or create a replacement item to evade the refusal.
- `unavailable`, `refused`, `bad_target`, `archived`, `wrong_target`, `conflict`,
  `lost`: inspect current facts and eligibility; the label alone cannot authorize retry.
- `invalid_receipt`, `legacy_identity_unverified`: display/evidence limitations,
  not proof that an earlier send did not happen.

## Stop a schedule, not history

```sh
bin/docket cancel dkt_0123456789abcdef --json
```

Inspect `schedule_stopped` separately from `retraction.outcome`: `not_sent`,
`retracted`, `too_late` or `uncertain`. Local stop intent prevents later
begin-send/retry and preserves identity/history. With possible dispatch, the
current OpenCode read/delete/read contract does not prove a retained cancellation
barrier: absence can remain `uncertain`; proven consumption is `too_late`.
A late consumed fact can remain visible after local lifecycle cancellation.
HTTP success is not “message removed”; terminal cancellation refusals retain
structured 409 evidence. Stopping/uninstalling the service is not retraction.

## MCP and panel

The actual stdio MCP tools are `docket_timer`, `docket_alarm`, `docket_recurring`,
`docket_watch`, `docket_list`, `docket_occurrences`, `docket_cancel`.
`docket_list` accepts `status` (not an `outcome` argument);
`docket_occurrences` accepts optional item `id` and `status`;
`docket_cancel` requires item `id`. For example, a tools/call params object:

```json
{"name":"docket_occurrences","arguments":{"id":"dkt_0123456789abcdef","status":"undeliverable"}}
```

There are **no MCP reconcile/retry tools** in this source. Use the documented
CLI/HTTP operations, not invented tool names. MCP calls the same local HTTP API;
it does not create a new provider or host capability.

Panel status check/retry delegates to the same core decisions. History is bounded
and optional failures must not hide outstanding work. API users can opt into these
fixed compact reads; default API fields, ordering and behavior remain unchanged:

```text
GET /api/v1/items?status=outstanding&view=panel
GET /api/v1/items?status=terminal&view=panel
GET /api/v1/occurrences?status=all&view=panel
GET /api/v1/items/{id}?view=panel
```

Outstanding uses up to 200 rows ordered by `fire_at,id`. Terminal items and
occurrences each have an independent 40-row window, ordered by actual
`updated_at DESC,id DESC`, not scheduled time. Collections include
`window: {view, limit, order, truncated}`; reaching exactly 40 rows alone does not
mean truncation. Missing/invalid window metadata means completeness is unavailable.
Compact fields may report `fields_truncated`; optional unavailable/truncated history
must be warned about, never silently called complete. No totals, pagination,
caller-selected fields/order/limits or exhaustive audit are promised.

Default delivery remains the current OpenCode transport: generic older service discovery
or principal-consented own-client paired desktop transport, with no silent fallback
from a broken binding. Default queue proof is within the owning host's active
drain; urgent steer is at a model-step boundary, not mid-token interruption.
This does not establish continuing-task guarantees or existing-session wake
parity with other agent products. The optional [Pi adapter](pi.md) is selected per
target without changing that default or adding fallback. Bounded native Windows
Docket queue saved intake is verified, not busy-steer integration, execution or
unattended availability. A transient busy check may need one later authorized
same-caller/key status; possible sends remain status/reconcile-only, never a new
ID or blind resend. Stale/incomplete locks fail closed: no public lock repair or
cancellation bypass. These recovery interfaces do not imply Codex/Claude
existing-session wake parity.
