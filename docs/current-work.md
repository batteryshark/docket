# Experimental source candidates — 22 October 2026

These modules are **NON-LIVE / not wired into the daemon, CLI, MCP or normal API**.
Publishing source does not install configuration, create subscriptions or enable
event delivery. They do not establish host stop precedence or execution-time
permission narrowing. Existing ordinary scheduling/recovery remains unchanged.

- [`subscriptions.py`](../docket/subscriptions.py): explicitly constructed SQLite
  intake/preparation candidate. Operator-installed publisher and profile bind the
  target/model/role; event payloads cannot select authority. Durable deduplication
  retains one admission identity per event/subscription, rejects changed-payload
  retries, and narrows candidate tools/budget. It never inserts a scheduler Item
  or sends a wake. Preparation is not execution authorization.
- [`subscription_api.py`](../docket/subscription_api.py): explicit, disabled-by-default
  server subclass and private offline config loader. The proposed `/api/v1/events`
  route is **absent from the normal Docket server**. Separate producer credentials
  cannot mutate admin routes; installed manifests contain hashes, not raw tokens.
  No automatic config path, token creation, live migration or route registration.
  Private config handling is POSIX-specific, not Windows compatibility proof.
- [`subscription_admission.py`](../docket/subscription_admission.py): local immutable
  generation/binding and receipt state contract, **without a sender**. Trusted
  owner/profile snapshots are preconditions, not an atomic host fence. Send intent
  still reports `execution_authorized: false`. Missing/uncertain admissions do not
  authorize replay. Optional explicitly supplied adapter reconciliation is read-only;
  no installed-host discovery is performed by this module.
- [`milestone.py`](../docket/milestone.py): source-only own-row helper requiring a
  trusted native identity wrapper that is not supplied/registered here. It changes
  only explicit milestone fields, never roster state, enrollment or delivery.
  See the [check-in operating card](../LEAD_CHECKINS.md) for its fixed path,
  private-file checks, platform and concurrency limits.

Focused tests live in `tests/test_subscriptions.py`, `tests/test_subscription_api.py`,
`tests/test_subscription_admission.py` and `tests/test_milestone.py`. They use fixture
identities/credentials and temporary storage. The API test source normally starts
an ephemeral listener: **do not run it where listeners are prohibited**; an offline
handler harness can exercise its assertions without networking. No full-suite or
native platform recertification, automatic check-in wake, live subscription intake,
subscription delivery or native milestone binding is claimed by this source update.
