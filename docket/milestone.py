"""Own-row milestone helper; identity must come from a trusted native wrapper.

Environment identity is trusted-process input, not cryptographic authentication.
No scheduling, database, reenrollment, state change, or generic file-write tool.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from docket.checkins import parse_roster, stamp
from docket.parse import validate_session_id


def _private(view, mode, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (not kind(view.st_mode) or view.st_uid != os.getuid()
            or stat.S_IMODE(view.st_mode) != mode or (not directory and view.st_nlink != 1)):
        raise ValueError("private owned path required")


def _identity(view):
    return (view.st_dev, view.st_ino, view.st_size, view.st_mtime_ns, view.st_ctime_ns,
            view.st_mode, view.st_uid, view.st_gid)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _read(directory_fd, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as file:
        before = os.fstat(file.fileno())
        _private(before, 0o600)
        raw = file.read(256001)
        if len(raw) > 256000 or _identity(before) != _identity(os.fstat(file.fileno())):
            raise ValueError("roster changed or too large")
        return raw, before


def update_own(path, *, session_id, directory, milestone_at, milestone_ref,
               expected=None, now=None):
    """Expected-bytes replacement, serialized for cooperating writers.

    Final reread detects observed conflicts; no CAS guarantee against an arbitrary
    noncooperating writer in the final read/rename interval. Caller controls no state.
    """
    if (not isinstance(session_id, str) or validate_session_id(session_id)
            or not isinstance(directory, str) or not directory.startswith("/") or "\x00" in directory):
        raise ValueError("trusted session/Location required")
    if not isinstance(milestone_ref, str) or not milestone_ref.strip() or len(milestone_ref) > 1000:
        raise ValueError("accepted result reference required")
    if isinstance(milestone_at, str) and milestone_at.endswith("Z"):
        milestone_at = milestone_at[:-1] + "+00:00"
    at = stamp(milestone_at)
    if at > (now or datetime.now(timezone.utc)):
        raise ValueError("future milestone refused")
    path = Path(path)
    if not path.is_absolute() or path.name != "lead-checkins.jsonl":
        raise ValueError("fixed roster path required")
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary, lock = None, None
    try:
        parent = os.fstat(dfd)
        _private(parent, 0o700, directory=True)
        lock = os.open(".lead-checkins.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                       0o600, dir_fd=dfd)
        _private(os.fstat(lock), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        raw, before = _read(dfd, path.name)
        if expected is not None and raw != expected:
            raise ValueError("expected bytes changed")
        # Refuse ambiguous objects rather than dropping an original field.
        for line in raw.decode().splitlines():
            if line.strip():
                value = json.loads(line, object_pairs_hook=_unique)
                if not isinstance(value, dict):
                    raise ValueError("roster row must be an object")
        row = parse_roster(raw.decode())[session_id]
        if row["directory"] != directory:
            raise ValueError("wrong enrolled directory")
        if row.get("milestone_at") and at < stamp(row["milestone_at"]):
            raise ValueError("backward milestone refused")
        if row.get("milestone_at") == milestone_at and row.get("milestone_ref") == milestone_ref:
            return {"status": "unchanged"}
        lines = raw.decode().splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() and json.loads(line)["session"] == session_id:
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                lines[index] = json.dumps(dict(row, milestone_at=milestone_at, milestone_ref=milestone_ref),
                                         ensure_ascii=False, separators=(",", ":")) + ending
                break
        replacement = "".join(lines).encode()
        parse_roster(replacement.decode())
        fd, temporary = tempfile.mkstemp(prefix=".milestone-", dir=path.parent)
        os.close(fd)
        # Fixed macOS command preserves mode/owner/ACL/xattrs, unlike new-file JSON
        # serialization. No event-provided paths/commands; source is rechecked below.
        subprocess.run(["/bin/cp", "-p", str(path), temporary], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(temporary, "r+b") as file:
            view = os.fstat(file.fileno())
            _private(view, 0o600)
            if (view.st_uid, view.st_gid) != (before.st_uid, before.st_gid):
                raise ValueError("ownership changed")
            file.write(replacement)
            file.truncate()
            file.flush()
            os.fsync(file.fileno())
        current, after = _read(dfd, path.name)
        if current != raw or _identity(after) != _identity(before):
            raise ValueError("roster conflict")
        latest_parent = path.parent.lstat()
        _private(latest_parent, 0o700, directory=True)
        if (latest_parent.st_dev, latest_parent.st_ino) != (parent.st_dev, parent.st_ino):
            raise ValueError("directory conflict")
        os.replace(Path(temporary).name, path.name, src_dir_fd=dfd, dst_dir_fd=dfd)
        temporary = None
        os.fsync(dfd)
        return {"status": "recorded", "receipt_sha256": hashlib.sha256(replacement).hexdigest()}
    finally:
        if temporary is not None:
            os.unlink(temporary)
        if lock is not None:
            os.close(lock)
        os.close(dfd)


def main():
    try:
        if len(sys.argv) != 1:
            raise ValueError("no command-line selectors")
        raw = sys.stdin.buffer.read(8193)
        if len(raw) > 8192:
            raise ValueError("input too large")
        value = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(value, dict) or set(value) != {"milestone_at", "milestone_ref"}:
            raise ValueError("only milestone fields allowed")
        result = update_own(Path.home() / ".docket" / "lead-checkins.jsonl",
                            session_id=os.environ.get("DOCKET_CALLER_SESSION"),
                            directory=os.environ.get("DOCKET_CALLER_DIRECTORY"), **value)
        print(json.dumps(result))
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        print(json.dumps({"status": "refused", "reason": "check trusted identity, enrolled row, private mode, time or conflict"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
