# Tower notices

Docket can publish two CloudEvents to the [Tower](https://github.com/batteryshark/tower) bus. This is off unless
`DOCKET_TOWER_NATS_URL` is set, so existing installs and tests do not connect,
read a token, or enqueue anything. Setting the variable is a later operator
step; this document does not enable it.

| Variable | Meaning |
|---|---|
| `DOCKET_TOWER_NATS_URL` | Opt-in. `nats://127.0.0.1:port` or `nats://[::1]:port` only. No DNS, userinfo, path, or query. Unset or any other host means off. |
| `DOCKET_TOWER_TOKEN_FILE` | Bus token path. Default `~/.tower/secrets/nats.token`. |

The token file must be a regular file, owned by this user, mode `0600`, and not
a symlink. The path is not canonicalized: each ancestor is opened with
`O_NOFOLLOW`, so a symlink anywhere in the path drops the notice. A bad file
drops the notice. The token is never logged, and event text cannot choose the
URL, the token, the subject, or a command.

## What is published

Both events are CloudEvents 1.0 structured JSON, source `docket`, at most 64 KiB.
`Nats-Msg-Id` is the event `id`, so a retry of the same notice is a duplicate.

| Subject | When | Id |
|---|---|---|
| `ev.docket.timer.fired` | A timer, an alarm, a recurring occurrence, or a matched watch is actually sent | Item id for a timer or alarm; occurrence id otherwise |
| `ev.lead.delivery.stalled` | A new delivery-stall episode is reserved | The existing epoch key (`sha256(scope:epoch)`) |

Fire data: `item_id` and `timer_id` (the same item id), `kind`, `occurrence_id`,
`target_session_id`, `directory`, `reason`, `created_by`, `scheduled_at`,
`fired_at`, and `delivery_outcome` when that attempt has already observed one
(admission, consumption, retry, or failure). That field is the first observed
result of the attempt, not a certified final delivery. A retry publishes the
same id and is best-effort; the bus may keep the first copy. Unmatched watches
and cancelled intent are not fires.

Stall data: `lead_session`, `directory`, `project` (the directory's final
component, not goal text), numeric `minutes_since_landed`, and a fixed detail
that this is an observed content baseline, not certified acceptance.
`minutes_since_landed` is minutes since the persisted evidence `observed`
timestamp, not the continuous running streak (that streak restarts after an
idle gap and is still what the inbox text describes). Already alerted epochs
are not backfilled. The chief-inbox file, its key, and frozen retry are
unchanged. Docket does not wake the chief; Tower owns that DATA wake.

## How it runs

The scheduler hook only `put_nowait`s onto one in-process queue of 32. It does
not wait, connect, or read the token. A full queue drops that notice. One
daemon thread in the existing process does one bounded NATS exchange (INFO,
CONNECT with the token, SUB reply inbox, HPUB) and stops within 2 seconds and
96 KiB of socket I/O. A PubAck counts only when `stream` is `TOWER` and `seq`
is a positive integer. The thread does not retry, create or update a stream,
or spawn a process. Watch checks stay on the existing check executor; bus I/O
cannot occupy those threads. There is no second pool, service, or durable
outbox. Process exit drops anything still queued. `TowerPublisher.close()`
joins the thread for tests and is not a service stop.
