# Docket

Historical Mac 0.3.0 own-client OpenChamber pairing and delivery were accepted; see
[PAIRING.md](PAIRING.md). A generic
installation can run before pairing; pairing requires principal consent.

A small always-on local service that wakes an agent session when a timer, an
alarm, a recurring schedule, or a built-in watch is due. Milestone 2 adds
bounded recurring wakes and cheap read-only watches. Delivery does not treat
an admission as consumed. OpenChamber's archive overlay is checked when that
file is present, and the pending view includes every outstanding state. The
OpenChamber panel lists outstanding items and undeliverable history, and can
remove an item. Historical Mac acceptance included visual approval and installed
relay health/snapshot/remove. This is not current source/platform acceptance or
completion of the whole goal. The panel does not deliver
wakes and needs no delivery callback. See the host-selection limits below.

An optional [Pi companion adapter](docs/pi.md) has verified bounded native Windows
queue saved intake through the normal installed Docket route and published 0.2.0
companion. `delivered` means exact saved intake, not execution. Pi selection is
per target (`--adapter pi`); default OpenCode/paired delivery is unchanged, with
no Pi fallback. See the setup, private-config requirements and availability/proof
limits before use; this is not unattended or universal Windows assurance.

Python 3.11+; Unix core uses the standard library, while Windows declares
`tzlocal>=5.0` and `tzdata`. The service binds to `127.0.0.1:8768` and
will not listen on any other address. Unix state lives in `~/.docket/` (`docket.db`,
`token`, `server.json`; log visibility depends on the service backend). The directory is
mode `0700`. The database, its WAL and SHM files, the token, and `server.json`
are mode `0600`. Windows defaults to `%LOCALAPPDATA%\Docket` with native private
storage guards. `DOCKET_HOME` selects an explicit private state root, not a
migration. See [per-user services](docs/services.md) for platform setup and limits. The token
and the OpenCode service password are read at runtime and are never written to
the repo or to item records.

A process that is already running keeps the code it started with. Historical Mac
0.3.0 acceptance covered own-client delivery, retained items through upgrade and
Docket-service restart, and fresh wake/ACK and installed relay removal after a
principal-managed app update. No automatic app restart is authorized.
That historical acceptance does not certify newer source/platform behavior.

## Start, stop, uninstall

For Linux systemd user units and Windows per-user interactive-logon tasks, use
the [portable service runbook](docs/services.md). It covers persistent source/runtime
requirements, platform state/log locations, uninstall retention and the Windows
installation-root reconfiguration gap. Native Windows corrected restart,
uninstall/reinstall retention and reboot/logon acceptance remain unproven.
These reference commands do not authorize running a safety-held host.

The approved user LaunchAgent is `local.docket`. The plist is
`~/Library/LaunchAgents/local.docket.plist`. Install pins the running absolute
Python 3.11+ interpreter (retaining a virtualenv symlink path), invokes
`python -m docket daemon`, and sets this package's root as WorkingDirectory.
`DOCKET_PYTHON` can select the installer interpreter via `bin/docket`; it is not
stored or reselected at daemon startup. Moving code/runtime or changing captured
settings requires deliberate reinstall; source changes do not update an existing job.

The plist captures only PATH, HOME, USER, LOGNAME, canonical DOCKET_HOME and
DOCKET_DELIVERY_HOST (`auto`, `opencode`, `openchamber-paired`, or
`openchamber-unpaired`). Credentials and unrelated variables are not copied.
Mac install alone accepts an explicit already-absolute state symlink to an
existing dedicated private directory, validates the canonical target with the
unchanged owner/mode guards, and pins that target. Broad permissions, dangling
or relative aliases are refused, not repaired. Use the canonical path for clients;
no state is migrated and ordinary storage still rejects symlink roots.

```
bin/docket service install      # write the plist, bootout any previous job, start it
bin/docket service start        # bootstrap the plist, or kickstart if already loaded
bin/docket service stop         # bootout. The plist stays, so it does not come back until start
bin/docket service restart      # kickstart -k
bin/docket service status
bin/docket service uninstall    # bootout and delete the plist
```

`bin/docket serve` runs the same process in the foreground. Stop it with
Ctrl-C. Do not run `serve` and the LaunchAgent together; they share one port.

These commands manage Docket only, not OpenChamber or a shared OpenCode server.
Coordinate maintenance around pending items; stopping prevents due-time delivery.
Uninstall removes the job/plist, not the retained state. Before an upgrade, take a
private, consistent SQLite backup including WAL (the verified upgrade used the
online backup API), check integrity and item invariants, and preserve the own-client
binding. Never blindly restore a DB or copy an old file over a live database:
restoring can discard newer items/receipts or replay stale pending work. Recovery
requires explicit coordination, a stopped Docket service and reviewed state.

## What you can set

A timer is a delay: `20m`, `in 20 minutes`, `90s`, `1h30m`, or a number of
seconds. An alarm is a local clock time `HH:MM` (today if it is still ahead or
at most a minute past, otherwise tomorrow) or an ISO-8601 timestamp. A
timestamp already in the past fires on the next tick and is reported late.

A recurring item wakes on a fixed interval. `every` is at least 60 seconds.
A `count` (1 to 100) or an `until` (at most 90 days out) is required; both may
be set, and whichever comes first stops the series. The first fire is one
interval from now unless `start` says otherwise. `count` is the number of
wakes attempted. Slots skipped while the machine was asleep do not consume it.

A watch wakes once, when a built-in check matches. It needs an `until`. The
checks are `file_appears`, `file_stable` (the file stops growing),
`process_ended`, `url_answers`, and `session_idle`. Docket does not accept a
command, a script, or a principal notification.

A fire more than 5 seconds after its scheduled time is marked late. The notice's
lateness text is measured at admission; final item lateness may include queue
consumption wait. The wake does not guess why. If a recurring item misses several
intervals, one late wake is delivered for the slot that was owed, the notice says how
many earlier intervals were coalesced, and the next fire is the next slot
strictly in the future. Missed slots are not delivered one by one. Slots after
`until` are not counted as coalesced. When the series is finished, Remaining
is 0, not the unused count.

The default target is an OpenCode session id (`ses_…`); explicit Pi targets use
`--adapter pi --session pi/<id>` with the [Pi setup and restrictions](docs/pi.md).
For OpenCode, Docket checks that the session
exists and is not archived when the item is set and again when it fires. If
you pass a directory, the session's location must still match at both times;
a mismatch is a wrong target. A missing, archived, or wrong target is rejected
at creation. If it becomes invalid before the fire, that occurrence is kept as
`undeliverable`. A dead target stops a recurring series. A delivery outage
that exhausts retries records that occurrence as undeliverable and, if the
series still has budget, continues at the next future slot. The miss is in
`docket occurrences` and is named in the next wake.

Docket does not execute item commands or contact the principal directly.
`context` is delivered as data.
A top-level `command` or `script` field is rejected. A `command` key inside
`context` is still only data. `urgent` must be a JSON boolean. The string
`"false"` is rejected, not treated as true.

If `~/.config/openchamber/sessions-archive.json` exists (or
`$OPENCHAMBER_DATA_DIR/sessions-archive.json`), a session id whose value is a
positive timestamp is archived even when OpenCode's `time.archived` is empty.
A missing file is not an error and does not require OpenChamber to be running.
An unreadable file is treated as unavailable, not as "nothing is archived".
Docket only reads that file. It does not write OpenChamber settings, and the
scheduler does not import the overlay.

## Limits

These are enforced at create time. `GET /api/v1/meta` returns the same numbers.

| Limit | Value |
|---|---|
| Recurring interval | 60 seconds to 90 days |
| Recurring count | 1 to 100, or an `until` within 90 days |
| Pending items for one target | 20, of which at most 5 recurring and 8 watches |
| Pending recurring or watches for one setter | 5 recurring, 8 watches. `created_by` is a self-reported label, not an authenticated id. An omitted label uses the anonymous bucket. A blank label is rejected. Per-target and global caps do not use the label. |
| Pending items on the service | 200 |
| File, process, and session poll | at least 5 seconds, default 15; `until` at most 7 days |
| URL poll | at least 30 seconds, default 60; `until` at most 24 hours |
| File stable window | 10 seconds to 6 hours, default 60 |
| URL fetch | 5 second hop, 8 second deadline, 2 redirects, 2048 byte body |

A file check stats one absolute path in a short-lived child, so a stuck mount
cannot hang the service. It does not read the file. Stability is device,
inode, size, and mtime. A shrink, a rewrite, or a replaced inode resets the
window. Content is not hashed. A `file_stable` watch is rejected when the
first sample plus the stable window cannot finish before `until`.
It detects a quiet window, not semantic producer finality: it cannot know future
writes, even after a successful wake.

Watch checks run on a fixed pool of two threads with a backlog of four. A full
backlog is skipped until the next tick, not queued without bound. A due timer,
alarm, or recurring wake is delivered before those checks and does not wait
on a slow URL, file, or session probe.

A process watch records the pid and the `ps` start stamp at create time. The
process must be running then. Later, a missing pid means it exited. The same
pid with a different start stamp means the pid was reused, which also counts
as ended. If `ps` times out, the watch waits; it does not treat that as ended.

A URL watch fetches `http` or `https` only, with no username, password, or
custom headers. Redirects stay on the original scheme, host, and port. A
redirect that changes host or port, including to another loopback port, is not
followed. The default match is a final 2xx. `expect: any` accepts any
completed response. A timeout, a refusal, a 5xx, or too many redirects is not
a match. The body is discarded.

A session-idle watch reads the session and the active-session list. It does
not read message history. An outage is not idle. A missing or archived
watched session ends the watch as undeliverable. Already idle matches on the
first successful check; the watch does not require having seen it busy.

## Delivery

OpenCode admits a prompt in one call. Docket uses that admission and does not
check "is the session busy?" and then send. The session-idle watch is the
only place that reads the active list, and it does not send.

- Default: `delivery: "queue"`, `resume: true`. Native queue proof is natural
  assistant finish followed by a new user message/ACK within the same active
  drain. An idle session is woken; this is not a stronger guarantee about a
  continuing multi-step task or a separate drain.
- Urgent (`--urgent`): `delivery: "steer"`, `resume: true`. Delivery is at a
  model-step boundary, not during token generation. Mid-token interruption is
  not promised.

Admission is not consumption or execution. Current delivery records preserve a
stable request ID and immutable envelope before sending. For OpenCode, `delivered`
requires a matching saved user transcript message with the exact ID and frozen
text, not ID-only visibility; Pi requires a consumed / `saved_exact` receipt.
Possible dispatch, including an unconfirmed send, is **status-only** for both
adapters: it is never resent. Lost
visibility remains uncertain; a missing receipt does not prove non-delivery.
Only bounded proven pre-dispatch recovery can reuse the original ID/body/target,
subject to core binding/budget checks. See [delivery status and recovery](docs/delivery-status.md)
for exact CLI/HTTP operations, separate lifecycle/outcome facts, cancellation
uncertainty and compact panel read limits. Completed recurrence is not proof
that all occurrences were consumed; coalesced slots are not delivered wakes.

Each recurring wake and each latched watch has its own occurrence id. A later
repeat is a new occurrence, not a retry of an earlier one. Cancel the item id
to stop a series. Cancellation asks the host to retract the durable admission
id, including the sending window before `inbox_id` was saved. Consumed, missing,
lost, unreadable or failed-drop outcomes do not claim removal. Host-accept races
make retraction best-effort, not an absolute undo. Cancelling does not delete the
occurrence row or mint a new wake id.

## Host selection and limits

Before pairing, a generic installation uses `docket/delivery.py`, discovering
the official older OpenCode service from `~/.local/state/opencode/service.json`
and using its runtime basic credential. Non-loopback OpenCode URLs are refused.
Queue guarantees apply only on the host owning the turn; shared-database
transcript visibility or idle delivery does not prove busy-turn ownership.

The actual verified 0.3.0 deployment is instead paired, after principal consent.
`docket/host.py` uses Docket's own client bearer against the current loopback
desktop origin, rediscovering its port; OpenChamber routes to its managed runtime.
Before every paired session request, the own-id guard requires exactly one
matching active client record with no expiry or revocation. Labels are arbitrary;
renaming the same own record does not break delivery. The own token is atomically
saved before optional response metadata checks. Missing, unreadable, revoked,
expired or mismatched binding fails visibly: no silent old-service fallback or
unauthenticated retry, and no borrowing another client's credentials.

That guard is not a host-enforced capability boundary. Optional-auth loopback
may accept no bearer or a bogus bearer; own-record checks and a guard/request
race do not prove host-enforced authentication. Real revocation remains unproven.
The absent `desktopLocalClientToken` blocker is **historical**, not a current
delivery blocker; supported own-client pairing replaced that approach. See
[PAIRING.md](PAIRING.md) for the procedure and its limits.

## CLI

The CLI talks to the running service. Mutations send `Authorization: Bearer`
with the token in `~/.docket/token`.

```
bin/docket timer --in 20m --session ses_… --reason "check the file"
bin/docket alarm --at 09:00 --session ses_… --reason "morning check"
bin/docket recurring --every 15m --count 8 --session ses_… --reason "check again"
bin/docket recurring --every 1h --until 6h --session ses_… --reason "…"
bin/docket watch --check file_stable --path /abs/file --until 6h --session ses_… --reason "download finished"
bin/docket watch --check process_ended --pid 123 --until 2h --session ses_… --reason "job finished"
bin/docket watch --check url_answers --url http://127.0.0.1:9/health --until 30m --session ses_… --reason "up"
bin/docket watch --check session_idle --watch-session ses_… --until 1h --session ses_… --reason "idle"
bin/docket list
bin/docket list --status all
bin/docket list --status undeliverable
bin/docket occurrences
bin/docket occurrences dkt_… --status undeliverable
bin/docket cancel dkt_…
```

`--by` records who set the item. `--directory` pins the session location.
`--context '{"file":"a"}'` is stored and quoted in the wake. `--json` prints
the API body. `--start` on a recurring item is a duration or an ISO timestamp,
not a bare clock time.

## HTTP

Loopback only. Reads are open to loopback clients. Mutations need the bearer
token.

```
GET  /health
GET  /api/v1/meta
GET  /api/v1/items?status=pending|outstanding|firing|admitted|ambiguous|all|delivered|completed|cancelled|undeliverable|expired
GET  /api/v1/items/{id}
GET  /api/v1/items/{id}/occurrences
GET  /api/v1/occurrences?status=undeliverable
POST /api/v1/timers          {"in","reason","session_id","urgent?","context?","created_by?","directory?"}
POST /api/v1/alarms          {"at","reason","session_id", ...}
POST /api/v1/recurring       {"every","reason","session_id","count?","until?","start?", ...}
POST /api/v1/watches         {"check","until","reason","session_id","every?","path?","stable_for?","pid?","url?","expect?","watch_session?", ...}
POST /api/v1/items           kind timer, alarm, recurring, or watch
POST /api/v1/items/{id}/cancel
```

`status=pending` (the default list) is every outstanding state: `pending`,
`firing`, `admitted`, and `ambiguous`. A firing claim is not hidden. Item
lists include an `occurrences` count and, when a wake is in flight, an
`open_occurrence`. A watch that ends without a match is `expired`. A series
that finishes after at least one consumed delivery is `completed`. Request
bodies are capped at 64 KiB. A negative `Content-Length` is rejected, and a
body that does not arrive within 5 seconds is cut off.

## MCP

`bin/docket mcp` is a stdio MCP server (newline-delimited JSON-RPC, and
Content-Length framing). Tools: `docket_timer`, `docket_alarm`,
`docket_recurring`, `docket_watch`, `docket_list`, `docket_occurrences`,
`docket_cancel`. It calls the local HTTP API, so any host that can spawn a
process can use it. It is not registered in OpenCode or OpenChamber config.

```
bin/docket mcp
```

Point the host at that command. `DOCKET_URL` and `DOCKET_TOKEN` override
discovery. Host-neutral setup examples and usage rules for the chief and the
leads are in `AGENT_USAGE.md`. Do not write those examples into a global
prompt, an OpenCode config, an OpenChamber setting, or another project.

## Panel

The listing panel is `openchamber/`. Choose that folder inside your own checkout
after independent review, not from an agent session:

`<your-docket-checkout>/openchamber`

It shows outstanding items and undeliverable history, and it can remove an
item. Removal of an already consumed wake says so and does not claim the wake
was removed. The panel has no authenticated callback to the host that owns
the current turn, and it does not deliver wakes. It does not change the app,
the host, or settings. Its list/history/remove relay keeps the Docket API token
server-side, never in the iframe. Historical Mac acceptance included installed
host-route relay/removal, not UI-click automation. See
[panel documentation](openchamber/README.md) for current scope and limits.

There is no cron expression; recurrence is an interval from an anchor. A file
watch does not hash content, and a URL watch sends no custom headers. There
is no panel delivery relay; paired delivery belongs to the Docket daemon.

## Tests

**Browser safety stop:** do not run full test discovery or browser/CDP checks.
The historical browser-era full suite was not green: two browser-startup
failures occurred before panel code. Installed Chrome's updater escaped isolation;
no further browser-driven jobs until containment is independently verified.
This documentation does not authorize resuming that lane.

`tests/test_panel_relay.py` is part of `discover`. It starts an isolated
Docket on a free port under a temporary `DOCKET_HOME`, a loopback relay, and
headless Chrome with its own user-data directory. It does not open
`~/.docket` and does not restart `local.docket`.

Historical Mac pairing/relay acceptance is not a fully green suite or current
native-platform certification. Real revocation remains unproven; do not destroy
the final credential for a test or declare the whole goal done.

## License and artwork

Original principal-owned code and documentation are [MIT licensed](LICENSE).
The existing comic marks are **AI-generated artwork**, also offered under MIT
to the extent applicable, without a claim of exclusive copyright in AI output.
The [32px mark](assets/workstack-mark-32.png) is a separate composition, not a
downscaled badge. See [RIGHTS.md](RIGHTS.md) for asset scope and outside notices.
