"""Built-in read-only watches.

Each check is cheap and bounded. None of them run a caller-supplied command.
A timeout or an outage is not a match: the watch keeps waiting until it
expires. File identity is device and inode. A process match is pid plus the
start time captured when the watch was set, so a reused pid still counts as
ended.
"""

from __future__ import annotations

import http.client
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from docket.delivery import DeliveryError


STAT_TIMEOUT = 2.0
PS_TIMEOUT = 2.0
URL_TIMEOUT = 5.0
URL_DEADLINE = 8.0
URL_MAX_REDIRECTS = 2
URL_MAX_BYTES = 2048

_STAT_CODE = """
import os, stat, sys
path = sys.argv[1]
try:
    st = os.stat(path, follow_symlinks=True)
except FileNotFoundError:
    print("missing")
    raise SystemExit(0)
except OSError as exc:
    print("error", getattr(exc, "errno", 0) or 0)
    raise SystemExit(0)
kind = "file" if stat.S_ISREG(st.st_mode) else "other"
mtime = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
print(kind, st.st_dev, st.st_ino, st.st_size, mtime)
"""


@dataclass
class Outcome:
    kind: str  # waiting, met, unavailable, terminal
    detail: str = ""
    observations: dict | None = None
    summary: dict | None = None


@dataclass
class FileView:
    state: str
    dev: int | None = None
    ino: int | None = None
    size: int | None = None
    mtime_ns: int | None = None
    detail: str = ""


class CheckExecutor:
    """A fixed pool for read-only watch checks.

    The queue is bounded. A full backlog is refused instead of spawning another
    thread or process. Callers that have a due timer must not wait on this.
    """

    def __init__(self, *, workers: int = 2, backlog: int = 4):
        if workers < 1 or backlog < 1:
            raise ValueError("check executor needs at least one worker and one backlog slot")
        self.workers = workers
        self.backlog = backlog
        self._queue: queue.Queue = queue.Queue(maxsize=backlog)
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._results: dict[str, object] = {}
        self._errors: dict[str, BaseException] = {}
        self._started = False
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def submit(self, key: str, fn) -> bool:
        """Queue one check. False means the backlog is full and nothing was queued."""
        self._ensure()
        with self._lock:
            if key in self._inflight or key in self._results or key in self._errors:
                return True
            self._inflight.add(key)
        try:
            self._queue.put_nowait((key, fn))
        except queue.Full:
            with self._lock:
                self._inflight.discard(key)
            return False
        return True

    def ready(self, key: str) -> bool:
        with self._lock:
            return key in self._results or key in self._errors

    def take(self, key: str):
        """Return a finished result, or None if it is still running."""
        with self._lock:
            if key in self._errors:
                self._inflight.discard(key)
                raise self._errors.pop(key)
            if key in self._results:
                self._inflight.discard(key)
                return self._results.pop(key)
        return None

    def wait(self, keys, timeout: float) -> None:
        pending = set(keys)
        deadline = time.monotonic() + timeout
        while pending and time.monotonic() < deadline:
            pending = {key for key in pending if not self.ready(key)}
            if pending:
                time.sleep(0.01)

    def _ensure(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            for index in range(self.workers):
                thread = threading.Thread(
                    target=self._loop, name=f"docket-check-{index}", daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                key, fn = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 — one check must not kill the pool
                with self._lock:
                    self._errors[key] = exc
                    self._inflight.discard(key)
            else:
                with self._lock:
                    self._results[key] = result
                    self._inflight.discard(key)
            finally:
                self._queue.task_done()


class Checks:
    def __init__(self, adapter=None, *, file_stat=None, process_reader=None, url_probe=None):
        self.adapter = adapter
        self.file_stat = file_stat or stat_file
        self.process_reader = process_reader or read_process_start
        self.url_probe = url_probe or probe_url

    def capture_process(self, pid: int) -> str:
        try:
            start = self.process_reader(pid)
        except (TimeoutError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("process identity could not be read") from exc
        except OSError as exc:
            raise RuntimeError("process identity could not be read") from exc
        if not start:
            raise ValueError("process is not running")
        return start

    def evaluate(self, spec: dict, now: datetime) -> Outcome:
        try:
            return self._evaluate(spec, now)
        except Exception as exc:  # noqa: BLE001 — one check must not kill the tick
            return Outcome("unavailable", _short(exc))

    def _evaluate(self, spec: dict, now: datetime) -> Outcome:
        check = spec.get("check")
        if check == "file_appears":
            return self._file_appears(spec)
        if check == "file_stable":
            return self._file_stable(spec, now)
        if check == "process_ended":
            return self._process_ended(spec)
        if check == "url_answers":
            return self._url_answers(spec)
        if check == "session_idle":
            return self._session_idle(spec)
        return Outcome("terminal", "unknown check")

    def _file_appears(self, spec: dict) -> Outcome:
        view = self.file_stat(spec["path"])
        if view.state == "unavailable":
            return Outcome("unavailable", view.detail or "file check unavailable")
        if view.state != "file":
            return Outcome("waiting", "file is not present" if view.state == "missing" else "path is not a file")
        return Outcome("met", summary={
            "path": spec["path"],
            "size": view.size,
            "identity": [view.dev, view.ino],
        })

    def _file_stable(self, spec: dict, now: datetime) -> Outcome:
        view = self.file_stat(spec["path"])
        previous = dict(spec.get("observations") or {})
        if view.state == "unavailable":
            return Outcome("unavailable", view.detail or "file check unavailable", observations=previous)
        if view.state != "file":
            return Outcome("waiting", "file is not present" if view.state == "missing" else "path is not a file", observations={})
        identity = [view.dev, view.ino]
        sample = {
            "identity": identity,
            "size": view.size,
            "mtime_ns": view.mtime_ns,
            "max_size": view.size,
            "stable_since": _iso(now),
            "samples": 1,
        }
        if previous.get("identity") != identity or previous.get("size") != view.size or previous.get("mtime_ns") != view.mtime_ns:
            # Grew, shrank, was replaced, or this is the first look. Reset the window.
            if previous.get("identity") == identity and previous.get("size") is not None:
                sample["samples"] = int(previous.get("samples") or 1) + 1
                if view.size is not None and previous.get("max_size") is not None and view.size < previous["max_size"]:
                    sample["max_size"] = view.size
                elif previous.get("max_size") is not None and view.size is not None:
                    sample["max_size"] = max(int(previous["max_size"]), int(view.size))
            return Outcome("waiting", "file is still changing", observations=sample)
        since = _parse(previous.get("stable_since")) or now
        observations = {
            "identity": identity,
            "size": view.size,
            "mtime_ns": view.mtime_ns,
            "max_size": previous.get("max_size", view.size),
            "stable_since": previous.get("stable_since") or _iso(now),
            "samples": int(previous.get("samples") or 1) + 1,
        }
        stable_for = int(spec.get("stable_for") or 0)
        elapsed = (_utc(now) - since).total_seconds()
        if observations["samples"] >= 2 and elapsed >= stable_for:
            return Outcome("met", observations=observations, summary={
                "path": spec["path"],
                "size": view.size,
                "identity": identity,
                "stable_seconds": int(elapsed),
            })
        return Outcome("waiting", "file has not been stable long enough", observations=observations)

    def _process_ended(self, spec: dict) -> Outcome:
        pid = int(spec["pid"])
        expected = str(spec.get("process_start") or "")
        try:
            current = self.process_reader(pid)
        except (TimeoutError, subprocess.TimeoutExpired):
            return Outcome("unavailable", "process check timed out")
        except OSError:
            return Outcome("unavailable", "process check unavailable")
        if current is None:
            return Outcome("met", summary={"pid": pid, "ended": "exited", "start": expected})
        if _norm(current) != _norm(expected):
            return Outcome("met", summary={
                "pid": pid,
                "ended": "pid_reused",
                "start": expected,
                "now_start": current,
            })
        return Outcome("waiting", "process is still running")

    def _url_answers(self, spec: dict) -> Outcome:
        try:
            status, final = self.url_probe(spec["url"])
        except TimeoutError:
            return Outcome("waiting", "url timed out")
        except OSError as exc:
            return Outcome("waiting", _short(exc) or "url is not answering")
        except ValueError as exc:
            return Outcome("waiting", _short(exc))
        expect = spec.get("expect") or "2xx"
        if expect == "any" or 200 <= status <= 299:
            return Outcome("met", summary={"url": final, "status": status})
        return Outcome("waiting", f"url returned {status}")

    def _session_idle(self, spec: dict) -> Outcome:
        session_id = spec.get("watch_session") or ""
        adapter = self.adapter
        if adapter is None or not hasattr(adapter, "session_state"):
            return Outcome("unavailable", "session check is not available")
        try:
            state = adapter.session_state(session_id)
        except DeliveryError as exc:
            return Outcome("unavailable", exc.detail or "session check unavailable")
        if state == "idle":
            return Outcome("met", summary={"session_id": session_id, "state": "idle"})
        if state == "active":
            return Outcome("waiting", "session is not idle")
        if state in ("missing", "archived"):
            return Outcome("terminal", f"{state}: watched session is {state}")
        return Outcome("unavailable", "session check unavailable")


def stat_file(path: str, *, timeout: float = STAT_TIMEOUT) -> FileView:
    """Stat one path in a child so a stuck mount cannot hang the service."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        return FileView("unavailable", detail="path is invalid")
    try:
        done = subprocess.run(
            [sys.executable, "-c", _STAT_CODE, path],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return FileView("unavailable", detail="file check timed out")
    except OSError as exc:
        return FileView("unavailable", detail=_short(exc) or "file check unavailable")
    line = (done.stdout or "").strip().split()
    if not line:
        return FileView("unavailable", detail="file check returned nothing")
    if line[0] == "missing":
        return FileView("missing")
    if line[0] == "error":
        if len(line) > 1 and line[1] == "2":
            return FileView("missing")
        return FileView("unavailable", detail="file check failed")
    if line[0] == "other":
        return FileView("other")
    if line[0] != "file" or len(line) < 5:
        return FileView("unavailable", detail="file check returned an unexpected result")
    try:
        return FileView("file", dev=int(line[1]), ino=int(line[2]), size=int(line[3]), mtime_ns=int(line[4]))
    except ValueError:
        return FileView("unavailable", detail="file check returned an unexpected result")


def read_process_start(pid: int, *, timeout: float = PS_TIMEOUT) -> str | None:
    """Return the process start stamp, or None if that pid is not running.

    The stamp is whatever ``ps`` prints for ``lstart``. It is compared with a
    later read from the same command, so a reused pid does not look like the
    process that was watched.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or pid > 2**31 - 1:
        raise ValueError("pid must be an integer greater than 1")
    try:
        done = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,lstart="],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("ps timed out") from exc
    text = (done.stdout or "").strip()
    if not text:
        return None
    pid_text, _, start = text.partition(" ")
    try:
        if int(pid_text) != pid:
            return None
    except ValueError:
        return None
    start = " ".join(start.split())
    return start or None


def probe_url(url: str, *, timeout: float = URL_TIMEOUT, deadline: float = URL_DEADLINE,
              max_redirects: int = URL_MAX_REDIRECTS, max_bytes: int = URL_MAX_BYTES) -> tuple[int, str]:
    """GET a URL with a hard deadline, a redirect cap, and a body cap.

    Returns ``(status, final_url)``. Raises ``TimeoutError``, ``OSError``, or
    ``ValueError`` when the URL does not answer within the bounds. The body is
    discarded; it is never returned to the caller.
    """
    import time
    current = url
    origin = _origin(url)
    end = time.monotonic() + deadline
    redirects = 0
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0.05:
            raise TimeoutError("url deadline exceeded")
        status, location = _one_hop(current, timeout=min(timeout, remaining), max_bytes=max_bytes)
        if status in (301, 302, 303, 307, 308) and location:
            if redirects >= max_redirects:
                raise ValueError("too many redirects")
            nxt = urljoin(current, location)
            parts = urlsplit(nxt)
            if parts.scheme not in ("http", "https") or not parts.hostname:
                raise ValueError("redirect left http")
            if parts.username or parts.password:
                raise ValueError("redirect carried credentials")
            if _origin(nxt) != origin:
                raise ValueError("redirect left the original origin")
            current = nxt
            redirects += 1
            continue
        return status, current


def _one_hop(url: str, *, timeout: float, max_bytes: int) -> tuple[int, str | None]:
    """One GET. Redirects are not followed here; the caller counts them."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("url is not an http(s) url without credentials")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = path + "?" + parts.query
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(parts.hostname, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=timeout)
    try:
        conn.request("GET", path, headers={
            "Accept": "*/*",
            "User-Agent": "docket-watch",
            "Connection": "close",
        })
        response = conn.getresponse()
        response.read(max_bytes)
        return response.status, response.getheader("Location")
    except http.client.HTTPException as exc:
        raise OSError(_short(exc) or "url request failed") from exc
    finally:
        conn.close()


def _origin(url: str) -> tuple[str, str, int]:
    """Scheme, host, and port. A different port is a different origin."""
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    if parts.port:
        port = parts.port
    elif scheme == "https":
        port = 443
    else:
        port = 80
    return scheme, host, port


def _norm(value: str) -> str:
    return " ".join(value.split())


def _short(exc) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:180]


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
