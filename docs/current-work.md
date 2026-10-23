# Clock-only split — 23 October 2026

Docket schedules timers, alarms, recurring wakes, watches, and check-ins/stall
attention, and can publish opt-in notices to
[Tower](https://github.com/batteryshark/tower). Event intake, subscription queues,
wake profiles, sandbox runners, and event journaling are not Docket capabilities.
Prior event-runtime code is retained at
[event-runtime-before-clock-cut-20261023](https://github.com/batteryshark/docket/tree/event-runtime-before-clock-cut-20261023)
for later agent launch profiles. See [CLOCK_ONLY.md](CLOCK_ONLY.md).

Publishing source does not install configuration, enroll owners, or enable
delivery. Existing ordinary cross-platform scheduling/recovery and Pi limits are
unchanged. Clock-only startup leaves legacy event tables and private configuration
untouched; it does not drop, drain, or replay them. This is not a platform or
provider recertification.

- [`milestone.py`](../docket/milestone.py): own-row helper; no automatic wrapper
  registration here. It changes only explicit milestone fields, never roster
  state, enrollment, or delivery. See the
  [check-in operating card](../LEAD_CHECKINS.md) for its fixed path, private-file
  checks, platform and concurrency limits.

Focused milestone tests live in `tests/test_milestone.py`. They use fixture
identities and temporary storage. No full-suite, native platform recertification,
or automatic check-in wake is claimed here. Uncertain delivery remains
status-only, never replacement identity or effect replay.
