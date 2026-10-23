"""Docket command line. Scheduling goes through the running service."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.parse

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
    for command in (serve, daemon):
        command.add_argument("--events-config", help="explicit private v2 subscription manifest; enables event intake/worker")
        command.add_argument("--event-host-config", help="explicit private native event-host config (required with events)")
        command.add_argument("--event-journal-config", help="explicit private write-only journal channel config (requires events)")

    events = sub.add_parser("events", help="inspect durable event deliveries or install trusted configuration")
    event_commands = events.add_subparsers(dest="event_command", required=True)
    for name in ("list", "deadletters", "inspect", "outcomes"):
        command = event_commands.add_parser(name)
        command.add_argument("--json", action="store_true")
        if name == "inspect":
            command.add_argument("id")
    install = event_commands.add_parser("install", help="offline operator-only immutable manifest installation")
    install.add_argument("--config", required=True, help="absolute private manifest path")
    install.add_argument("--database", required=True, help="explicit absolute Docket database path")

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
    listing.add_argument("--outcome", default=None, help="delivery outcome filter")
    listing.add_argument("--json", action="store_true")

    occurrences = sub.add_parser("occurrences", help="list occurrence outcomes")
    occurrences.add_argument("id", nargs="?", help="item id; omit to list across items")
    occurrences.add_argument("--status", default=None, help="delivered, undeliverable, cancelled, pending")
    occurrences.add_argument("--json", action="store_true")

    reconcile = sub.add_parser("reconcile", help="reconcile delivery state")
    reconcile.add_argument("id", help="item or occurrence id")
    reconcile.add_argument("--occurrence", action="store_true", help="target an occurrence rather than an item")
    reconcile.add_argument("--json", action="store_true")

    retry = sub.add_parser("retry", help="authorize one additional delivery attempt")
    retry.add_argument("id", help="item or occurrence id")
    retry.add_argument("--occurrence", action="store_true", help="target an occurrence rather than an item")
    retry.add_argument("--json", action="store_true")

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
            return _serve(args.host, args.port, events_config=args.events_config, event_host_config=args.event_host_config,
                          event_journal_config=args.event_journal_config)
        if args.command == "events":
            return _events(args)
        if args.command == "timer":
            return _create("timer", args)
        if args.command == "alarm":
            return _create("alarm", args)
        if args.command == "recurring":
            return _create("recurring", args)
        if args.command == "watch":
            return _create("watch", args)
        if args.command == "list":
            return _list(args.status, args.json, args.outcome)
        if args.command == "occurrences":
            return _occurrences(args.id, args.status, args.json)
        if args.command == "reconcile":
            return _reconcile(args.id, args.occurrence, args.json)
        if args.command == "retry":
            return _retry(args.id, args.occurrence, args.json)
        if args.command == "cancel":
            return _cancel(args.id, args.json)
        if args.command == "service":
            from docket.service import run
            return run(args.action)
        if args.command == "mcp":
            from docket.mcp_server import serve
            return serve()
    except ClientError as exc:
        if getattr(args, "json", False):
            payload = exc.payload
            if not isinstance(payload, dict):
                err = {"detail": exc.detail}
                if exc.code:
                    err["code"] = exc.code
                payload = {"error": err}
            print(json.dumps(payload))
            return 1
        print(f"docket: {exc.detail}", file=sys.stderr)
        return 1
    return 2


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--session", required=True, help="target session id")
    parser.add_argument("--adapter", choices=("opencode", "pi"), default=None,
                        help="optional target adapter; omitted keeps normal OpenCode delivery")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--urgent", action="store_true")
    parser.add_argument("--context", default=None, help="JSON object")
    parser.add_argument("--by", dest="created_by", default=None)
    parser.add_argument("--directory", default=None, help="pin the session directory")
    parser.add_argument("--json", action="store_true")


def _serve(host: str, port: int, *, events_config=None, event_host_config=None, event_journal_config=None) -> int:
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
    if bool(events_config) != bool(event_host_config):
        print("docket: events require both explicit private config paths", file=sys.stderr)
        return 1
    if event_journal_config and not events_config:
        print("docket: journal requires explicit event configuration", file=sys.stderr)
        return 1
    event_worker = None
    server_type = DocketServer
    options = {}
    if events_config:
        from docket.subscription_api import SubscriptionServer, read_private_json
        from docket.subscription_worker import EventStore, EventWorker
        from docket.subscriptions import SubscriptionRefusal
        store = None
        try:
            manifest = read_private_json(events_config)
            if not isinstance(manifest, dict) or manifest.get("version") != 2:
                raise SubscriptionRefusal("live event mode requires native version 2 profiles")
            host_config = read_private_json(event_host_config)
            from docket.event_host import configured_event_adapter
            event_adapter = configured_event_adapter(host_config)
            store = EventStore(root / "docket.db")
            store.install_manifest(manifest)
            event_worker = EventWorker(store, event_adapter)
            if event_journal_config:
                from docket.event_journal import EventJournal
                options["journal"] = EventJournal(store.path.resolve(), event_journal_config)
        except (ValueError, TypeError, OSError, ImportError, sqlite3.Error):
            if store is not None:
                store.close()
            print("docket: native event configuration unavailable or refused", file=sys.stderr)
            return 1
        server_type = SubscriptionServer
        options["enabled"] = True
    else:
        store = Store(root / "docket.db")
    server = server_type(
        store, Scheduler(store, configured_adapter(archive=OpenChamberArchive()), SystemClock(), event_worker=event_worker),
        load_token(), host, port, **options,
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
        server._sched_stop.set()
        worker_stopped = event_worker is None or event_worker.close()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        if worker_stopped:
            store.close()
        else:
            print("docket: event worker shutdown unresolved; durable intent retained", file=sys.stderr)
    return 0


def _events(args):
    if args.event_command == "install":
        from pathlib import Path
        from docket.subscription_api import load_installed
        from docket.subscription_worker import EventStore
        if not Path(args.database).is_absolute():
            print("docket: installation requires an explicit absolute database path", file=sys.stderr)
            return 1
        store = None
        try:
            store = EventStore(Path(args.database))
            load_installed(store, Path(args.config))
        except (ValueError, TypeError, OSError, sqlite3.Error):
            print("docket: trusted event installation refused", file=sys.stderr)
            return 1
        finally:
            if store is not None:
                store.close()
        print("docket: immutable event configuration installed; daemon activation is separate")
        return 0
    from urllib.parse import quote
    path = "/api/v1/events"
    if args.event_command == "deadletters":
        path += "/deadletters"
    elif args.event_command == "outcomes":
        path += "/outcomes"
    elif args.event_command == "inspect":
        path += "/" + quote(args.id, safe="")
    _, payload = request("GET", path)
    if args.json or args.event_command in ("inspect", "outcomes"):
        print(json.dumps(payload, indent=None if args.json else 2))
    else:
        for row in payload.get("events", []):
            print(f"{row['id']}  {row.get('terminal') or row.get('phase') or row['status']}  {row.get('reason') or ''}")
    return 0


def _create(kind: str, args) -> int:
    body = {
        "session_id": args.session,
        "reason": args.reason,
        "urgent": args.urgent,
        "created_by": args.created_by,
        "directory": args.directory,
    }
    if args.adapter is not None:
        body["adapter"] = args.adapter
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


def _list(status: str, as_json: bool, outcome: str | None = None) -> int:
    path = f"/api/v1/items?status={urllib.parse.quote(status, safe='')}"
    if outcome:
        path += f"&outcome={urllib.parse.quote(outcome, safe='')}"
    _status, payload = request("GET", path)
    if as_json:
        print(json.dumps(payload))
        return 0
    items = payload.get("items") or []
    if not items:
        print(f"docket: no {status} items ({resolve_url()})")
        return 0
    for item in items:
        delivery = item.get("delivery") if isinstance(item.get("delivery"), dict) else {}
        deliv_outcome = delivery.get("outcome") or "unknown"
        flags = []
        if item.get("urgent"):
            flags.append("urgent")
        if item.get("late"):
            flags.append("late")
        issue_info = delivery.get("issue") or delivery.get("check_issue")
        if isinstance(issue_info, dict):
            code = issue_info.get("code")
            detail_msg = issue_info.get("detail")
            if isinstance(code, str) and code:
                if isinstance(detail_msg, str) and detail_msg and detail_msg != code:
                    flags.append(f"issue={code}:{detail_msg}")
                else:
                    flags.append(f"issue={code}")
            elif isinstance(detail_msg, str) and detail_msg:
                flags.append(f"issue={detail_msg}")
        elif item.get("error"):
            flags.append(str(item["error"]))
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
        print(f"{item['id']}  {item['status']:13}  outcome={deliv_outcome}  {item['fire_at']}  {target}  {item['reason']}{detail}{extra}")
    return 0


def _occurrences(item_id: str | None, status: str | None, as_json: bool) -> int:
    if item_id:
        encoded = urllib.parse.quote(item_id, safe="")
        path = f"/api/v1/items/{encoded}/occurrences"
    else:
        path = "/api/v1/occurrences"
        if status:
            path += f"?status={urllib.parse.quote(status, safe='')}"
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
        delivery = occ.get("delivery") if isinstance(occ.get("delivery"), dict) else {}
        deliv_outcome = delivery.get("outcome") or "unknown"
        flags = []
        if occ.get("late"):
            flags.append("late")
        if occ.get("coalesced"):
            flags.append(f"coalesced={occ['coalesced']}")
        issue_info = delivery.get("issue") or delivery.get("check_issue")
        if isinstance(issue_info, dict):
            code = issue_info.get("code")
            detail_msg = issue_info.get("detail")
            if isinstance(code, str) and code:
                if isinstance(detail_msg, str) and detail_msg and detail_msg != code:
                    flags.append(f"issue={code}:{detail_msg}")
                else:
                    flags.append(f"issue={code}")
            elif isinstance(detail_msg, str) and detail_msg:
                flags.append(f"issue={detail_msg}")
        elif occ.get("error"):
            flags.append(str(occ["error"]))
        extra = ("  " + ", ".join(flags)) if flags else ""
        print(f"{occ['id']}  {occ['status']:14}  outcome={deliv_outcome}  {occ['scheduled_at']}  {occ['item_id']}  seq={occ['seq']}{extra}")
    return 0


def _reconcile(ident: str, is_occurrence: bool, as_json: bool) -> int:
    encoded = urllib.parse.quote(ident, safe="")
    path = f"/api/v1/occurrences/{encoded}/reconcile" if is_occurrence else f"/api/v1/items/{encoded}/reconcile"
    _status, payload = request("POST", path, {})
    if as_json:
        print(json.dumps(payload))
        return 0
    unit = payload.get("occurrence") or payload.get("item") or {}
    status = unit.get("status") or "unknown"
    delivery = unit.get("delivery") if isinstance(unit.get("delivery"), dict) else {}
    outcome = delivery.get("outcome") or "unknown"
    issue_info = delivery.get("issue") or delivery.get("check_issue")
    issue_str = ""
    if isinstance(issue_info, dict):
        code = issue_info.get("code")
        detail = issue_info.get("detail")
        if isinstance(code, str) and code:
            if isinstance(detail, str) and detail and detail != code:
                issue_str = f"  issue={code}:{detail}"
            else:
                issue_str = f"  issue={code}"
        elif isinstance(detail, str) and detail:
            issue_str = f"  issue={detail}"
    print(f"reconciled {ident}  status={status}  outcome={outcome}{issue_str}")
    return 0


def _retry(ident: str, is_occurrence: bool, as_json: bool) -> int:
    encoded = urllib.parse.quote(ident, safe="")
    path = f"/api/v1/occurrences/{encoded}/retry" if is_occurrence else f"/api/v1/items/{encoded}/retry"
    _status, payload = request("POST", path, {})
    if as_json:
        print(json.dumps(payload))
        return 0
    print(f"authorized retry for {ident} (not delivered)")
    return 0


def _cancel(item_id: str, as_json: bool) -> int:
    encoded = urllib.parse.quote(item_id, safe="")
    _status, payload = request("POST", f"/api/v1/items/{encoded}/cancel", {})
    if as_json:
        print(json.dumps(payload))
        return 0
    stopped = "schedule_stopped" if payload.get("schedule_stopped") else "schedule_not_stopped"
    retraction = payload.get("retraction") if isinstance(payload.get("retraction"), dict) else {}
    outcome = retraction.get("outcome") or "uncertain"
    print(f"cancelled {item_id} ({stopped}, retraction={outcome})")
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
