# Clock-only architecture and split

Docket is a focused, clock-only scheduling service. It schedules timers, alarms, recurring schedules, watches, and check-ins/stall attention, and publishes best-effort notices.

Event intake, routing, subscription handling, and event-driven workloads belong to [Tower](https://github.com/batteryshark/tower).

## Split and retention

- **Scope:** Docket schedules time-based and watch-based wakes and emits opt-in notices to Tower. Event intake, subscription queues, wake profiles, sandbox runners, and event outcome journaling are not Docket capabilities.
- **Data retention:** Clock-only startup leaves legacy event tables/data and private configuration untouched; it does not drop, drain or replay them. A consistent pre-cut backup is retained by the operator.
- **Code retention:** Prior event-intake and sandbox-runtime code is preserved at [event-runtime-before-clock-cut-20261023](https://github.com/batteryshark/docket/tree/event-runtime-before-clock-cut-20261023) for later agent launch profiles.
- **No new framework:** This split introduces no new service or framework in Docket.
