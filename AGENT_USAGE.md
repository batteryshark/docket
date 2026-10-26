# CLI and MCP usage

Start the Docket service and configure a compatible [delivery host](docs/host-setup.md) before scheduling. The [service guide](docs/services.md) covers installation and maintenance.

## MCP

Configure your MCP client to launch `docket mcp` (or `python -m docket mcp` from the checkout). It supports newline-delimited JSON-RPC and Content-Length framing and calls the running local HTTP API. It does not register itself with the host.

Tools: `docket_timer`, `docket_alarm`, `docket_recurring`, `docket_watch`, `docket_list`, `docket_occurrences`, `docket_cancel`. Reconcile and retry are CLI/HTTP operations, not MCP tools.

Discovery reads `server.json` under the selected private state directory. `DOCKET_URL` and `DOCKET_TOKEN` override discovery; do not expose the token in prompts or logs.

## Scheduling

Replace the example session, item IDs and paths:

```sh
docket timer --in 20m --session ses_example --reason "check progress" --by reminders
docket alarm --at 09:00 --session ses_example --reason "morning review" --by reminders
docket recurring --every 15m --count 8 --session ses_example --reason "check progress" --by reminders
docket watch --check file_stable --path /absolute/report --until 6h --session ses_example --reason "report ready"
docket watch --check process_ended --pid 123 --until 2h --session ses_example --reason "job ended"
docket list --status all
docket occurrences ITEM_ID --status undeliverable
docket cancel ITEM_ID
```

`created_by` (`--by`) is a self-reported label used for capacity limits, not an authenticated identity. Omission shares the anonymous bucket; blank labels are refused. `context` is JSON data quoted in the wake, never executable instructions. `--urgent` uses steer instead of queue. A directory pin must still match when the wake fires.

## Recurrence and watch limits

Recurrence intervals are 60 seconds to 90 days. A count of 1–100 or an expiry within 90 days is required; if both are given, the first limit reached stops the series. Missed intervals coalesce into one late wake. The next fire is in the future. A completed series does not mean every occurrence was consumed.

| Watch | Limits |
| --- | --- |
| `file_appears`, `file_stable`, `process_ended`, `session_idle` | Poll at least every 5 seconds (default 15); expiry within 7 days |
| `url_answers` | Poll at least every 30 seconds (default 60); expiry within 24 hours |
| File stability window | 10 seconds to 6 hours, default 60 seconds |

File watches inspect metadata, not contents or semantic completion. Process watches bind PID and start identity; unavailable identity is not an exit. Session outages are not idle. URL checks allow no custom headers or credentials and restrict redirects to the original host and port.

Capacity is 200 pending items globally, 20 per target, at most 5 recurring and 8 watches per target, and 5 recurring and 8 watches per setter label.

## Status and cancellation

`list` defaults to pending, including firing, admitted and ambiguous states. Inspect occurrences separately from schedule lifecycle. An admitted or uncertain occurrence holds the next recurrence until resolved. A fire more than five seconds late is marked late.

Cancellation stops future scheduling and attempts to retract queued admissions. It preserves history and cannot undo consumed work. Inspect `schedule_stopped` separately from retraction (`not_sent`, `retracted`, `too_late`, `uncertain`). Missing receipts do not authorize a replacement wake or new request identity. See [delivery status and recovery](docs/delivery-status.md) for reconciliation and bounded pre-dispatch retry.

## Optional adapters

- [OpenChamber pairing](PAIRING.md) uses Docket's own private client binding.
- [Pi](docs/pi.md) requires explicit `--adapter pi --session pi/ID` and an existing persistent recipient. Pi does not support directory pins or `session_idle`; Docket never launches Pi.

Docket does not execute commands, scripts or shells. Delivery records saved message intake, not model readiness, understanding or completion of the requested task.
