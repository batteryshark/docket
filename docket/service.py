"""The ``local.docket`` user LaunchAgent.

Stop with ``docket service stop`` (bootout, plist kept) or
``docket service uninstall`` (bootout and delete the plist).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "local.docket"
PLIST = Path("~/Library/LaunchAgents/local.docket.plist").expanduser()

def _domain() -> str:
    return f"gui/{os.getuid()}"


def _target() -> str:
    return f"{_domain()}/{LABEL}"


def _install_plist() -> bytes:
    from docket.store import home

    # Pin this running interpreter, not the launcher's PATH-dependent default.
    # Do not resolve its symlink: that would discard a virtualenv's identity.
    exe = Path(sys.executable)
    if (sys.version_info < (3, 11) or not exe.is_absolute()
            or not exe.is_file() or not os.access(exe, os.X_OK)):
        raise ValueError("unsupported interpreter")
    raw = os.environ.get("DOCKET_HOME", "~/.docket")
    if not raw or not Path(raw).expanduser().is_absolute():
        raise ValueError("unsupported DOCKET_HOME")
    mode = os.environ.get("DOCKET_DELIVERY_HOST", "auto")
    if mode not in ("auto", "opencode", "openchamber-paired", "openchamber-unpaired"):
        raise ValueError("unsupported DOCKET_DELIVERY_HOST")
    state = home().resolve()
    path = os.environ.get("PATH", "")
    for extra in (str(Path.home() / ".local/bin"), "/opt/homebrew/bin",
                  "/usr/local/bin", "/usr/bin", "/bin"):
        if extra not in path.split(":"):
            path = f"{path}:{extra}" if path else extra
    env = {"PATH": path, "HOME": str(Path.home()),
           "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("USER", ""),
           "DOCKET_HOME": str(state), "DOCKET_DELIVERY_HOST": mode}
    return plistlib.dumps({
        "Label": LABEL, "ProgramArguments": [str(exe), "-m", "docket", "daemon"],
        "WorkingDirectory": str(Path(__file__).resolve().parents[1]),
        "RunAtLoad": True, "KeepAlive": True, "EnvironmentVariables": env,
        "StandardOutPath": str(state / "daemon.out.log"),
        "StandardErrorPath": str(state / "daemon.err.log"),
    })


def run(action: str) -> int:
    if action == "install":
        try:
            data = _install_plist()
        except (OSError, ValueError, RuntimeError):
            print("docket service: unsupported interpreter/state/delivery configuration; no install")
            return 1
        PLIST.parent.mkdir(parents=True, exist_ok=True)
        PLIST.write_bytes(data)
        subprocess.run(["launchctl", "bootout", _target()], capture_output=True)
        done = subprocess.run(
            ["launchctl", "bootstrap", _domain(), str(PLIST)],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            print(done.stderr.strip() or done.stdout.strip() or "docket service: bootstrap failed")
            return done.returncode
        print(f"docket service: installed and started {PLIST}")
        return 0
    if action == "uninstall":
        subprocess.run(["launchctl", "bootout", _target()], capture_output=True)
        PLIST.unlink(missing_ok=True)
        print("docket service: uninstalled")
        return 0
    if action == "stop":
        done = subprocess.run(
            ["launchctl", "bootout", _target()], capture_output=True, text=True,
        )
        if done.returncode != 0:
            print(done.stderr.strip() or "docket service: not loaded")
            return done.returncode
        print("docket service: stopped (plist kept; start loads it again)")
        return 0
    if action == "start":
        if not PLIST.exists():
            print("docket service: plist absent; run: docket service install")
            return 1
        loaded = subprocess.run(
            ["launchctl", "print", _target()], capture_output=True,
        )
        if loaded.returncode != 0:
            done = subprocess.run(
                ["launchctl", "bootstrap", _domain(), str(PLIST)],
                capture_output=True, text=True,
            )
        else:
            done = subprocess.run(
                ["launchctl", "kickstart", _target()],
                capture_output=True, text=True,
            )
        if done.returncode != 0:
            print(done.stderr.strip() or done.stdout.strip() or "docket service: start failed")
            return done.returncode
        print("docket service: started")
        return 0
    if action == "restart":
        done = subprocess.run(
            ["launchctl", "kickstart", "-k", _target()],
            capture_output=True, text=True,
        )
        print(done.stdout.strip() or done.stderr.strip() or f"docket service: restarted {LABEL}")
        return done.returncode
    out = subprocess.run(
        ["launchctl", "print", _target()], capture_output=True, text=True,
    )
    if out.returncode != 0:
        present = "present" if PLIST.exists() else "absent"
        print(f"docket service: not loaded ({PLIST} {present})")
        return 1
    line = next(
        (ln.strip() for ln in out.stdout.splitlines() if "pid =" in ln or "state =" in ln),
        "loaded",
    )
    print(f"docket service: {line}")
    return 0
