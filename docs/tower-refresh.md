# Fixed Tower refresh and recorded-marker source contract v1

This optional integration schedules bounded refresh notices. It does not establish
that a downstream consumer has refreshed its content.

## Normal caller boundary

Use the normal Docket executable, URL discovery and admin bearer token, never a
guest/agent credential. Pass argv directly, not shell-interpolated text:

```text
docket project-markers --json
docket tower-refresh show --json
docket tower-refresh ensure --until 2027-01-22T04:00:00Z --json
docket tower-refresh cancel --json
```

Success: one JSON object on stdout, exit 0. Failure with `--json`: one
`{"version":1,"error":{"code":"unavailable","detail":"safe cause and fix"}}`
object on stdout, exit 1. Nonzero, malformed, incomplete or absent output means
unknown, never absent lease/empty markers. Human output names the ID, state,
cadence, next fire, expiry and queue-only meaning. Help exposes each command.

## Authenticated HTTP

All three routes below require the existing admin bearer, including GET (unlike
ordinary public Docket GETs). No requester-selected source or native access.

* GET `/api/v1/tower/project-markers`: fixed private validated check-in roster.
* GET/POST `/api/v1/tower/snapshot-refresh-schedule`: show/ensure.
  Ensure body is exactly `{"until":"<timezone-aware future ISO deadline>"}`,
  at most daemon now +90 days; no other fields.
* POST `/api/v1/tower/snapshot-refresh-schedule/cancel`: body `{}` only.

Marker success example (all timestamps UTC, exactly six fractional digits):

```json
{"version":1,"source":"docket.project-markers","observed_at":"2026-10-24T04:00:00.000000Z","complete":true,"markers":[{"id":"mil_<64 lowercase hex>","session":"ses_fixture","directory":"/fixture","milestone_at":"2026-10-20T04:00:00.000000Z","milestone_ref":"untrusted technical reference","roster_state":"hold"}]}
```

Only valid paired markers appear; no other roster fields. Valid empty roster or
valid unmarked rows gives complete empty success. Missing/unreadable/invalid or
oversized source gives generic 503 unavailable, not false-empty/truncated success.
Maximum 20 markers, directory <=4096 characters, existing reference <=1000
characters, complete HTTP serialized UTF-8 <=65536 bytes (including JSON escaping).

Marker ID: normalize milestone_at to UTC fixed microseconds ending Z. SHA256
the UTF-8 reference. Construct exactly
`{session,directory,milestone_at,milestone_ref_sha256}`; encode with Python
`json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")`.
ID is `mil_` + lowercase SHA256 hex of those bytes. Projection and event use
the same builder. Parent should consume this ID, not infer new acceptance.

Schedule success example:

```json
{"version":1,"source":"docket.tower-refresh-schedule","observed_at":"2026-10-24T04:00:00.000000Z","changed":true,"schedule":{"item_id":"dkt_tower_refresh","status":"pending","purpose":"project_pages","every_seconds":900,"anchor":"2026-10-24T04:00:00.000000Z","next_fire_at":"2026-10-24T04:15:00.000000Z","until":"2027-01-22T04:00:00.000000Z","lease_expired":false,"cancelled_at":null,"last_occurrence":null}}
```

Absent schedule is null, changed false (including cancel absent); GET always
changed false. Last occurrence is exactly
`{id,status,scheduled_at,queue_result}`; queue_result is null, accepted, full,
disabled or uncertain. Next fire is null for expired/nonpending series. Enqueued
means in-process queue acceptance ONLY, not broker publication, consumption or
page refresh. Failed/full/disabled/crash-reserved attempts are terminal and never
automatically resent. Existing occurrence slots, coalescing, claims and expiry
are reused; the singleton counts against ordinary global/actor/target capacity.
The reserved target is `{adapter:"tower"}`, actor `docket.tower-refresh`.

Ensure extends only to a later deadline, preserving ID, anchor, sequence and
history. Expired-until schedules can renew; unrelated failures cannot silently
reset. Explicit cancellation is permanent even across restart: 409
schedule_cancelled, requiring fresh operator approval outside this interface.
There is no override flag. Cancel does not retract an already queued notice.
Unconfigured publisher refuses initial creation; later disabled publisher
records disabled/undeliverable and advances the existing lease conservatively.

## Fixed CloudEvents

CloudEvents 1.0 JSON source `docket`, datacontenttype `application/json`.
No arbitrary topic, command, action or native recipient. Existing bounded queue
and one Tower daemon thread only; no pool, service, scanner, wake or broker retry.

* Type `docket.snapshot.refresh_due`, subject `ev.docket.snapshot.refresh_due`.
  Envelope ID and Nats-Msg-Id = existing occurrence ID; time = fired_at.
  Exact data: `{version:1,purpose:"project_pages",item_id,occurrence_id,
  scheduled_at,fired_at,coalesced,schedule_until,provenance:"docket_recurring_clock"}`.
* Type `lead.milestone.recorded`, subject `ev.lead.milestone.recorded`.
  Envelope ID and Nats-Msg-Id = marker ID; time = observed_at, NOT marker time.
  Exact data: `{version:1,lead_session,directory,milestone_at,milestone_ref,
  observed_at,roster_state,provenance:"recorded_marker_observation"}`.

The existing minute DeliveryStall pass reuses its validated rows. Historical
initial seeds, hold and done markers are observations, not resumed/current-goal
or accepted-now claims. Additive Checkins ledger reservations precede best-effort
enqueue; crash-reserved observations never automatically resend. No milestone
helper call or sender-state mutation. References are untrusted technical data;
Tower chooses its approved plain label <=100 characters, never a Docket headline.

## Parent Tower responsibilities and proposed install/rollback

Parent filters ONLY explicitly approved origins and periodically repairs via
the authenticated authoritative projection and deterministic IDs. This read
source is required: bus hints alone cannot repair dropped marker observations.
Parent startup/refresh handler renews the same lease when <7 days remain, to
<=now+90 days. No Docket self-renew scanner or agent wake. Cancelled leases are
not renewed; outages past expiry make freshness visibly stale until permitted
renewal. Do not implement a Tower handler in this repository.

Proposed install inputs: reviewed source commit, current service launch inputs,
normal admin token discovery, existing operator-controlled publisher URL/token,
approved-origin mapping and caller contract above. Parent must capture existing
installed source/service/publisher settings and lease/ledger state before install.
Parent must review this exact rollback before any installation. Rollback order:
disable parent renewal and new handlers first. While NEW Docket is still installed,
an authorized conditional rollback must cancel the fixed lease through the
authenticated fixed cancel endpoint and verify terminal state with no open firing
occurrence BEFORE loading prior source/service inputs. The prior scheduler does
not recognize the event-only target; never run it with a pending fixed-series row.
If cancellation/downgrade is not approved or cannot be verified, keep the service
stopped and ask the parent rather than run old code with pending event-only intent.
Cancellation remains permanent until fresh operator approval; never automatically
recreate the lease. Preserve all occurrence history and the additive marker ledger;
never wipe/replay them. Rolling back code is not retraction of queued/published
notices. No schema migration or pinned kit-binding hash refresh is needed. These
are proposed instructions only: no cancellation or runtime effect is authorized now.

Parent coordinates ONE natural 15-minute snapshot proof after install approval:
read lease/projection, wait for its already scheduled occurrence (no new replay
timer), correlate occurrence ID/Nats-Msg-Id through handler and resulting page
snapshot; distinguish queue receipt, broker acceptance and actual page proof.

## Supplemental enrolled project index

`docket project-index --json` uses normal Docket discovery and the existing admin
bearer for `GET /api/v1/tower/project-index`. No selectors or alternate credentials
are accepted. `docket project-index --help` describes the command.

Success is one JSON object, exit 0: exactly `version: 1`,
`source: "docket.project-index"`, `observed_at` (UTC six-fraction Z), `complete: true`,
and `leads`. Each lead has only `session`, absolute `directory`, `roster_state`
(`active|hold|done`), and `activity`. Markerless enrolled rows are included. This is
the complete enrolled roster, NOT a whole-project catalog; do not enroll Nod for it.
Limits are 20 rows and 65536 UTF-8 bytes including HTTP JSON escaping; the existing
private roster reader also rejects source files larger than 256000 bytes.
Invalid, missing, unreadable, nonprivate or oversized sources return HTTP 503;
there is no truncation or false-empty fallback. JSON CLI failures exit 1 with
`{"version":1,"error":{"code":"unavailable","detail":"..."}}` (safe detail).

Activity is exactly `{"state":"unknown"}` or, for example,
`{"state":"busy","sample_at":"2026-10-24T00:00:00.000000Z","age_seconds":42,"basis":"docket.running_sample"}`.
Busy uses only the existing volatile completed-evidence sample with matching
session/directory/native-goal identity and healthy manager. Original sample age
must be 0 through 120 seconds before truncation to an integer. Reads never renew
sample time or query native status; restart, stale, failure, changed binding,
hold/done or missing evidence means unknown, not idle. `observed_at` is response
time, not sample time. No busy-start, last-work or acceptance inference is made.
This read-only addition changes no clock, lease, holds, history or sampling policy.
