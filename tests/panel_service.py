"""Isolated Docket HTTP API for the panel relay test.

This is the real Docket server. It does not start the scheduler and it uses
the test adapter, so it cannot wake a session or touch the live LaunchAgent.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from docket.api import DocketServer
from docket.clock import SystemClock
from docket.scheduler import Scheduler
from docket.store import Store, home, load_token, server_path
from tests.support import FakeAdapter


def main() -> int:
    root = home()
    store = Store(root / "docket.db")
    adapter = FakeAdapter()
    adapter.inbox.add("msg_paneladmit0001")
    server = DocketServer(
        store,
        Scheduler(store, adapter, SystemClock()),
        load_token(),
        "127.0.0.1",
        0,
    )
    path = server_path()
    path.write_text(json.dumps({"url": server.url, "pid": os.getpid()}) + "\n")
    path.chmod(0o600)
    print(server.url, flush=True)

    def stop(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
