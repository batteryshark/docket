"""Optional OpenChamber archive overlay.

OpenChamber stores archives in ``sessions-archive.json``. OpenCode's
``time.archived`` does not reflect that overlay. This reader is optional: a
missing file means there is no overlay, and nothing here starts OpenChamber
or writes its settings. The scheduler does not import this module.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def default_archive_path() -> Path:
    override = os.environ.get("DOCKET_OPENCHAMBER_ARCHIVE")
    if override:
        return Path(override).expanduser()
    root = os.environ.get("OPENCHAMBER_DATA_DIR")
    base = Path(root).expanduser() if root else Path("~/.config/openchamber").expanduser()
    return base / "sessions-archive.json"


class OpenChamberArchive:
    """Read-only view of OpenChamber's archive overlay.

    ``check`` returns ``archived``, ``clear``, or ``unreadable``. A failed
    read is not treated as clear: OpenChamber itself does not treat a failed
    read as evidence that nothing is archived.
    """

    def __init__(self, path: Path | None = None):
        self.path = path if path is not None else default_archive_path()

    def check(self, session_id: str) -> str:
        if not session_id:
            return "clear"
        try:
            raw = self.path.read_text()
        except FileNotFoundError:
            return "clear"
        except OSError:
            return "unreadable"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return "unreadable"
        if not isinstance(parsed, dict):
            return "unreadable"
        value = parsed.get(session_id)
        # OpenChamber: archived only when the stored value is a number.
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "clear"
        if value > 0:
            return "archived"
        return "clear"
