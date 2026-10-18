# Docket

<p><a href="assets/workstack-mark-full.png"><img src="assets/workstack-mark.png" alt="Docket: a comic alarm-clock mascot holding a reminder card" width="180"></a></p>

**Wake an agent session later, without leaving a polling agent running.**

Docket is a bounded local scheduler for OpenCode sessions: timers, alarms,
interval recurrence and built-in watches. It provides a CLI, a loopback HTTP
API and stdio MCP, with SQLite persistence and a Python 3.11+ standard-library
runtime. An optional OpenChamber panel lists and removes items; it does not
deliver wakes.

## Quick start

From a stable checkout with Python 3.11+:

```sh
git clone https://github.com/batteryshark/docket.git
cd docket
python3 -m docket --help
python3 -m docket serve
```

No dependency installation is needed to run from source. `serve` starts the
**real scheduler**, not a dry run, at `http://127.0.0.1:8768`; non-loopback
binding is refused. Ctrl-C stops it. Review [host setup](#host-setup) before
scheduling: delivery requires a compatible local host and an existing session.

For an installed `docket` command, optionally use an isolated virtual environment
and `python3 -m pip install -e .`. This requires pip and Hatchling build tooling;
the editable install has not been validated in this release preparation.
The source launcher `bin/docket` defaults to `python3.13`; `DOCKET_PYTHON` can
select another Python >=3.11. `python3 -m docket` uses the invoked interpreter.

## Schedule a wake

After host setup, use another terminal. Set `TARGET_SESSION` to an existing
session ID and replace `ITEM_ID` with the ID returned by a create command.

```sh
python3 -m docket timer --in 20m --session "$TARGET_SESSION" --reason "check progress"
python3 -m docket alarm --at 09:00 --session "$TARGET_SESSION" --reason "review"
python3 -m docket recurring --every 15m --count 4 --session "$TARGET_SESSION" --reason "check again"
python3 -m docket list
python3 -m docket occurrences
python3 -m docket cancel "$ITEM_ID"
```

- Recurrence requires `count` (1–100) or an `until` within 90 days, with an
  interval of at least 60 seconds. Missed intervals coalesce rather than replay.
- Watches require `until`. Checks are `file_appears`, `file_stable`,
  `process_ended`, `url_answers` and `session_idle`; see `python3 -m docket watch --help`.
  File stability means a quiet metadata window, not semantic producer completion.
  URL checks have no custom headers and cannot redirect to another origin.
- `--directory` pins the target location. Missing, archived or wrong-location
  targets are refused or become undeliverable.
- Items cannot execute commands or scripts. Context is data, not instructions.
  Docket neither reserves model quotas nor dispatches arbitrary worker processes
  or contacts the operator directly. A woken agent may act on its own instructions.

## What delivery means

Normal wakes use `queue` with `resume: true`; urgent wakes use `steer` at a model
step boundary, not mid-token interruption. Queuing does not guarantee continuation
of an arbitrary multi-step task or a separate drain.

**Admission is not consumption.** Each occurrence persists a stable message ID
before sending. `delivered` requires that ID in the transcript; unconfirmed
retries reuse it. Confirmed admissions that later disappear are lost/undeliverable,
not silently reissued. Ambiguous outcomes remain visible, and an admitted
occurrence blocks the next recurrence until resolved.

Cancel stops the item/series and attempts to retract the admission, including
during sending. Retraction is best-effort: it cannot undo consumed work, and
missing, unreadable, lost or failed-drop outcomes do not claim undo. Occurrence
history is retained. Admission-time lateness may differ from final consumption lateness.

## Host setup

Generic unpaired discovery uses older official OpenCode service metadata and
its runtime basic credential, restricted to loopback. Host/API compatibility
must be checked before real scheduling; merely sharing a transcript database
or delivering to an idle session does not prove ownership of a busy turn.

Optional OpenChamber own-client pairing requires explicit operator consent;
follow [PAIRING.md](PAIRING.md). It grants **broad session API access**, not
wake-only permissions. Paired mode uses Docket's own binding and rediscovers
the desktop loopback port. Enrollment latches host selection; missing, expired,
revoked, unreadable or mismatched binding fails without credential borrowing,
old-service or unauthenticated fallback.

The own-record guard is **not host-enforced authentication or capability proof**.
Optional-auth loopback hosts may accept missing/bogus bearers, and a guard/request
race remains. Live revocation is unproven; synthetic refusal tests are not proof.
Do not destroy the last credential to test revocation.

### Optional macOS service

`python3 -m docket service` supports `install`, `start`, `stop`, `restart`,
`status` and `uninstall` for a user LaunchAgent. **Install starts the job; never
run it alongside `serve`.** Installation pins the running Python executable and
package location, not `bin/docket`. Relocation requires reinstalling.

State defaults to `~/.docket`; `DOCKET_HOME` must resolve to a nonempty absolute
path. `DOCKET_DELIVERY_HOST` accepts `auto` (default), `opencode`,
`openchamber-paired` or `openchamber-unpaired`; enrollment still takes precedence.
The plist persists only PATH, HOME, USER, LOGNAME, resolved DOCKET_HOME and
DOCKET_DELIVERY_HOST, not the whole environment or credentials. Changes require
reinstallation; existing state is not migrated. Stopping prevents delivery;
uninstall does not erase state or history.

## API, MCP and private state

API reads are local; mutations require the Docket bearer token. Metadata is at
`GET /api/v1/meta`, with listings at `/api/v1/items` and `/api/v1/occurrences`.
Pending listings include firing, admitted and ambiguous states.

`python3 -m docket mcp` exposes stdio JSON-RPC tools for creating, listing and
cancelling schedules through the running API; it does not register itself in
host configuration. `DOCKET_URL` and `DOCKET_TOKEN` override discovery; protect
them as private configuration.

`~/.docket/` holds SQLite, token, server metadata and logs; pairing adds private
binding/enrollment records. Keep this state out of source control. Back up SQLite
consistently (including WAL semantics) and preserve bindings. Never copy an old
database over a live one: it can discard history or replay stale work. Recovery
requires coordinated, stopped-service review. The optional archive overlay is
read, never modified; an unreadable overlay is not treated as no archived sessions.

## Optional OpenChamber panel

![Docket panel showing zero outstanding items, one undeliverable item and a service 0.3.0 notice](assets/screenshots/q88-manual-srgb-docket.png)

Real owner-provided panel capture: **0 outstanding, 1 undeliverable**, not an
empty healthy scheduler. The service upgrades separately; the panel does not
restart it. This is not proof of delivery, pairing, restart or packaged-extension
acceptance. The prepared sRGB image is reused unchanged.

The [panel source](openchamber/README.md) is included, but no built bundle or
manifest icon is supplied. **It is not a ready-to-activate extension.**

## Validation and known limits

Do not run full test discovery, browser/CDP, e2e or evidence scripts: a browser
updater escaped an earlier isolation attempt. Browser test sources remain for
containment review. The historical full-suite result was 143/145 with browser
startup failures, not green and not a result of this preparation.

Use only inspected offline tests with a temporary HOME, network/process guards
and a watchdog of at most 120 seconds. No current-host delivery, pairing,
revocation, service restart or full-suite acceptance is claimed here.

## License and artwork

Original principal-owned code and documentation are [MIT licensed](LICENSE).
The existing comic marks are **AI-generated artwork**, also offered under MIT
to the extent applicable, without a claim of exclusive copyright in AI output.
The [32px mark](assets/workstack-mark-32.png) is a separate composition, not a
downscaled badge. See [RIGHTS.md](RIGHTS.md) for asset scope and outside notices.
