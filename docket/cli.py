"""Docket command line. Scheduling goes through the running service."""

from __future__ import annotations

import argparse
import json
import sys

from docket import __version__
from docket.client import ClientError, request, resolve_url


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # Even unrecognized/fragmentary credentials must not enter argparse diagnostics.
        self.print_usage(sys.stderr)
        self.exit(2, "docket: invalid arguments; use --help (Connection links require pair --stdin)\n")


def main(argv: list[str] | None = None) -> int:
    supplied = list(sys.argv[1:] if argv is None else argv)
    if (any("openchamber:" in arg.lower() or "oc_client_" in arg for arg in supplied)
            or (supplied and supplied[0] == "pair"
                and supplied not in (["pair", "--stdin"], ["pair", "--help"], ["pair", "-h"]))):
        # argparse normally prints rejected arguments, potentially leaking an accidentally pasted link.
        print("docket: use pair --stdin; Connection links are never accepted as arguments", file=sys.stderr)
        return 2
    parser = SafeArgumentParser(prog="docket", description="Local schedule service that wakes an agent session")
    parser.add_argument("--version", action="version", version=f"docket {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="run the HTTP service in the foreground")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8768)
    daemon = sub.add_parser("daemon", help="run the service for launchd")
    daemon.add_argument("--host", default="127.0.0.1")
    daemon.add_argument("--port", type=int, default=8768)

    timer = sub.add_parser("timer", help="set a relative timer")
    _common(timer)
    timer.add_argument("--in", dest="delay", required=True, help="delay, e.g. 20m or 90")

    alarm = sub.add_parser("alarm", help="set an absolute alarm")
    _common(alarm)
    alarm.add_argument("--at", required=True, help="HH:MM local, or an ISO timestamp")

    recurring = sub.add_parser("recurring", help="set a bounded recurring wake")
    _common(recurring)
    recurring.add_argument("--every", required=True, help="interval, at least 60s, e.g. 15m")
    recurring.add_argument("--count", type=int, help="maximum wakes; required if --until is omitted")
    recurring.add_argument("--until", help="duration or ISO timestamp; required if --count is omitted")
    recurring.add_argument("--start", help="first fire, duration or ISO timestamp; default is one interval from now")

    watch = sub.add_parser("watch", help="set a built-in read-only watch")
    _common(watch)
    watch.add_argument("--check", required=True, choices=(
        "file_appears", "file_stable", "process_ended", "url_answers", "session_idle",
    ))
    watch.add_argument("--until", required=True, help="duration or ISO timestamp")
    watch.add_argument("--every", help="poll interval; defaults are conservative")
    watch.add_argument("--path", help="absolute path for a file check")
    watch.add_argument("--stable-for", dest="stable_for", help="how long a file must stop changing")
    watch.add_argument("--pid", type=int, help="process id for process_ended")
    watch.add_argument("--url", help="http(s) URL for url_answers")
    watch.add_argument("--expect", choices=("2xx", "any"), help="what counts as an answer; default 2xx")
    watch.add_argument("--watch-session", dest="watch_session", help="session to wait on; default is the target")

    listing = sub.add_parser("list", help="list items")
    listing.add_argument("--status", default="pending", help="pending (includes firing, admitted, ambiguous), all, delivered, completed, cancelled, undeliverable, expired")
    listing.add_argument("--json", action="store_true")

    occurrences = sub.add_parser("occurrences", help="list occurrence outcomes")
    occurrences.add_argument("id", nargs="?", help="item id; omit to list across items")
    occurrences.add_argument("--status", default=None, help="delivered, undeliverable, cancelled, pending")
    occurrences.add_argument("--json", action="store_true")

    cancel = sub.add_parser("cancel", help="cancel a pending item")
    cancel.add_argument("id")
    cancel.add_argument("--json", action="store_true")

    service = sub.add_parser("service", help="manage the LaunchAgent")
    service.add_argument("action", choices=("install", "uninstall", "start", "stop", "restart", "status"))

    sub.add_parser("mcp", help="stdio MCP server (talks to the running service)")
    pairing = sub.add_parser("pair", help="redeem a principal-created Connection link from stdin only")
    pairing.add_argument("--stdin", action="store_true", required=True,
                         help="read link from stdin; hidden prompt on a terminal; never pass the link as an argument")

    args = parser.parse_args(supplied)
    if not args.command:
        parser.print_help()
        return 2
    try:
        if args.command == "pair":
            from docket.pairing import PairingError, intake, redeem
            try:
                result = redeem(intake(sys.stdin))
            except (OSError, EOFError):
                raise PairingError("pairing input/storage failed; inspect enrollment before retrying") from None
            print(json.dumps(result))
            return 0
        if args.command in ("serve", "daemon"):
            return _serve(args.host, args.port)
        if args.command == "timer":
            return _create("timer", args)
        if args.command == "alarm":
            return _create("alarm", args)
        if args.command == "recurring":
            return _create("recurring", args)
        if args.command == "watch":
            return _create("watch", args)
        if args.command == "list":
            return _list(args.status, args.json)
        if args.command == "occurrences":
            return _occurrences(args.id, args.status, args.json)
        if args.command == "cancel":
            return _cancel(args.id, args.json)
        if args.command == "service":
            from docket.service import run
            return run(args.action)
        if args.command == "mcp":
            from docket.mcp_server import serve
            return serve()
    except ClientError as exc:
        print(f"docket: {exc.detail}", file=sys.stderr)
        return 1
    return 2


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", required=True, help="target session id")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--urgent", action="store_true")
    parser.add_argument("--context", default=None, help="JSON object")
    parser.add_argument("--by", dest="created_by", default=None)
    parser.add_argument("--directory", default=None, help="pin the session directory")
    parser.add_argument("--json", action="store_true")


def _serve(host: str, port: int) -> int:
    from docket.api import DocketServer
    from docket.clock import SystemClock
    from docket.host import configured_adapter
    from docket.overlay import OpenChamberArchive
    from docket.scheduler import Scheduler
    from docket.store import Store, home, load_token, server_path

    if host not in ("127.0.0.1", "localhost"):
        print("docket: refusing to bind a non-loopback address", file=sys.stderr)
        return 1
    root = home()
    store = Store(root / "docket.db")
    server = DocketServer(
        store, Scheduler(store, configured_adapter(archive=OpenChamberArchive()), SystemClock()),
        load_token(), host, port,
    )
    info = {"url": server.url, "pid": _pid()}
    path = server_path()
    path.write_text(json.dumps(info) + "\n")
    path.chmod(0o600)
    print(f"docket: listening on {server.url}", flush=True)
    server.start_scheduler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        store.close()
    return 0


def _create(kind: str, args) -> int:
    body = {
        "session_id": args.session,
        "reason": args.reason,
        "urgent": args.urgent,
        "created_by": args.created_by,
        "directory": args.directory,
    }
    if args.context:
        try:
            body["context"] = json.loads(args.context)
        except json.JSONDecodeError:
            print("docket: --context must be JSON", file=sys.stderr)
            return 1
    if kind == "timer":
        delay = args.delay
        body["in"] = float(delay) if _numeric(delay) else delay
        path = "/api/v1/timers"
    elif kind == "alarm":
        body["at"] = args.at
        path = "/api/v1/alarms"
    elif kind == "recurring":
        body["every"] = args.every
        if args.count is not None:
            body["count"] = args.count
        if args.until:
            body["until"] = args.until
        if args.start:
            body["start"] = args.start
        path = "/api/v1/recurring"
    else:
        body["check"] = args.check
        body["until"] = args.until
        for key in ("every", "path", "stable_for", "url", "expect", "watch_session"):
            value = getattr(args, key)
            if value:
                body[key] = value
        if args.pid is not None:
            body["pid"] = args.pid
        path = "/api/v1/watches"
    _status, payload = request("POST", path, body)
    item = payload["item"]
    if args.json:
        print(json.dumps(payload))
    else:
        print(f"{item['id']}  {item['kind']}  {item['fire_at']}  {item['target']['session_id']}")
    return 0


def _list(status: str, as_json: bool) -> int:
    _status, payload = request("GET", f"/api/v1/items?status={status}")
    if as_json:
        print(json.dumps(payload))
        return 0
    items = payload.get("items") or []
    if not items:
        print(f"docket: no {status} items ({resolve_url()})")
        return 0
    for item in items:
        flags = []
        if item.get("urgent"):
            flags.append("urgent")
        if item.get("late"):
            flags.append("late")
        if item.get("error"):
            flags.append(item["error"])
        extra = ("  " + ", ".join(flags)) if flags else ""
        target = (item.get("target") or {}).get("session_id", "")
        spec = item.get("spec") or {}
        detail = ""
        if item.get("kind") == "watch":
            detail = "  " + str(spec.get("check") or "")
        elif item.get("kind") == "recurring":
            detail = f"  every {spec.get('every')}s"
        missed = (item.get("occurrences") or {}).get("undeliverable")
        if missed:
            detail += f"  undeliverable_occurrences={missed}"
        print(f"{item['id']}  {item['status']:13}  {item['fire_at']}  {target}  {item['reason']}{detail}{extra}")
    return 0


def _occurrences(item_id: str | None, status: str | None, as_json: bool) -> int:
    if item_id:
        path = f"/api/v1/items/{item_id}/occurrences"
    else:
        path = "/api/v1/occurrences"
        if status:
            path += f"?status={status}"
    _status, payload = request("GET", path)
    if item_id and status:
        payload = {
            "occurrences": [
                occ for occ in payload.get("occurrences") or []
                if occ.get("status") == status
            ]
        }
    if as_json:
        print(json.dumps(payload))
        return 0
    found = payload.get("occurrences") or []
    if not found:
        print("docket: no occurrences")
        return 0
    for occ in found:
        flags = []
        if occ.get("late"):
            flags.append("late")
        if occ.get("coalesced"):
            flags.append(f"coalesced={occ['coalesced']}")
        if occ.get("error"):
            flags.append(occ["error"])
        extra = ("  " + ", ".join(flags)) if flags else ""
        print(f"{occ['id']}  {occ['status']:14}  {occ['scheduled_at']}  {occ['item_id']}  seq={occ['seq']}{extra}")
    return 0


def _cancel(item_id: str, as_json: bool) -> int:
    _status, payload = request("POST", f"/api/v1/items/{item_id}/cancel", {})
    if as_json:
        print(json.dumps(payload))
    else:
        print(f"cancelled {item_id}")
    return 0


def _numeric(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _pid() -> int:
    import os
    return os.getpid()
