"""Owned, per-user systemd service. No state-store imports or service discovery."""

from __future__ import annotations

import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
import unicodedata

UNIT = "docket.service"
OWNER = "# Managed by Docket: systemd-user-v1\n"
LIMIT = 8192
TIMEOUT = 9  # Reserve one second for kill/reap; total call budget <= 10 seconds.
PROPERTIES = "LoadState,UnitFileState,ActiveState,SubState,FragmentPath,DropInPaths"


class ServiceError(Exception):
    pass


def _quote(value: str, *, command: bool = False) -> str:
    # Reject controls (including newlines/NUL), surrogates and ambiguous paths.
    if not value or any(unicodedata.category(c).startswith("C") for c in value):
        raise ServiceError("unrepresentable installation path")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def _path(value: str) -> str:
    # Reject controls (including newlines/NUL), surrogates, non-absolute paths and line continuations.
    if not value or not value.startswith("/") or any(unicodedata.category(c).startswith("C") for c in value):
        raise ServiceError("unrepresentable installation path")
    # systemd trims trailing whitespace from path directives, even when valid.
    if value.endswith("\\") or value[-1].isspace():
        raise ServiceError("unrepresentable installation path")
    return value.replace("%", "%%")


def render_unit(python: Path, root: Path, home: Path, state: Path) -> str:
    """Pure renderer; callers must supply trusted absolute deployment paths."""
    if not all(p.is_absolute() for p in (python, root, home, state)):
        raise ServiceError("installation paths must be absolute")
    return OWNER + f"""[Unit]
Description=Docket per-user scheduler
StartLimitIntervalSec=60
StartLimitBurst=5

[Service]
Type=simple
ExecStart={_quote(str(python), command=True)} -B -m docket daemon
WorkingDirectory={_path(str(root))}
Environment={_quote('HOME=' + str(home))}
Environment={_quote('DOCKET_HOME=' + str(state))}
Environment={_quote('PATH=' + str(home / '.local/bin') + ':/usr/local/bin:/usr/bin:/bin')}
UMask=0077
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _call(*args: str) -> tuple[int, str]:
    """Bound both elapsed time and output; never echo systemctl diagnostics/secrets."""
    deadline = time.monotonic() + TIMEOUT
    try:
        process = subprocess.Popen(
            ["systemctl", "--user", "--no-pager", *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )
        try:
            output = bytearray()
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ServiceError("systemctl timed out")
                    if not selector.select(remaining):
                        raise ServiceError("systemctl timed out")
                    chunk = os.read(process.stdout.fileno(), min(4096, LIMIT + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > LIMIT:
                        raise ServiceError("systemctl output limit exceeded")
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
            return code, output.decode("utf-8", errors="replace")
        finally:
            try:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=1)
            finally:
                process.stdout.close()
    except (OSError, subprocess.TimeoutExpired):
        raise ServiceError("systemctl unavailable or timed out") from None


def _checked(*args: str) -> None:
    code, _ = _call(*args)
    if code:
        raise ServiceError("systemctl operation failed")


def _show() -> dict[str, str]:
    code, text = _call("show", UNIT, "--property=" + PROPERTIES)
    values = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if not values.get("LoadState") or (code and values["LoadState"] != "not-found"):
        raise ServiceError("cannot query user service manager")
    return values


def _owned(path: Path) -> bool:
    if path.is_symlink():
        raise ServiceError("refusing a symlink unit")
    if not path.exists():
        return False
    with path.open("rb") as stream:
        if stream.read(len(OWNER)) != OWNER.encode():
            raise ServiceError("refusing an unowned unit")
    return True


def _registration(values: dict[str, str], path: Path) -> None:
    fragment = values.get("FragmentPath", "")
    if (fragment and fragment != str(path)) or values.get("DropInPaths"):
        raise ServiceError("refusing an unowned registration or unit overrides")
    if values["LoadState"] != "not-found" and not fragment:
        raise ServiceError("refusing an unverifiable registration")


def installation() -> str:
    """Explicit operator attestation: never auto-install from the current checkout."""
    python = os.environ.get("DOCKET_SERVICE_PYTHON", "")
    root = os.environ.get("DOCKET_SERVICE_ROOT", "")
    if not python or not root:
        raise ServiceError("install requires trusted persistent DOCKET_SERVICE_PYTHON and DOCKET_SERVICE_ROOT")
    paths = [Path(p) for p in (python, root)]
    _quote(str(paths[0]))
    _path(str(paths[1]))
    for path in paths:
        if not path.is_absolute() or ".." in path.parts:
            raise ServiceError("installation paths must be absolute and normalized")
        # Check lexical AND resolved locations: a temporary venv's Python may
        # resolve to a perfectly persistent system executable.
        for candidate in (path, path.resolve()):
            if (any(candidate.is_relative_to(base) for base in
                    (Path(tempfile.gettempdir()).resolve(), Path('/tmp'), Path('/var/tmp'), Path('/run')))
                    or any(part in ("wt", ".worktrees") for part in candidate.parts)
                    or any((parent / ".git").is_file() for parent in (candidate, *candidate.parents))):
                raise ServiceError("installation must not use a temporary directory or worktree")
    if not paths[0].is_file() or not os.access(paths[0], os.X_OK):
        raise ServiceError("persistent Python executable missing")
    if not (paths[1] / "docket" / "__main__.py").is_file():
        raise ServiceError("persistent Docket package missing")
    home = Path.home()
    state = Path(os.environ.get("DOCKET_HOME", str(home / ".docket"))).expanduser()
    # Preserve a virtualenv's executable path (resolving its symlink loses the venv).
    return render_unit(paths[0], paths[1].resolve(), home, state)


def run(action: str) -> int:
    try:
        if action not in ("install", "status", "start", "stop", "restart", "uninstall"):
            raise ServiceError("unsupported service action")
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        if not config.is_absolute():
            raise ServiceError("XDG_CONFIG_HOME must be absolute")
        path = config / "systemd/user" / UNIT
        owned = _owned(path)
        values = _show()
        if action == "status":
            installed = "installed" if owned else "missing"
            enabled = values.get("UnitFileState") or "unknown"
            active = values.get("ActiveState") or "unknown"
            # Only allow known state labels into diagnostics; no raw manager output.
            enabled = enabled if enabled in ("enabled", "enabled-runtime", "disabled", "static", "masked", "linked", "linked-runtime") else "unknown"
            active = active if active in ("active", "inactive", "failed", "activating", "deactivating") else "unknown"
            load = values["LoadState"]
            load = load if load in ("loaded", "not-found", "error", "bad-setting", "masked", "merged", "stub") else "unknown"
            print(f"docket service: {installed}; enabled={enabled}; active={active}; load={load}")
            return 0 if owned and values["LoadState"] == "loaded" and active != "failed" else 1
        _registration(values, path)
        if action == "install":
            content = installation()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation for a missing unit; never clobber another writer.
            if owned:
                if path.read_text() != content:
                    raise ServiceError("owned unit differs; uninstall before changing deployment")
            else:
                with path.open("x") as stream:
                    os.chmod(path, 0o600)
                    stream.write(content)
            _checked("daemon-reload")
            _checked("enable", "--now", UNIT)
        elif action == "uninstall":
            if owned:
                _checked("stop", UNIT)
                _checked("disable", UNIT)
                if not _owned(path):
                    raise ServiceError("unit changed during uninstall")
                path.unlink()
                _checked("daemon-reload")
            elif values["LoadState"] != "not-found":
                raise ServiceError("refusing registration without an owned unit")
        else:
            if not owned:
                raise ServiceError("unit missing; run: docket service install")
            _checked(action, UNIT)
        print(f"docket service: {action} complete (user state retained)")
        return 0
    except ServiceError as exc:
        print(f"docket service: {exc}")
        return 1
    except (OSError, ValueError):
        # Do not expose arbitrary paths, environment values or manager diagnostics.
        print("docket service: operation refused or failed; check owned unit, persistent install paths and user manager")
        return 1
