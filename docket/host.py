"""Optional desktop-origin adapter; generic OpenCode remains the unconfigured default.

Before a paired call, require exactly Docket's stored active client record:
same id, with no expiry and no revocation. Display names/kind are not identity.
Anything else is a visible refusal and does not fall back to the older OpenCode service or to
an unauthenticated request. The check detects revocation. It does not prove
the host requires authentication. Password-free loopback can accept a request
without a valid bearer; that optional success is not a reason to reject the
host. No live pairing is performed here.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from docket.delivery import DeliveryError, OpenCodeAdapter
from docket.pairing import (
    PairingError, RecordRefusal, health, json_request, read_private, require_own_record, state_dir,
    valid_client_id, valid_client_token,
)


def desktop_origin(settings: Path | None = None) -> str:
    path = settings or Path("~/.config/openchamber/settings.json").expanduser()
    try:
        # No credential fields are used, copied, reported, or persisted.
        port = json.loads(path.read_text()).get("desktopLocalPort")
        if type(port) is not int or not 0 < port < 65536:
            raise ValueError
    except (OSError, ValueError, AttributeError):
        raise PairingError("OpenChamber desktopLocalPort unavailable") from None
    return f"http://127.0.0.1:{port}"


class OpenChamberAdapter(OpenCodeAdapter):
    name = "openchamber"

    def __init__(self, *, paired=True, root=None, settings=None, timeout=10):
        super().__init__(timeout=timeout)
        self.paired = paired
        self.root = root if root is not None else state_dir()
        self.settings = settings
        self._status = {"adapter": self.name, "mode": "paired" if paired else "unpaired-loopback",
                        "origin": None, "state": "not_checked",
                        "host_enforced_authentication": "not proven"}

    def host_status(self):
        return dict(self._status)

    def _request(self, method, path, body=None):
        try:
            self._status["checked_at"] = datetime.now(timezone.utc).isoformat()
            self._status["origin"] = None
            if _configured(self.root):
                self.paired = True
                self._status["mode"] = "paired"
            # Rediscover on EVERY call, not once at daemon launch; never consult OpenCode credentials.
            origin = desktop_origin(self.settings)
            self._status["origin"] = origin
            health(origin)
            token = None
            if self.paired:
                binding = read_private(self.root / "client.json")
                token = binding.get("clientToken")
                if (binding.get("version") != 1 or not valid_client_token(token)
                        or not valid_client_id(binding.get("clientId"))):
                    raise PairingError("invalid Docket client binding")
                status, result = json_request(origin, "GET", "/api/client-auth/clients", token=token,
                                              timeout=self.timeout)
                clients = result.get("clients") if isinstance(result, dict) else None
                if status != 200:
                    raise RecordRefusal("OpenChamber did not return Docket's own client record; no fallback")
                require_own_record(clients, binding)
            self.calls.append(f"{method} {path}")
            status, result = json_request(origin, method, path, body, token, timeout=self.timeout)
            if status in (401, 403) or 300 <= status < 400:
                raise PairingError(f"OpenChamber refused request ({status}); no fallback")
            self._status["state"] = "ready" if status < 500 else "unavailable"
            self._status.pop("error", None)
            return status, result
        except RecordRefusal as exc:
            self._status["state"] = "refused"
            self._status["error"] = exc.detail
            # The scheduler and panel show this detail. It names the refusal and
            # must not contain a token, link, or host body.
            raise DeliveryError("refused", exc.detail, retryable=False) from None
        except (PairingError, OSError, ValueError) as exc:
            self._status["state"] = "refused"
            self._status["error"] = exc.detail if isinstance(exc, PairingError) else "private client binding unavailable"
            # Deliberately no exception/response interpolation: never leak a token.
            raise DeliveryError("unavailable", "OpenChamber host/client check failed; no fallback (see host status)",
                                retryable=True) from None

    def deliver(self, *args, **kwargs):
        receipt = super().deliver(*args, **kwargs)
        receipt.raw["host"] = self.host_status()
        return receipt


def configured_adapter(archive=None):
    root = state_dir()
    mode = os.environ.get("DOCKET_DELIVERY_HOST", "auto")
    # An identity means enrollment was attempted: do not silently route elsewhere after a missing binding.
    configured = _configured(root)
    if configured or mode == "openchamber-paired":
        return OpenChamberAdapter(root=root)
    if mode == "openchamber-unpaired":
        return OpenChamberAdapter(paired=False, root=root)
    if mode not in ("auto", "opencode"):
        raise PairingError("invalid DOCKET_DELIVERY_HOST")
    return AutoAdapter(root=root, archive=archive)


def _configured(root):
    return any((root / name).exists() or (root / name).is_symlink()
               for name in ("identity.json", "client.json"))


class AutoAdapter(OpenCodeAdapter):
    """Latch into paired delivery when enrollment appears, including in a running daemon."""

    def __init__(self, *, root, archive=None):
        super().__init__(archive=archive)
        self.root = root
        self.desktop = None

    def _request(self, method, path, body=None):
        if self.desktop is None and _configured(self.root):
            self.desktop = OpenChamberAdapter(root=self.root)
        if self.desktop is not None:
            return self.desktop._request(method, path, body)
        return super()._request(method, path, body)

    def host_status(self):
        return self.desktop.host_status() if self.desktop else super().host_status()
