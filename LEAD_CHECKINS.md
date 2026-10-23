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

Idle/milestone prompts ask the **lead** to tell the chief in one plain sentence.
They do not message any chief ID and never route through retired Ops. The separate
delivery-stall inbox lane resolves the operator's current intended recipient via
`~/.chief-of-staff/chief.json`; no recipient identity is bundled here.

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

## RUNNING / no-delivery inbox lane

The same minute tick runs this independent lane after ordinary reconciliation.
Only roster active + native goal active + session active + validated absolute
directory and `(id, createdAt)` qualify. Blocked, held, done, idle, archived,
moved and unknown sessions suppress. Restart, failed observations, backward
clock movement and gaps over two minutes restart the continuous running interval.
At 30 observed minutes without a fingerprint change, reserve one deterministic
alert per session/directory/native-goal delivery epoch in the existing ledger's
additive `delivery_stalls` map. Idle gaps, milestones and chief replacement do
not rearm it. Content change rearms; activity, counters and milestones do not.

`docket.delivery_evidence.snapshot` supplies bounded source/content observations.
Optional roster `delivery_artifacts` is a list of at most 16 absolute paths, each
at most 4096 characters and without NULs. Invalid configuration or unavailable
evidence suppresses this lane. Configuration changes rebaseline conservatively,
without counting as delivery or rearming an alerted epoch. No trusted installation
receipts exist. Content observations cannot attribute work to one writer, establish
acceptance, or prove installation success. The first observation is a baseline,
not a claim that anything was delivered. Usable/remaining text is factual evidence
and chief review scope, never a project forecast or quota estimate.

Reader coverage is the enrolled Git root plus up to eight registered descendant
worktrees of the same repository. Aggregate two-pass bounds are 10 seconds,
16 MiB of content, 2 MiB of Git output and 2048 enumerated file entries. Source
text comparison is whitespace-insensitive; logs, LEAD files, private/generated
files and ignored untracked files are excluded. Configured artifacts are compared
by exact bytes, not mtime. Non-Git directories require explicit regular artifact
files: this is selected-file coverage, not whole-project coverage. Other lane
writes may be missed. An unchanged observation is an attention flag, not proof
of failure or of no useful work.

Each publication freshly resolves `~/.chief-of-staff/chief.json` (`session`,
absolute `directory`). Pointer and destination must be owned, not symlinks, and
not group/other writable; existing read/execute bits are allowed. Roster/ledger
privacy rules remain unchanged. The destination is only that directory's `inbox`.
New alert files are 0600, fsynced and atomically linked without overwrite to
`delivery-stall-<deterministic-key>.md`. No wake, resume, principal card,
service, or new scheduler is involved. The inbox write is not a bus publish.
An opt-in Tower notice, off unless `DOCKET_TOWER_NATS_URL` is a numeric loopback
URL, may enqueue `ev.lead.delivery.stalled` for a new episode only; already-alerted
epochs are not backfilled. See [docs/TOWER.md](docs/TOWER.md). Per-tick probes are bounded by the existing
20-row roster and the evidence reader's bounds; ordinary due work runs first.

Fresh pointer/destination validation precedes reservation. Known pre-reservation
refusals remain retryable. The ledger then freezes the alert text, key, chief
session/directory and inbox device/inode before publication. Uncertain writes
retry only that exact inbox and deterministic file; matching existing bytes
reconcile success, conflicts or replaced inboxes suppress with a diagnostic.
A crash after linking but before ledger completion cannot replay into a replacement
chief. Residual limits: a crash can leave an unlinked private temporary file;
lost ledger durability, filesystem inode reuse or external deletion of the alert
cannot establish exactly-once delivery. Do not delete the ledger. Frozen destination
failures require owner inspection, not redirection to a new chief.
Bounded generic diagnostics surface through `Checkins.error`; ordinary ineligibility
does not report a failure. Evidence/inbox failures cannot disable old checks.
Evidence filesystem reads use the existing bounded check executor's submit/take,
never wait or create a new pool. Pending results cannot advance continuity.
Queue the next frozen probe immediately after consuming a successful result,
keeping steady-state sampling at one cadence despite drift. Results older than
two minutes or probes taking over two minutes are discarded. Hold,
goal/directory/config changes invalidate pending results; completed observations
alone count toward the interval. The evidence reader's optional opaque scope
detects source-root/artifact topology changes: these rebaseline without rearming.
No roster state is edited here. Coverage always follows the current roster and
native eligibility, not a cached report of which projects were active or held.
Isolated policy tests inject the reader and writer. Operational installation and
actual-use readback are recorded separately by the runtime owner. This lane never
changes native lead models, goals or accepted markers, and never notifies the principal.

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
