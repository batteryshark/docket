# Docket

<img src="assets/workstack-mark.png" alt="Docket alarm-clock mascot" width="180">

Docket is a local scheduler that wakes an existing agent session later without keeping a model running to poll. It provides timers, alarms, bounded recurring schedules and watches through a CLI, loopback HTTP API and stdio MCP server. Schedules persist in SQLite independently of the agent host; Docket must be running and the target host available to deliver them.

## Requirements

- Python 3.11+ on macOS, Linux or Windows.
- A compatible local OpenCode host and an existing session for delivery. See [host setup](docs/host-setup.md).
- Windows requires `tzlocal` and `tzdata`, installed by pip below.

## Install

```sh
git clone https://github.com/batteryshark/docket.git
cd docket
python3 -m venv .venv
. .venv/bin/activate
python -m pip install .
docket --help
```

On Windows, use `python -m venv .venv` and activate with `.venv\Scripts\Activate.ps1` in PowerShell. Unix users can also run `python3 -m docket` directly from the checkout without installing dependencies.

## Quick start

Start the scheduler:

```sh
docket serve
```

It listens at `http://127.0.0.1:8768`; Ctrl-C stops it. Do not run it alongside an installed Docket daemon. After [configuring the delivery host](docs/host-setup.md), use another terminal and replace the example session ID with an existing one:

```sh
docket timer --in 20m --session ses_example --reason "check progress"
docket list
docket occurrences
docket cancel ITEM_ID
```

Use the item ID returned by `timer` for cancellation. Delivery means the host saved the matching wake message, not that the agent completed the requested work. Cancellation cannot undo a message already consumed. See [delivery status and recovery](docs/delivery-status.md).

## Configuration

| Setting | Purpose |
| --- | --- |
| `DOCKET_HOME` | Private state directory; default `~/.docket` on Unix or `%LOCALAPPDATA%\Docket` on Windows |
| `DOCKET_DELIVERY_HOST` | `auto`, `opencode`, `openchamber-paired` or `openchamber-unpaired`; pairing state takes precedence |
| `DOCKET_URL`, `DOCKET_TOKEN` | Override API discovery for CLI/MCP clients |
| `DOCKET_PYTHON` | Interpreter used by the Unix `bin/docket` launcher |

Keep state and credentials out of source control. Changing `DOCKET_HOME` selects a different store; it does not migrate schedules. For persistent operation, follow the [platform service guide](docs/services.md). macOS and Linux installation start the service; Windows installation registers a task but does not start it.

## Key commands

```sh
docket alarm --at 09:00 --session ses_example --reason "review"
docket recurring --every 15m --count 4 --session ses_example --reason "check again"
docket watch --help
docket list --status all
docket reconcile ITEM_ID --json
docket service status
docket mcp
```

Recurring schedules require a count or expiry; missed intervals coalesce. Watches require an expiry and support file appearance/stability, process completion, URL availability and session idle. Docket sends messages; it does not execute shell commands. MCP exposes creation, listing and cancellation through the running API.

## Documentation

- [CLI and MCP usage](AGENT_USAGE.md)
- [Host setup](docs/host-setup.md) and [OpenChamber pairing](PAIRING.md)
- [Service installation and maintenance](docs/services.md)
- [Delivery status and recovery](docs/delivery-status.md)
- [Optional Pi adapter](docs/pi.md)
- [Optional check-ins](LEAD_CHECKINS.md), [event notices](docs/TOWER.md) and [refresh schedules](docs/tower-refresh.md)
- [Optional OpenChamber panel](openchamber/README.md)

## License

[MIT](LICENSE). See [RIGHTS.md](RIGHTS.md) for artwork scope and third-party notices. The comic marks are AI-generated artwork offered under MIT to the extent applicable, without a claim of exclusive copyright in AI output.
