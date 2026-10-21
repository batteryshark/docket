# Using Docket

Canonical reusable chief/lead instructions, kept in this project only. Do not
write global prompts, settings, host configs or other projects from these examples.
The service must already be running. Use the [service runbook](docs/services.md)
for platform setup, stop/start/uninstall and safe maintenance; never blindly restore a DB.
Use [delivery status and recovery](docs/delivery-status.md) for exact-unit status
checks and bounded core-authorized pre-dispatch retry. These instructions do not
authorize running a safety-held host. Coordinate Docket maintenance
around pending items. Do not open private state without explicit authorization.
Historical Mac acceptance included a principal-managed app update and fresh
wake/ACK plus installed relay removal. Do not restart the app automatically or
treat earlier Docket-service approval as permission.

## Tools command

From your Docket checkout:

```
bin/docket mcp
```

Stdio MCP. Newline-delimited JSON-RPC, and Content-Length framing. The
process calls the local HTTP API. Leave `DOCKET_URL` and `DOCKET_TOKEN` unset
unless you are pointing at an isolated test service. Discovery reads
`server.json` under the selected Docket state root (`~/.docket` on Unix,
`%LOCALAPPDATA%\Docket` on Windows, or explicit `DOCKET_HOME`). For Windows,
invoke `-B -m docket mcp` with the recorded venv console Python from its source root.

Tools: `docket_timer`, `docket_alarm`, `docket_recurring`, `docket_watch`,
`docket_list`, `docket_occurrences`, `docket_cancel`.

There are no MCP reconcile/retry tools or MCP `outcome` list argument in this
source. CLI `list --outcome`, `reconcile ID [--occurrence] [--json]` and
`retry ID [--occurrence] [--json]` use the core HTTP contract; see the
[recovery runbook](docs/delivery-status.md). Possible dispatch is status-only,
never a force/new-ID resend. Retry 202 means authorization, not delivery.

Prefer tools when the host already has this MCP server. Equivalent CLI examples
from the Docket checkout (replace session/id/path placeholders):

```sh
bin/docket timer --in 20m --session ses_… --reason "check progress" --by chief
bin/docket alarm --at 09:00 --session ses_… --reason "morning check" --by chief
bin/docket recurring --every 15m --count 8 --session ses_… --reason "check progress" --by lead
bin/docket watch --check file_stable --path /abs/report --until 6h --session ses_… --reason "report ready" --by lead
bin/docket watch --check process_ended --pid 123 --until 2h --session ses_… --reason "job ended" --by lead
bin/docket list
bin/docket list --status all
bin/docket occurrences dkt_… --status undeliverable
bin/docket cancel dkt_…
```

## Optional Pi target

Use the [Pi companion setup](docs/pi.md) only with an existing live, capable,
persistent receiving Pi and operator-provisioned private `pi-companion.json`.
Docket does not launch Pi. CLI create commands take `--adapter pi --session pi/<id>`;
the four MCP create tools and HTTP create bodies accept optional `adapter: "pi"`
and `session_id: "pi/<id>"`. Missing config refuses Pi operations. Omitted adapter
keeps OpenCode/paired delivery unchanged; there is no fallback.

Bounded native Windows Docket queue/no-reply use has verified `consumed` /
`saved_exact` intake, not execution, provider readiness or unattended availability.
Urgent maps to Pi steer, but Docket busy-steer integration is not established.
Admission, claim and injection are not saved consumption. A transient busy status
may need one later authorized same-caller/key status; after possible send use
status/reconcile only, never resend or mint a replacement ID. Cancellation cannot
bypass the shared lock or undo irreversible claim. See the Pi guide for namespace,
capacity, security and runner limits; never read private mailbox/transcripts or
repair locks as a shortcut.

## Context and setter

- `session_id` is the agent session to wake (`ses_…` by default, `pi/<id>` only
  with explicit Pi selection). Pi rejects directory pins and `session_idle` watches.
- `directory`, when set, must still match at fire time. A moved session is a
  wrong target.
- `created_by` is a self-reported label, not an authenticated id. Pass one
  (`chief`, `lead-downloads`) so the setter cap is not the shared anonymous
  bucket. An omitted label uses that bucket. A blank label is rejected.
- `context` is JSON data quoted in the wake. A `command` key inside `context`
  is still data. Docket does not run it.
- `urgent` is a JSON boolean (CLI: `--urgent`). Default/`false` uses native
  queue; `true` uses steer at a model-step boundary for OpenCode, not mid-token generation.
  The string `"false"` is rejected. See proof limits below.

## Recurring: expiry or count

`every` is at least 60 seconds and at most 90 days. A `count` (1 to 100) or
an `until` (at most 90 days out) is required. Both may be set; whichever
comes first stops the series. The first fire is one interval from now unless
`start` overrides it with a duration or ISO timestamp, not a bare clock time.

`count` is attempted wakes, not slots that elapsed while the machine slept.
Missed slots coalesce into one late wake. The next fire is the next slot
strictly in the future. Cancel the item id to stop later wakes.

## Built-in watches

Checks: `file_appears`, `file_stable`, `process_ended`, `url_answers`,
`session_idle`. `until` is required. One wake, then the watch is done.

| Check | Bound |
|---|---|
| File, process, session poll | at least 5 seconds, default 15; `until` at most 7 days |
| URL poll | at least 30 seconds, default 60; `until` at most 24 hours |
| `file_stable` window | 10 seconds to 6 hours, default 60; must be able to finish before `until` |
| URL fetch | `http` or `https`, no user, password, or custom headers; redirects stay on the original host and port; 5 second hop, 8 second deadline |
| `session_idle` | does not read message history; already idle matches; an outage is not idle |

A file check stats one absolute path. It does not read the file. A process
watch records pid and start stamp at create time; the process must be running
then. A missing or archived watched session ends `session_idle` as
undeliverable.
`file_stable` means a quiet window, not semantic producer finality; it cannot
know future writes, even after a successful wake.

Caps, enforced at create: 20 pending items for one target, of which at most 5
recurring and 8 watches; 5 pending recurring and 8 watches for one setter
label; 200 pending items on the service.

## List and cancel

`docket_list` defaults to `pending`, which includes `firing`, `admitted`, and
`ambiguous`. Other statuses: `all`, `admitted`, `ambiguous`, `delivered`,
`completed`, `cancelled`, `undeliverable`, `expired`.

`docket_cancel` takes the item id. Cancelling a recurring item or a watch
stops later wakes. Cancel asks the host to drop a known admission id, including
one persisted while sending before `inbox_id` was saved. A prompt the host
already consumed, an unreadable admission, or a retraction the host did not
accept is not reported as removed. A confirmed admission that is already
absent remains uncertain, not a witnessed drop. Inspect local `schedule_stopped`
separately from retraction (`not_sent`, `retracted`, `too_late`, `uncertain`). Cancel
does not delete occurrence rows, does not schedule another wake, and does
not mint a new admission id.

`docket_occurrences` with `status: "undeliverable"` lists wakes that could not
be delivered. Pass `id` to limit that to one item.

## Late and undeliverable

A fire more than 5 seconds after its scheduled time is late. Wake lateness text
is admission-time; final item lateness may include queue consumption wait. It
does not guess why. A coalesced recurring wake names how many
earlier intervals were not delivered separately.

Undeliverable can mean the target was missing, archived, or in the wrong
directory, or proven pre-dispatch delivery failed after bounded retries.
A confirmed admission that later vanishes is delivery uncertainty (`lost`),
not proof of non-delivery or permission to resend. A dead target stops a recurring
series. A pre-dispatch delivery outage that exhausts retries records that occurrence and, if the series still has
budget, continues at the next future slot. The next wake names the miss.

Do not read a shared-database transcript as proof the right host queued the
turn. List the item and its occurrences.

## Never

Docket does not run a command, a script, or a shell, and it does not contact
the principal. A top-level `command`, `script`, `shell`, `argv`, `exec`,
`program`, `notify`, `notification`, or `phone` field is rejected. The wake
is a message. The agent does the work with its own tools. If a permission
blocks that work, tell the principal. Do not ask Docket to notify them.

## Deployment and proof limits

Schedule lifecycle and delivery outcome are separate. Read `delivery.facts`,
safe issue/check evidence and recovery eligibility; missing projections are
unknown, not proven non-delivery. `delivered` is matching saved intake (Pi requires
`saved_exact`), never execution. A completed recurring parent does not mean all
occurrences were consumed, and coalesced slots are not delivered occurrences.

Generic unpaired installations default to the older OpenCode service; queue
semantics only apply when that host owns the turn. Historical Mac 0.3.0 acceptance
covered principal-consented own-client pairing, current-origin port rediscovery,
CLI/MCP/HTTP consumption/ACKs and retained items through upgrade and Docket-service
restart. Admission alone is not delivery; this is not newer source/platform acceptance.

OpenCode native queue proof is natural assistant finish then new user/ACK within the same
active drain. Urgent proof is at a model-step boundary, not mid-token generation;
there is no stronger continuing multi-step guarantee. Cancellation is best-effort
across host-accept races, not an absolute undo; inspect its outcome and occurrences.

Never borrow credentials or seek the historical missing `desktopLocalClientToken`.
Own pairing accepts arbitrary labels/renames and persists the own token before
optional metadata checks. The own-id/active-record revocation guard refuses visibly
with no silent fallback or anonymous retry. Optional loopback auth is not a
host-enforced capability; actual revocation remains unproven. See `PAIRING.md`.

Historical Mac panel acceptance included visual approval and actual installed
health/snapshot/removal, not UI-click automation. The panel uses server-side token
relay, not wake delivery. This is not a fully green suite or whole-goal completion.
Real revocation remains unproven; do not destroy the final credential for a test.
The historical browser-era full suite had two browser-startup failures, not green.
Browser/CDP tests and full discovery remain stopped after installed Chrome's
updater escaped isolation, until containment is independently verified.
Do not run the stopped lane.
