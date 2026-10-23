# Opt-in lead check-ins — operating card

**Off by default.** No new service, CLI, API field, shell check or agent loop.
The existing scheduler reconciles at most once a minute; existing watch polling,
queue admission, stable message IDs, receipts and retries do the delivery.

Check-ins are opt-in and verified in isolated macOS source tests. An existing
macOS installation was enabled; an automatic consumed check-in wake is still
unproved in the recorded activation report. This public source port is not a
deployment. The POSIX private-path checks and absolute POSIX roster directories
are not Windows/Linux check-in compatibility proof. Ordinary cross-platform
scheduling and the optional Pi adapter retain their existing contracts; check-ins
target OpenCode only, not Pi.

## One line per lead

Runtime roster: **`~/.docket/lead-checkins.jsonl`** (or
`$DOCKET_HOME/lead-checkins.jsonl`, beside the scheduler database). Directory
must be owned by the service user, mode 0700; file must be a regular owned
0600 file, not a symlink. Install only after source/diff approval. Use normal
private file creation, never broaden permissions. Keep real IDs out of Git.

Add one JSON line (replace fixture session and directory):

```json
{"session":"ses_examplelead0001","directory":"/absolute/project","state":"active"}
```

- **Hold:** change that line's `"state":"active"` to `"state":"hold"`.
- **Resume:** change it back to `"active"`.
- **Done:** use `"state":"done"`; this overrides stale host `blocked`.
- **Remove:** delete that one line. History and unrelated watches remain.
- **Record a visible milestone:** add/update BOTH fields on that line:
  `"milestone_at":"2026-10-22T12:00:00Z","milestone_ref":"accepted screenshot or result reference"`.
  Record an actual visible result, not incidental activity, log touches or mtimes.
- **Disable:** rename/remove the roster (or empty it). No uninstall needed.
  Do not delete the ledger. Edits apply within roughly 60 seconds plus host
  request/scheduler latency; they do not require a restart.

Malformed, missing, unreadable, non-private or duplicate-line roster disables
all check-in eligibility. Keep edits valid JSONL (an atomic file replacement is
preferable). There is a limit of 20 roster lines; this is not capacity to wake
20 leads simultaneously. Existing limits are unchanged: eight pending watches
per setter, eight watches/twenty pending items per target, 200 pending globally.
Both check-ins use the fixed `lead-checkins` setter: **four eligible idle leads
with two watch slots each fit**; ordinary watches may reduce available capacity.
Excess waits for capacity; a scheduler diagnostic says capacity was reached.

### Cooperating roster writers

Serialize trusted roster edits with the existing owned0600
`.lead-checkins.lock` beside the roster (`flock`, exclusive/nonblocking). Follow
`docket/milestone.py`: validate private owned directory/file and unique rows,
retain expected bytes and file identity, prepare a metadata-preserving temporary
file in the same directory, fsync it, re-read/revalidate the original and parent,
then atomically replace and fsync the directory. Preserve unrelated bytes/fields.
This detects observed conflicts, not arbitrary noncooperating-writer CAS.

The native `docket_milestone` wrapper binds identity and permits only the caller's
two milestone fields, never state, enrollment or another row. Hold/resume remains
an explicitly authorized operator edit; it must not replay a milestone. See the
[native binding contract](integrations/opencode/README.md#existing-own-milestone-tool).

## Eligibility and timing

Only roster `active` + host goal `active` or `blocked` is unfinished. `complete`,
missing/unknown goals, archived/unavailable/wrong-directory sessions suppress.
Host `outcome` is not eligibility. Native `(id, createdAt)` must be valid;
changing either resets enrollment baselines. `updatedAt`, token/turn counters
and message activity are never visible milestones. Explicit hold/done always wins.

- **Idle:** about 30 minutes of continuously *observed* idle. Busy resets it.
  Enrollment, restart, unavailable samples and observation gaps over two minutes
  start conservatively; historical `time.idle` is not trusted. After consumption,
  wait another 30 minutes before rearming, never duplicate an outstanding pair.
- **Milestone:** about three hours since this goal's enrollment or the latest
  explicit milestone reset. If no semantic marker exists, enrollment is an
  honest baseline, **not proof of an actual milestone**. Busy sessions may get
  this normal queued prompt. Consumption starts the next three-hour epoch;
  at most one prompt per epoch. A marker or goal change stops old intent first.
- Each watch has a one-day lifetime. Expired unmatched watches automatically
  get a fresh bounded watch while still eligible, preserving the milestone
  baseline. This continues beyond today; no cron/daemon renewal is needed.

Prompts ask the **lead** to tell the chief in one plain sentence. This feature
does not message any chief ID and never routes through retired Ops. Any later
chief-directed operation must resolve the operator's current intended recipient;
no recipient identity is bundled here.

## Cancellation, uncertainty and recovery

The private **`lead-checkins-state.json`** beside the roster reserves owned IDs
before insertion. Only these IDs can be cancelled; a caller label is not
ownership. Ordinary watches retain immediate, one-shot, latched behavior.
Roster/goal eligibility is checked again immediately before every new send or
retry, including a previously latched watch. Removing/holding a lead retracts
its queued admission using existing verified retraction behavior. A host may
consume between that check and retraction; cancellation is best effort, not a
cross-process atomic promise. Already consumed prompts cannot be unsent.

Ambiguous/lost/undeliverable or failed-retraction slots are **not automatically
replaced**. Missing reserved rows and corrupt/missing ledgers with outstanding
intent also fail closed. This favors no ambiguity replay over guaranteed nudges.
The scheduler logs a generic reconciliation/ledger error or capacity warning,
without private row contents. A failed private ledger write stops this feature
until restart; it never starts another admission. A leftover `.tmp` reservation
requires owner inspection, not blind deletion. Do not reset the ledger to
silence a fault: inspect the owned IDs and existing receipts/inbox first, prove
the prior intent consumed/stopped, then repair that specific private slot.
There is no new automated recovery protocol.

## Source installation (not performed by this change)

This source integrates the small check-in hooks into the public frozen-envelope
core. Eligibility stops use that core's durable cancellation and exact frozen
status evidence, not an ID-only consumption lookup. No schema migration,
CLI delivery format change, new dependency, service or scheduler registration.
Do not copy the entire public core over an older live installation without a
separate deployment review, or overwrite its private state.
The existing Docket process needs **one ordinary restart** to load changed Python
source. Installing the private roster then enables check-ins automatically.
Review diff and author tests before any live install/restart/roster creation.
No restart, roster installation or live wake is performed by publishing source.

## Experimental own-row milestone helper (not wired)

[`docket.milestone`](docket/milestone.py) accepts only `milestone_at` and
`milestone_ref` on stdin. A trusted native wrapper must supply
`DOCKET_CALLER_SESSION` and `DOCKET_CALLER_DIRECTORY`; environment identity is
trusted-process input, **not authentication**. No native tool registration exists.
The CLI uses the fixed `~/.docket/lead-checkins.jsonl` path, not `DOCKET_HOME`.
It updates only the enrolled matching row, preserving hold/done, other fields,
other lines and private metadata. It cannot enroll, resume, schedule or send.
Private modes, symlinks, duplicate keys, conflicts and future/backward timestamps
are refused. Cooperating writers use a nonblocking lock plus an expected-bytes
check; a noncooperating final read/rename race remains, not a general CAS promise.
Its `fcntl`/fixed macOS copy command implementation is source-tested on macOS,
not Windows/Linux certified. See [current-work limits](docs/current-work.md).
