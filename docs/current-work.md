# Opt-in event runtime and scoped milestones — 23 October 2026

Explicit V2 configuration now wires authenticated intake, durable queue/worker,
native existing-owner wakes and fresh scoped handlers into the daemon and CLI.
See [operator entrypoints, manifests and profiles](../integrations/opencode/README.md)
and [handler/Tool Stack/Nod requirements](../integrations/opencode/sandbox-runtime.md).
Publishing source does not install configuration, enroll owners or enable delivery.
Existing ordinary cross-platform scheduling/recovery and Pi limits are unchanged;
the concrete native handler backend is macOS-only, not platform/provider parity.

- [`subscriptions.py`](../docket/subscriptions.py): explicitly constructed SQLite
  intake/preparation store. Operator-installed publisher and profile bind the
  target/model/role; event payloads cannot select authority. Durable deduplication
  retains one admission identity per event/subscription, rejects changed-payload
  retries, and narrows tools/budget. EventWorker and EventHost now own dispatch;
  the store itself never inserts an ordinary scheduler Item or sends a wake.
  Preparation alone is not execution authorization.
- [`subscription_api.py`](../docket/subscription_api.py): explicit, disabled-by-default
  server subclass and private offline config loader. `/api/v1/events` is enabled
  only with explicit daemon event/host flags. Separate producer credentials
  cannot mutate admin routes; installed manifests contain hashes, not raw tokens.
  No automatic config path, token creation or live migration.
  Private config handling is POSIX-specific, not Windows compatibility proof.
- [`subscription_admission.py`](../docket/subscription_admission.py): local immutable
  generation/binding and receipt state contract used by the worker/host. Trusted
  owner/profile snapshots are preconditions, not an atomic host fence. Send intent
  is not a blanket execution grant. Missing/uncertain admissions do not
  authorize replay. Optional explicitly supplied adapter reconciliation is read-only;
  no installed-host discovery is performed by this module.
- [`milestone.py`](../docket/milestone.py): own-row helper with accepted native
  identity-bound wrapper use; no automatic wrapper registration here. It changes
  only explicit milestone fields, never roster state, enrollment or delivery.
  See the [check-in operating card](../LEAD_CHECKINS.md) for its fixed path,
  private-file checks, platform and concurrency limits, and the
  [native binding contract](../integrations/opencode/README.md#existing-own-milestone-tool).

Focused tests live in `tests/test_subscriptions.py`, `tests/test_subscription_api.py`,
`tests/test_subscription_admission.py` and `tests/test_milestone.py`. They use fixture
identities/credentials and temporary storage. The API test source normally starts
an ephemeral listener: **do not run it where listeners are prohibited**; an offline
handler harness can exercise its assertions without networking. Separate accepted
native proofs cover both event actions, injection rejection, installed scoped tool
use and automatic revoke/prune; the live class is journal-only, exercised over
xAI/grok-4.7 HTTP. Real Nod pending/refusal is not a principal approval. No full-suite,
native platform recertification or automatic check-in wake is claimed here. Native
stop/revocation/active-goal gates are not an atomic host user-stop CAS; uncertain
delivery remains status-only, never replacement identity or effect replay.
