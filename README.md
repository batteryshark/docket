# Docket

<p><a href="assets/workstack-mark-full.png"><img src="assets/workstack-mark.png" alt="Docket comic mark" width="160" style="max-width:100%;height:auto;"></a></p>

Reviewed comic mark, locally wired only; not a product capture, native acceptance or release clearance.
[Small-use Tiny32](assets/workstack-mark-32.png) is a separately designed composition, not a shrunk badge.

A bounded local scheduler that wakes OpenCode sessions with timers, alarms,
interval recurrence and built-in watches. Includes CLI, loopback HTTP API and
stdio MCP. The optional OpenChamber panel lists/removes items; it does not
deliver wakes. Python 3.11+, SQLite, standard-library runtime.

**Local review staging only.** Original owned source/documentation is MIT under
[LICENSE](LICENSE); [RIGHTS.md](RIGHTS.md) excludes outside dependencies, vendor
tools, models, reference/media and binaries and preserves their notice obligations.
The reviewed comic mark is locally wired only. The selected panel capture below is included. Other operational
deployment records and images are intentionally not distributed.

## Install and run

With Python 3.11+ from a stable checkout, no dependency install is needed:

```sh
python3 -m docket serve
```

This starts a real local scheduler/API, not a dry run. Ctrl-C stops it. The
default is `http://127.0.0.1:8768`; non-loopback binding is refused. Optional
editable installation in an isolated virtual environment is `python3 -m pip
install -e .` and provides `docket`; this requires pip and Hatchling/build
tooling, separately obtained. No install/build was run for staging.

The source launcher `bin/docket` defaults to `python3.13`; set `DOCKET_PYTHON`
to an available Python >=3.11 if using it. `python3 -m docket` avoids that
launcher-specific default. macOS `service install/start/stop/restart/status/
uninstall` commands manage a user LaunchAgent, not OpenChamber or OpenCode.
Install starts the job; never run it alongside `serve`. Service installation
pins the running Python's absolute executable (requires >=3.11, an existing
executable file), using `-m docket daemon` and the package parent as its working
directory. It does not call `bin/docket` at startup. `DOCKET_PYTHON` selects the
interpreter only when entering through that source launcher; with `python -m`,
the invoked Python is authoritative. Interpreter symlinks are retained for venvs.
Source/package/interpreter relocation requires a reviewed reinstall.
Stopping prevents due-time delivery; uninstall
does not erase state or receipts. Do not run any service command for validation.

The generated plist uses `plistlib`; spaces, `&`, `<` and Unicode are data, not
shell/XML fragments. It persists only PATH (with standard executable directories
appended), HOME, USER, LOGNAME, resolved absolute DOCKET_HOME and
DOCKET_DELIVERY_HOST. State defaults to `~/.docket`; overrides must be nonempty
absolute or tilde-expanded absolute paths, not relative paths. Logs and daemon
state use the same resolved directory. Delivery mode defaults to `auto`; accepted
values are `auto`, `opencode`, `openchamber-paired`, `openchamber-unpaired`.
Enrollment still takes precedence as described below; this is not a pairing
bypass. Invalid state/mode/interpreter or unrepresentable plist values refuse
installation before service calls. No whole environment, token, credential,
PYTHONPATH or unrelated setting is copied. Changes require reviewed reinstall;
no existing runtime state is migrated.

## Usage (after explicit host setup)

`TARGET_SESSION` and `ITEM_ID` below are operator-provided placeholders, not
bundled live identities. The target must exist on the delivery host.

```sh
python3 -m docket timer --in 20m --session "$TARGET_SESSION" --reason "check progress"
python3 -m docket alarm --at 09:00 --session "$TARGET_SESSION" --reason "review"
python3 -m docket recurring --every 15m --count 4 --session "$TARGET_SESSION" --reason "check again"
python3 -m docket list
python3 -m docket occurrences
python3 -m docket cancel "$ITEM_ID"
```

Recurrence needs `count` (1–100) or an `until` within 90 days, with interval
at least 60s. Missed intervals coalesce rather than replaying every slot.
Watches need `until`; checks are `file_appears`, `file_stable`, `process_ended`,
`url_answers`, `session_idle`. File stability means a quiet metadata window,
not semantic producer completion; URL checks have no custom headers and cannot
redirect to another origin. Items cannot execute commands/scripts. Context
is data, not instructions. `--directory` pins target location. Missing,
archived or wrong-location targets are refused or become undeliverable.

## Admission, consumption and cancellation

Default delivery uses `queue`, `resume: true`; urgent uses `steer` at a model
step boundary, not mid-token interruption. A queued wake does not guarantee
continuation of an arbitrary multi-step task or separate drain.

HTTP admission is **not consumption**. Each occurrence persists a stable
message ID before sending; `delivered` requires that ID in the transcript.
Unconfirmed retries reuse the ID. Confirmed admissions that later disappear
are lost/undeliverable, not silently reissued. Ambiguous outcomes remain visible.
An admitted occurrence blocks the next recurrence until resolved.

Cancel stops the item/series and attempts to retract its durable admission ID,
including the sending window. Retraction is best-effort: consumed, missing,
unreadable, lost or failed-drop outcomes do not claim undo. Cancellation keeps
occurrence history. Admission-time wake lateness can differ from final lateness
after queue consumption.

## Host setup and pairing limits

Generic unpaired discovery uses the older official OpenCode service metadata
and its runtime basic credential, restricted to loopback. Merely sharing a
transcript database or delivering to an idle session does not prove ownership
of a busy turn. Host/API compatibility must be reviewed before real scheduling.

Optional own-client OpenChamber pairing requires explicit principal consent;
see [PAIRING.md](PAIRING.md). It grants broad session API access, not a scoped
wake-only capability. Paired mode uses only Docket's binding, rediscovering the
desktop loopback port; it does not borrow another client's credential. Missing,
unreadable, expired, revoked or mismatched binding fails visibly without an
old-service or unauthenticated fallback. Enrollment state latches host selection.

The own-record guard is **not host-enforced authentication/capability proof**.
Optional-auth loopback hosts may accept missing/bogus bearers; a guard/request
race remains. Real revocation remains unproven. Fake-host refusal tests do not
establish live revocation. Never destroy the last credential just to test it.

## API, MCP and state

API reads are local; mutations require the Docket bearer token. Metadata is
`GET /api/v1/meta`; item/occurrence listings are under `/api/v1/items` and
`/api/v1/occurrences`. Default pending listings include firing, admitted and
ambiguous states. `python3 -m docket mcp` exposes stdio JSON-RPC tools for
timer/alarm/recurring/watch/list/occurrences/cancel via the running local API;
it does not register itself in any host config. `DOCKET_URL`/`DOCKET_TOKEN`
override discovery; protect them as private configuration.

State under `~/.docket/` (`DOCKET_HOME` override) includes SQLite, token,
server metadata and logs. Pairing adds private binding/enrollment records.
Keep this state out of source control. Back up SQLite consistently, including
WAL semantics, and preserve bindings. Never copy an old DB over a live one:
it can discard receipts or replay stale work. Recovery requires coordinated,
stopped-service review. The optional archive overlay is read, never modified;
unreadable overlay is not interpreted as no archived sessions.

Fleet can supervise a deliberately configured daemon; Tarmac can supply quota
observations to a separate router. Docket itself neither reserves quotas nor
dispatches arbitrary worker processes or contacts the principal directly.

## Optional panel and validation safety stop

See [openchamber/README.md](openchamber/README.md) for the source-only extension.
No bundle/icon or extension activation is supplied here.

![Real full Docket panel at 9:47 PM, Outstanding 0 and Undeliverable 1, service 0.3.0](assets/screenshots/q88-manual-srgb-docket.png)

Owner-provided real full panel read at 9:47 PM: **0 outstanding and 1 undeliverable**, not an empty healthy scheduler. The service 0.3.0 warning says the installed service upgrades separately and the panel does not restart it. This is not delivery, pairing, restart or packaged-extension acceptance. The approved sRGB derivative intentionally color-converted the original RGB; it is included byte-for-byte without another conversion.

**Do not run full discovery, browser, CDP, e2e or evidence scripts.** The inherited
browser-suite containment issue is unresolved: a browser updater escaped the
previous isolation attempt. The prior reviewed full-suite result was 143/145,
with browser-startup failures, not green; it is not a staging result. Browser
test sources are retained (with synthetic paths), not removed to hide failures.
Only separately inspected, selected offline tests in a temporary HOME with
network/process guards and <=120s watchdog are appropriate for this lane.
No current-host delivery, pairing, revocation, restart or full-suite acceptance
is claimed by this package.
